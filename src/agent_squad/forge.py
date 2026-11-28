"""Typed forge boundary, immutable evidence, and shared review vocabulary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import (
    Callable, Literal, NotRequired, Protocol, TypedDict, TypeVar,
    runtime_checkable,
)
import re
import unicodedata

from .initialization import AgentSquadError, Repository
from .validation import JsonValidator


Role = Literal["implementer", "reviewer"]


class ReviewState(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    PENDING = "pending"


def requested_state(verdict: str) -> ReviewState:
    """Map the shared protocol verdict to its neutral publication state."""
    if verdict == "needs_human":
        return ReviewState.COMMENTED
    if verdict in (ReviewState.APPROVED, ReviewState.CHANGES_REQUESTED):
        return ReviewState(verdict)
    raise AgentSquadError(f"invalid review verdict: {verdict}")


class ForgeError(AgentSquadError):
    """A forge failure. ``written`` marks one raised after the forge
    answered a write with the new object's ID, so the write happened
    although the command failed (docs/events.md)."""

    def __init__(
        self, message: str, status: int | None = None, *,
        written: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.written = written


class MergeRefused(ForgeError):
    """An error answer to a merge request, after which a read of the PR
    showed it still unmerged: a refusal whatever its status (#142)."""


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
    pending_drafts: tuple[int, ...] = ()
    protects_pending_drafts: bool = False
    # The checks at pr.head when the adapter read them in the same read
    # (#149); null when it did not and ``checks`` must read them.
    head_checks: CommitChecks | None = None


@dataclass(frozen=True)
class Anchor:
    path: str
    line: int
    start_line: int | None = None


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


CheckResult = Literal["passed", "skipped", "failed", "pending"]
# HTTP statuses with which a forge refuses the checks read for the acting
# identity (#145); any other failure remains an error.
CHECK_REFUSALS = (401, 403, 404)


@dataclass(frozen=True)
class Check:
    """One check recorded for a commit; the adapter sets ``result`` (§11.0)."""

    name: str
    event: str | None
    state: str
    result: CheckResult
    source: str | None
    url: str | None
    started_at: str | None
    completed_at: str | None
    # True only for a GitHub check run, whatever its app: a commit status
    # can carry any creator login, ``github-actions`` included, so only
    # this says that ``url`` names a job (#176). Not in the public record.
    check_run: bool = False
    # The check run's own ID, which for an Actions job is the job's ID, so
    # the job a URL names must be this one (#176). Not in the public record.
    check_run_id: int | None = None

    def record(self) -> dict[str, object]:
        """The public check record of §11.0."""
        row = asdict(self)
        del row["check_run"], row["check_run_id"]
        return row


@dataclass(frozen=True)
class UnfinishedRun:
    """A GitHub Actions workflow run at the commit that has not completed.

    GitHub creates a job behind ``needs`` only when the jobs it needs have
    finished, so its check run is missing until then (#169).
    """

    id: int
    status: str

    def record(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CommitChecks:
    """What one checks read found at a commit (§11.0).

    ``unfinished_runs`` is ordered by ID; Forgejo never reports one.
    """

    checks: tuple[Check, ...]
    unfinished_runs: tuple[UnfinishedRun, ...] = ()


class ChecksUnavailable(ForgeError):
    """The forge refused the checks read for the acting identity."""


class NoJobLog(ForgeError):
    """No Actions job of this repository matches a check exactly (#176)."""


@dataclass(frozen=True)
class JobLog:
    """A failed check's Actions job and its latest attempt's log (#176)."""

    job_id: int
    text: str


def visible(text: str) -> str:
    """Show every control character except newline and tab as ``\\xNN``.

    Forge text such as a job log carries what a job printed, colour codes
    and other terminal sequences included (#176).
    """
    return "".join(
        f"\\x{ord(c):02x}"
        if c not in "\n\t" and unicodedata.category(c) == "Cc" else c
        for c in text
    )


class IssueRecord(TypedDict):
    number: int
    title: str
    state: str
    is_pull_request: bool
    body: str
    labels: list[str]
    # The title of the issue's milestone, or null (#164).
    milestone: str | None
    comments: tuple[Evidence, ...]


def milestone_title(value: object, label: str) -> str | None:
    """Read a milestone object's title; null when the issue has none."""
    if value is None:
        return None
    return text_value(
        object_value(value, label).get("title"), f"{label}.title"
    )


@dataclass(frozen=True)
class Milestone:
    """One milestone of the repository, open or closed (#164).

    ``number`` is the forge's own reference: GitHub's milestone number,
    Forgejo's milestone ID.
    """

    number: int
    title: str
    state: str


def parse_milestone(value: object, key: str) -> Milestone:
    """Read one milestone; ``key`` names the forge's reference field."""
    data = object_value(value, "milestone")
    state = data.get("state")
    if state not in ("open", "closed"):
        raise ForgeError("milestone.state must be open or closed")
    return Milestone(
        positive(data.get(key), f"milestone.{key}"),
        text_value(data.get("title"), "milestone.title"),
        state,
    )


class ClosingPullRequest(TypedDict):
    """A merged pull request the forge links to an issue as closing it."""

    number: int
    # The pull request's repository as ``owner/name``.
    repository: str


class ListedIssue(TypedDict):
    """One issue an adapter read for ``issue list`` (#164).

    The command prints every field but ``is_pull_request``; there is no
    body and no comment.
    """

    number: int
    title: str
    state: str
    # The forge's reason in lower case; null where the forge gives none.
    state_reason: str | None
    labels: list[str]
    milestone: str | None
    is_pull_request: bool
    # Empty for an open issue, for which the forge is not asked; null,
    # meaning unknown, when the forge refuses the read after the listing
    # was read or, on Forgejo, when the timeline read did not reach the
    # issue's last close (decision 5 of #164).
    closed_by: list[ClosingPullRequest] | None


IssueState = Literal["open", "closed", "all"]


@dataclass(frozen=True)
class IssueQuery:
    """What ``issue list`` asks for; ``matches`` decides on each record."""

    state: IssueState
    milestone: Milestone | None
    labels: tuple[str, ...]

    def matches(self, issue: ListedIssue) -> bool:
        """Whether an issue's own record fits the query.

        A forge's filter may be ignored or may select another milestone, so
        no row is trusted to match because the forge returned it.
        """
        return (
            not issue["is_pull_request"]
            and self.state in ("all", issue["state"])
            and (
                self.milestone is None
                or issue["milestone"] == self.milestone.title
            )
            and all(name in issue["labels"] for name in self.labels)
        )


def closing_order(
    found: list[ClosingPullRequest],
) -> list[ClosingPullRequest]:
    """Each pull request once, ordered by repository and number."""
    unique = {(pr["repository"], pr["number"]): pr for pr in found}
    return [unique[key] for key in sorted(unique)]


class RelatedIssue(TypedDict):
    """An issue on the other side of a dependency (#144)."""

    number: int
    title: str
    state: str
    # The related issue's repository as ``owner/name``.
    repository: str


class IssueDependencies(TypedDict):
    """Each list is null when the forge does not expose it (#144)."""

    blocks: list[RelatedIssue] | None
    blocked_by: list[RelatedIssue] | None


# HTTP statuses with which a forge refuses a dependency read for the acting
# identity after the issue itself was read (#144).
DEPENDENCY_REFUSALS = (401, 403, 404)


def related_issue(
    number: object, title: object, state: object, repository: str,
) -> RelatedIssue:
    if state not in ("open", "closed"):
        raise ForgeError("related issue state must be open or closed")
    return {
        "number": positive(number, "related issue number"),
        "title": text_value(title, "related issue title"),
        "state": state,
        "repository": repository,
    }


Exposed = TypeVar("Exposed")


def exposed(read: Callable[[], Exposed | None]) -> Exposed | None:
    """Return one list read after the main read, or null when the forge
    refuses it: a dependency list (#144) or an issue's closing pull
    requests (#164)."""
    try:
        return read()
    except ForgeError as error:
        if error.status in DEPENDENCY_REFUSALS:
            return None
        raise


def label_scope(name: str) -> str | None:
    """The scope a label name would have if exclusive: the name before its
    last ``/``, when that ``/`` is neither first nor last (§11.0)."""
    cut = name.rfind("/")
    return name[:cut] if 0 < cut < len(name) - 1 else None


@dataclass(frozen=True)
class Label:
    """A label found by its exact name to put on pull requests (#174).

    ``id`` is the forge's label ID; the adapter chooses whether it applies
    the label by ID or by name. ``exclusive`` is Forgejo's flag: adding an
    exclusive label removes the other labels of its scope (§11.4).
    """

    name: str
    id: int
    exclusive: bool = False

    @property
    def scope(self) -> str | None:
        """The exclusive scope, or none for a label that is not exclusive."""
        return label_scope(self.name) if self.exclusive else None


class MergeAnswer(TypedDict):
    status: int
    message: str


class MergeResult(TypedDict):
    sha: str
    message: str
    # The error answer of a merge request the forge completed (#142).
    answer: NotRequired[MergeAnswer]


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
    def credential_label(self) -> str | None: ...

    @property
    def can_mutate(self) -> bool: ...

    @property
    def mutation_unavailable_message(self) -> str: ...

    @property
    def can_resolve_threads(self) -> bool: ...

    @property
    def can_read_thread_resolution(self) -> bool: ...

    @property
    def can_read_branch_rules(self) -> bool: ...

    @property
    def log_end_marker(self) -> str | None: ...

    def version(self) -> str: ...
    def verify_credentials(self) -> None: ...
    def verify_identity(self) -> None: ...
    def repository_record(self) -> dict[str, object]: ...
    def repository_permission(self) -> str: ...
    def issue(self, number: int) -> IssueRecord: ...
    def open_issues(self) -> dict[int, str]: ...
    def labels(self) -> tuple[str, ...]: ...
    def milestones(self) -> tuple[Milestone, ...]: ...
    def issues(self, query: IssueQuery) -> list[ListedIssue]: ...

    def create_issue(
        self, title: str, body: str, label: str,
    ) -> IssueRecord: ...

    def issue_dependencies(self, number: int) -> IssueDependencies: ...

    def change_issue_labels(
        self, number: int, add: tuple[str, ...], remove: tuple[str, ...],
    ) -> None: ...

    def pr(self, number: int) -> PullRequest: ...
    def reviews(self, number: int) -> tuple[Review, ...]: ...

    def snapshot(
        self, number: int, *, with_checks: bool = False,
        conversation: bool = True,
    ) -> Snapshot:
        """Read the PR with its reviews and review comments. Without
        ``conversation`` the PR's ordinary conversation is not read, and
        the snapshot holds none (#175)."""
        ...

    def branch_rules(self, branch: str) -> dict[str, object]: ...
    def checks(self, sha: str) -> CommitChecks: ...
    def job_log(self, check: Check, sha: str) -> JobLog: ...
    def branch_head(self, branch: str) -> str | None: ...
    def delete_branch(self, branch: str, expected_head: str) -> None: ...
    def validate_anchors(self, anchors: tuple[Anchor, ...]) -> None: ...

    def branch_prs(
        self, branch: str, complete: bool = False,
    ) -> list[dict[str, object]]: ...

    def pr_label(self, name: str) -> Label | None: ...

    def create_pr(
        self, title: str, head_branch: str, base_branch: str, body: str,
        label: Label | None = None,
        on_created: Callable[[int], None] | None = None,
    ) -> PullRequest: ...

    def add_pr_label(self, number: int, label: Label) -> None: ...
    def pr_labels(self, number: int) -> tuple[str, ...]: ...

    def update_body(self, number: int, body: str) -> PullRequest: ...

    def prepare_review(
        self, number: int, *, reviews: tuple[Review, ...],
        discard_draft: int | None = None,
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


def branch_present(forge: Forge, branch: str, expected_head: str) -> bool:
    """Read the branch; refuse one that moved from the approved head."""
    head = forge.branch_head(branch)
    if head is not None and head != expected_head:
        raise ForgeError("remote branch no longer matches approved head")
    return head is not None


def delete_and_confirm(
    forge: Forge, branch: str, expected_head: str,
    delete: Callable[[], object],
) -> None:
    """Send one DELETE for a branch read at the approved head, then read it.

    A DELETE that loses a race with another deletion fails (GitHub 404 or
    422, Forgejo 500), so only the fresh read decides (#120).
    """
    try:
        delete()
    except ForgeError as error:
        try:
            if not branch_present(forge, branch, expected_head):
                return
        except ForgeError as read_error:
            raise ForgeError(
                f"{error}; after the failed DELETE: {read_error}"
            ) from None
        raise
    if branch_present(forge, branch, expected_head):
        raise ForgeError(f"remote branch remains: {branch}")


def make_forge(repository: Repository, role: Role) -> Forge:
    """Select a configured adapter without reading credentials."""
    assert repository.configuration is not None
    kind = repository.configuration.forge.kind
    if kind == "github":
        from .github import GitHub

        return GitHub(repository, role)
    if kind == "forgejo":
        from .forgejo import Forgejo

        return Forgejo(repository, role)
    raise AgentSquadError(f"unsupported forge.kind: {kind}")
