"""Delivery and idempotent recovery of durable review handoffs."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from . import runs
from .artifacts import (
    ActiveRoundRecord,
    ArtifactValidationError,
    HandoffRecord,
    HandoffStatus,
    ReviewRequest,
)
from .herdr import (
    HerdrClient,
    HerdrError,
    HerdrInstallation,
    format_herdr_error,
)
from .initialization import (
    AgentSquadError,
    REVIEW_DIRECTORY_NAME,
    load_initialized_repository,
)
from .storage import (
    append_event,
    atomic_write,
    encode_json,
    exclusive_file_lock,
    utc_timestamp,
)


class HandoffRecoveryError(AgentSquadError):
    """Raised when the active review handoff cannot be recovered safely."""


class HandoffRecoveryAction(StrEnum):
    """Observable way in which one logical handoff was recovered."""

    ADOPTED = "adopted"
    REPROMPTED = "reprompted"
    RELAUNCHED = "relaunched"
    RESULT_READY = "result_ready"


@dataclass(frozen=True)
class RetryHandoffResult:
    """Durable outcome of one review-request recovery attempt."""

    run_id: str
    round_number: int
    request_id: str
    reviewer_name: str
    handoff_status: HandoffStatus
    handoff_error: str | None
    action: HandoffRecoveryAction | None
    result_id: str | None


def review_request_handoff_record(
    *,
    round_number: int,
    target: str,
    status: HandoffStatus,
    timestamp: str,
    error: str | None,
    installation: HerdrInstallation | None,
) -> HandoffRecord:
    """Build the canonical durable state for one request handoff."""

    return HandoffRecord(
        round_number=round_number,
        status=status,
        target=target,
        last_error=error,
        updated_at=timestamp,
        herdr_version=(
            installation.version if installation is not None else None
        ),
        herdr_protocol=(
            installation.protocol if installation is not None else None
        ),
    )


def record_review_request_handoff(
    control_root: Path,
    *,
    run_id: str,
    round_number: int,
    request_id: str,
    target: str,
    status: HandoffStatus,
    error: str | None,
    installation: HerdrInstallation | None,
    event_name: str,
    error_type: type[AgentSquadError],
    extra_event_fields: Mapping[str, object] | None = None,
) -> None:
    """Persist one request handoff and its ordered event record."""

    timestamp = utc_timestamp()
    handoff = review_request_handoff_record(
        round_number=round_number,
        target=target,
        status=status,
        timestamp=timestamp,
        error=error,
        installation=installation,
    )
    state_path = control_root / runs.STATE_FILE_NAME
    state = runs.load_json_object(state_path, "authoritative state")
    if state.get("active_run_id") != run_id:
        raise error_type(
            "active run changed before the review handoff was recorded"
        )
    state_round = state.get("active_round")
    if not isinstance(state_round, dict) or (
        state_round.get("request_id") != request_id
    ):
        raise error_type(
            "active round changed before the review handoff was recorded"
        )

    next_state = copy.deepcopy(state)
    next_state["updated_at"] = timestamp
    next_state["handoff"] = handoff.to_dict()
    try:
        atomic_write(state_path, encode_json(next_state), mode=0o600)
    except OSError as write_error:
        raise error_type(
            f"could not record review handoff state: {write_error}"
        ) from write_error

    event: dict[str, object] = {
        "timestamp": timestamp,
        "event": event_name,
        "run_id": run_id,
        "round": round_number,
        "request_id": request_id,
        "target": target,
        "error": error,
    }
    if extra_event_fields is not None:
        event.update(extra_event_fields)
    try:
        append_event(
            control_root
            / runs.RUNS_DIRECTORY_NAME
            / run_id
            / runs.EVENT_LOG_FILE_NAME,
            event,
        )
    except OSError as event_error:
        raise error_type(
            "the handoff state is durable, but its event could not be "
            f"recorded: {event_error}"
        ) from event_error


def retry_handoff(
    start: Path,
    *,
    herdr_client: HerdrClient | None = None,
) -> RetryHandoffResult:
    """Probe and redrive the current review request without a new round."""

    repository = load_initialized_repository(start)
    lock_path = repository.control_root / runs.LOCK_FILE_NAME
    try:
        with exclusive_file_lock(lock_path):
            status = runs.inspect_status_locked(
                repository.worktree.invocation_directory
            )
            active = status.active_run
            if active is None:
                raise HandoffRecoveryError(
                    "there is no active review handoff to retry"
                )
            if active.phase is not runs.RunPhase.REVIEWING:
                raise HandoffRecoveryError(
                    "retry-handoff requires an active reviewing round"
                )
            active_round = active.active_round
            handoff = active.handoff
            if active_round is None or handoff is None:
                raise HandoffRecoveryError(
                    "the active reviewing round has no durable handoff"
                )
            if isinstance(
                active.unapplied_review,
                runs.UnappliedReviewResult,
            ):
                return RetryHandoffResult(
                    run_id=active.run_id,
                    round_number=active_round.round_number,
                    request_id=active_round.request_id,
                    reviewer_name=active_round.reviewer_name,
                    handoff_status=handoff.status,
                    handoff_error=handoff.last_error,
                    action=HandoffRecoveryAction.RESULT_READY,
                    result_id=active.unapplied_review.result_id,
                )
            if not active.review_worktree_available:
                raise HandoffRecoveryError(
                    "the expected review worktree is unavailable; refusing "
                    "to create replacement review authority"
                )

            request = _load_active_request(
                repository.control_root,
                run_id=active.run_id,
                active_round=active_round,
            )
            prompt = format_review_request_prompt(
                request,
                active_round.review_worktree,
            )
            client = herdr_client or HerdrClient(repository.worktree.root)
            installation: HerdrInstallation | None = None
            try:
                installation = client.discover(
                    active.reviewer_kind,
                    role="Reviewer",
                )
                probe = client.probe_review_request(
                    reviewer_name=active_round.reviewer_name,
                    reviewer_kind=active.reviewer_kind,
                    review_worktree=active_round.review_worktree,
                )
                if (
                    probe.history is not None
                    and prompt in probe.history
                    and handoff.status is not HandoffStatus.SENT
                ):
                    action = HandoffRecoveryAction.ADOPTED
                else:
                    session = client.dispatch_review_request(
                        reviewer_name=active_round.reviewer_name,
                        reviewer_kind=active.reviewer_kind,
                        start_args=active.reviewer_start_args,
                        review_worktree=active_round.review_worktree,
                        prompt=prompt,
                    )
                    action = (
                        HandoffRecoveryAction.REPROMPTED
                        if session.adopted
                        else HandoffRecoveryAction.RELAUNCHED
                    )
            except HerdrError as error:
                detail = format_herdr_error(str(error))
                _record_handoff(
                    repository.control_root,
                    active=active,
                    active_round=active_round,
                    status=HandoffStatus.FAILED,
                    error=detail,
                    installation=installation,
                    action=None,
                )
                return _recovery_result(
                    active,
                    active_round,
                    status=HandoffStatus.FAILED,
                    error=detail,
                    action=None,
                )

            _record_handoff(
                repository.control_root,
                active=active,
                active_round=active_round,
                status=HandoffStatus.SENT,
                error=None,
                installation=installation,
                action=action,
            )
            return _recovery_result(
                active,
                active_round,
                status=HandoffStatus.SENT,
                error=None,
                action=action,
            )
    except AgentSquadError:
        raise
    except OSError as error:
        raise HandoffRecoveryError(
            f"could not acquire or use the local recovery lock "
            f"{lock_path}: {error}"
        ) from error


def _record_handoff(
    control_root: Path,
    *,
    active: runs.ActiveRunStatus,
    active_round: ActiveRoundRecord,
    status: HandoffStatus,
    error: str | None,
    installation: HerdrInstallation | None,
    action: HandoffRecoveryAction | None,
) -> None:
    """Persist one recovery attempt against the unchanged logical request."""

    record_review_request_handoff(
        control_root,
        run_id=active.run_id,
        round_number=active_round.round_number,
        request_id=active_round.request_id,
        target=active_round.reviewer_name,
        status=status,
        error=error,
        installation=installation,
        event_name=(
            "review_request_recovered"
            if status is HandoffStatus.SENT
            else "review_request_recovery_failed"
        ),
        error_type=HandoffRecoveryError,
        extra_event_fields={
            "action": action.value if action is not None else None,
        },
    )


def format_review_request_prompt(
    request: ReviewRequest,
    review_worktree: Path,
) -> str:
    """Return the canonical short control prompt for one review request."""

    request_path = (
        review_worktree / REVIEW_DIRECTORY_NAME / "input" / "request.json"
    )
    return (
        "AGENT_SQUAD/0.4.4 REVIEW_REQUEST\n\n"
        f"run_id: {request.run_id}\n"
        f"round: {request.round_number}\n"
        f"request_id: {request.request_id}\n"
        f"base_oid: {request.base_oid}\n"
        f"head_oid: {request.head_oid}\n"
        f"review_worktree: {review_worktree}\n"
        f"request: {request_path}\n\n"
        "Review the exact requested revision in this worktree.\n"
        "Read the complete local review bundle, including any Developer "
        "resolutions.\n"
        "Do not modify tracked files.\n"
        "Write the required review artifacts and run agent-squad "
        "review-submit."
    )


def _load_active_request(
    control_root: Path,
    *,
    run_id: str,
    active_round: ActiveRoundRecord,
) -> ReviewRequest:
    """Reload the already-validated immutable request for prompt reuse."""

    request_path = (
        control_root
        / runs.RUNS_DIRECTORY_NAME
        / run_id
        / "rounds"
        / f"{active_round.round_number:03d}"
        / "request.json"
    )
    try:
        return ReviewRequest.from_dict(
            runs.load_json_object(request_path, "active review request")
        )
    except ArtifactValidationError as error:
        raise HandoffRecoveryError(
            f"active review request is invalid: {error}"
        ) from error


def _recovery_result(
    active: runs.ActiveRunStatus,
    active_round: ActiveRoundRecord,
    *,
    status: HandoffStatus,
    error: str | None,
    action: HandoffRecoveryAction | None,
) -> RetryHandoffResult:
    return RetryHandoffResult(
        run_id=active.run_id,
        round_number=active_round.round_number,
        request_id=active_round.request_id,
        reviewer_name=active_round.reviewer_name,
        handoff_status=status,
        handoff_error=error,
        action=action,
        result_id=None,
    )
