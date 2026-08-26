"""Durable first-round review submission for an exact Git revision."""

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
import uuid

from . import runs
from .artifacts import (
    ActiveRoundRecord,
    ArtifactValidationError,
    BundleArtifact,
    HandoffStatus,
    ReviewRequest,
    RoundStatus,
    SubmissionMode,
)
from .herdr import HerdrClient, HerdrError, HerdrInstallation
from .initialization import (
    AgentSquadError,
    GitWorktree,
    InitializedRepository,
    SCHEMA_VERSION,
    load_initialized_repository,
    run_git,
)
from .storage import (
    atomic_write,
    encode_event,
    encode_json,
    exclusive_file_lock,
    utc_timestamp,
)
from .validation import JsonValidator, OID_LENGTHS


ROUNDS_DIRECTORY_NAME = "rounds"
ROUND_RECORD_FILE_NAME = "round.json"
REQUEST_FILE_NAME = "request.json"
IMPLEMENTATION_REPORT_FILE_NAME = "implementation-report.md"
REVIEW_BUNDLE_DIRECTORY_NAME = ".agent-squad-review"
SENSITIVE_ROOT_FILES = {".github/copilot-instructions.md"}


class SubmissionError(AgentSquadError):
    """Raised when a review request cannot be prepared safely."""


_VALIDATOR = JsonValidator(SubmissionError)


@dataclass(frozen=True)
class ImplementationReport:
    """Validated report content staged for the exact review round."""

    source_path: Path
    content: bytes
    sha256: str


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
    herdr_client: HerdrClient | None = None,
) -> SubmitResult:
    """Persist a first review round, then attempt its Herdr notification."""

    repository = load_initialized_repository(start)
    selected_mode = _submission_mode(mode)
    lock_path = repository.control_root / runs.LOCK_FILE_NAME
    try:
        with exclusive_file_lock(lock_path):
            prepared = _prepare_submission_locked(
                repository,
                report_path=report_path,
                mode=selected_mode,
            )
            client = herdr_client or HerdrClient(repository.worktree.root)
            installation: HerdrInstallation | None = None
            try:
                installation = client.discover(
                    prepared.request.reviewer_kind
                )
                client.dispatch_review_request(
                    reviewer_name=prepared.request.reviewer_name,
                    reviewer_kind=prepared.request.reviewer_kind,
                    start_args=prepared.reviewer_start_args,
                    review_worktree=prepared.review_worktree,
                    prompt=_review_request_prompt(prepared),
                )
            except HerdrError as error:
                detail = _single_line(str(error))
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


def deterministic_reviewer_name(run_id: str, round_number: int) -> str:
    """Return a stable Herdr-safe Reviewer name for one logical round."""

    try:
        canonical_run_id = str(uuid.UUID(run_id))
    except ValueError:
        raise SubmissionError("run ID must be a canonical UUID") from None
    if canonical_run_id != run_id:
        raise SubmissionError("run ID must be a canonical UUID")
    if round_number < 1:
        raise SubmissionError("review round must be positive")
    name = f"asq-{run_id.replace('-', '')[:12]}-r{round_number:03d}-reviewer"
    if len(name) > 32:
        raise SubmissionError("deterministic Reviewer name exceeds 32 bytes")
    return name


