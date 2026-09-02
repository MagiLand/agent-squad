"""Durable review submission for an exact Git revision."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import NoReturn
import uuid

from . import runs
from .artifacts import (
    ActiveRoundRecord,
    ArtifactValidationError,
    BundleArtifact,
    HandoffStatus,
    PREVIOUS_RESPONSE_BUNDLE_PATH,
    PREVIOUS_REVIEW_BUNDLE_PATH,
    RECOVERY_ROUND_BUNDLE_PATH,
    ReviewRequest,
    ReviewResponse,
    ReviewResult,
    ReviewRoundRecord,
    ReviewVerdict,
    ROUND_RESPONSE_FILE_NAME,
    ResponseDisposition,
    RoundStatus,
    SubmissionMode,
    deterministic_reviewer_name,
    response_validation_mode,
    validate_followup_submission_head,
    validate_review_response,
)
from .herdr import (
    HerdrClient,
    HerdrError,
    HerdrInstallation,
    format_herdr_error,
)
from .handoffs import (
    format_review_request_prompt,
    record_review_request_handoff,
    review_request_handoff_record,
)
from .initialization import (
    AgentSquadError,
    GitWorktree,
    InitializedRepository,
    REVIEW_DIRECTORY_NAME,
    is_agent_squad_runtime_path,
    load_initialized_repository,
    matches_allowed_generated_path,
    run_git,
)
from .storage import (
    InvalidJsonError,
    append_event,
    atomic_write,
    decode_json,
    encode_json,
    exclusive_file_lock,
    utc_timestamp,
)
from .validation import JsonValidator, OID_LENGTHS


ROUNDS_DIRECTORY_NAME = "rounds"
ROUND_RECORD_FILE_NAME = "round.json"
REQUEST_FILE_NAME = "request.json"
IMPLEMENTATION_REPORT_FILE_NAME = "implementation-report.md"
SENSITIVE_ROOT_FILES = {".github/copilot-instructions.md"}


class SubmissionError(AgentSquadError):
    """Raised when a review request cannot be prepared safely."""


def _raise_submission_failure(
    error: BaseException,
    message: str,
) -> NoReturn:
    """Preserve interruptions while normalizing ordinary failures."""

    if isinstance(error, Exception):
        raise SubmissionError(message) from error
    error.add_note(message)
    raise error


_VALIDATOR = JsonValidator(SubmissionError)


@dataclass(frozen=True)
class ImplementationReport:
    """Validated report content staged for the exact review round."""

    source_path: Path
    content: bytes
    sha256: str


@dataclass(frozen=True)
class ImplementationResponse:
    """Validated response content staged for a corrected submission."""

    source_path: Path
    content: bytes


@dataclass(frozen=True)
class SubmitResult:
    """Durable outcome of preparing and attempting one review request."""

    run_id: str
    round_number: int
    request_id: str
    base_oid: str
    head_oid: str
    review_worktree: Path
    reviewer_name: str
    handoff_status: HandoffStatus
    handoff_error: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _PreparedSubmission:
    repository: InitializedRepository
    run_id: str
    round_number: int
    request: ReviewRequest
    round_directory: Path
    review_worktree: Path
    reviewer_start_args: tuple[str, ...]
    warnings: tuple[str, ...]


def submit_candidate(
    start: Path,
    *,
    report_path: Path,
    mode: SubmissionMode | str,
    response_path: Path | None = None,
    herdr_client: HerdrClient | None = None,
) -> SubmitResult:
    """Persist one review round, then attempt its Herdr notification."""

    repository = load_initialized_repository(start)
    selected_mode = _submission_mode(mode)
    lock_path = repository.control_root / runs.LOCK_FILE_NAME
    try:
        with exclusive_file_lock(lock_path):
            prepared = _prepare_submission_locked(
                repository,
                report_path=report_path,
                mode=selected_mode,
                response_path=response_path,
            )
            client = herdr_client or HerdrClient(repository.worktree.root)
            installation: HerdrInstallation | None = None
            try:
                installation = client.discover(
                    prepared.request.reviewer_kind,
                    role="Reviewer",
                )
                client.dispatch_review_request(
                    reviewer_name=prepared.request.reviewer_name,
                    reviewer_kind=prepared.request.reviewer_kind,
                    start_args=prepared.reviewer_start_args,
                    review_worktree=prepared.review_worktree,
                    prompt=format_review_request_prompt(
                        prepared.request,
                        prepared.review_worktree,
                    ),
                )
            except HerdrError as error:
                detail = format_herdr_error(str(error))
                _record_handoff(
                    prepared,
                    status=HandoffStatus.FAILED,
                    error=detail,
                    installation=installation,
                )
                return _submission_result(
                    prepared,
                    status=HandoffStatus.FAILED,
                    error=detail,
                )

            _record_handoff(
                prepared,
                status=HandoffStatus.SENT,
                error=None,
                installation=installation,
            )
            return _submission_result(
                prepared,
                status=HandoffStatus.SENT,
                error=None,
            )
    except AgentSquadError:
        raise
    except OSError as error:
        raise SubmissionError(
            f"could not acquire or use the local submission lock "
            f"{lock_path}: {error}"
        ) from error


def _prepare_submission_locked(
    repository: InitializedRepository,
    *,
    report_path: Path,
    mode: SubmissionMode,
    response_path: Path | None,
) -> _PreparedSubmission:
    status = runs.inspect_status_locked(
        repository.worktree.invocation_directory
    )
    active = status.active_run
    if active is None:
        raise SubmissionError(
            "there is no active run; run agent-squad start before submit"
        )
    if active.phase is not runs.RunPhase.IMPLEMENTING:
        raise SubmissionError(
            f"run {active.run_id} is in phase {active.phase.value}; "
            "submit is allowed only while implementing"
        )
    is_first_round = active.current_round == 0
    if is_first_round and (
        active.current_head_oid is not None
        or active.active_round is not None
        or active.handoff is not None
    ):
        raise SubmissionError(
            "first-round run state is internally inconsistent"
        )
    _validate_branch_identity(active.repository, repository.worktree)

    artifact_candidates = [
        _submission_artifact_candidate(
            report_path,
            repository.worktree.invocation_directory,
        )
    ]
    if response_path is not None:
        artifact_candidates.append(
            _submission_artifact_candidate(
                response_path,
                repository.worktree.invocation_directory,
            )
        )
    _validate_implementation_cleanliness(
        repository,
        artifact_sources=tuple(artifact_candidates),
    )
    object_format = _current_object_format(repository.worktree.root)
    if object_format != active.git_object_format:
        raise SubmissionError(
            "current Git object format does not match the active run"
        )
    head_oid = _resolve_head(repository.worktree.root, object_format)
    _require_base_ancestor(
        repository.worktree.root,
        active.base_oid,
        head_oid,
    )

    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    state_path = repository.control_root / runs.STATE_FILE_NAME
    run_record_path = run_directory / runs.RUN_RECORD_FILE_NAME
    state = runs.load_json_object(state_path, "authoritative state")
    original_state = state_path.read_bytes()
    run_record = runs.load_json_object(run_record_path, "active run record")
    original_run_record = run_record_path.read_bytes()

    previous: runs.AppliedReviewAuthority | None = None
    implementation_response: ImplementationResponse | None = None
    response_mode = mode
    recovering = False
    recovery_round_content: bytes | None = None
    additional_bundle_contents: tuple[
        tuple[BundleArtifact, bytes], ...
    ] = ()
    if is_first_round:
        _validate_first_submission_mode(
            mode,
            base_oid=active.base_oid,
            head_oid=head_oid,
        )
        round_number = 1
    else:
        active_round = active.active_round
        if active_round is None:
            raise SubmissionError(
                "a follow-up submission requires closed round history"
            )
        recovering = active_round.status in runs.RECOVERY_ROUND_STATUSES
        if recovering:
            previous = _find_applied_changes_review(
                run_directory,
                active,
            )
            if previous is None:
                if mode is not SubmissionMode.NEW_REVISION:
                    raise SubmissionError(
                        "recovery without an applied prior review must use "
                        "--mode new_revision"
                    )
                if head_oid == active.base_oid:
                    raise SubmissionError(
                        "recovery without an applied prior review requires a "
                        "committed candidate whose HEAD differs from the "
                        "fixed base"
                    )
            else:
                try:
                    validate_followup_submission_head(
                        mode,
                        head_oid=head_oid,
                        previous_reviewed_head_oid=previous.review.head_oid,
                        recovery_head_oid=active.current_head_oid,
                    )
                except ArtifactValidationError as error:
                    raise SubmissionError(str(error)) from error
            recovery_round_content = _read_file(
                run_directory
                / ROUNDS_DIRECTORY_NAME
                / f"{active.current_round:03d}"
                / ROUND_RECORD_FILE_NAME
            )
        else:
            previous = _load_applied_changes_review(
                run_directory,
                active,
            )
            try:
                validate_followup_submission_head(
                    mode,
                    head_oid=head_oid,
                    previous_reviewed_head_oid=previous.review.head_oid,
                )
            except ArtifactValidationError as error:
                raise SubmissionError(str(error)) from error
        round_number = active.current_round + 1

    if previous is not None:
        response_mode = response_validation_mode(
            mode,
            head_oid=head_oid,
            previous_reviewed_head_oid=previous.review.head_oid,
        )

    report = _capture_report(
        report_path,
        repository.worktree.invocation_directory,
    )
    if previous is None:
        previous_review_path = None
        previous_response_path = None
        if response_path is not None:
            if is_first_round:
                raise SubmissionError(
                    "the first review round does not accept --response"
                )
            raise SubmissionError(
                "a recovery submission without an applied previous review "
                "does not accept --response"
            )
    else:
        previous_review_path = PREVIOUS_REVIEW_BUNDLE_PATH
        previous_response_path = PREVIOUS_RESPONSE_BUNDLE_PATH
        if response_path is None:
            raise SubmissionError(
                "a submission after changes_requested requires --response "
                "<response.json>"
            )
        implementation_response = _capture_response(
            response_path,
            repository.worktree.invocation_directory,
            object_format=object_format,
            previous_review=previous.review,
            mode=response_mode,
        )
        additional_bundle_contents = (
            (
                BundleArtifact(
                    path=previous_review_path,
                    sha256=hashlib.sha256(
                        previous.review_bytes
                    ).hexdigest(),
                ),
                previous.review_bytes,
            ),
            (
                BundleArtifact(
                    path=previous_response_path,
                    sha256=hashlib.sha256(
                        implementation_response.content
                    ).hexdigest(),
                ),
                implementation_response.content,
            ),
        )
    recovery_round_path: str | None = None
    if recovering:
        if recovery_round_content is None:
            raise SubmissionError(
                "recovery round authority disappeared during submission"
            )
        recovery_round_path = RECOVERY_ROUND_BUNDLE_PATH
        additional_bundle_contents = (
            *additional_bundle_contents,
            (
                BundleArtifact(
                    path=recovery_round_path,
                    sha256=hashlib.sha256(
                        recovery_round_content
                    ).hexdigest(),
                ),
                recovery_round_content,
            ),
        )

    artifact_sources = [report.source_path]
    if implementation_response is not None:
        artifact_sources.append(implementation_response.source_path)
    warnings = _sensitive_change_warnings(
        repository.worktree.root,
        active.base_oid,
        head_oid,
    )

    request_id = str(uuid.uuid4())
    timestamp = utc_timestamp()
    try:
        reviewer_name = deterministic_reviewer_name(
            active.run_id,
            round_number,
        )
    except ArtifactValidationError as error:
        raise SubmissionError(
            f"cannot derive deterministic Reviewer name: {error}"
        ) from error
    review_worktree = review_worktree_path(
        repository,
        repository_id=active.repository.repository_id,
        run_id=active.run_id,
        round_number=round_number,
    )
    task = BundleArtifact(
        path="input/task.md",
        sha256=active.task_sha256,
    )
    context_files = _context_bundle_artifacts(run_record)
    request = ReviewRequest(
        created_at=timestamp,
        request_id=request_id,
        run_id=active.run_id,
        round_number=round_number,
        mode=mode,
        object_format=object_format,
        base_oid=active.base_oid,
        head_oid=head_oid,
        task=task,
        implementation_report=BundleArtifact(
            path="input/implementation-report.md",
            sha256=report.sha256,
        ),
        context_files=context_files,
        previous_review_path=previous_review_path,
        previous_response_path=previous_response_path,
        recovery_round_path=recovery_round_path,
        resolution_paths=(),
        review_output_path="output/review.json",
        review_markdown_path="output/review.md",
        implementer_agent=active.implementer_agent,
        implementer_kind=active.implementer_kind,
        reviewer_kind=active.reviewer_kind,
        reviewer_name=reviewer_name,
        allowed_generated_paths=(
            repository.configuration.allowed_generated_paths
        ),
    )
    try:
        request = ReviewRequest.from_dict(request.to_dict())
    except ArtifactValidationError as error:
        raise SubmissionError(
            f"cannot build review request: {error}"
        ) from error
    request_bytes = encode_json(request.to_dict())
    request_digest = hashlib.sha256(request_bytes).hexdigest()
    pending_handoff = review_request_handoff_record(
        round_number=round_number,
        target=reviewer_name,
        status=HandoffStatus.PENDING,
        timestamp=timestamp,
        error=None,
        installation=None,
    )
    round_record = ReviewRoundRecord.for_request(
        request=request,
        review_worktree=review_worktree,
        reviewer_start_args=active.reviewer_start_args,
        request_digest=request_digest,
        warnings=warnings,
        additional_bundle_inputs=tuple(
            artifact for artifact, _ in additional_bundle_contents
        ),
    )

    rounds_root = run_directory / ROUNDS_DIRECTORY_NAME
    rounds_root_created = False
    staging_directory: Path | None = None
    round_directory = rounds_root / f"{round_number:03d}"
    worktree_created = False
    bundle_created = False
    bundle_root = review_worktree / REVIEW_DIRECTORY_NAME
    round_directory_committed = False
    run_record_written = False
    response_authority_path = (
        previous.round_directory / ROUND_RESPONSE_FILE_NAME
        if previous is not None
        else None
    )
    original_response: bytes | None = None
    response_existed = False
    response_written = False
    if response_authority_path is not None and os.path.lexists(
        response_authority_path
    ):
        response_existed = True
        original_response = _read_file(response_authority_path)
    next_state_bytes: bytes | None = None
    commit_point_reached = False
    try:
        rounds_root_created = _ensure_rounds_root(rounds_root)
        if os.path.lexists(round_directory):
            raise SubmissionError(
                f"review round directory already exists: {round_directory}"
            )
        staging_directory = Path(
            tempfile.mkdtemp(
                prefix=f".{round_number:03d}.",
                dir=rounds_root,
            )
        )
        staging_directory.chmod(0o700)
        atomic_write(
            staging_directory / IMPLEMENTATION_REPORT_FILE_NAME,
            report.content,
            mode=0o400,
        )
        atomic_write(
            staging_directory / REQUEST_FILE_NAME,
            request_bytes,
            mode=0o400,
        )
        atomic_write(
            staging_directory / ROUND_RECORD_FILE_NAME,
            encode_json(round_record.to_dict()),
            mode=0o600,
        )

        _create_detached_review_worktree(
            repository.worktree,
            review_worktree,
            head_oid,
        )
        worktree_created = True
        _create_review_bundle_root(bundle_root)
        bundle_created = True
        _build_review_bundle(
            bundle_root=bundle_root,
            run_directory=run_directory,
            staging_directory=staging_directory,
            request=request,
            request_bytes=request_bytes,
            report=report,
            additional_inputs=additional_bundle_contents,
        )
        _verify_review_worktree(
            review_worktree,
            request=request,
            request_bytes=request_bytes,
            run_directory=run_directory,
            report=report,
            additional_inputs=additional_bundle_contents,
        )
        _validate_branch_identity(active.repository, repository.worktree)
        _validate_implementation_cleanliness(
            repository,
            artifact_sources=tuple(artifact_sources),
        )
        current_head_oid = _resolve_head(
            repository.worktree.root,
            object_format,
        )
        if current_head_oid != head_oid:
            raise SubmissionError(
                "implementation HEAD changed while the review request was "
                "being prepared; no round was committed"
            )

        next_run_record = copy.deepcopy(run_record)
        next_run_record["phase"] = runs.RunPhase.REVIEWING.value
        active_round = ActiveRoundRecord(
            round_number=round_number,
            status=RoundStatus.REVIEWING,
            mode=mode,
            request_id=request_id,
            result_id=None,
            review_worktree=review_worktree,
            reviewer_name=reviewer_name,
        )
        next_state = copy.deepcopy(state)
        next_state.update(
            updated_at=timestamp,
            phase=runs.RunPhase.REVIEWING.value,
            current_round=round_number,
            current_head_oid=head_oid,
            approved_head_oid=None,
            active_round=active_round.to_dict(),
            handoff=pending_handoff.to_dict(),
        )
        next_state_bytes = encode_json(next_state)

        staging_directory.replace(round_directory)
        staging_directory = None
        round_directory_committed = True
        if (
            response_authority_path is not None
            and implementation_response is not None
        ):
            atomic_write(
                response_authority_path,
                implementation_response.content,
                mode=0o400,
            )
            response_written = True
        atomic_write(
            run_record_path,
            encode_json(next_run_record),
            mode=0o600,
        )
        run_record_written = True
        atomic_write(
            state_path,
            next_state_bytes,
            mode=0o600,
        )
        commit_point_reached = True
        append_event(
            run_directory / runs.EVENT_LOG_FILE_NAME,
            {
                "timestamp": timestamp,
                "event": "review_request_persisted",
                "run_id": active.run_id,
                "round": round_number,
                "request_id": request_id,
                "base_oid": active.base_oid,
                "head_oid": head_oid,
                "mode": mode.value,
                "reviewer_name": reviewer_name,
            },
        )
    except BaseException as error:
        if not commit_point_reached:
            try:
                current_state = state_path.read_bytes()
            except OSError as inspection_error:
                message = (
                    "could not determine whether the review-request state "
                    f"committed after {error}: {inspection_error}; staged "
                    "artifacts were left in place"
                )
                _raise_submission_failure(error, message)
            if (
                next_state_bytes is not None
                and current_state == next_state_bytes
            ):
                message = (
                    "the review request is durable, but state persistence "
                    f"reported an error; retry the command: {error}"
                )
                _raise_submission_failure(error, message)
            if current_state != original_state:
                message = (
                    "authoritative state changed unexpectedly after a "
                    f"review-request failure ({error}); staged artifacts "
                    "were left in place"
                )
                _raise_submission_failure(error, message)
        else:
            if isinstance(error, SubmissionError):
                raise
            message = (
                "the review request is durable, but its persisted event "
                f"could not be recorded: {error}"
            )
            _raise_submission_failure(error, message)
        cleanup_errors: list[str] = []
        if response_written and response_authority_path is not None:
            try:
                current_response = _read_file(response_authority_path)
                if (
                    implementation_response is None
                    or current_response != implementation_response.content
                ):
                    raise OSError("content changed during rollback")
                if response_existed:
                    if original_response is None:
                        raise OSError("original response was not captured")
                    atomic_write(
                        response_authority_path,
                        original_response,
                        mode=0o400,
                    )
                else:
                    response_authority_path.unlink()
            except (OSError, SubmissionError) as cleanup_error:
                cleanup_errors.append(
                    "could not restore the previous-round response: "
                    f"{cleanup_error}"
                )
        if run_record_written:
            try:
                atomic_write(
                    run_record_path,
                    original_run_record,
                    mode=0o600,
                )
            except OSError as cleanup_error:
                cleanup_errors.append(
                    "could not restore the active run record: "
                    f"{cleanup_error}"
                )
        if round_directory_committed:
            cleanup_errors.extend(
                _remove_owned_directory(round_directory, "review round")
            )
        if staging_directory is not None:
            cleanup_errors.extend(
                _remove_owned_directory(staging_directory, "staged round")
            )
        if bundle_created:
            cleanup_errors.extend(
                _remove_owned_directory(bundle_root, "review bundle")
            )
        if worktree_created:
            cleanup_errors.extend(
                _remove_review_worktree(
                    repository.worktree,
                    review_worktree,
                )
            )
        if rounds_root_created:
            try:
                rounds_root.rmdir()
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                cleanup_errors.append(
                    f"could not remove newly created {rounds_root}: "
                    f"{cleanup_error}"
                )
        cleanup_note = (
            " Rollback also encountered: " + "; ".join(cleanup_errors)
            if cleanup_errors
            else ""
        )
        if isinstance(error, AgentSquadError):
            _raise_submission_failure(error, f"{error}.{cleanup_note}")
        message = (
            f"could not prepare the review request: {error}.{cleanup_note}"
        )
        _raise_submission_failure(error, message)

    return _PreparedSubmission(
        repository=repository,
        run_id=active.run_id,
        round_number=round_number,
        request=request,
        round_directory=round_directory,
        review_worktree=review_worktree,
        reviewer_start_args=active.reviewer_start_args,
        warnings=warnings,
    )


def _record_handoff(
    prepared: _PreparedSubmission,
    *,
    status: HandoffStatus,
    error: str | None,
    installation: HerdrInstallation | None,
) -> None:
    record_review_request_handoff(
        prepared.repository.control_root,
        run_id=prepared.run_id,
        round_number=prepared.round_number,
        request_id=prepared.request.request_id,
        target=prepared.request.reviewer_name,
        status=status,
        error=error,
        installation=installation,
        sent_event_name="review_request_sent",
        failed_event_name="review_request_failed",
        error_type=SubmissionError,
    )


def _submission_result(
    prepared: _PreparedSubmission,
    *,
    status: HandoffStatus,
    error: str | None,
) -> SubmitResult:
    request = prepared.request
    return SubmitResult(
        run_id=prepared.run_id,
        round_number=prepared.round_number,
        request_id=request.request_id,
        base_oid=request.base_oid,
        head_oid=request.head_oid,
        review_worktree=prepared.review_worktree,
        reviewer_name=request.reviewer_name,
        handoff_status=status,
        handoff_error=error,
        warnings=prepared.warnings,
    )


def _submission_mode(value: SubmissionMode | str) -> SubmissionMode:
    return _VALIDATOR.require_enum(value, "submission mode", SubmissionMode)


def _validate_branch_identity(
    stored: runs.RepositoryIdentity,
    worktree: GitWorktree,
) -> None:
    current = runs.repository_identity(worktree)
    if (
        current.start_branch_ref != stored.start_branch_ref
        or current.start_head_detached != stored.start_head_detached
    ):
        recorded = stored.start_branch_ref or "detached HEAD"
        actual = current.start_branch_ref or "detached HEAD"
        raise SubmissionError(
            "current branch or detached state does not match the active "
            f"run: recorded {recorded}, current {actual}"
        )


def _submission_artifact_candidate(
    path: Path,
    invocation_directory: Path,
) -> Path:
    """Resolve an artifact name for the pre-ingest clean-tree scan."""

    candidate = path if path.is_absolute() else invocation_directory / path
    return candidate.resolve(strict=False)


def _capture_report(
    path: Path,
    invocation_directory: Path,
) -> ImplementationReport:
    candidate = path if path.is_absolute() else invocation_directory / path
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise SubmissionError(
                f"implementation report must be a regular file: {resolved}"
            )
        with resolved.open("rb") as report_file:
            if not stat.S_ISREG(os.fstat(report_file.fileno()).st_mode):
                raise SubmissionError(
                    "implementation report must be a regular file: "
                    f"{resolved}"
                )
            content = report_file.read()
    except SubmissionError:
        raise
    except (OSError, RuntimeError) as error:
        raise SubmissionError(
            f"cannot read implementation report {candidate}: {error}"
        ) from error
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SubmissionError(
            "implementation report must contain UTF-8 text"
        ) from error
    if not text.strip():
        raise SubmissionError("implementation report must not be empty")
    return ImplementationReport(
        source_path=resolved,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _load_applied_changes_review(
    run_directory: Path,
    active: runs.ActiveRunStatus,
) -> runs.AppliedReviewAuthority:
    authority = _find_applied_changes_review(run_directory, active)
    if authority is None:
        raise SubmissionError(
            "a correction-round request has no previous applied review"
        )
    return authority


def _find_applied_changes_review(
    run_directory: Path,
    active: runs.ActiveRunStatus,
) -> runs.AppliedReviewAuthority | None:
    """Find and validate the latest applied changes-requested authority."""

    active_round = active.active_round
    if (
        active.current_round < 1
        or active.current_head_oid is None
        or active_round is None
    ):
        raise SubmissionError(
            "a follow-up submission requires one applied prior review"
        )
    try:
        authority = runs.find_latest_applied_review_before(
            run_directory=run_directory,
            run_id=active.run_id,
            current_round=active.current_round + 1,
            base_oid=active.base_oid,
            object_format=active.git_object_format,
        )
    except runs.RunStateError as error:
        raise SubmissionError(str(error)) from error
    if authority is None:
        return None
    round_record = authority.round_record
    if round_record.verdict is not ReviewVerdict.CHANGES_REQUESTED:
        raise SubmissionError(
            "a follow-up submission requires an applied changes_requested "
            "review"
        )
    if round_record.round_number == active.current_round:
        comparisons = (
            (round_record.request_id, active_round.request_id, "request ID"),
            (round_record.result_id, active_round.result_id, "result ID"),
            (round_record.head_oid, active.current_head_oid, "head OID"),
            (active_round.status, RoundStatus.APPLIED, "status"),
        )
        for actual, expected, label in comparisons:
            if actual != expected:
                raise SubmissionError(
                    f"previous applied round {label} does not match "
                    "authoritative state"
                )
    return authority


def _capture_response(
    path: Path,
    invocation_directory: Path,
    *,
    object_format: str,
    previous_review: ReviewResult,
    mode: SubmissionMode,
) -> ImplementationResponse:
    candidate = path if path.is_absolute() else invocation_directory / path
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise SubmissionError(
                f"implementation response must be a regular file: {resolved}"
            )
        with resolved.open("rb") as response_file:
            if not stat.S_ISREG(os.fstat(response_file.fileno()).st_mode):
                raise SubmissionError(
                    "implementation response must be a regular file: "
                    f"{resolved}"
                )
            content = response_file.read()
    except SubmissionError:
        raise
    except (OSError, RuntimeError) as error:
        raise SubmissionError(
            f"cannot read implementation response {candidate}: {error}"
        ) from error
    try:
        value = decode_json(content.decode("utf-8"))
        response = ReviewResponse.from_dict(
            value,
            object_format=object_format,
        )
        validate_review_response(response, previous_review, mode)
    except (
        UnicodeDecodeError,
        InvalidJsonError,
        ArtifactValidationError,
    ) as error:
        raise SubmissionError(
            f"implementation response failed validation: {error}"
        ) from error
    if (
        response.supersedes_response_id is not None
        or response.resolution_ids
    ):
        raise SubmissionError(
            "response replacement after Developer resolution is not "
            "supported by this command version"
        )
    if any(
        item.disposition is ResponseDisposition.NEEDS_HUMAN
        for item in response.responses
    ):
        raise SubmissionError(
            "implementation response requires Developer authority; use "
            "agent-squad escalate --response instead of creating a review "
            "round"
        )
    return ImplementationResponse(
        source_path=resolved,
        content=content,
    )


def _validate_implementation_cleanliness(
    repository: InitializedRepository,
    *,
    artifact_sources: tuple[Path, ...],
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
        raise SubmissionError(
            "Agent Squad runtime files are not Git-excluded; run "
            "agent-squad init to restore the local exclusions"
        )

    tracked = run_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
    )
    if tracked.returncode != 0:
        detail = tracked.stderr.strip() or "unknown Git error"
        raise SubmissionError(
            f"could not inspect tracked worktree cleanliness: {detail}"
        )
    tracked_entries = [
        entry for entry in tracked.stdout.split("\x00") if entry
    ]
    if tracked_entries:
        raise SubmissionError(
            "implementation worktree has uncommitted tracked changes; "
            "commit or restore them before submit"
        )

    untracked = run_git(
        root,
        "ls-files",
        "--others",
        "-z",
        "--",
    )
    if untracked.returncode != 0:
        detail = untracked.stderr.strip() or "unknown Git error"
        raise SubmissionError(f"could not inspect untracked files: {detail}")
    artifact_paths = {
        relative
        for source in artifact_sources
        if (relative := _relative_to_repository(source, root)) is not None
    }
    unexpected = [
        entry
        for entry in untracked.stdout.split("\x00")
        if entry
        and entry not in artifact_paths
        and not is_agent_squad_runtime_path(entry)
        and not matches_allowed_generated_path(
            entry,
            repository.configuration.allowed_generated_paths,
        )
    ]
    if unexpected:
        rendered = ", ".join(repr(path) for path in unexpected[:5])
        suffix = " ..." if len(unexpected) > 5 else ""
        raise SubmissionError(
            "implementation worktree has unexpected untracked files: "
            f"{rendered}{suffix}; preserve them and configure only known "
            "generated paths when appropriate"
        )


def _relative_to_repository(path: Path, root: Path) -> str | None:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _current_object_format(repository_root: Path) -> str:
    result = run_git(repository_root, "rev-parse", "--show-object-format")
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown Git error"
        raise SubmissionError(
            f"could not determine Git object format: {detail}"
        )
    value = result.stdout.rstrip("\r\n")
    if value not in OID_LENGTHS:
        raise SubmissionError(f"unsupported Git object format: {value!r}")
    return value


def _resolve_head(repository_root: Path, object_format: str) -> str:
    result = run_git(
        repository_root,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "HEAD is not a commit"
        raise SubmissionError(f"cannot resolve candidate HEAD: {detail}")
    return _VALIDATOR.require_oid(
        result.stdout.rstrip("\r\n"),
        object_format,
        "candidate HEAD",
    )


def _require_base_ancestor(
    repository_root: Path,
    base_oid: str,
    head_oid: str,
) -> None:
    result = run_git(
        repository_root,
        "merge-base",
        "--is-ancestor",
        base_oid,
        head_oid,
    )
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise SubmissionError(
            "the fixed review base is no longer an ancestor of the "
            "candidate head; cancel this run and start a new run with a "
            "newly resolved base"
        )
    detail = result.stderr.strip() or "unknown Git error"
    raise SubmissionError(f"could not validate base ancestry: {detail}")


def _validate_first_submission_mode(
    mode: SubmissionMode,
    *,
    base_oid: str,
    head_oid: str,
) -> None:
    if mode is not SubmissionMode.NEW_REVISION:
        raise SubmissionError(
            "the first review round must use --mode new_revision"
        )
    if head_oid == base_oid:
        raise SubmissionError(
            "the first review round requires a committed candidate whose "
            "HEAD differs from the fixed base"
        )


def _sensitive_change_warnings(
    repository_root: Path,
    base_oid: str,
    head_oid: str,
) -> tuple[str, ...]:
    result = run_git(
        repository_root,
        "diff",
        "--name-only",
        "-z",
        f"{base_oid}..{head_oid}",
        "--",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown Git error"
        raise SubmissionError(
            f"could not inspect candidate paths for warnings: {detail}"
        )
    sensitive = sorted(
        path
        for path in result.stdout.split("\x00")
        if path and _is_sensitive_path(path)
    )
    if not sensitive:
        return ()
    return (
        "candidate changes review-control or agent-instruction files: "
        + ", ".join(sensitive),
    )


def _is_sensitive_path(path_text: str) -> bool:
    if path_text in SENSITIVE_ROOT_FILES:
        return True
    path = PurePosixPath(path_text)
    lowered_parts = tuple(part.lower() for part in path.parts)
    name = path.name.lower()
    if path.name in {"AGENTS.md", "CLAUDE.md"}:
        return True
    if ".agents" in lowered_parts or ".claude" in lowered_parts:
        return True
    if path.parts[:2] == ("src", "agent_squad"):
        return True
    if "prompt" in name and path.suffix.lower() in {".md", ".py", ".txt"}:
        return True
    return "herdr" in name and path.suffix.lower() in {
        ".py",
        ".sh",
        ".js",
        ".ts",
    }


def _context_bundle_artifacts(
    run_record: dict[str, object],
) -> tuple[BundleArtifact, ...]:
    value = run_record.get("context_files")
    if not isinstance(value, list):
        raise SubmissionError("active run context metadata is invalid")
    artifacts: list[BundleArtifact] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SubmissionError(
                f"active run context metadata at index {index} is invalid"
            )
        path = item.get("path")
        digest = item.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise SubmissionError(
                f"active run context metadata at index {index} is invalid"
            )
        artifacts.append(
            BundleArtifact(path=f"input/{path}", sha256=digest)
        )
    return tuple(artifacts)


def review_worktree_path(
    repository: InitializedRepository,
    *,
    repository_id: str,
    run_id: str,
    round_number: int,
) -> Path:
    try:
        root = repository.configuration.review_worktree_root.resolve(
            strict=False
        )
    except (OSError, RuntimeError) as error:
        raise SubmissionError(
            "cannot resolve configured review-worktree root: "
            f"{error}"
        ) from error
    return root / repository_id / run_id / f"round-{round_number:03d}"


def _ensure_rounds_root(path: Path) -> bool:
    if path.is_symlink():
        raise SubmissionError(
            f"review-round history must not be a symbolic link: {path}"
        )
    if path.exists():
        if not path.is_dir():
            raise SubmissionError(
                f"review-round history must be a directory: {path}"
            )
        return False
    path.mkdir(mode=0o700)
    return True


def _create_detached_review_worktree(
    implementation: GitWorktree,
    review_worktree: Path,
    head_oid: str,
) -> None:
    if os.path.lexists(review_worktree):
        raise SubmissionError(
            f"deterministic review worktree already exists: {review_worktree}"
        )
    try:
        review_worktree.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as error:
        raise SubmissionError(
            f"cannot create review-worktree parent "
            f"{review_worktree.parent}: {error}"
        ) from error
    result = run_git(
        implementation.root,
        "worktree",
        "add",
        "--detach",
        str(review_worktree),
        head_oid,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown Git error"
        raise SubmissionError(
            f"could not create detached review worktree: {detail}"
        )


def _create_review_bundle_root(bundle_root: Path) -> None:
    try:
        bundle_root.mkdir(mode=0o700)
    except FileExistsError as error:
        raise SubmissionError(
            "candidate revision already contains the reserved review-bundle "
            f"path: {bundle_root}"
        ) from error
    except OSError as error:
        raise SubmissionError(
            f"cannot create review-bundle directory {bundle_root}: {error}"
        ) from error


def _build_review_bundle(
    *,
    bundle_root: Path,
    run_directory: Path,
    staging_directory: Path,
    request: ReviewRequest,
    request_bytes: bytes,
    report: ImplementationReport,
    additional_inputs: tuple[tuple[BundleArtifact, bytes], ...],
) -> None:
    input_root = bundle_root / "input"
    output_root = bundle_root / "output"
    input_root.mkdir(mode=0o700)
    output_root.mkdir(mode=0o700)
    atomic_write(input_root / "request.json", request_bytes, mode=0o400)
    atomic_write(
        input_root / "task.md",
        _read_file(run_directory / "task.md"),
        mode=0o400,
    )
    atomic_write(
        input_root / IMPLEMENTATION_REPORT_FILE_NAME,
        report.content,
        mode=0o400,
    )
    for artifact in request.context_files:
        relative = PurePosixPath(artifact.path)
        run_relative = PurePosixPath(*relative.parts[1:])
        source = run_directory.joinpath(*run_relative.parts)
        destination = bundle_root.joinpath(*relative.parts)
        directory = bundle_root
        for part in relative.parts[:-1]:
            directory /= part
            directory.mkdir(mode=0o700, exist_ok=True)
        atomic_write(destination, _read_file(source), mode=0o400)
    for artifact, content in additional_inputs:
        relative = PurePosixPath(artifact.path)
        destination = bundle_root.joinpath(*relative.parts)
        destination.parent.mkdir(mode=0o700, exist_ok=True)
        atomic_write(destination, content, mode=0o400)

    authoritative_request = staging_directory / REQUEST_FILE_NAME
    if authoritative_request.read_bytes() != request_bytes:
        raise SubmissionError(
            "staged authoritative request differs from its review-bundle copy"
        )


def _verify_review_worktree(
    review_worktree: Path,
    *,
    request: ReviewRequest,
    request_bytes: bytes,
    run_directory: Path,
    report: ImplementationReport,
    additional_inputs: tuple[tuple[BundleArtifact, bytes], ...],
) -> None:
    if not review_worktree.is_dir() or review_worktree.is_symlink():
        raise SubmissionError(
            f"review worktree is not a normal directory: {review_worktree}"
        )
    head = _resolve_head(review_worktree, request.object_format)
    if head != request.head_oid:
        raise SubmissionError(
            f"review worktree HEAD is {head}, expected {request.head_oid}"
        )
    branch = run_git(review_worktree, "symbolic-ref", "--quiet", "HEAD")
    if branch.returncode != 1:
        raise SubmissionError("review worktree must have a detached HEAD")
    tracked = run_git(
        review_worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
    )
    if tracked.returncode != 0 or tracked.stdout:
        raise SubmissionError(
            "review worktree tracked content is not clean before launch"
        )
    ignored = run_git(
        review_worktree,
        "check-ignore",
        "--quiet",
        "--no-index",
        "--",
        ".agent-squad-review/input/request.json",
    )
    if ignored.returncode != 0:
        raise SubmissionError(
            ".agent-squad-review is not excluded in the review worktree"
        )
    visible = run_git(
        review_worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if visible.returncode != 0 or visible.stdout:
        raise SubmissionError(
            "review worktree contains unexpected visible files before launch"
        )

    bundle_root = review_worktree / REVIEW_DIRECTORY_NAME
    expected = (
        (request.task, _read_file(run_directory / "task.md")),
        (request.implementation_report, report.content),
        *tuple(
            (
                artifact,
                _read_file(
                    run_directory.joinpath(
                        *PurePosixPath(artifact.path).parts[1:]
                    )
                ),
            )
            for artifact in request.context_files
        ),
        *additional_inputs,
    )
    for artifact, content in expected:
        path = bundle_root.joinpath(*PurePosixPath(artifact.path).parts)
        _verify_bundle_file(path, artifact.sha256, content)
    request_path = bundle_root / "input/request.json"
    _verify_bundle_file(
        request_path,
        hashlib.sha256(request_bytes).hexdigest(),
        request_bytes,
    )
    try:
        decoded_request = json.loads(request_path.read_text(encoding="utf-8"))
        ReviewRequest.from_dict(decoded_request)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SubmissionError(
            f"review-bundle request cannot be read back: {error}"
        ) from error
    except ArtifactValidationError as error:
        raise SubmissionError(
            f"review-bundle request failed validation: {error}"
        ) from error


def _verify_bundle_file(
    path: Path,
    expected_digest: str,
    content: bytes,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise SubmissionError(
            f"review-bundle input must be a regular non-symlink file: {path}"
        )
    actual = _read_file(path)
    if actual != content:
        raise SubmissionError(f"review-bundle input content mismatch: {path}")
    if hashlib.sha256(actual).hexdigest() != expected_digest:
        raise SubmissionError(f"review-bundle input digest mismatch: {path}")


def _remove_review_worktree(
    implementation: GitWorktree,
    review_worktree: Path,
) -> list[str]:
    errors: list[str] = []
    result = run_git(
        implementation.root,
        "worktree",
        "remove",
        str(review_worktree),
    )
    if result.returncode != 0 and os.path.lexists(review_worktree):
        detail = result.stderr.strip() or "unknown Git error"
        errors.append(
            f"could not remove new review worktree {review_worktree}: "
            f"{detail}"
        )
    return errors


def _remove_owned_directory(path: Path, label: str) -> list[str]:
    if not os.path.lexists(path):
        return []
    try:
        if path.is_symlink():
            raise OSError("refusing to remove a symbolic link")
        shutil.rmtree(path)
        return []
    except OSError as error:
        return [f"could not remove {label} {path}: {error}"]


def _read_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SubmissionError(
            "authoritative artifact must be a regular non-symlink file: "
            f"{path}"
        )
    try:
        return path.read_bytes()
    except OSError as error:
        raise SubmissionError(
            f"cannot read authoritative artifact {path}: {error}"
        ) from error
