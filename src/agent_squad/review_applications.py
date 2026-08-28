"""Implementation-side application and completion of approved reviews."""

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
    ApprovalRecord,
    ArtifactValidationError,
    BundleArtifact,
    ReviewRoundRecord,
    ReviewVerdict,
    RoundStatus,
)
from .initialization import (
    AgentSquadError,
    InitializedRepository,
    REVIEW_DIRECTORY_NAME,
    load_initialized_repository,
    matches_allowed_generated_path,
    run_git,
)
from .review_submissions import (
    MarkerConfirmedReview,
    ReviewSubmissionError,
    load_marker_confirmed_review,
    verify_flagged_tracked_files,
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


APPROVAL_FILE_NAME = "approval.json"
BUNDLE_ARCHIVE_DIRECTORY_NAME = "bundle"
REVIEW_RESULT_FILE_NAME = "review.json"
REVIEW_MARKDOWN_FILE_NAME = "review.md"
REVIEW_MARKER_FILE_NAME = "review-marker.json"


class ReviewApplicationError(AgentSquadError):
    """Raised when review evidence cannot be applied or completed safely."""


@dataclass(frozen=True)
class ApplyReviewResult:
    """Durable outcome of applying one approved review result."""

    run_id: str
    round_number: int
    result_id: str
    verdict: ReviewVerdict
    head_oid: str
    approval_path: Path
    bundle_archive: Path
    replayed: bool
    next_action: str


@dataclass(frozen=True)
class CompleteRunResult:
    """Durable outcome of completing one exactly approved run."""

    run_id: str
    head_oid: str
    already_completed: bool
    cleanup_warnings: tuple[str, ...]


def apply_review(
    start: Path,
    *,
    result_id: str | None = None,
) -> ApplyReviewResult:
    """Apply the active marker-confirmed approved result exactly once."""

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
    if active.phase is runs.RunPhase.APPROVED:
        return _approved_replay(
            repository,
            active,
            presented_result_id=presented_result_id,
        )
    if active.phase is not runs.RunPhase.REVIEWING:
        raise ReviewApplicationError(
            f"run {active.run_id} is in phase {active.phase.value}; "
            "apply-review requires an active reviewing round"
        )
    ready = active.unapplied_result
    if ready is None:
        raise ReviewApplicationError(
            "the active round has no valid marker-confirmed result to apply"
        )
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
            f"head {active.current_head_oid}; no approval was applied"
        )
    active_round = active.active_round
    if active_round is None:
        raise ReviewApplicationError(
            "reviewing state lost its active round before validation"
        )
    try:
        evidence = load_marker_confirmed_review(
            active_round.review_worktree
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
    if evidence.review.verdict is not ReviewVerdict.APPROVED:
        raise ReviewApplicationError(
            f"valid verdict {evidence.review.verdict.value} is not supported "
            "by this command version; no state was changed"
        )

    run_directory = runs.safe_run_directory(
        repository.control_root,
        active.run_id,
    )
    round_directory = (
        run_directory / "rounds" / f"{active.current_round:03d}"
    )
    round_path = round_directory / "round.json"
    state_path = repository.control_root / runs.STATE_FILE_NAME
    run_path = run_directory / runs.RUN_RECORD_FILE_NAME
    state = runs.load_json_object(state_path, "authoritative state")
    run_record = runs.load_json_object(run_path, "active run record")
    round_data = runs.load_json_object(round_path, "active round record")
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
    approval = ApprovalRecord(
        created_at=timestamp,
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
    approval_bytes = encode_json(approval.to_dict())
    approval_artifact = _write_immutable_artifact(
        round_directory / APPROVAL_FILE_NAME,
        approval_bytes,
        path=APPROVAL_FILE_NAME,
    )

    next_round = replace(
        round_record,
        updated_at=timestamp,
        result_id=evidence.review.result_id,
        verdict=ReviewVerdict.APPROVED,
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
    next_run["phase"] = runs.RunPhase.APPROVED.value
    next_state = copy.deepcopy(state)
    next_state.update(
        updated_at=timestamp,
        phase=runs.RunPhase.APPROVED.value,
        approved_head_oid=evidence.review.head_oid,
        active_round=next_active_round.to_dict(),
    )
    _validate_implementation_identity(repository, active)
    if _current_head(
        repository.worktree.root,
        active.git_object_format,
        label="implementation",
    ) != current_head:
        raise ReviewApplicationError(
            "implementation HEAD changed while approval artifacts were being "
            "prepared; no approval was applied"
        )
    _persist_application_transition(
        round_path=round_path,
        run_path=run_path,
        state_path=state_path,
        original_round=round_path.read_bytes(),
        original_run=run_path.read_bytes(),
        next_round=encode_json(next_round.to_dict()),
        next_run=encode_json(next_run),
        next_state=encode_json(next_state),
    )
    try:
        _ensure_event(
            run_directory / runs.EVENT_LOG_FILE_NAME,
            _review_applied_event(approval),
            identity_fields=("event", "run_id", "round", "result_id"),
        )
    except OSError as error:
        raise ReviewApplicationError(
            "the approved result is authoritative, but its event could not "
            f"be recorded: {error}"
        ) from error

    return ApplyReviewResult(
        run_id=active.run_id,
        round_number=active.current_round,
        result_id=evidence.review.result_id,
        verdict=ReviewVerdict.APPROVED,
        head_oid=evidence.review.head_oid,
        approval_path=round_directory / APPROVAL_FILE_NAME,
        bundle_archive=round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME,
        replayed=False,
        next_action="agent-squad complete",
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
    round_directory = (
        run_directory / "rounds" / f"{active.current_round:03d}"
    )
    try:
        _ensure_event(
            run_directory / runs.EVENT_LOG_FILE_NAME,
            _review_applied_event(approval),
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
    )


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
    atomic_write(run_path, encode_json(next_run), mode=0o600)
    try:
        atomic_write(state_path, encode_json(next_state), mode=0o600)
    except OSError as error:
        rollback_error: OSError | None = None
        try:
            atomic_write(run_path, original_run, mode=0o600)
        except OSError as restore_error:
            rollback_error = restore_error
        detail = (
            f"; run-record rollback also failed: {rollback_error}"
            if rollback_error is not None
            else ""
        )
        raise ReviewApplicationError(
            f"could not release the active-run slot: {error}{detail}"
        ) from error
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
    state_path = repository.control_root / runs.STATE_FILE_NAME
    if not os.path.lexists(state_path):
        return None
    state = runs.load_json_object(state_path, "authoritative state")
    if state.get("active_run_id") is not None:
        return None
    if state.get("phase") != runs.RunPhase.COMPLETED.value:
        return None
    terminal_run_id = state.get("terminal_run_id")
    try:
        canonical_run_id = str(uuid.UUID(str(terminal_run_id)))
    except ValueError:
        raise ReviewApplicationError(
            "completed state has no valid terminal run identity"
        ) from None
    if canonical_run_id != terminal_run_id:
        raise ReviewApplicationError(
            "completed state has no canonical terminal run identity"
        )
    run_directory = runs.safe_run_directory(
        repository.control_root,
        canonical_run_id,
    )
    run_record_data = runs.load_json_object(
        run_directory / runs.RUN_RECORD_FILE_NAME,
        "completed run record",
    )
    try:
        run_record = runs._validate_run_record(
            run_record_data,
            run_directory,
            canonical_run_id,
        )
    except runs.RunStateError as error:
        raise ReviewApplicationError(str(error)) from error
    if run_record.phase is not runs.RunPhase.COMPLETED:
        raise ReviewApplicationError(
            "completed state does not match its terminal run record"
        )
    current_identity = runs.repository_identity(repository.worktree)
    identity_comparisons = (
        (
            state.get("implementation_root"),
            str(current_identity.implementation_root),
            "implementation root",
        ),
        (
            state.get("git_common_dir"),
            str(current_identity.git_common_dir),
            "Git common directory",
        ),
        (
            state.get("worktree_git_dir"),
            str(current_identity.worktree_git_dir),
            "worktree Git directory",
        ),
        (
            state.get("repository_id"),
            current_identity.repository_id,
            "repository ID",
        ),
    )
    for actual, expected, label in identity_comparisons:
        if actual != expected:
            raise ReviewApplicationError(
                f"completed state {label} does not match this worktree"
            )
    current_round = state.get("current_round")
    if type(current_round) is not int or current_round < 1:
        raise ReviewApplicationError(
            "completed state has no valid approved round"
        )
    round_directory = run_directory / "rounds" / f"{current_round:03d}"
    try:
        round_record = ReviewRoundRecord.from_dict(
            runs.load_json_object(
                round_directory / "round.json",
                "completed approval round",
            ),
            label="completed approval round",
        )
        approval = runs._validate_approval_artifacts(
            run_directory=run_directory,
            round_record=round_record,
            run_id=canonical_run_id,
            record=run_record,
            approved_head_oid=(
                state.get("approved_head_oid")
                if isinstance(state.get("approved_head_oid"), str)
                else None
            ),
        )
    except (ArtifactValidationError, runs.RunStateError) as error:
        raise ReviewApplicationError(str(error)) from error
    replay_warnings: list[str] = []
    try:
        _ensure_event(
            run_directory / runs.EVENT_LOG_FILE_NAME,
            {
                "timestamp": run_record_data["finished_at"],
                "event": "run_completed",
                "run_id": canonical_run_id,
                "round": round_record.round_number,
                "result_id": approval.result_id,
                "approved_head_oid": approval.head_oid,
            },
            identity_fields=("event", "run_id"),
        )
    except OSError as error:
        replay_warnings.append(
            "the run is completed, but its missing completion event could "
            f"not be recovered: {error}"
        )
    return CompleteRunResult(
        run_id=canonical_run_id,
        head_oid=approval.head_oid,
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
    archive_root = round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME
    manifest = tuple(
        BundleArtifact(
            path=(
                PurePosixPath(BUNDLE_ARCHIVE_DIRECTORY_NAME) / item.path
            ).as_posix(),
            sha256=hashlib.sha256(item.content).hexdigest(),
        )
        for item in evidence.bundle_files
    )
    if os.path.lexists(archive_root):
        _verify_archive(round_directory, manifest)
        return manifest

    staging = Path(
        tempfile.mkdtemp(prefix=".bundle.", dir=round_directory)
    )
    try:
        staging.chmod(0o700)
        for item in evidence.bundle_files:
            destination = staging.joinpath(*item.path.parts)
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            atomic_write(destination, item.content, mode=0o400)
        _verify_staged_archive(staging, manifest)
        staging.replace(archive_root)
        _make_archive_read_only(archive_root)
    except Exception:
        if os.path.lexists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        raise
    _verify_archive(round_directory, manifest)
    return manifest


def _verify_staged_archive(
    staging: Path,
    manifest: tuple[BundleArtifact, ...],
) -> None:
    expected = {
        PurePosixPath(*PurePosixPath(item.path).parts[1:]): item.sha256
        for item in manifest
    }
    actual = _regular_tree_files(staging)
    if set(actual) != set(expected):
        raise ReviewApplicationError(
            "staged review bundle archive is incomplete"
        )
    for path, content in actual.items():
        if hashlib.sha256(content).hexdigest() != expected[path]:
            raise ReviewApplicationError(
                f"staged review bundle digest mismatch for {path}"
            )


def _verify_archive(
    round_directory: Path,
    manifest: tuple[BundleArtifact, ...],
) -> None:
    archive_root = round_directory / BUNDLE_ARCHIVE_DIRECTORY_NAME
    actual = _regular_tree_files(archive_root)
    expected = {
        PurePosixPath(*PurePosixPath(item.path).parts[1:]): item.sha256
        for item in manifest
    }
    if set(actual) != set(expected):
        raise ReviewApplicationError(
            "existing review bundle archive differs from validated evidence"
        )
    for path, content in actual.items():
        if hashlib.sha256(content).hexdigest() != expected[path]:
            raise ReviewApplicationError(
                f"existing review bundle digest mismatch for {path}"
            )


def _regular_tree_files(root: Path) -> dict[PurePosixPath, bytes]:
    try:
        root_status = root.lstat()
    except OSError as error:
        raise ReviewApplicationError(
            f"cannot inspect review archive {root}: {error}"
        ) from error
    if not stat.S_ISDIR(root_status.st_mode):
        raise ReviewApplicationError(
            f"review archive must be a normal directory: {root}"
        )
    files: dict[PurePosixPath, bytes] = {}
    folded: dict[str, PurePosixPath] = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in names:
            path = current / name
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ReviewApplicationError(
                    f"review archive directory must not be a symlink: {path}"
                )
        for name in filenames:
            path = current / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ReviewApplicationError(
                    f"review archive file must not be a symlink: {path}"
                )
            relative = PurePosixPath(path.relative_to(root).as_posix())
            key = str(relative).casefold()
            if key in folded and folded[key] != relative:
                raise ReviewApplicationError(
                    "review archive contains case-colliding paths"
                )
            folded[key] = relative
            files[relative] = path.read_bytes()
    return files


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


def _persist_application_transition(
    *,
    round_path: Path,
    run_path: Path,
    state_path: Path,
    original_round: bytes,
    original_run: bytes,
    next_round: bytes,
    next_run: bytes,
    next_state: bytes,
) -> None:
    round_written = False
    run_written = False
    try:
        atomic_write(round_path, next_round, mode=0o600)
        round_written = True
        atomic_write(run_path, next_run, mode=0o600)
        run_written = True
        atomic_write(state_path, next_state, mode=0o600)
    except OSError as error:
        rollback_errors: list[str] = []
        if run_written:
            try:
                atomic_write(run_path, original_run, mode=0o600)
            except OSError as restore_error:
                rollback_errors.append(f"run record: {restore_error}")
        if round_written:
            try:
                atomic_write(round_path, original_round, mode=0o600)
            except OSError as restore_error:
                rollback_errors.append(f"round record: {restore_error}")
        detail = (
            "; rollback also failed for " + ", ".join(rollback_errors)
            if rollback_errors
            else ""
        )
        raise ReviewApplicationError(
            f"could not persist approved review state: {error}{detail}"
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
        and not _is_agent_squad_runtime_path(entry)
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


def _cleanup_review_resources(
    repository: InitializedRepository,
    active: runs.ActiveRunStatus,
) -> tuple[str, ...]:
    active_round = active.active_round
    if active_round is None:
        return ("approved run did not retain its review-worktree identity",)
    review_worktree = active_round.review_worktree
    if not os.path.lexists(review_worktree):
        return ()
    try:
        evidence = load_marker_confirmed_review(review_worktree)
        _validate_evidence(repository, active, evidence)
    except AgentSquadError as error:
        return (
            "retained review worktree because its applied evidence could not "
            f"be revalidated: {error}",
        )

    warnings: list[str] = []
    bundle_root = review_worktree / REVIEW_DIRECTORY_NAME
    try:
        shutil.rmtree(bundle_root)
    except OSError as error:
        return (
            f"could not remove archived review bundle {bundle_root}: {error}",
        )
    for configured in repository.configuration.allowed_generated_paths:
        warning = _remove_generated_path(review_worktree, configured)
        if warning is not None:
            warnings.append(warning)
    if warnings:
        return tuple(warnings)

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
            f"could not verify review worktree cleanup: {detail}",
        )
    if cleanliness.stdout:
        return (
            "retained review worktree because files remain after scoped "
            "cleanup",
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
            f"could not remove review worktree {review_worktree}: {detail}",
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


def _review_applied_event(approval: ApprovalRecord) -> dict[str, object]:
    return {
        "timestamp": approval.created_at,
        "event": "review_applied",
        "run_id": approval.run_id,
        "round": approval.round_number,
        "request_id": approval.request_id,
        "result_id": approval.result_id,
        "verdict": ReviewVerdict.APPROVED.value,
        "head_oid": approval.head_oid,
    }


def _ensure_event(
    path: Path,
    event: dict[str, object],
    *,
    identity_fields: tuple[str, ...],
) -> None:
    """Append one logical event unless the exact event already exists."""

    if path.is_symlink() or not path.is_file():
        raise OSError(f"event log is not a regular non-symlink file: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise OSError(f"cannot inspect event log {path}: {error}") from error
    matches: list[dict[str, object]] = []
    for index, line in enumerate(lines, start=1):
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
        return
    append_event(path, event)


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


def _is_agent_squad_runtime_path(candidate: str) -> bool:
    path = PurePosixPath(candidate)
    return bool(path.parts) and path.parts[0] in {
        ".agent-squad",
        ".agent-squad-review",
    }
