"""Reviewer-side validation, marking, and result notification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

from .artifacts import (
    ArtifactValidationError,
    BundleArtifact,
    DeveloperResolution,
    ReviewRequest,
    ReviewerLocalMarker,
    ReviewResult,
    ReviewVerdict,
    deterministic_reviewer_name,
)
from .herdr import HerdrClient, HerdrError, format_herdr_error
from .initialization import (
    AgentSquadError,
    GitWorktree,
    REVIEW_DIRECTORY_NAME,
    SCHEMA_VERSION,
    discover_git_worktree,
    matches_allowed_generated_path,
    run_git,
)
from .storage import (
    InvalidJsonError,
    atomic_write,
    decode_json,
    encode_json,
    exclusive_file_lock,
    read_regular_tree,
    utc_timestamp,
)
from .validation import JsonValidator


REQUEST_PATH = PurePosixPath("input/request.json")
MARKER_PATH = PurePosixPath("local-state.json")
SUBMISSION_LOCK_PATH = PurePosixPath("output/.review-submit.lock")


class ReviewSubmissionError(AgentSquadError):
    """Raised when a Reviewer result cannot be marked safely."""


_VALIDATOR = JsonValidator(ReviewSubmissionError)


@dataclass(frozen=True)
class ReviewSubmitResult:
    """Outcome of marking a result and attempting its notification."""

    run_id: str
    round_number: int
    request_id: str
    result_id: str
    head_oid: str
    verdict: ReviewVerdict
    review_path: Path
    marker_path: Path
    marker_created: bool
    notification_sent: bool
    notification_error: str | None


@dataclass(frozen=True)
class MarkerConfirmedReview:
    """Fully revalidated evidence for one marker-confirmed result."""

    worktree: GitWorktree
    request: ReviewRequest
    review: ReviewResult
    review_bytes: bytes
    review_path: Path
    review_markdown_bytes: bytes
    marker: ReviewerLocalMarker
    marker_bytes: bytes
    bundle_files: tuple[ReviewBundleFile, ...]


@dataclass(frozen=True)
class ReviewBundleFile:
    """One regular file captured from a validated review bundle."""

    path: PurePosixPath
    content: bytes


def submit_review_result(
    start: Path,
    *,
    herdr_client: HerdrClient | None = None,
) -> ReviewSubmitResult:
    """Validate, marker-confirm, and notify one prepared review result."""

    worktree = discover_git_worktree(start)
    bundle_root = worktree.root / REVIEW_DIRECTORY_NAME
    _require_normal_directory(bundle_root, "review bundle")
    input_root = bundle_root / "input"
    output_root = bundle_root / "output"
    _require_normal_directory(input_root, "review bundle input")
    _require_normal_directory(output_root, "review bundle output")
    lock_path = bundle_root.joinpath(*SUBMISSION_LOCK_PATH.parts)

    try:
        with exclusive_file_lock(lock_path):
            request = _load_review_request(bundle_root)
            _validate_request_identity(worktree, request)
            _validate_worktree_integrity(worktree, request)
            previous_review = _validate_bundle_inputs(bundle_root, request)
            _validate_bundle_root_entries(bundle_root)
            _validate_output_tree(output_root)
            review, review_bytes, review_path = _load_review_result(
                bundle_root,
                request,
            )
            _validate_review_markdown(bundle_root, request)
            _assert_result_identity(request, review)
            _assert_fresh_result_id(previous_review, review)

            review_digest = hashlib.sha256(review_bytes).hexdigest()
            marker_path = bundle_root.joinpath(*MARKER_PATH.parts)
            marker, marker_created = _write_or_validate_marker(
                marker_path,
                request=request,
                review=review,
                review_digest=review_digest,
            )

            client = herdr_client or HerdrClient(worktree.root)
            try:
                client.discover(
                    request.implementer_kind,
                    role="Implementer",
                )
                client.dispatch_review_result(
                    implementer_name=request.implementer_agent,
                    implementer_kind=request.implementer_kind,
                    prompt=_review_result_prompt(
                        request=request,
                        result_id=marker.result_id,
                        review_path=review_path,
                    ),
                )
            except HerdrError as error:
                notification_error = format_herdr_error(str(error))
            else:
                notification_error = None

            return ReviewSubmitResult(
                run_id=request.run_id,
                round_number=request.round_number,
                request_id=request.request_id,
                result_id=marker.result_id,
                head_oid=request.head_oid,
                verdict=review.verdict,
                review_path=review_path,
                marker_path=marker_path,
                marker_created=marker_created,
                notification_sent=notification_error is None,
                notification_error=notification_error,
            )
    except AgentSquadError:
        raise
    except ArtifactValidationError as error:
        raise ReviewSubmissionError(str(error)) from error
    except OSError as error:
        raise ReviewSubmissionError(
            f"cannot validate or mark the local review result: {error}"
        ) from error


def load_marker_confirmed_review(start: Path) -> MarkerConfirmedReview:
    """Independently validate one submitted result without notifying anyone."""

    worktree = discover_git_worktree(start)
    bundle_root = worktree.root / REVIEW_DIRECTORY_NAME
    _require_normal_directory(bundle_root, "review bundle")
    input_root = bundle_root / "input"
    output_root = bundle_root / "output"
    _require_normal_directory(input_root, "review bundle input")
    _require_normal_directory(output_root, "review bundle output")
    lock_path = bundle_root.joinpath(*SUBMISSION_LOCK_PATH.parts)
    _read_regular_file(lock_path, "review submission lock")

    try:
        with exclusive_file_lock(lock_path):
            _require_normal_directory(bundle_root, "review bundle")
            _require_normal_directory(input_root, "review bundle input")
            _require_normal_directory(output_root, "review bundle output")
            request = _load_review_request(bundle_root)
            _validate_request_identity(worktree, request)
            _validate_worktree_integrity(worktree, request)
            previous_review = _validate_bundle_inputs(bundle_root, request)
            _validate_bundle_root_entries(bundle_root)
            _validate_output_tree(output_root)
            review, review_bytes, review_path = _load_review_result(
                bundle_root,
                request,
            )
            markdown_bytes = _validate_review_markdown(
                bundle_root,
                request,
            )
            _assert_result_identity(request, review)
            _assert_fresh_result_id(previous_review, review)

            marker_path = bundle_root.joinpath(*MARKER_PATH.parts)
            marker_value, marker_bytes = _load_json_file(
                marker_path,
                "review marker",
            )
            try:
                marker = ReviewerLocalMarker.from_dict(marker_value)
            except ArtifactValidationError as error:
                raise ReviewSubmissionError(
                    f"review marker failed validation: {error}"
                ) from error
            comparisons = (
                (marker.request_id, request.request_id, "request ID"),
                (marker.result_id, review.result_id, "result ID"),
                (
                    marker.review_json_path,
                    request.review_output_path,
                    "review path",
                ),
                (
                    marker.review_sha256,
                    hashlib.sha256(review_bytes).hexdigest(),
                    "review digest",
                ),
            )
            for actual, expected, label in comparisons:
                if actual != expected:
                    raise ReviewSubmissionError(
                        f"review marker {label} does not match the validated "
                        "result"
                    )
            captured_files = read_regular_tree(
                bundle_root,
                label="review bundle",
                error_type=ReviewSubmissionError,
            ).files
            bundle_files = tuple(
                ReviewBundleFile(
                    path=path,
                    content=content,
                )
                for path, content in sorted(
                    captured_files.items(),
                    key=lambda item: str(item[0]),
                )
            )
            return MarkerConfirmedReview(
                worktree=worktree,
                request=request,
                review=review,
                review_bytes=review_bytes,
                review_path=review_path,
                review_markdown_bytes=markdown_bytes,
                marker=marker,
                marker_bytes=marker_bytes,
                bundle_files=bundle_files,
            )
    except AgentSquadError:
        raise
    except ArtifactValidationError as error:
        raise ReviewSubmissionError(str(error)) from error
    except OSError as error:
        raise ReviewSubmissionError(
            f"cannot validate the marker-confirmed review result: {error}"
        ) from error


def _load_review_request(bundle_root: Path) -> ReviewRequest:
    request_path = bundle_root.joinpath(*REQUEST_PATH.parts)
    value, _ = _load_json_file(request_path, "review request")
    try:
        return ReviewRequest.from_dict(value)
    except ArtifactValidationError as error:
        raise ReviewSubmissionError(
            f"review request failed validation: {error}"
        ) from error


def _validate_request_identity(
    worktree: GitWorktree,
    request: ReviewRequest,
) -> None:
    expected_name = deterministic_reviewer_name(
        request.run_id,
        request.round_number,
    )
    if request.reviewer_name != expected_name:
        raise ReviewSubmissionError(
            "review request Reviewer name does not match its run and round"
        )
    expected_round_name = f"round-{request.round_number:03d}"
    if worktree.root.name != expected_round_name:
        raise ReviewSubmissionError(
            f"review-submit must run from {expected_round_name}, not "
            f"{worktree.root.name}"
        )
    if worktree.root.parent.name != request.run_id:
        raise ReviewSubmissionError(
            "review worktree path does not match the request run ID"
        )


def _validate_worktree_integrity(
    worktree: GitWorktree,
    request: ReviewRequest,
) -> None:
    object_format = _git_output(
        worktree.root,
        ("rev-parse", "--show-object-format"),
        "determine the review worktree Git object format",
    )
    if object_format != request.object_format:
        raise ReviewSubmissionError(
            "review worktree Git object format does not match the request"
        )
    head_oid = _git_output(
        worktree.root,
        ("rev-parse", "--verify", "HEAD^{commit}"),
        "resolve the review worktree HEAD",
    )
    if head_oid != request.head_oid:
        raise ReviewSubmissionError(
            f"review worktree HEAD is {head_oid}, expected "
            f"{request.head_oid}"
        )
    branch = run_git(
        worktree.root,
        "symbolic-ref",
        "--quiet",
        "HEAD",
    )
    if branch.returncode == 0:
        raise ReviewSubmissionError(
            "review-submit requires the expected detached review worktree"
        )
    if branch.returncode != 1:
        detail = branch.stderr.strip() or "unknown Git error"
        raise ReviewSubmissionError(
            f"could not verify detached review HEAD: {detail}"
        )
    base_commit = run_git(
        worktree.root,
        "cat-file",
        "-e",
        f"{request.base_oid}^{{commit}}",
    )
    if base_commit.returncode != 0:
        raise ReviewSubmissionError(
            "review request base object is not an available commit"
        )
    ancestor = run_git(
        worktree.root,
        "merge-base",
        "--is-ancestor",
        request.base_oid,
        request.head_oid,
    )
    if ancestor.returncode == 1:
        raise ReviewSubmissionError(
            "review request base is not an ancestor of its head"
        )
    if ancestor.returncode != 0:
        detail = ancestor.stderr.strip() or "unknown Git error"
        raise ReviewSubmissionError(
            f"could not verify the requested review range: {detail}"
        )

    index_diff = run_git(
        worktree.root,
        "diff",
        "--quiet",
        "--no-ext-diff",
        "--ignore-submodules=none",
        "--cached",
        "HEAD",
        "--",
    )
    if index_diff.returncode == 1:
        raise ReviewSubmissionError("review worktree index differs from HEAD")
    if index_diff.returncode != 0:
        detail = index_diff.stderr.strip() or "unknown Git error"
        raise ReviewSubmissionError(
            f"could not verify the review worktree index: {detail}"
        )
    worktree_diff = run_git(
        worktree.root,
        "diff",
        "--quiet",
        "--no-ext-diff",
        "--ignore-submodules=none",
        "HEAD",
        "--",
    )
    if worktree_diff.returncode == 1:
        raise ReviewSubmissionError(
            "review worktree tracked files differ from HEAD"
        )
    if worktree_diff.returncode != 0:
        detail = worktree_diff.stderr.strip() or "unknown Git error"
        raise ReviewSubmissionError(
            f"could not verify tracked review content: {detail}"
        )
    verify_flagged_tracked_files(worktree.root, request.object_format)

    visible = run_git(
        worktree.root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if visible.returncode != 0:
        detail = visible.stderr.strip() or "unknown Git error"
        raise ReviewSubmissionError(
            f"could not inspect review worktree output: {detail}"
        )
    unexpected_visible = _unexpected_worktree_entries(
        visible.stdout,
        request.allowed_generated_paths,
    )
    if unexpected_visible:
        raise ReviewSubmissionError(
            "review worktree contains tracked changes or unexpected "
            "non-ignored files outside the review bundle"
        )
    ignored = run_git(
        worktree.root,
        "check-ignore",
        "--quiet",
        "--no-index",
        "--",
        str(PurePosixPath(REVIEW_DIRECTORY_NAME) / REQUEST_PATH),
    )
    if ignored.returncode != 0:
        raise ReviewSubmissionError(
            ".agent-squad-review is not Git-excluded in the review worktree"
        )


def _unexpected_worktree_entries(
    status_output: str,
    allowed_generated_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """Return visible entries not covered by the generated-path policy."""

    unexpected: list[str] = []
    for entry in status_output.split("\0"):
        if not entry:
            continue
        if entry.startswith("?? ") and matches_allowed_generated_path(
            entry[3:],
            allowed_generated_paths,
        ):
            continue
        unexpected.append(entry)
    return tuple(unexpected)


def verify_flagged_tracked_files(
    repository_root: Path,
    object_format: str,
) -> None:
    tracked = run_git(
        repository_root,
        "ls-files",
        "-v",
        "-z",
        "--stage",
    )
    if tracked.returncode != 0:
        detail = tracked.stderr.strip() or "unknown Git error"
        raise ReviewSubmissionError(
            f"could not inspect tracked-file flags: {detail}"
        )
    for record in tracked.stdout.split("\0"):
        if not record:
            continue
        try:
            metadata, path_text = record.split("\t", 1)
            tag, mode, expected_oid, stage_number = metadata.split(" ", 3)
        except ValueError:
            raise ReviewSubmissionError(
                "Git returned malformed tracked-file metadata"
            ) from None
        if stage_number != "0":
            raise ReviewSubmissionError(
                f"review worktree has an unmerged index entry: {path_text}"
            )
        if tag == "H":
            continue
        if mode == "160000":
            continue
        if mode not in {"100644", "100755", "120000"}:
            raise ReviewSubmissionError(
                f"Git returned unsupported tracked entry {path_text!r}"
            )
        path = _tracked_path(repository_root, path_text)
        try:
            status = path.lstat()
        except FileNotFoundError as error:
            raise ReviewSubmissionError(
                f"tracked review file is missing: {path_text}"
            ) from error
        except OSError as error:
            raise ReviewSubmissionError(
                f"cannot inspect tracked review file {path_text}: {error}"
            ) from error

        if mode == "120000":
            if not stat.S_ISLNK(status.st_mode):
                raise ReviewSubmissionError(
                    f"tracked review symlink changed type: {path_text}"
                )
            try:
                content = os.fsencode(os.readlink(path))
            except OSError as error:
                raise ReviewSubmissionError(
                    f"cannot read tracked review symlink {path_text}: {error}"
                ) from error
            actual_oid = _blob_oid(content, object_format)
        else:
            if not stat.S_ISREG(status.st_mode):
                raise ReviewSubmissionError(
                    f"tracked review file changed type: {path_text}"
                )
            expected_executable = mode == "100755"
            actual_executable = bool(status.st_mode & 0o111)
            if actual_executable != expected_executable:
                raise ReviewSubmissionError(
                    f"tracked review file mode differs from HEAD: {path_text}"
                )
            hashed = run_git(
                repository_root,
                "hash-object",
                f"--path={path_text}",
                "--",
                path_text,
            )
            if hashed.returncode != 0:
                detail = hashed.stderr.strip() or "unknown Git error"
                raise ReviewSubmissionError(
                    f"could not hash tracked review file {path_text}: "
                    f"{detail}"
                )
            actual_oid = hashed.stdout.strip()
        if actual_oid != expected_oid:
            raise ReviewSubmissionError(
                f"tracked review file differs from HEAD: {path_text}"
            )


def _tracked_path(repository_root: Path, path_text: str) -> Path:
    relative = PurePosixPath(path_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReviewSubmissionError(
            "Git returned an unsafe tracked-file path"
        )
    current = repository_root
    relative_directory = PurePosixPath()
    for part in relative.parts[:-1]:
        current /= part
        relative_directory /= part
        try:
            status = current.lstat()
        except OSError as error:
            raise ReviewSubmissionError(
                "cannot inspect tracked review directory "
                f"{relative_directory}: {error}"
            ) from error
        if not stat.S_ISDIR(status.st_mode):
            raise ReviewSubmissionError(
                "tracked review directory changed type: "
                f"{relative_directory}"
            )
    return repository_root.joinpath(*relative.parts)


def _blob_oid(content: bytes, object_format: str) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    if object_format == "sha1":
        return hashlib.sha1(header + content).hexdigest()
    if object_format == "sha256":
        return hashlib.sha256(header + content).hexdigest()
    raise ReviewSubmissionError(
        f"unsupported Git object format: {object_format!r}"
    )


def _validate_bundle_inputs(
    bundle_root: Path,
    request: ReviewRequest,
) -> ReviewResult | None:
    input_root = bundle_root / "input"
    input_tree = read_regular_tree(
        input_root,
        label="review bundle",
        error_type=ReviewSubmissionError,
    )
    actual_paths = set(input_tree.files)
    actual_directories = set(input_tree.directories)
    actual_paths = {
        PurePosixPath("input") / path for path in actual_paths
    }
    actual_directories = {
        PurePosixPath("input") / path for path in actual_directories
    }
    expected_paths = {REQUEST_PATH}
    previous_review: ReviewResult | None = None
    artifacts = (
        request.task,
        request.implementation_report,
        *request.context_files,
    )
    for artifact in artifacts:
        expected_paths.add(PurePosixPath(artifact.path))
        _verify_artifact(bundle_root, artifact)

    if request.previous_review_path is not None:
        previous_path = PurePosixPath(request.previous_review_path)
        expected_paths.add(previous_path)
        previous_value, _ = _load_json_file(
            bundle_root.joinpath(*previous_path.parts),
            "previous review",
        )
        try:
            previous_review = ReviewResult.from_dict(
                previous_value,
                object_format=request.object_format,
            )
        except ArtifactValidationError as error:
            raise ReviewSubmissionError(
                f"previous review failed validation: {error}"
            ) from error
        if previous_review.run_id != request.run_id:
            raise ReviewSubmissionError(
                "previous review run ID does not match the request"
            )
        if previous_review.round_number >= request.round_number:
            raise ReviewSubmissionError(
                "previous review round must precede the requested round"
            )

    if request.previous_response_path is not None:
        previous_response_path = PurePosixPath(
            request.previous_response_path
        )
        expected_paths.add(previous_response_path)
        response, _ = _load_json_file(
            bundle_root.joinpath(*previous_response_path.parts),
            "previous response",
        )
        response_data = _VALIDATOR.require_object(
            response,
            "previous response",
        )
        schema_version = _VALIDATOR.require_int(
            response_data.get("schema_version"),
            "previous response.schema_version",
        )
        if schema_version != SCHEMA_VERSION:
            raise ReviewSubmissionError(
                f"previous response.schema_version must be {SCHEMA_VERSION}"
            )

    resolution_times: list[datetime] = []
    resolution_ids: set[str] = set()
    for index, path_text in enumerate(request.resolution_paths):
        resolution_path = PurePosixPath(path_text)
        expected_paths.add(resolution_path)
        value, _ = _load_json_file(
            bundle_root.joinpath(*resolution_path.parts),
            f"Developer resolution {index + 1}",
        )
        try:
            resolution = DeveloperResolution.from_dict(
                value,
                label=f"Developer resolution {index + 1}",
            )
        except ArtifactValidationError as error:
            raise ReviewSubmissionError(
                f"Developer resolution {index + 1} failed validation: "
                f"{error}"
            ) from error
        if resolution.run_id != request.run_id:
            raise ReviewSubmissionError(
                f"Developer resolution {index + 1} run ID does not match "
                "the request"
            )
        if resolution.resolution_id in resolution_ids:
            raise ReviewSubmissionError(
                "review request contains duplicate Developer resolution IDs"
            )
        resolution_ids.add(resolution.resolution_id)
        created_at = datetime.fromisoformat(
            f"{resolution.created_at[:-1]}+00:00"
        )
        if resolution_times and created_at <= resolution_times[-1]:
            raise ReviewSubmissionError(
                "Developer resolutions must be ordered by strictly "
                "increasing created_at"
            )
        resolution_times.append(created_at)
        companion = resolution_path.parent / resolution.resolution_path
        expected_paths.add(companion)
        _verify_digest(
            bundle_root.joinpath(*companion.parts),
            resolution.resolution_sha256,
            f"Developer resolution {index + 1} companion",
        )

    _reject_case_collisions(expected_paths, "review bundle input paths")
    if actual_paths != expected_paths:
        unexpected = sorted(
            str(path) for path in actual_paths - expected_paths
        )
        missing = sorted(str(path) for path in expected_paths - actual_paths)
        details: list[str] = []
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        raise ReviewSubmissionError(
            "review bundle input files do not match the request"
            + (f" ({'; '.join(details)})" if details else "")
        )
    expected_directories = {
        parent
        for path in expected_paths
        for parent in path.parents
        if parent not in {PurePosixPath("."), PurePosixPath("input")}
    }
    if actual_directories != expected_directories:
        raise ReviewSubmissionError(
            "review bundle input contains unexpected or missing directories"
        )
    return previous_review


def _load_review_result(
    bundle_root: Path,
    request: ReviewRequest,
) -> tuple[ReviewResult, bytes, Path]:
    review_relative = PurePosixPath(request.review_output_path)
    review_path = bundle_root.joinpath(*review_relative.parts)
    value, review_bytes = _load_json_file(review_path, "review result")
    try:
        review = ReviewResult.from_dict(
            value,
            object_format=request.object_format,
        )
    except ArtifactValidationError as error:
        raise ReviewSubmissionError(
            f"review result failed validation: {error}"
        ) from error
    return review, review_bytes, review_path


def _validate_review_markdown(
    bundle_root: Path,
    request: ReviewRequest,
) -> bytes:
    relative = PurePosixPath(request.review_markdown_path)
    path = bundle_root.joinpath(*relative.parts)
    content = _read_regular_file(path, "human-readable review")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReviewSubmissionError(
            "human-readable review must contain UTF-8 Markdown"
        ) from error
    if not text.strip():
        raise ReviewSubmissionError(
            "human-readable review must contain non-whitespace text"
        )
    if "\x00" in text:
        raise ReviewSubmissionError(
            "human-readable review must not contain null bytes"
        )
    return content


def _assert_result_identity(
    request: ReviewRequest,
    review: ReviewResult,
) -> None:
    comparisons = (
        (review.request_id, request.request_id, "request ID"),
        (review.run_id, request.run_id, "run ID"),
        (review.round_number, request.round_number, "round"),
        (review.base_oid, request.base_oid, "base object ID"),
        (review.head_oid, request.head_oid, "head object ID"),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise ReviewSubmissionError(
                f"review result {label} does not match the review request"
            )


def _assert_fresh_result_id(
    previous_review: ReviewResult | None,
    review: ReviewResult,
) -> None:
    if (
        previous_review is not None
        and previous_review.result_id == review.result_id
    ):
        raise ReviewSubmissionError(
            "review result.result_id repeats the result ID of round "
            f"{previous_review.round_number}; each result needs a fresh ID"
        )


def _write_or_validate_marker(
    marker_path: Path,
    *,
    request: ReviewRequest,
    review: ReviewResult,
    review_digest: str,
) -> tuple[ReviewerLocalMarker, bool]:
    if os.path.lexists(marker_path):
        value, _ = _load_json_file(marker_path, "review marker")
        try:
            marker = ReviewerLocalMarker.from_dict(value)
        except ArtifactValidationError as error:
            raise ReviewSubmissionError(
                f"existing review marker failed validation: {error}"
            ) from error
        comparisons = (
            (marker.request_id, request.request_id, "request ID"),
            (marker.result_id, review.result_id, "result ID"),
            (
                marker.review_json_path,
                request.review_output_path,
                "review path",
            ),
            (marker.review_sha256, review_digest, "review digest"),
        )
        for actual, expected, label in comparisons:
            if actual != expected:
                raise ReviewSubmissionError(
                    f"marker-confirmed review {label} cannot be changed"
                )
        return marker, False

    marker = ReviewerLocalMarker(
        submitted_at=utc_timestamp(),
        request_id=request.request_id,
        result_id=review.result_id,
        review_json_path=request.review_output_path,
        review_sha256=review_digest,
    )
    atomic_write(marker_path, encode_json(marker.to_dict()), mode=0o600)
    value, _ = _load_json_file(marker_path, "review marker")
    try:
        persisted = ReviewerLocalMarker.from_dict(value)
    except ArtifactValidationError as error:
        raise ReviewSubmissionError(
            f"persisted review marker failed validation: {error}"
        ) from error
    if persisted != marker:
        raise ReviewSubmissionError(
            "persisted review marker differs from the validated result"
        )
    return persisted, True


def _review_result_prompt(
    *,
    request: ReviewRequest,
    result_id: str,
    review_path: Path,
) -> str:
    return (
        "AGENT_SQUAD/0.4.4 REVIEW_RESULT\n\n"
        f"run_id: {request.run_id}\n"
        f"round: {request.round_number}\n"
        f"request_id: {request.request_id}\n"
        f"result_id: {result_id}\n"
        f"head_oid: {request.head_oid}\n"
        f"review: {review_path}\n\n"
        "A marker-confirmed review result is ready.\n"
        "Run agent-squad apply-review --result-id "
        f"{result_id} and follow the state and verdict recorded by "
        "Agent Squad."
    )


def _validate_bundle_root_entries(bundle_root: Path) -> None:
    allowed = {"input", "output", MARKER_PATH.name}
    try:
        entries = list(bundle_root.iterdir())
    except OSError as error:
        raise ReviewSubmissionError(
            f"cannot inspect review bundle root: {error}"
        ) from error
    unexpected = sorted(
        entry.name for entry in entries if entry.name not in allowed
    )
    if unexpected:
        raise ReviewSubmissionError(
            "review bundle contains files outside documented input, output, "
            f"and marker locations: {', '.join(unexpected)}"
        )
    marker_path = bundle_root / MARKER_PATH.name
    if os.path.lexists(marker_path):
        _read_regular_file(marker_path, "review marker")


def _validate_output_tree(output_root: Path) -> None:
    read_regular_tree(
        output_root,
        label="review bundle output",
        error_type=ReviewSubmissionError,
    )


def _reject_case_collisions(
    paths: set[PurePosixPath],
    label: str,
) -> None:
    folded: dict[str, PurePosixPath] = {}
    for path in paths:
        key = str(path).casefold()
        existing = folded.get(key)
        if existing is not None and existing != path:
            raise ReviewSubmissionError(
                f"{label} contains case-colliding paths: {existing}, {path}"
            )
        folded[key] = path


def _verify_artifact(bundle_root: Path, artifact: BundleArtifact) -> None:
    relative = PurePosixPath(artifact.path)
    _verify_digest(
        bundle_root.joinpath(*relative.parts),
        artifact.sha256,
        artifact.path,
    )


def _verify_digest(path: Path, expected: str, label: str) -> None:
    content = _read_regular_file(path, label)
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise ReviewSubmissionError(
            f"review bundle digest mismatch for {label}"
        )


def _load_json_file(path: Path, label: str) -> tuple[object, bytes]:
    content = _read_regular_file(path, label)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReviewSubmissionError(
            f"{label} must contain UTF-8 JSON"
        ) from error
    try:
        return decode_json(text), content
    except InvalidJsonError as error:
        raise ReviewSubmissionError(
            f"{label} contains invalid JSON: {error}"
        ) from error


def _lstat_or_reject(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError as error:
        raise ReviewSubmissionError(f"{label} is missing: {path}") from error
    except OSError as error:
        raise ReviewSubmissionError(
            f"cannot inspect {label}: {error}"
        ) from error


def _read_regular_file(path: Path, label: str) -> bytes:
    status = _lstat_or_reject(path, label)
    if not stat.S_ISREG(status.st_mode):
        raise ReviewSubmissionError(
            f"{label} must be a regular non-symlink file: {path}"
        )
    return path.read_bytes()


def _require_normal_directory(path: Path, label: str) -> None:
    status = _lstat_or_reject(path, label)
    if not stat.S_ISDIR(status.st_mode):
        raise ReviewSubmissionError(
            f"{label} must be a normal directory: {path}"
        )


def _git_output(
    working_directory: Path,
    arguments: tuple[str, ...],
    action: str,
) -> str:
    result = run_git(working_directory, *arguments)
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown Git error"
        raise ReviewSubmissionError(f"could not {action}: {detail}")
    output = result.stdout.strip()
    if not output:
        raise ReviewSubmissionError(
            f"could not {action}: Git returned no value"
        )
    return output
