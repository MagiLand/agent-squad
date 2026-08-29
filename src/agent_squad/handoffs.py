"""Delivery and idempotent recovery of durable review handoffs."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from . import runs
from .artifacts import (
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
from .initialization import AgentSquadError, load_initialized_repository
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

    ADOPTED = "adopted existing request"
    REPROMPTED = "re-prompted Reviewer"
    RELAUNCHED = "relaunched Reviewer"
    RESULT_READY = "use marker-confirmed result"


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
            if isinstance(
                active.unapplied_review,
                runs.InvalidUnappliedReviewResult,
            ):
                raise HandoffRecoveryError(
                    "the active round has marker-confirmed output that did "
                    "not revalidate; inspect agent-squad status before "
                    "retrying"
                )
            if not active.review_worktree_available:
                raise HandoffRecoveryError(
                    "the expected review worktree is unavailable; refusing "
                    "to create replacement review authority"
                )

            request = _load_active_request(repository.control_root, active)
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
                    request_id=active_round.request_id,
                )
                if (
                    probe.session is not None
                    and probe.request_seen
                    and handoff.status is not HandoffStatus.SENT
                ):
                    action = HandoffRecoveryAction.ADOPTED
                else:
                    session = client.dispatch_review_request(
                        reviewer_name=active_round.reviewer_name,
                        reviewer_kind=active.reviewer_kind,
                        start_args=active.reviewer_start_args,
                        review_worktree=active_round.review_worktree,
                        prompt=format_review_request_prompt(
                            request,
                            active_round.review_worktree,
                        ),
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
                    status=HandoffStatus.FAILED,
                    error=detail,
                    installation=installation,
                    action=None,
                )
                return _result(
                    active,
                    status=HandoffStatus.FAILED,
                    error=detail,
                    action=None,
                )

            _record_handoff(
                repository.control_root,
                active=active,
                status=HandoffStatus.SENT,
                error=None,
                installation=installation,
                action=action,
            )
            return _result(
                active,
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
    status: HandoffStatus,
    error: str | None,
    installation: HerdrInstallation | None,
    action: HandoffRecoveryAction | None,
) -> None:
    """Persist one recovery attempt against the unchanged logical request."""

    active_round = active.active_round
    if active_round is None:
        raise HandoffRecoveryError("the active review round disappeared")
    timestamp = utc_timestamp()
    handoff = HandoffRecord(
        round_number=active_round.round_number,
        status=status,
        target=active_round.reviewer_name,
        last_error=error,
        updated_at=timestamp,
        herdr_version=(
            installation.version if installation is not None else None
        ),
        herdr_protocol=(
            installation.protocol if installation is not None else None
        ),
    )
    state_path = control_root / runs.STATE_FILE_NAME
    state = runs.load_json_object(state_path, "authoritative state")
    if state.get("active_run_id") != active.run_id:
        raise HandoffRecoveryError(
            "active run changed before recovery could be recorded"
        )
    state_round = state.get("active_round")
    if not isinstance(state_round, dict) or (
        state_round.get("request_id") != active_round.request_id
    ):
        raise HandoffRecoveryError(
            "active round changed before recovery could be recorded"
        )
    next_state = copy.deepcopy(state)
    next_state["updated_at"] = timestamp
    next_state["handoff"] = handoff.to_dict()
    try:
        atomic_write(state_path, encode_json(next_state), mode=0o600)
    except OSError as write_error:
        raise HandoffRecoveryError(
            f"could not record review handoff recovery: {write_error}"
        ) from write_error
    try:
        append_event(
            control_root
            / runs.RUNS_DIRECTORY_NAME
            / active.run_id
            / runs.EVENT_LOG_FILE_NAME,
            {
                "timestamp": timestamp,
                "event": (
                    "review_request_recovered"
                    if status is HandoffStatus.SENT
                    else "review_request_recovery_failed"
                ),
                "run_id": active.run_id,
                "round": active_round.round_number,
                "request_id": active_round.request_id,
                "target": active_round.reviewer_name,
                "action": action.value if action is not None else None,
                "error": error,
            },
        )
    except OSError as event_error:
        raise HandoffRecoveryError(
            "the recovered handoff state is durable, but its event could "
            f"not be recorded: {event_error}"
        ) from event_error


def format_review_request_prompt(
    request: ReviewRequest,
    review_worktree: Path,
) -> str:
    """Return the canonical short control prompt for one review request."""

    request_path = (
        review_worktree / ".agent-squad-review/input/request.json"
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
    active: runs.ActiveRunStatus,
) -> ReviewRequest:
    """Reload the already-validated immutable request for prompt reuse."""

    active_round = active.active_round
    if active_round is None:
        raise HandoffRecoveryError("the active review request is incomplete")
    request_path = (
        control_root
        / runs.RUNS_DIRECTORY_NAME
        / active.run_id
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


def _result(
    active: runs.ActiveRunStatus,
    *,
    status: HandoffStatus,
    error: str | None,
    action: HandoffRecoveryAction | None,
) -> RetryHandoffResult:
    active_round = active.active_round
    if active_round is None:
        raise HandoffRecoveryError("the active review round disappeared")
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
