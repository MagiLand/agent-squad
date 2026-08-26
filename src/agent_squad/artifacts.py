"""Typed protocol artifacts shared across review-request commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
import re

from .initialization import AgentKind, SCHEMA_VERSION
from .validation import JsonValidator, OID_LENGTHS


REVIEWER_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class ArtifactValidationError(ValueError):
    """Raised when a protocol artifact does not satisfy its contract."""


_VALIDATOR = JsonValidator(ArtifactValidationError)
_require_object = _VALIDATOR.require_object
_require_string = _VALIDATOR.require_string
_require_int = _VALIDATOR.require_int
_require_uuid = _VALIDATOR.require_uuid
_require_digest = _VALIDATOR.require_digest
_require_oid = _VALIDATOR.require_oid
_require_timestamp = _VALIDATOR.require_timestamp


class SubmissionMode(StrEnum):
    """Supported relationships between a candidate and an earlier review."""

    NEW_REVISION = "new_revision"
    RECONSIDERATION = "reconsideration"


class RoundStatus(StrEnum):
    """Authoritative implementation-side review-round statuses."""

    PREPARED = "prepared"
    REVIEWING = "reviewing"
    APPLIED = "applied"
    SUPERSEDED = "superseded"
    STALE = "stale"
    INVALID = "invalid"


class HandoffStatus(StrEnum):
    """Durable delivery states for one logical handoff."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True)
