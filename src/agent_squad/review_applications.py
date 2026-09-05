"""Implementation-side application of reviews and approved completion."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import uuid

from . import runs
from .artifacts import (
    ActiveRoundRecord,
    APPROVAL_FILE_NAME,
    ApprovalRecord,
    ArtifactValidationError,
    BundleArtifact,
    DeveloperResolution,
    ESCALATIONS_DIRECTORY_NAME,
    EscalationReason,
    EscalationRecord,
    REVIEW_MARKDOWN_FILE_NAME,
    REVIEW_MARKER_FILE_NAME,
    ReviewResult,
    ReviewResponse,
    ReviewRoundRecord,
    ReviewerLocalMarker,
    ReviewSupersession,
    ReviewVerdict,
    REVIEW_RESULT_FILE_NAME,
    RESOLUTIONS_DIRECTORY_NAME,
    ResponseDisposition,
    ROUND_RESPONSE_FILE_NAME,
    RoundStatus,
    SubmissionMode,
    validate_review_response,
)
from .handoffs import (
    HandoffRecoveryError,
    archive_invalid_review_evidence,
    validate_invalid_review_diagnostic,
)
from .herdr import HerdrClient, HerdrError, format_herdr_error
from .initialization import (
    AgentSquadError,
    InitializedRepository,
    REVIEW_DIRECTORY_NAME,
    discover_git_worktree,
    is_agent_squad_runtime_path,
    load_initialized_repository,
    matches_allowed_generated_path,
    run_git,
)
from .review_submissions import (
    MARKER_PATH,
    REQUEST_PATH,
    MarkerConfirmedReview,
    RETIRED_RESULTS_PATH,
    ReviewEvidenceAccessError,
    RetiredReviewIdentityError,
    ReviewSubmissionError,
    SUBMISSION_LOCK_PATH,
    load_marker_confirmed_review,
    retired_review_identity_digest,
    verify_flagged_tracked_files,
)
from .storage import (
    InvalidJsonError,
    append_event,
    atomic_write,
    decode_json,
    encode_event,
    encode_json,
    exclusive_file_lock,
    read_regular_tree,
    utc_timestamp,
)
from .submissions import review_worktree_path


BUNDLE_ARCHIVE_DIRECTORY_NAME = "bundle"
RETIRED_APPLY_ATTEMPTS_DIRECTORY_NAME = "retired-apply-attempts"
LATE_RESULTS_DIRECTORY = PurePosixPath("diagnostics/late-results")
INVALID_RESULTS_DIRECTORY = PurePosixPath("diagnostics/invalid-results")
_PROVISIONAL_APPLY_ARTIFACT_NAMES = (
    REVIEW_RESULT_FILE_NAME,
    REVIEW_MARKDOWN_FILE_NAME,
    REVIEW_MARKER_FILE_NAME,
    APPROVAL_FILE_NAME,
)
_DEFAULT_ESCALATION_NOTE = (
    b"# Developer decision needed\n\n"
    b"The Implementer requested Developer authority.\n"
)


class ReviewApplicationError(AgentSquadError):
    """Raised when review evidence cannot be applied or completed safely."""


@dataclass(frozen=True)
class ApplyReviewResult:
    """Durable outcome of applying one review result."""

    run_id: str
    round_number: int
    result_id: str
    classification: RoundStatus
    verdict: ReviewVerdict | None
    head_oid: str
    observed_head_oid: str | None
    approval_path: Path | None
    bundle_archive: Path | None
    diagnostic_path: Path | None
    reason: str | None
    replayed: bool
    next_action: str
    cleanup_warnings: tuple[str, ...]


@dataclass(frozen=True)
class CompleteRunResult:
    """Durable outcome of completing one exactly approved run."""

    run_id: str
    head_oid: str
    already_completed: bool
    cleanup_warnings: tuple[str, ...]


@dataclass(frozen=True)
class SupersedeReviewResult:
    """Durable outcome of invalidating one active review round."""

    run_id: str
    round_number: int
    head_oid: str
    actor: str
    cause: str
    reviewer_notice_sent: bool
    reviewer_notice_error: str | None
    late_result_id: str | None
    cleanup_warnings: tuple[str, ...]


@dataclass(frozen=True)
class EscalateRunResult:
    """Durable outcome of requesting a Developer decision."""

    run_id: str
    escalation_id: str
    previous_phase: runs.RunPhase
    round_number: int
    head_oid: str
    escalation_path: Path
    note_path: Path
    response_path: Path | None
    reviewer_notice_sent: bool
    reviewer_notice_error: str | None
    cleanup_warnings: tuple[str, ...]


@dataclass(frozen=True)
class ResumeRunResult:
    """Durable outcome of recording a Developer resolution and resuming."""

    run_id: str
    resolution_id: str
    escalation_id: str
    resolution_path: Path
    companion_path: Path
    additional_rounds_granted: int
    effective_review_limit: int


@dataclass(frozen=True)
class _CapturedMarkdown:
    """Validated Markdown staged for an authoritative artifact."""

    content: bytes
    sha256: str


@dataclass(frozen=True)
class _CapturedEscalationResponse:
    """Validated response staged with a direct escalation."""

    response: ReviewResponse
    content: bytes
    authority_path: Path
    original: bytes | None


@dataclass(frozen=True)
class _StagedRecord:
    """One metadata record participating in an authoritative transition."""

    path: Path
    original: bytes | None
    staged: bytes


@dataclass(frozen=True)
class _ActiveReviewRecords:
    """Validated mutable records for one active review transition."""

    run_directory: Path
    round_directory: Path
    round_path: Path
    run_path: Path
    state_path: Path
    event_path: Path
    round_record: ReviewRoundRecord
    run_record: dict[str, object]
    state: dict[str, object]
    original_round: bytes
    original_run: bytes
    original_state: bytes


def apply_review(
    start: Path,
    *,
    result_id: str | None = None,
) -> ApplyReviewResult:
    """Apply the active marker-confirmed review result exactly once."""

    repository = load_initialized_repository(start)
    presented_result_id = _optional_result_id(result_id)
    lock_path = repository.control_root / runs.LOCK_FILE_NAME
    try:
        with exclusive_file_lock(lock_path):
            return _apply_review_locked(
                repository,
                presented_result_id=presented_result_id,
            )
    except AgentSquadError:
        raise
    except OSError as error:
        raise ReviewApplicationError(
            f"could not acquire or use the local review-application lock "
            f"{lock_path}: {error}"
        ) from error


@dataclass(frozen=True)
class CancelRunResult:
    """Durable cancellation outcome, including best-effort resource release."""

    run_id: str
    already_cancelled: bool
    cleanup_warnings: tuple[str, ...]


def cancel_run(
    start: Path,
    *,
    reason: str,
    herdr_client: HerdrClient | None = None,
) -> CancelRunResult:
    """Cancel a nonterminal run without discarding its authority history."""

    repository = load_initialized_repository(start)
    cause = _supersede_cause(reason, command="cancel")
    try:
        with exclusive_file_lock(
            repository.control_root / runs.LOCK_FILE_NAME
        ):
            return _cancel_run_locked(repository, cause, herdr_client)
    except OSError as error:
        raise ReviewApplicationError(
            f"could not cancel the run: {error}"
        ) from error


def _cancel_run_locked(
    repository: InitializedRepository,
    cause: str,
    herdr_client: HerdrClient | None,
) -> CancelRunResult:
    try:
        terminal = runs.load_cancelled_run(repository)
    except runs.RunStateError as error:
        raise ReviewApplicationError(str(error)) from error
    if terminal is not None:
        return CancelRunResult(
            run_id=terminal.run_id,
            already_cancelled=True,
            cleanup_warnings=(),
        )

    active = runs.inspect_status_locked(
        repository.worktree.invocation_directory,
        validate_live_review_bundle=False,
    ).active_run
    if active is None:
        raise ReviewApplicationError("there is no active run to cancel")
    _validate_implementation_identity(repository, active)
    run_directory = runs.safe_run_directory(
        repository.control_root, active.run_id
    )
    run_path = run_directory / runs.RUN_RECORD_FILE_NAME
    event_path = run_directory / runs.EVENT_LOG_FILE_NAME
    run_record = runs.load_json_object(run_path, "run record")
    state_path = repository.control_root / runs.STATE_FILE_NAME
    state = runs.load_json_object(state_path, "authoritative state")
    timestamp = utc_timestamp()
    next_state = copy.deepcopy(state)
    next_state.update(
        updated_at=timestamp,
        active_run_id=None,
        terminal_run_id=active.run_id,
        phase=runs.RunPhase.CANCELLED.value,
        approved_head_oid=None,
        active_escalation_id=None,
    )
    next_run = copy.deepcopy(run_record)
    next_run.update(phase=runs.RunPhase.CANCELLED.value, finished_at=timestamp)
    records: list[_StagedRecord] = []
    closed_round: ReviewRoundRecord | None = None
    round_directory = run_directory / "rounds" / f"{active.current_round:03d}"
    if active.phase is runs.RunPhase.REVIEWING:
        round_path = round_directory / "round.json"
        round_record = ReviewRoundRecord.from_dict(
            runs.load_json_object(round_path, "active round"),
            label="active round",
        )
        _assert_round_is_active(round_record, active)
        closed_round = replace(
            round_record,
            updated_at=timestamp,
            status=RoundStatus.SUPERSEDED,
            supersession=ReviewSupersession(
                created_at=timestamp,
                actor=active.implementer_agent,
                cause=f"run_cancelled: {cause}",
            ),
        )
        if active.active_round is None:
            raise ReviewApplicationError(
                "reviewing state lost its active round during cancellation"
            )
        next_state["active_round"] = replace(
            active.active_round, status=RoundStatus.SUPERSEDED
        ).to_dict()
        records.append(_StagedRecord(
            path=round_path,
            original=round_path.read_bytes(),
            staged=encode_json(closed_round.to_dict()),
        ))
    original_events, staged_events = _stage_event_log(
        event_path,
        {
            "timestamp": timestamp,
            "event": "run_cancelled",
            "run_id": active.run_id,
            "round": active.current_round,
            "previous_phase": active.phase.value,
            "actor": active.implementer_agent,
            "cause": cause,
        },
        identity_fields=("event", "run_id"),
    )
    records.extend((
        _StagedRecord(
            path=run_path,
            original=run_path.read_bytes(),
            staged=encode_json(next_run),
        ),
        _StagedRecord(
            path=event_path,
            original=original_events,
            staged=staged_events,
        ),
    ))
    _persist_authoritative_transition(
        records=tuple(records),
        state_path=state_path,
        original_state=state_path.read_bytes(),
        next_state=encode_json(next_state),
        failure_message="could not persist cancellation",
    )
    warnings: list[str] = []
    if closed_round is not None:
        if active.active_round is None:
            raise ReviewApplicationError(
                "reviewing state lost its active round during cancellation"
            )
        client = herdr_client or HerdrClient(repository.worktree.root)
        try:
            client.discover(active.reviewer_kind, role="Reviewer")
            sent = client.dispatch_reviewer_notice(
                reviewer_name=active.active_round.reviewer_name,
                reviewer_kind=active.reviewer_kind,
                review_worktree=active.active_round.review_worktree,
                prompt=_run_cancelled_prompt(
                    run_id=active.run_id,
                    round_number=active.current_round,
                    cause=cause,
                ),
            )
            if not sent:
                warnings.append(
                    "Reviewer cancellation notice could not be delivered"
                )
        except HerdrError as error:
            warnings.append(
                "Reviewer cancellation notice failed: "
                f"{format_herdr_error(str(error))}"
            )
    try:
        if closed_round is not None:
            _, cleanup = _cleanup_superseded_review_resources(
                repository, active=active, round_directory=round_directory,
                round_record=closed_round,
            )
            warnings.extend(cleanup)
        elif active.active_round is not None:
            if active.active_round.status is RoundStatus.SUPERSEDED:
                round_record = ReviewRoundRecord.from_dict(
                    runs.load_json_object(
                        round_directory / "round.json", "closed round"
                    ),
                    label="closed round",
                )
                _, cleanup = _cleanup_superseded_review_resources(
                    repository, active=active, round_directory=round_directory,
                    round_record=round_record,
                )
                warnings.extend(cleanup)
            else:
                warnings.extend(_cleanup_review_resources(repository, active))
    except (AgentSquadError, OSError) as error:
        warnings.append(f"run cancelled, but review cleanup failed: {error}")
    return CancelRunResult(
        run_id=active.run_id,
        already_cancelled=False,
        cleanup_warnings=tuple(warnings),
    )


def complete_run(start: Path) -> CompleteRunResult:
    """Complete and release one run at its exact approved revision."""

    repository = load_initialized_repository(start)
    lock_path = repository.control_root / runs.LOCK_FILE_NAME
    try:
        with exclusive_file_lock(lock_path):
            terminal = _completed_replay(repository)
            if terminal is not None:
                return terminal
            return _complete_run_locked(repository)
    except AgentSquadError:
        raise
    except OSError as error:
        raise ReviewApplicationError(
            f"could not acquire or use the local completion lock "
            f"{lock_path}: {error}"
        ) from error


def supersede_review(
    start: Path,
    *,
    reason: str,
    herdr_client: HerdrClient | None = None,
) -> SupersedeReviewResult:
    """Supersede the active review and return its run to implementation."""

    repository = load_initialized_repository(start)
    cause = _supersede_cause(reason)
    lock_path = repository.control_root / runs.LOCK_FILE_NAME
    try:
        with exclusive_file_lock(lock_path):
            return _supersede_review_locked(
                repository,
                cause=cause,
                herdr_client=herdr_client,
            )
    except AgentSquadError:
        raise
    except OSError as error:
        raise ReviewApplicationError(
            f"could not acquire or use the local supersede lock "
            f"{lock_path}: {error}"
        ) from error


def escalate_run(
    start: Path,
    *,
    note_path: Path | None = None,
    response_path: Path | None = None,
    herdr_client: HerdrClient | None = None,
) -> EscalateRunResult:
    """Persist a direct request for Developer authority."""

    repository = load_initialized_repository(start)
    lock_path = repository.control_root / runs.LOCK_FILE_NAME
    try:
        with exclusive_file_lock(lock_path):
            return _escalate_run_locked(
                repository,
                note_path=note_path,
                response_path=response_path,
                herdr_client=herdr_client,
            )
    except AgentSquadError:
        raise
    except OSError as error:
        raise ReviewApplicationError(
            f"could not acquire or use the local escalation lock "
            f"{lock_path}: {error}"
        ) from error


def resume_run(
    start: Path,
    *,
    resolution_path: Path,
    applies_to_finding_ids: tuple[str, ...] = (),
    additional_rounds: int = 0,
) -> ResumeRunResult:
    """Record a Developer decision and return a run to implementation."""

    if type(additional_rounds) is not int or additional_rounds < 0:
        raise ReviewApplicationError(
            "additional review rounds must be a non-negative integer"
        )
    repository = load_initialized_repository(start)
    lock_path = repository.control_root / runs.LOCK_FILE_NAME
    try:
        with exclusive_file_lock(lock_path):
            return _resume_run_locked(
                repository,
                resolution_path=resolution_path,
                applies_to_finding_ids=applies_to_finding_ids,
                additional_rounds=additional_rounds,
            )
    except AgentSquadError:
        raise
    except OSError as error:
        raise ReviewApplicationError(
            f"could not acquire or use the local resume lock "
            f"{lock_path}: {error}"
        ) from error


def _escalate_run_locked(
    repository: InitializedRepository,
    *,
    note_path: Path | None,
    response_path: Path | None,
    herdr_client: HerdrClient | None,
) -> EscalateRunResult:
    status = runs.inspect_status_locked(
        repository.worktree.invocation_directory,
        validate_live_review_bundle=False,
    )
    active = status.active_run
    if active is None:
        raise ReviewApplicationError("there is no active run to escalate")
    if active.phase is runs.RunPhase.NEEDS_HUMAN:
        return _replay_direct_escalation(
            repository,
            active=active,
            note_path=note_path,
            response_path=response_path,
        )
    if active.phase not in {
        runs.RunPhase.IMPLEMENTING,
        runs.RunPhase.REVIEWING,
        runs.RunPhase.APPROVED,
    }:
        raise ReviewApplicationError(
            f"run {active.run_id} is in phase {active.phase.value}; "
            "escalate is allowed only while implementing, reviewing, or "
            "approved"
        )
    _validate_implementation_identity(repository, active)
    head_oid = _current_head(
        repository.worktree.root,
        active.git_object_format,
        label="implementation",
    )
    note = _capture_markdown(
        note_path,
        invocation_directory=repository.worktree.invocation_directory,
        label="escalation note",
        default=_DEFAULT_ESCALATION_NOTE,
    )
    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    response = _capture_escalation_response(
        repository,
        active=active,
        run_directory=run_directory,
        response_path=response_path,
    )
    escalation_number = len(active.escalations) + 1
    timestamp = _next_history_timestamp(
        (
            *(item.record.created_at for item in active.escalations),
            *(item.record.created_at for item in active.resolutions),
        )
    )
    escalation_id = str(uuid.uuid4())
    stem = f"{escalation_number:03d}-escalation"
    escalation_directory = run_directory / ESCALATIONS_DIRECTORY_NAME
    escalation_path = escalation_directory / f"{stem}.json"
    companion_path = escalation_directory / f"{stem}.md"
    _require_new_artifact_path(escalation_path, "escalation artifact")
    _require_new_artifact_path(companion_path, "escalation note")

    source_request_id: str | None = None
    source_result_id: str | None = None
    active_round = active.active_round
    if active_round is not None:
        source_request_id = active_round.request_id
        source_result_id = active_round.result_id
    if response is not None:
        previous = runs.find_latest_applied_review_before(
            run_directory=run_directory,
            run_id=active.run_id,
            current_round=active.current_round + 1,
            base_oid=active.base_oid,
            object_format=active.git_object_format,
        )
        if previous is None:
            raise ReviewApplicationError(
                "an escalation response has no applied review authority"
            )
        source_request_id = previous.review.request_id
        source_result_id = previous.review.result_id
    related_finding_ids = (
        tuple(
            item.finding_id
            for item in response.response.responses
            if item.disposition is ResponseDisposition.NEEDS_HUMAN
        )
        if response is not None
        else ()
    )
    escalation = EscalationRecord(
        created_at=timestamp,
        escalation_id=escalation_id,
        run_id=active.run_id,
        round_number=active.current_round,
        head_oid=head_oid,
        related_finding_ids=related_finding_ids,
        source_request_id=source_request_id,
        source_result_id=source_result_id,
        previous_approved_head_oid=active.approved_head_oid,
        previous_phase=active.phase.value,
        actor=active.implementer_agent,
        note_path=companion_path.name,
        note_sha256=note.sha256,
        response_id=(
            response.response.response_id if response is not None else None
        ),
        reason=EscalationReason.IMPLEMENTER_REQUESTED,
    )
    try:
        escalation = EscalationRecord.from_dict(
            escalation.to_dict(),
            object_format=active.git_object_format,
            label="Developer escalation",
        )
    except ArtifactValidationError as error:
        raise ReviewApplicationError(
            f"cannot build Developer escalation: {error}"
        ) from error

    state_path = repository.control_root / runs.STATE_FILE_NAME
    run_path = run_directory / runs.RUN_RECORD_FILE_NAME
    event_path = run_directory / runs.EVENT_LOG_FILE_NAME
    state = runs.load_json_object(state_path, "authoritative state")
    run_record = runs.load_json_object(run_path, "active run record")
    next_run = copy.deepcopy(run_record)
    next_run["phase"] = runs.RunPhase.NEEDS_HUMAN.value
    next_state = copy.deepcopy(state)
    next_state.update(
        updated_at=timestamp,
        phase=runs.RunPhase.NEEDS_HUMAN.value,
        approved_head_oid=None,
        active_escalation_id=escalation_id,
    )
    records: list[_StagedRecord] = [
        _StagedRecord(
            path=companion_path,
            original=None,
            staged=note.content,
        ),
        _StagedRecord(
            path=escalation_path,
            original=None,
            staged=encode_json(escalation.to_dict()),
        ),
    ]
    next_round: ReviewRoundRecord | None = None
    round_directory: Path | None = None
    if active.phase is runs.RunPhase.REVIEWING:
        if active_round is None:
            raise ReviewApplicationError(
                "reviewing state lost its active round during escalation"
            )
        records_for_review = _load_active_review_records(repository, active)
        supersession = ReviewSupersession(
            created_at=timestamp,
            actor=active.implementer_agent,
            cause=f"escalation:{escalation_id}",
        )
        next_round = replace(
            records_for_review.round_record,
            updated_at=timestamp,
            status=RoundStatus.SUPERSEDED,
            supersession=supersession,
        )
        next_active_round = replace(
            active_round,
            status=RoundStatus.SUPERSEDED,
        )
        next_state["active_round"] = next_active_round.to_dict()
        records.append(
            _StagedRecord(
                path=records_for_review.round_path,
                original=records_for_review.original_round,
                staged=encode_json(next_round.to_dict()),
            )
        )
        round_directory = records_for_review.round_directory
    if response is not None:
        records.append(
            _StagedRecord(
                path=response.authority_path,
                original=response.original,
                staged=response.content,
            )
        )
    records.append(
        _StagedRecord(
            path=run_path,
            original=run_path.read_bytes(),
            staged=encode_json(next_run),
        )
    )
    event = {
        "timestamp": timestamp,
        "event": "run_escalated",
        "run_id": active.run_id,
        "escalation_id": escalation_id,
        "round": active.current_round,
        "head_oid": head_oid,
        "previous_phase": active.phase.value,
        "actor": active.implementer_agent,
        "reason": EscalationReason.IMPLEMENTER_REQUESTED.value,
    }
    original_events, staged_events = _stage_event_log(
        event_path,
        event,
        identity_fields=("event", "run_id", "escalation_id"),
    )
    records.append(
        _StagedRecord(
            path=event_path,
            original=original_events,
            staged=staged_events,
        )
    )
    directory_created = _ensure_history_directory(
        escalation_directory,
        "Developer escalation history",
    )
    try:
        _validate_implementation_identity(repository, active)
        if _current_head(
            repository.worktree.root,
            active.git_object_format,
            label="implementation",
        ) != head_oid:
            raise ReviewApplicationError(
                "implementation HEAD changed while escalation artifacts "
                "were being prepared; no escalation was recorded"
            )
        _persist_authoritative_transition(
            records=tuple(records),
            state_path=state_path,
            original_state=state_path.read_bytes(),
            next_state=encode_json(next_state),
            failure_message="could not persist Developer escalation",
        )
    except BaseException:
        if directory_created:
            _remove_empty_directory(escalation_directory)
        raise

    reviewer_notice_sent = False
    reviewer_notice_error: str | None = None
    cleanup_warnings: tuple[str, ...] = ()
    if (
        active.phase is runs.RunPhase.REVIEWING
        and active_round is not None
        and next_round is not None
        and round_directory is not None
    ):
        client = herdr_client or HerdrClient(repository.worktree.root)
        try:
            client.discover(active.reviewer_kind, role="Reviewer")
            reviewer_notice_sent = client.dispatch_reviewer_notice(
                reviewer_name=active_round.reviewer_name,
                reviewer_kind=active.reviewer_kind,
                review_worktree=active_round.review_worktree,
                prompt=_review_escalated_prompt(
                    run_id=active.run_id,
                    round_number=active.current_round,
                    escalation_id=escalation_id,
                ),
            )
            if not reviewer_notice_sent:
                reviewer_notice_error = (
                    f"Reviewer {active_round.reviewer_name!r} is not available"
                )
        except HerdrError as error:
            reviewer_notice_error = format_herdr_error(str(error))
        _, cleanup_warnings = _cleanup_superseded_review_resources(
            repository,
            active=active,
            round_directory=round_directory,
            round_record=next_round,
        )
    return EscalateRunResult(
        run_id=active.run_id,
        escalation_id=escalation_id,
        previous_phase=active.phase,
        round_number=active.current_round,
        head_oid=head_oid,
        escalation_path=escalation_path,
        note_path=companion_path,
        response_path=(
            response.authority_path if response is not None else None
        ),
        reviewer_notice_sent=reviewer_notice_sent,
        reviewer_notice_error=reviewer_notice_error,
        cleanup_warnings=cleanup_warnings,
    )


def _replay_direct_escalation(
    repository: InitializedRepository,
    *,
    active: runs.ActiveRunStatus,
    note_path: Path | None,
    response_path: Path | None,
) -> EscalateRunResult:
    """Replay an identical committed direct escalation without mutation."""

    escalation_id = active.active_escalation_id
    if escalation_id is None:
        raise ReviewApplicationError(
            "needs_human state lost its active escalation"
        )
    authority = next(
        (
            item
            for item in active.escalations
            if item.record.escalation_id == escalation_id
        ),
        None,
    )
    if authority is None or (
        authority.record.reason
        is not EscalationReason.IMPLEMENTER_REQUESTED
    ):
        raise ReviewApplicationError(
            f"run {active.run_id} is already in needs_human; resolve its "
            "active escalation before requesting another"
        )
    note = _capture_markdown(
        note_path,
        invocation_directory=repository.worktree.invocation_directory,
        label="escalation note",
        default=_DEFAULT_ESCALATION_NOTE,
    )
    if note.content != authority.note_bytes:
        raise ReviewApplicationError(
            "the active escalation is already authoritative and its note "
            "differs from this retry"
        )

    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    canonical_response: Path | None = None
    if authority.record.response_id is None:
        if response_path is not None:
            raise ReviewApplicationError(
                "the active escalation is already authoritative without a "
                "response; escalate cannot add one"
            )
    else:
        if response_path is None:
            raise ReviewApplicationError(
                "an identical retry of this active escalation requires its "
                "original --response"
            )
        canonical_response = (
            run_directory
            / "rounds"
            / f"{authority.record.round_number:03d}"
            / ROUND_RESPONSE_FILE_NAME
        )
        candidate = _read_input_bytes(
            response_path,
            invocation_directory=repository.worktree.invocation_directory,
            label="implementation response",
        )
        canonical = _read_authoritative_bytes(
            canonical_response,
            label="authoritative implementation response",
        )
        if candidate != canonical:
            raise ReviewApplicationError(
                "the active escalation already references a different "
                "response; escalate cannot overwrite it"
            )
        try:
            response = ReviewResponse.from_dict(
                decode_json(canonical.decode("utf-8")),
                object_format=active.git_object_format,
            )
        except (
            UnicodeDecodeError,
            InvalidJsonError,
            ArtifactValidationError,
        ) as error:
            raise ReviewApplicationError(
                f"authoritative implementation response is invalid: {error}"
            ) from error
        if response.response_id != authority.record.response_id:
            raise ReviewApplicationError(
                "authoritative implementation response ID does not match "
                "the active escalation"
            )
        if (
            response.run_id != active.run_id
            or response.review_result_id
            != authority.record.source_result_id
        ):
            raise ReviewApplicationError(
                "authoritative implementation response source does not "
                "match the active escalation"
            )
    return EscalateRunResult(
        run_id=active.run_id,
        escalation_id=authority.record.escalation_id,
        previous_phase=runs.RunPhase(authority.record.previous_phase),
        round_number=authority.record.round_number,
        head_oid=authority.record.head_oid,
        escalation_path=authority.path,
        note_path=authority.note_path,
        response_path=canonical_response,
        reviewer_notice_sent=False,
        reviewer_notice_error=None,
        cleanup_warnings=(),
    )


def _resume_run_locked(
    repository: InitializedRepository,
    *,
    resolution_path: Path,
    applies_to_finding_ids: tuple[str, ...],
    additional_rounds: int,
) -> ResumeRunResult:
    status = runs.inspect_status_locked(
        repository.worktree.invocation_directory,
        validate_live_review_bundle=False,
    )
    active = status.active_run
    if active is None:
        raise ReviewApplicationError("there is no active run to resume")
    if active.phase is not runs.RunPhase.NEEDS_HUMAN:
        raise ReviewApplicationError(
            f"run {active.run_id} is in phase {active.phase.value}; "
            "resume requires a needs_human run"
        )
    if (
        active.review_budget.completed_change_reviews
        >= active.review_budget.effective_limit
        and additional_rounds == 0
    ):
        raise ReviewApplicationError(
            "review budget is exhausted; resume requires --extend-rounds"
        )
    escalation_id = active.active_escalation_id
    if escalation_id is None:
        raise ReviewApplicationError(
            "needs_human state lost its active escalation"
        )
    escalation_authority = next(
        (
            item
            for item in active.escalations
            if item.record.escalation_id == escalation_id
        ),
        None,
    )
    if escalation_authority is None:
        raise ReviewApplicationError(
            "active escalation does not belong to the current run"
        )
    finding_ids = _validated_finding_ids(applies_to_finding_ids)
    unknown_ids = sorted(
        set(finding_ids)
        - set(escalation_authority.record.related_finding_ids)
    )
    if unknown_ids:
        raise ReviewApplicationError(
            "Developer resolution references finding IDs outside the active "
            f"escalation: {', '.join(unknown_ids)}"
        )
    resolution = _capture_markdown(
        resolution_path,
        invocation_directory=repository.worktree.invocation_directory,
        label="Developer resolution",
    )
    _validate_implementation_identity(repository, active)
    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    resolution_number = len(active.resolutions) + 1
    timestamp = _next_history_timestamp(
        (
            escalation_authority.record.created_at,
            *(item.record.created_at for item in active.resolutions),
        )
    )
    resolution_id = str(uuid.uuid4())
    stem = f"{resolution_number:03d}-resolution"
    resolution_directory = run_directory / RESOLUTIONS_DIRECTORY_NAME
    record_path = resolution_directory / f"{stem}.json"
    companion_path = resolution_directory / f"{stem}.md"
    _require_new_artifact_path(record_path, "Developer resolution artifact")
    _require_new_artifact_path(companion_path, "Developer resolution")
    record = DeveloperResolution(
        created_at=timestamp,
        resolution_id=resolution_id,
        run_id=active.run_id,
        resolves_escalation_id=escalation_id,
        applies_to_finding_ids=finding_ids,
        resolution_path=companion_path.name,
        resolution_sha256=resolution.sha256,
        additional_rounds_granted=additional_rounds,
    )
    try:
        record = DeveloperResolution.from_dict(
            record.to_dict(),
            label="Developer resolution",
        )
    except ArtifactValidationError as error:
        raise ReviewApplicationError(
            f"cannot build Developer resolution: {error}"
        ) from error

    next_budget = replace(
        active.review_budget,
        additional_rounds_granted=(
            active.review_budget.additional_rounds_granted
            + additional_rounds
        ),
        effective_limit=(
            active.review_budget.effective_limit + additional_rounds
        ),
    )
    state_path = repository.control_root / runs.STATE_FILE_NAME
    run_path = run_directory / runs.RUN_RECORD_FILE_NAME
    event_path = run_directory / runs.EVENT_LOG_FILE_NAME
    state = runs.load_json_object(state_path, "authoritative state")
    run_record = runs.load_json_object(run_path, "active run record")
    next_run = copy.deepcopy(run_record)
    next_run["phase"] = runs.RunPhase.IMPLEMENTING.value
    next_state = copy.deepcopy(state)
    next_state.update(
        updated_at=timestamp,
        phase=runs.RunPhase.IMPLEMENTING.value,
        active_escalation_id=None,
        review_budget=next_budget.to_dict(),
    )
    event = {
        "timestamp": timestamp,
        "event": "run_resumed",
        "run_id": active.run_id,
        "escalation_id": escalation_id,
        "resolution_id": resolution_id,
        "additional_rounds_granted": additional_rounds,
        "effective_review_limit": next_budget.effective_limit,
    }
    original_events, staged_events = _stage_event_log(
        event_path,
        event,
        identity_fields=("event", "run_id", "resolution_id"),
    )
    directory_created = _ensure_history_directory(
        resolution_directory,
        "Developer resolution history",
    )
    try:
        _validate_implementation_identity(repository, active)
        _persist_authoritative_transition(
            records=(
                _StagedRecord(
                    path=companion_path,
                    original=None,
                    staged=resolution.content,
                ),
                _StagedRecord(
                    path=record_path,
                    original=None,
                    staged=encode_json(record.to_dict()),
                ),
                _StagedRecord(
                    path=run_path,
                    original=run_path.read_bytes(),
                    staged=encode_json(next_run),
                ),
                _StagedRecord(
                    path=event_path,
                    original=original_events,
                    staged=staged_events,
                ),
            ),
            state_path=state_path,
            original_state=state_path.read_bytes(),
            next_state=encode_json(next_state),
            failure_message="could not persist Developer resolution",
        )
    except BaseException:
        if directory_created:
            _remove_empty_directory(resolution_directory)
        raise
    return ResumeRunResult(
        run_id=active.run_id,
        resolution_id=resolution_id,
        escalation_id=escalation_id,
        resolution_path=record_path,
        companion_path=companion_path,
        additional_rounds_granted=additional_rounds,
        effective_review_limit=next_budget.effective_limit,
    )


def _supersede_review_locked(
    repository: InitializedRepository,
    *,
    cause: str,
    herdr_client: HerdrClient | None,
) -> SupersedeReviewResult:
    status = runs.inspect_status_locked(
        repository.worktree.invocation_directory,
        validate_live_review_bundle=False,
    )
    active = status.active_run
    if active is None:
        raise ReviewApplicationError(
            "there is no active run with a review to supersede"
        )
    active_round = active.active_round
    if (
        active.phase is not runs.RunPhase.REVIEWING
        or active_round is None
        or active_round.status is not RoundStatus.REVIEWING
    ):
        raise ReviewApplicationError(
            f"run {active.run_id} is in phase {active.phase.value}; "
            "supersede requires an active reviewing round"
        )
    _validate_implementation_identity(repository, active)

    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    round_directory = (
        run_directory / "rounds" / f"{active.current_round:03d}"
    )
    round_path = round_directory / "round.json"
    run_path = run_directory / runs.RUN_RECORD_FILE_NAME
    state_path = repository.control_root / runs.STATE_FILE_NAME
    state = runs.load_json_object(state_path, "authoritative state")
    run_record = runs.load_json_object(run_path, "active run record")
    round_data = runs.load_json_object(round_path, "active round record")
    original_state = state_path.read_bytes()
    try:
        round_record = ReviewRoundRecord.from_dict(
            round_data,
            label="active round record",
        )
    except ArtifactValidationError as error:
        raise ReviewApplicationError(str(error)) from error
    _assert_round_is_active(round_record, active)

    timestamp = utc_timestamp()
    actor = active.implementer_agent
    supersession = ReviewSupersession(
        created_at=timestamp,
        actor=actor,
        cause=cause,
    )
    next_round = replace(
        round_record,
        updated_at=timestamp,
        status=RoundStatus.SUPERSEDED,
        supersession=supersession,
    )
    next_active_round = replace(
        active_round,
        status=RoundStatus.SUPERSEDED,
    )
    next_run = copy.deepcopy(run_record)
    next_run["phase"] = runs.RunPhase.IMPLEMENTING.value
    next_state = copy.deepcopy(state)
    next_state.update(
        updated_at=timestamp,
        phase=runs.RunPhase.IMPLEMENTING.value,
        active_round=next_active_round.to_dict(),
    )
    superseded_event = {
        "timestamp": timestamp,
        "event": "review_superseded",
        "run_id": active.run_id,
        "round": active.current_round,
        "request_id": active_round.request_id,
        "head_oid": active.current_head_oid,
        "actor": actor,
        "cause": cause,
    }
    event_path = run_directory / runs.EVENT_LOG_FILE_NAME
    original_events, staged_events = _stage_event_log(
        event_path,
        superseded_event,
        identity_fields=("event", "run_id", "round"),
    )
    _validate_implementation_identity(repository, active)
    _persist_authoritative_transition(
        records=(
            _StagedRecord(
                path=round_path,
                original=round_path.read_bytes(),
                staged=encode_json(next_round.to_dict()),
            ),
            _StagedRecord(
                path=run_path,
                original=run_path.read_bytes(),
                staged=encode_json(next_run),
            ),
            _StagedRecord(
                path=event_path,
                original=original_events,
                staged=staged_events,
            ),
        ),
        state_path=state_path,
        original_state=original_state,
        next_state=encode_json(next_state),
        failure_message="could not persist superseded review state",
    )

    client = herdr_client or HerdrClient(repository.worktree.root)
    reviewer_notice_sent = False
    reviewer_notice_error: str | None = None
    try:
        client.discover(active.reviewer_kind, role="Reviewer")
        reviewer_notice_sent = client.dispatch_reviewer_notice(
            reviewer_name=active_round.reviewer_name,
            reviewer_kind=active.reviewer_kind,
            review_worktree=active_round.review_worktree,
            prompt=_review_superseded_prompt(
                run_id=active.run_id,
                round_number=active.current_round,
                cause=cause,
            ),
        )
        if not reviewer_notice_sent:
            reviewer_notice_error = (
                f"Reviewer {active_round.reviewer_name!r} is not available"
            )
    except HerdrError as error:
        reviewer_notice_error = format_herdr_error(str(error))

    late_result_id, cleanup_warnings = (
        _cleanup_superseded_review_resources(
            repository,
            active=active,
            round_directory=round_directory,
            round_record=next_round,
        )
    )
    return SupersedeReviewResult(
        run_id=active.run_id,
        round_number=active.current_round,
        head_oid=active.current_head_oid or round_record.head_oid,
        actor=actor,
        cause=cause,
        reviewer_notice_sent=reviewer_notice_sent,
        reviewer_notice_error=reviewer_notice_error,
        late_result_id=late_result_id,
        cleanup_warnings=cleanup_warnings,
    )


def _apply_review_locked(
    repository: InitializedRepository,
    *,
    presented_result_id: str | None,
) -> ApplyReviewResult:
    status = runs.inspect_status_locked(
        repository.worktree.invocation_directory,
        validate_live_review_bundle=False,
    )
    active = status.active_run
    if active is None:
        raise ReviewApplicationError(
            "there is no active run with a review result to apply"
        )
    active_round = active.active_round
    marker: ReviewerLocalMarker | None = None
    selected_result_id = presented_result_id
    if selected_result_id is None:
        if active.phase is runs.RunPhase.REVIEWING:
            if active_round is None:
                raise ReviewApplicationError(
                    "reviewing state lost its active round"
                )
            marker = _load_active_result_marker(active_round)
            selected_result_id = marker.result_id
        elif active_round is not None:
            selected_result_id = active_round.result_id
    if selected_result_id is not None:
        historical = _historical_result_replay(
            repository,
            active,
            result_id=selected_result_id,
            next_action=status.next_action,
        )
        if historical is not None:
            return historical
    if active.phase is runs.RunPhase.APPROVED:
        if presented_result_id is not None:
            raise ReviewApplicationError(
                f"result ID {presented_result_id} is not the result that "
                "approved the active run; no state was changed"
            )
        raise ReviewApplicationError(
            "the approved result is missing from authoritative round history"
        )
    if active.phase is runs.RunPhase.IMPLEMENTING:
        if (
            active_round is not None
            and active_round.status is RoundStatus.APPLIED
            and active_round.result_id is not None
        ):
            if presented_result_id is not None:
                raise ReviewApplicationError(
                    f"result ID {presented_result_id} is not the result that "
                    "returned the active run to implementation; no state was "
                    "changed"
                )
            raise ReviewApplicationError(
                "the applied changes-requested result is missing from "
                "authoritative round history"
            )
        raise ReviewApplicationError(
            f"run {active.run_id} is in phase {active.phase.value}; "
            "apply-review requires an active reviewing round"
        )
    if active.phase is not runs.RunPhase.REVIEWING:
        raise ReviewApplicationError(
            f"run {active.run_id} is in phase {active.phase.value}; "
            "apply-review requires an active reviewing round"
        )
    if active_round is None:
        raise ReviewApplicationError(
            "reviewing state lost its active round before validation"
        )
    if marker is None:
        marker = _load_active_result_marker(active_round)
    if selected_result_id is None:
        selected_result_id = marker.result_id
    if selected_result_id != marker.result_id:
        raise ReviewApplicationError(
            f"result ID {selected_result_id} does not match the active "
            "Reviewer-local marker; no state was changed"
        )

    _validate_implementation_identity(repository, active)
    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    round_directory = (
        run_directory / "rounds" / f"{active.current_round:03d}"
    )
    try:
        evidence = load_marker_confirmed_review(
            active_round.review_worktree,
            authoritative_results_root=round_directory,
        )
    except (RetiredReviewIdentityError, ReviewEvidenceAccessError) as error:
        raise ReviewApplicationError(str(error)) from error
    except ReviewSubmissionError as error:
        return _classify_invalid_review(
            repository,
            active,
            result_id=selected_result_id,
            reason=f"review result failed independent validation: {error}",
        )
    authoritative_round = _load_active_review_records(
        repository,
        active,
    ).round_record
    try:
        _validate_authoritative_bundle_inputs(authoritative_round, evidence)
        _validate_evidence(repository, active, evidence)
    except ReviewApplicationError as error:
        return _classify_invalid_review(
            repository,
            active,
            result_id=selected_result_id,
            reason=str(error),
        )
    if evidence.review.result_id != selected_result_id:
        return _classify_invalid_review(
            repository,
            active,
            result_id=selected_result_id,
            reason=(
                "validated review result ID changed during application"
            ),
        )
    current_head = _current_head(
        repository.worktree.root,
        active.git_object_format,
        label="implementation",
    )
    if current_head != active.current_head_oid:
        return _classify_stale_review(
            repository,
            active,
            evidence=evidence,
            observed_head_oid=current_head,
        )
    budget_exhausted = (
        evidence.review.verdict is ReviewVerdict.CHANGES_REQUESTED
        and active.review_budget.completed_change_reviews + 1
        >= active.review_budget.effective_limit
    )
    requires_escalation = (
        budget_exhausted
        or evidence.review.verdict is ReviewVerdict.NEEDS_HUMAN
    )

    round_path = round_directory / "round.json"
    state_path = repository.control_root / runs.STATE_FILE_NAME
    run_path = run_directory / runs.RUN_RECORD_FILE_NAME
    state = runs.load_json_object(state_path, "authoritative state")
    run_record = runs.load_json_object(run_path, "active run record")
    round_data = runs.load_json_object(round_path, "active round record")
    original_state = state_path.read_bytes()
    try:
        round_record = ReviewRoundRecord.from_dict(
            round_data,
            label="active round record",
        )
    except ArtifactValidationError as error:
        raise ReviewApplicationError(str(error)) from error
    _assert_round_is_active(round_record, active)

    timestamp = (
        _next_history_timestamp(
            (
                evidence.request.created_at,
                *(item.record.created_at for item in active.escalations),
                *(item.record.created_at for item in active.resolutions),
            )
        )
        if requires_escalation
        else utc_timestamp()
    )
    bundle_manifest = _archive_bundle(round_directory, evidence)
    result_artifact = _write_immutable_artifact(
        round_directory / REVIEW_RESULT_FILE_NAME,
        evidence.review_bytes,
        path=REVIEW_RESULT_FILE_NAME,
    )
    markdown_artifact = _write_immutable_artifact(
        round_directory / REVIEW_MARKDOWN_FILE_NAME,
        evidence.review_markdown_bytes,
        path=REVIEW_MARKDOWN_FILE_NAME,
    )
    marker_artifact = _write_immutable_artifact(
        round_directory / REVIEW_MARKER_FILE_NAME,
        evidence.marker_bytes,
        path=REVIEW_MARKER_FILE_NAME,
    )
    approval: ApprovalRecord | None = None
    approval_artifact: BundleArtifact | None = None
    approval_path: Path | None = None
    if evidence.review.verdict is ReviewVerdict.APPROVED:
        approval_path = round_directory / APPROVAL_FILE_NAME
        approval_created_at = timestamp
        if os.path.lexists(approval_path):
            try:
                approval_created_at = ApprovalRecord.from_dict(
                    runs.load_json_object(
                        approval_path,
                        "existing approval artifact",
                    )
                ).created_at
            except (ArtifactValidationError, runs.RunStateError) as error:
                raise ReviewApplicationError(
                    f"existing approval artifact is invalid: "
                    f"{approval_path}: {error}"
                ) from error
        approval = ApprovalRecord(
            created_at=approval_created_at,
            run_id=active.run_id,
            round_number=active.current_round,
            request_id=evidence.request.request_id,
            result_id=evidence.review.result_id,
            task_sha256=active.task_sha256,
            object_format=active.git_object_format,
            base_oid=active.base_oid,
            head_oid=evidence.review.head_oid,
            reviewer_name=evidence.request.reviewer_name,
            reviewer_kind=evidence.request.reviewer_kind,
            review_sha256=hashlib.sha256(evidence.review_bytes).hexdigest(),
        )
        approval_artifact = _write_immutable_artifact(
            approval_path,
            encode_json(approval.to_dict()),
            path=APPROVAL_FILE_NAME,
        )

    next_phase = runs.RunPhase.APPROVED
    next_budget = active.review_budget
    next_action = "agent-squad complete"
    approved_head_oid: str | None = evidence.review.head_oid
    if evidence.review.verdict is ReviewVerdict.CHANGES_REQUESTED:
        completed_change_reviews = (
            active.review_budget.completed_change_reviews + 1
        )
        next_budget = replace(
            active.review_budget,
            completed_change_reviews=completed_change_reviews,
        )
        next_phase = runs.RunPhase.IMPLEMENTING
        next_action = runs.CORRECTION_SUBMIT_NEXT_ACTION
        approved_head_oid = None
    if requires_escalation:
        next_phase = runs.RunPhase.NEEDS_HUMAN
        next_action = runs.RESUME_NEXT_ACTION
        approved_head_oid = None

    next_round = replace(
        round_record,
        updated_at=timestamp,
        result_id=evidence.review.result_id,
        verdict=evidence.review.verdict,
        status=RoundStatus.APPLIED,
        review_result=result_artifact,
        review_markdown=markdown_artifact,
        review_marker=marker_artifact,
        approval=approval_artifact,
        bundle_archive=bundle_manifest,
    )
    next_active_round = replace(
        active_round,
        status=RoundStatus.APPLIED,
        result_id=evidence.review.result_id,
    )
    next_run = copy.deepcopy(run_record)
    next_run["phase"] = next_phase.value
    next_state = copy.deepcopy(state)
    next_state.update(
        updated_at=timestamp,
        phase=next_phase.value,
        approved_head_oid=approved_head_oid,
        active_escalation_id=None,
        active_round=next_active_round.to_dict(),
        review_budget=next_budget.to_dict(),
    )
    transition_records: list[_StagedRecord] = [
        _StagedRecord(
            path=round_path,
            original=round_path.read_bytes(),
            staged=encode_json(next_round.to_dict()),
        ),
        _StagedRecord(
            path=run_path,
            original=run_path.read_bytes(),
            staged=encode_json(next_run),
        ),
    ]
    escalation_directory: Path | None = None
    escalation_directory_created = False
    if requires_escalation:
        escalation_number = len(active.escalations) + 1
        escalation_id = str(uuid.uuid4())
        stem = f"{escalation_number:03d}-escalation"
        escalation_directory = run_directory / ESCALATIONS_DIRECTORY_NAME
        escalation_path = escalation_directory / f"{stem}.json"
        escalation_note_path = escalation_directory / f"{stem}.md"
        _require_new_artifact_path(
            escalation_path,
            "automatic escalation artifact",
        )
        _require_new_artifact_path(
            escalation_note_path,
            "automatic escalation note",
        )
        escalation_note = (
            "# Developer decision needed\n\n"
            f"{evidence.review.summary.strip()}\n"
        ).encode("utf-8")
        escalation = EscalationRecord(
            created_at=timestamp,
            escalation_id=escalation_id,
            run_id=active.run_id,
            round_number=active.current_round,
            head_oid=evidence.review.head_oid,
            related_finding_ids=tuple(
                finding.finding_id for finding in evidence.review.findings
                if not budget_exhausted or finding.blocking
            ),
            source_request_id=evidence.request.request_id,
            source_result_id=evidence.review.result_id,
            previous_approved_head_oid=None,
            previous_phase=runs.RunPhase.REVIEWING.value,
            actor=evidence.request.reviewer_name,
            note_path=escalation_note_path.name,
            note_sha256=hashlib.sha256(escalation_note).hexdigest(),
            response_id=None,
            reason=(
                EscalationReason.REVIEW_BUDGET_EXHAUSTED
                if budget_exhausted
                else EscalationReason.REVIEWER_NEEDS_HUMAN
            ),
        )
        try:
            escalation = EscalationRecord.from_dict(
                escalation.to_dict(),
                object_format=active.git_object_format,
                label="automatic Developer escalation",
            )
        except ArtifactValidationError as error:
            raise ReviewApplicationError(
                f"cannot build automatic Developer escalation: {error}"
            ) from error
        next_state["active_escalation_id"] = escalation_id
        transition_records[0:0] = [
            _StagedRecord(
                path=escalation_note_path,
                original=None,
                staged=escalation_note,
            ),
            _StagedRecord(
                path=escalation_path,
                original=None,
                staged=encode_json(escalation.to_dict()),
            ),
        ]
        review_event = _review_applied_event(
            timestamp=next_round.updated_at,
            run_id=active.run_id,
            round_number=active.current_round,
            request_id=evidence.request.request_id,
            result_id=evidence.review.result_id,
            verdict=evidence.review.verdict,
            head_oid=evidence.review.head_oid,
        )
        review_event["escalation_id"] = escalation_id
        event_path = run_directory / runs.EVENT_LOG_FILE_NAME
        original_events, staged_events = _stage_event_log(
            event_path,
            review_event,
            identity_fields=("event", "run_id", "round", "result_id"),
        )
        transition_records.append(
            _StagedRecord(
                path=event_path,
                original=original_events,
                staged=staged_events,
            )
        )
        escalation_directory_created = _ensure_history_directory(
            escalation_directory,
            "Developer escalation history",
        )
    try:
        _validate_implementation_identity(repository, active)
        if _current_head(
            repository.worktree.root,
            active.git_object_format,
            label="implementation",
        ) != current_head:
            raise ReviewApplicationError(
                "implementation HEAD changed while review artifacts were "
                "prepared; no result was applied"
            )
        _persist_authoritative_transition(
            records=tuple(transition_records),
            state_path=state_path,
            original_state=original_state,
            next_state=encode_json(next_state),
            failure_message="could not persist applied review state",
        )
    except BaseException:
        if escalation_directory_created and escalation_directory is not None:
            _remove_empty_directory(escalation_directory)
        raise
    if not requires_escalation:
        try:
            _ensure_event(
                run_directory / runs.EVENT_LOG_FILE_NAME,
                _review_applied_event(
                    timestamp=next_round.updated_at,
                    run_id=active.run_id,
                    round_number=active.current_round,
                    request_id=evidence.request.request_id,
                    result_id=evidence.review.result_id,
                    verdict=evidence.review.verdict,
                    head_oid=evidence.review.head_oid,
                ),
                identity_fields=("event", "run_id", "round", "result_id"),
            )
        except OSError as error:
            raise ReviewApplicationError(
                "the applied result is authoritative, but its event could "
                f"not be recorded: {error}"
            ) from error

    cleanup_warnings: tuple[str, ...] = ()
    if evidence.review.verdict in {
        ReviewVerdict.CHANGES_REQUESTED,
        ReviewVerdict.NEEDS_HUMAN,
    }:
        cleanup_warnings = _cleanup_review_resources(repository, active)
    return ApplyReviewResult(
        run_id=active.run_id,
        round_number=active.current_round,
        result_id=evidence.review.result_id,
        classification=RoundStatus.APPLIED,
        verdict=evidence.review.verdict,
        head_oid=evidence.review.head_oid,
        observed_head_oid=None,
        approval_path=approval_path,
        bundle_archive=round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME,
        diagnostic_path=None,
        reason=None,
        replayed=False,
        next_action=next_action,
        cleanup_warnings=cleanup_warnings,
    )


def _load_active_result_marker(
    active_round: ActiveRoundRecord,
) -> ReviewerLocalMarker:
    """Load the active marker far enough to establish result ownership."""

    marker_path = (
        active_round.review_worktree / REVIEW_DIRECTORY_NAME / MARKER_PATH
    )
    try:
        marker_status = marker_path.lstat()
        if not stat.S_ISREG(marker_status.st_mode):
            raise ReviewApplicationError(
                "the active review marker must be a regular non-symlink file"
            )
        marker_value = decode_json(marker_path.read_text(encoding="utf-8"))
        return ReviewerLocalMarker.from_dict(marker_value)
    except ReviewApplicationError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        InvalidJsonError,
        ArtifactValidationError,
    ) as error:
        raise ReviewApplicationError(
            "the active round has no valid marker-confirmed result to "
            f"apply: {error}"
        ) from error


def _classify_stale_review(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
    *,
    evidence: MarkerConfirmedReview,
    observed_head_oid: str,
) -> ApplyReviewResult:
    """Archive a valid result whose implementation revision has advanced."""

    records = _load_active_review_records(repository, active)
    reason = (
        f"implementation HEAD advanced from {records.round_record.head_oid} "
        f"to {observed_head_oid} before review application"
    )
    bundle_manifest = _archive_bundle(records.round_directory, evidence)
    result_artifact = _write_immutable_artifact(
        records.round_directory / REVIEW_RESULT_FILE_NAME,
        evidence.review_bytes,
        path=REVIEW_RESULT_FILE_NAME,
    )
    markdown_artifact = _write_immutable_artifact(
        records.round_directory / REVIEW_MARKDOWN_FILE_NAME,
        evidence.review_markdown_bytes,
        path=REVIEW_MARKDOWN_FILE_NAME,
    )
    marker_artifact = _write_immutable_artifact(
        records.round_directory / REVIEW_MARKER_FILE_NAME,
        evidence.marker_bytes,
        path=REVIEW_MARKER_FILE_NAME,
    )
    return _persist_review_classification(
        repository,
        active,
        records=records,
        classification=RoundStatus.STALE,
        result_id=evidence.review.result_id,
        verdict=evidence.review.verdict,
        reason=reason,
        observed_head_oid=observed_head_oid,
        diagnostic_id=None,
        review_result=result_artifact,
        review_markdown=markdown_artifact,
        review_marker=marker_artifact,
        bundle_archive=bundle_manifest,
    )


def _classify_invalid_review(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
    *,
    result_id: str,
    reason: str,
) -> ApplyReviewResult:
    """Preserve and classify invalid evidence owned by the active marker."""

    records = _load_active_review_records(repository, active)
    active_round = active.active_round
    if active_round is None:
        raise ReviewApplicationError(
            "reviewing state lost its active round before invalidation"
        )
    bundle_root = active_round.review_worktree / REVIEW_DIRECTORY_NAME
    submission_lock = bundle_root.joinpath(*SUBMISSION_LOCK_PATH.parts)
    try:
        with exclusive_file_lock(submission_lock):
            current_marker = _load_active_result_marker(active_round)
            if current_marker.result_id != result_id:
                raise ReviewApplicationError(
                    "the active review marker changed during validation; "
                    "no state was changed"
                )
            captured = _capture_invalid_review_evidence(bundle_root)
            diagnostic_id = archive_invalid_review_evidence(
                repository.control_root,
                run_id=active.run_id,
                round_number=active.current_round,
                request_id=active_round.request_id,
                reason=reason,
                captured=captured,
            )
    except (HandoffRecoveryError, OSError) as error:
        raise ReviewApplicationError(
            f"could not archive invalid review evidence: {error}"
        ) from error
    return _persist_review_classification(
        repository,
        active,
        records=records,
        classification=RoundStatus.INVALID,
        result_id=result_id,
        verdict=None,
        reason=reason,
        observed_head_oid=None,
        diagnostic_id=diagnostic_id,
        review_result=None,
        review_markdown=None,
        review_marker=None,
        bundle_archive=(),
    )


def _capture_invalid_review_evidence(
    bundle_root: Path,
) -> dict[str, bytes]:
    """Capture safe regular diagnostic files without following links."""

    candidates = (
        (
            REVIEW_RESULT_FILE_NAME,
            PurePosixPath("output") / REVIEW_RESULT_FILE_NAME,
        ),
        (
            REVIEW_MARKDOWN_FILE_NAME,
            PurePosixPath("output") / REVIEW_MARKDOWN_FILE_NAME,
        ),
        (MARKER_PATH.name, MARKER_PATH),
    )
    captured: dict[str, bytes] = {}
    for name, relative_path in candidates:
        path = bundle_root.joinpath(*relative_path.parts)
        try:
            path_status = path.lstat()
            if stat.S_ISREG(path_status.st_mode):
                captured[name] = path.read_bytes()
        except OSError:
            continue
    return captured


def _load_active_review_records(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
) -> _ActiveReviewRecords:
    """Load and bind every authoritative record for one classification."""

    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    round_directory = (
        run_directory / "rounds" / f"{active.current_round:03d}"
    )
    round_path = round_directory / "round.json"
    run_path = run_directory / runs.RUN_RECORD_FILE_NAME
    state_path = repository.control_root / runs.STATE_FILE_NAME
    original_round = round_path.read_bytes()
    original_run = run_path.read_bytes()
    original_state = state_path.read_bytes()
    round_data = runs.load_json_object(round_path, "active round record")
    run_record = runs.load_json_object(run_path, "active run record")
    state = runs.load_json_object(state_path, "authoritative state")
    if (
        round_path.read_bytes() != original_round
        or run_path.read_bytes() != original_run
        or state_path.read_bytes() != original_state
    ):
        raise ReviewApplicationError(
            "authoritative review state changed while it was being loaded"
        )
    try:
        round_record = ReviewRoundRecord.from_dict(
            round_data,
            label="active round record",
        )
    except ArtifactValidationError as error:
        raise ReviewApplicationError(str(error)) from error
    _assert_round_is_active(round_record, active)
    return _ActiveReviewRecords(
        run_directory=run_directory,
        round_directory=round_directory,
        round_path=round_path,
        run_path=run_path,
        state_path=state_path,
        event_path=run_directory / runs.EVENT_LOG_FILE_NAME,
        round_record=round_record,
        run_record=run_record,
        state=state,
        original_round=original_round,
        original_run=original_run,
        original_state=original_state,
    )


def _persist_review_classification(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
    *,
    records: _ActiveReviewRecords,
    classification: RoundStatus,
    result_id: str,
    verdict: ReviewVerdict | None,
    reason: str,
    observed_head_oid: str | None,
    diagnostic_id: str | None,
    review_result: BundleArtifact | None,
    review_markdown: BundleArtifact | None,
    review_marker: BundleArtifact | None,
    bundle_archive: tuple[BundleArtifact, ...],
) -> ApplyReviewResult:
    """Commit one stale or invalid transition without consuming budget."""

    if classification not in {RoundStatus.STALE, RoundStatus.INVALID}:
        raise ReviewApplicationError(
            "review classification must be stale or invalid"
        )
    active_round = active.active_round
    if active_round is None:
        raise ReviewApplicationError(
            "reviewing state lost its active round before classification"
        )
    timestamp = utc_timestamp()
    next_round = replace(
        records.round_record,
        updated_at=timestamp,
        result_id=result_id,
        verdict=verdict,
        status=classification,
        classification_reason=reason,
        diagnostic_id=diagnostic_id,
        observed_head_oid=observed_head_oid,
        review_result=review_result,
        review_markdown=review_markdown,
        review_marker=review_marker,
        approval=None,
        bundle_archive=bundle_archive,
    )
    next_active_round = replace(
        active_round,
        status=classification,
        result_id=result_id,
    )
    next_run = copy.deepcopy(records.run_record)
    next_run["phase"] = runs.RunPhase.IMPLEMENTING.value
    next_state = copy.deepcopy(records.state)
    next_state.update(
        updated_at=timestamp,
        phase=runs.RunPhase.IMPLEMENTING.value,
        approved_head_oid=None,
        active_round=next_active_round.to_dict(),
    )
    event = {
        "timestamp": timestamp,
        "event": f"review_{classification.value}",
        "run_id": active.run_id,
        "round": active.current_round,
        "request_id": active_round.request_id,
        "result_id": result_id,
        "head_oid": records.round_record.head_oid,
        "reason": reason,
    }
    if observed_head_oid is not None:
        event["observed_head_oid"] = observed_head_oid
    if diagnostic_id is not None:
        event["diagnostic_id"] = diagnostic_id
    original_events, staged_events = _stage_event_log(
        records.event_path,
        event,
        identity_fields=("event", "run_id", "round", "result_id"),
    )
    _validate_implementation_identity(repository, active)
    if observed_head_oid is not None and _current_head(
        repository.worktree.root,
        active.git_object_format,
        label="implementation",
    ) != observed_head_oid:
        raise ReviewApplicationError(
            "implementation HEAD changed while the stale result was being "
            "archived; no classification was applied"
        )
    _persist_authoritative_transition(
        records=(
            _StagedRecord(
                path=records.round_path,
                original=records.original_round,
                staged=encode_json(next_round.to_dict()),
            ),
            _StagedRecord(
                path=records.run_path,
                original=records.original_run,
                staged=encode_json(next_run),
            ),
            _StagedRecord(
                path=records.event_path,
                original=original_events,
                staged=staged_events,
            ),
        ),
        state_path=records.state_path,
        original_state=records.original_state,
        next_state=encode_json(next_state),
        failure_message=(
            f"could not persist {classification.value} review state"
        ),
    )
    cleanup_warnings = (
        _cleanup_review_resources(repository, active)
        if classification is RoundStatus.STALE
        else (
            "retained invalid-review worktree "
            f"{active_round.review_worktree} for diagnostic recovery",
        )
    )
    diagnostic_path = (
        None
        if diagnostic_id is None
        else records.round_directory.joinpath(
            *INVALID_RESULTS_DIRECTORY.parts,
        )
        / diagnostic_id
    )
    return ApplyReviewResult(
        run_id=active.run_id,
        round_number=active.current_round,
        result_id=result_id,
        classification=classification,
        verdict=verdict,
        head_oid=records.round_record.head_oid,
        observed_head_oid=observed_head_oid,
        approval_path=None,
        bundle_archive=(
            records.round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME
            if bundle_archive
            else None
        ),
        diagnostic_path=diagnostic_path,
        reason=reason,
        replayed=False,
        next_action="continue implementing the captured task",
        cleanup_warnings=cleanup_warnings,
    )


def _historical_result_replay(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
    *,
    result_id: str,
    next_action: str,
) -> ApplyReviewResult | None:
    """Return a recorded outcome without repeating authoritative effects."""

    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    matched = _find_recorded_review_round(
        active,
        run_directory,
        result_id=result_id,
    )
    if matched is None:
        return None
    round_directory, round_record = matched
    if round_record.status is RoundStatus.INVALID:
        diagnostic_id = round_record.diagnostic_id
        reason = round_record.classification_reason
        if diagnostic_id is None or reason is None:
            raise ReviewApplicationError(
                "historical invalid result is missing its diagnostic "
                "authority"
            )
        diagnostic_path = (
            round_directory.joinpath(*INVALID_RESULTS_DIRECTORY.parts)
            / diagnostic_id
        )
        try:
            validate_invalid_review_diagnostic(
                diagnostic_path,
                run_id=active.run_id,
                round_number=round_record.round_number,
                request_id=round_record.request_id,
                diagnostic_id=diagnostic_id,
                reason=reason,
            )
        except HandoffRecoveryError as error:
            raise ReviewApplicationError(str(error)) from error
        return ApplyReviewResult(
            run_id=active.run_id,
            round_number=round_record.round_number,
            result_id=result_id,
            classification=RoundStatus.INVALID,
            verdict=None,
            head_oid=round_record.head_oid,
            observed_head_oid=None,
            approval_path=None,
            bundle_archive=None,
            diagnostic_path=diagnostic_path,
            reason=reason,
            replayed=True,
            next_action=next_action,
            cleanup_warnings=(),
        )
    validator = runs.validate_applied_review_round
    if round_record.status is RoundStatus.STALE:
        validator = runs.validate_stale_review_round
    try:
        authority = validator(
            round_directory=round_directory,
            round_record=round_record,
            round_number=round_record.round_number,
        )
    except runs.RunStateError as error:
        raise ReviewApplicationError(str(error)) from error
    _verify_bundle_tree(
        round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME,
        round_record.bundle_archive,
        label="historical",
    )
    approval_path: Path | None = None
    if round_record.status is RoundStatus.STALE:
        if round_record.classification_reason is None or (
            round_record.observed_head_oid is None
        ):
            raise ReviewApplicationError(
                "historical stale result is missing its classification "
                "authority"
            )
    elif authority.review.verdict is ReviewVerdict.APPROVED:
        approval_artifact = round_record.approval
        if approval_artifact is None:
            raise ReviewApplicationError(
                "historical approved result is missing approval authority"
            )
        _verify_recorded_artifact(
            round_directory,
            approval_artifact,
            expected_path=APPROVAL_FILE_NAME,
            label="historical approval authority",
        )
        approval_path = round_directory / APPROVAL_FILE_NAME
    elif authority.review.verdict not in {
        ReviewVerdict.CHANGES_REQUESTED, ReviewVerdict.NEEDS_HUMAN,
    }:
        raise ReviewApplicationError(
            f"historical applied verdict {authority.review.verdict.value} "
            "is not supported by this command version"
        )
    if (
        round_record.status is RoundStatus.APPLIED
        and active.current_round == round_record.round_number
        and active.active_round is not None
        and active.active_round.result_id == result_id
    ):
        event = _review_applied_event(
            timestamp=round_record.updated_at,
            run_id=active.run_id,
            round_number=round_record.round_number,
            request_id=round_record.request_id,
            result_id=result_id,
            verdict=authority.review.verdict,
            head_oid=authority.review.head_oid,
        )
        for escalation in active.escalations:
            if (
                escalation.record.source_result_id == result_id
                and escalation.record.reason in {
                    EscalationReason.REVIEW_BUDGET_EXHAUSTED,
                    EscalationReason.REVIEWER_NEEDS_HUMAN,
                }
            ):
                event["escalation_id"] = escalation.record.escalation_id
        try:
            _ensure_event(
                run_directory / runs.EVENT_LOG_FILE_NAME,
                event,
                identity_fields=("event", "run_id", "round", "result_id"),
            )
        except OSError as error:
            raise ReviewApplicationError(
                "the applied result is authoritative, but its missing event "
                f"could not be recovered: {error}"
            ) from error
    return ApplyReviewResult(
        run_id=active.run_id,
        round_number=round_record.round_number,
        result_id=result_id,
        classification=round_record.status,
        verdict=authority.review.verdict,
        head_oid=authority.review.head_oid,
        observed_head_oid=round_record.observed_head_oid,
        approval_path=approval_path,
        bundle_archive=(
            round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME
        ),
        diagnostic_path=None,
        reason=round_record.classification_reason,
        replayed=True,
        next_action=next_action,
        cleanup_warnings=(),
    )


def _find_recorded_review_round(
    active: runs.ActiveRunStatus,
    run_directory: Path,
    *,
    result_id: str,
) -> tuple[Path, ReviewRoundRecord] | None:
    """Find a recorded result, translating run-state errors for callers."""

    try:
        return runs.find_recorded_review_round(
            run_directory=run_directory,
            run_id=active.run_id,
            current_round=active.current_round,
            base_oid=active.base_oid,
            object_format=active.git_object_format,
            result_id=result_id,
        )
    except runs.RunStateError as error:
        raise ReviewApplicationError(str(error)) from error


def _complete_run_locked(
    repository: InitializedRepository,
) -> CompleteRunResult:
    status = runs.inspect_status_locked(
        repository.worktree.invocation_directory
    )
    active = status.active_run
    if active is None:
        raise ReviewApplicationError("there is no active run to complete")
    if active.phase is not runs.RunPhase.APPROVED:
        raise ReviewApplicationError(
            f"run {active.run_id} is in phase {active.phase.value}; complete "
            "requires an approved run"
        )
    if active.approval is None:
        raise ReviewApplicationError(
            "approved state is missing its exact approval authority"
        )
    _validate_implementation_identity(repository, active)
    current_head = _current_head(
        repository.worktree.root,
        active.git_object_format,
        label="implementation",
    )
    if current_head != active.approved_head_oid:
        raise ReviewApplicationError(
            f"implementation HEAD is {current_head}, but approval is bound "
            f"to {active.approved_head_oid}; commit and review the newer "
            "revision or restore the exact approved head"
        )
    _validate_completion_cleanliness(
        repository,
        object_format=active.git_object_format,
    )

    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    run_path = run_directory / runs.RUN_RECORD_FILE_NAME
    state_path = repository.control_root / runs.STATE_FILE_NAME
    run_record = runs.load_json_object(run_path, "active run record")
    state = runs.load_json_object(state_path, "authoritative state")
    original_run = run_path.read_bytes()
    original_state = state_path.read_bytes()
    timestamp = utc_timestamp()
    next_run = copy.deepcopy(run_record)
    next_run.update(
        phase=runs.RunPhase.COMPLETED.value,
        finished_at=timestamp,
    )
    next_state = copy.deepcopy(state)
    next_state.update(
        updated_at=timestamp,
        active_run_id=None,
        phase=runs.RunPhase.COMPLETED.value,
        terminal_run_id=active.run_id,
    )
    _persist_authoritative_transition(
        records=(
            _StagedRecord(
                path=run_path,
                original=original_run,
                staged=encode_json(next_run),
            ),
        ),
        state_path=state_path,
        original_state=original_state,
        next_state=encode_json(next_state),
        failure_message="could not release the active-run slot",
    )
    warnings: list[str] = []
    try:
        _ensure_event(
            run_directory / runs.EVENT_LOG_FILE_NAME,
            {
                "timestamp": timestamp,
                "event": "run_completed",
                "run_id": active.run_id,
                "round": active.current_round,
                "result_id": active.approval.result_id,
                "approved_head_oid": active.approval.head_oid,
            },
            identity_fields=("event", "run_id"),
        )
    except OSError as error:
        warnings.append(
            "the run is completed, but its completion event could not be "
            f"recorded: {error}"
        )

    try:
        warnings.extend(_cleanup_review_resources(repository, active))
    except (AgentSquadError, OSError) as error:
        warnings.append(f"run completed, but review cleanup failed: {error}")
    return CompleteRunResult(
        run_id=active.run_id,
        head_oid=current_head,
        already_completed=False,
        cleanup_warnings=tuple(warnings),
    )


def _completed_replay(
    repository: InitializedRepository,
) -> CompleteRunResult | None:
    try:
        completed = runs.load_completed_run(repository)
    except runs.RunStateError as error:
        raise ReviewApplicationError(str(error)) from error
    if completed is None:
        return None
    replay_warnings: list[str] = []
    try:
        _ensure_event(
            completed.run_directory / runs.EVENT_LOG_FILE_NAME,
            {
                "timestamp": completed.finished_at,
                "event": "run_completed",
                "run_id": completed.run_id,
                "round": completed.round_number,
                "result_id": completed.result_id,
                "approved_head_oid": completed.approved_head_oid,
            },
            identity_fields=("event", "run_id"),
        )
    except OSError as error:
        replay_warnings.append(
            "the run is completed, but its missing completion event could "
            f"not be recovered: {error}"
        )
    return CompleteRunResult(
        run_id=completed.run_id,
        head_oid=completed.approved_head_oid,
        already_completed=True,
        cleanup_warnings=tuple(replay_warnings),
    )


def _validate_implementation_identity(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
) -> None:
    current = runs.repository_identity(repository.worktree)
    comparisons = (
        (
            current.implementation_root,
            active.repository.implementation_root,
            "implementation root",
        ),
        (
            current.git_common_dir,
            active.repository.git_common_dir,
            "Git common directory",
        ),
        (
            current.worktree_git_dir,
            active.repository.worktree_git_dir,
            "worktree Git directory",
        ),
        (
            current.repository_id,
            active.repository.repository_id,
            "repository ID",
        ),
        (
            current.start_branch_ref,
            active.repository.start_branch_ref,
            "branch identity",
        ),
        (
            current.start_head_detached,
            active.repository.start_head_detached,
            "detached-HEAD identity",
        ),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise ReviewApplicationError(
                f"current {label} does not match the active run"
            )


def _validate_evidence(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
    evidence: MarkerConfirmedReview,
) -> None:
    active_round = active.active_round
    if active_round is None:
        raise ReviewApplicationError("reviewing state has no active round")
    if evidence.worktree.root != active_round.review_worktree:
        raise ReviewApplicationError(
            "review evidence came from a different review worktree"
        )
    if (
        evidence.worktree.common_directory
        != active.repository.git_common_dir
    ):
        raise ReviewApplicationError(
            "review worktree belongs to a different Git repository"
        )
    request = evidence.request
    comparisons = (
        (request.run_id, active.run_id, "run ID"),
        (request.round_number, active.current_round, "round number"),
        (request.request_id, active_round.request_id, "request ID"),
        (request.mode, active_round.mode, "submission mode"),
        (request.object_format, active.git_object_format, "object format"),
        (request.base_oid, active.base_oid, "base OID"),
        (request.head_oid, active.current_head_oid, "head OID"),
        (request.task.sha256, active.task_sha256, "task digest"),
        (
            request.implementer_agent,
            active.implementer_agent,
            "Implementer identity",
        ),
        (
            request.implementer_kind,
            active.implementer_kind,
            "Implementer kind",
        ),
        (request.reviewer_kind, active.reviewer_kind, "Reviewer kind"),
        (
            request.reviewer_name,
            active_round.reviewer_name,
            "Reviewer session",
        ),
        (
            request.allowed_generated_paths,
            repository.configuration.allowed_generated_paths,
            "generated-path policy",
        ),
        (evidence.marker.request_id, request.request_id, "marker request ID"),
        (evidence.marker.result_id, evidence.review.result_id, "result ID"),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise ReviewApplicationError(
                f"review evidence {label} does not match authoritative state"
            )
    _validate_resolution_bundle_inputs(active, evidence)


def _validate_authoritative_bundle_inputs(
    round_record: ReviewRoundRecord,
    evidence: MarkerConfirmedReview,
) -> None:
    """Bind every captured authority input to the round manifest."""

    bundle_files = {item.path: item.content for item in evidence.bundle_files}
    # Compare input/request.json last: False sorts before True, so every
    # other input is checked first. If an input and its request-declared
    # digest were altered together, the error names that input. The request
    # is still compared, so it remains bound to the manifest too.
    ordered_artifacts = sorted(
        round_record.bundle_inputs,
        key=lambda artifact: artifact.path == REQUEST_PATH.as_posix(),
    )
    for artifact in ordered_artifacts:
        path = PurePosixPath(artifact.path)
        content = bundle_files.get(path)
        if content is None or (
            hashlib.sha256(content).hexdigest() != artifact.sha256
        ):
            raise ReviewApplicationError(
                "review bundle input does not match the authoritative round "
                f"digest: {path}"
            )


def _validate_resolution_bundle_inputs(
    active: runs.ActiveRunStatus,
    evidence: MarkerConfirmedReview,
) -> None:
    """Bind every reviewed resolution file to canonical run history."""

    request = evidence.request
    bundle_files = {item.path: item.content for item in evidence.bundle_files}
    resolutions_by_name = {
        authority.path.name: authority for authority in active.resolutions
    }
    request_created_at = datetime.fromisoformat(
        f"{request.created_at[:-1]}+00:00"
    )
    expected_paths = tuple(
        f"input/resolutions/{authority.path.name}"
        for authority in active.resolutions
        if datetime.fromisoformat(
            f"{authority.record.created_at[:-1]}+00:00"
        ) < request_created_at
    )
    if request.resolution_paths != expected_paths:
        raise ReviewApplicationError(
            "review bundle Developer resolutions do not match the "
            "authoritative run history"
        )
    for path_text in request.resolution_paths:
        record_path = PurePosixPath(path_text)
        authority = resolutions_by_name.get(record_path.name)
        record_bytes = bundle_files.get(record_path)
        if authority is None or record_bytes is None or hashlib.sha256(
            record_bytes
        ).digest() != hashlib.sha256(authority.record_bytes).digest():
            raise ReviewApplicationError(
                "review bundle Developer resolution record does not match "
                f"the authoritative run copy: {record_path}"
            )
        companion_path = record_path.parent / authority.companion_path.name
        companion_bytes = bundle_files.get(companion_path)
        if companion_bytes is None or hashlib.sha256(
            companion_bytes
        ).hexdigest() != authority.record.resolution_sha256:
            raise ReviewApplicationError(
                "review bundle Developer resolution companion does not "
                f"match the authoritative run copy: {companion_path}"
            )


def _assert_round_is_active(
    round_record: ReviewRoundRecord,
    active: runs.ActiveRunStatus,
) -> None:
    active_round = active.active_round
    if active_round is None:
        raise ReviewApplicationError("reviewing state has no active round")
    comparisons = (
        (round_record.run_id, active.run_id, "run ID"),
        (round_record.round_number, active.current_round, "round number"),
        (round_record.request_id, active_round.request_id, "request ID"),
        (round_record.result_id, None, "result ID"),
        (round_record.status, RoundStatus.REVIEWING, "status"),
        (round_record.base_oid, active.base_oid, "base OID"),
        (round_record.head_oid, active.current_head_oid, "head OID"),
        (
            round_record.reviewer_name,
            active_round.reviewer_name,
            "Reviewer name",
        ),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise ReviewApplicationError(
                f"active round record {label} changed during application"
            )


def _archive_bundle(
    round_directory: Path,
    evidence: MarkerConfirmedReview,
) -> tuple[BundleArtifact, ...]:
    live_files = {
        item.path: item.content for item in evidence.bundle_files
    }
    return _archive_bundle_files(
        round_directory,
        live_files,
        evidence=evidence,
    )


def _archive_bundle_files(
    round_directory: Path,
    live_files: dict[PurePosixPath, bytes],
    *,
    evidence: MarkerConfirmedReview | None = None,
) -> tuple[BundleArtifact, ...]:
    """Persist and verify one complete review-bundle snapshot."""

    archive_root = round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME
    if os.path.lexists(archive_root):
        try:
            return _validate_existing_bundle_archive(
                archive_root,
                live_files=live_files,
            )
        except ReviewApplicationError:
            if evidence is None or not _quarantine_retired_apply_attempt(
                round_directory,
                archive_root=archive_root,
                evidence=evidence,
            ):
                raise

    manifest = _bundle_manifest(live_files)
    staging = Path(
        tempfile.mkdtemp(prefix=".bundle.", dir=round_directory)
    )
    try:
        staging.chmod(0o700)
        for path, content in live_files.items():
            destination = staging.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            atomic_write(destination, content, mode=0o400)
        _verify_bundle_tree(staging, manifest, label="staged")
        staging.replace(archive_root)
        _make_archive_read_only(archive_root)
    except Exception:
        if os.path.lexists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        raise
    _verify_bundle_tree(archive_root, manifest, label="existing")
    return manifest


def _quarantine_retired_apply_attempt(
    round_directory: Path,
    *,
    archive_root: Path,
    evidence: MarkerConfirmedReview,
) -> bool:
    """Retire one failed apply snapshot only after identity retirement."""

    archived_files = read_regular_tree(
        archive_root,
        label="existing review bundle archive",
        error_type=ReviewApplicationError,
    )
    archived_review_bytes = archived_files.get(
        PurePosixPath("output") / REVIEW_RESULT_FILE_NAME
    )
    if archived_review_bytes is None:
        return False
    try:
        archived_review = ReviewResult.from_dict(
            decode_json(archived_review_bytes.decode("utf-8")),
            object_format=evidence.request.object_format,
        )
    except (
        UnicodeDecodeError,
        InvalidJsonError,
        ArtifactValidationError,
    ):
        return False
    identity = (
        (archived_review.run_id, evidence.request.run_id),
        (archived_review.round_number, evidence.request.round_number),
        (archived_review.request_id, evidence.request.request_id),
        (archived_review.base_oid, evidence.request.base_oid),
        (archived_review.head_oid, evidence.request.head_oid),
    )
    if any(actual != expected for actual, expected in identity):
        return False
    review_digest = hashlib.sha256(archived_review_bytes).hexdigest()
    try:
        retired_digest = retired_review_identity_digest(
            round_directory,
            request=evidence.request,
            result_id=archived_review.result_id,
        )
    except ReviewSubmissionError as error:
        raise ReviewApplicationError(
            "could not validate the retired provisional apply identity: "
            f"{error}"
        ) from error
    if retired_digest != review_digest:
        return False

    bundle_manifest = _bundle_manifest(archived_files)
    parent = _retired_apply_attempts_directory(round_directory)
    destination = parent / archived_review.result_id
    destination = _require_direct_subdirectory(
        destination,
        parent=parent,
        label="retired apply-attempt diagnostic",
        create=not os.path.lexists(destination),
    )
    _resume_retired_apply_artifact_moves(
        round_directory,
        destination=destination,
    )
    _verify_bundle_tree(
        archive_root,
        bundle_manifest,
        label="retired provisional",
    )
    try:
        archive_root.chmod(0o700)
        archive_root.replace(
            destination / BUNDLE_ARCHIVE_DIRECTORY_NAME
        )
        _make_archive_read_only(
            destination / BUNDLE_ARCHIVE_DIRECTORY_NAME
        )
        destination.chmod(0o500)
    except OSError as error:
        raise ReviewApplicationError(
            "could not quarantine the retired provisional bundle archive: "
            f"{error}"
        ) from error
    _verify_retired_apply_attempt(
        destination,
        bundle_manifest=bundle_manifest,
    )
    return True


def _retired_apply_attempts_directory(round_directory: Path) -> Path:
    """Return a local diagnostic parent without following linked entries."""

    try:
        round_root = round_directory.resolve(strict=True)
    except OSError as error:
        raise ReviewApplicationError(
            f"cannot resolve the active review round: {error}"
        ) from error
    diagnostics = _require_direct_subdirectory(
        round_root / "diagnostics",
        parent=round_root,
        label="review diagnostics",
        create=True,
    )
    return _require_direct_subdirectory(
        diagnostics / RETIRED_APPLY_ATTEMPTS_DIRECTORY_NAME,
        parent=diagnostics,
        label="retired apply-attempt diagnostics",
        create=True,
    )


def _require_direct_subdirectory(
    path: Path,
    *,
    parent: Path,
    label: str,
    create: bool,
) -> Path:
    """Create or validate one direct, non-symlink directory."""

    try:
        status = path.lstat()
    except FileNotFoundError:
        if not create:
            raise ReviewApplicationError(
                f"{label} must be a non-symlink directory: {path}"
            ) from None
        try:
            path.mkdir(mode=0o700)
            status = path.lstat()
        except OSError as error:
            raise ReviewApplicationError(
                f"cannot create {label} {path}: {error}"
            ) from error
    except OSError as error:
        raise ReviewApplicationError(
            f"cannot inspect {label} {path}: {error}"
        ) from error
    if not stat.S_ISDIR(status.st_mode):
        raise ReviewApplicationError(
            f"{label} must be a non-symlink directory: {path}"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ReviewApplicationError(
            f"cannot resolve {label} {path}: {error}"
        ) from error
    if resolved.parent != parent:
        raise ReviewApplicationError(
            f"{label} escaped its owned parent directory: {path}"
        )
    return resolved


def _resume_retired_apply_artifact_moves(
    round_directory: Path,
    *,
    destination: Path,
) -> None:
    """Resume atomic moves of round-root provisional apply artifacts."""

    allowed = set(_PROVISIONAL_APPLY_ARTIFACT_NAMES)
    try:
        entries = list(destination.iterdir())
    except OSError as error:
        raise ReviewApplicationError(
            f"cannot inspect retired apply-attempt diagnostic: {error}"
        ) from error
    for entry in entries:
        try:
            status = entry.lstat()
        except OSError as error:
            raise ReviewApplicationError(
                f"cannot inspect retired apply-attempt artifact: {error}"
            ) from error
        if entry.name not in allowed or not stat.S_ISREG(status.st_mode):
            raise ReviewApplicationError(
                "retired apply-attempt diagnostic contains an unexpected "
                f"entry: {entry}"
            )
    for name in _PROVISIONAL_APPLY_ARTIFACT_NAMES:
        source = round_directory / name
        target = destination / name
        if not os.path.lexists(source):
            continue
        if os.path.lexists(target):
            raise ReviewApplicationError(
                "provisional apply artifact exists in both active and "
                f"retired storage: {source}"
            )
        try:
            status = source.lstat()
        except OSError as error:
            raise ReviewApplicationError(
                f"cannot inspect provisional apply artifact {source}: {error}"
            ) from error
        if not stat.S_ISREG(status.st_mode):
            raise ReviewApplicationError(
                "provisional apply artifact must be a regular non-symlink "
                f"file: {source}"
            )
        try:
            source.replace(target)
        except OSError as error:
            raise ReviewApplicationError(
                f"cannot quarantine provisional apply artifact {source}: "
                f"{error}"
            ) from error


def _verify_retired_apply_attempt(
    root: Path,
    *,
    bundle_manifest: tuple[BundleArtifact, ...],
) -> None:
    """Verify one completed retired apply-attempt diagnostic."""

    files = read_regular_tree(
        root,
        label="retired apply-attempt diagnostic",
        error_type=ReviewApplicationError,
    )
    allowed_root_files = {
        PurePosixPath(name) for name in _PROVISIONAL_APPLY_ARTIFACT_NAMES
    }
    bundle_paths = {
        PurePosixPath(artifact.path): artifact.sha256
        for artifact in bundle_manifest
    }
    if not set(files).issubset(allowed_root_files | set(bundle_paths)):
        raise ReviewApplicationError(
            "retired apply-attempt diagnostic contains unexpected evidence"
        )
    if not set(bundle_paths).issubset(files):
        raise ReviewApplicationError(
            "retired apply-attempt diagnostic is missing bundle evidence"
        )
    for path, digest in bundle_paths.items():
        if hashlib.sha256(files[path]).hexdigest() != digest:
            raise ReviewApplicationError(
                f"retired apply-attempt digest mismatch for {path}"
            )


def _bundle_manifest(
    files: dict[PurePosixPath, bytes],
) -> tuple[BundleArtifact, ...]:
    return tuple(
        BundleArtifact(
            path=(
                PurePosixPath(BUNDLE_ARCHIVE_DIRECTORY_NAME) / path
            ).as_posix(),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        for path, content in sorted(
            files.items(),
            key=lambda item: str(item[0]),
        )
    )


def _validate_existing_bundle_archive(
    archive_root: Path,
    *,
    live_files: dict[PurePosixPath, bytes],
) -> tuple[BundleArtifact, ...]:
    archived_files = read_regular_tree(
        archive_root,
        label="existing review bundle archive",
        error_type=ReviewApplicationError,
    )
    required_live = {
        path: content
        for path, content in live_files.items()
        if path != RETIRED_RESULTS_PATH
    }
    required_archived = {
        path: content
        for path, content in archived_files.items()
        if path != RETIRED_RESULTS_PATH
    }
    if set(required_archived) != set(required_live):
        raise ReviewApplicationError(
            "existing review bundle archive does not match validated evidence"
        )
    for path, content in required_archived.items():
        if hashlib.sha256(content).digest() != hashlib.sha256(
            required_live[path]
        ).digest():
            raise ReviewApplicationError(
                f"existing review bundle digest mismatch for {path}"
            )
    manifest = _bundle_manifest(archived_files)
    _verify_bundle_tree(archive_root, manifest, label="existing")
    return manifest


def _verify_bundle_tree(
    root: Path,
    manifest: tuple[BundleArtifact, ...],
    *,
    label: str,
) -> None:
    expected = {
        PurePosixPath(*PurePosixPath(item.path).parts[1:]): item.sha256
        for item in manifest
    }
    actual = read_regular_tree(
        root,
        label=f"{label} review bundle archive",
        error_type=ReviewApplicationError,
    )
    if set(actual) != set(expected):
        raise ReviewApplicationError(
            f"{label} review bundle archive does not match validated evidence"
        )
    for path, content in actual.items():
        if hashlib.sha256(content).hexdigest() != expected[path]:
            raise ReviewApplicationError(
                f"{label} review bundle digest mismatch for {path}"
            )


def _make_archive_read_only(root: Path) -> None:
    for directory, _, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for name in filenames:
            (current / name).chmod(0o400)
        current.chmod(0o500)


def _write_immutable_artifact(
    destination: Path,
    content: bytes,
    *,
    path: str,
) -> BundleArtifact:
    digest = hashlib.sha256(content).hexdigest()
    if os.path.lexists(destination):
        try:
            status = destination.lstat()
            existing = destination.read_bytes()
        except OSError as error:
            raise ReviewApplicationError(
                f"cannot inspect existing immutable artifact {destination}: "
                f"{error}"
            ) from error
        if not stat.S_ISREG(status.st_mode) or existing != content:
            raise ReviewApplicationError(
                f"existing immutable artifact differs: {destination}"
            )
    else:
        atomic_write(destination, content, mode=0o400)
    return BundleArtifact(path=path, sha256=digest)


def _verify_recorded_artifact(
    round_directory: Path,
    artifact: BundleArtifact,
    *,
    expected_path: str,
    label: str,
) -> None:
    if artifact.path != expected_path:
        raise ReviewApplicationError(
            f"{label} path must be {expected_path}"
        )
    path = round_directory / expected_path
    if path.is_symlink() or not path.is_file():
        raise ReviewApplicationError(
            f"{label} must be a regular non-symlink file: {path}"
        )
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ReviewApplicationError(
            f"cannot read {label} {path}: {error}"
        ) from error
    if hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise ReviewApplicationError(
            f"{label} does not match its authoritative digest"
        )


def _persist_authoritative_transition(
    *,
    records: tuple[_StagedRecord, ...],
    state_path: Path,
    original_state: bytes,
    next_state: bytes,
    failure_message: str,
) -> None:
    """Commit metadata before state and safely roll back interruptions."""

    try:
        for record in records:
            atomic_write(record.path, record.staged, mode=0o600)
        atomic_write(state_path, next_state, mode=0o600)
    except BaseException as error:
        message: str | None = None
        try:
            current_state = state_path.read_bytes()
        except OSError as inspection_error:
            message = (
                f"{failure_message}: {error}; could not determine whether "
                f"authoritative state committed: {inspection_error}"
            )
        else:
            if current_state == next_state:
                if not isinstance(error, OSError):
                    raise
                message = (
                    f"{failure_message}: {error}; authoritative state was "
                    "already committed, so retry the command"
                )
            elif current_state != original_state:
                message = (
                    f"{failure_message}: {error}; authoritative state "
                    "changed unexpectedly, so staged metadata was left in "
                    "place"
                )
        if message is not None:
            if isinstance(error, OSError):
                raise ReviewApplicationError(message) from error
            error.add_note(message)
            raise

        rollback_errors: list[str] = []
        for record in reversed(records):
            try:
                current = record.path.read_bytes()
            except FileNotFoundError:
                if record.original is None:
                    continue
                rollback_errors.append(f"{record.path}: file disappeared")
                continue
            except OSError as inspection_error:
                rollback_errors.append(
                    f"{record.path}: {inspection_error}"
                )
                continue
            if record.original is not None and current == record.original:
                continue
            if current != record.staged:
                rollback_errors.append(f"{record.path}: content changed")
                continue
            try:
                if record.original is None:
                    record.path.unlink()
                else:
                    atomic_write(record.path, record.original, mode=0o600)
            except OSError as restore_error:
                rollback_errors.append(f"{record.path}: {restore_error}")
        detail = (
            "; rollback also failed for " + ", ".join(rollback_errors)
            if rollback_errors
            else ""
        )
        if not isinstance(error, OSError):
            if detail:
                error.add_note(detail.removeprefix("; "))
            raise
        raise ReviewApplicationError(
            f"{failure_message}: {error}{detail}"
        ) from error


def _validate_completion_cleanliness(
    repository: InitializedRepository,
    *,
    object_format: str,
) -> None:
    root = repository.worktree.root
    ignored = run_git(
        root,
        "check-ignore",
        "--quiet",
        "--no-index",
        "--",
        ".agent-squad/config.json",
    )
    if ignored.returncode != 0:
        raise ReviewApplicationError(
            "Agent Squad runtime files are not Git-excluded; run "
            "agent-squad init before complete"
        )
    tracked = run_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
        "--ignore-submodules=none",
    )
    if tracked.returncode != 0:
        detail = tracked.stderr.strip() or "unknown Git error"
        raise ReviewApplicationError(
            f"could not inspect tracked worktree cleanliness: {detail}"
        )
    if any(entry for entry in tracked.stdout.split("\0") if entry):
        raise ReviewApplicationError(
            "implementation worktree has uncommitted tracked changes; "
            "commit or restore them before complete"
        )
    try:
        verify_flagged_tracked_files(
            root,
            object_format,
        )
    except ReviewSubmissionError as error:
        raise ReviewApplicationError(
            f"implementation worktree tracked files are not clean: {error}"
        ) from error
    untracked = run_git(root, "ls-files", "--others", "-z", "--")
    if untracked.returncode != 0:
        detail = untracked.stderr.strip() or "unknown Git error"
        raise ReviewApplicationError(
            f"could not inspect untracked files: {detail}"
        )
    unexpected = [
        entry
        for entry in untracked.stdout.split("\0")
        if entry
        and not is_agent_squad_runtime_path(entry)
        and not matches_allowed_generated_path(
            entry,
            repository.configuration.allowed_generated_paths,
        )
    ]
    if unexpected:
        rendered = ", ".join(repr(path) for path in unexpected[:5])
        suffix = " ..." if len(unexpected) > 5 else ""
        raise ReviewApplicationError(
            "implementation worktree has unexpected untracked files: "
            f"{rendered}{suffix}; preserve them or configure only known "
            "generated paths before complete"
        )


def _capture_markdown(
    path: Path | None,
    *,
    invocation_directory: Path,
    label: str,
    default: bytes | None = None,
) -> _CapturedMarkdown:
    """Capture one non-empty UTF-8 Markdown input."""

    if path is None:
        if default is None:
            raise ReviewApplicationError(f"{label} is required")
        content = default
    else:
        content = _read_input_bytes(
            path,
            invocation_directory=invocation_directory,
            label=label,
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReviewApplicationError(
            f"{label} must contain UTF-8 Markdown"
        ) from error
    if not text.strip():
        raise ReviewApplicationError(
            f"{label} must contain non-whitespace text"
        )
    if "\x00" in text:
        raise ReviewApplicationError(f"{label} must not contain null bytes")
    return _CapturedMarkdown(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _read_input_bytes(
    path: Path,
    *,
    invocation_directory: Path,
    label: str,
) -> bytes:
    candidate = path if path.is_absolute() else invocation_directory / path
    try:
        resolved = candidate.resolve(strict=True)
        with resolved.open("rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ReviewApplicationError(
                    f"{label} must be a regular file: {resolved}"
                )
            return source.read()
    except ReviewApplicationError:
        raise
    except (OSError, RuntimeError) as error:
        raise ReviewApplicationError(
            f"cannot read {label} {candidate}: {error}"
        ) from error


def _read_authoritative_bytes(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ReviewApplicationError(
            f"{label} must be a regular non-symlink file: {path}"
        )
    try:
        return path.read_bytes()
    except OSError as error:
        raise ReviewApplicationError(
            f"cannot read {label} {path}: {error}"
        ) from error


def _capture_escalation_response(
    repository: InitializedRepository,
    *,
    active: runs.ActiveRunStatus,
    run_directory: Path,
    response_path: Path | None,
) -> _CapturedEscalationResponse | None:
    """Validate a response without making its canonical copy authoritative."""

    if response_path is None:
        return None
    if active.phase is not runs.RunPhase.IMPLEMENTING:
        raise ReviewApplicationError(
            "--response is accepted only while implementing after an "
            "applied changes_requested review"
        )
    try:
        previous = runs.find_latest_applied_review_before(
            run_directory=run_directory,
            run_id=active.run_id,
            current_round=active.current_round + 1,
            base_oid=active.base_oid,
            object_format=active.git_object_format,
        )
    except runs.RunStateError as error:
        raise ReviewApplicationError(str(error)) from error
    if previous is None or (
        previous.review.verdict is not ReviewVerdict.CHANGES_REQUESTED
    ):
        raise ReviewApplicationError(
            "--response requires an applied changes_requested review"
        )
    content = _read_input_bytes(
        response_path,
        invocation_directory=repository.worktree.invocation_directory,
        label="implementation response",
    )
    try:
        value = decode_json(content.decode("utf-8"))
        response = ReviewResponse.from_dict(
            value,
            object_format=active.git_object_format,
        )
        validate_review_response(
            response,
            previous.review,
            SubmissionMode.NEW_REVISION,
        )
    except (
        UnicodeDecodeError,
        InvalidJsonError,
        ArtifactValidationError,
    ) as error:
        raise ReviewApplicationError(
            f"implementation response failed validation: {error}"
        ) from error
    if response.supersedes_response_id is not None or response.resolution_ids:
        raise ReviewApplicationError(
            "an escalation-time response cannot replace an earlier response"
        )
    authority_path = previous.round_directory / ROUND_RESPONSE_FILE_NAME
    original: bytes | None = None
    already_referenced = any(
        authority.record.response_id is not None
        and authority.record.round_number
        == previous.round_record.round_number
        for authority in active.escalations
    )
    if os.path.lexists(authority_path):
        if authority_path.is_symlink() or not authority_path.is_file():
            raise ReviewApplicationError(
                "existing implementation response must be a regular "
                f"non-symlink file: {authority_path}"
            )
        try:
            original = authority_path.read_bytes()
        except OSError as error:
            raise ReviewApplicationError(
                f"cannot read existing implementation response "
                f"{authority_path}: {error}"
            ) from error
        if (
            previous.round_record.round_number < active.current_round
            or already_referenced
        ) and original != content:
            raise ReviewApplicationError(
                "the applied review already has an authoritative response; "
                "escalate cannot overwrite it"
            )
    elif already_referenced:
        raise ReviewApplicationError(
            "the applied review's escalation-referenced response is "
            "missing; escalate cannot replace it"
        )
    return _CapturedEscalationResponse(
        response=response,
        content=content,
        authority_path=authority_path,
        original=original,
    )


def _validated_finding_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ReviewApplicationError(
            "Developer resolution finding IDs must be a tuple"
        )
    for value in values:
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or "\x00" in value
        ):
            raise ReviewApplicationError(
                "Developer resolution finding IDs must be non-empty text "
                "without surrounding whitespace or null bytes"
            )
    if len(values) != len(set(values)):
        raise ReviewApplicationError(
            "Developer resolution finding IDs must not contain duplicates"
        )
    return values


def _next_history_timestamp(previous_values: tuple[str, ...]) -> str:
    now = datetime.now(timezone.utc)
    if previous_values:
        previous = max(
            datetime.fromisoformat(f"{value[:-1]}+00:00")
            for value in previous_values
        )
        if now <= previous:
            now = previous + timedelta(microseconds=1)
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ensure_history_directory(path: Path, label: str) -> bool:
    if path.is_symlink():
        raise ReviewApplicationError(f"{label} must not be a symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise ReviewApplicationError(
                f"{label} must be a directory: {path}"
            )
        return False
    try:
        path.mkdir(mode=0o700)
    except OSError as error:
        raise ReviewApplicationError(
            f"cannot create {label} {path}: {error}"
        ) from error
    return True


def _require_new_artifact_path(path: Path, label: str) -> None:
    if os.path.lexists(path):
        raise ReviewApplicationError(f"{label} already exists: {path}")


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _supersede_cause(value: str, *, command: str = "supersede") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewApplicationError(
            f"{command} --reason must contain non-whitespace text"
        )
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ReviewApplicationError(
            f"{command} --reason must be a single line without null bytes"
        )
    return value.strip()


def _run_cancelled_prompt(
    *,
    run_id: str,
    round_number: int,
    cause: str,
) -> str:
    return (
        "AGENT_SQUAD/0.4.4 RUN_CANCELLED\n\n"
        f"run_id: {run_id}\n"
        f"round: {round_number}\n"
        f"reason: {cause}\n\n"
        "The Agent Squad run has been cancelled.\n"
        "Do not continue or submit a current review result."
    )


def _review_superseded_prompt(
    *,
    run_id: str,
    round_number: int,
    cause: str,
) -> str:
    return (
        "AGENT_SQUAD/0.4.4 REVIEW_SUPERSEDED\n\n"
        f"run_id: {run_id}\n"
        f"round: {round_number}\n"
        f"reason: {cause}\n\n"
        "This round is no longer authoritative.\n"
        "Stop work when safe and do not submit it as the current result."
    )


def _review_escalated_prompt(
    *,
    run_id: str,
    round_number: int,
    escalation_id: str,
) -> str:
    return (
        "AGENT_SQUAD/0.4.4 REVIEW_ESCALATED\n"
        f"run_id={run_id}\n"
        f"round={round_number}\n"
        f"escalation_id={escalation_id}\n"
        "The review no longer controls the run. Pause work and preserve "
        "local evidence.\n"
    )


def _cleanup_superseded_review_resources(
    repository: InitializedRepository,
    *,
    active: runs.ActiveRunStatus,
    round_directory: Path,
    round_record: ReviewRoundRecord,
) -> tuple[str | None, tuple[str, ...]]:
    """Archive late evidence and remove only the superseded review worktree."""

    active_round = active.active_round
    if active_round is None:
        return None, (
            "superseded round did not retain its review-worktree identity",
        )
    review_worktree = active_round.review_worktree
    if not os.path.lexists(review_worktree):
        return None, _remove_missing_review_worktree_registration(
            repository,
            active=active,
            review_worktree=review_worktree,
        )
    bundle_root = review_worktree / REVIEW_DIRECTORY_NAME
    output_root = bundle_root / "output"
    late_result_id: str | None = None
    try:
        _require_cleanup_directory(bundle_root, "review bundle")
        _require_cleanup_directory(output_root, "review bundle output")
        lock_path = bundle_root.joinpath(*SUBMISSION_LOCK_PATH.parts)
        with exclusive_file_lock(lock_path):
            _validate_superseded_cleanup_target(
                repository,
                active=active,
                active_round=active_round,
                round_record=round_record,
            )
            evidence: MarkerConfirmedReview | None = None
            marker_path = bundle_root.joinpath(*MARKER_PATH.parts)
            if os.path.lexists(marker_path):
                try:
                    evidence = load_marker_confirmed_review(
                        review_worktree,
                        authoritative_results_root=round_directory,
                        lock_held=True,
                    )
                    _validate_evidence(repository, active, evidence)
                except AgentSquadError as error:
                    raise ReviewApplicationError(
                        "marker-confirmed late review evidence could not be "
                        f"validated: {error}"
                    ) from error
                late_result_id = _archive_late_review(
                    round_directory,
                    evidence,
                )
                _ensure_event(
                    round_directory.parents[1] / runs.EVENT_LOG_FILE_NAME,
                    {
                        "timestamp": utc_timestamp(),
                        "event": "late_review_archived",
                        "run_id": active.run_id,
                        "round": active.current_round,
                        "request_id": active_round.request_id,
                        "result_id": late_result_id,
                        "head_oid": evidence.review.head_oid,
                    },
                    identity_fields=(
                        "event",
                        "run_id",
                        "round",
                        "result_id",
                    ),
                )

            if evidence is None:
                bundle_files = read_regular_tree(
                    bundle_root,
                    label="superseded review bundle",
                    error_type=ReviewApplicationError,
                )
                bundle_files.pop(SUBMISSION_LOCK_PATH, None)
            else:
                bundle_files = {
                    item.path: item.content for item in evidence.bundle_files
                }
            bundle_manifest = _archive_bundle_files(
                round_directory,
                bundle_files,
            )
            archived_round = replace(
                round_record,
                bundle_archive=bundle_manifest,
            )
            atomic_write(
                round_directory / "round.json",
                encode_json(archived_round.to_dict()),
                mode=0o600,
            )

            try:
                shutil.rmtree(bundle_root)
            except OSError as error:
                raise ReviewApplicationError(
                    f"could not remove superseded review bundle "
                    f"{bundle_root}: {error}"
                ) from error

            return late_result_id, _remove_clean_review_worktree(
                repository,
                review_worktree,
                inspect_ignored=True,
                operational_failure_prefix=(
                    f"retained review worktree {review_worktree} because "
                    "safe superseded-round cleanup failed: "
                ),
            )
    except (AgentSquadError, OSError) as error:
        return late_result_id, (
            f"retained review worktree {review_worktree} because safe "
            f"superseded-round cleanup failed: {error}",
        )


def _remove_missing_review_worktree_registration(
    repository: InitializedRepository,
    *,
    active: runs.ActiveRunStatus,
    review_worktree: Path,
) -> tuple[str, ...]:
    """Unregister one exact review worktree whose directory is already gone."""

    try:
        resolved = review_worktree.resolve(strict=False)
        expected = review_worktree_path(
            repository,
            repository_id=active.repository.repository_id,
            run_id=active.run_id,
            round_number=active.current_round,
        )
    except (AgentSquadError, OSError, RuntimeError) as error:
        return (
            "could not validate missing review worktree registration for "
            f"{review_worktree}: {error}",
        )
    if resolved != expected:
        return (
            "refused to remove missing review worktree registration because "
            f"{review_worktree} does not match its deterministic path",
        )
    return _remove_review_worktree_registration(
        repository,
        review_worktree,
        failure_prefix=(
            "could not remove missing review worktree registration for "
            f"{review_worktree}: "
        ),
        require_success=True,
    )


def _remove_review_worktree_registration(
    repository: InitializedRepository,
    review_worktree: Path,
    *,
    failure_prefix: str,
    require_success: bool,
) -> tuple[str, ...]:
    """Remove one exact Git worktree registration and empty owned parents."""

    try:
        removed = run_git(
            repository.worktree.root,
            "worktree",
            "remove",
            str(review_worktree),
        )
    except AgentSquadError as error:
        return (f"{failure_prefix}{error}",)
    if removed.returncode != 0 and (
        require_success or os.path.lexists(review_worktree)
    ):
        detail = removed.stderr.strip() or "unknown Git error"
        return (f"{failure_prefix}{detail}",)
    _remove_empty_review_parents(
        review_worktree.parent,
        stop=repository.configuration.review_worktree_root,
    )
    return ()


def _validate_superseded_cleanup_target(
    repository: InitializedRepository,
    *,
    active: runs.ActiveRunStatus,
    active_round: ActiveRoundRecord,
    round_record: ReviewRoundRecord,
) -> None:
    review_worktree = active_round.review_worktree
    try:
        resolved = review_worktree.resolve(strict=True)
        expected = review_worktree_path(
            repository,
            repository_id=active.repository.repository_id,
            run_id=active.run_id,
            round_number=active.current_round,
        )
    except (AgentSquadError, OSError, RuntimeError) as error:
        raise ReviewApplicationError(
            f"cannot resolve superseded review-worktree identity: {error}"
        ) from error
    if resolved != expected:
        raise ReviewApplicationError(
            "superseded review worktree does not match its deterministic path"
        )
    worktree = discover_git_worktree(resolved)
    if worktree.root != resolved:
        raise ReviewApplicationError(
            "superseded cleanup target is not the review-worktree root"
        )
    if worktree.common_directory != active.repository.git_common_dir:
        raise ReviewApplicationError(
            "superseded review worktree belongs to a different repository"
        )
    if _current_head(
        worktree.root,
        active.git_object_format,
        label="superseded review",
    ) != round_record.head_oid:
        raise ReviewApplicationError(
            "superseded review worktree HEAD does not match its round"
        )
    branch = run_git(worktree.root, "symbolic-ref", "--quiet", "HEAD")
    if branch.returncode != 1:
        raise ReviewApplicationError(
            "superseded review cleanup requires the detached review worktree"
        )

    bundle_root = worktree.root / REVIEW_DIRECTORY_NAME
    for artifact in round_record.bundle_inputs:
        relative = PurePosixPath(artifact.path)
        path = bundle_root.joinpath(*relative.parts)
        try:
            status = path.lstat()
            content = path.read_bytes()
        except OSError as error:
            raise ReviewApplicationError(
                f"cannot validate superseded bundle input {path}: {error}"
            ) from error
        if not stat.S_ISREG(status.st_mode):
            raise ReviewApplicationError(
                "superseded bundle input must be a regular non-symlink "
                f"file: {path}"
            )
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ReviewApplicationError(
                f"superseded bundle input digest changed: {path}"
            )


def _require_cleanup_directory(path: Path, label: str) -> None:
    try:
        status = path.lstat()
    except OSError as error:
        raise ReviewApplicationError(
            f"cannot inspect {label} {path}: {error}"
        ) from error
    if not stat.S_ISDIR(status.st_mode):
        raise ReviewApplicationError(
            f"{label} must be a normal directory: {path}"
        )


def _archive_late_review(
    round_directory: Path,
    evidence: MarkerConfirmedReview,
) -> str:
    files = {
        PurePosixPath(REVIEW_RESULT_FILE_NAME): evidence.review_bytes,
        PurePosixPath(REVIEW_MARKDOWN_FILE_NAME): (
            evidence.review_markdown_bytes
        ),
        PurePosixPath(REVIEW_MARKER_FILE_NAME): evidence.marker_bytes,
    }
    late_root = round_directory
    for part in LATE_RESULTS_DIRECTORY.parts:
        late_root /= part
        if os.path.lexists(late_root):
            _require_cleanup_directory(late_root, "late-result archive")
        else:
            late_root.mkdir(mode=0o700)
    destination = late_root / evidence.review.result_id
    if os.path.lexists(destination):
        _verify_late_review_archive(destination, files)
        return evidence.review.result_id

    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=".late-result.", dir=late_root)
    )
    try:
        staging.chmod(0o700)
        for relative, content in files.items():
            atomic_write(
                staging.joinpath(*relative.parts),
                content,
                mode=0o400,
            )
        _verify_late_review_archive(staging, files)
        staging.replace(destination)
        staging = None
        destination.chmod(0o500)
        _verify_late_review_archive(destination, files)
    finally:
        if staging is not None and os.path.lexists(staging):
            shutil.rmtree(staging, ignore_errors=True)
    return evidence.review.result_id


def _verify_late_review_archive(
    root: Path,
    expected: dict[PurePosixPath, bytes],
) -> None:
    actual = read_regular_tree(
        root,
        label="late review archive",
        error_type=ReviewApplicationError,
    )
    if set(actual) != set(expected):
        raise ReviewApplicationError(
            "late review archive does not contain the expected diagnostics"
        )
    for path, content in actual.items():
        if hashlib.sha256(content).digest() != hashlib.sha256(
            expected[path]
        ).digest():
            raise ReviewApplicationError(
                f"late review archive digest mismatch for {path}"
            )


def _cleanup_review_resources(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
) -> tuple[str, ...]:
    active_round = active.active_round
    if active_round is None:
        return ("applied round did not retain its review-worktree identity",)
    review_worktree = active_round.review_worktree
    if not os.path.lexists(review_worktree):
        return ()
    try:
        run_directory = runs.safe_run_directory(
            repository.control_root,
            active.run_id,
        )
        evidence = load_marker_confirmed_review(
            review_worktree,
            authoritative_results_root=(
                run_directory
                / "rounds"
                / f"{active.current_round:03d}"
            ),
        )
        _validate_evidence(repository, active, evidence)
    except AgentSquadError as error:
        return (
            f"retained review worktree {review_worktree} because its "
            f"archived evidence could not be revalidated: {error}",
        )

    bundle_root = review_worktree / REVIEW_DIRECTORY_NAME
    try:
        shutil.rmtree(bundle_root)
    except OSError as error:
        return (
            f"could not remove archived review bundle {bundle_root}: {error}",
        )
    return _remove_clean_review_worktree(repository, review_worktree)


def _remove_clean_review_worktree(
    repository: InitializedRepository,
    review_worktree: Path,
    *,
    inspect_ignored: bool = False,
    operational_failure_prefix: str = "",
) -> tuple[str, ...]:
    """Remove scoped generated files, then a demonstrably clean worktree."""

    generated_warnings = tuple(
        warning
        for configured in repository.configuration.allowed_generated_paths
        if (
            warning := _remove_generated_path(
                review_worktree,
                configured,
            )
        )
        is not None
    )
    if generated_warnings:
        return generated_warnings
    try:
        cleanliness = run_git(
            review_worktree,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        )
    except AgentSquadError as error:
        return (
            f"{operational_failure_prefix}could not verify review worktree "
            f"cleanup for {review_worktree}: {error}",
        )
    if cleanliness.returncode != 0:
        detail = cleanliness.stderr.strip() or "unknown Git error"
        return (
            f"{operational_failure_prefix}could not verify review worktree "
            "cleanup for "
            f"{review_worktree}: {detail}",
        )
    if cleanliness.stdout:
        return (
            f"retained review worktree {review_worktree} because files "
            "remain after scoped cleanup",
        )
    if inspect_ignored:
        try:
            ignored = run_git(
                review_worktree,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
                "--",
            )
        except AgentSquadError as error:
            return (
                f"{operational_failure_prefix}could not inspect ignored "
                f"review-worktree files: {error}",
            )
        if ignored.returncode != 0:
            detail = ignored.stderr.strip() or "unknown Git error"
            return (
                f"{operational_failure_prefix}could not inspect ignored "
                f"review-worktree files: {detail}",
            )
        if any(entry for entry in ignored.stdout.split("\0") if entry):
            return (
                f"retained review worktree {review_worktree} because "
                "unconfigured ignored files remain after scoped cleanup",
            )
    return _remove_review_worktree_registration(
        repository,
        review_worktree,
        failure_prefix=(
            f"{operational_failure_prefix}could not remove review worktree "
            f"{review_worktree}: "
        ),
        require_success=False,
    )


def _remove_generated_path(
    review_worktree: Path,
    configured: str,
) -> str | None:
    relative = PurePosixPath(configured)
    current = review_worktree
    for part in relative.parts[:-1]:
        current /= part
        if not os.path.lexists(current):
            return None
        try:
            status = current.lstat()
        except OSError as error:
            return (
                f"could not inspect generated-path parent {current}: {error}"
            )
        if not stat.S_ISDIR(status.st_mode):
            return (
                "refused to traverse non-directory generated-path parent: "
                f"{current}"
            )
    target = review_worktree.joinpath(*relative.parts)
    if not os.path.lexists(target):
        return None
    tracked = run_git(
        review_worktree,
        "ls-files",
        "-z",
        "--",
        configured,
    )
    if tracked.returncode != 0 or tracked.stdout:
        return (
            "refused to remove generated path with tracked content: "
            f"{target}"
        )
    try:
        status = target.lstat()
        if stat.S_ISLNK(status.st_mode):
            return f"refused to remove symlinked generated path: {target}"
        if stat.S_ISDIR(status.st_mode):
            shutil.rmtree(target)
        elif stat.S_ISREG(status.st_mode):
            target.unlink()
        else:
            return f"refused to remove unusual generated path: {target}"
    except OSError as error:
        return f"could not remove generated path {target}: {error}"
    return None


def _remove_empty_review_parents(path: Path, *, stop: Path) -> None:
    try:
        boundary = stop.resolve(strict=False)
    except (OSError, RuntimeError):
        return
    current = path
    while current != boundary:
        try:
            current.rmdir()
        except OSError:
            return
        if boundary not in current.parents:
            return
        current = current.parent


def _review_applied_event(
    *,
    timestamp: str,
    run_id: str,
    round_number: int,
    request_id: str,
    result_id: str,
    verdict: ReviewVerdict,
    head_oid: str,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "event": "review_applied",
        "run_id": run_id,
        "round": round_number,
        "request_id": request_id,
        "result_id": result_id,
        "verdict": verdict.value,
        "head_oid": head_oid,
    }


def _ensure_event(
    path: Path,
    event: dict[str, object],
    *,
    identity_fields: tuple[str, ...],
) -> None:
    """Append one logical event unless the exact event already exists."""

    original, staged = _stage_event_log(
        path,
        event,
        identity_fields=identity_fields,
    )
    if staged != original:
        append_event(path, event)


def _stage_event_log(
    path: Path,
    event: dict[str, object],
    *,
    identity_fields: tuple[str, ...],
) -> tuple[bytes, bytes]:
    """Validate an event log and return its original and next contents."""

    if path.is_symlink() or not path.is_file():
        raise OSError(f"event log is not a regular non-symlink file: {path}")
    try:
        original = path.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise OSError(f"cannot inspect event log {path}: {error}") from error
    if original and not original.endswith(b"\n"):
        raise ReviewApplicationError(
            "event log must end with a newline before another event is added"
        )
    matches: list[dict[str, object]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        try:
            candidate = decode_json(line)
        except InvalidJsonError as error:
            raise ReviewApplicationError(
                f"event log line {index} contains invalid JSON: {error}"
            ) from error
        if not isinstance(candidate, dict):
            raise ReviewApplicationError(
                f"event log line {index} must be a JSON object"
            )
        if all(
            candidate.get(field) == event.get(field)
            for field in identity_fields
        ):
            matches.append(candidate)
    if len(matches) > 1:
        raise ReviewApplicationError(
            "event log contains duplicate logical transition events"
        )
    if matches:
        if matches[0] != event:
            raise ReviewApplicationError(
                "event log contains a conflicting logical transition event"
            )
        return original, original
    return original, original + encode_event(event)


def _current_head(root: Path, object_format: str, *, label: str) -> str:
    current_format = run_git(root, "rev-parse", "--show-object-format")
    if current_format.returncode != 0:
        detail = current_format.stderr.strip() or "unknown Git error"
        raise ReviewApplicationError(
            f"could not determine {label} Git object format: {detail}"
        )
    if current_format.stdout.strip() != object_format:
        raise ReviewApplicationError(
            f"current {label} Git object format does not match the run"
        )
    head = run_git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode != 0:
        detail = head.stderr.strip() or "unknown Git error"
        raise ReviewApplicationError(
            f"could not resolve {label} HEAD: {detail}"
        )
    value = head.stdout.strip()
    expected_length = 40 if object_format == "sha1" else 64
    if len(value) != expected_length or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ReviewApplicationError(
            f"{label} HEAD is not a full lowercase {object_format} object ID"
        )
    return value


def _optional_result_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        raise ReviewApplicationError(
            "--result-id must be a canonical UUID"
        ) from None
    if canonical != value:
        raise ReviewApplicationError(
            "--result-id must be a canonical UUID"
        )
    return canonical
