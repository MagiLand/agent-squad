"""Delivery and idempotent recovery of durable review handoffs."""

from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile

from . import runs
from .artifacts import (
    ActiveRoundRecord,
    ArtifactValidationError,
    HandoffRecord,
    HandoffStatus,
    REVIEW_MARKDOWN_FILE_NAME,
    ReviewRequest,
    REVIEW_RESULT_FILE_NAME,
    ReviewerLocalMarker,
    ReviewResult,
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
    SCHEMA_VERSION,
    load_initialized_repository,
)
from .review_submissions import (
    ADVISORY_IGNORED_ROOT_ENTRIES,
    MARKER_PATH,
    SUBMISSION_LOCK_PATH,
    ReviewSubmissionError,
    mirror_retired_review_identities,
    record_retired_review_identity,
    retired_review_identity_digest,
)
from .storage import (
    InvalidJsonError,
    append_event,
    atomic_write,
    decode_json,
    encode_json,
    exclusive_file_lock,
    inspect_regular_tree,
    read_regular_tree,
    utc_timestamp,
)
from .validation import JsonValidator


class HandoffRecoveryError(AgentSquadError):
    """Raised when the active review handoff cannot be recovered safely."""


_DIAGNOSTIC_VALIDATOR = JsonValidator(HandoffRecoveryError)


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
    diagnostic_id: str | None


