"""Forgejo REST transport, evidence, and guarded mutations."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from email.message import Message
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .forge import (
    CHECK_REFUSALS,
    Anchor,
    Check,
    CheckResult,
    ChecksUnavailable,
    ClosingPullRequest,
    Comment,
    DEPENDENCY_REFUSALS,
    Evidence,
    ForgeError,
    IssueDependencies,
    IssueQuery,
    IssueRecord,
    ListedIssue,
    MergeRefused,
    MergeResult,
    Milestone,
    PullRequest,
    RelatedIssue,
    Review,
    ReviewPublication,
    ReviewState,
    Role,
    Snapshot,
    ThreadState,
    V,
    array,
    boolean,
    branch_present,
    closing_order,
    delete_and_confirm,
    exposed,
    milestone_title,
    object_value,
    oid,
    parse_milestone,
    positive,
    related_issue,
    text_value,
)
from .initialization import (
    GateError,
    Repository,
    decode_json,
    list_worktrees,
    validate_base_url,
)

STATES = {
    "APPROVED": ReviewState.APPROVED,
    "REQUEST_CHANGES": ReviewState.CHANGES_REQUESTED,
    "COMMENT": ReviewState.COMMENTED,
    "PENDING": ReviewState.PENDING,
}
TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")
FINDING = re.compile(r"\[REV-[1-9][0-9]*\](?=\[| |$)")
# The page size of the one blocks read (#144): F16 FormInt parses the limit
# with strconv.Atoi, and this is the largest value every platform's int
# holds, above any number of issues one issue can block.
FORGEJO_BLOCKS_LIMIT = 2**31 - 1


def anchor_payload(anchor: Anchor) -> dict[str, object]:
    start = anchor.start_line or anchor.line
    return {
        "path": anchor.path, "new_position": start,
        "extra_lines_count": anchor.line - start,
    }


def parse_evidence(value: object, *, review: bool = False) -> Evidence:
    data = object_value(value, "evidence")
    user = object_value(data.get("user"), "evidence.user")
    timestamp = text_value(
        data.get("submitted_at" if review else "created_at"),
        "evidence.timestamp",
    )
    try:
        if not TIMESTAMP.fullmatch(timestamp):
            raise ValueError
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise ForgeError(
            "evidence.timestamp must be ISO 8601 with an offset"
        ) from None
    return Evidence(
        positive(data.get("id"), "evidence.id"),
        V.require_string(user.get("login"), "evidence.user.login"),
        timestamp,
        text_value(
            "" if data.get("body") is None else data["body"],
            "evidence.body",
        ).replace("\r\n", "\n"),
    )


def evidence_order(evidence: Evidence) -> tuple[datetime, int]:
    return (
        datetime.fromisoformat(evidence.created_at.replace("Z", "+00:00")),
        evidence.id,
    )


def parse_review(value: object) -> Review | None:
    data = object_value(value, "review")
    state = text_value(data.get("state"), "review.state")
    # Requested reviewers can have an empty commit and are not reviews.
    if state == "REQUEST_REVIEW":
        return None
    if state not in STATES:
        raise ForgeError("unknown review state")
    return Review(
        parse_evidence(data, review=True),
        oid(data.get("commit_id"), "review.commit_id"),
        STATES[state],
        boolean(data.get("dismissed"), "review.dismissed"),
    )


def parse_issue(value: object) -> IssueRecord:
    data = object_value(value, "issue")
    evidence = parse_evidence(data)
    number = positive(data.get("number"), "issue.number")
    if data.get("state") not in ("open", "closed"):
        raise ForgeError("issue.state must be open or closed")
    return {
        "number": number,
        "title": text_value(data.get("title"), "issue.title"),
        "state": data["state"],
        "is_pull_request": data.get("pull_request") is not None,
        "body": evidence.body,
        "labels": [
            V.require_string(
                object_value(label, "label").get("name"),
                "label.name",
            )
            for label in array(data.get("labels"), "labels")
        ],
        "milestone": milestone_title(data.get("milestone"), "issue.milestone"),
        "comments": (),
    }


def parse_listed_issue(value: object) -> ListedIssue:
    """One row of the issue listing (#164); F16 gives no state reason."""
    data = object_value(value, "issue")
    if "milestone" not in data:
        raise ForgeError("issue.milestone is missing")
    record = parse_issue(data)
    return {
        "number": record["number"],
        "title": record["title"],
        "state": record["state"],
        "state_reason": None,
        "labels": record["labels"],
        "milestone": record["milestone"],
        "is_pull_request": record["is_pull_request"],
        "closed_by": [],
    }


def closing_reference(value: object) -> ClosingPullRequest | None:
    """A timeline row's merged closing pull request; null for any other.

    F16 writes a ``pull_ref`` row when a pull request names the issue:
    ``ref_action`` is ``closes`` for a closing keyword and ``none`` for a
    plain reference, and ``ref_issue`` is the pull request in issue form.
    """
    data = object_value(value, "timeline row")
    if text_value(data.get("type"), "timeline.type") != "pull_ref":
        return None
    action = text_value(data.get("ref_action"), "timeline.ref_action")
    if action != "closes":
        return None
    issue = object_value(data.get("ref_issue"), "timeline.ref_issue")
    pull = object_value(
        issue.get("pull_request"), "timeline.ref_issue.pull_request"
    )
    if not boolean(pull.get("merged"), "timeline.ref_issue.merged"):
        return None
    repository = object_value(
        issue.get("repository"), "timeline.ref_issue.repository"
    )
    return {
        "number": positive(issue.get("number"), "timeline.ref_issue.number"),
        "repository": V.require_string(
            repository.get("full_name"),
            "timeline.ref_issue.repository.full_name",
        ),
    }


def parse_related_issue(value: object) -> RelatedIssue | None:
    """Read a dependency row; null for a row the forge hides (#144).

    F16 serves a blocker that the identity may not read as an issue
    numbered 0, without its repository.
    """
    data = object_value(value, "related issue")
    if data.get("number") == 0:
        return None
    repository = object_value(data.get("repository"), "issue.repository")
    return related_issue(
        data.get("number"), data.get("title"), data.get("state"),
        V.require_string(repository.get("full_name"), "repository.full_name"),
    )


def parse_pullrequest(value: object) -> PullRequest:
    data = object_value(value, "pull request")
    head = object_value(data.get("head"), "pull request head")
    base = object_value(data.get("base"), "pull request base")
    if data.get("state") not in ("open", "closed"):
        raise ForgeError("pull request state must be open or closed")
    # Both are informational. The review merge-base is computed with Git.
    oid(data.get("merge_base"), "merge_base")
    merge_commit = data.get("merge_commit_sha")
    if merge_commit is not None:
        merge_commit = oid(merge_commit, "merge_commit_sha")
    return PullRequest(
        parse_evidence(data),
        positive(data.get("number"), "PR number"),
        text_value(data.get("title"), "PR title"),
        oid(head.get("sha"), "head.sha"),
        V.require_string(head.get("ref"), "head.ref"),
        oid(base.get("sha"), "base.sha"),
        V.require_string(base.get("ref"), "base.ref"),
        data["state"],
        boolean(data.get("merged"), "merged"),
        merge_commit,
        None,
    )


def hunk_anchor(hunk: str, extra: int) -> tuple[int | None, int | None]:
    """Decode the displayed head-side range, never the wire position."""
    current = None
    displayed: list[int] = []
    ends_on_head = False
    remaining_old = remaining_new = 0
    for line in hunk.splitlines():
        header = HUNK.fullmatch(line)
        if header:
            current = int(header[3])
            remaining_old = int(header[2] or 1)
            remaining_new = int(header[4] or 1)
            displayed = []
            ends_on_head = False
        elif line.startswith("\\ No newline at end of file"):
            continue
        elif current is None or not line or line[0] not in " +-":
            return None, None
        else:
            ends_on_head = line[0] in " +"
            if line[0] in " -":
                remaining_old -= 1
            if line[0] in " +":
                remaining_new -= 1
                displayed.append(current)
                current += 1
            if remaining_old < 0 or remaining_new < 0:
                return None, None
    if (
        not ends_on_head or not displayed
        or displayed[-1] < 1 or len(displayed) <= extra
    ):
        return None, None
    end = displayed[-1]
    start = end - extra
    if start < 1 or start not in displayed:
        return None, None
    return end, start if extra else None


def parse_comments(
    values: object,
    review_id: int,
) -> tuple[tuple[Comment, ...], tuple[ThreadState, ...]]:
    rows = [
        object_value(v, "review comment") for v in array(values, "comments")
    ]
    rows.sort(key=lambda r: positive(r.get("id"), "comment.id"))
    groups: dict[tuple[str, int], list[tuple[Comment, dict]]] = {}
    for row in rows:
        if (
            positive(row.get("pull_request_review_id"), "review ID")
            != review_id
        ):
            raise ForgeError("comment returned another review ID")
        path = text_value(row.get("path"), "comment.path")
        position = V.require_int(row.get("position"), "comment.position")
        extra = V.require_int(
            row.get("extra_lines_count"), "extra_lines_count"
        )
        if extra < 0:
            raise ForgeError("extra_lines_count must not be negative")
        line, start = hunk_anchor(
            text_value(row.get("diff_hunk"), "diff_hunk"),
            extra,
        )
        comment = Comment(
            parse_evidence(row),
            review_id,
            None,
            path,
            line,
            start,
            "RIGHT" if line is not None else None,
        )
        groups.setdefault((path, position), []).append((comment, row))
    comments = []
    threads = []
    for entries in groups.values():
        roots = [c for c, _ in entries if FINDING.match(c.evidence.body)]
        # Ambiguous roots must never acquire somebody else's dispositions.
        usable = [c for c in roots if c.line is not None]
        root = usable[0] if len(usable) == 1 else None
        for comment, row in entries:
            is_root = any(c.evidence.id == comment.evidence.id for c in roots)
            if root is not None and not is_root:
                comment = replace(comment, in_reply_to_id=root.evidence.id)
            elif len(usable) > 1:
                comment = replace(
                    comment, line=None, start_line=None, side=None
                )
            comments.append(comment)
            if is_root:
                resolved = None
                if "resolver" in row:
                    resolved = row["resolver"] is not None
                    if resolved:
                        resolver = object_value(row["resolver"], "resolver")
                        V.require_string(
                            resolver.get("login"), "resolver.login"
                        )
                threads.append(
                    ThreadState(comment.evidence.id, None, resolved)
                )
    return (
        tuple(sorted(comments, key=lambda c: c.evidence.id)),
        tuple(sorted(threads, key=lambda t: t.root_id)),
    )


# Forgejo stores any string a client posts as a status, so every state not
# named here, including warning and error, counts as failed (#145).
STATUS_RESULTS: dict[str, CheckResult] = {
    "success": "passed", "skipped": "skipped", "pending": "pending",
}


def parse_status(value: object) -> Check:
    data = object_value(value, "commit status")
    state = text_value(data.get("status"), "status.status")
    # Forgejo Actions writes its statuses without a creator.
    creator = data.get("creator")
    url = data.get("target_url")
    return Check(
        # Forgejo Actions names a status "<workflow> / <job> (<event>)"; the
        # event is never parsed out of it.
        text_value(data.get("context"), "status.context"),
        None,
        state,
        STATUS_RESULTS.get(state, "failed"),
        None if creator is None else V.require_string(
            object_value(creator, "status.creator").get("login"),
            "status.creator.login",
        ),
        None if url in (None, "") else text_value(url, "status.target_url"),
        None,
        None,
    )


def read_token(repository: Repository, role: Role) -> str:
    """Validate the file's live identity and permissions on every read."""
    assert repository.configuration is not None
    value = getattr(repository.configuration, role).token_file
    location_error = (
        f"{role} token_file must be an absolute regular file outside"
        " repository worktrees (no symlink)"
    )
    if value is None or not Path(value).is_absolute():
        raise ForgeError(location_error)
    path = Path(value)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ForgeError(location_error)
        resolved = path.resolve(strict=True)
        for tree in list_worktrees(repository.root):
            # samefile covers case-insensitive aliases as well as symlinks
            # in ancestor paths. No lexical prefix is trusted on its own.
            if resolved.is_relative_to(tree.root) or any(
                parent.exists()
                and tree.root.exists()
                and parent.samefile(tree.root)
                for parent in resolved.parents
            ):
                raise ForgeError(location_error)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (info.st_dev, info.st_ino):
                raise ForgeError(location_error)
            if opened.st_mode & 0o077:
                raise ForgeError(
                    f"{role} token_file must have no group or other"
                    " permissions"
                )
            content = stream.read(65537)
    except (OSError, ValueError):
        raise ForgeError(location_error) from None
    try:
        lines = content.decode("utf-8").splitlines()
        if len(content) > 65536 or len(lines) != 1 or not lines[0].strip():
            raise ValueError
        token = lines[0].strip()
        if any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError
    except (UnicodeError, ValueError):
        raise ForgeError(
            f"{role} token_file must contain one non-empty line"
        ) from None
    return token


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        # Even same-origin redirects are unnecessary for the fixed API routes.
        return None


class Forgejo:
    version_label = "forge client"
    credential_label = "token file"
    can_resolve_threads = False
    can_read_thread_resolution = True
    can_read_branch_rules = True
    can_mutate = True
    mutation_unavailable_message = ""

    def __init__(
        self,
        repository: Repository,
        role: Role,
        *,
        timeout: float = 45,
    ) -> None:
        assert repository.configuration is not None
        config = repository.configuration
        self.repository = repository
        self.role = role
        self.account = config.account(role)
        self.base_url = validate_base_url(config.forge.base_url).rstrip("/")
        self.prefix = (
            f'/repos/{quote(config.forge.owner, safe="")}'
            f'/{quote(config.forge.repo, safe="")}'
        )
        self.timeout = timeout
        self._opener = build_opener(ProxyHandler({}), NoRedirect())
        self._verified_token: str | None = None
        self._version: str | None = None

    def verify_credentials(self) -> None:
        read_token(self.repository, self.role)

    def _request(
        self,
        endpoint: str,
        token: str,
        *,
        method: str = "GET",
        body: object = None,
    ) -> object:
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise ForgeError("API endpoint must be an origin-relative path")
        request = Request(
            self.base_url + "/api/v1" + endpoint,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": "token " + token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                content = response.read()
        except HTTPError as error:
            detail = f"HTTP {error.code}"
            try:
                payload = decode_json(error.read().decode("utf-8"))
                if isinstance(payload, dict) and isinstance(
                    payload.get("message"), str
                ):
                    detail = payload["message"]
            except (OSError, ValueError, HTTPException):
                pass
            finally:
                error.close()
            detail = detail.replace(token, "[redacted]")
            if self._verified_token:
                detail = detail.replace(self._verified_token, "[redacted]")
            raise ForgeError(
                " ".join(detail.splitlines()), error.code
            ) from None
        except (TimeoutError, URLError, OSError, ValueError, HTTPException):
            raise ForgeError(
                "forge request failed or timed out; re-read the PR before"
                " repeating a mutation"
            ) from None
        try:
            return (
                decode_json(content.decode("utf-8"))
                if content.strip()
                else None
            )
        except (UnicodeError, ValueError):
            raise ForgeError("forge returned invalid JSON") from None

    def _check_version(self, token: str) -> str:
        try:
            data = object_value(self._request("/version", token), "version")
            value = text_value(data.get("version"), "version.version")
            match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value)
            if match is None:
                raise ForgeError("cannot read forge version")
            try:
                numeric = tuple(map(int, match.group(1, 2, 3)))
            except ValueError:
                raise ForgeError("cannot read forge version") from None
        except ForgeError as error:
            raise ForgeError(
                "cannot read forge version", error.status
            ) from None
        if numeric < (16, 0, 0):
            raise ForgeError("Forgejo version must be at least 16.0.0")
        self._version = value
        return value.replace(token, "[redacted]")

    def version(self) -> str:
        return self._check_version(read_token(self.repository, self.role))

    def _verify_identity(self, token: str) -> None:
        if token != self._verified_token:
            data = object_value(self._request("/user", token), "user")
            login = V.require_string(data.get("login"), "user.login")
            if login.casefold() != self.account.casefold():
                raise ForgeError(
                    f"GET /user does not match configured {self.role} account"
                )
            self._verified_token = token

    def verify_identity(self) -> None:
        self._verify_identity(read_token(self.repository, self.role))

    def api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        body: object = None,
    ) -> object:
        token = read_token(self.repository, self.role)
        if method != "GET":
            self._verify_identity(token)
            if self._version is None:
                self._check_version(token)
        return self._request(endpoint, token, method=method, body=body)

    def listing(self, endpoint: str) -> list:
        result = []
        page = 1
        while True:
            separator = "&" if "?" in endpoint else "?"
            entries = array(
                self.api(f"{endpoint}{separator}limit=50&page={page}"),
                "list",
            )
            result.extend(entries)
            if len(entries) < 50:
                return result
            page += 1

    def repository_record(self) -> dict[str, object]:
        data = object_value(self.api(self.prefix), "repository")
        positive(data.get("id"), "repository.id")
        actual = V.require_string(
            data.get("full_name"), "repository.full_name"
        )
        config = self.repository.configuration.forge
        if actual.casefold() != f"{config.owner}/{config.repo}".casefold():
            raise ForgeError("repository response has another identity")
        return data

    def repository_permission(self) -> str:
        permissions = object_value(
            self.repository_record().get("permissions"),
            "permissions",
        )
        admin = boolean(permissions.get("admin"), "permissions.admin")
        push = boolean(permissions.get("push"), "permissions.push")
        pull = boolean(permissions.get("pull"), "permissions.pull")
        if admin:
            return "admin"
        if push:
            return "write"
        return "read" if pull else "none"

    def _conversation(self, number: int) -> tuple[Evidence, ...]:
        # This endpoint returns the complete list; do not apply review paging.
        values = array(
            self.api(f"{self.prefix}/issues/{number}/comments"), "comments"
        )
        return tuple(sorted(map(parse_evidence, values), key=lambda c: c.id))

    def issue(self, number: int) -> IssueRecord:
        record = parse_issue(self.api(f"{self.prefix}/issues/{number}"))
        if record["number"] != number:
            raise ForgeError("issue response has another number")
        record["comments"] = self._conversation(number)
        return record

    def open_issues(self) -> dict[int, str]:
        result = {}
        query = "state=open&type=issues"
        for row in self.listing(f"{self.prefix}/issues?{query}"):
            data = object_value(row, "issue")
            if data.get("pull_request") is None:
                number = positive(data.get("number"), "issue.number")
                result[number] = text_value(data.get("title"), "issue.title")
        return result

    def _label_ids(self) -> dict[str, int]:
        result = {}
        for row in self.listing(f"{self.prefix}/labels"):
            data = object_value(row, "label")
            name = V.require_string(data.get("name"), "label.name")
            result[name] = positive(data.get("id"), "label.id")
        return result

    def labels(self) -> tuple[str, ...]:
        return tuple(self._label_ids())

    def milestones(self) -> tuple[Milestone, ...]:
        return tuple(
            parse_milestone(row, "id")
            # Without ``state`` F16 lists open and closed milestones.
            for row in self._all_pages(f"{self.prefix}/milestones")
        )

    def issues(self, query: IssueQuery) -> list[ListedIssue]:
        """Read every issue in the state and check each record (#164).

        No milestone or label filter is sent: F16 ignores one it does not
        recognise, matches a milestone title before an ID, and splits both
        at commas, so a filter could return another milestone's issues.
        """
        result = []
        for value in self._all_pages(
            f"{self.prefix}/issues?state={query.state}&type=issues"
        ):
            row = parse_listed_issue(value)
            if not query.matches(row):
                continue
            if row["state"] == "closed":
                row["closed_by"] = self._closed_by(row["number"])
            result.append(row)
        return result

    def _closed_by(self, number: int) -> list[ClosingPullRequest] | None:
        """Merged pull requests the timeline links as closing the issue;
        null when the read is refused after the listing was read."""
        try:
            rows = self._all_pages(
                f"{self.prefix}/issues/{number}/timeline", null_ends=True
            )
        except ForgeError as error:
            if error.status in DEPENDENCY_REFUSALS:
                return None
            raise
        return closing_order(
            [pr for pr in map(closing_reference, rows) if pr is not None]
        )

    def create_issue(self, title: str, body: str, label: str) -> IssueRecord:
        # Creation takes label IDs and silently drops unknown ones (F16).
        ids = self._label_ids()
        if label not in ids:
            raise ForgeError(f"repository has no {label} label")
        data = object_value(self.api(
            f"{self.prefix}/issues", method="POST",
            body={"title": title, "body": body, "labels": [ids[label]]},
        ), "created issue")
        record = parse_issue(data)
        if (
            parse_evidence(data).author.casefold() != self.account.casefold()
            or record["body"] != body.replace("\r\n", "\n")
            or record["title"] != title
        ):
            raise ForgeError(
                f'issue {record["number"]} created; response mismatch'
            )
        return record

    def _all_pages(self, endpoint: str, *, null_ends: bool = False) -> list:
        """Read pages until an empty one; fail a page that repeats.

        A short page does not end the read: the server caps ``limit`` at its
        ``max_response_items``, which may be below 50, and some lists drop
        rows after paging (#164). ``null_ends`` accepts JSON null as the
        empty page, which F16's issue timeline serves beyond its end.
        """
        separator = "&" if "?" in endpoint else "?"
        result: list = []
        previous = None
        page = 1
        while True:
            value = self.api(f"{endpoint}{separator}limit=50&page={page}")
            entries = [] if value is None and null_ends else array(
                value, "list")
            if not entries:
                return result
            if entries == previous:
                raise ForgeError(f"{endpoint} repeated a page")
            result.extend(entries)
            previous = entries
            page += 1

    def issue_dependencies(self, number: int) -> IssueDependencies:
        endpoint = f"{self.prefix}/issues/{number}"

        def parsed(rows: list) -> list[RelatedIssue] | None:
            found = [parse_related_issue(row) for row in rows]
            # A hidden dependency leaves the list unknown, not shorter.
            return None if None in found else found

        def blocks() -> list[RelatedIssue] | None:
            # F16 GetIssueBlocks slices the whole list by page and limit
            # before it drops the issues the identity may not read, so any
            # page, even an empty one, may precede readable rows. It never
            # caps limit, so this one page holds every readable issue.
            return parsed(array(self.api(
                f"{endpoint}/blocks?limit={FORGEJO_BLOCKS_LIMIT}&page=1"
            ), "list"))

        return {
            "blocks": exposed(blocks),
            # GetIssueDependencies pages in the database, caps limit, and
            # keeps a row for every blocker, so paging ends at an empty page.
            "blocked_by": exposed(
                lambda: parsed(self._all_pages(f"{endpoint}/dependencies"))
            ),
        }

    def change_issue_labels(
        self, number: int, add: tuple[str, ...], remove: tuple[str, ...],
    ) -> None:
        """Add, then remove one at a time; never replace the whole set."""
        endpoint = f"{self.prefix}/issues/{number}/labels"
        if add:
            # By ID: F16 drops an unknown name or ID silently, and a name
            # would also add an organization label of the same name.
            ids = self._label_ids()
            for name in add:
                if name not in ids:
                    raise ForgeError(f"repository has no {name} label")
            self.api(
                endpoint, method="POST",
                body={"labels": [ids[name] for name in add]},
            )
        for name in remove:
            # By name: F16 looks the name up among the repository's labels
            # first and reads it as a label ID only when none has that name.
            self.api(f'{endpoint}/{quote(name, safe="")}', method="DELETE")

    def pr(self, number: int) -> PullRequest:
        data = self.api(f"{self.prefix}/pulls/{number}")
        result = parse_pullrequest(data)
        if result.number != number:
            raise ForgeError("pull request response has another number")
        config = self.repository.configuration.forge
        base = object_value(data.get("base"), "base")
        base_repo = object_value(base.get("repo"), "base.repo")
        name = V.require_string(
            base_repo.get("full_name"), "base.repo.full_name"
        )
        if name.casefold() != f"{config.owner}/{config.repo}".casefold():
            raise ForgeError("pull request response has another repository")
        return result

    def reviews(self, number: int) -> tuple[Review, ...]:
        result = [
            parse_review(r)
            for r in self.listing(f"{self.prefix}/pulls/{number}/reviews")
        ]
        return tuple(
            sorted(
                (r for r in result if r is not None),
                key=lambda r: evidence_order(r.evidence),
            )
        )

    def _comments(
        self,
        number: int,
        reviews: tuple[Review, ...],
    ) -> tuple[tuple[Comment, ...], tuple[ThreadState, ...]]:
        comments = []
        threads = []
        for review in reviews:
            parsed, states = parse_comments(
                self.api(
                    f"{self.prefix}/pulls/{number}/reviews/"
                    f"{review.evidence.id}/comments",
                ),
                review.evidence.id,
            )
            comments.extend(parsed)
            threads.extend(states)
        return (
            tuple(sorted(comments, key=lambda c: c.evidence.id)),
            tuple(threads),
        )

    def snapshot(self, number: int, *, with_checks: bool = False) -> Snapshot:
        # Checks are a separate read here: head_checks stays null.
        pr = self.pr(number)
        reviews = self.reviews(number)
        comments, threads = self._comments(number, reviews)
        return Snapshot(
            pr,
            reviews,
            comments,
            self._conversation(number),
            threads,
            self.can_resolve_threads,
            self.can_read_thread_resolution,
            self.can_read_branch_rules,
            "approved",
            tuple(r.evidence.id for r in reviews
                  if r.state == ReviewState.PENDING
                  and r.evidence.author.casefold() == self.account.casefold()),
            True,
        )

    def thread_states(self, number: int) -> tuple[ThreadState, ...]:
        return self._comments(number, self.reviews(number))[1]

    def branch_rules(self, branch: str) -> dict[str, object]:
        branch_path = quote(branch, safe="")
        try:
            return object_value(
                self.api(
                    f"{self.prefix}/branch_protections/{branch_path}",
                ),
                "branch protection",
            )
        except ForgeError as error:
            if error.status in (403, 404):
                return {"visibility": "not visible"}
            raise

    def checks(self, sha: str) -> tuple[Check, ...]:
        """Read the latest commit status per context at one commit (#145).

        The combined read pages at 50 rows; each page's ``state`` and
        ``total_count`` describe that page alone, so neither is read.
        """
        rows: list = []
        page = 1
        try:
            while True:
                data = object_value(self.api(
                    f"{self.prefix}/commits/{sha}/status"
                    f"?limit=50&page={page}"
                ), "combined status")
                # A commit without statuses answers ``statuses: null``.
                values = data.get("statuses")
                entries = [] if values is None else array(values, "statuses")
                rows.extend(entries)
                if len(entries) < 50:
                    break
                page += 1
        except ForgeError as error:
            if error.status not in CHECK_REFUSALS:
                raise
            raise ChecksUnavailable(
                f"{error} (HTTP {error.status})", error.status
            ) from None
        return tuple(sorted(map(parse_status, rows), key=lambda c: c.name))

    def branch_prs(self, branch: str) -> list[dict[str, object]]:
        matches = []
        for row in self.listing(f"{self.prefix}/pulls?state=all"):
            data = object_value(row, "pull request")
            head = object_value(data.get("head"), "pull request head")
            # Historical PRs may lack commit metadata after branch deletion.
            if V.require_string(head.get("ref"), "head.ref") == branch:
                matches.append(data)
        return matches

    def create_pr(
        self, title: str, head_branch: str, base_branch: str, body: str
    ) -> PullRequest:
        data = object_value(self.api(
            f"{self.prefix}/pulls", method="POST",
            body={"title": title, "head": head_branch,
                  "base": base_branch, "body": body},
        ), "created pull request")
        number = positive(data.get("number"), "PR number")
        pr = self.pr(number)
        if (
            pr.head_branch != head_branch or pr.base_branch != base_branch
            or pr.evidence.author.casefold() != self.account.casefold()
            or pr.evidence.body != body.replace("\r\n", "\n")
            or pr.title != title
        ):
            raise ForgeError(f"PR {number} created; read-back mismatch")
        return pr

    def update_body(self, number: int, body: str) -> PullRequest:
        self.api(
            f"{self.prefix}/pulls/{number}", method="PATCH",
            body={"body": body},
        )
        pr = self.pr(number)
        if pr.evidence.body != body.replace("\r\n", "\n"):
            raise ForgeError(f"PR {number} body read-back mismatch")
        return pr

    def validate_anchors(self, anchors: tuple[Anchor, ...]) -> None:
        seen = set()
        for anchor in anchors:
            location = (anchor.path, anchor.start_line or anchor.line)
            if location in seen:
                raise ForgeError("duplicate finding conversation anchor")
            seen.add(location)

    def prepare_review(
        self, number: int, *, reviews: tuple[Review, ...],
        discard_draft: int | None = None,
    ) -> None:
        # Refresh immediately before publication; snapshot reads may be old.
        current = self.reviews(number)
        pending = [
            r for r in current if r.state == ReviewState.PENDING
            and r.evidence.author.casefold() == self.account.casefold()
        ]
        if discard_draft is not None:
            if not any(r.evidence.id == discard_draft for r in pending):
                raise ForgeError(
                    f"review {discard_draft} is not a PENDING review of the"
                    " acting account on this PR"
                )
            self.api(
                f"{self.prefix}/pulls/{number}/reviews/{discard_draft}",
                method="DELETE",
            )
            pending = [
                r for r in self.reviews(number)
                if r.state == ReviewState.PENDING
                and r.evidence.author.casefold() == self.account.casefold()
            ]
        if pending:
            ids = ", ".join(str(r.evidence.id) for r in pending)
            raise GateError(f"pending_draft: protected review IDs: {ids}")

    def post_review(
        self, number: int, publication: ReviewPublication
    ) -> Review:
        event = {
            ReviewState.APPROVED: "APPROVED",
            ReviewState.CHANGES_REQUESTED: "REQUEST_CHANGES",
            ReviewState.COMMENTED: "COMMENT",
        }[publication.state]
        endpoint = f"{self.prefix}/pulls/{number}/reviews"
        # E2/E6: body first, with durable full finding text, never comments.
        data = object_value(self.api(
            endpoint, method="POST", body={
                "commit_id": publication.head, "event": event,
                "body": publication.fallback_body,
            },
        ), "posted review")
        review_id = positive(data.get("id"), "review ID")
        try:
            review = parse_review(self.api(f"{endpoint}/{review_id}"))
            if (
                review is None or review.evidence.id != review_id
                or review.state != publication.state or review.dismissed
                or review.commit_id != publication.head
                or review.evidence.author.casefold() != self.account.casefold()
                or review.evidence.body
                != publication.fallback_body.replace("\r\n", "\n")
            ):
                raise ForgeError(f"expected {event}, author, head and body")
        except ForgeError as error:
            raise ForgeError(
                f"review {review_id} published; read-back failed: {error}"
            ) from None
        return review

    def _review_comments(self, number: int, review_id: int) -> list[dict]:
        rows = array(self.api(
            f"{self.prefix}/pulls/{number}/reviews/{review_id}/comments",
        ), "review comments")
        # Parsing validates identities and grouping; retain wire positions
        # privately because they cannot be reconstructed from head-side lines.
        parse_comments(rows, review_id)
        return sorted(rows, key=lambda row: row["id"])

    def post_root(
        self, number: int, head: str, review_id: int, anchor: Anchor, body: str
    ) -> Comment:
        endpoint = f"{self.prefix}/pulls/{number}/reviews/{review_id}/comments"
        existing = self._review_comments(number, review_id)
        comments, _ = parse_comments(existing, review_id)
        usable = {c.evidence.id for c in comments
                  if c.line is not None and FINDING.match(c.evidence.body)}
        if any(r["id"] in usable and r["path"] == anchor.path
               and r["position"] == (anchor.start_line or anchor.line)
               for r in existing):
            raise ForgeError("finding conversation anchor already has a root")
        written = object_value(self.api(
            endpoint, method="POST", body={**anchor_payload(anchor),
                                           "body": body},
        ), "posted root")
        comment_id = positive(written.get("id"), "root ID")
        try:
            comments, _ = parse_comments(
                self._review_comments(number, review_id), review_id,
            )
        except ForgeError as error:
            raise ForgeError(
                f"root {comment_id} in review {review_id} written;"
                f" read-back failed: {error}"
            ) from None
        tag = FINDING.match(body)
        matches = [c for c in comments if c.evidence.id == comment_id]
        root = matches[0] if len(matches) == 1 else None
        if (
            root is None or tag is None
            or not root.evidence.body.startswith(tag[0])
            or root.evidence.body != body.replace("\r\n", "\n")
            or root.evidence.author.casefold() != self.account.casefold()
            or root.path != anchor.path or root.line != anchor.line
            or root.start_line != (
                anchor.start_line if anchor.start_line != anchor.line else None
            )
            or root.in_reply_to_id is not None
        ):
            raise ForgeError(
                f"root {comment_id} in review {review_id} is missing,"
                " unanchored or has a read-back mismatch"
            )
        return root

    def reply(self, number: int, root: int, body: str) -> Comment:
        for review in self.reviews(number):
            review_id = review.evidence.id
            rows = self._review_comments(number, review_id)
            found = next((r for r in rows if r["id"] == root), None)
            if found is None:
                continue
            comments, _ = parse_comments(rows, review_id)
            comment = next(c for c in comments if c.evidence.id == root)
            if (not FINDING.match(comment.evidence.body)
                    or comment.line is None or comment.in_reply_to_id):
                raise ForgeError("reply target is not a usable finding root")
            endpoint = (
                f"{self.prefix}/pulls/{number}/reviews/{review_id}/comments"
            )
            written = object_value(self.api(
                endpoint, method="POST", body={
                    "path": found["path"], "new_position": found["position"],
                    "body": body,
                },
            ), "posted reply")
            reply_id = positive(written.get("id"), "reply ID")
            comments, _ = parse_comments(
                self._review_comments(number, review_id), review_id,
            )
            reply = next(
                (c for c in comments if c.evidence.id == reply_id), None,
            )
            if (
                reply is None or reply.in_reply_to_id != root
                or reply.evidence.author.casefold() != self.account.casefold()
                or reply.evidence.body != body.replace("\r\n", "\n")
            ):
                raise ForgeError(f"reply {reply_id} read-back mismatch")
            return reply
        raise ForgeError(f"finding root {root} is missing")

    def comment(self, number: int, body: str) -> Evidence:
        data = self.api(
            f"{self.prefix}/issues/{number}/comments", method="POST",
            body={"body": body},
        )
        evidence = parse_evidence(data)
        if (evidence.author.casefold() != self.account.casefold()
                or evidence.body != body.replace("\r\n", "\n")):
            raise ForgeError(f"comment {evidence.id} response mismatch")
        return evidence

    def resolve(self, node_id: str) -> dict[str, object]:
        raise ForgeError("not supported on this forge")

    def merge(self, number: int, head: str, method: str) -> MergeResult:
        answer: ForgeError | None = None
        try:
            self.api(
                f"{self.prefix}/pulls/{number}/merge", method="POST",
                body={"Do": method, "head_commit_id": head,
                      "delete_branch_after_merge": True},
            )
        except ForgeError as error:
            if error.status is None:
                # No answer arrived (a timeout or a broken connection), so
                # the outcome stays unknown and nothing is read.
                raise
            # F16 merges before it deletes the head branch and answers 403
            # or 500 when that deletion fails, with the PR already merged.
            # One read of the PR decides, whatever the status (#142).
            answer = error
        context = "merge submitted"
        if answer is not None:
            context = f"merge answered {answer.status}" + (
                f": {answer}" if str(answer) else ""
            )
        # A successful POST may have no body. The merged PR's head can change
        # after branch deletion (E9); only its merge commit is used here.
        try:
            pr = self.pr(number)
        except ForgeError as error:
            # A failed read is not a refusal: the merge may have completed.
            # Drop its HTTP status so callers retain an uncertain merge
            # outcome and all local resources.
            raise ForgeError(
                f"{context}; confirmation failed: {error}; re-read the PR"
            ) from None
        if answer is not None and not pr.merged:
            raise MergeRefused(str(answer), answer.status)
        if not pr.merged or pr.merge_commit is None:
            raise ForgeError(
                "merge not confirmed; re-read the PR" if answer is None
                else f"{context}; merge not confirmed; re-read the PR"
            )
        result: MergeResult = {
            "sha": pr.merge_commit, "message": "merge confirmed",
        }
        if answer is not None and answer.status is not None:
            result["answer"] = {
                "status": answer.status, "message": str(answer),
            }
        return result

    def branch_head(self, branch: str) -> str | None:
        try:
            data = object_value(self.api(
                f"{self.prefix}/branches/{quote(branch, safe='')}",
            ), "branch")
        except ForgeError as error:
            if error.status == 404:
                return None
            raise
        if data.get("name") != branch:
            raise ForgeError("branch response has another identity")
        commit = object_value(data.get("commit"), "branch.commit")
        return oid(commit.get("id"), "branch commit ID")

    def delete_branch(self, branch: str, expected_head: str) -> None:
        # The merge deletes the branch before it answers (F16), so there is
        # no deletion to wait for.
        if branch_present(self, branch, expected_head):
            delete_and_confirm(self, branch, expected_head, lambda: self.api(
                f"{self.prefix}/branches/{quote(branch, safe='')}",
                method="DELETE",
            ))
