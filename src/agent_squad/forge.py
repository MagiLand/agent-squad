"""A bounded GitHub adapter with explicit identity and response validation."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shutil
import subprocess
from urllib.parse import quote, urlencode
import json

from .initialization import AgentSquadError, Repository, decode_json
from .validation import JsonValidator


class ForgeError(AgentSquadError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


V = JsonValidator(ForgeError)


def object_value(value: object, label: str) -> dict[str, object]:
    return V.require_object(value, label)


def array(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise ForgeError(f"{label} must be an array")
    return value


def text_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ForgeError(f"{label} must be a string")
    return value


def positive(value: object, label: str) -> int:
    value = V.require_int(value, label)
    if value < 1:
        raise ForgeError(f"{label} must be positive")
    return value


def boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ForgeError(f"{label} must be a boolean")
    return value


def oid(value: object, label: str) -> str:
    value = V.require_string(value, label)
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise ForgeError(f"{label} must be a full lowercase Git SHA")
    return value


@dataclass(frozen=True)
class Evidence:
    id: int
    author: str
    created_at: str
    body: str

    @classmethod
    def from_dict(cls, value: object, *, review: bool = False) -> Evidence:
        data = object_value(value, "evidence")
        user = object_value(data.get("user"), "evidence.user")
        timestamp = (
            data.get("submitted_at") if review else data.get("created_at")
        )
        if review and data.get("state") == "PENDING":
            timestamp = data.get("created_at") or "1970-01-01T00:00:00Z"
        return cls(
            positive(data.get("id"), "evidence.id"),
            V.require_string(user.get("login"), "evidence.user.login"),
            V.require_timestamp(timestamp, "evidence.timestamp"),
            text_value(
                "" if data.get("body") is None else data["body"],
                "evidence.body",
            ).replace("\r\n", "\n"),
        )


@dataclass(frozen=True)
class Review:
    evidence: Evidence
    commit_id: str
    state: str

    @classmethod
    def from_dict(cls, value: object) -> Review:
        data = object_value(value, "review")
        state = V.require_string(data.get("state"), "review.state")
        if state not in (
            "PENDING",
            "APPROVED",
            "CHANGES_REQUESTED",
            "COMMENTED",
            "DISMISSED",
        ):
            raise ForgeError(f"unknown review state: {state}")
        return cls(
            Evidence.from_dict(data, review=True),
            oid(data.get("commit_id"), "review.commit_id"),
            state,
        )


@dataclass(frozen=True)
class Comment:
    evidence: Evidence
    review_id: int
    in_reply_to_id: int | None
    path: str | None
    line: int | None
    start_line: int | None
    side: str | None

    @classmethod
    def from_dict(cls, value: object) -> Comment:
        data = object_value(value, "review comment")
        numbers = {
            key: None if data.get(key) is None else positive(data[key], key)
            for key in ("in_reply_to_id", "line", "start_line")
        }
        strings = {
            key: (
                None
                if data.get(key) is None
                else V.require_string(data[key], key)
            )
            for key in ("path", "side")
        }
        return cls(
            Evidence.from_dict(data),
            positive(
                data.get("pull_request_review_id"), "pull_request_review_id"
            ),
            numbers["in_reply_to_id"],
            strings["path"],
            numbers["line"],
            numbers["start_line"],
            strings["side"],
        )


@dataclass(frozen=True)
class PullRequest:
    evidence: Evidence
    number: int
    title: str
    head: str
    head_branch: str
    base: str
    base_branch: str
    state: str
    merged: bool
    merge_commit: str | None
    mergeable_state: str | None

    @classmethod
    def from_dict(cls, value: object) -> PullRequest:
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
        return cls(
            Evidence.from_dict(data),
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


@dataclass(frozen=True)
class ThreadState:
    root_id: int
    node_id: str
    resolved: bool


@dataclass(frozen=True)
class Snapshot:
    pr: PullRequest
    reviews: tuple[Review, ...]
    comments: tuple[Comment, ...]
    conversation: tuple[Evidence, ...]
    threads: tuple[ThreadState, ...]


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

    def __init__(
        self, repository: Repository, role: str, *, timeout: float = 45
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

    def issue(self, number: int) -> dict[str, object]:
        data = object_value(
            self.api(f"{self.prefix}/issues/{number}"), "issue"
        )
        evidence = Evidence.from_dict(data)
        labels = [
            V.require_string(
                object_value(label, "label").get("name"), "label.name"
            )
            for label in array(data.get("labels"), "labels")
        ]
        comments = tuple(
            Evidence.from_dict(c)
            for c in self.listing(f"{self.prefix}/issues/{number}/comments")
        )
        return {
            "number": positive(data.get("number"), "issue.number"),
            "title": text_value(data.get("title"), "issue.title"),
            "body": evidence.body,
            "labels": labels,
            "comments": comments,
        }

    def pr(self, number: int) -> PullRequest:
        return PullRequest.from_dict(self.api(f"{self.prefix}/pulls/{number}"))

    def reviews(self, number: int) -> tuple[Review, ...]:
        return tuple(
            Review.from_dict(r)
            for r in self.listing(f"{self.prefix}/pulls/{number}/reviews")
        )

    def snapshot(self, number: int) -> Snapshot:
        pr = self.pr(number)
        reviews = self.reviews(number)
        flat = {
            c.evidence.id: c
            for c in (
                Comment.from_dict(c)
                for c in self.listing(f"{self.prefix}/pulls/{number}/comments")
            )
        }
        comments = dict(flat)
        for review in reviews:
            for value in self.listing(
                f"{self.prefix}/pulls/{number}/reviews/"
                f"{review.evidence.id}/comments"
            ):
                comment = Comment.from_dict(value)
                # The flat endpoint has anchors; the per-review endpoint has
                # all replies.
                comments[comment.evidence.id] = flat.get(
                    comment.evidence.id, comment
                )
        conversation = tuple(
            Evidence.from_dict(c)
            for c in self.listing(f"{self.prefix}/issues/{number}/comments")
        )
        return Snapshot(
            pr,
            reviews,
            tuple(comments.values()),
            conversation,
            self.thread_states(number),
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

    def create_pr(self, body: dict[str, object]) -> PullRequest:
        return PullRequest.from_dict(
            self.api(f"{self.prefix}/pulls", method="POST", body=body)
        )

    def branch_prs(self, branch: str) -> list:
        config = self.repository.configuration
        query = urlencode(
            {"head": f"{config.forge.owner}:{branch}", "state": "all"}
        )
        return self.listing(f"{self.prefix}/pulls?{query}")

    def update_body(self, number: int, body: str) -> PullRequest:
        return PullRequest.from_dict(
            self.api(
                f"{self.prefix}/pulls/{number}",
                method="PATCH",
                body={"body": body},
            )
        )

    def post_review(self, number: int, payload: dict[str, object]) -> Review:
        return Review.from_dict(
            self.api(
                f"{self.prefix}/pulls/{number}/reviews",
                method="POST",
                body=payload,
            )
        )

    def submit_pending(self, number: int, review: int) -> Review:
        return Review.from_dict(
            self.api(
                f"{self.prefix}/pulls/{number}/reviews/{review}/events",
                method="POST",
                body={"event": "COMMENT"},
            )
        )

    def post_root(self, number: int, payload: dict[str, object]) -> Comment:
        return Comment.from_dict(
            self.api(
                f"{self.prefix}/pulls/{number}/comments",
                method="POST",
                body=payload,
            )
        )

    def reply(self, number: int, root: int, body: str) -> Comment:
        return Comment.from_dict(
            self.api(
                f"{self.prefix}/pulls/{number}/comments/{root}/replies",
                method="POST",
                body={"body": body},
            )
        )

    def comment(self, number: int, body: str) -> Evidence:
        return Evidence.from_dict(
            self.api(
                f"{self.prefix}/issues/{number}/comments",
                method="POST",
                body={"body": body},
            )
        )

    def resolve(self, node_id: str) -> dict[str, object]:
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
