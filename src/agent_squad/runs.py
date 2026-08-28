"""Creation and inspection of one authoritative local Agent Squad run."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import uuid

from .artifacts import (
    ActiveRoundRecord,
    ApprovalRecord,
    ArtifactValidationError,
    BundleArtifact,
    HandoffRecord,
    HandoffStatus,
    ReviewRequest,
    ReviewResult,
    ReviewRoundRecord,
    ReviewVerdict,
    ReviewerLocalMarker,
    RoundStatus,
)
from .initialization import (
    AgentKind,
    AgentSquadError,
    GitWorktree,
    InitializedRepository,
    SCHEMA_VERSION,
    load_initialized_repository,
    run_git,
)
from .review_submissions import load_marker_confirmed_review
from .storage import (
    InvalidJsonError,
    atomic_write,
    decode_json,
    encode_event,
    encode_json,
    exclusive_file_lock,
    read_regular_tree,
    utc_timestamp,
)
from .validation import JsonValidator


STATE_FILE_NAME = "state.json"
LOCK_FILE_NAME = "lock"
RUNS_DIRECTORY_NAME = "runs"
RUN_RECORD_FILE_NAME = "run.json"
TASK_FILE_NAME = "task.md"
EVENT_LOG_FILE_NAME = "events.jsonl"
CONTEXT_DIRECTORY_NAME = "context"


class RunError(AgentSquadError):
    """Raised when run state cannot be created or inspected safely."""


class RunStartError(RunError):
    """Raised when a requested run cannot be started atomically."""


class RunStateError(RunError):
    """Raised when authoritative run artifacts are invalid or inconsistent."""


_VALIDATOR = JsonValidator(RunStateError)
_require_object = _VALIDATOR.require_object
_check_fields = _VALIDATOR.check_fields
_require_string = _VALIDATOR.require_string
_require_int = _VALIDATOR.require_int
_require_uuid = _VALIDATOR.require_uuid
_require_digest = _VALIDATOR.require_digest
_require_oid = _VALIDATOR.require_oid
_require_timestamp = _VALIDATOR.require_timestamp
_require_optional_string = _VALIDATOR.require_optional_string
_require_absolute_path = _VALIDATOR.require_absolute_path
_require_object_format = _VALIDATOR.require_object_format


class RunPhase(StrEnum):
    """Authoritative phases for an Agent Squad run."""

    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    NEEDS_HUMAN = "needs_human"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


TERMINAL_PHASES = {RunPhase.COMPLETED, RunPhase.CANCELLED}
CLOSED_ROUND_STATUSES = {
    RoundStatus.APPLIED,
    RoundStatus.SUPERSEDED,
    RoundStatus.STALE,
    RoundStatus.INVALID,
}


@dataclass(frozen=True)
class ReviewBudget:
    """Authoritative accounting for applied changes-requested reviews."""

    original_limit: int
    additional_rounds_granted: int
    effective_limit: int
    completed_change_reviews: int

    @classmethod
    def initial(cls, original_limit: int) -> "ReviewBudget":
        """Create the initial budget for a new run."""

        return cls(
            original_limit=original_limit,
            additional_rounds_granted=0,
            effective_limit=original_limit,
            completed_change_reviews=0,
        )

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        path: str = "state.review_budget",
    ) -> "ReviewBudget":
        """Validate budget data loaded from authoritative state."""

        data = _require_object(value, path)
        _check_fields(
            data,
            required={
                "original_limit",
                "additional_rounds_granted",
                "effective_limit",
                "completed_change_reviews",
            },
            path=path,
        )
        budget = cls(
            original_limit=_require_int(
                data["original_limit"],
                f"{path}.original_limit",
            ),
            additional_rounds_granted=_require_int(
                data["additional_rounds_granted"],
                f"{path}.additional_rounds_granted",
            ),
            effective_limit=_require_int(
                data["effective_limit"],
                f"{path}.effective_limit",
            ),
            completed_change_reviews=_require_int(
                data["completed_change_reviews"],
                f"{path}.completed_change_reviews",
            ),
        )
        if budget.original_limit < 1:
            raise RunStateError(f"{path}.original_limit must be positive")
        if budget.additional_rounds_granted < 0:
            raise RunStateError(
                f"{path}.additional_rounds_granted must not be negative"
            )
        if budget.effective_limit != (
            budget.original_limit + budget.additional_rounds_granted
        ):
            raise RunStateError(
                f"{path}.effective_limit must equal the original limit plus "
                "granted rounds"
            )
        if not 0 <= budget.completed_change_reviews <= budget.effective_limit:
            raise RunStateError(
                f"{path}.completed_change_reviews must be between zero and "
                "the effective limit"
            )
        return budget

    def to_dict(self) -> dict[str, int]:
        """Return the stable JSON representation of this budget."""

        return {
            "original_limit": self.original_limit,
            "additional_rounds_granted": self.additional_rounds_granted,
            "effective_limit": self.effective_limit,
            "completed_change_reviews": self.completed_change_reviews,
        }


@dataclass(frozen=True)
class RepositoryIdentity:
    """Canonical local Git identity for an implementation worktree."""

    implementation_root: Path
    git_common_dir: Path
    worktree_git_dir: Path
    repository_id: str
    start_branch_ref: str | None
    start_head_detached: bool


@dataclass(frozen=True)
class CapturedInput:
    """One input captured into immutable run storage."""

    source_path: Path
    run_path: str
    sha256: str
    content: bytes

    def to_record(self) -> dict[str, str]:
        """Return metadata suitable for ``run.json``."""

        return {
            "source_path": str(self.source_path),
            "path": self.run_path,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class _CapturedRecord:
    """Validated metadata for one captured run input."""

    source_path: Path
    run_path: str
    sha256: str


@dataclass(frozen=True)
class _ImplementerRecord:
    """Validated Implementer selection stored in a run record."""

    agent_name: str
    kind: AgentKind


@dataclass(frozen=True)
class _ReviewerRecord:
    """Validated Reviewer selection stored in a run record."""

    kind: AgentKind
    start_args: tuple[str, ...]


@dataclass(frozen=True)
class _ValidatedRunRecord:
    """Typed view of one fully validated immutable run record."""

    phase: RunPhase
    task_path: Path
    task_sha256: str
    context_records: tuple[_CapturedRecord, ...]
    repository: RepositoryIdentity
    base_ref: str
    base_oid: str
    object_format: str
    implementer: _ImplementerRecord
    reviewer: _ReviewerRecord
    initial_budget: ReviewBudget

    @property
    def context_count(self) -> int:
        """Return the number of immutable context inputs."""

        return len(self.context_records)


@dataclass(frozen=True)
class StartRunResult:
    """Stable details returned after a run reaches its commit point."""

    run_id: str
    phase: RunPhase
    run_directory: Path
    task_path: Path
    state_path: Path
    repository_id: str
    base_ref: str
    base_oid: str
    next_action: str


@dataclass(frozen=True)
class ActiveRunStatus:
    """Validated active-run details used by the status command."""

    run_id: str
    phase: RunPhase
    task_path: Path
    task_sha256: str
    context_count: int
    repository: RepositoryIdentity
    base_ref: str
    base_oid: str
    git_object_format: str
    implementer_agent: str
    implementer_kind: AgentKind
    reviewer_kind: AgentKind
    reviewer_start_args: tuple[str, ...]
    current_round: int
    current_head_oid: str | None
    approved_head_oid: str | None
    active_escalation_id: str | None
    active_round: ActiveRoundRecord | None
    handoff: HandoffRecord | None
    review_worktree_available: bool | None
    unapplied_result: UnappliedReviewResult | None
    unapplied_result_error: str | None
    approval: ApprovalRecord | None
    review_budget: ReviewBudget


@dataclass(frozen=True)
class RepositoryStatus:
    """Initialized repository status, with or without an active run."""

    repository_root: Path
    repository_id: str
    git_common_dir: Path
    worktree_git_dir: Path
    active_run: ActiveRunStatus | None
    next_action: str


@dataclass(frozen=True)
class UnappliedReviewResult:
    """One validated marker-confirmed result awaiting application."""

    result_id: str
    verdict: ReviewVerdict
    result_path: Path


@dataclass(frozen=True)
class CompletedRunStatus:
    """Validated authority needed to replay one completed run."""

    run_id: str
    run_directory: Path
    round_number: int
    result_id: str
    approved_head_oid: str
    finished_at: str


def start_run(
    start: Path,
    *,
    task_path: Path,
    context_paths: Sequence[Path] = (),
    implementer_agent: str | None = None,
    reviewer_kind: AgentKind | None = None,
    base_ref: str | None = None,
) -> StartRunResult:
    """Capture inputs and atomically start one run in this worktree."""

    repository = load_initialized_repository(start)
    configuration = repository.configuration
    selected_implementer = _ImplementerRecord(
        agent_name=_validate_selection(
            (
                implementer_agent
                if implementer_agent is not None
                else configuration.implementer.agent_name
            ),
            "Implementer agent identity",
        ),
        kind=configuration.implementer.kind,
    )
    selected_reviewer_kind = reviewer_kind or configuration.reviewer.kind
    selected_reviewer = _ReviewerRecord(
        kind=selected_reviewer_kind,
        start_args=(
            configuration.reviewer.start_args
            if selected_reviewer_kind == configuration.reviewer.kind
            else ()
        ),
    )
    selected_base = _validate_selection(
        (
            base_ref
            if base_ref is not None
            else configuration.base_ref
        ),
        "base reference",
    )
    lock_path = repository.control_root / LOCK_FILE_NAME
    try:
        with exclusive_file_lock(lock_path):
            return _start_run_locked(
                repository,
                task_path=task_path,
                context_paths=context_paths,
                implementer=selected_implementer,
                reviewer=selected_reviewer,
                base_ref=selected_base,
            )
    except RunError:
        raise
    except OSError as error:
        raise RunStartError(
            f"could not acquire or use the local run lock {lock_path}: {error}"
        ) from error


def inspect_status(start: Path) -> RepositoryStatus:
    """Return validated state while holding the active-run lock."""

    return _inspect_status(start, lock_held=False)


def inspect_status_locked(start: Path) -> RepositoryStatus:
    """Return validated state when the caller holds the canonical lock."""

    return _inspect_status(start, lock_held=True)


def load_completed_run(
    repository: InitializedRepository,
) -> CompletedRunStatus | None:
    """Load and validate terminal completion authority, when present."""

    state_path = repository.control_root / STATE_FILE_NAME
    state = _load_existing_state(state_path)
    if state is None or state.get("active_run_id") is not None:
        return None
    if state.get("phase") != RunPhase.COMPLETED.value:
        return None
    _check_fields(
        state,
        required={
            "schema_version",
            "updated_at",
            "active_run_id",
            "phase",
            "implementation_root",
            "git_common_dir",
            "worktree_git_dir",
            "repository_id",
            "base_oid",
            "current_round",
            "current_head_oid",
            "approved_head_oid",
            "active_escalation_id",
            "active_round",
            "review_budget",
            "handoff",
            "terminal_run_id",
        },
        path="completed state",
    )
    _require_timestamp(state["updated_at"], "completed state.updated_at")
    run_id = _require_uuid(
        state["terminal_run_id"],
        "completed state.terminal_run_id",
    )
    run_directory = safe_run_directory(repository.control_root, run_id)
    run_data = load_json_object(
        run_directory / RUN_RECORD_FILE_NAME,
        "completed run record",
    )
    record = _validate_run_record(run_data, run_directory, run_id)
    if record.phase is not RunPhase.COMPLETED:
        raise RunStateError(
            "completed state does not match its terminal run record"
        )
    finished_at = _require_timestamp(
        run_data["finished_at"],
        "completed run record.finished_at",
    )
    identity_values = (
        (
            state["implementation_root"],
            record.repository.implementation_root,
            "implementation_root",
            "implementation root",
        ),
        (
            state["git_common_dir"],
            record.repository.git_common_dir,
            "git_common_dir",
            "Git common directory",
        ),
        (
            state["worktree_git_dir"],
            record.repository.worktree_git_dir,
            "worktree_git_dir",
            "worktree Git directory",
        ),
        (
            state["repository_id"],
            record.repository.repository_id,
            "repository_id",
            "repository ID",
        ),
    )
    for value, expected, field, label in identity_values:
        _assert_matching_state_value(
            value,
            expected,
            field=field,
            label=label,
        )
    _assert_current_identity(
        record.repository,
        repository_identity(repository.worktree),
    )
    _assert_matching_state_value(
        state["base_oid"],
        record.base_oid,
        field="base_oid",
        label="base OID",
    )

    round_number = _require_nonnegative_int(
        state["current_round"],
        "completed state.current_round",
    )
    if round_number < 1:
        raise RunStateError("completed state has no valid approved round")
    current_head_oid = _require_optional_string(
        state["current_head_oid"],
        "completed state.current_head_oid",
    )
    approved_head_oid = _require_optional_string(
        state["approved_head_oid"],
        "completed state.approved_head_oid",
    )
    if current_head_oid is None or approved_head_oid is None or (
        current_head_oid != approved_head_oid
    ):
        raise RunStateError(
            "completed state must retain one exact approved head"
        )
    _require_oid(
        approved_head_oid,
        record.object_format,
        "completed state.approved_head_oid",
    )
    try:
        active_round = ActiveRoundRecord.from_dict(state["active_round"])
    except ArtifactValidationError as error:
        raise RunStateError(str(error)) from error
    _, round_record = _validate_active_review_artifacts(
        run_directory=run_directory,
        record=record,
        run_id=run_id,
        current_round=round_number,
        current_head_oid=current_head_oid,
        active_round=active_round,
        validate_live_worktree=False,
    )
    approval = _validate_approval_artifacts(
        run_directory=run_directory,
        round_record=round_record,
        run_id=run_id,
        record=record,
        approved_head_oid=approved_head_oid,
    )
    return CompletedRunStatus(
        run_id=run_id,
        run_directory=run_directory,
        round_number=round_number,
        result_id=approval.result_id,
        approved_head_oid=approval.head_oid,
        finished_at=finished_at,
    )


def _inspect_status(
    start: Path,
    *,
    lock_held: bool,
) -> RepositoryStatus:
    """Implement status inspection with explicit lock ownership."""

    repository = load_initialized_repository(start)
    current_identity = repository_identity(repository.worktree)
    state_path = repository.control_root / STATE_FILE_NAME
    state = _load_existing_state(state_path)
    if state is None or state["active_run_id"] is None:
        return _idle_status(current_identity)
    if not lock_held:
        lock_path = repository.control_root / LOCK_FILE_NAME
        if lock_path.is_symlink() or not lock_path.is_file():
            raise RunStateError(
                "an active run must have a regular non-symlink lock file: "
                f"{lock_path}"
            )
        try:
            with exclusive_file_lock(lock_path):
                return _inspect_status(start, lock_held=True)
        except RunError:
            raise
        except OSError as error:
            raise RunStateError(
                f"could not acquire the active-run lock {lock_path}: {error}"
            ) from error

    active_run_id = _require_uuid(
        state["active_run_id"],
        "state.active_run_id",
    )
    phase = _require_phase(state.get("phase"), "state.phase")
    if phase in TERMINAL_PHASES:
        raise RunStateError(
            f"state.active_run_id must be null when phase is {phase.value}"
        )
    _check_fields(
        state,
        required={
            "schema_version",
            "updated_at",
            "active_run_id",
            "phase",
            "implementation_root",
            "git_common_dir",
            "worktree_git_dir",
            "repository_id",
            "base_oid",
            "current_round",
            "current_head_oid",
            "approved_head_oid",
            "active_escalation_id",
            "active_round",
            "review_budget",
            "handoff",
        },
        path="state",
    )
    _require_timestamp(state["updated_at"], "state.updated_at")
    budget = ReviewBudget.from_dict(state["review_budget"])
    current_round = _require_nonnegative_int(
        state["current_round"], "state.current_round"
    )
    current_head_oid = _require_optional_string(
        state["current_head_oid"], "state.current_head_oid"
    )
    approved_head_oid = _require_optional_string(
        state["approved_head_oid"], "state.approved_head_oid"
    )
    active_escalation_id = _require_optional_string(
        state["active_escalation_id"], "state.active_escalation_id"
    )
    active_round_value = state["active_round"]
    handoff_value = state["handoff"]
    try:
        active_round = (
            None
            if active_round_value is None
            else ActiveRoundRecord.from_dict(active_round_value)
        )
        handoff = (
            None
            if handoff_value is None
            else HandoffRecord.from_dict(handoff_value)
        )
    except ArtifactValidationError as error:
        raise RunStateError(str(error)) from error

    run_directory = safe_run_directory(repository.control_root, active_run_id)
    run_record_path = run_directory / RUN_RECORD_FILE_NAME
    run_record = load_json_object(run_record_path, "active run record")
    record = _validate_run_record(run_record, run_directory, active_run_id)

    stored_identity = record.repository
    _assert_matching_state_value(
        state["implementation_root"],
        stored_identity.implementation_root,
        field="implementation_root",
        label="implementation root",
    )
    _assert_matching_state_value(
        state["git_common_dir"],
        stored_identity.git_common_dir,
        field="git_common_dir",
        label="Git common directory",
    )
    _assert_matching_state_value(
        state["worktree_git_dir"],
        stored_identity.worktree_git_dir,
        field="worktree_git_dir",
        label="worktree Git directory",
    )
    _assert_matching_state_value(
        state["repository_id"],
        stored_identity.repository_id,
        field="repository_id",
        label="repository ID",
    )
    _assert_current_identity(stored_identity, current_identity)
    _assert_matching_state_value(
        state["base_oid"],
        record.base_oid,
        field="base_oid",
        label="base OID",
    )
    if phase is not record.phase:
        raise RunStateError(
            "state.phase does not match the active run record phase"
        )
    if budget.original_limit != record.initial_budget.original_limit:
        raise RunStateError(
            "state review-budget original limit does not match run metadata"
        )
    validated_round = _validate_active_state_shape(
        phase=phase,
        current_round=current_round,
        current_head_oid=current_head_oid,
        approved_head_oid=approved_head_oid,
        active_escalation_id=active_escalation_id,
        active_round=active_round,
        handoff=handoff,
        object_format=record.object_format,
    )

    _validate_event_log(
        run_directory / EVENT_LOG_FILE_NAME,
        active_run_id,
        record.base_oid,
    )
    review_worktree_available: bool | None = None
    round_record: ReviewRoundRecord | None = None
    if validated_round is not None:
        review_worktree_available, round_record = (
            _validate_active_review_artifacts(
                run_directory=run_directory,
                record=record,
                run_id=active_run_id,
                current_round=current_round,
                current_head_oid=current_head_oid,
                active_round=validated_round,
            )
        )
    approval: ApprovalRecord | None = None
    if phase is RunPhase.APPROVED:
        if round_record is None:
            raise RunStateError(
                "an approved run must have a validated applied round"
            )
        approval = _validate_approval_artifacts(
            run_directory=run_directory,
            round_record=round_record,
            run_id=active_run_id,
            record=record,
            approved_head_oid=approved_head_oid,
        )
    unapplied_result, unapplied_result_error = _discover_unapplied_result(
        phase=phase,
        active_round=active_round,
        active_run_id=active_run_id,
        current_head_oid=current_head_oid,
        object_format=record.object_format,
        review_worktree_available=review_worktree_available,
    )
    next_action = _next_action(
        phase,
        handoff_status=handoff.status if handoff is not None else None,
        result_id=(
            unapplied_result.result_id
            if unapplied_result is not None
            else None
        ),
        result_error=unapplied_result_error,
    )
    return RepositoryStatus(
        repository_root=current_identity.implementation_root,
        repository_id=current_identity.repository_id,
        git_common_dir=current_identity.git_common_dir,
        worktree_git_dir=current_identity.worktree_git_dir,
        active_run=ActiveRunStatus(
            run_id=active_run_id,
            phase=phase,
            task_path=record.task_path,
            task_sha256=record.task_sha256,
            context_count=record.context_count,
            repository=stored_identity,
            base_ref=record.base_ref,
            base_oid=record.base_oid,
            git_object_format=record.object_format,
            implementer_agent=record.implementer.agent_name,
            implementer_kind=record.implementer.kind,
            reviewer_kind=record.reviewer.kind,
            reviewer_start_args=record.reviewer.start_args,
            current_round=current_round,
            current_head_oid=current_head_oid,
            approved_head_oid=approved_head_oid,
            active_escalation_id=active_escalation_id,
            active_round=active_round,
            handoff=handoff,
            review_worktree_available=review_worktree_available,
            unapplied_result=unapplied_result,
            unapplied_result_error=unapplied_result_error,
            approval=approval,
            review_budget=budget,
        ),
        next_action=next_action,
    )


def _start_run_locked(
    repository: InitializedRepository,
    *,
    task_path: Path,
    context_paths: Sequence[Path],
    implementer: _ImplementerRecord,
    reviewer: _ReviewerRecord,
    base_ref: str,
) -> StartRunResult:
    state_path = repository.control_root / STATE_FILE_NAME
    _ensure_state_path_is_safe(state_path)
    existing_state = _load_existing_state(state_path)
    if (
        existing_state is not None
        and existing_state["active_run_id"] is not None
    ):
        active_run_id = _require_uuid(
            existing_state["active_run_id"], "state.active_run_id"
        )
        phase = _require_phase(existing_state.get("phase"), "state.phase")
        raise RunStartError(
            f"run {active_run_id} is already active in phase {phase.value}; "
            "run agent-squad status before continuing"
        )

    identity = repository_identity(repository.worktree)
    object_format = _git_object_format(repository.worktree.root)
    resolved_base_oid = _resolve_commit(
        repository.worktree.root,
        base_ref,
        object_format,
    )
    task = _capture_input(
        task_path,
        repository.worktree.invocation_directory,
        TASK_FILE_NAME,
        label="task specification",
        require_text=True,
    )
    contexts = tuple(
        _capture_input(
            path,
            repository.worktree.invocation_directory,
            _context_run_path(index, path),
            label=f"context file {index}",
            require_text=False,
        )
        for index, path in enumerate(context_paths, start=1)
    )
    _reject_duplicate_context_sources(contexts)
    run_id = str(uuid.uuid4())
    timestamp = utc_timestamp()
    budget = ReviewBudget.initial(
        repository.configuration.max_completed_change_reviews
    )
    run_record = _new_run_record(
        run_id=run_id,
        timestamp=timestamp,
        task=task,
        contexts=contexts,
        identity=identity,
        base_ref=base_ref,
        base_oid=resolved_base_oid,
        object_format=object_format,
        implementer=implementer,
        reviewer=reviewer,
        budget=budget,
    )
    state = _new_state(
        run_id=run_id,
        timestamp=timestamp,
        identity=identity,
        base_oid=resolved_base_oid,
        budget=budget,
    )
    event = {
        "timestamp": timestamp,
        "event": "run_started",
        "run_id": run_id,
        "phase": RunPhase.IMPLEMENTING.value,
        "base_oid": resolved_base_oid,
    }

    return _persist_new_run(
        repository.control_root,
        run_id=run_id,
        task=task,
        contexts=contexts,
        run_record=run_record,
        state=state,
        event=event,
        identity=identity,
        base_ref=base_ref,
        base_oid=resolved_base_oid,
    )


def _persist_new_run(
    control_root: Path,
    *,
    run_id: str,
    task: CapturedInput,
    contexts: tuple[CapturedInput, ...],
    run_record: dict[str, object],
    state: dict[str, object],
    event: dict[str, object],
    identity: RepositoryIdentity,
    base_ref: str,
    base_oid: str,
) -> StartRunResult:
    runs_root = control_root / RUNS_DIRECTORY_NAME
    runs_root_created = False
    staging_directory: Path | None = None
    run_directory = runs_root / run_id
    run_directory_committed = False
    try:
        runs_root_created = _ensure_runs_root(runs_root)
        if _path_exists(run_directory):
            raise RunStartError(
                f"generated run ID already exists in local history: {run_id}"
            )
        staging_directory = Path(
            tempfile.mkdtemp(prefix=f".{run_id}.", dir=runs_root)
        )
        staging_directory.chmod(0o700)
        atomic_write(
            staging_directory / TASK_FILE_NAME,
            task.content,
            mode=0o400,
        )
        for context in contexts:
            destination = staging_directory / Path(context.run_path)
            destination.parent.mkdir(parents=True, mode=0o700)
            atomic_write(destination, context.content, mode=0o400)
        atomic_write(
            staging_directory / RUN_RECORD_FILE_NAME,
            encode_json(run_record),
            mode=0o600,
        )
        atomic_write(
            staging_directory / EVENT_LOG_FILE_NAME,
            encode_event(event),
            mode=0o600,
        )
        staging_directory.replace(run_directory)
        staging_directory = None
        run_directory_committed = True
        atomic_write(
            control_root / STATE_FILE_NAME,
            encode_json(state),
            mode=0o600,
        )
    except Exception as error:
        cleanup_errors = _rollback_new_run(
            staging_directory=staging_directory,
            run_directory=(run_directory if run_directory_committed else None),
            runs_root=(runs_root if runs_root_created else None),
        )
        cleanup_note = (
            " Rollback also encountered: " + "; ".join(cleanup_errors)
            if cleanup_errors
            else ""
        )
        if isinstance(error, RunError):
            if cleanup_note:
                raise RunStartError(f"{error}.{cleanup_note}") from error
            raise
        raise RunStartError(
            f"could not start the run atomically: {error}.{cleanup_note}"
        ) from error

    next_action = _next_action(RunPhase.IMPLEMENTING)
    return StartRunResult(
        run_id=run_id,
        phase=RunPhase.IMPLEMENTING,
        run_directory=run_directory,
        task_path=run_directory / TASK_FILE_NAME,
        state_path=control_root / STATE_FILE_NAME,
        repository_id=identity.repository_id,
        base_ref=base_ref,
        base_oid=base_oid,
        next_action=next_action,
    )


def _new_run_record(
    *,
    run_id: str,
    timestamp: str,
    task: CapturedInput,
    contexts: tuple[CapturedInput, ...],
    identity: RepositoryIdentity,
    base_ref: str,
    base_oid: str,
    object_format: str,
    implementer: _ImplementerRecord,
    reviewer: _ReviewerRecord,
    budget: ReviewBudget,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "phase": RunPhase.IMPLEMENTING.value,
        "started_at": timestamp,
        "finished_at": None,
        "task": task.to_record(),
        "context_files": [context.to_record() for context in contexts],
        "repository": {
            "implementation_root": str(identity.implementation_root),
            "git_common_dir": str(identity.git_common_dir),
            "worktree_git_dir": str(identity.worktree_git_dir),
            "repository_id": identity.repository_id,
            "start_branch_ref": identity.start_branch_ref,
            "start_head_detached": identity.start_head_detached,
        },
        "base_ref": base_ref,
        "base_oid": base_oid,
        "git_object_format": object_format,
        "implementer": {
            "agent_name": implementer.agent_name,
            "kind": implementer.kind.value,
        },
        "reviewer": {
            "kind": reviewer.kind.value,
            "start_args": list(reviewer.start_args),
        },
        "initial_review_budget": budget.to_dict(),
    }


def _new_state(
    *,
    run_id: str,
    timestamp: str,
    identity: RepositoryIdentity,
    base_oid: str,
    budget: ReviewBudget,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": timestamp,
        "active_run_id": run_id,
        "phase": RunPhase.IMPLEMENTING.value,
        "implementation_root": str(identity.implementation_root),
        "git_common_dir": str(identity.git_common_dir),
        "worktree_git_dir": str(identity.worktree_git_dir),
        "repository_id": identity.repository_id,
        "base_oid": base_oid,
        "current_round": 0,
        "current_head_oid": None,
        "approved_head_oid": None,
        "active_escalation_id": None,
        "active_round": None,
        "review_budget": budget.to_dict(),
        "handoff": None,
    }


def _validate_run_record(
    data: dict[str, object],
    run_directory: Path,
    expected_run_id: str,
) -> _ValidatedRunRecord:
    _check_fields(
        data,
        required={
            "schema_version",
            "run_id",
            "phase",
            "started_at",
            "finished_at",
            "task",
            "context_files",
            "repository",
            "base_ref",
            "base_oid",
            "git_object_format",
            "implementer",
            "reviewer",
            "initial_review_budget",
        },
        path="run record",
    )
    _validate_schema_version(data, "run record")
    run_id = _require_uuid(data["run_id"], "run record.run_id")
    if run_id != expected_run_id:
        raise RunStateError(
            "run record identity does not match its run directory"
        )
    phase = _require_phase(data["phase"], "run record.phase")
    _require_timestamp(data["started_at"], "run record.started_at")
    if data["finished_at"] is not None:
        _require_timestamp(data["finished_at"], "run record.finished_at")
    if (phase in TERMINAL_PHASES) != (data["finished_at"] is not None):
        raise RunStateError(
            "run record.finished_at must be set exactly when the phase is "
            "terminal"
        )

    task_record = _validate_captured_record(
        data["task"],
        "run record.task",
        expected_path=TASK_FILE_NAME,
    )
    task_path = _captured_path(
        run_directory,
        task_record.run_path,
        "run record.task",
    )
    task_sha256 = _verify_captured_digest(
        task_path,
        task_record.sha256,
        "captured task",
    )
    context_value = data["context_files"]
    if not isinstance(context_value, list):
        raise RunStateError("run record.context_files must be a JSON array")
    seen_context_paths: set[str] = set()
    context_records: list[_CapturedRecord] = []
    for index, value in enumerate(context_value):
        label = f"run record.context_files[{index}]"
        context_record = _validate_captured_record(value, label)
        path_text = context_record.run_path
        if path_text in seen_context_paths:
            raise RunStateError(
                f"run record has duplicate captured context path: {path_text}"
            )
        seen_context_paths.add(path_text)
        context_path = _captured_path(
            run_directory,
            context_record.run_path,
            label,
        )
        _verify_captured_digest(
            context_path,
            context_record.sha256,
            f"captured context {path_text}",
        )
        context_records.append(context_record)

    identity = _validate_repository_record(data["repository"])
    base_ref = _require_string(data["base_ref"], "run record.base_ref")
    object_format = _require_object_format(
        data["git_object_format"],
        "run record.git_object_format",
    )
    base_oid = _require_oid(
        data["base_oid"], object_format, "run record.base_oid"
    )
    implementer = _validate_implementer(data["implementer"])
    reviewer = _validate_reviewer(data["reviewer"])
    initial_budget = ReviewBudget.from_dict(
        data["initial_review_budget"],
        path="run record.initial_review_budget",
    )
    if initial_budget.additional_rounds_granted != 0 or (
        initial_budget.completed_change_reviews != 0
    ):
        raise RunStateError(
            "run record.initial_review_budget must describe an unused initial "
            "budget"
        )
    return _ValidatedRunRecord(
        phase=phase,
        task_path=task_path,
        task_sha256=task_sha256,
        context_records=tuple(context_records),
        repository=identity,
        base_ref=base_ref,
        base_oid=base_oid,
        object_format=object_format,
        implementer=implementer,
        reviewer=reviewer,
        initial_budget=initial_budget,
    )


def _validate_repository_record(value: object) -> RepositoryIdentity:
    data = _require_object(value, "run record.repository")
    _check_fields(
        data,
        required={
            "implementation_root",
            "git_common_dir",
            "worktree_git_dir",
            "repository_id",
            "start_branch_ref",
            "start_head_detached",
        },
        path="run record.repository",
    )
    implementation_root = _require_absolute_path(
        data["implementation_root"],
        "run record.repository.implementation_root",
    )
    git_common_dir = _require_absolute_path(
        data["git_common_dir"], "run record.repository.git_common_dir"
    )
    worktree_git_dir = _require_absolute_path(
        data["worktree_git_dir"],
        "run record.repository.worktree_git_dir",
    )
    repository_id = _require_digest(
        data["repository_id"], "run record.repository.repository_id"
    )
    branch_ref = _require_optional_string(
        data["start_branch_ref"],
        "run record.repository.start_branch_ref",
    )
    detached = data["start_head_detached"]
    if type(detached) is not bool:
        raise RunStateError(
            "run record.repository.start_head_detached must be a boolean"
        )
    if detached == (branch_ref is not None):
        raise RunStateError(
            "run record repository branch and detached fields are inconsistent"
        )
    return RepositoryIdentity(
        implementation_root=implementation_root,
        git_common_dir=git_common_dir,
        worktree_git_dir=worktree_git_dir,
        repository_id=repository_id,
        start_branch_ref=branch_ref,
        start_head_detached=detached,
    )


def _validate_implementer(value: object) -> _ImplementerRecord:
    data = _require_object(value, "run record.implementer")
    _check_fields(
        data,
        required={"agent_name", "kind"},
        path="run record.implementer",
    )
    return _ImplementerRecord(
        agent_name=_require_string(
            data["agent_name"],
            "run record.implementer.agent_name",
        ),
        kind=_VALIDATOR.require_enum(
            data["kind"],
            "run record.implementer.kind",
            AgentKind,
        ),
    )


def _validate_reviewer(value: object) -> _ReviewerRecord:
    data = _require_object(value, "run record.reviewer")
    _check_fields(
        data,
        required={"kind", "start_args"},
        path="run record.reviewer",
    )
    arguments = data["start_args"]
    if not isinstance(arguments, list) or not all(
        isinstance(argument, str) and argument for argument in arguments
    ):
        raise RunStateError(
            "run record.reviewer.start_args must be a JSON array of "
            "non-empty strings"
        )
    return _ReviewerRecord(
        kind=_VALIDATOR.require_enum(
            data["kind"],
            "run record.reviewer.kind",
            AgentKind,
        ),
        start_args=tuple(arguments),
    )


def _validate_captured_record(
    value: object,
    path: str,
    *,
    expected_path: str | None = None,
) -> _CapturedRecord:
    data = _require_object(value, path)
    _check_fields(
        data,
        required={"source_path", "path", "sha256"},
        path=path,
    )
    source_path = _require_absolute_path(
        data["source_path"],
        f"{path}.source_path",
    )
    run_path = _require_string(data["path"], f"{path}.path")
    digest = _require_digest(data["sha256"], f"{path}.sha256")
    if expected_path is not None and run_path != expected_path:
        raise RunStateError(f"{path}.path must be {expected_path}")
    return _CapturedRecord(
        source_path=source_path,
        run_path=run_path,
        sha256=digest,
    )


def _captured_path(run_directory: Path, run_path: str, label: str) -> Path:
    relative = PurePosixPath(run_path)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.name in {"", "."}
    ):
        raise RunStateError(f"{label}.path must stay inside the run directory")
    path = run_directory.joinpath(*relative.parts)
    candidate = run_directory
    for component in relative.parts:
        candidate /= component
        if candidate.is_symlink():
            raise RunStateError(
                f"{label}.path must not contain symbolic links"
            )
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(run_directory)
    except (OSError, RuntimeError, ValueError) as error:
        raise RunStateError(
            f"{label}.path does not resolve to a safe captured file: {error}"
        ) from error
    if path.is_symlink() or not resolved.is_file():
        raise RunStateError(f"{label}.path must be a regular non-symlink file")
    return resolved


def _verify_captured_digest(path: Path, expected: str, label: str) -> str:
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise RunStateError(f"cannot read {label} {path}: {error}") from error
    if actual != expected:
        raise RunStateError(f"{label} digest does not match run metadata")
    return actual


def _validate_event_log(
    path: Path,
    expected_run_id: str,
    expected_base_oid: str,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise RunStateError(
            f"active run event log must be a regular non-symlink file: {path}"
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise RunStateError(
            f"cannot read active run event log {path}: {error}"
        ) from error
    if not lines:
        raise RunStateError("active run event log must contain run_started")
    events: list[dict[str, object]] = []
    for index, line in enumerate(lines, start=1):
        try:
            value = decode_json(line)
        except InvalidJsonError as error:
            raise RunStateError(
                f"active run event log line {index} contains invalid JSON: "
                f"{error}"
            ) from error
        event = _require_object(value, f"event log line {index}")
        _require_timestamp(
            event.get("timestamp"),
            f"event log line {index}.timestamp",
        )
        _require_string(event.get("event"), f"event log line {index}.event")
        events.append(event)
    event = events[0]
    _check_fields(
        event,
        required={"timestamp", "event", "run_id", "phase", "base_oid"},
        path="run_started event",
    )
    if event.get("event") != "run_started":
        raise RunStateError("active run event log must begin with run_started")
    if (
        _require_uuid(event.get("run_id"), "run_started.run_id")
        != expected_run_id
    ):
        raise RunStateError("run_started event identifies a different run")
    if event.get("phase") != RunPhase.IMPLEMENTING.value:
        raise RunStateError("run_started event must record phase implementing")
    if event.get("base_oid") != expected_base_oid:
        raise RunStateError(
            "run_started event base OID does not match run metadata"
        )


def _validate_active_review_artifacts(
    *,
    run_directory: Path,
    record: _ValidatedRunRecord,
    run_id: str,
    current_round: int,
    current_head_oid: str | None,
    active_round: ActiveRoundRecord,
    validate_live_worktree: bool = True,
) -> tuple[bool, ReviewRoundRecord]:
    """Validate round artifacts and report review-worktree availability."""

    round_directory = (
        run_directory / "rounds" / f"{current_round:03d}"
    )
    if round_directory.is_symlink() or not round_directory.is_dir():
        raise RunStateError(
            "active round directory must be a non-symlink directory: "
            f"{round_directory}"
        )
    try:
        round_record = ReviewRoundRecord.from_dict(
            load_json_object(
                round_directory / "round.json",
                "active round record",
            ),
            label="active round record",
        )
    except ArtifactValidationError as error:
        raise RunStateError(str(error)) from error
    comparisons = (
        (round_record.run_id, run_id, "run ID"),
        (round_record.round_number, current_round, "round number"),
        (round_record.mode, active_round.mode, "submission mode"),
        (round_record.request_id, active_round.request_id, "request ID"),
        (round_record.result_id, active_round.result_id, "result ID"),
        (round_record.status, active_round.status, "round status"),
        (
            round_record.object_format,
            record.object_format,
            "Git object format",
        ),
        (round_record.base_oid, record.base_oid, "base OID"),
        (round_record.head_oid, current_head_oid, "head OID"),
        (
            round_record.review_worktree,
            active_round.review_worktree,
            "review worktree",
        ),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise RunStateError(
                f"active round record {label} does not match state or run "
                "metadata"
            )
    if (
        active_round.status is RoundStatus.REVIEWING
        and round_record.result_id is not None
    ):
        raise RunStateError(
            "a reviewing active round record must have no result ID"
        )

    if round_record.reviewer_name != active_round.reviewer_name:
        raise RunStateError(
            "active round Reviewer name does not match state"
        )
    if round_record.reviewer_kind is not record.reviewer.kind:
        raise RunStateError(
            "active round Reviewer kind does not match run metadata"
        )
    if round_record.reviewer_start_args != record.reviewer.start_args:
        raise RunStateError(
            "active round Reviewer arguments do not match run metadata"
        )
    request_artifact = round_record.request_artifact
    report_artifact = round_record.implementation_report
    request_path = _captured_path(
        round_directory,
        request_artifact.path,
        "active round request artifact",
    )
    report_path = _captured_path(
        round_directory,
        report_artifact.path,
        "active round report artifact",
    )
    _verify_captured_digest(
        request_path,
        request_artifact.sha256,
        "active round request",
    )
    _verify_captured_digest(
        report_path,
        report_artifact.sha256,
        "active round implementation report",
    )
    request_data = load_json_object(request_path, "active review request")
    try:
        request = ReviewRequest.from_dict(request_data)
    except ArtifactValidationError as error:
        raise RunStateError(
            f"active review request is invalid: {error}"
        ) from error
    _assert_request_matches_active_round(
        request=request,
        record=record,
        run_id=run_id,
        current_round=current_round,
        current_head_oid=current_head_oid,
        active_round=active_round,
        report_artifact=report_artifact,
    )

    request_bundle_artifact = BundleArtifact(
        path="input/request.json",
        sha256=request_artifact.sha256,
    )
    expected_bundle_inputs = (
        request_bundle_artifact,
        request.task,
        request.implementation_report,
        *request.context_files,
    )
    bundle_inputs = round_record.bundle_inputs
    if bundle_inputs != expected_bundle_inputs:
        raise RunStateError(
            "active round bundle-input manifest does not match the request"
        )

    review_worktree = active_round.review_worktree
    if not validate_live_worktree:
        return os.path.lexists(review_worktree), round_record
    if not os.path.lexists(review_worktree):
        return False, round_record
    if review_worktree.is_symlink() or not review_worktree.is_dir():
        raise RunStateError(
            "active review worktree is not a normal directory: "
            f"{review_worktree}"
        )
    bundle_root = review_worktree / ".agent-squad-review"
    for artifact in bundle_inputs:
        path = bundle_root.joinpath(*PurePosixPath(artifact.path).parts)
        if path.is_symlink() or not path.is_file():
            raise RunStateError(
                "active review bundle input must be a regular non-symlink "
                f"file: {path}"
            )
        _verify_captured_digest(
            path,
            artifact.sha256,
            f"active review bundle input {artifact.path}",
        )
    return True, round_record


def _validate_approval_artifacts(
    *,
    run_directory: Path,
    round_record: ReviewRoundRecord,
    run_id: str,
    record: _ValidatedRunRecord,
    approved_head_oid: str | None,
) -> ApprovalRecord:
    """Validate archived evidence and exact authority for an approved run."""

    if round_record.status is not RoundStatus.APPLIED:
        raise RunStateError("an approved run must reference an applied round")
    if round_record.verdict is not ReviewVerdict.APPROVED:
        raise RunStateError(
            "an approved run must reference an approved review verdict"
        )
    if round_record.result_id is None:
        raise RunStateError("an approved round must record its result ID")
    if approved_head_oid != round_record.head_oid:
        raise RunStateError(
            "approved head does not match the applied review round"
        )
    required_artifacts = {
        "review result": (round_record.review_result, "review.json"),
        "review Markdown": (round_record.review_markdown, "review.md"),
        "review marker": (round_record.review_marker, "review-marker.json"),
        "approval": (round_record.approval, "approval.json"),
    }
    resolved: dict[str, tuple[BundleArtifact, Path]] = {}
    round_directory = (
        run_directory / "rounds" / f"{round_record.round_number:03d}"
    )
    for label, (artifact, expected_path) in required_artifacts.items():
        if artifact is None:
            raise RunStateError(f"approved round is missing its {label}")
        if artifact.path != expected_path:
            raise RunStateError(
                f"approved round {label} path must be {expected_path}"
            )
        path = _captured_path(
            round_directory,
            artifact.path,
            f"approved round {label}",
        )
        _verify_captured_digest(
            path,
            artifact.sha256,
            f"approved round {label}",
        )
        resolved[label] = artifact, path

    archived: dict[str, BundleArtifact] = {}
    for artifact in round_record.bundle_archive:
        if not artifact.path.startswith("bundle/"):
            raise RunStateError(
                "approved bundle manifest paths must start with bundle/"
            )
        path = _captured_path(
            round_directory,
            artifact.path,
            "approved bundle artifact",
        )
        _verify_captured_digest(
            path,
            artifact.sha256,
            f"approved bundle artifact {artifact.path}",
        )
        archived[artifact.path] = artifact
    _validate_archive_tree(
        round_directory / "bundle",
        expected_paths=set(archived),
    )

    core_archive = {
        "bundle/input/request.json": round_record.request_artifact.sha256,
        "bundle/output/review.json": resolved["review result"][0].sha256,
        "bundle/output/review.md": resolved["review Markdown"][0].sha256,
        "bundle/local-state.json": resolved["review marker"][0].sha256,
    }
    for path_text, digest in core_archive.items():
        artifact = archived.get(path_text)
        if artifact is None or artifact.sha256 != digest:
            raise RunStateError(
                f"approved bundle archive does not preserve {path_text}"
            )

    approval_path = resolved["approval"][1]
    try:
        approval = ApprovalRecord.from_dict(
            load_json_object(approval_path, "approval record")
        )
    except ArtifactValidationError as error:
        raise RunStateError(str(error)) from error
    review_artifact = resolved["review result"][0]
    comparisons = (
        (approval.run_id, run_id, "run ID"),
        (
            approval.round_number,
            round_record.round_number,
            "round number",
        ),
        (approval.request_id, round_record.request_id, "request ID"),
        (approval.result_id, round_record.result_id, "result ID"),
        (approval.task_sha256, record.task_sha256, "task digest"),
        (
            approval.object_format,
            round_record.object_format,
            "Git object format",
        ),
        (approval.base_oid, record.base_oid, "base OID"),
        (approval.head_oid, round_record.head_oid, "head OID"),
        (
            approval.reviewer_name,
            round_record.reviewer_name,
            "Reviewer name",
        ),
        (
            approval.reviewer_kind,
            round_record.reviewer_kind,
            "Reviewer kind",
        ),
        (approval.review_sha256, review_artifact.sha256, "review digest"),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise RunStateError(
                f"approval record {label} does not match authoritative "
                "round data"
            )

    archived_request = load_json_object(
        _captured_path(
            round_directory,
            "bundle/input/request.json",
            "archived review request",
        ),
        "archived review request",
    )
    archived_review = load_json_object(
        _captured_path(
            round_directory,
            "bundle/output/review.json",
            "archived review result",
        ),
        "archived review result",
    )
    archived_marker = load_json_object(
        _captured_path(
            round_directory,
            "bundle/local-state.json",
            "archived review marker",
        ),
        "archived review marker",
    )
    try:
        request = ReviewRequest.from_dict(archived_request)
        review = ReviewResult.from_dict(
            archived_review,
            object_format=record.object_format,
        )
        marker = ReviewerLocalMarker.from_dict(archived_marker)
    except ArtifactValidationError as error:
        raise RunStateError(
            f"approved bundle contains invalid protocol evidence: {error}"
        ) from error
    evidence_comparisons = (
        (request.request_id, approval.request_id, "request ID"),
        (request.run_id, approval.run_id, "run ID"),
        (request.round_number, approval.round_number, "round number"),
        (request.base_oid, approval.base_oid, "base OID"),
        (request.head_oid, approval.head_oid, "head OID"),
        (request.task.sha256, approval.task_sha256, "task digest"),
        (
            request.reviewer_name,
            approval.reviewer_name,
            "Reviewer name",
        ),
        (
            request.reviewer_kind,
            approval.reviewer_kind,
            "Reviewer kind",
        ),
        (review.result_id, approval.result_id, "result ID"),
        (review.request_id, approval.request_id, "result request ID"),
        (review.run_id, approval.run_id, "result run ID"),
        (review.round_number, approval.round_number, "result round"),
        (review.base_oid, approval.base_oid, "result base OID"),
        (review.head_oid, approval.head_oid, "result head OID"),
        (review.verdict, ReviewVerdict.APPROVED, "verdict"),
        (marker.request_id, approval.request_id, "marker request ID"),
        (marker.result_id, approval.result_id, "marker result ID"),
        (
            marker.review_sha256,
            approval.review_sha256,
            "marker review digest",
        ),
    )
    for actual, expected, label in evidence_comparisons:
        if actual != expected:
            raise RunStateError(
                f"approved bundle {label} does not match approval authority"
            )
    return approval


def _validate_archive_tree(
    root: Path,
    *,
    expected_paths: set[str],
) -> None:
    """Reject missing, extra, unusual, or case-colliding archive entries."""

    files = read_regular_tree(
        root,
        label="approved bundle archive",
        error_type=RunStateError,
    ).files
    actual_paths = {
        (PurePosixPath("bundle") / path).as_posix() for path in files
    }
    if actual_paths != expected_paths:
        raise RunStateError(
            "approved bundle archive does not match its authoritative manifest"
        )


def _discover_unapplied_result(
    *,
    phase: RunPhase,
    active_round: ActiveRoundRecord | None,
    active_run_id: str,
    current_head_oid: str | None,
    object_format: str,
    review_worktree_available: bool | None,
) -> tuple[UnappliedReviewResult | None, str | None]:
    """Probe the expected active bundle for a valid local result marker."""

    if (
        phase is not RunPhase.REVIEWING
        or active_round is None
        or not review_worktree_available
    ):
        return None, None
    marker_path = active_round.review_worktree / (
        ".agent-squad-review/local-state.json"
    )
    if not os.path.lexists(marker_path):
        return None, None
    try:
        evidence = load_marker_confirmed_review(
            active_round.review_worktree
        )
    except AgentSquadError as error:
        return (
            None,
            f"marker-confirmed review result is invalid: {error}",
        )
    comparisons = (
        (evidence.request.run_id, active_run_id, "run ID"),
        (
            evidence.request.round_number,
            active_round.round_number,
            "round number",
        ),
        (
            evidence.request.request_id,
            active_round.request_id,
            "request ID",
        ),
        (evidence.request.head_oid, current_head_oid, "head OID"),
        (
            evidence.request.object_format,
            object_format,
            "Git object format",
        ),
        (
            evidence.request.reviewer_name,
            active_round.reviewer_name,
            "Reviewer name",
        ),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            return (
                None,
                f"marker-confirmed review {label} does not match the active "
                "round",
            )
    return (
        UnappliedReviewResult(
            result_id=evidence.review.result_id,
            verdict=evidence.review.verdict,
            result_path=evidence.review_path,
        ),
        None,
    )


def _assert_request_matches_active_round(
    *,
    request: ReviewRequest,
    record: _ValidatedRunRecord,
    run_id: str,
    current_round: int,
    current_head_oid: str | None,
    active_round: ActiveRoundRecord,
    report_artifact: BundleArtifact,
) -> None:
    expected_context = tuple(
        BundleArtifact(
            path=f"input/{context.run_path}",
            sha256=context.sha256,
        )
        for context in record.context_records
    )
    comparisons = (
        (request.run_id, run_id, "run ID"),
        (request.round_number, current_round, "round number"),
        (request.mode, active_round.mode, "submission mode"),
        (request.request_id, active_round.request_id, "request ID"),
        (request.object_format, record.object_format, "Git object format"),
        (request.base_oid, record.base_oid, "base OID"),
        (request.head_oid, current_head_oid, "head OID"),
        (request.task.sha256, record.task_sha256, "task digest"),
        (
            request.implementation_report.sha256,
            report_artifact.sha256,
            "implementation-report digest",
        ),
        (request.context_files, expected_context, "context manifest"),
        (
            request.implementer_agent,
            record.implementer.agent_name,
            "Implementer identity",
        ),
        (
            request.implementer_kind,
            record.implementer.kind,
            "Implementer kind",
        ),
        (request.reviewer_kind, record.reviewer.kind, "Reviewer kind"),
        (
            request.reviewer_name,
            active_round.reviewer_name,
            "Reviewer name",
        ),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise RunStateError(
                f"active review request {label} does not match state or run "
                "metadata"
            )
    if request.previous_review_path is not None:
        raise RunStateError(
            "the first active review request cannot reference a previous "
            "review"
        )
    if request.previous_response_path is not None:
        raise RunStateError(
            "the first active review request cannot reference a previous "
            "response"
        )
    if request.resolution_paths:
        raise RunStateError(
            "this first-round implementation does not support Developer "
            "resolution inputs"
        )


def _validate_active_state_shape(
    *,
    phase: RunPhase,
    current_round: int,
    current_head_oid: str | None,
    approved_head_oid: str | None,
    active_escalation_id: str | None,
    active_round: ActiveRoundRecord | None,
    handoff: HandoffRecord | None,
    object_format: str,
) -> ActiveRoundRecord | None:
    """Validate state relationships and return a reviewing round."""

    if current_head_oid is not None:
        _require_oid(current_head_oid, object_format, "state.current_head_oid")
    if approved_head_oid is not None:
        _require_oid(
            approved_head_oid,
            object_format,
            "state.approved_head_oid",
        )
    if phase is RunPhase.IMPLEMENTING:
        if approved_head_oid is not None or active_escalation_id is not None:
            raise RunStateError(
                "an implementing run cannot retain approval or active "
                "escalation"
            )
        if current_round == 0:
            if current_head_oid is not None:
                raise RunStateError(
                    "an unused implementing run must have no current round "
                    "or requested head"
                )
            if active_round is not None or handoff is not None:
                raise RunStateError(
                    "an unused implementing run must have no active round or "
                    "handoff"
                )
            return None
        if current_head_oid is None:
            raise RunStateError(
                "an implementing run with round history must identify a "
                "current head"
            )
        # Section 26.11 returns to implementing after closing a round, while
        # Section 41.3 retains its request handoff for status and recovery.
        closed_round = _require_linked_active_round(
            current_round=current_round,
            active_round=active_round,
            handoff=handoff,
            run_description="an implementing run with round history",
        )
        if closed_round.status not in CLOSED_ROUND_STATUSES:
            raise RunStateError(
                "an implementing run must record a closed active round"
            )
        return None
    if phase is RunPhase.APPROVED:
        if current_round < 1 or current_head_oid is None:
            raise RunStateError(
                "an approved run must identify its current round and head"
            )
        if approved_head_oid != current_head_oid:
            raise RunStateError(
                "an approved run must bind its current and approved heads"
            )
        if active_escalation_id is not None:
            raise RunStateError(
                "an approved run cannot retain an active escalation"
            )
        approved_round = _require_linked_active_round(
            current_round=current_round,
            active_round=active_round,
            handoff=handoff,
            run_description="an approved run",
        )
        if approved_round.status is not RoundStatus.APPLIED:
            raise RunStateError(
                "an approved run must reference an applied round"
            )
        if approved_round.result_id is None:
            raise RunStateError(
                "an approved run must record the applied result ID"
            )
        return approved_round
    if phase is not RunPhase.REVIEWING:
        return None
    if current_round < 1 or current_head_oid is None:
        raise RunStateError(
            "a reviewing run must identify a current round and head"
        )
    if approved_head_oid is not None or active_escalation_id is not None:
        raise RunStateError(
            "a reviewing run cannot retain approval or active escalation"
        )
    reviewing_round = _require_linked_active_round(
        current_round=current_round,
        active_round=active_round,
        handoff=handoff,
        run_description="a reviewing run",
    )
    if reviewing_round.status is not RoundStatus.REVIEWING:
        raise RunStateError(
            "the active round status must be reviewing while the run is "
            "reviewing"
        )
    if reviewing_round.result_id is not None:
        raise RunStateError(
            "a reviewing round cannot have an authoritative result ID"
        )
    return reviewing_round


def _require_linked_active_round(
    *,
    current_round: int,
    active_round: ActiveRoundRecord | None,
    handoff: HandoffRecord | None,
    run_description: str,
) -> ActiveRoundRecord:
    """Return records whose round and Reviewer identities agree."""

    if active_round is None:
        raise RunStateError(
            f"{run_description} must record an active round"
        )
    if active_round.round_number != current_round:
        raise RunStateError(
            "state.active_round.round must match state.current_round"
        )
    if handoff is None:
        raise RunStateError(f"{run_description} must record a handoff")
    if handoff.round_number != current_round:
        raise RunStateError(
            "state.handoff.round must match state.current_round"
        )
    if handoff.target != active_round.reviewer_name:
        raise RunStateError(
            "state.handoff.target must match the active Reviewer name"
        )
    return active_round


def repository_identity(worktree: GitWorktree) -> RepositoryIdentity:
    """Return the current canonical Git and branch identity."""

    branch_result = run_git(worktree.root, "symbolic-ref", "--quiet", "HEAD")
    if branch_result.returncode == 0:
        branch_ref = branch_result.stdout.rstrip("\r\n")
        if not branch_ref:
            raise RunStateError(
                "Git returned an empty symbolic HEAD reference"
            )
        detached = False
    elif branch_result.returncode == 1:
        branch_ref = None
        detached = True
    else:
        detail = branch_result.stderr.strip() or "unknown Git error"
        raise RunStateError(
            f"could not determine branch or detached state: {detail}"
        )
    repository_id = hashlib.sha256(
        os.fsencode(str(worktree.common_directory))
    ).hexdigest()
    return RepositoryIdentity(
        implementation_root=worktree.root,
        git_common_dir=worktree.common_directory,
        worktree_git_dir=worktree.git_directory,
        repository_id=repository_id,
        start_branch_ref=branch_ref,
        start_head_detached=detached,
    )


def _git_object_format(repository_root: Path) -> str:
    result = run_git(repository_root, "rev-parse", "--show-object-format")
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown Git error"
        raise RunStartError(f"could not determine Git object format: {detail}")
    return _require_object_format(
        result.stdout.rstrip("\r\n"),
        "Git object format",
    )


def _resolve_commit(
    repository_root: Path,
    reference: str,
    object_format: str,
) -> str:
    result = run_git(
        repository_root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{reference}^{{commit}}",
    )
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or "reference does not identify a commit"
        )
        raise RunStartError(
            f"cannot resolve base reference {reference!r} to a commit: "
            f"{detail}"
        )
    return _require_oid(
        result.stdout.rstrip("\r\n"),
        object_format,
        "resolved base",
    )


def _capture_input(
    path: Path,
    invocation_directory: Path,
    run_path: str,
    *,
    label: str,
    require_text: bool,
) -> CapturedInput:
    candidate = path if path.is_absolute() else invocation_directory / path
    try:
        resolved = candidate.resolve(strict=True)
        not_regular_message = f"{label} must be a regular file: {resolved}"
        if not resolved.is_file():
            raise RunStartError(not_regular_message)
        with resolved.open("rb") as input_file:
            if not stat.S_ISREG(os.fstat(input_file.fileno()).st_mode):
                raise RunStartError(not_regular_message)
            content = input_file.read()
    except RunStartError:
        raise
    except (OSError, RuntimeError) as error:
        raise RunStartError(
            f"cannot read {label} {candidate}: {error}"
        ) from error
    if require_text:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RunStartError(f"{label} must contain UTF-8 text") from error
        if not text.strip():
            raise RunStartError(f"{label} must not be empty")
    return CapturedInput(
        source_path=resolved,
        run_path=run_path,
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _context_run_path(index: int, source: Path) -> str:
    return str(
        PurePosixPath(CONTEXT_DIRECTORY_NAME, f"{index:03d}", source.name)
    )


def _reject_duplicate_context_sources(
    contexts: tuple[CapturedInput, ...],
) -> None:
    seen: set[Path] = set()
    for context in contexts:
        if context.source_path in seen:
            raise RunStartError(
                "context file was provided more than once: "
                f"{context.source_path}"
            )
        seen.add(context.source_path)


def _load_existing_state(path: Path) -> dict[str, object] | None:
    if not _path_exists(path):
        return None
    data = load_json_object(path, "authoritative state")
    _validate_schema_version(data, "state")
    if "active_run_id" not in data:
        raise RunStateError("state is missing required field: active_run_id")
    if data["active_run_id"] is not None:
        _require_uuid(data["active_run_id"], "state.active_run_id")
    return data


def _ensure_state_path_is_safe(path: Path) -> None:
    if path.is_symlink():
        raise RunStateError(
            f"{path} is a symbolic link; refusing to read or replace it"
        )
    if path.exists() and not path.is_file():
        raise RunStateError(
            f"authoritative state must be a regular file: {path}"
        )


def _ensure_runs_root(path: Path) -> bool:
    if path.is_symlink():
        raise RunStateError(
            f"{path} is a symbolic link; the run history must be local storage"
        )
    if path.exists():
        if not path.is_dir():
            raise RunStateError(
                f"run history path must be a directory: {path}"
            )
        return False
    path.mkdir(mode=0o700)
    return True


def safe_run_directory(control_root: Path, run_id: str) -> Path:
    """Resolve one owned run directory without following unsafe paths."""

    runs_root = control_root / RUNS_DIRECTORY_NAME
    if runs_root.is_symlink() or not runs_root.is_dir():
        raise RunStateError(
            "run history path must be a non-symlink directory: "
            f"{runs_root}"
        )
    run_directory = runs_root / run_id
    if run_directory.is_symlink() or not run_directory.is_dir():
        raise RunStateError(
            "active run directory must be a non-symlink directory: "
            f"{run_directory}"
        )
    return run_directory.resolve(strict=True)


def _rollback_new_run(
    *,
    staging_directory: Path | None,
    run_directory: Path | None,
    runs_root: Path | None,
) -> list[str]:
    errors: list[str] = []
    for path in (staging_directory, run_directory):
        if path is None:
            continue
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            errors.append(f"could not remove newly created {path}: {error}")
    if runs_root is not None:
        try:
            runs_root.rmdir()
        except FileNotFoundError:
            pass
        except OSError as error:
            errors.append(
                f"could not remove newly created {runs_root}: {error}"
            )
    return errors


def _idle_status(identity: RepositoryIdentity) -> RepositoryStatus:
    next_action = "agent-squad start --task <task.md>"
    return RepositoryStatus(
        repository_root=identity.implementation_root,
        repository_id=identity.repository_id,
        git_common_dir=identity.git_common_dir,
        worktree_git_dir=identity.worktree_git_dir,
        active_run=None,
        next_action=next_action,
    )


def _next_action(
    phase: RunPhase,
    *,
    handoff_status: HandoffStatus | None = None,
    result_id: str | None = None,
    result_error: str | None = None,
) -> str:
    if phase is RunPhase.IMPLEMENTING:
        return "continue implementing the captured task"
    if phase is RunPhase.REVIEWING:
        if result_error is not None:
            return (
                "inspect the review worktree; its marker-confirmed result "
                "did not revalidate"
            )
        if result_id is not None:
            return f"agent-squad apply-review --result-id {result_id}"
        if handoff_status is HandoffStatus.FAILED:
            return "recover the preserved review-request handoff"
        if handoff_status is HandoffStatus.PENDING:
            return "finish or recover the pending review-request handoff"
        return "wait for the Reviewer result"
    if phase is RunPhase.APPROVED:
        return "agent-squad complete"
    raise RunStateError(
        f"phase {phase.value} is not supported by this implementation "
        "increment"
    )


def _assert_current_identity(
    stored: RepositoryIdentity,
    current: RepositoryIdentity,
) -> None:
    comparisons = (
        (
            stored.implementation_root,
            current.implementation_root,
            "implementation root",
        ),
        (
            stored.git_common_dir,
            current.git_common_dir,
            "Git common directory",
        ),
        (
            stored.worktree_git_dir,
            current.worktree_git_dir,
            "worktree Git directory",
        ),
        (stored.repository_id, current.repository_id, "repository ID"),
    )
    for expected, actual, label in comparisons:
        if expected != actual:
            raise RunStateError(
                f"active run {label} does not match the current worktree: "
                f"recorded {expected}, current {actual}"
            )


def _assert_matching_state_value(
    value: object,
    expected: Path | str,
    *,
    field: str,
    label: str,
) -> None:
    if isinstance(expected, Path):
        actual: Path | str = _require_absolute_path(value, f"state.{field}")
    else:
        actual = _require_string(value, f"state.{field}")
    if actual != expected:
        raise RunStateError(f"state {label} does not match run metadata")


def load_json_object(path: Path, label: str) -> dict[str, object]:
    """Load one authoritative run artifact as a validated JSON object."""

    if path.is_symlink() or not path.is_file():
        raise RunStateError(
            f"{label} must be a regular non-symlink file: {path}"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RunStateError(f"cannot read {label} {path}: {error}") from error
    try:
        value = decode_json(raw)
    except InvalidJsonError as error:
        raise RunStateError(
            f"{label} contains invalid JSON: {error}"
        ) from error
    return _require_object(value, label)


def _validate_schema_version(data: dict[str, object], path: str) -> None:
    version = _require_int(
        data.get("schema_version"),
        f"{path}.schema_version",
    )
    if version != SCHEMA_VERSION:
        raise RunStateError(f"{path}.schema_version must be {SCHEMA_VERSION}")


def _require_nonnegative_int(value: object, path: str) -> int:
    result = _require_int(value, path)
    if result < 0:
        raise RunStateError(f"{path} must not be negative")
    return result


def _require_phase(value: object, path: str) -> RunPhase:
    return _VALIDATOR.require_enum(value, path, RunPhase)


def _validate_selection(value: str, label: str) -> str:
    if not value or value != value.strip() or "\x00" in value:
        raise RunStartError(
            f"{label} must be non-empty and contain no surrounding whitespace "
            "or null bytes"
        )
    return value


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)
