"""GitHub subprocess transport, wire validation, and publication mechanics."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from urllib.parse import quote, urlencode

from .forge import (
    Anchor, Comment, Evidence, ForgeError, IssueRecord, MergeResult,
    PullRequest, Review, ReviewPublication, ReviewState, Role, Snapshot,
    ThreadState, V, array, boolean, object_value, oid, positive, text_value,
)
from .initialization import Repository, decode_json


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
        "comments": (),
    }


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


THREAD_QUERY = """query SquadThreads(
  $owner:String!, $repo:String!, $pr:Int!, $cursor:String
) {
  repository(owner:$owner, name:$repo) { pullRequest(number:$pr) {
    reviewThreads(first:100, after:$cursor) {
      nodes { id isResolved comments(first:1) { nodes { databaseId } } }
      pageInfo { hasNextPage endCursor }
    }
  } }
}"""
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
            raise ForgeError(
                " ".join(detail.splitlines()),
                int(status[1]) if status else None,
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
        if body is not None:
            args.extend(["--input", "-"])
        output = self._run(args, authenticated=True, body=body)
        try:
            value = decode_json(output) if output.strip() else None
        except ValueError:
            raise ForgeError("gh returned invalid JSON") from None
        if (
            endpoint == "graphql"
            and isinstance(value, dict)
            and value.get("errors")
        ):
            messages = [
                text_value(
                    object_value(e, "GraphQL error").get("message"), "message"
                )
                for e in array(value["errors"], "GraphQL errors")
            ]
            detail = "; ".join(messages)
            if self._token:
                detail = detail.replace(self._token, "[redacted]")
            raise ForgeError(detail)
        return value

    def listing(self, endpoint: str) -> list:
        result = []
        page = 1
        while True:
            separator = "&" if "?" in endpoint else "?"
            entries = array(
                self.api(f"{endpoint}{separator}per_page=100&page={page}"),
                endpoint,
            )
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

    def create_issue(self, title: str, body: str, label: str) -> IssueRecord:
        return parse_issue(self.api(
            f"{self.prefix}/issues", method="POST",
            body={"title": title, "body": body, "labels": [label]},
        ))

    def pr(self, number: int) -> PullRequest:
        return parse_pullrequest(self.api(f"{self.prefix}/pulls/{number}"))

    def reviews(self, number: int) -> tuple[Review, ...]:
        return tuple(
            parse_review(r)
            for r in self.listing(f"{self.prefix}/pulls/{number}/reviews")
        )

    def snapshot(self, number: int) -> Snapshot:
        pr = self.pr(number)
        reviews = self.reviews(number)
        flat = {
            c.evidence.id: c
            for c in (
                parse_comment(c)
                for c in self.listing(f"{self.prefix}/pulls/{number}/comments")
            )
        }
        comments = dict(flat)
        for review in reviews:
            for value in self.listing(
                f"{self.prefix}/pulls/{number}/reviews/"
                f"{review.evidence.id}/comments"
            ):
                comment = parse_comment(value)
                # The flat endpoint has anchors; the per-review endpoint has
                # all replies.
                comments[comment.evidence.id] = flat.get(
                    comment.evidence.id, comment
                )
        conversation = tuple(
            parse_evidence(c)
            for c in self.listing(f"{self.prefix}/issues/{number}/comments")
        )
        return Snapshot(
            pr,
            reviews,
            tuple(comments.values()),
            conversation,
            (self.thread_states(number)
             if self.can_read_thread_resolution else ()),
            self.can_resolve_threads,
            self.can_read_thread_resolution,
            self.can_read_branch_rules,
            "APPROVED",
            tuple(r.evidence.id for r in reviews
                  if r.state == ReviewState.PENDING
                  and r.evidence.author.casefold() == self.account.casefold()),
        )

    def thread_states(self, number: int) -> tuple[ThreadState, ...]:
        config = self.repository.configuration
        cursor = None
        seen = set()
        result = []
        while True:
            data = self.api(
                "graphql",
                method="POST",
                mutation=False,
                body={
                    "query": THREAD_QUERY,
                    "variables": {
                        "owner": config.forge.owner,
                        "repo": config.forge.repo,
                        "pr": number,
                        "cursor": cursor,
                    },
                },
            )
            try:
                connection = object_value(
                    data["data"]["repository"]["pullRequest"]["reviewThreads"],
                    "threads",
                )
            except (KeyError, TypeError):
                raise ForgeError(
                    "missing GraphQL reviewThreads response"
                ) from None
            for value in array(connection.get("nodes"), "thread nodes"):
                node = object_value(value, "thread")
                roots = array(
                    object_value(node.get("comments"), "thread comments").get(
                        "nodes"
                    ),
                    "root nodes",
                )
                if not roots:
                    raise ForgeError("thread has no root comment")
                result.append(
                    ThreadState(
                        positive(
                            object_value(roots[0], "root").get("databaseId"),
                            "root id",
                        ),
                        V.require_string(node.get("id"), "thread.id"),
                        boolean(node.get("isResolved"), "thread.isResolved"),
                    )
                )
            page = object_value(connection.get("pageInfo"), "pageInfo")
            if not boolean(page.get("hasNextPage"), "hasNextPage"):
                return tuple(result)
            cursor = V.require_string(page.get("endCursor"), "endCursor")
            if cursor in seen:
                raise ForgeError("GraphQL pagination repeated a cursor")
            seen.add(cursor)

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
        head = self.branch_head(branch)
        if head is None:
            return
        if head != expected_head:
            raise ForgeError("remote branch no longer matches approved head")
        self.api(
            f"{self.prefix}/git/refs/heads/{quote(branch, safe='')}",
            method="DELETE",
        )
        if self.branch_head(branch) is not None:
            raise ForgeError(f"remote branch remains: {branch}")

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
