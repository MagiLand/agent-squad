"""Typed protocol records shared across review-workflow commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
import re

from .initialization import AgentKind, SCHEMA_VERSION
from .validation import JsonValidator


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
_require_optional_string = _VALIDATOR.require_optional_string
_require_absolute_path = _VALIDATOR.require_absolute_path
_require_object_format = _VALIDATOR.require_object_format
_require_narrow_relative_paths = (
    _VALIDATOR.require_narrow_relative_paths
)


def deterministic_reviewer_name(run_id: str, round_number: int) -> str:
    """Return the stable Herdr-safe Reviewer name for one logical round."""

    canonical_run_id = _require_uuid(run_id, "run ID")
    if type(round_number) is not int or round_number < 1:
        raise ArtifactValidationError("review round must be positive")
    name = (
        f"asq-{canonical_run_id.replace('-', '')[:12]}-"
        f"r{round_number:03d}-reviewer"
    )
    if REVIEWER_NAME_PATTERN.fullmatch(name) is None:
        raise ArtifactValidationError(
            "deterministic Reviewer name must be a valid Herdr agent name"
        )
    return name


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


class ReviewVerdict(StrEnum):
    """Supported outcomes recorded by an independent Reviewer."""

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    NEEDS_HUMAN = "needs_human"


class ResponseDisposition(StrEnum):
    """Supported Implementer dispositions for one review finding."""

    FIXED = "fixed"
    REJECTED = "rejected"
    NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True)
class HandoffRecord:
    """Serializable review-request delivery state."""

    round_number: int
    status: HandoffStatus
    target: str
    last_error: str | None
    updated_at: str
    herdr_version: str | None
    herdr_protocol: int | None

    @classmethod
    def from_dict(cls, value: object) -> "HandoffRecord":
        """Validate one non-null ``state.handoff`` record."""

        label = "state.handoff"
        data = _require_object(value, label)
        _VALIDATOR.check_fields(
            data,
            required={
                "kind",
                "round",
                "status",
                "target",
                "last_error",
                "updated_at",
                "herdr_version",
                "herdr_protocol",
            },
            path=label,
        )
        if data["kind"] != "review_request":
            raise ArtifactValidationError(
                f"{label}.kind must be review_request"
            )
        status = _VALIDATOR.require_enum(
            data["status"],
            f"{label}.status",
            HandoffStatus,
        )
        target = _require_reviewer_name(data["target"], f"{label}.target")
        last_error = _require_optional_string(
            data["last_error"],
            f"{label}.last_error",
        )
        version = _require_optional_string(
            data["herdr_version"],
            f"{label}.herdr_version",
        )
        protocol_value = data["herdr_protocol"]
        protocol = (
            None
            if protocol_value is None
            else _require_int(protocol_value, f"{label}.herdr_protocol")
        )
        if protocol is not None and protocol < 1:
            raise ArtifactValidationError(
                f"{label}.herdr_protocol must be positive"
            )
        if status is HandoffStatus.FAILED and last_error is None:
            raise ArtifactValidationError(
                f"{label}.last_error is required when handoff failed"
            )
        if status is not HandoffStatus.FAILED and last_error is not None:
            raise ArtifactValidationError(
                f"{label}.last_error is allowed only when handoff failed"
            )
        if status is HandoffStatus.SENT and (
            version is None or protocol is None
        ):
            raise ArtifactValidationError(
                "a sent handoff must record Herdr version and protocol"
            )
        return cls(
            round_number=_require_positive_int(
                data["round"],
                f"{label}.round",
            ),
            status=status,
            target=target,
            last_error=last_error,
            updated_at=_require_timestamp(
                data["updated_at"],
                f"{label}.updated_at",
            ),
            herdr_version=version,
            herdr_protocol=protocol,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation."""

        return {
            "kind": "review_request",
            "round": self.round_number,
            "status": self.status.value,
            "target": self.target,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
            "herdr_version": self.herdr_version,
            "herdr_protocol": self.herdr_protocol,
        }