class BundleArtifact:
    """One bundle-relative file and its exact content digest."""

    path: str
    sha256: str

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        label: str,
    ) -> "BundleArtifact":
        """Validate one serialized bundle artifact."""

        data = _require_object(value, label)
        _VALIDATOR.check_fields(
            data,
            required={"path", "sha256"},
            path=label,
        )
        return cls(
            path=_require_bundle_path(data["path"], f"{label}.path"),
            sha256=_require_digest(data["sha256"], f"{label}.sha256"),
        )

    def to_dict(self) -> dict[str, str]:
        """Return the stable JSON representation."""

        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class ReviewRequest:
    """The complete immutable request given to one round-scoped Reviewer."""

    created_at: str
    request_id: str
    run_id: str
    round_number: int
    mode: SubmissionMode
    object_format: str
    base_oid: str
    head_oid: str
    task: BundleArtifact
    implementation_report: BundleArtifact
    context_files: tuple[BundleArtifact, ...]
    previous_review_path: str | None
    previous_response_path: str | None
    resolution_paths: tuple[str, ...]
    review_output_path: str
    review_markdown_path: str
    implementer_agent: str
    implementer_kind: AgentKind
    reviewer_kind: AgentKind
    reviewer_name: str

    @classmethod
    def from_dict(cls, value: object) -> "ReviewRequest":
        """Validate and construct a review request from decoded JSON."""

        data = _require_object(value, "review request")
        _VALIDATOR.check_fields(
            data,
            required={
                "schema_version",
                "created_at",
                "request_id",
                "run_id",
                "round",
                "mode",
                "git_object_format",
                "base_oid",
                "head_oid",
                "task",
                "implementation_report",
                "context_files",
                "previous_review_path",
                "previous_response_path",
                "resolution_paths",
                "review_output_path",
                "review_markdown_path",
                "implementer_agent",
                "implementer_kind",
                "reviewer_kind",
                "reviewer_name",
            },
            path="review request",
        )
        schema_version = _require_int(
            data["schema_version"],
            "review request.schema_version",
        )
        if schema_version != SCHEMA_VERSION:
            raise ArtifactValidationError(
                f"review request.schema_version must be {SCHEMA_VERSION}"
            )

        mode_text = _require_string(data["mode"], "review request.mode")
        try:
            mode = SubmissionMode(mode_text)
        except ValueError:
            supported = ", ".join(item.value for item in SubmissionMode)
            raise ArtifactValidationError(
                f"review request.mode must be one of: {supported}"
            ) from None

        object_format = _require_string(
            data["git_object_format"],
            "review request.git_object_format",
        )
        if object_format not in OID_LENGTHS:
            raise ArtifactValidationError(
                "review request.git_object_format must be sha1 or sha256"
            )
        base_oid = _require_oid(
            data["base_oid"],
            object_format,
            "review request.base_oid",
        )
        head_oid = _require_oid(
            data["head_oid"],
            object_format,
            "review request.head_oid",
        )
        round_number = _require_int(data["round"], "review request.round")
        if round_number < 1:
            raise ArtifactValidationError(
                "review request.round must be positive"
            )
        if round_number == 1:
            if mode is not SubmissionMode.NEW_REVISION:
                raise ArtifactValidationError(
                    "the first review round must use mode new_revision"
                )
            if head_oid == base_oid:
                raise ArtifactValidationError(
                    "the first review round head must differ from its base"
                )

        task = BundleArtifact.from_dict(
            data["task"],
            label="review request.task",
        )
        report = BundleArtifact.from_dict(
            data["implementation_report"],
            label="review request.implementation_report",
        )
        if task.path != "input/task.md":
            raise ArtifactValidationError(
                "review request.task.path must be input/task.md"
            )
        if report.path != "input/implementation-report.md":
            raise ArtifactValidationError(
                "review request.implementation_report.path must be "
                "input/implementation-report.md"
            )

        contexts = _require_artifact_list(
            data["context_files"],
            "review request.context_files",
        )
        if any(
            not artifact.path.startswith("input/context/")
            for artifact in contexts
        ):
            raise ArtifactValidationError(
                "review request context paths must be below input/context/"
            )
        _reject_duplicate_paths(contexts, "review request.context_files")

        previous_review_path = _require_optional_bundle_path(
            data["previous_review_path"],
            "review request.previous_review_path",
        )
        previous_response_path = _require_optional_bundle_path(
            data["previous_response_path"],
            "review request.previous_response_path",
        )
        if previous_review_path not in (None, "input/previous-review.json"):
            raise ArtifactValidationError(
                "review request.previous_review_path must be "
                "input/previous-review.json when present"
            )
        if previous_response_path not in (
            None,
            "input/previous-response.json",
        ):
            raise ArtifactValidationError(
                "review request.previous_response_path must be "
                "input/previous-response.json when present"
            )
        if round_number == 1 and (
            previous_review_path is not None
            or previous_response_path is not None
        ):
            raise ArtifactValidationError(
                "the first review round cannot reference previous review "
                "artifacts"
            )
        resolution_paths = _require_path_list(
            data["resolution_paths"],
            "review request.resolution_paths",
        )
        if any(
            not path.startswith("input/resolutions/")
            for path in resolution_paths
        ):
            raise ArtifactValidationError(
                "review request resolution paths must be below "
                "input/resolutions/"
            )
        review_output_path = _require_bundle_path(
            data["review_output_path"],
            "review request.review_output_path",
        )
        review_markdown_path = _require_bundle_path(
            data["review_markdown_path"],
            "review request.review_markdown_path",
        )
        if review_output_path != "output/review.json":
            raise ArtifactValidationError(
                "review request.review_output_path must be output/review.json"
            )
        if review_markdown_path != "output/review.md":
            raise ArtifactValidationError(
                "review request.review_markdown_path must be output/review.md"
            )

        reviewer_name = _require_string(
            data["reviewer_name"],
            "review request.reviewer_name",
        )
        if REVIEWER_NAME_PATTERN.fullmatch(reviewer_name) is None:
            raise ArtifactValidationError(
                "review request.reviewer_name must match "
                "[a-z][a-z0-9_-]{0,31}"
            )

        return cls(
            created_at=_require_timestamp(
                data["created_at"],
                "review request.created_at",
            ),
            request_id=_require_uuid(
                data["request_id"],
                "review request.request_id",
            ),
            run_id=_require_uuid(data["run_id"], "review request.run_id"),
            round_number=round_number,
            mode=mode,
            object_format=object_format,
            base_oid=base_oid,
            head_oid=head_oid,
            task=task,
            implementation_report=report,
            context_files=contexts,
            previous_review_path=previous_review_path,
            previous_response_path=previous_response_path,
            resolution_paths=resolution_paths,
            review_output_path=review_output_path,
            review_markdown_path=review_markdown_path,
            implementer_agent=_require_string(
                data["implementer_agent"],
                "review request.implementer_agent",
            ),
            implementer_kind=_VALIDATOR.require_enum(
                data["implementer_kind"],
                "review request.implementer_kind",
                AgentKind,
            ),
            reviewer_kind=_VALIDATOR.require_enum(
                data["reviewer_kind"],
                "review request.reviewer_kind",
                AgentKind,
            ),
            reviewer_name=reviewer_name,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable machine-readable request representation."""

        return {
            "schema_version": SCHEMA_VERSION,
            "created_at": self.created_at,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "round": self.round_number,
            "mode": self.mode.value,
            "git_object_format": self.object_format,
            "base_oid": self.base_oid,
            "head_oid": self.head_oid,
            "task": self.task.to_dict(),
            "implementation_report": self.implementation_report.to_dict(),
            "context_files": [
                artifact.to_dict() for artifact in self.context_files
            ],
            "previous_review_path": self.previous_review_path,
            "previous_response_path": self.previous_response_path,
            "resolution_paths": list(self.resolution_paths),
            "review_output_path": self.review_output_path,
            "review_markdown_path": self.review_markdown_path,
            "implementer_agent": self.implementer_agent,
            "implementer_kind": self.implementer_kind.value,
            "reviewer_kind": self.reviewer_kind.value,
            "reviewer_name": self.reviewer_name,
        }


def _require_bundle_path(value: object, label: str) -> str:
    text = _require_string(value, label)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path == PurePosixPath(".")
        or str(path) != text
    ):
        raise ArtifactValidationError(
            f"{label} must be a normalized bundle-relative path"
        )
    return text


def _require_optional_bundle_path(
    value: object,
    label: str,
) -> str | None:
    if value is None:
        return None
    return _require_bundle_path(value, label)


def _require_artifact_list(
    value: object,
    label: str,
) -> tuple[BundleArtifact, ...]:
    if not isinstance(value, list):
        raise ArtifactValidationError(f"{label} must be a JSON array")
    return tuple(
        BundleArtifact.from_dict(item, label=f"{label}[{index}]")
        for index, item in enumerate(value)
    )


def _require_path_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ArtifactValidationError(f"{label} must be a JSON array")
    paths = tuple(
        _require_bundle_path(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    )
    if len(paths) != len(set(paths)):
        raise ArtifactValidationError(f"{label} contains duplicate paths")
    return paths


def _reject_duplicate_paths(
    artifacts: tuple[BundleArtifact, ...],
    label: str,
) -> None:
    paths = [artifact.path for artifact in artifacts]
    if len(paths) != len(set(paths)):
        raise ArtifactValidationError(f"{label} contains duplicate paths")
