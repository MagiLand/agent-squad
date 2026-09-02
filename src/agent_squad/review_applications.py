"""Implementation-side application of reviews and approved completion."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
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
    REVIEW_MARKDOWN_FILE_NAME,
    REVIEW_MARKER_FILE_NAME,
    ReviewResult,
    ReviewRoundRecord,
    ReviewSupersession,
    ReviewVerdict,
    REVIEW_RESULT_FILE_NAME,
    RoundStatus,
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
    MarkerConfirmedReview,
    RETIRED_RESULTS_PATH,
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


BUNDLE_ARCHIVE_DIRECTORY_NAME = "bundle"
RETIRED_APPLY_ATTEMPTS_DIRECTORY_NAME = "retired-apply-attempts"
LATE_RESULTS_DIRECTORY = PurePosixPath("diagnostics/late-results")
_PROVISIONAL_APPLY_ARTIFACT_NAMES = (
    REVIEW_RESULT_FILE_NAME,
    REVIEW_MARKDOWN_FILE_NAME,
    REVIEW_MARKER_FILE_NAME,
    APPROVAL_FILE_NAME,
)


class ReviewApplicationError(AgentSquadError):
    """Raised when review evidence cannot be applied or completed safely."""


@dataclass(frozen=True)
class ApplyReviewResult:
    """Durable outcome of applying one review result."""

    run_id: str
    round_number: int
    result_id: str
    verdict: ReviewVerdict
    head_oid: str
    approval_path: Path | None
    bundle_archive: Path
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
class _StagedRecord:
    """One metadata record participating in an authoritative transition."""

    path: Path
    original: bytes
    staged: bytes


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


def _supersede_review_locked(
    repository: InitializedRepository,
    *,
    cause: str,
    herdr_client: HerdrClient | None,
) -> SupersedeReviewResult:
    status = runs.inspect_status_locked(
        repository.worktree.invocation_directory
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
        repository.worktree.invocation_directory
    )
    active = status.active_run
    if active is None:
        raise ReviewApplicationError(
            "there is no active run with a review result to apply"
        )
    active_result_id = (
        active.active_round.result_id
        if active.active_round is not None
        else None
    )
    if (
        presented_result_id is not None
        and presented_result_id != active_result_id
    ):
        historical = _historical_result_replay(
            repository,
            active,
            result_id=presented_result_id,
            next_action=status.next_action,
        )
        if historical is not None:
            return historical
    if active.phase is runs.RunPhase.APPROVED:
        return _approved_replay(
            repository,
            active,
            presented_result_id=presented_result_id,
        )
    if active.phase is runs.RunPhase.IMPLEMENTING:
        return _changes_requested_replay(
            repository,
            active,
            presented_result_id=presented_result_id,
        )
    if active.phase is not runs.RunPhase.REVIEWING:
        raise ReviewApplicationError(
            f"run {active.run_id} is in phase {active.phase.value}; "
            "apply-review requires an active reviewing round"
        )
    unapplied_review = active.unapplied_review
    if isinstance(unapplied_review, runs.InvalidUnappliedReviewResult):
        raise ReviewApplicationError(unapplied_review.reason)
    if unapplied_review is None:
        raise ReviewApplicationError(
            "the active round has no valid marker-confirmed result to "
            "apply"
        )
    ready = unapplied_review
    selected_result_id = presented_result_id or ready.result_id
    if selected_result_id != ready.result_id:
        raise ReviewApplicationError(
            f"result ID {selected_result_id} does not match the active "
            "Reviewer-local marker; no state was changed"
        )

    _validate_implementation_identity(repository, active)
    current_head = _current_head(
        repository.worktree.root,
        active.git_object_format,
        label="implementation",
    )
    if current_head != active.current_head_oid:
        raise ReviewApplicationError(
            f"implementation HEAD is {current_head}, expected the reviewed "
            f"head {active.current_head_oid}; no review result was applied"
        )
    active_round = active.active_round
    if active_round is None:
        raise ReviewApplicationError(
            "reviewing state lost its active round before validation"
        )
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
    except ReviewSubmissionError as error:
        raise ReviewApplicationError(
            f"review result failed independent validation: {error}"
        ) from error
    _validate_evidence(repository, active, evidence)
    if evidence.review.result_id != selected_result_id:
        raise ReviewApplicationError(
            "validated review result ID changed during application; no state "
            "was changed"
        )
    if evidence.review.verdict not in {
        ReviewVerdict.APPROVED,
        ReviewVerdict.CHANGES_REQUESTED,
    }:
        raise ReviewApplicationError(
            f"valid verdict {evidence.review.verdict.value} is not supported "
            "by this command version; no state was changed"
        )
    if (
        evidence.review.verdict is ReviewVerdict.CHANGES_REQUESTED
        and active.review_budget.completed_change_reviews + 1
        >= active.review_budget.effective_limit
    ):
        raise ReviewApplicationError(
            "review budget exhaustion is not supported by this command "
            "version; no state was changed"
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

    timestamp = utc_timestamp()
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
        active_round=next_active_round.to_dict(),
        review_budget=next_budget.to_dict(),
    )
    _validate_implementation_identity(repository, active)
    if _current_head(
        repository.worktree.root,
        active.git_object_format,
        label="implementation",
    ) != current_head:
        raise ReviewApplicationError(
            "implementation HEAD changed while review artifacts were being "
            "prepared; no result was applied"
        )
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
        ),
        state_path=state_path,
        original_state=original_state,
        next_state=encode_json(next_state),
        failure_message="could not persist applied review state",
    )
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
            "the applied result is authoritative, but its event could not "
            f"be recorded: {error}"
        ) from error

    cleanup_warnings: tuple[str, ...] = ()
    if evidence.review.verdict is ReviewVerdict.CHANGES_REQUESTED:
        cleanup_warnings = _cleanup_review_resources(repository, active)
    return ApplyReviewResult(
        run_id=active.run_id,
        round_number=active.current_round,
        result_id=evidence.review.result_id,
        verdict=evidence.review.verdict,
        head_oid=evidence.review.head_oid,
        approval_path=approval_path,
        bundle_archive=round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME,
        replayed=False,
        next_action=next_action,
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
    if round_record.status is not RoundStatus.APPLIED:
        raise ReviewApplicationError(
            f"result ID {result_id} was previously classified "
            f"{round_record.status.value} in round "
            f"{round_record.round_number}; no state was changed"
        )
    try:
        authority = runs.validate_applied_review_round(
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
    if authority.review.verdict is ReviewVerdict.APPROVED:
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
    elif authority.review.verdict is not ReviewVerdict.CHANGES_REQUESTED:
        raise ReviewApplicationError(
            f"historical applied verdict {authority.review.verdict.value} "
            "is not supported by this command version"
        )
    return ApplyReviewResult(
        run_id=active.run_id,
        round_number=round_record.round_number,
        result_id=result_id,
        verdict=authority.review.verdict,
        head_oid=authority.review.head_oid,
        approval_path=approval_path,
        bundle_archive=(
            round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME
        ),
        replayed=True,
        next_action=next_action,
        cleanup_warnings=(),
    )


def _approved_replay(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
    *,
    presented_result_id: str | None,
) -> ApplyReviewResult:
    active_round = active.active_round
    approval = active.approval
    if active_round is None or active_round.result_id is None or (
        approval is None
    ):
        raise ReviewApplicationError(
            "approved state is missing its applied result authority"
        )
    if presented_result_id is not None and (
        presented_result_id != active_round.result_id
    ):
        raise ReviewApplicationError(
            f"result ID {presented_result_id} is not the result that approved "
            "the active run; no state was changed"
        )
    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    matched = _find_recorded_review_round(
        active,
        run_directory,
        result_id=active_round.result_id,
    )
    if matched is None:
        raise ReviewApplicationError(
            "the approved result is missing from authoritative round history"
        )
    round_directory, round_record = matched
    comparisons = (
        (round_record.round_number, active.current_round, "round number"),
        (round_record.request_id, approval.request_id, "request ID"),
        (round_record.result_id, approval.result_id, "result ID"),
        (round_record.head_oid, approval.head_oid, "head OID"),
        (round_record.status, RoundStatus.APPLIED, "status"),
        (round_record.verdict, ReviewVerdict.APPROVED, "verdict"),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise ReviewApplicationError(
                f"approved round {label} does not match authoritative state"
            )
    try:
        _ensure_event(
            run_directory / runs.EVENT_LOG_FILE_NAME,
            _review_applied_event(
                timestamp=round_record.updated_at,
                run_id=approval.run_id,
                round_number=approval.round_number,
                request_id=approval.request_id,
                result_id=approval.result_id,
                verdict=ReviewVerdict.APPROVED,
                head_oid=approval.head_oid,
            ),
            identity_fields=("event", "run_id", "round", "result_id"),
        )
    except OSError as error:
        raise ReviewApplicationError(
            "the approval is authoritative, but its missing event could not "
            f"be recovered: {error}"
        ) from error
    return ApplyReviewResult(
        run_id=active.run_id,
        round_number=active.current_round,
        result_id=active_round.result_id,
        verdict=ReviewVerdict.APPROVED,
        head_oid=approval.head_oid,
        approval_path=round_directory / APPROVAL_FILE_NAME,
        bundle_archive=round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME,
        replayed=True,
        next_action="agent-squad complete",
        cleanup_warnings=(),
    )


def _changes_requested_replay(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
    *,
    presented_result_id: str | None,
) -> ApplyReviewResult:
    active_round = active.active_round
    if (
        active_round is None
        or active_round.status is not RoundStatus.APPLIED
        or active_round.result_id is None
    ):
        raise ReviewApplicationError(
            f"run {active.run_id} is in phase {active.phase.value}; "
            "apply-review requires an active reviewing round"
        )
    if presented_result_id is not None and (
        presented_result_id != active_round.result_id
    ):
        raise ReviewApplicationError(
            f"result ID {presented_result_id} is not the result that returned "
            "the active run to implementation; no state was changed"
        )
    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    matched = _find_recorded_review_round(
        active,
        run_directory,
        result_id=active_round.result_id,
    )
    if matched is None:
        raise ReviewApplicationError(
            "the applied changes-requested result is missing from "
            "authoritative round history"
        )
    round_directory, round_record = matched
    comparisons = (
        (round_record.round_number, active.current_round, "round number"),
        (round_record.request_id, active_round.request_id, "request ID"),
        (round_record.result_id, active_round.result_id, "result ID"),
        (round_record.head_oid, active.current_head_oid, "head OID"),
        (round_record.status, RoundStatus.APPLIED, "status"),
        (
            round_record.verdict,
            ReviewVerdict.CHANGES_REQUESTED,
            "verdict",
        ),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise ReviewApplicationError(
                f"applied changes-requested round {label} does not match "
                "authoritative state"
            )
    try:
        authority = runs.validate_applied_review_round(
            round_directory=round_directory,
            round_record=round_record,
            round_number=active.current_round,
        )
    except runs.RunStateError as error:
        raise ReviewApplicationError(str(error)) from error
    review = authority.review
    if (
        review.request_id != active_round.request_id
        or review.result_id != active_round.result_id
        or review.verdict is not ReviewVerdict.CHANGES_REQUESTED
    ):
        raise ReviewApplicationError(
            "applied review result does not match authoritative state"
        )
    _verify_bundle_tree(
        round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME,
        round_record.bundle_archive,
        label="existing",
    )
    try:
        _ensure_event(
            run_directory / runs.EVENT_LOG_FILE_NAME,
            _review_applied_event(
                timestamp=round_record.updated_at,
                run_id=active.run_id,
                round_number=active.current_round,
                request_id=active_round.request_id,
                result_id=active_round.result_id,
                verdict=ReviewVerdict.CHANGES_REQUESTED,
                head_oid=review.head_oid,
            ),
            identity_fields=("event", "run_id", "round", "result_id"),
        )
    except OSError as error:
        raise ReviewApplicationError(
            "the changes-requested result is authoritative, but its missing "
            f"event could not be recovered: {error}"
        ) from error
    cleanup_warnings = _cleanup_review_resources(repository, active)
    return ApplyReviewResult(
        run_id=active.run_id,
        round_number=active.current_round,
        result_id=active_round.result_id,
        verdict=ReviewVerdict.CHANGES_REQUESTED,
        head_oid=review.head_oid,
        approval_path=None,
        bundle_archive=round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME,
        replayed=True,
        next_action=runs.CORRECTION_SUBMIT_NEXT_ACTION,
        cleanup_warnings=cleanup_warnings,
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

    warnings.extend(_cleanup_review_resources(repository, active))
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
            except OSError as inspection_error:
                rollback_errors.append(
                    f"{record.path}: {inspection_error}"
                )
                continue
            if current == record.original:
                continue
            if current != record.staged:
                rollback_errors.append(f"{record.path}: content changed")
                continue
            try:
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


def _supersede_cause(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewApplicationError(
            "supersede --reason must contain non-whitespace text"
        )
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ReviewApplicationError(
            "supersede --reason must be a single line without null bytes"
        )
    return value.strip()


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
        return None, ()
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
        review_root = repository.configuration.review_worktree_root.resolve(
            strict=False
        )
    except (OSError, RuntimeError) as error:
        raise ReviewApplicationError(
            f"cannot resolve superseded review-worktree identity: {error}"
        ) from error
    expected = (
        review_root
        / active.repository.repository_id
        / active.run_id
        / f"round-{active.current_round:03d}"
    )
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
            f"retained review worktree {review_worktree} because its applied "
            f"evidence could not be revalidated: {error}",
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
    cleanliness = run_git(
        review_worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
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
        ignored = run_git(
            review_worktree,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
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
    removed = run_git(
        repository.worktree.root,
        "worktree",
        "remove",
        str(review_worktree),
    )
    if removed.returncode != 0 and os.path.lexists(review_worktree):
        detail = removed.stderr.strip() or "unknown Git error"
        return (
            f"{operational_failure_prefix}could not remove review worktree "
            f"{review_worktree}: {detail}",
        )
    _remove_empty_review_parents(
        review_worktree.parent,
        stop=repository.configuration.review_worktree_root,
    )
    return ()


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