@dataclass(frozen=True)
class _MarkerRecoveryDisposition:
    """Whether a marker blocks correction and which identity it retires."""

    blocks_resubmission: bool
    result_id: str | None
    review_sha256: str | None


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
    sent_event_name: str,
    failed_event_name: str,
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

    event_name = (
        sent_event_name
        if status is HandoffStatus.SENT
        else failed_event_name
    )
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
                repository.worktree.invocation_directory,
                validate_live_review_bundle=False,
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
                    diagnostic_id=None,
                )
            if isinstance(
                active.unapplied_review,
                runs.UnavailableReviewEvidence,
            ):
                raise HandoffRecoveryError(
                    "review evidence is temporarily unavailable; refusing "
                    "to preserve or resend it: "
                    f"{active.unapplied_review.reason}"
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
            diagnostic_id = None
            if isinstance(
                active.unapplied_review,
                runs.InvalidUnappliedReviewResult,
            ):
                diagnostic_id = _preserve_invalid_review_evidence(
                    repository.control_root,
                    active=active,
                    active_round=active_round,
                    request=request,
                    invalid_review=active.unapplied_review,
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
                result = _recovery_result(
                    active,
                    active_round,
                    status=HandoffStatus.FAILED,
                    error=detail,
                    action=None,
                    diagnostic_id=diagnostic_id,
                )
                _record_handoff(
                    repository.control_root,
                    result=result,
                    installation=installation,
                )
                return result

            result = _recovery_result(
                active,
                active_round,
                status=HandoffStatus.SENT,
                error=None,
                action=action,
                diagnostic_id=diagnostic_id,
            )
            _record_handoff(
                repository.control_root,
                result=result,
                installation=installation,
            )
            return result
    except AgentSquadError:
        raise
    except OSError as error:
        raise HandoffRecoveryError(
            f"could not acquire or use the local recovery lock "
            f"{lock_path}: {error}"
        ) from error


def _preserve_invalid_review_evidence(
    control_root: Path,
    *,
    active: runs.ActiveRunStatus,
    active_round: ActiveRoundRecord,
    request: ReviewRequest,
    invalid_review: runs.InvalidUnappliedReviewResult,
) -> str:
    """Snapshot invalid evidence and unblock a corrected submission safely."""

    bundle_root = active_round.review_worktree / REVIEW_DIRECTORY_NAME
    lock_path = bundle_root.joinpath(*SUBMISSION_LOCK_PATH.parts)
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
    try:
        with exclusive_file_lock(lock_path):
            bundle_files = read_regular_tree(
                bundle_root,
                label="invalid marker-confirmed review bundle",
                error_type=HandoffRecoveryError,
                ignored_root_entries=ADVISORY_IGNORED_ROOT_ENTRIES,
            )
            captured = {
                name: bundle_files[path]
                for name, path in candidates
                if path in bundle_files
            }
            diagnostic_id = archive_invalid_review_evidence(
                control_root,
                run_id=active.run_id,
                round_number=active_round.round_number,
                request_id=active_round.request_id,
                reason=invalid_review.reason,
                captured=captured,
            )
            marker_bytes = captured.get(MARKER_PATH.name)
            disposition = (
                _classify_marker_for_recovery(
                    marker_bytes=marker_bytes,
                    review_bytes=captured.get(REVIEW_RESULT_FILE_NAME),
                    request=request,
                )
                if marker_bytes is not None
                else None
            )
            should_remove_marker = (
                disposition is not None
                and disposition.blocks_resubmission
            )
            authoritative_root: Path | None = None
            authoritative_digest: str | None = None
            if (
                disposition is not None
                and disposition.result_id is not None
                and disposition.review_sha256 is not None
            ):
                authoritative_root = _review_round_directory(
                    control_root,
                    run_id=active.run_id,
                    round_number=active_round.round_number,
                )
                authoritative_digest = retired_review_identity_digest(
                    authoritative_root,
                    request=request,
                    result_id=disposition.result_id,
                )
                if (
                    authoritative_digest is not None
                    and authoritative_digest
                    != disposition.review_sha256
                ):
                    should_remove_marker = True
            if should_remove_marker and disposition is not None:
                if (
                    disposition.result_id is not None
                    and disposition.review_sha256 is not None
                ):
                    assert authoritative_root is not None
                    if authoritative_digest is None:
                        record_retired_review_identity(
                            authoritative_root,
                            request=request,
                            result_id=disposition.result_id,
                            review_sha256=disposition.review_sha256,
                        )
                    mirror_retired_review_identities(
                        authoritative_root,
                        bundle_root,
                        request=request,
                    )
                _remove_captured_marker(
                    bundle_root.joinpath(*MARKER_PATH.parts),
                    expected=marker_bytes,
                )
    except HandoffRecoveryError:
        raise
    except runs.RunStateError as error:
        raise HandoffRecoveryError(
            "could not preserve invalid marker-confirmed review evidence: "
            f"{error}"
        ) from error
    except ReviewSubmissionError as error:
        raise HandoffRecoveryError(
            "could not retire invalid marker-confirmed review identity: "
            f"{error}"
        ) from error
    except OSError as error:
        raise HandoffRecoveryError(
            "could not preserve invalid marker-confirmed review evidence: "
            f"{error}"
        ) from error
    return diagnostic_id


def archive_invalid_review_evidence(
    control_root: Path,
    *,
    run_id: str,
    round_number: int,
    request_id: str,
    reason: str,
    captured: Mapping[str, bytes],
) -> str:
    """Commit one content-addressed diagnostic through a staged directory."""

    diagnostic_id = _invalid_review_diagnostic_id(reason, captured)
    parent = _invalid_results_directory(
        control_root,
        run_id=run_id,
        round_number=round_number,
    )
    diagnostic_root = parent / diagnostic_id
    if os.path.lexists(diagnostic_root):
        _verify_invalid_review_diagnostic(
            diagnostic_root,
            run_id=run_id,
            round_number=round_number,
            request_id=request_id,
            diagnostic_id=diagnostic_id,
            reason=reason,
            captured=captured,
        )
        return diagnostic_id

    summary = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_timestamp(),
        "run_id": run_id,
        "round": round_number,
        "request_id": request_id,
        "diagnostic_id": diagnostic_id,
        "reason": reason,
        "captured_files": sorted(captured),
    }
    staging = Path(
        tempfile.mkdtemp(prefix=f".{diagnostic_id}.", dir=parent)
    )
    try:
        staging.chmod(0o700)
        if staging.resolve(strict=True).parent != parent:
            raise HandoffRecoveryError(
                "invalid-result staging directory escaped owned run storage"
            )
        for name, content in captured.items():
            atomic_write(staging / name, content, mode=0o400)
        atomic_write(
            staging / "validation-error.json",
            encode_json(summary),
            mode=0o400,
        )
        _verify_invalid_review_diagnostic(
            staging,
            run_id=run_id,
            round_number=round_number,
            request_id=request_id,
            diagnostic_id=diagnostic_id,
            reason=reason,
            captured=captured,
        )
        staging.replace(diagnostic_root)
    except Exception:
        if os.path.lexists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        raise
    _verify_invalid_review_diagnostic(
        diagnostic_root,
        run_id=run_id,
        round_number=round_number,
        request_id=request_id,
        diagnostic_id=diagnostic_id,
        reason=reason,
        captured=captured,
    )
    return diagnostic_id


def validate_invalid_review_diagnostic(
    root: Path,
    *,
    run_id: str,
    round_number: int,
    request_id: str,
    diagnostic_id: str,
    reason: str,
) -> None:
    """Validate one recorded invalid-result diagnostic from history."""

    files = read_regular_tree(
        root,
        label="invalid-result diagnostic",
        error_type=HandoffRecoveryError,
    )
    allowed_evidence = {
        REVIEW_RESULT_FILE_NAME,
        REVIEW_MARKDOWN_FILE_NAME,
        MARKER_PATH.name,
    }
    captured: dict[str, bytes] = {}
    for path, content in files.items():
        if path == PurePosixPath("validation-error.json"):
            continue
        if len(path.parts) != 1 or path.name not in allowed_evidence:
            raise HandoffRecoveryError(
                "invalid-result diagnostic contains unexpected evidence"
            )
        captured[path.name] = content
    _verify_invalid_review_diagnostic(
        root,
        run_id=run_id,
        round_number=round_number,
        request_id=request_id,
        diagnostic_id=diagnostic_id,
        reason=reason,
        captured=captured,
    )


def _invalid_review_diagnostic_id(
    reason: str,
    captured: Mapping[str, bytes],
) -> str:
    """Hash unambiguously framed diagnostic fields and evidence bytes."""

    digest = hashlib.sha256()
    parts = [(b"reason", reason.encode("utf-8"))]
    parts.extend(
        (name.encode("utf-8"), content)
        for name, content in sorted(captured.items())
    )
    for name, content in parts:
        for value in (name, content):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def _invalid_results_directory(
    control_root: Path,
    *,
    run_id: str,
    round_number: int,
) -> Path:
    """Return a validated local diagnostic parent without following links."""

    round_directory = _review_round_directory(
        control_root,
        run_id=run_id,
        round_number=round_number,
    )
    diagnostics = _require_owned_directory(
        round_directory / "diagnostics",
        parent=round_directory,
        label="review diagnostics",
        create=True,
    )
    return _require_owned_directory(
        diagnostics / "invalid-results",
        parent=diagnostics,
        label="invalid-result diagnostics",
        create=True,
    )


def _review_round_directory(
    control_root: Path,
    *,
    run_id: str,
    round_number: int,
) -> Path:
    """Return one validated implementation-owned review round directory."""

    run_directory = runs.safe_run_directory(control_root, run_id)
    rounds_root = _require_owned_directory(
        run_directory / "rounds",
        parent=run_directory,
        label="review rounds",
        create=False,
    )
    return _require_owned_directory(
        rounds_root / f"{round_number:03d}",
        parent=rounds_root,
        label="active review round",
        create=False,
    )


def _require_owned_directory(
    path: Path,
    *,
    parent: Path,
    label: str,
    create: bool,
) -> Path:
    """Create or validate one direct, normal directory descendant."""

    try:
        status = path.lstat()
    except FileNotFoundError:
        if not create:
            raise HandoffRecoveryError(
                f"{label} must be a non-symlink directory: {path}"
            ) from None
        path.mkdir(mode=0o700)
        status = path.lstat()
    if not stat.S_ISDIR(status.st_mode):
        raise HandoffRecoveryError(
            f"{label} must be a non-symlink directory: {path}"
        )
    resolved = path.resolve(strict=True)
    if resolved.parent != parent:
        raise HandoffRecoveryError(
            f"{label} escaped its owned parent directory: {path}"
        )
    return resolved


def _verify_invalid_review_diagnostic(
    root: Path,
    *,
    run_id: str,
    round_number: int,
    request_id: str,
    diagnostic_id: str,
    reason: str,
    captured: Mapping[str, bytes],
) -> None:
    """Verify the exact immutable contents and identity of a diagnostic."""

    tree = inspect_regular_tree(
        root,
        label="invalid-result diagnostic",
        error_type=HandoffRecoveryError,
    )
    expected_names = set(captured) | {"validation-error.json"}
    expected_paths = {PurePosixPath(name) for name in expected_names}
    if tree.files != expected_paths or tree.directories:
        raise HandoffRecoveryError(
            "invalid-result diagnostic does not match captured evidence"
        )
    files = read_regular_tree(
        root,
        label="invalid-result diagnostic",
        error_type=HandoffRecoveryError,
    )
    for name, expected in captured.items():
        if files[PurePosixPath(name)] != expected:
            raise HandoffRecoveryError(
                f"invalid-result diagnostic differs for {name}"
            )

    summary_bytes = files[PurePosixPath("validation-error.json")]
    try:
        summary_value = decode_json(summary_bytes.decode("utf-8"))
    except (UnicodeDecodeError, InvalidJsonError) as error:
        raise HandoffRecoveryError(
            f"invalid-result diagnostic metadata is invalid: {error}"
        ) from error
    summary = _DIAGNOSTIC_VALIDATOR.require_object(
        summary_value,
        "invalid-result diagnostic metadata",
    )
    _DIAGNOSTIC_VALIDATOR.check_fields(
        summary,
        required={
            "schema_version",
            "created_at",
            "run_id",
            "round",
            "request_id",
            "diagnostic_id",
            "reason",
            "captured_files",
        },
        path="invalid-result diagnostic metadata",
    )
    if _DIAGNOSTIC_VALIDATOR.require_int(
        summary["schema_version"],
        "invalid-result diagnostic metadata.schema_version",
    ) != SCHEMA_VERSION:
        raise HandoffRecoveryError(
            "invalid-result diagnostic metadata.schema_version must be "
            f"{SCHEMA_VERSION}"
        )
    _DIAGNOSTIC_VALIDATOR.require_timestamp(
        summary["created_at"],
        "invalid-result diagnostic metadata.created_at",
    )
    actual = {
        "run_id": _DIAGNOSTIC_VALIDATOR.require_uuid(
            summary["run_id"],
            "invalid-result diagnostic metadata.run_id",
        ),
        "round": _DIAGNOSTIC_VALIDATOR.require_int(
            summary["round"],
            "invalid-result diagnostic metadata.round",
        ),
        "request_id": _DIAGNOSTIC_VALIDATOR.require_uuid(
            summary["request_id"],
            "invalid-result diagnostic metadata.request_id",
        ),
        "diagnostic_id": _DIAGNOSTIC_VALIDATOR.require_digest(
            summary["diagnostic_id"],
            "invalid-result diagnostic metadata.diagnostic_id",
        ),
        "reason": _DIAGNOSTIC_VALIDATOR.require_string(
            summary["reason"],
            "invalid-result diagnostic metadata.reason",
        ),
        "captured_files": list(
            _DIAGNOSTIC_VALIDATOR.require_narrow_relative_paths(
                summary["captured_files"],
                "invalid-result diagnostic metadata.captured_files",
            )
        ),
    }
    expected = {
        "run_id": run_id,
        "round": round_number,
        "request_id": request_id,
        "diagnostic_id": diagnostic_id,
        "reason": reason,
        "captured_files": sorted(captured),
    }
    if actual != expected:
        raise HandoffRecoveryError(
            "invalid-result diagnostic metadata does not match captured "
            "evidence"
        )
    if summary_bytes != encode_json(summary):
        raise HandoffRecoveryError(
            "invalid-result diagnostic metadata is not canonically encoded"
        )


def _classify_marker_for_recovery(
    *,
    marker_bytes: bytes,
    review_bytes: bytes | None,
    request: ReviewRequest,
) -> _MarkerRecoveryDisposition:
    """Classify marker replacement and retain the best known identity."""

    marker: ReviewerLocalMarker | None = None
    try:
        marker = ReviewerLocalMarker.from_dict(
            decode_json(marker_bytes.decode("utf-8"))
        )
    except (
        UnicodeDecodeError,
        InvalidJsonError,
        ArtifactValidationError,
    ):
        pass

    review: ReviewResult | None = None
    review_digest: str | None = None
    if review_bytes is not None:
        review_digest = hashlib.sha256(review_bytes).hexdigest()
        try:
            review = ReviewResult.from_dict(
                decode_json(review_bytes.decode("utf-8")),
                object_format=request.object_format,
            )
        except (
            UnicodeDecodeError,
            InvalidJsonError,
            ArtifactValidationError,
        ):
            pass

    review_matches_request = review is not None and all(
        actual == expected
        for actual, expected in (
            (review.request_id, request.request_id),
            (review.run_id, request.run_id),
            (review.round_number, request.round_number),
            (review.base_oid, request.base_oid),
            (review.head_oid, request.head_oid),
        )
    )
    if marker is not None and review is not None:
        marker_matches = all(
            actual == expected
            for actual, expected in (
                (marker.request_id, request.request_id),
                (marker.result_id, review.result_id),
                (marker.review_json_path, request.review_output_path),
                (marker.review_sha256, review_digest),
            )
        )
        if marker_matches and review_matches_request:
            assert review_digest is not None
            return _MarkerRecoveryDisposition(
                False,
                review.result_id,
                review_digest,
            )

    if marker is not None:
        return _MarkerRecoveryDisposition(
            True,
            marker.result_id,
            marker.review_sha256,
        )
    if (
        review_matches_request
        and review is not None
        and review_digest is not None
    ):
        return _MarkerRecoveryDisposition(
            True,
            review.result_id,
            review_digest,
        )
    return _MarkerRecoveryDisposition(True, None, None)


def _remove_captured_marker(path: Path, *, expected: bytes) -> None:
    """Remove a blocking marker only if it is still the captured file."""

    try:
        status = path.lstat()
    except FileNotFoundError:
        raise HandoffRecoveryError(
            "review marker changed after invalid evidence was preserved"
        ) from None
    if not stat.S_ISREG(status.st_mode) or path.read_bytes() != expected:
        raise HandoffRecoveryError(
            "review marker changed after invalid evidence was preserved"
        )
    path.unlink()


def _record_handoff(
    control_root: Path,
    *,
    result: RetryHandoffResult,
    installation: HerdrInstallation | None,
) -> None:
    """Persist one recovery attempt against the unchanged logical request."""

    event_fields: dict[str, object] = {
        "action": (
            result.action.value if result.action is not None else None
        ),
    }
    if result.diagnostic_id is not None:
        event_fields["diagnostic_id"] = result.diagnostic_id
    record_review_request_handoff(
        control_root,
        run_id=result.run_id,
        round_number=result.round_number,
        request_id=result.request_id,
        target=result.reviewer_name,
        status=result.handoff_status,
        error=result.handoff_error,
        installation=installation,
        sent_event_name="review_request_recovered",
        failed_event_name="review_request_recovery_failed",
        error_type=HandoffRecoveryError,
        extra_event_fields=event_fields,
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
    diagnostic_id: str | None,
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
        diagnostic_id=diagnostic_id,
    )
