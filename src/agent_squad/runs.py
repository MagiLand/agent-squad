"""Creation and inspection of one authoritative local Agent Squad run."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import uuid

from .initialization import (
    AgentKind,
    AgentSquadError,
    GitWorktree,
    InitializedRepository,
    SCHEMA_VERSION,
    load_initialized_repository,
    run_git,
)
from .storage import (
    InvalidJsonError,
    atomic_write,
    decode_json,
    exclusive_file_lock,
)


STATE_FILE_NAME = "state.json"
LOCK_FILE_NAME = "lock"
RUNS_DIRECTORY_NAME = "runs"
RUN_RECORD_FILE_NAME = "run.json"
TASK_FILE_NAME = "task.md"
EVENT_LOG_FILE_NAME = "events.jsonl"
CONTEXT_DIRECTORY_NAME = "context"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
UTC_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
)
OID_LENGTHS = {"sha1": 40, "sha256": 64}


class RunError(AgentSquadError):
    """Raised when run state cannot be created or inspected safely."""


class RunStartError(RunError):
    """Raised when a requested run cannot be started atomically."""


class RunStateError(RunError):
    """Raised when authoritative run artifacts are invalid or inconsistent."""


class RunPhase(StrEnum):
    """Authoritative phases for an Agent Squad run."""

    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    NEEDS_HUMAN = "needs_human"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


TERMINAL_PHASES = {RunPhase.COMPLETED, RunPhase.CANCELLED}


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
    context_count: int
    repository: RepositoryIdentity
    base_ref: str
    base_oid: str
    object_format: str
    implementer: _ImplementerRecord
    reviewer: _ReviewerRecord
    initial_budget: ReviewBudget


@dataclass(frozen=True)
class _RoundSummary:
    """Validated status fields for the active review round."""

    status: str | None
    mode: str | None
    review_worktree: str | None


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
    round_status: str | None
    submission_mode: str | None
    handoff_status: str | None
    review_worktree: str | None
    review_budget: ReviewBudget
    next_action: str


@dataclass(frozen=True)
class RepositoryStatus:
    """Initialized repository status, with or without an active run."""

    repository_root: Path
    repository_id: str
    git_common_dir: Path
    worktree_git_dir: Path
    active_run: ActiveRunStatus | None
    next_action: str


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
    invocation_directory = _invocation_directory(start)

    lock_path = repository.control_root / LOCK_FILE_NAME
    try:
        with exclusive_file_lock(lock_path):
            return _start_run_locked(
                repository,
                task_path=task_path,
                context_paths=context_paths,
                invocation_directory=invocation_directory,
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
    """Return validated idle or active state for an initialized worktree."""

    repository = load_initialized_repository(start)
    current_identity = _repository_identity(repository.worktree)
    state_path = repository.control_root / STATE_FILE_NAME
    state = _load_existing_state(state_path)
    if state is None or state["active_run_id"] is None:
        return _idle_status(current_identity)

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
    round_details = _round_status_details(state["active_round"])
    handoff_status = _handoff_status(state["handoff"])

    run_directory = _safe_run_directory(repository.control_root, active_run_id)
    run_record_path = run_directory / RUN_RECORD_FILE_NAME
    run_record = _load_json_object(run_record_path, "active run record")
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

    _validate_event_log(
        run_directory / EVENT_LOG_FILE_NAME,
        active_run_id,
        record.base_oid,
    )
    next_action = _next_action(phase)
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
            round_status=round_details.status,
            submission_mode=round_details.mode,
            review_worktree=round_details.review_worktree,
            handoff_status=handoff_status,
            review_budget=budget,
            next_action=next_action,
        ),
        next_action=next_action,
    )


def _start_run_locked(
    repository: InitializedRepository,
    *,
    task_path: Path,
    context_paths: Sequence[Path],
    invocation_directory: Path,
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

    identity = _repository_identity(repository.worktree)
    object_format = _git_object_format(repository.worktree.root)
    resolved_base_oid = _resolve_commit(
        repository.worktree.root,
        base_ref,
        object_format,
    )
    task = _capture_input(
        task_path,
        invocation_directory,
        TASK_FILE_NAME,
        label="task specification",
        require_text=True,
    )
    contexts = tuple(
        _capture_input(
            path,
            invocation_directory,
            _context_run_path(index, path),
            label=f"context file {index}",
            require_text=False,
        )
        for index, path in enumerate(context_paths, start=1)
    )
    _reject_duplicate_context_sources(contexts)
    run_id = str(uuid.uuid4())
    timestamp = _utc_timestamp()
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
            _encode_json(run_record),
            mode=0o600,
        )
        atomic_write(
            staging_directory / EVENT_LOG_FILE_NAME,
            _encode_event(event),
            mode=0o600,
        )
        staging_directory.replace(run_directory)
        staging_directory = None
        run_directory_committed = True
        atomic_write(
            control_root / STATE_FILE_NAME,
            _encode_json(state),
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

    identity = _validate_repository_record(data["repository"])
    base_ref = _require_string(data["base_ref"], "run record.base_ref")
    object_format = _require_object_format(data["git_object_format"])
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
        context_count=len(context_value),
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
        kind=_require_agent_kind(
            data["kind"],
            "run record.implementer.kind",
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
        kind=_require_agent_kind(
            data["kind"],
            "run record.reviewer.kind",
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


def _captured_path(
    run_directory: Path,
    run_path: str,
    label: str,
) -> Path:
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
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
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


def _round_status_details(
    value: object,
) -> _RoundSummary:
    if value is None:
        return _RoundSummary(None, None, None)
    data = _require_object(value, "state.active_round")
    status_value = data.get("status")
    mode_value = data.get("mode")
    worktree_value = data.get("review_worktree")
    return _RoundSummary(
        status=_require_optional_string(
            status_value,
            "state.active_round.status",
        ),
        mode=_require_optional_string(
            mode_value,
            "state.active_round.mode",
        ),
        review_worktree=_require_optional_string(
            worktree_value, "state.active_round.review_worktree"
        ),
    )


def _handoff_status(value: object) -> str | None:
    if value is None:
        return None
    data = _require_object(value, "state.handoff")
    return _require_optional_string(data.get("status"), "state.handoff.status")


def _repository_identity(worktree: GitWorktree) -> RepositoryIdentity:
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
    return _require_object_format(result.stdout.rstrip("\r\n"))


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
        if not resolved.is_file():
            raise RunStartError(
                f"{label} must be a regular file: {resolved}"
            )
        with resolved.open("rb") as input_file:
            if not stat.S_ISREG(os.fstat(input_file.fileno()).st_mode):
                raise RunStartError(
                    f"{label} must be a regular file: {resolved}"
                )
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


def _invocation_directory(start: Path) -> Path:
    try:
        directory = start.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RunStartError(
            f"cannot resolve the current directory: {error}"
        ) from error
    if not directory.is_dir():
        raise RunStartError(f"current path is not a directory: {directory}")
    return directory


def _load_existing_state(path: Path) -> dict[str, object] | None:
    if not _path_exists(path):
        return None
    data = _load_json_object(path, "authoritative state")
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


def _safe_run_directory(control_root: Path, run_id: str) -> Path:
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


def _next_action(phase: RunPhase) -> str:
    if phase is not RunPhase.IMPLEMENTING:
        raise RunStateError(
            f"phase {phase.value} is not supported by this implementation "
            "increment"
        )
    return "continue implementing the captured task"


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
        raise RunStateError(
            f"state {label} does not match active run metadata"
        )


def _load_json_object(path: Path, label: str) -> dict[str, object]:
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


def _require_object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise RunStateError(f"{path} must be a JSON object")
    return value


def _check_fields(
    data: dict[str, object],
    *,
    required: set[str],
    path: str,
) -> None:
    missing = sorted(required - data.keys())
    if missing:
        raise RunStateError(
            f"{path} is missing required field(s): {', '.join(missing)}"
        )
    unknown = sorted(data.keys() - required)
    if unknown:
        label = "field" if len(unknown) == 1 else "fields"
        raise RunStateError(
            f"{path} has unknown {label}: {', '.join(unknown)}"
        )


def _require_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise RunStateError(f"{path} must be a non-empty string")
    if "\x00" in value:
        raise RunStateError(f"{path} must not contain null bytes")
    return value


def _require_optional_string(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, path)


def _require_int(value: object, path: str) -> int:
    if type(value) is not int:
        raise RunStateError(f"{path} must be an integer")
    return value


def _require_nonnegative_int(value: object, path: str) -> int:
    result = _require_int(value, path)
    if result < 0:
        raise RunStateError(f"{path} must not be negative")
    return result


def _require_uuid(value: object, path: str) -> str:
    text = _require_string(value, path)
    try:
        parsed = uuid.UUID(text)
    except ValueError:
        raise RunStateError(f"{path} must be a canonical UUID") from None
    if str(parsed) != text:
        raise RunStateError(f"{path} must be a canonical UUID")
    return text


def _require_phase(value: object, path: str) -> RunPhase:
    text = _require_string(value, path)
    try:
        return RunPhase(text)
    except ValueError:
        supported = ", ".join(phase.value for phase in RunPhase)
        raise RunStateError(f"{path} must be one of: {supported}") from None


def _require_agent_kind(value: object, path: str) -> AgentKind:
    text = _require_string(value, path)
    try:
        return AgentKind(text)
    except ValueError:
        supported = ", ".join(kind.value for kind in AgentKind)
        raise RunStateError(f"{path} must be one of: {supported}") from None


def _require_absolute_path(value: object, path: str) -> Path:
    text = _require_string(value, path)
    result = Path(text)
    if not result.is_absolute():
        raise RunStateError(f"{path} must be an absolute path")
    return result


def _require_digest(value: object, path: str) -> str:
    text = _require_string(value, path)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise RunStateError(f"{path} must be a lowercase SHA-256 digest")
    return text


def _require_object_format(value: object) -> str:
    object_format = _require_string(value, "Git object format")
    if object_format not in OID_LENGTHS:
        raise RunStateError(
            f"unsupported Git object format {object_format!r}; expected "
            "sha1 or sha256"
        )
    return object_format


def _require_oid(value: object, object_format: str, path: str) -> str:
    text = _require_string(value, path)
    expected_length = OID_LENGTHS[object_format]
    if (
        len(text) != expected_length
        or re.fullmatch(r"[0-9a-f]+", text) is None
    ):
        raise RunStateError(
            f"{path} must be a full lowercase {object_format} object ID"
        )
    return text


def _require_timestamp(value: object, path: str) -> str:
    text = _require_string(value, path)
    if UTC_TIMESTAMP_PATTERN.fullmatch(text) is None:
        raise RunStateError(f"{path} must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(f"{text[:-1]}+00:00")
    except ValueError:
        raise RunStateError(
            f"{path} must be an RFC 3339 UTC timestamp"
        ) from None
    if parsed.tzinfo != timezone.utc:
        raise RunStateError(f"{path} must be an RFC 3339 UTC timestamp")
    return text


def _validate_selection(value: str, label: str) -> str:
    if not value or value != value.strip() or "\x00" in value:
        raise RunStartError(
            f"{label} must be non-empty and contain no surrounding whitespace "
            "or null bytes"
        )
    return value


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _encode_json(value: dict[str, object]) -> bytes:
    return f"{json.dumps(value, indent=2)}\n".encode("utf-8")


def _encode_event(value: dict[str, object]) -> bytes:
    return f"{json.dumps(value, separators=(',', ':'))}\n".encode("utf-8")


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)
