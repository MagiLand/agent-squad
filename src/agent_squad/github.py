"""GitHub subprocess transport, wire validation, and publication mechanics."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
import re
import shutil
import subprocess
import time
from urllib.parse import quote, unquote, urlencode, urlsplit

from .forge import (
    CHECK_REFUSALS, Anchor, Check, CheckResult, ChecksUnavailable,
    ClosingPullRequest, Comment, Evidence, ForgeError, IssueDependencies,
    IssueQuery, IssueRecord, ListedIssue, MergeResult, Milestone,
    PullRequest, RelatedIssue, Review, ReviewPublication, ReviewState, Role,
    Snapshot, ThreadState, V, array, boolean, branch_present, closing_order,
    delete_and_confirm, exposed, milestone_title, object_value, oid,
    parse_milestone, positive, related_issue, text_value,
)
from .initialization import Repository, decode_json

# With delete_branch_on_merge set, GitHub deletes the merged head branch a few
# seconds after answering the merge request (#120). Both values are seconds.
AUTO_DELETION_WAIT = 30.0
AUTO_DELETION_POLL = 1.0


STATES = {
    "APPROVED": ReviewState.APPROVED,
    "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
    "COMMENTED": ReviewState.COMMENTED,
    "PENDING": ReviewState.PENDING,
    "DISMISSED": ReviewState.COMMENTED,
}
EVENTS = {
    ReviewState.APPROVED: "APPROVE",
    ReviewState.CHANGES_REQUESTED: "REQUEST_CHANGES",
    ReviewState.COMMENTED: "COMMENT",
}


def review_state(value: str) -> tuple[ReviewState, bool]:
    if value not in STATES:
        raise ForgeError(f"unknown review state: {value}")
    return STATES[value], value == "DISMISSED"


def review_event(state: ReviewState) -> str:
    try:
        return EVENTS[state]
    except KeyError:
        raise ForgeError(f"cannot publish review state: {state}") from None


def anchor_payload(anchor: Anchor) -> dict[str, object]:
    result: dict[str, object] = {
        "path": anchor.path, "line": anchor.line, "side": "RIGHT",
    }
    if anchor.start_line is not None:
        result.update(start_line=anchor.start_line, start_side="RIGHT")
    return result


def parse_evidence(value: object, *, review: bool = False) -> Evidence:
    data = object_value(value, "evidence")
    user = object_value(data.get("user"), "evidence.user")
    timestamp = (
        data.get("submitted_at") if review else data.get("created_at")
    )
    if review and data.get("state") == "PENDING":
        timestamp = data.get("created_at") or "1970-01-01T00:00:00Z"
    return Evidence(
        positive(data.get("id"), "evidence.id"),
        V.require_string(user.get("login"), "evidence.user.login"),
        V.require_timestamp(timestamp, "evidence.timestamp"),
        text_value(
            "" if data.get("body") is None else data["body"],
            "evidence.body",
        ).replace("\r\n", "\n"),
    )


def parse_review(value: object) -> Review:
    data = object_value(value, "review")
    state = V.require_string(data.get("state"), "review.state")
    normalized, dismissed = review_state(state)
    return Review(
        parse_evidence(data, review=True),
        oid(data.get("commit_id"), "review.commit_id"),
        normalized, dismissed, state,
    )


def parse_comment(value: object) -> Comment:
    data = object_value(value, "review comment")
    numbers = {}
    for key in ("in_reply_to_id", "line", "start_line"):
        value = data.get(key)
        # Outdated roots retain their anchor at the opening review's head.
        if value is None and key in ("line", "start_line"):
            value = data.get(f"original_{key}")
        numbers[key] = None if value is None else positive(value, key)
    strings = {
        key: (
            None
            if data.get(key) is None
            else V.require_string(data[key], key)
        )
        for key in ("path", "side")
    }
    return Comment(
        parse_evidence(data),
        positive(
            data.get("pull_request_review_id"), "pull_request_review_id"
        ),
        numbers["in_reply_to_id"],
        strings["path"],
        numbers["line"],
        numbers["start_line"],
        strings["side"],
    )


def parse_issue(value: object) -> IssueRecord:
    data = object_value(value, "issue")
    return {
        "number": positive(data.get("number"), "issue.number"),
        "title": text_value(data.get("title"), "issue.title"),
        "state": text_value(data.get("state"), "issue.state"),
        "is_pull_request": "pull_request" in data,
        "body": parse_evidence(data).body,
        "labels": [
            V.require_string(
                object_value(label, "label").get("name"), "label.name"
            )
            for label in array(data.get("labels"), "labels")
        ],
        "milestone": milestone_title(data.get("milestone"), "issue.milestone"),
        "comments": (),
    }


def parse_related_issue(value: object) -> RelatedIssue:
    """Read a dependency row; ``repository_url`` names its repository."""
    data = object_value(value, "related issue")
    url = text_value(data.get("repository_url"), "repository_url")
    parts = urlsplit(url).path.split("/")
    if len(parts) != 4 or parts[:2] != ["", "repos"] or not all(parts[2:]):
        raise ForgeError("repository_url must name a repository")
    return related_issue(
        data.get("number"), data.get("title"), data.get("state"),
        f"{unquote(parts[2])}/{unquote(parts[3])}",
    )


def parse_pullrequest(value: object) -> PullRequest:
    data = object_value(value, "pull request")
    head = object_value(data.get("head"), "pull request head")
    base = object_value(data.get("base"), "pull request base")
    state = data.get("state")
    if state not in ("open", "closed"):
        raise ForgeError("pull request state must be open or closed")
    merged = boolean(data.get("merged"), "merged")
    merge_commit = data.get("merge_commit_sha")
    if merge_commit is not None:
        merge_commit = oid(merge_commit, "merge_commit_sha")
    mergeable = data.get("mergeable_state")
    if mergeable is not None:
        mergeable = text_value(mergeable, "mergeable_state")
    return PullRequest(
        parse_evidence(data),
        positive(data.get("number"), "PR number"),
        text_value(data.get("title"), "PR title"),
        oid(head.get("sha"), "head.sha"),
        V.require_string(head.get("ref"), "head.ref"),
        oid(base.get("sha"), "base.sha"),
        V.require_string(base.get("ref"), "base.ref"),
        state,
        merged,
        merge_commit,
        mergeable,
    )


def optional_text(value: object, label: str) -> str | None:
    """Read a field that may be absent, null, or empty as null."""
    return None if value in (None, "") else text_value(value, label)


def check_run_result(status: str, conclusion: str | None) -> CheckResult:
    """Classify a check run; a conclusion not named here counts as failed."""
    if status != "completed":
        return "pending"
    if conclusion in ("success", "neutral"):
        return "passed"
    return "skipped" if conclusion == "skipped" else "failed"


# A commit status state not named here counts as failed (#145).
STATUS_RESULTS: dict[str, CheckResult] = {
    "success": "passed", "pending": "pending",
}


def parse_check_run(value: object, events: dict[int, str]) -> Check:
    data = object_value(value, "check run")
    status = text_value(data.get("status"), "check_run.status")
    conclusion = data.get("conclusion")
    if conclusion is not None:
        conclusion = text_value(conclusion, "check_run.conclusion")
    suite = data.get("check_suite")
    suite_id = None if suite is None else positive(
        object_value(suite, "check_suite").get("id"), "check_suite.id"
    )
    app = data.get("app")
    timestamps = {
        key: (None if data.get(key) is None
              else V.require_timestamp(data[key], f"check_run.{key}"))
        for key in ("started_at", "completed_at")
    }
    return Check(
        text_value(data.get("name"), "check_run.name"),
        # Only the owning workflow run states an event; never a name.
        events.get(suite_id),
        status if conclusion is None else f"{status}/{conclusion}",
        check_run_result(status, conclusion),
        None if app is None else V.require_string(
            object_value(app, "check_run.app").get("slug"), "app.slug"
        ),
        optional_text(data.get("html_url"), "check_run.html_url"),
        timestamps["started_at"],
        timestamps["completed_at"],
    )


def parse_commit_status(value: object) -> Check:
    data = object_value(value, "commit status")
    state = text_value(data.get("state"), "status.state")
    creator = data.get("creator")
    return Check(
        text_value(data.get("context"), "status.context"),
        None,
        state,
        STATUS_RESULTS.get(state, "failed"),
        None if creator is None else V.require_string(
            object_value(creator, "status.creator").get("login"),
            "status.creator.login",
        ),
        optional_text(data.get("target_url"), "status.target_url"),
        None,
        None,
    )


def ordered(checks: list[Check]) -> tuple[Check, ...]:
    return tuple(sorted(
        checks, key=lambda check: (check.name, check.event or "")
    ))


# One GraphQL read of the PR record and every list behind `status` (#149).
# Each node is rewritten into the record the REST read returns, so the REST
# parsers above validate both reads.
PAGE_SIZE = 100


@dataclass(frozen=True)
class Connection:
    """One paginated GraphQL list, read to its last page.

    ``owner`` is the GraphQL type holding the list; a further page is read
    through ``node(id:)`` with the owner's ID, which every node that holds a
    nested list selects.
    """

    field: str
    owner: str
    nodes: str
    nested: tuple[Connection, ...] = ()
    arguments: str = ""


def selection(connection: Connection, *, after: bool = False) -> str:
    arguments = ", ".join(filter(None, (
        f"first:{PAGE_SIZE}", "after:$cursor" if after else "",
        connection.arguments,
    )))
    nodes = " ".join((connection.nodes, *map(selection, connection.nested)))
    return (
        f"{connection.field}({arguments}) {{ nodes {{ {nodes} }}"
        " pageInfo { hasNextPage endCursor } }"
    )


def page_query(connection: Connection) -> str:
    return (
        "query SquadPage($id:ID!, $cursor:String) { node(id:$id) {"
        f" ... on {connection.owner} {{ {selection(connection, after=True)} }}"
        " } }"
    )


ACTOR = "author { __typename login }"
REVIEW_COMMENT = Connection(
    "comments", "", f"fullDatabaseId {ACTOR} body createdAt path line"
    " originalLine startLine originalStartLine replyTo { fullDatabaseId }"
    " pullRequestReview { fullDatabaseId }",
)
REVIEWS = Connection(
    "reviews", "PullRequest",
    f"id fullDatabaseId {ACTOR} body state submittedAt commit {{ oid }}",
    (replace(REVIEW_COMMENT, owner="PullRequestReview"),),
)
THREADS = Connection(
    "reviewThreads", "PullRequest", "id isResolved diffSide",
    (replace(REVIEW_COMMENT, owner="PullRequestReviewThread"),),
)
CONVERSATION = Connection(
    "comments", "PullRequest", f"fullDatabaseId {ACTOR} body createdAt"
)
CHECK_SUITES = Connection(
    "checkSuites", "Commit", "id app { slug } workflowRun { event }",
    (Connection(
        "checkRuns", "CheckSuite", "databaseId name status conclusion"
        " permalink startedAt completedAt",
        arguments="filterBy:{checkType:LATEST}",
    ),),
)
COMMIT = (
    f"id oid {selection(CHECK_SUITES)} status {{ contexts {{ context state"
    " targetUrl creator { __typename login } } }"
)
SNAPSHOT_QUERY = f"""query SquadSnapshot(
  $owner:String!, $repo:String!, $pr:Int!, $head:String!, $checks:Boolean!
) {{
  repository(owner:$owner, name:$repo) {{
    pullRequest(number:$pr) {{
      id fullDatabaseId {ACTOR} body createdAt number title
      headRefOid headRefName baseRefOid baseRefName state merged
      mergeCommit {{ oid }} potentialMergeCommit {{ oid }} mergeStateStatus
      {selection(REVIEWS)}
      {selection(THREADS)}
      {selection(CONVERSATION)}
    }}
    head: object(expression:$head) @include(if:$checks) {{
      ... on Commit {{ {COMMIT} }}
    }}
  }}
}}"""
CHECKS_QUERY = f"""query SquadChecks(
  $owner:String!, $repo:String!, $sha:GitObjectID!
) {{
  repository(owner:$owner, name:$repo) {{
    object(oid:$sha) {{ ... on Commit {{ {COMMIT} }} }}
  }}
}}"""
PR_STATES = {"OPEN": "open", "CLOSED": "closed", "MERGED": "closed"}
# The issue listing of `issue list` (#164). Every request value is a
# variable. `labels:` returns the issues carrying any of the labels and
# `filterBy.milestoneNumber` takes the milestone's number; `issues` holds no
# pull request. The linked pull requests leave out those closed unmerged.
ISSUE_LABELS = Connection("labels", "Issue", "name")
CLOSING_PRS = Connection(
    "closedByPullRequestsReferences", "Issue",
    "number merged repository { nameWithOwner }",
    arguments="includeClosedPrs:false",
)
ISSUES_QUERY = f"""query SquadIssues(
  $owner:String!, $repo:String!, $states:[IssueState!], $labels:[String!],
  $filter:IssueFilters, $cursor:String
) {{
  repository(owner:$owner, name:$repo) {{
    issues(
      first:{PAGE_SIZE}, after:$cursor, states:$states, labels:$labels,
      filterBy:$filter
    ) {{
      nodes {{
        id number title state stateReason milestone {{ title }}
        {selection(ISSUE_LABELS)}
        {selection(CLOSING_PRS)}
      }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}"""
ISSUE_STATES = {"OPEN": "open", "CLOSED": "closed"}
QUERY_STATES = {
    "open": ["OPEN"], "closed": ["CLOSED"], "all": ["OPEN", "CLOSED"],
}


def closing_pull_request(value: object) -> ClosingPullRequest | None:
    """A linked pull request when it is merged; null otherwise."""
    data = object_value(value, "closing pull request")
    if not boolean(data.get("merged"), "closing pull request.merged"):
        return None
    repository = V.require_string(
        object_value(
            data.get("repository"), "closing pull request.repository"
        ).get("nameWithOwner"),
        "closing pull request.repository.nameWithOwner",
    )
    if re.fullmatch(r"[^/]+/[^/]+", repository) is None:
        raise ForgeError("closing pull request repository must be owner/name")
    return {
        "number": positive(data.get("number"), "closing pull request.number"),
        "repository": repository,
    }


def actor(value: object, label: str) -> dict[str, object]:
    """REST's user record for a GraphQL actor.

    REST names a GitHub App's account ``<slug>[bot]`` and an account deleted
    since as ``ghost``; GraphQL gives the bare slug and null.
    """
    if value is None:
        return {"login": "ghost"}
    data = object_value(value, label)
    login = V.require_string(data.get("login"), f"{label}.login")
    if data.get("__typename") == "Bot":
        login += "[bot]"
    return {"login": login}


def database_id(value: object, label: str) -> int:
    """Read a ``fullDatabaseId``, which GraphQL encodes as a decimal string."""
    text = V.require_string(value, label)
    if re.fullmatch(r"[1-9][0-9]*", text) is None:
        raise ForgeError(f"{label} must be a positive decimal ID")
    return int(text)


def lower(value: object) -> object:
    """Lower-case a GraphQL enum; other values reach the REST validators."""
    return value.lower() if isinstance(value, str) else value


def oid_of(value: object, label: str) -> object:
    return None if value is None else object_value(value, label).get("oid")


def rest_evidence(node: dict, label: str) -> dict[str, object]:
    return {
        "id": database_id(node.get("fullDatabaseId"), f"{label}.id"),
        "user": actor(node.get("author"), f"{label}.author"),
        "created_at": node.get("createdAt"),
        "body": node.get("body"),
    }


def rest_pullrequest(node: dict) -> dict[str, object]:
    return {
        **rest_evidence(node, "pullRequest"),
        "number": node.get("number"),
        "title": node.get("title"),
        **{side: {"sha": node.get(f"{side}RefOid"),
                  "ref": node.get(f"{side}RefName")}
           for side in ("head", "base")},
        "state": PR_STATES.get(node.get("state")),
        "merged": node.get("merged"),
        # REST reports the merge commit, or else the latest test merge.
        "merge_commit_sha": oid_of(
            node.get("mergeCommit") or node.get("potentialMergeCommit"),
            "pullRequest.mergeCommit",
        ),
        "mergeable_state": lower(node.get("mergeStateStatus")),
    }


def rest_review(node: dict) -> dict[str, object]:
    record = rest_evidence(node, "review")
    # A REST review has no created_at, which orders a PENDING one.
    del record["created_at"]
    return {
        **record,
        "state": node.get("state"),
        "submitted_at": node.get("submittedAt"),
        "commit_id": oid_of(node.get("commit"), "review.commit"),
    }


def rest_comment(node: dict, thread: dict | None) -> dict[str, object]:
    """REST's record of a review comment read in ``thread``.

    A comment read only under its review gets no anchor, as REST's
    per-review listing reports none.
    """
    review = object_value(node.get("pullRequestReview"), "comment.review")
    reply = node.get("replyTo")
    record = {
        **rest_evidence(node, "comment"),
        "pull_request_review_id": database_id(
            review.get("fullDatabaseId"), "comment.review.id"
        ),
        "in_reply_to_id": None if reply is None else database_id(
            object_value(reply, "comment.replyTo").get("fullDatabaseId"),
            "comment.replyTo.id",
        ),
        "path": node.get("path"),
    }
    if thread is not None:
        record.update(
            line=node.get("line"),
            original_line=node.get("originalLine"),
            start_line=node.get("startLine"),
            original_start_line=node.get("originalStartLine"),
            side=thread.get("diffSide"),
        )
    return record


def rest_check_run(node: dict, suite: int, app: object) -> dict[str, object]:
    return {
        "name": node.get("name"),
        "status": lower(node.get("status")),
        "conclusion": lower(node.get("conclusion")),
        "check_suite": {"id": suite},
        "app": app,
        "html_url": node.get("permalink"),
        "started_at": node.get("startedAt"),
        "completed_at": node.get("completedAt"),
    }


def rest_status(node: dict) -> dict[str, object]:
    creator = node.get("creator")
    return {
        "context": node.get("context"),
        "state": lower(node.get("state")),
        "creator": None if creator is None else actor(
            creator, "status.creator"
        ),
        "target_url": node.get("targetUrl"),
    }


class CommandFailed(ForgeError):
    """A failed ``gh`` command and what it printed on standard output."""

    def __init__(self, message: str, status: int | None, output: str) -> None:
        super().__init__(message, status)
        self.output = output


RESOLVE_MUTATION = """mutation SquadResolve($id:ID!) {
  resolveReviewThread(input:{threadId:$id}) { thread { id isResolved } }
}"""


class GitHub:
    """One process-local forge identity. Mutation calls are never retried."""

    version_label = "forge client"
    credential_label = None
    can_mutate = True
    mutation_unavailable_message = "not supported on this forge"
    can_resolve_threads = True
    can_read_thread_resolution = True
    can_read_branch_rules = True

    def __init__(
        self, repository: Repository, role: Role, *, timeout: float = 45
    ) -> None:
        assert repository.configuration is not None
        self.repository = repository
        self.role = role
        self.account = repository.configuration.account(role)
        forge = repository.configuration.forge
        self.prefix = (
            f'/repos/{quote(forge.owner, safe="")}'
            f'/{quote(forge.repo, safe="")}'
        )
        self.timeout = timeout
        self._token: str | None = None
        self._verified = False
        self.executable = shutil.which("gh")
        if self.executable is None:
            raise ForgeError("gh is not installed or not on PATH")

    def _run(
        self,
        arguments: list[str],
        *,
        authenticated: bool = False,
        body: object = None,
    ) -> str:
        env = os.environ.copy()
        for key in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
        ):
            env.pop(key, None)
        if authenticated:
            env["GH_TOKEN"] = self.token()
        env["GH_PROMPT_DISABLED"] = "1"
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                cwd=self.repository.root,
                env=env,
                shell=False,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=self.timeout,
                input=None if body is None else json.dumps(body),
            )
        except subprocess.TimeoutExpired:
            raise ForgeError(
                "gh call timed out; re-read the PR before repeating a mutation"
            ) from None
        except OSError as error:
            raise ForgeError(f"could not execute gh: {error}") from None
        if result.returncode:
            # Token-resolution output is never included, even on a failing
            # command.
            if arguments[:2] == ["auth", "token"]:
                raise ForgeError(
                    f"cannot resolve token for configured {self.role} account"
                )
            detail = (result.stderr or result.stdout).strip()
            if self._token:
                detail = detail.replace(self._token, "[redacted]")
            status = re.search(r"HTTP (\d{3})", detail)
            raise CommandFailed(
                " ".join(detail.splitlines()),
                int(status[1]) if status else None,
                result.stdout,
            )
        return result.stdout

    def token(self) -> str:
        if self._token is None:
            value = self._run(
                ["auth", "token", "--user", self.account]
            ).strip()
            if not value or "\n" in value or "\r" in value:
                raise ForgeError(f"invalid token response for {self.role}")
            self._token = value
        return self._token

    def version(self) -> str:
        """Report the installed version without pinning a release number."""
        value = self._run(["--version"]).strip()
        if not value:
            raise ForgeError("gh --version returned no version text")
        return value

    def verify_credentials(self) -> None:
        self.token()

    def verify_identity(self) -> None:
        if not self._verified:
            data = object_value(self.api("/user"), "user")
            login = V.require_string(data.get("login"), "user.login")
            if login.casefold() != self.account.casefold():
                raise ForgeError(
                    f"GET /user does not match configured {self.role} account"
                )
            self._verified = True

    def api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        body: object = None,
        mutation: bool | None = None,
    ) -> object:
        mutating = method != "GET" if mutation is None else mutation
        if mutating:
            self.verify_identity()
        output = self._run(
            self.arguments(endpoint, method, body is not None),
            authenticated=True, body=body,
        )
        try:
            value = decode_json(output) if output.strip() else None
        except ValueError:
            raise ForgeError("gh returned invalid JSON") from None
        if (
            endpoint == "graphql"
            and isinstance(value, dict)
            and value.get("errors")
        ):
            raise ForgeError(self.graphql_errors(value["errors"]))
        return value

    @staticmethod
    def arguments(endpoint: str, method: str, body: bool) -> list[str]:
        args = [
            "api",
            endpoint,
            "--hostname",
            "github.com",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        if body:
            args.extend(["--input", "-"])
        return args

    def graphql_errors(self, errors: object) -> str:
        messages = [
            text_value(
                object_value(e, "GraphQL error").get("message"), "message"
            )
            for e in array(errors, "GraphQL errors")
        ]
        detail = "; ".join(messages)
        if self._token:
            detail = detail.replace(self._token, "[redacted]")
        return detail

    def graphql(
        self, query: str, variables: dict[str, object],
        *, optional: str | None = None,
    ) -> dict[str, object]:
        """Send one read query and return its ``data`` (#149).

        ``gh`` exits 1 on a response that carries errors but still prints
        it. Errors that all lie under ``repository.<optional>`` null that
        field; any other error fails the read.
        """
        body = {"query": query, "variables": variables}
        failure = None
        try:
            output = self._run(
                self.arguments("graphql", "POST", True),
                authenticated=True, body=body,
            )
        except CommandFailed as error:
            output, failure = error.output, error
        try:
            value = decode_json(output)
        except ValueError:
            if failure is not None:
                raise failure from None
            raise ForgeError("gh returned invalid JSON") from None
        errors = value.get("errors") if isinstance(value, dict) else None
        if not errors:
            if failure is not None:
                raise failure
            return object_value(
                object_value(value, "GraphQL response").get("data"),
                "GraphQL data",
            )
        detail = self.graphql_errors(errors)
        paths = [object_value(e, "GraphQL error").get("path") for e in errors]
        if optional is None or not all(
            isinstance(path, list) and path[:2] == ["repository", optional]
            for path in paths
        ):
            raise failure or ForgeError(detail)
        data = object_value(value.get("data"), "GraphQL data")
        object_value(data.get("repository"), "repository")[optional] = None
        return data

    def complete(
        self, value: object, connection: Connection, owner: object,
    ) -> list[dict]:
        """Return every node of one list, completing nested lists in place.

        A further page is one ``node(id:)`` query for the owner's list.
        """
        nodes: list[dict] = []
        seen = set()
        while True:
            page = object_value(value, connection.field)
            nodes.extend(
                object_value(node, connection.field)
                for node in array(page.get("nodes"), connection.field)
            )
            info = object_value(page.get("pageInfo"), "pageInfo")
            if not boolean(info.get("hasNextPage"), "hasNextPage"):
                break
            cursor = V.require_string(info.get("endCursor"), "endCursor")
            if cursor in seen:
                raise ForgeError("GraphQL pagination repeated a cursor")
            seen.add(cursor)
            data = self.graphql(page_query(connection), {
                "id": V.require_string(owner, f"{connection.owner}.id"),
                "cursor": cursor,
            })
            value = object_value(
                data.get("node"), connection.owner
            ).get(connection.field)
        for node in nodes:
            for inner in connection.nested:
                node[inner.field] = self.complete(
                    node.get(inner.field), inner, node.get("id")
                )
        return nodes

    def listing(self, endpoint: str, key: str | None = None) -> list:
        """Read every page; ``key`` names the array of a wrapped page."""
        result = []
        page = 1
        while True:
            separator = "&" if "?" in endpoint else "?"
            value = self.api(f"{endpoint}{separator}per_page=100&page={page}")
            if key is not None:
                # A wrapped page's total_count and state are never read.
                value = object_value(value, endpoint).get(key)
            entries = array(value, endpoint)
            result.extend(entries)
            if len(entries) < 100:
                return result
            page += 1

    def repository_record(self) -> dict[str, object]:
        data = object_value(self.api(self.prefix), "repository")
        positive(data.get("id"), "repository.id")
        V.require_string(data.get("full_name"), "repository.full_name")
        return data

    def repository_permission(self) -> str:
        """Read this account's base repository permission, including roles."""
        data = object_value(
            self.api(
                f'{self.prefix}/collaborators/'
                f'{quote(self.account, safe="")}/permission'
            ),
            "collaborator permission",
        )
        user = object_value(data.get("user"), "collaborator user")
        login = V.require_string(user.get("login"), "collaborator login")
        if login.casefold() != self.account.casefold():
            raise ForgeError("collaborator permission returned another user")
        permission = data.get("permission")
        # GitHub maps maintain to write, triage to read, and custom roles to
        # their base permission. Do not infer access from role_name.
        if permission not in ("admin", "write", "read", "none"):
            raise ForgeError("invalid collaborator permission")
        return permission

    def issue(self, number: int) -> IssueRecord:
        record = parse_issue(self.api(f"{self.prefix}/issues/{number}"))
        record["comments"] = tuple(
            parse_evidence(c)
            for c in self.listing(f"{self.prefix}/issues/{number}/comments")
        )
        return record

    def open_issues(self) -> dict[int, str]:
        result = {}
        for row in self.listing(f"{self.prefix}/issues?state=open"):
            data = object_value(row, "issue")
            # The issues listing also returns pull requests.
            if "pull_request" not in data:
                number = positive(data.get("number"), "issue.number")
                result[number] = text_value(data.get("title"), "issue.title")
        return result

    def labels(self) -> tuple[str, ...]:
        return tuple(
            V.require_string(
                object_value(label, "label").get("name"), "label.name"
            )
            for label in self.listing(f"{self.prefix}/labels")
        )

    def milestones(self) -> tuple[Milestone, ...]:
        # Without state=all GitHub lists only open milestones.
        return tuple(
            parse_milestone(row, "number")
            for row in self.listing(f"{self.prefix}/milestones?state=all")
        )

    def issues(self, query: IssueQuery) -> list[ListedIssue]:
        """Read the issue listing for ``query`` in one query per page.

        The forge's filters only narrow the read; ``query.matches`` decides
        on each issue's record before its linked pull requests are read.
        """
        config = self.repository.configuration
        variables: dict[str, object] = {
            "owner": config.forge.owner, "repo": config.forge.repo,
            "states": QUERY_STATES[query.state],
        }
        if query.labels:
            variables["labels"] = list(query.labels)
        if query.milestone is not None:
            variables["filter"] = {
                "milestoneNumber": str(query.milestone.number)
            }
        result: list[ListedIssue] = []
        seen: set[str] = set()
        cursor = None
        while True:
            data = self.graphql(ISSUES_QUERY, {**variables, "cursor": cursor})
            page = object_value(
                object_value(data.get("repository"), "repository").get(
                    "issues"
                ),
                "issues",
            )
            for node in array(page.get("nodes"), "issues"):
                row = self.listed_issue(object_value(node, "issue"), query)
                if row is not None:
                    result.append(row)
            info = object_value(page.get("pageInfo"), "pageInfo")
            if not boolean(info.get("hasNextPage"), "hasNextPage"):
                return result
            cursor = V.require_string(info.get("endCursor"), "endCursor")
            if cursor in seen:
                raise ForgeError("GraphQL pagination repeated a cursor")
            seen.add(cursor)

    def listed_issue(
        self, node: dict, query: IssueQuery,
    ) -> ListedIssue | None:
        """One issue node, or null when its record does not match."""
        state = ISSUE_STATES.get(node.get("state"))
        if state is None:
            raise ForgeError("issue.state must be OPEN or CLOSED")
        if "milestone" not in node:
            raise ForgeError("issue.milestone is missing")
        reason = optional_text(node.get("stateReason"), "issue.stateReason")
        row: ListedIssue = {
            "number": positive(node.get("number"), "issue.number"),
            "title": text_value(node.get("title"), "issue.title"),
            "state": state,
            "state_reason": None if reason is None else reason.lower(),
            "labels": [
                V.require_string(
                    object_value(label, "label").get("name"), "label.name"
                )
                for label in self.complete(
                    node.get(ISSUE_LABELS.field), ISSUE_LABELS, node.get("id")
                )
            ],
            "milestone": milestone_title(
                node["milestone"], "issue.milestone"
            ),
            "is_pull_request": False,
            "closed_by": [],
        }
        if not query.matches(row):
            return None
        if state == "closed":
            row["closed_by"] = closing_order([
                pr for pr in map(closing_pull_request, self.complete(
                    node.get(CLOSING_PRS.field), CLOSING_PRS, node.get("id")
                )) if pr is not None
            ])
        return row

    def create_issue(self, title: str, body: str, label: str) -> IssueRecord:
        return parse_issue(self.api(
            f"{self.prefix}/issues", method="POST",
            body={"title": title, "body": body, "labels": [label]},
        ))

    def issue_dependencies(self, number: int) -> IssueDependencies:
        base = f"{self.prefix}/issues/{number}/dependencies"

        def read(direction: str) -> list[RelatedIssue]:
            return [parse_related_issue(row)
                    for row in self.listing(f"{base}/{direction}")]

        return {
            "blocks": exposed(lambda: read("blocking")),
            "blocked_by": exposed(lambda: read("blocked_by")),
        }

    def change_issue_labels(
        self, number: int, add: tuple[str, ...], remove: tuple[str, ...],
    ) -> None:
        """Add, then remove one at a time; never replace the whole set."""
        endpoint = f"{self.prefix}/issues/{number}/labels"
        if add:
            self.api(endpoint, method="POST", body={"labels": list(add)})
        for name in remove:
            self.api(f'{endpoint}/{quote(name, safe="")}', method="DELETE")

    def pr(self, number: int) -> PullRequest:
        return parse_pullrequest(self.api(f"{self.prefix}/pulls/{number}"))

    def reviews(self, number: int) -> tuple[Review, ...]:
        return tuple(
            parse_review(r)
            for r in self.listing(f"{self.prefix}/pulls/{number}/reviews")
        )

    def snapshot(self, number: int, *, with_checks: bool = False) -> Snapshot:
        """Read the PR and every list behind ``status`` in one query (#149).

        With ``with_checks`` the query also reads the checks at the PR's
        head ref; ``head_checks`` stays null when that commit is not the
        head or its checks could not be read, and ``checks`` reads them.
        """
        config = self.repository.configuration
        data = self.graphql(SNAPSHOT_QUERY, {
            "owner": config.forge.owner, "repo": config.forge.repo,
            "pr": number, "head": f"refs/pull/{number}/head",
            "checks": with_checks,
        }, optional="head")
        repository = object_value(data.get("repository"), "repository")
        node = object_value(repository.get("pullRequest"), "pullRequest")
        lists = {
            connection.field: self.complete(
                node.get(connection.field), connection, node.get("id")
            )
            for connection in (REVIEWS, THREADS, CONVERSATION)
        }
        pr = parse_pullrequest(rest_pullrequest(node))
        reviews = tuple(parse_review(rest_review(r)) for r in lists["reviews"])
        # Thread comments stand for REST's flat listing, which has anchors
        # and lists by ID; review comments for its per-review listing, which
        # has every reply.
        comments = {}
        threads = []
        for thread in lists["reviewThreads"]:
            members = [
                parse_comment(rest_comment(value, thread))
                for value in thread["comments"]
            ]
            if not members:
                raise ForgeError("thread has no root comment")
            threads.append(ThreadState(
                members[0].evidence.id,
                V.require_string(thread.get("id"), "thread.id"),
                boolean(thread.get("isResolved"), "thread.isResolved"),
            ))
            comments.update((c.evidence.id, c) for c in members)
        comments = dict(sorted(comments.items()))
        for review in lists["reviews"]:
            for value in review["comments"]:
                comment = parse_comment(rest_comment(value, None))
                comments.setdefault(comment.evidence.id, comment)
        return Snapshot(
            pr,
            reviews,
            tuple(comments.values()),
            tuple(
                parse_evidence(rest_evidence(c, "comment"))
                for c in lists["comments"]
            ),
            tuple(threads) if self.can_read_thread_resolution else (),
            self.can_resolve_threads,
            self.can_read_thread_resolution,
            self.can_read_branch_rules,
            "APPROVED",
            tuple(r.evidence.id for r in reviews
                  if r.state == ReviewState.PENDING
                  and r.evidence.author.casefold() == self.account.casefold()),
            head_checks=self.head_checks(repository.get("head"), pr.head),
        )

    def head_checks(
        self, commit: object, head: str,
    ) -> tuple[Check, ...] | None:
        """Checks read with the snapshot, or null when ``checks`` must."""
        if commit is None or object_value(commit, "head").get("oid") != head:
            return None
        try:
            return self.commit_checks(commit)
        except ForgeError:
            return None

    def create_pr(
        self, title: str, head_branch: str, base_branch: str, body: str,
    ) -> PullRequest:
        payload = {
            "title": title, "head": head_branch, "base": base_branch,
            "body": body,
        }
        return parse_pullrequest(
            self.api(f"{self.prefix}/pulls", method="POST", body=payload)
        )

    def branch_rules(self, branch: str) -> dict[str, object]:
        """Report classic protection and active rulesets when exposed."""
        result = {}
        for name, endpoint in (
            ("protection", f"branches/{quote(branch, safe='')}/protection"),
            ("rules", f"rules/branches/{quote(branch, safe='')}"),
        ):
            try:
                value = (
                    self.listing(f"{self.prefix}/{endpoint}")
                    if name == "rules"
                    else self.api(f"{self.prefix}/{endpoint}")
                )
                if name == "protection":
                    object_value(value, "branch protection")
                result[name] = value
            except ForgeError as error:
                if error.status == 404 or (
                    error.status == 403 and re.search(
                        r"upgrade|not available.*plan|requires GitHub",
                        str(error), re.IGNORECASE,
                    )
                ):
                    result[name] = {"visibility": "no rule visible"}
                else:
                    raise
        return result

    def checks(self, sha: str) -> tuple[Check, ...]:
        """Read check runs and commit statuses at one commit in one query.

        Any failure of the query leaves the result to the REST reads, which
        decide between checks, a refusal, and an error as before (#149).
        """
        config = self.repository.configuration
        try:
            data = self.graphql(CHECKS_QUERY, {
                "owner": config.forge.owner, "repo": config.forge.repo,
                "sha": sha,
            })
            return self.commit_checks(object_value(
                object_value(data.get("repository"), "repository").get(
                    "object"
                ),
                "commit",
            ))
        except ForgeError:
            return self.rest_checks(sha)

    def commit_checks(self, commit: dict) -> tuple[Check, ...]:
        """Check records of one GraphQL commit, as ``rest_checks`` reads them.

        ``LATEST`` keeps each check run's latest attempt and ``contexts`` the
        latest status per context. REST lists check runs newest first; that
        order is kept for runs with the same name and event.
        """
        suites = self.complete(
            commit.get("checkSuites"), CHECK_SUITES, commit.get("id")
        )
        events: dict[int, str] = {}
        runs = []
        # Each suite's position stands for its ID, which binds a check run
        # to the event of the suite's workflow run.
        for key, suite in enumerate(suites, 1):
            workflow = suite.get("workflowRun")
            if workflow is not None:
                events[key] = V.require_string(
                    object_value(workflow, "workflowRun").get("event"),
                    "workflowRun.event",
                )
            runs.extend(
                (positive(run.get("databaseId"), "checkRun.databaseId"),
                 rest_check_run(run, key, suite.get("app")))
                for run in suite["checkRuns"]
            )
        status = commit.get("status")
        contexts = [] if status is None else array(
            object_value(status, "status").get("contexts"), "status contexts"
        )
        # The list has no pages; one that may be cut off is read by REST.
        if len(contexts) >= PAGE_SIZE:
            raise ForgeError("status contexts may be incomplete")
        return ordered(
            [parse_check_run(run, events)
             for _, run in sorted(runs, key=lambda r: r[0], reverse=True)]
            + [parse_commit_status(rest_status(object_value(s, "context")))
               for s in contexts]
        )

    def rest_checks(self, sha: str) -> tuple[Check, ...]:
        """Read check runs and commit statuses at one commit (#145).

        The default check-run filter keeps each run's latest attempt, and the
        combined status keeps the latest status per context; its own state
        reads pending for a commit without statuses, so it is never used.
        """
        commit = f"{self.prefix}/commits/{sha}"
        try:
            runs = self.listing(
                f"{commit}/check-runs?filter=latest", "check_runs"
            )
            statuses = self.listing(f"{commit}/status", "statuses")
            workflow_runs = self.listing(
                f"{self.prefix}/actions/runs?head_sha={sha}", "workflow_runs"
            )
        except ForgeError as error:
            if error.status not in CHECK_REFUSALS:
                raise
            raise ChecksUnavailable(str(error), error.status) from None
        events = {}
        for value in workflow_runs:
            data = object_value(value, "workflow run")
            events[positive(data.get("check_suite_id"), "check_suite_id")] = (
                V.require_string(data.get("event"), "workflow_run.event")
            )
        return ordered(
            [parse_check_run(run, events) for run in runs]
            + [parse_commit_status(status) for status in statuses]
        )

    def merge(self, number: int, head: str, method: str) -> MergeResult:
        data = object_value(self.api(
            f"{self.prefix}/pulls/{number}/merge", method="PUT",
            body={"sha": head, "merge_method": method},
        ), "merge response")
        message = text_value(data.get("message"), "merge.message")
        if not boolean(data.get("merged"), "merge.merged"):
            raise ForgeError(message, 405)
        return {"sha": oid(data.get("sha"), "merge.sha"), "message": message}

    def validate_anchors(self, anchors: tuple[Anchor, ...]) -> None:
        # Common validation already refuses duplicate end-line anchors.
        pass

    def branch_head(self, branch: str) -> str | None:
        try:
            data = object_value(self.api(
                f"{self.prefix}/git/ref/heads/{quote(branch, safe='')}",
            ), "branch ref")
        except ForgeError as error:
            if error.status == 404:
                return None
            raise
        if data.get("ref") != f"refs/heads/{branch}":
            raise ForgeError("branch response has another identity")
        target = object_value(data.get("object"), "branch object")
        return oid(target.get("sha"), "branch SHA")

    def delete_branch(self, branch: str, expected_head: str) -> None:
        if not branch_present(self, branch, expected_head):
            return
        if self._deletes_merged_branches():
            # A DELETE sent now would race GitHub's own deletion.
            deadline = time.monotonic() + AUTO_DELETION_WAIT
            while (remaining := deadline - time.monotonic()) > 0:
                time.sleep(min(AUTO_DELETION_POLL, remaining))
                if not branch_present(self, branch, expected_head):
                    return
        delete_and_confirm(self, branch, expected_head, lambda: self.api(
            f"{self.prefix}/git/refs/heads/{quote(branch, safe='')}",
            method="DELETE",
        ))

    def _deletes_merged_branches(self) -> bool:
        # Not every caller can read the field (an anonymous read omits it); a
        # missing or unreadable setting falls back to the guarded DELETE.
        try:
            record = self.repository_record()
        except ForgeError:
            return False
        return record.get("delete_branch_on_merge") is True

    def branch_prs(self, branch: str) -> list[dict[str, object]]:
        config = self.repository.configuration
        query = urlencode(
            {"head": f"{config.forge.owner}:{branch}", "state": "all"}
        )
        return self.listing(f"{self.prefix}/pulls?{query}")

    def update_body(self, number: int, body: str) -> PullRequest:
        return parse_pullrequest(
            self.api(
                f"{self.prefix}/pulls/{number}",
                method="PATCH",
                body={"body": body},
            )
        )

    def prepare_review(
        self, number: int, *, reviews: tuple[Review, ...],
        discard_draft: int | None = None,
    ) -> None:
        if discard_draft is not None:
            raise ForgeError("--discard-draft is not supported on this forge")
        # Preserve stranded drafts and replies before publishing a new review.
        # Reuse the command's snapshot to preserve the existing read sequence.
        for review in reviews:
            if (
                review.state == ReviewState.PENDING
                and review.evidence.author.casefold()
                == self.account.casefold()
            ):
                self.submit_pending(number, review.evidence.id)

    def post_review(
        self, number: int, publication: ReviewPublication,
    ) -> Review:
        payload = {
            "commit_id": publication.head,
            "event": review_event(publication.state),
            "body": publication.body,
            "comments": [
                {**anchor_payload(comment.anchor), "body": comment.body}
                for comment in publication.comments
            ],
        }
        try:
            value = self.api(
                f"{self.prefix}/pulls/{number}/reviews",
                method="POST", body=payload,
            )
            return parse_review(value)
        except ForgeError as error:
            # Only a definite batch rejection permits the fallback write.
            if error.status != 422 or not publication.comments:
                raise
            from .anchors import commentable_lines, validate_anchor

            lines = commentable_lines(
                self.repository.root, publication.base, publication.head,
            )
            for comment in publication.comments:
                validate_anchor(comment.anchor, lines)
            payload["comments"] = []
            payload["body"] = publication.fallback_body
            return parse_review(self.api(
                f"{self.prefix}/pulls/{number}/reviews",
                method="POST", body=payload,
            ))

    def submit_pending(self, number: int, review: int) -> Review:
        return parse_review(
            self.api(
                f"{self.prefix}/pulls/{number}/reviews/{review}/events",
                method="POST",
                body={"event": "COMMENT"},
            )
        )

    def post_root(
        self, number: int, head: str, review_id: int,
        anchor: Anchor, body: str,
    ) -> Comment:
        # GitHub attaches these roots to a synthetic review, as before.
        payload = {"commit_id": head, **anchor_payload(anchor), "body": body}
        return parse_comment(
            self.api(
                f"{self.prefix}/pulls/{number}/comments",
                method="POST",
                body=payload,
            )
        )

    def reply(self, number: int, root: int, body: str) -> Comment:
        return parse_comment(
            self.api(
                f"{self.prefix}/pulls/{number}/comments/{root}/replies",
                method="POST",
                body={"body": body},
            )
        )

    def comment(self, number: int, body: str) -> Evidence:
        return parse_evidence(
            self.api(
                f"{self.prefix}/issues/{number}/comments",
                method="POST",
                body={"body": body},
            )
        )

    def resolve(self, node_id: str) -> dict[str, object]:
        if not self.can_resolve_threads:
            raise ForgeError("not supported on this forge")
        value = self.api(
            "graphql",
            method="POST",
            body={"query": RESOLVE_MUTATION, "variables": {"id": node_id}},
        )
        try:
            thread = object_value(
                value["data"]["resolveReviewThread"]["thread"],
                "resolved thread",
            )
        except (KeyError, TypeError):
            raise ForgeError("missing resolveReviewThread response") from None
        if thread.get("id") != node_id or thread.get("isResolved") is not True:
            raise ForgeError("forge did not confirm thread resolution")
        return thread
