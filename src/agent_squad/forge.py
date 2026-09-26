"""Typed forge boundary, immutable evidence, and shared review vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, TypedDict, runtime_checkable
import re

from .initialization import AgentSquadError, Repository
from .validation import JsonValidator


Role = Literal["implementer", "reviewer"]


class ReviewState(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    PENDING = "pending"


def requested_state(verdict: str, identity_mode: str = "dual") -> ReviewState:
    """Map the shared protocol verdict to its neutral publication state."""
    if identity_mode == "single" and verdict in (
        "approved", "changes_requested", "needs_human",
    ):
        return ReviewState.COMMENTED
    if verdict == "needs_human":
        return ReviewState.COMMENTED
    if verdict in (ReviewState.APPROVED, ReviewState.CHANGES_REQUESTED):
        return ReviewState(verdict)
    raise AgentSquadError(f"invalid review verdict: {verdict}")


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


@dataclass(frozen=True)
class Review:
    evidence: Evidence
    commit_id: str
    state: ReviewState
    dismissed: bool = False
    # Adapter-supplied display text is never used for authority decisions.
    display_state: str | None = None

    @property
    def state_label(self) -> str:
        return self.display_state or self.state.value


@dataclass(frozen=True)
class Comment:
    evidence: Evidence
    review_id: int
    in_reply_to_id: int | None
    path: str | None
    line: int | None
    start_line: int | None
    side: str | None


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


@dataclass(frozen=True)
class ThreadState:
    root_id: int
    node_id: str | None
    resolved: bool | None


@dataclass(frozen=True)
class Snapshot:
    pr: PullRequest
    reviews: tuple[Review, ...]
    comments: tuple[Comment, ...]
    conversation: tuple[Evidence, ...]
    threads: tuple[ThreadState, ...]
    can_resolve_threads: bool = False
    can_read_thread_resolution: bool = False
    can_read_branch_rules: bool = False
    approved_state_label: str = "approved"
    human_approvals: tuple[Approval, ...] = ()


@dataclass(frozen=True)
class Anchor:
    path: str
    line: int
    start_line: int | None = None


@dataclass(frozen=True)
class Approval:
    login: str
    state: ReviewState
    commit_id: str
    dismissed: bool
    timestamp: str
    id: int


@dataclass(frozen=True)
class ReviewComment:
    anchor: Anchor
    body: str


@dataclass(frozen=True)
class ReviewPublication:
    head: str
    state: ReviewState
    body: str
    fallback_body: str
    comments: tuple[ReviewComment, ...]
    base: str


class IssueRecord(TypedDict):
    number: int
    title: str
    state: str
    is_pull_request: bool
    body: str
    labels: list[str]
    comments: tuple[Evidence, ...]


class MergeResult(TypedDict):
    sha: str
    message: str


@runtime_checkable
class Forge(Protocol):
    """Only operations consumed by this increment; transport stays private."""

    @property
    def role(self) -> Role: ...

    @property
    def account(self) -> str: ...

    @property
    def version_label(self) -> str: ...

    @property
    def can_resolve_threads(self) -> bool: ...

    @property
    def can_read_thread_resolution(self) -> bool: ...

    @property
    def can_read_branch_rules(self) -> bool: ...

    def version(self) -> str: ...
    def verify_identity(self) -> None: ...
    def repository_record(self) -> dict[str, object]: ...
    def repository_permission(self) -> str: ...
    def user_exists(self, login: str) -> None: ...
    def issue(self, number: int) -> IssueRecord: ...
    def pr(self, number: int) -> PullRequest: ...
    def reviews(self, number: int) -> tuple[Review, ...]: ...
    def approvals(self, number: int) -> tuple[Approval, ...]: ...
    def snapshot(self, number: int) -> Snapshot: ...
    def thread_states(self, number: int) -> tuple[ThreadState, ...]: ...
    def branch_rules(self, branch: str) -> dict[str, object]: ...
    def branch_prs(self, branch: str) -> list[dict[str, object]]: ...

    def create_pr(
        self, title: str, head_branch: str, base_branch: str, body: str,
    ) -> PullRequest: ...

    def update_body(self, number: int, body: str) -> PullRequest: ...

    def prepare_review(
        self, number: int, *, reviews: tuple[Review, ...],
    ) -> None: ...

    def post_review(
        self, number: int, publication: ReviewPublication,
    ) -> Review: ...

    def post_root(
        self, number: int, head: str, review_id: int,
        anchor: Anchor, body: str,
    ) -> Comment: ...

    def reply(self, number: int, root: int, body: str) -> Comment: ...
    def comment(self, number: int, body: str) -> Evidence: ...
    def resolve(self, node_id: str) -> dict[str, object]: ...
    def merge(self, number: int, head: str, method: str) -> MergeResult: ...


def make_forge(repository: Repository, role: Role) -> Forge:
    """Select a configured adapter without reading credentials."""
    assert repository.configuration is not None
    kind = repository.configuration.forge.kind
    if kind == "github":
        from .github import GitHub

        return GitHub(repository, role)
    if kind == "forgejo":
        raise AgentSquadError(
            "forge.kind forgejo is not implemented until Increment 3"
        )
    raise AgentSquadError(f"unsupported forge.kind: {kind}")