@dataclass(frozen=True)
class ActiveRoundRecord:
    """Serializable current-round state shared by readers and writers."""

    round_number: int
    status: RoundStatus
    mode: SubmissionMode
    request_id: str
    result_id: str | None
    review_worktree: Path
    reviewer_name: str

    @classmethod
    def from_dict(cls, value: object) -> "ActiveRoundRecord":
        """Validate one non-null ``state.active_round`` record."""

        label = "state.active_round"
        data = _require_object(value, label)
        _VALIDATOR.check_fields(
            data,
            required={
                "round",
                "status",
                "mode",
                "request_id",
                "result_id",
                "review_worktree",
                "reviewer_name",
            },
            path=label,
        )
        return cls(
            round_number=_require_positive_int(
                data["round"],
                f"{label}.round",
            ),
            status=_VALIDATOR.require_enum(
                data["status"],
                f"{label}.status",
                RoundStatus,
            ),
            mode=_VALIDATOR.require_enum(
                data["mode"],
                f"{label}.mode",
                SubmissionMode,
            ),
            request_id=_require_uuid(
                data["request_id"],
                f"{label}.request_id",
            ),
            result_id=_require_optional_uuid(
                data["result_id"],
                f"{label}.result_id",
            ),
            review_worktree=_require_absolute_path(
                data["review_worktree"],
                f"{label}.review_worktree",
            ),
            reviewer_name=_require_reviewer_name(
                data["reviewer_name"],
                f"{label}.reviewer_name",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation."""

        return {
            "round": self.round_number,
            "status": self.status.value,
            "mode": self.mode.value,
            "request_id": self.request_id,
            "result_id": self.result_id,
            "review_worktree": str(self.review_worktree),
            "reviewer_name": self.reviewer_name,
        }


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
class DeveloperResolution:
    """One authoritative Developer decision and its companion artifact."""

    created_at: str
    resolution_id: str
    run_id: str
    resolves_escalation_id: str
    applies_to_finding_ids: tuple[str, ...]
    resolution_path: str
    resolution_sha256: str
    additional_rounds_granted: int

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        label: str,
    ) -> "DeveloperResolution":
        """Validate and construct one Developer resolution artifact."""

        data = _require_object(value, label)
        _VALIDATOR.check_fields(
            data,
            required={
                "schema_version",
                "created_at",
                "resolution_id",
                "run_id",
                "resolves_escalation_id",
                "applies_to_finding_ids",
                "resolution_path",
                "resolution_sha256",
                "additional_rounds_granted",
            },
            path=label,
        )
        schema_version = _require_int(
            data["schema_version"],
            f"{label}.schema_version",
        )
        if schema_version != SCHEMA_VERSION:
            raise ArtifactValidationError(
                f"{label}.schema_version must be {SCHEMA_VERSION}"
            )
        finding_ids = _require_string_list(
            data["applies_to_finding_ids"],
            f"{label}.applies_to_finding_ids",
        )
        if len(finding_ids) != len(set(finding_ids)):
            raise ArtifactValidationError(
                f"{label}.applies_to_finding_ids contains duplicates"
            )
        resolution_path = _require_string(
            data["resolution_path"],
            f"{label}.resolution_path",
        )
        parsed_path = PurePosixPath(resolution_path)
        if (
            parsed_path.is_absolute()
            or ".." in parsed_path.parts
            or len(parsed_path.parts) != 1
            or str(parsed_path) != resolution_path
        ):
            raise ArtifactValidationError(
                f"{label}.resolution_path must name a companion file in "
                "the same bundle directory"
            )
        additional_rounds = _require_int(
            data["additional_rounds_granted"],
            f"{label}.additional_rounds_granted",
        )
        if additional_rounds < 0:
            raise ArtifactValidationError(
                f"{label}.additional_rounds_granted must not be negative"
            )
        return cls(
            created_at=_require_timestamp(
                data["created_at"],
                f"{label}.created_at",
            ),
            resolution_id=_require_uuid(
                data["resolution_id"],
                f"{label}.resolution_id",
            ),
            run_id=_require_uuid(data["run_id"], f"{label}.run_id"),
            resolves_escalation_id=_require_uuid(
                data["resolves_escalation_id"],
                f"{label}.resolves_escalation_id",
            ),
            applies_to_finding_ids=finding_ids,
            resolution_path=resolution_path,
            resolution_sha256=_require_digest(
                data["resolution_sha256"],
                f"{label}.resolution_sha256",
            ),
            additional_rounds_granted=additional_rounds,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable machine-readable resolution representation."""

        return {
            "schema_version": SCHEMA_VERSION,
            "created_at": self.created_at,
            "resolution_id": self.resolution_id,
            "run_id": self.run_id,
            "resolves_escalation_id": self.resolves_escalation_id,
            "applies_to_finding_ids": list(self.applies_to_finding_ids),
            "resolution_path": self.resolution_path,
            "resolution_sha256": self.resolution_sha256,
            "additional_rounds_granted": self.additional_rounds_granted,
        }


@dataclass(frozen=True)
class ReviewFinding:
    """One structured finding in a Reviewer result."""

    finding_id: str
    severity: str
    blocking: bool
    category: str
    file: str
    line_start: int
    line_end: int
    problem: str
    evidence: str
    impact: str
    required_change: str
    verification: str

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        label: str,
    ) -> "ReviewFinding":
        """Validate and construct one structured review finding."""

        data = _require_object(value, label)
        _VALIDATOR.check_fields(
            data,
            required={
                "id",
                "severity",
                "blocking",
                "category",
                "file",
                "line_start",
                "line_end",
                "problem",
                "evidence",
                "impact",
                "required_change",
                "verification",
            },
            path=label,
        )
        line_start = _require_positive_int(
            data["line_start"],
            f"{label}.line_start",
        )
        line_end = _require_positive_int(
            data["line_end"],
            f"{label}.line_end",
        )
        if line_end < line_start:
            raise ArtifactValidationError(
                f"{label}.line_end must be at least line_start"
            )
        return cls(
            finding_id=_require_meaningful_string(
                data["id"],
                f"{label}.id",
            ),
            severity=_require_meaningful_string(
                data["severity"],
                f"{label}.severity",
            ),
            blocking=_require_bool(
                data["blocking"],
                f"{label}.blocking",
            ),
            category=_require_meaningful_string(
                data["category"],
                f"{label}.category",
            ),
            file=_require_bundle_path(
                data["file"],
                f"{label}.file",
                kind="repository",
            ),
            line_start=line_start,
            line_end=line_end,
            problem=_require_meaningful_string(
                data["problem"],
                f"{label}.problem",
            ),
            evidence=_require_meaningful_string(
                data["evidence"],
                f"{label}.evidence",
            ),
            impact=_require_meaningful_string(
                data["impact"],
                f"{label}.impact",
            ),
            required_change=_require_meaningful_string(
                data["required_change"],
                f"{label}.required_change",
            ),
            verification=_require_meaningful_string(
                data["verification"],
                f"{label}.verification",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable machine-readable finding representation."""

        return {
            "id": self.finding_id,
            "severity": self.severity,
            "blocking": self.blocking,
            "category": self.category,
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "problem": self.problem,
            "evidence": self.evidence,
            "impact": self.impact,
            "required_change": self.required_change,
            "verification": self.verification,
        }


@dataclass(frozen=True)
class ReviewResult:
    """A structurally and semantically validated Reviewer result."""

    created_at: str
    result_id: str
    request_id: str
    run_id: str
    round_number: int
    base_oid: str
    head_oid: str
    verdict: ReviewVerdict
    summary: str
    findings: tuple[ReviewFinding, ...]
    non_blocking_observations: tuple[str, ...]

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        object_format: str,
    ) -> "ReviewResult":
        """Validate and construct one complete Reviewer result."""

        data = _require_object(value, "review result")
        _VALIDATOR.check_fields(
            data,
            required={
                "schema_version",
                "created_at",
                "result_id",
                "request_id",
                "run_id",
                "round",
                "base_oid",
                "head_oid",
                "verdict",
                "summary",
                "findings",
                "non_blocking_observations",
            },
            path="review result",
        )
        schema_version = _require_int(
            data["schema_version"],
            "review result.schema_version",
        )
        if schema_version != SCHEMA_VERSION:
            raise ArtifactValidationError(
                f"review result.schema_version must be {SCHEMA_VERSION}"
            )
        _require_object_format(
            object_format,
            "review result Git object format",
        )
        findings_value = data["findings"]
        if not isinstance(findings_value, list):
            raise ArtifactValidationError(
                "review result.findings must be a JSON array"
            )
        findings = tuple(
            ReviewFinding.from_dict(
                item,
                label=f"review result.findings[{index}]",
            )
            for index, item in enumerate(findings_value)
        )
        finding_ids = [finding.finding_id for finding in findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ArtifactValidationError(
                "review result.findings contains duplicate finding IDs"
            )
        observations = _require_meaningful_string_list(
            data["non_blocking_observations"],
            "review result.non_blocking_observations",
        )
        verdict = _VALIDATOR.require_enum(
            data["verdict"],
            "review result.verdict",
            ReviewVerdict,
        )
        blocking_count = sum(finding.blocking for finding in findings)
        if verdict is ReviewVerdict.APPROVED and blocking_count:
            raise ArtifactValidationError(
                "an approved review result cannot contain blocking findings"
            )
        if (
            verdict is ReviewVerdict.CHANGES_REQUESTED
            and blocking_count == 0
        ):
            raise ArtifactValidationError(
                "a changes_requested review result must contain at least "
                "one blocking finding"
            )

        result_id = _require_uuid(
            data["result_id"],
            "review result.result_id",
        )
        request_id = _require_uuid(
            data["request_id"],
            "review result.request_id",
        )
        run_id = _require_uuid(
            data["run_id"],
            "review result.run_id",
        )
        if result_id in {request_id, run_id}:
            raise ArtifactValidationError(
                "review result.result_id must be distinct from request and "
                "run IDs"
            )

        return cls(
            created_at=_require_timestamp(
                data["created_at"],
                "review result.created_at",
            ),
            result_id=result_id,
            request_id=request_id,
            run_id=run_id,
            round_number=_require_positive_int(
                data["round"],
                "review result.round",
            ),
            base_oid=_require_oid(
                data["base_oid"],
                object_format,
                "review result.base_oid",
            ),
            head_oid=_require_oid(
                data["head_oid"],
                object_format,
                "review result.head_oid",
            ),
            verdict=verdict,
            summary=_require_meaningful_string(
                data["summary"],
                "review result.summary",
            ),
            findings=findings,
            non_blocking_observations=observations,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable machine-readable result representation."""

        return {
            "schema_version": SCHEMA_VERSION,
            "created_at": self.created_at,
            "result_id": self.result_id,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "round": self.round_number,
            "base_oid": self.base_oid,
            "head_oid": self.head_oid,
            "verdict": self.verdict.value,
            "summary": self.summary,
            "findings": [finding.to_dict() for finding in self.findings],
            "non_blocking_observations": list(
                self.non_blocking_observations
            ),
        }


@dataclass(frozen=True)
class FindingResponse:
    """One Implementer disposition for a Reviewer finding."""

    finding_id: str
    disposition: ResponseDisposition
    rationale: str
    changed_files: tuple[str, ...]
    evidence: tuple[str, ...]
    verification: str

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        label: str,
    ) -> "FindingResponse":
        """Validate one finding-complete Implementer response."""

        data = _require_object(value, label)
        _VALIDATOR.check_fields(
            data,
            required={
                "finding_id",
                "disposition",
                "rationale",
                "changed_files",
                "evidence",
                "verification",
            },
            path=label,
        )
        disposition = _VALIDATOR.require_enum(
            data["disposition"],
            f"{label}.disposition",
            ResponseDisposition,
        )
        changed_files = _require_path_list(
            data["changed_files"],
            f"{label}.changed_files",
        )
        evidence = _require_meaningful_string_list(
            data["evidence"],
            f"{label}.evidence",
        )
        verification = _require_string(
            data["verification"],
            f"{label}.verification",
        )
        if disposition is ResponseDisposition.FIXED:
            if not changed_files:
                raise ArtifactValidationError(
                    f"{label}.changed_files is required when fixed"
                )
            if not verification.strip():
                raise ArtifactValidationError(
                    f"{label}.verification is required when fixed"
                )
        if disposition is ResponseDisposition.REJECTED and not evidence:
            raise ArtifactValidationError(
                f"{label}.evidence is required when rejected"
            )
        return cls(
            finding_id=_require_meaningful_string(
                data["finding_id"],
                f"{label}.finding_id",
            ),
            disposition=disposition,
            rationale=_require_meaningful_string(
                data["rationale"],
                f"{label}.rationale",
            ),
            changed_files=changed_files,
            evidence=evidence,
            verification=verification,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable machine-readable response representation."""

        return {
            "finding_id": self.finding_id,
            "disposition": self.disposition.value,
            "rationale": self.rationale,
            "changed_files": list(self.changed_files),
            "evidence": list(self.evidence),
            "verification": self.verification,
        }


@dataclass(frozen=True)
class ReviewResponse:
    """A structurally validated Implementer response to one review."""

    created_at: str
    response_id: str
    supersedes_response_id: str | None
    resolution_ids: tuple[str, ...]
    run_id: str
    review_round: int
    review_result_id: str
    reviewed_head_oid: str
    responses: tuple[FindingResponse, ...]

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        object_format: str,
    ) -> "ReviewResponse":
        """Validate one complete versioned implementation response."""

        label = "implementation response"
        data = _require_object(value, label)
        _VALIDATOR.check_fields(
            data,
            required={
                "schema_version",
                "created_at",
                "response_id",
                "supersedes_response_id",
                "resolution_ids",
                "run_id",
                "review_round",
                "review_result_id",
                "reviewed_head_oid",
                "responses",
            },
            path=label,
        )
        schema_version = _require_int(
            data["schema_version"],
            f"{label}.schema_version",
        )
        if schema_version != SCHEMA_VERSION:
            raise ArtifactValidationError(
                f"{label}.schema_version must be {SCHEMA_VERSION}"
            )
        _require_object_format(object_format, f"{label} Git object format")
        resolution_value = data["resolution_ids"]
        if not isinstance(resolution_value, list):
            raise ArtifactValidationError(
                f"{label}.resolution_ids must be a JSON array"
            )
        resolution_ids = tuple(
            _require_uuid(item, f"{label}.resolution_ids[{index}]")
            for index, item in enumerate(resolution_value)
        )
        if len(resolution_ids) != len(set(resolution_ids)):
            raise ArtifactValidationError(
                f"{label}.resolution_ids contains duplicate IDs"
            )
        responses_value = data["responses"]
        if not isinstance(responses_value, list):
            raise ArtifactValidationError(
                f"{label}.responses must be a JSON array"
            )
        responses = tuple(
            FindingResponse.from_dict(
                item,
                label=f"{label}.responses[{index}]",
            )
            for index, item in enumerate(responses_value)
        )
        finding_ids = [item.finding_id for item in responses]
        if len(finding_ids) != len(set(finding_ids)):
            raise ArtifactValidationError(
                f"{label}.responses contains duplicate finding IDs"
            )
        response_id = _require_uuid(
            data["response_id"],
            f"{label}.response_id",
        )
        supersedes_response_id = _require_optional_uuid(
            data["supersedes_response_id"],
            f"{label}.supersedes_response_id",
        )
        run_id = _require_uuid(data["run_id"], f"{label}.run_id")
        review_result_id = _require_uuid(
            data["review_result_id"],
            f"{label}.review_result_id",
        )
        if response_id in {run_id, review_result_id}:
            raise ArtifactValidationError(
                f"{label}.response_id must be distinct from run and result "
                "IDs"
            )
        if supersedes_response_id == response_id:
            raise ArtifactValidationError(
                f"{label}.supersedes_response_id cannot equal response_id"
            )
        return cls(
            created_at=_require_timestamp(
                data["created_at"],
                f"{label}.created_at",
            ),
            response_id=response_id,
            supersedes_response_id=supersedes_response_id,
            resolution_ids=resolution_ids,
            run_id=run_id,
            review_round=_require_positive_int(
                data["review_round"],
                f"{label}.review_round",
            ),
            review_result_id=review_result_id,
            reviewed_head_oid=_require_oid(
                data["reviewed_head_oid"],
                object_format,
                f"{label}.reviewed_head_oid",
            ),
            responses=responses,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable machine-readable response representation."""

        return {
            "schema_version": SCHEMA_VERSION,
            "created_at": self.created_at,
            "response_id": self.response_id,
            "supersedes_response_id": self.supersedes_response_id,
            "resolution_ids": list(self.resolution_ids),
            "run_id": self.run_id,
            "review_round": self.review_round,
            "review_result_id": self.review_result_id,
            "reviewed_head_oid": self.reviewed_head_oid,
            "responses": [item.to_dict() for item in self.responses],
        }


def validate_review_response(
    response: ReviewResponse,
    review: ReviewResult,
    mode: SubmissionMode,
) -> None:
    """Validate response identity, completeness, and submission semantics."""

    if review.verdict is not ReviewVerdict.CHANGES_REQUESTED:
        raise ArtifactValidationError(
            "an implementation response requires a changes_requested review"
        )
    comparisons = (
        (response.run_id, review.run_id, "run ID"),
        (response.review_round, review.round_number, "review round"),
        (response.review_result_id, review.result_id, "review result ID"),
        (response.reviewed_head_oid, review.head_oid, "reviewed head OID"),
    )
    for actual, expected, label in comparisons:
        if actual != expected:
            raise ArtifactValidationError(
                f"implementation response {label} does not match the "
                "previous review"
            )
    known_ids = {finding.finding_id for finding in review.findings}
    blocking_ids = {
        finding.finding_id for finding in review.findings if finding.blocking
    }
    response_ids = {item.finding_id for item in response.responses}
    unknown_ids = sorted(response_ids - known_ids)
    if unknown_ids:
        raise ArtifactValidationError(
            "implementation response references unknown finding IDs: "
            + ", ".join(unknown_ids)
        )
    missing_ids = sorted(blocking_ids - response_ids)
    if missing_ids:
        raise ArtifactValidationError(
            "implementation response is missing blocking finding IDs: "
            + ", ".join(missing_ids)
        )
    if mode is SubmissionMode.RECONSIDERATION:
        invalid = [
            item.finding_id
            for item in response.responses
            if item.disposition is not ResponseDisposition.REJECTED
        ]
        if invalid:
            raise ArtifactValidationError(
                "reconsideration requires rejected dispositions with "
                "evidence for every response"
            )


@dataclass(frozen=True)
class ReviewerLocalMarker:
    """Reviewer-side proof that one result passed local validation."""

    submitted_at: str
    request_id: str
    result_id: str
    review_json_path: str
    review_sha256: str

    @classmethod
    def from_dict(cls, value: object) -> "ReviewerLocalMarker":
        """Validate and construct the Reviewer-local result marker."""

        data = _require_object(value, "review marker")
        _VALIDATOR.check_fields(
            data,
            required={
                "schema_version",
                "submitted_at",
                "request_id",
                "result_id",
                "status",
                "review_json_path",
                "review_sha256",
            },
            path="review marker",
        )
        schema_version = _require_int(
            data["schema_version"],
            "review marker.schema_version",
        )
        if schema_version != SCHEMA_VERSION:
            raise ArtifactValidationError(
                f"review marker.schema_version must be {SCHEMA_VERSION}"
            )
        if data["status"] != "result_submitted":
            raise ArtifactValidationError(
                "review marker.status must be result_submitted"
            )
        review_json_path = _require_bundle_path(
            data["review_json_path"],
            "review marker.review_json_path",
        )
        if review_json_path != "output/review.json":
            raise ArtifactValidationError(
                "review marker.review_json_path must be output/review.json"
            )
        return cls(
            submitted_at=_require_timestamp(
                data["submitted_at"],
                "review marker.submitted_at",
            ),
            request_id=_require_uuid(
                data["request_id"],
                "review marker.request_id",
            ),
            result_id=_require_uuid(
                data["result_id"],
                "review marker.result_id",
            ),
            review_json_path=review_json_path,
            review_sha256=_require_digest(
                data["review_sha256"],
                "review marker.review_sha256",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable marker representation."""

        return {
            "schema_version": SCHEMA_VERSION,
            "submitted_at": self.submitted_at,
            "request_id": self.request_id,
            "result_id": self.result_id,
            "status": "result_submitted",
            "review_json_path": self.review_json_path,
            "review_sha256": self.review_sha256,
        }


@dataclass(frozen=True)
class ApprovalRecord:
    """Immutable authority for one exact approved review result."""

    created_at: str
    run_id: str
    round_number: int
    request_id: str
    result_id: str
    task_sha256: str
    object_format: str
    base_oid: str
    head_oid: str
    reviewer_name: str
    reviewer_kind: AgentKind
    review_sha256: str

    @classmethod
    def from_dict(cls, value: object) -> "ApprovalRecord":
        """Validate and construct one exact-revision approval record."""

        label = "approval record"
        data = _require_object(value, label)
        _VALIDATOR.check_fields(
            data,
            required={
                "schema_version",
                "created_at",
                "run_id",
                "round",
                "request_id",
                "result_id",
                "task_sha256",
                "git_object_format",
                "base_oid",
                "head_oid",
                "reviewer",
                "review_sha256",
            },
            path=label,
        )
        schema_version = _require_int(
            data["schema_version"],
            f"{label}.schema_version",
        )
        if schema_version != SCHEMA_VERSION:
            raise ArtifactValidationError(
                f"{label}.schema_version must be {SCHEMA_VERSION}"
            )
        object_format = _require_object_format(
            data["git_object_format"],
            f"{label}.git_object_format",
        )
        reviewer = _require_object(data["reviewer"], f"{label}.reviewer")
        _VALIDATOR.check_fields(
            reviewer,
            required={"name", "kind"},
            path=f"{label}.reviewer",
        )
        run_id = _require_uuid(data["run_id"], f"{label}.run_id")
        request_id = _require_uuid(
            data["request_id"],
            f"{label}.request_id",
        )
        result_id = _require_uuid(
            data["result_id"],
            f"{label}.result_id",
        )
        if len({run_id, request_id, result_id}) != 3:
            raise ArtifactValidationError(
                f"{label} run, request, and result IDs must be distinct"
            )
        return cls(
            created_at=_require_timestamp(
                data["created_at"],
                f"{label}.created_at",
            ),
            run_id=run_id,
            round_number=_require_positive_int(
                data["round"],
                f"{label}.round",
            ),
            request_id=request_id,
            result_id=result_id,
            task_sha256=_require_digest(
                data["task_sha256"],
                f"{label}.task_sha256",
            ),
            object_format=object_format,
            base_oid=_require_oid(
                data["base_oid"],
                object_format,
                f"{label}.base_oid",
            ),
            head_oid=_require_oid(
                data["head_oid"],
                object_format,
                f"{label}.head_oid",
            ),
            reviewer_name=_require_reviewer_name(
                reviewer["name"],
                f"{label}.reviewer.name",
            ),
            reviewer_kind=_VALIDATOR.require_enum(
                reviewer["kind"],
                f"{label}.reviewer.kind",
                AgentKind,
            ),
            review_sha256=_require_digest(
                data["review_sha256"],
                f"{label}.review_sha256",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable machine-readable approval authority."""

        return {
            "schema_version": SCHEMA_VERSION,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "round": self.round_number,
            "request_id": self.request_id,
            "result_id": self.result_id,
            "task_sha256": self.task_sha256,
            "git_object_format": self.object_format,
            "base_oid": self.base_oid,
            "head_oid": self.head_oid,
            "reviewer": {
                "name": self.reviewer_name,
                "kind": self.reviewer_kind.value,
            },
            "review_sha256": self.review_sha256,
        }


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
    allowed_generated_paths: tuple[str, ...]

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
                "allowed_generated_paths",
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

        mode = _VALIDATOR.require_enum(
            data["mode"],
            "review request.mode",
            SubmissionMode,
        )

        object_format = _require_object_format(
            data["git_object_format"],
            "review request.git_object_format",
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
            allowed_generated_paths=_require_narrow_relative_paths(
                data["allowed_generated_paths"],
                "review request.allowed_generated_paths",
            ),
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
            "allowed_generated_paths": list(self.allowed_generated_paths),
        }


@dataclass(frozen=True)
class ReviewRoundRecord:
    """Serializable authoritative history for one review round."""

    created_at: str
    updated_at: str
    run_id: str
    round_number: int
    mode: SubmissionMode
    request_id: str
    result_id: str | None
    verdict: ReviewVerdict | None
    base_oid: str
    head_oid: str
    object_format: str
    status: RoundStatus
    review_worktree: Path
    reviewer_name: str
    reviewer_kind: AgentKind
    reviewer_start_args: tuple[str, ...]
    request_artifact: BundleArtifact
    implementation_report: BundleArtifact
    bundle_inputs: tuple[BundleArtifact, ...]
    review_result: BundleArtifact | None
    review_markdown: BundleArtifact | None
    review_marker: BundleArtifact | None
    approval: BundleArtifact | None
    bundle_archive: tuple[BundleArtifact, ...]
    warnings: tuple[str, ...]

    @classmethod
    def for_request(
        cls,
        request: ReviewRequest,
        *,
        review_worktree: Path,
        reviewer_start_args: tuple[str, ...],
        request_digest: str,
        warnings: tuple[str, ...],
        additional_bundle_inputs: tuple[BundleArtifact, ...] = (),
    ) -> "ReviewRoundRecord":
        """Build the initial authoritative record for a review request."""

        request_artifact = BundleArtifact(
            path="request.json",
            sha256=request_digest,
        )
        return cls(
            created_at=request.created_at,
            updated_at=request.created_at,
            run_id=request.run_id,
            round_number=request.round_number,
            mode=request.mode,
            request_id=request.request_id,
            result_id=None,
            verdict=None,
            base_oid=request.base_oid,
            head_oid=request.head_oid,
            object_format=request.object_format,
            status=RoundStatus.REVIEWING,
            review_worktree=review_worktree,
            reviewer_name=request.reviewer_name,
            reviewer_kind=request.reviewer_kind,
            reviewer_start_args=reviewer_start_args,
            request_artifact=request_artifact,
            implementation_report=BundleArtifact(
                path="implementation-report.md",
                sha256=request.implementation_report.sha256,
            ),
            bundle_inputs=(
                BundleArtifact(
                    path="input/request.json",
                    sha256=request_digest,
                ),
                request.task,
                request.implementation_report,
                *request.context_files,
                *additional_bundle_inputs,
            ),
            review_result=None,
            review_markdown=None,
            review_marker=None,
            approval=None,
            bundle_archive=(),
            warnings=warnings,
        )

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        label: str,
    ) -> "ReviewRoundRecord":
        """Validate one serialized authoritative round record."""

        data = _require_object(value, label)
        _VALIDATOR.check_fields(
            data,
            required={
                "schema_version",
                "created_at",
                "updated_at",
                "run_id",
                "round",
                "mode",
                "request_id",
                "result_id",
                "verdict",
                "base_oid",
                "head_oid",
                "git_object_format",
                "status",
                "review_worktree",
                "reviewer",
                "artifacts",
                "warnings",
            },
            path=label,
        )
        schema_version = _require_int(
            data["schema_version"],
            f"{label}.schema_version",
        )
        if schema_version != SCHEMA_VERSION:
            raise ArtifactValidationError(
                f"{label}.schema_version must be {SCHEMA_VERSION}"
            )
        object_format = _require_object_format(
            data["git_object_format"],
            f"{label}.git_object_format",
        )
        reviewer = _require_object(data["reviewer"], f"{label}.reviewer")
        _VALIDATOR.check_fields(
            reviewer,
            required={"name", "kind", "start_args"},
            path=f"{label}.reviewer",
        )
        artifacts = _require_object(
            data["artifacts"],
            f"{label}.artifacts",
        )
        _VALIDATOR.check_fields(
            artifacts,
            required={
                "request",
                "implementation_report",
                "bundle_inputs",
                "review_result",
                "review_markdown",
                "review_marker",
                "approval",
                "bundle_archive",
            },
            path=f"{label}.artifacts",
        )
        request_artifact = BundleArtifact.from_dict(
            artifacts["request"],
            label=f"{label}.artifacts.request",
        )
        implementation_report = BundleArtifact.from_dict(
            artifacts["implementation_report"],
            label=f"{label}.artifacts.implementation_report",
        )
        if request_artifact.path != "request.json":
            raise ArtifactValidationError(
                f"{label} request artifact path must be request.json"
            )
        if implementation_report.path != "implementation-report.md":
            raise ArtifactValidationError(
                f"{label} report path must be implementation-report.md"
            )
        result_id = _require_optional_uuid(
            data["result_id"],
            f"{label}.result_id",
        )
        verdict_value = data["verdict"]
        verdict = (
            None
            if verdict_value is None
            else _VALIDATOR.require_enum(
                verdict_value,
                f"{label}.verdict",
                ReviewVerdict,
            )
        )
        status = _VALIDATOR.require_enum(
            data["status"],
            f"{label}.status",
            RoundStatus,
        )
        review_result = _require_optional_artifact(
            artifacts["review_result"],
            f"{label}.artifacts.review_result",
        )
        review_markdown = _require_optional_artifact(
            artifacts["review_markdown"],
            f"{label}.artifacts.review_markdown",
        )
        review_marker = _require_optional_artifact(
            artifacts["review_marker"],
            f"{label}.artifacts.review_marker",
        )
        approval = _require_optional_artifact(
            artifacts["approval"],
            f"{label}.artifacts.approval",
        )
        bundle_archive = _require_artifact_list(
            artifacts["bundle_archive"],
            f"{label}.artifacts.bundle_archive",
        )
        result_artifacts = (
            review_result,
            review_markdown,
            review_marker,
        )
        if status in {RoundStatus.PREPARED, RoundStatus.REVIEWING}:
            if result_id is not None or verdict is not None:
                raise ArtifactValidationError(
                    f"{label} cannot record a result before classification"
                )
            if any(item is not None for item in result_artifacts) or (
                approval is not None or bundle_archive
            ):
                raise ArtifactValidationError(
                    f"{label} cannot record result artifacts before "
                    "classification"
                )
        if status is RoundStatus.APPLIED:
            if result_id is None or verdict is None:
                raise ArtifactValidationError(
                    f"{label} must record its applied result and verdict"
                )
            if any(item is None for item in result_artifacts):
                raise ArtifactValidationError(
                    f"{label} must record every applied result artifact"
                )
            if not bundle_archive:
                raise ArtifactValidationError(
                    f"{label} must record the complete applied bundle archive"
                )
            if (verdict is ReviewVerdict.APPROVED) != (approval is not None):
                raise ArtifactValidationError(
                    f"{label} approval artifact must exist exactly for an "
                    "approved result"
                )

        return cls(
            created_at=_require_timestamp(
                data["created_at"],
                f"{label}.created_at",
            ),
            updated_at=_require_timestamp(
                data["updated_at"],
                f"{label}.updated_at",
            ),
            run_id=_require_uuid(data["run_id"], f"{label}.run_id"),
            round_number=_require_positive_int(
                data["round"],
                f"{label}.round",
            ),
            mode=_VALIDATOR.require_enum(
                data["mode"],
                f"{label}.mode",
                SubmissionMode,
            ),
            request_id=_require_uuid(
                data["request_id"],
                f"{label}.request_id",
            ),
            result_id=result_id,
            verdict=verdict,
            base_oid=_require_oid(
                data["base_oid"],
                object_format,
                f"{label}.base_oid",
            ),
            head_oid=_require_oid(
                data["head_oid"],
                object_format,
                f"{label}.head_oid",
            ),
            object_format=object_format,
            status=status,
            review_worktree=_require_absolute_path(
                data["review_worktree"],
                f"{label}.review_worktree",
            ),
            reviewer_name=_require_reviewer_name(
                reviewer["name"],
                f"{label}.reviewer.name",
            ),
            reviewer_kind=_VALIDATOR.require_enum(
                reviewer["kind"],
                f"{label}.reviewer.kind",
                AgentKind,
            ),
            reviewer_start_args=_require_string_list(
                reviewer["start_args"],
                f"{label}.reviewer.start_args",
            ),
            request_artifact=request_artifact,
            implementation_report=implementation_report,
            bundle_inputs=_require_artifact_list(
                artifacts["bundle_inputs"],
                f"{label}.artifacts.bundle_inputs",
            ),
            review_result=review_result,
            review_markdown=review_markdown,
            review_marker=review_marker,
            approval=approval,
            bundle_archive=bundle_archive,
            warnings=_require_string_list(
                data["warnings"],
                f"{label}.warnings",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation."""

        return {
            "schema_version": SCHEMA_VERSION,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_id": self.run_id,
            "round": self.round_number,
            "mode": self.mode.value,
            "request_id": self.request_id,
            "result_id": self.result_id,
            "verdict": (
                self.verdict.value if self.verdict is not None else None
            ),
            "base_oid": self.base_oid,
            "head_oid": self.head_oid,
            "git_object_format": self.object_format,
            "status": self.status.value,
            "review_worktree": str(self.review_worktree),
            "reviewer": {
                "name": self.reviewer_name,
                "kind": self.reviewer_kind.value,
                "start_args": list(self.reviewer_start_args),
            },
            "artifacts": {
                "request": self.request_artifact.to_dict(),
                "implementation_report": self.implementation_report.to_dict(),
                "bundle_inputs": [
                    artifact.to_dict() for artifact in self.bundle_inputs
                ],
                "review_result": (
                    self.review_result.to_dict()
                    if self.review_result is not None
                    else None
                ),
                "review_markdown": (
                    self.review_markdown.to_dict()
                    if self.review_markdown is not None
                    else None
                ),
                "review_marker": (
                    self.review_marker.to_dict()
                    if self.review_marker is not None
                    else None
                ),
                "approval": (
                    self.approval.to_dict()
                    if self.approval is not None
                    else None
                ),
                "bundle_archive": [
                    artifact.to_dict() for artifact in self.bundle_archive
                ],
            },
            "warnings": list(self.warnings),
        }


def _require_bundle_path(
    value: object,
    label: str,
    *,
    kind: str = "bundle",
) -> str:
    text = _require_string(value, label)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path == PurePosixPath(".")
        or str(path) != text
    ):
        raise ArtifactValidationError(
            f"{label} must be a normalized {kind}-relative path"
        )
    return text


def _require_optional_bundle_path(
    value: object,
    label: str,
) -> str | None:
    if value is None:
        return None
    return _require_bundle_path(value, label)


def _require_optional_artifact(
    value: object,
    label: str,
) -> BundleArtifact | None:
    if value is None:
        return None
    return BundleArtifact.from_dict(value, label=label)


def _require_optional_uuid(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_uuid(value, label)


def _require_positive_int(value: object, label: str) -> int:
    result = _require_int(value, label)
    if result < 1:
        raise ArtifactValidationError(f"{label} must be positive")
    return result


def _require_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ArtifactValidationError(f"{label} must be a boolean")
    return value


def _require_meaningful_string(value: object, label: str) -> str:
    result = _require_string(value, label)
    if not result.strip():
        raise ArtifactValidationError(
            f"{label} must contain non-whitespace text"
        )
    return result


def _require_meaningful_string_list(
    value: object,
    label: str,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ArtifactValidationError(f"{label} must be a JSON array")
    return tuple(
        _require_meaningful_string(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    )


def _require_reviewer_name(value: object, label: str) -> str:
    result = _require_string(value, label)
    if REVIEWER_NAME_PATTERN.fullmatch(result) is None:
        raise ArtifactValidationError(
            f"{label} must be a valid Herdr agent name"
        )
    return result


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


def _require_string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ArtifactValidationError(f"{label} must be a JSON array")
    return tuple(
        _require_string(item, f"{label}[{index}]")
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