def _prepare_submission_locked(
    repository: InitializedRepository,
    *,
    report_path: Path,
    mode: SubmissionMode,
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
    if (
        active.current_round != 0
        or active.current_head_oid is not None
        or active.round_status is not None
        or active.handoff_status is not None
    ):
        raise SubmissionError(
            "the first submission requires an unused run with no existing "
            "round"
        )
    _validate_branch_identity(active.repository, repository.worktree)

    report = _capture_report(
        report_path,
        repository.worktree.invocation_directory,
    )
    _validate_implementation_cleanliness(
        repository,
        report_source=report.source_path,
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
    _validate_first_submission_mode(
        mode,
        base_oid=active.base_oid,
        head_oid=head_oid,
    )
    warnings = _sensitive_change_warnings(
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
    run_record = runs.load_json_object(run_record_path, "active run record")
    original_run_record = run_record_path.read_bytes()

    round_number = 1
    request_id = str(uuid.uuid4())
    timestamp = utc_timestamp()
    reviewer_name = deterministic_reviewer_name(
        active.run_id,
        round_number,
    )
    review_worktree = _review_worktree_path(
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
        previous_review_path=None,
        previous_response_path=None,
        resolution_paths=(),
        review_output_path="output/review.json",
        review_markdown_path="output/review.md",
        implementer_agent=active.implementer_agent,
        implementer_kind=active.implementer_kind,
        reviewer_kind=active.reviewer_kind,
        reviewer_name=reviewer_name,
    )
    try:
        request = ReviewRequest.from_dict(request.to_dict())
    except ArtifactValidationError as error:
        raise SubmissionError(
            f"cannot build review request: {error}"
        ) from error
    request_bytes = encode_json(request.to_dict())
    request_digest = hashlib.sha256(request_bytes).hexdigest()
    pending_handoff = _handoff_record(
        round_number=round_number,
        target=reviewer_name,
        status=HandoffStatus.PENDING,
        timestamp=timestamp,
        error=None,
        installation=None,
    )
    round_record = _new_round_record(
        request=request,
        review_worktree=review_worktree,
        reviewer_start_args=active.reviewer_start_args,
        request_digest=request_digest,
        warnings=warnings,
    )

    rounds_root = run_directory / ROUNDS_DIRECTORY_NAME
    rounds_root_created = False
    staging_directory: Path | None = None
    round_directory = rounds_root / f"{round_number:03d}"
    worktree_created = False
    bundle_created = False
    bundle_root = review_worktree / REVIEW_BUNDLE_DIRECTORY_NAME
    round_directory_committed = False
    run_record_written = False
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
            encode_json(round_record),
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
        )
        _verify_review_worktree(
            review_worktree,
            request=request,
            request_bytes=request_bytes,
            run_directory=run_directory,
            report=report,
        )
        _validate_branch_identity(active.repository, repository.worktree)
        _validate_implementation_cleanliness(
            repository,
            report_source=report.source_path,
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
            handoff=pending_handoff,
        )

        staging_directory.replace(round_directory)
        staging_directory = None
        round_directory_committed = True
        atomic_write(
            run_record_path,
            encode_json(next_run_record),
            mode=0o600,
        )
        run_record_written = True
        atomic_write(
            state_path,
            encode_json(next_state),
            mode=0o600,
        )
        commit_point_reached = True
        _append_event(
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
    except Exception as error:
        if commit_point_reached:
            if isinstance(error, SubmissionError):
                raise
            raise SubmissionError(
                "the review request is durable, but its persisted event "
                f"could not be recorded: {error}"
            ) from error
        cleanup_errors: list[str] = []
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
            raise SubmissionError(f"{error}.{cleanup_note}") from error
        raise SubmissionError(
            f"could not prepare the review request: {error}.{cleanup_note}"
        ) from error

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
    timestamp = utc_timestamp()
    handoff = _handoff_record(
        round_number=prepared.round_number,
        target=prepared.request.reviewer_name,
        status=status,
        timestamp=timestamp,
        error=error,
        installation=installation,
    )
    state_path = prepared.repository.control_root / runs.STATE_FILE_NAME
    state = runs.load_json_object(state_path, "authoritative state")
    if state.get("active_run_id") != prepared.run_id:
        raise SubmissionError(
            "active run changed before the review handoff was recorded"
        )
    active_round = state.get("active_round")
    if not isinstance(active_round, dict) or (
        active_round.get("request_id") != prepared.request.request_id
    ):
        raise SubmissionError(
            "active round changed before the review handoff was recorded"
        )

    next_state = copy.deepcopy(state)
    next_state["updated_at"] = timestamp
    next_state["handoff"] = handoff
    try:
        atomic_write(state_path, encode_json(next_state), mode=0o600)
    except OSError as write_error:
        raise SubmissionError(
            f"could not record review handoff state: {write_error}"
        ) from write_error

    try:
        _append_event(
            prepared.round_directory.parent.parent / runs.EVENT_LOG_FILE_NAME,
            {
                "timestamp": timestamp,
                "event": (
                    "review_request_sent"
                    if status is HandoffStatus.SENT
                    else "review_request_failed"
                ),
                "run_id": prepared.run_id,
                "round": prepared.round_number,
                "request_id": prepared.request.request_id,
                "target": prepared.request.reviewer_name,
                "error": error,
            },
        )
    except OSError as event_error:
        raise SubmissionError(
            "the handoff state is durable, but its event could not be "
            f"recorded: {event_error}"
        ) from event_error


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


def _validate_implementation_cleanliness(
    repository: InitializedRepository,
    *,
    report_source: Path,
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
    report_relative = _relative_to_repository(report_source, root)
    unexpected = [
        entry
        for entry in untracked.stdout.split("\x00")
        if entry
        and entry != report_relative
        and not _is_agent_squad_runtime_path(entry)
        and not _matches_allowed_generated_path(
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


def _is_agent_squad_runtime_path(candidate: str) -> bool:
    path = PurePosixPath(candidate)
    return bool(path.parts) and path.parts[0] in {
        ".agent-squad",
        ".agent-squad-review",
    }


def _relative_to_repository(path: Path, root: Path) -> str | None:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _matches_allowed_generated_path(
    candidate: str,
    configured_paths: tuple[str, ...],
) -> bool:
    path = PurePosixPath(candidate)
    for configured in configured_paths:
        allowed = PurePosixPath(configured)
        if (
            path == allowed
            or path.parts[: len(allowed.parts)] == allowed.parts
        ):
            return True
    return False


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


def _review_worktree_path(
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
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        atomic_write(destination, _read_file(source), mode=0o400)

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

    bundle_root = review_worktree / REVIEW_BUNDLE_DIRECTORY_NAME
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


def _new_round_record(
    *,
    request: ReviewRequest,
    review_worktree: Path,
    reviewer_start_args: tuple[str, ...],
    request_digest: str,
    warnings: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": request.created_at,
        "updated_at": request.created_at,
        "run_id": request.run_id,
        "round": request.round_number,
        "mode": request.mode.value,
        "request_id": request.request_id,
        "result_id": None,
        "base_oid": request.base_oid,
        "head_oid": request.head_oid,
        "git_object_format": request.object_format,
        "status": RoundStatus.REVIEWING.value,
        "review_worktree": str(review_worktree),
        "reviewer": {
            "name": request.reviewer_name,
            "kind": request.reviewer_kind.value,
            "start_args": list(reviewer_start_args),
        },
        "artifacts": {
            "request": {
                "path": REQUEST_FILE_NAME,
                "sha256": request_digest,
            },
            "implementation_report": {
                "path": IMPLEMENTATION_REPORT_FILE_NAME,
                "sha256": request.implementation_report.sha256,
            },
            "bundle_inputs": [
                {
                    "path": "input/request.json",
                    "sha256": request_digest,
                },
                request.task.to_dict(),
                request.implementation_report.to_dict(),
                *[
                    artifact.to_dict()
                    for artifact in request.context_files
                ],
            ],
        },
        "warnings": list(warnings),
    }


def _handoff_record(
    *,
    round_number: int,
    target: str,
    status: HandoffStatus,
    timestamp: str,
    error: str | None,
    installation: HerdrInstallation | None,
) -> dict[str, object]:
    return {
        "kind": "review_request",
        "round": round_number,
        "status": status.value,
        "target": target,
        "last_error": error,
        "updated_at": timestamp,
        "herdr_version": (
            installation.version if installation is not None else None
        ),
        "herdr_protocol": (
            installation.protocol if installation is not None else None
        ),
    }


def _review_request_prompt(prepared: _PreparedSubmission) -> str:
    request = prepared.request
    request_path = (
        prepared.review_worktree
        / REVIEW_BUNDLE_DIRECTORY_NAME
        / "input"
        / REQUEST_FILE_NAME
    )
    return (
        "AGENT_SQUAD/0.4.4 REVIEW_REQUEST\n\n"
        f"run_id: {request.run_id}\n"
        f"round: {request.round_number}\n"
        f"request_id: {request.request_id}\n"
        f"base_oid: {request.base_oid}\n"
        f"head_oid: {request.head_oid}\n"
        f"review_worktree: {prepared.review_worktree}\n"
        f"request: {request_path}\n\n"
        "Review the exact requested revision in this worktree.\n"
        "Read the complete local review bundle, including any Developer "
        "resolutions.\n"
        "Do not modify tracked files.\n"
        "Write the required review artifacts and run agent-squad "
        "review-submit."
    )


def _append_event(path: Path, event: dict[str, object]) -> None:
    content = encode_event(event)
    flags = os.O_WRONLY | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"event log is not a regular file: {path}")
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise OSError(f"short write to event log: {path}")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


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


def _single_line(value: str) -> str:
    return " ".join(value.splitlines()) or "unknown Herdr error"
