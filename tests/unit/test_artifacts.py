from __future__ import annotations

import copy
from pathlib import Path
import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad.artifacts import (  # noqa: E402
    ActiveRoundRecord,
    ApprovalRecord,
    ArtifactValidationError,
    DeveloperResolution,
    HandoffRecord,
    HandoffStatus,
    ReviewRequest,
    ReviewResponse,
    ReviewerLocalMarker,
    ReviewResult,
    ReviewRoundRecord,
    ReviewSupersession,
    ReviewVerdict,
    ResponseDisposition,
    RoundStatus,
    SubmissionMode,
    deterministic_reviewer_name,
    response_validation_mode,
    validate_followup_submission_head,
    validate_review_response,
)
from agent_squad.initialization import AgentKind  # noqa: E402


class DeterministicReviewerNameTests(unittest.TestCase):
    def test_name_is_stable_and_herdr_safe(self) -> None:
        run_id = "12345678-1234-5678-9234-567812345678"

        name = deterministic_reviewer_name(run_id, 1)

        self.assertEqual(name, "asq-123456781234-r001-reviewer")
        self.assertLessEqual(len(name), 32)
        self.assertEqual(deterministic_reviewer_name(run_id, 1), name)

    def test_name_rejects_invalid_rounds_and_oversized_result(self) -> None:
        run_id = "12345678-1234-5678-9234-567812345678"
        cases = (
            (0, "review round must be positive"),
            (True, "review round must be positive"),
            (100000, "must be a valid Herdr agent name"),
        )
        for round_number, message in cases:
            with self.subTest(round_number=round_number):
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    deterministic_reviewer_name(run_id, round_number)


def _request() -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_at": "2026-08-25T12:00:00Z",
        "request_id": "12345678-1234-5678-9234-567812345678",
        "run_id": "87654321-4321-6789-a234-678912345678",
        "round": 1,
        "mode": "new_revision",
        "git_object_format": "sha1",
        "base_oid": "a" * 40,
        "head_oid": "b" * 40,
        "task": {"path": "input/task.md", "sha256": "c" * 64},
        "implementation_report": {
            "path": "input/implementation-report.md",
            "sha256": "d" * 64,
        },
        "context_files": [
            {
                "path": "input/context/001/notes.md",
                "sha256": "e" * 64,
            }
        ],
        "previous_review_path": None,
        "previous_response_path": None,
        "recovery_round_path": None,
        "resolution_paths": [],
        "review_output_path": "output/review.json",
        "review_markdown_path": "output/review.md",
        "implementer_agent": "codex-main",
        "implementer_kind": "codex",
        "reviewer_kind": "claude",
        "reviewer_name": "asq-87654321-r001-reviewer",
        "allowed_generated_paths": ["build/", "coverage/"],
    }


def _developer_resolution() -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_at": "2026-08-25T13:00:00Z",
        "resolution_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "87654321-4321-6789-a234-678912345678",
        "resolves_escalation_id": (
            "22222222-2222-4222-8222-222222222222"
        ),
        "applies_to_finding_ids": ["REV-001", "REV-002"],
        "resolution_path": "001-resolution.md",
        "resolution_sha256": "a" * 64,
        "additional_rounds_granted": 1,
    }


def _active_round() -> dict[str, object]:
    return {
        "round": 1,
        "status": "reviewing",
        "mode": "new_revision",
        "request_id": "12345678-1234-5678-9234-567812345678",
        "result_id": None,
        "review_worktree": "/tmp/review",
        "reviewer_name": "asq-12345678-r001-reviewer",
    }


def _handoff() -> dict[str, object]:
    return {
        "kind": "review_request",
        "round": 1,
        "status": "sent",
        "target": "asq-12345678-r001-reviewer",
        "last_error": None,
        "updated_at": "2026-08-25T12:00:00Z",
        "herdr_version": "herdr test",
        "herdr_protocol": 20,
    }


def _round_record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_at": "2026-08-25T12:00:00Z",
        "updated_at": "2026-08-25T12:00:00Z",
        "run_id": "87654321-4321-6789-a234-678912345678",
        "round": 1,
        "mode": "new_revision",
        "request_id": "12345678-1234-5678-9234-567812345678",
        "result_id": None,
        "verdict": None,
        "base_oid": "a" * 40,
        "head_oid": "b" * 40,
        "git_object_format": "sha1",
        "status": "reviewing",
        "supersession": None,
        "classification_reason": None,
        "diagnostic_id": None,
        "observed_head_oid": None,
        "review_worktree": "/tmp/review",
        "reviewer": {
            "name": "asq-87654321-r001-reviewer",
            "kind": "claude",
            "start_args": ["--strict"],
        },
        "artifacts": {
            "request": {"path": "request.json", "sha256": "f" * 64},
            "implementation_report": {
                "path": "implementation-report.md",
                "sha256": "d" * 64,
            },
            "bundle_inputs": [
                {"path": "input/request.json", "sha256": "f" * 64},
                {"path": "input/task.md", "sha256": "c" * 64},
                {
                    "path": "input/implementation-report.md",
                    "sha256": "d" * 64,
                },
                {
                    "path": "input/context/001/notes.md",
                    "sha256": "e" * 64,
                },
            ],
            "review_result": None,
            "review_markdown": None,
            "review_marker": None,
            "approval": None,
            "bundle_archive": [],
        },
        "warnings": ["captured instruction warning"],
    }


def _review_result() -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_at": "2026-08-25T12:30:00Z",
        "result_id": "abcdefab-1234-5678-9234-567812345678",
        "request_id": "12345678-1234-5678-9234-567812345678",
        "run_id": "87654321-4321-6789-a234-678912345678",
        "round": 1,
        "base_oid": "a" * 40,
        "head_oid": "b" * 40,
        "verdict": "changes_requested",
        "summary": "One correctness issue blocks approval.",
        "findings": [
            {
                "id": "REV-001",
                "severity": "high",
                "blocking": True,
                "category": "correctness",
                "file": "src/example.py",
                "line_start": 4,
                "line_end": 7,
                "problem": "Empty input bypasses the fallback.",
                "evidence": "The early return runs first.",
                "impact": "The result is incorrect.",
                "required_change": "Handle empty input first.",
                "verification": "Add an empty-input regression test.",
            }
        ],
        "non_blocking_observations": ["Consider a shorter helper name."],
    }


def _review_marker() -> dict[str, object]:
    return {
        "schema_version": 1,
        "submitted_at": "2026-08-25T12:31:00Z",
        "request_id": "12345678-1234-5678-9234-567812345678",
        "result_id": "abcdefab-1234-5678-9234-567812345678",
        "status": "result_submitted",
        "review_json_path": "output/review.json",
        "review_sha256": "f" * 64,
    }


def _review_response() -> dict[str, object]:
    review = _review_result()
    return {
        "schema_version": 1,
        "created_at": "2026-08-25T12:40:00Z",
        "response_id": "fedcbafe-1234-5678-9234-567812345678",
        "supersedes_response_id": None,
        "resolution_ids": [],
        "run_id": review["run_id"],
        "review_round": review["round"],
        "review_result_id": review["result_id"],
        "reviewed_head_oid": review["head_oid"],
        "responses": [
            {
                "finding_id": "REV-001",
                "disposition": "fixed",
                "rationale": "The empty-input branch now uses the fallback.",
                "changed_files": [
                    "src/example.py",
                    "tests/test_example.py",
                ],
                "evidence": [],
                "verification": "python -m unittest tests.test_example",
            }
        ],
    }


def _approval_record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_at": "2026-08-25T12:32:00Z",
        "run_id": "87654321-4321-6789-a234-678912345678",
        "round": 1,
        "request_id": "12345678-1234-5678-9234-567812345678",
        "result_id": "abcdefab-1234-5678-9234-567812345678",
        "task_sha256": "c" * 64,
        "git_object_format": "sha1",
        "base_oid": "a" * 40,
        "head_oid": "b" * 40,
        "reviewer": {
            "name": "asq-87654321-r001-reviewer",
            "kind": "claude",
        },
        "review_sha256": "f" * 64,
    }


class ActiveRoundRecordTests(unittest.TestCase):
    def test_record_round_trips_with_typed_fields(self) -> None:
        record = ActiveRoundRecord.from_dict(_active_round())

        self.assertEqual(record.to_dict(), _active_round())
        self.assertEqual(record.round_number, 1)
        self.assertIs(record.status, RoundStatus.REVIEWING)
        self.assertIs(record.mode, SubmissionMode.NEW_REVISION)
        self.assertEqual(record.review_worktree, Path("/tmp/review"))

    def test_record_rejects_invalid_shape_and_values(self) -> None:
        cases = (
            (lambda data: data.update(round=0), "round must be positive"),
            (lambda data: data.update(status="waiting"), "must be one of"),
            (lambda data: data.update(mode="initial"), "must be one of"),
            (
                lambda data: data.update(request_id="not-a-uuid"),
                "canonical UUID",
            ),
            (
                lambda data: data.update(result_id="not-a-uuid"),
                "canonical UUID",
            ),
            (
                lambda data: data.update(review_worktree="relative"),
                "must be an absolute path",
            ),
            (
                lambda data: data.update(reviewer_name="Reviewer Name"),
                "valid Herdr agent name",
            ),
            (lambda data: data.update(unexpected=True), "unknown field"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                data = _active_round()
                mutate(data)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ActiveRoundRecord.from_dict(data)


class HandoffRecordTests(unittest.TestCase):
    def test_record_round_trips_with_typed_fields(self) -> None:
        record = HandoffRecord.from_dict(_handoff())

        self.assertEqual(record.to_dict(), _handoff())
        self.assertIs(record.status, HandoffStatus.SENT)
        self.assertEqual(record.herdr_protocol, 20)

    def test_record_rejects_invalid_delivery_state(self) -> None:
        cases = (
            (lambda data: data.update(round=0), "round must be positive"),
            (lambda data: data.update(status="unknown"), "must be one of"),
            (
                lambda data: data.update(status="failed", last_error=None),
                "last_error is required",
            ),
            (
                lambda data: data.update(herdr_protocol=None),
                "sent handoff must record",
            ),
            (
                lambda data: data.update(target="Reviewer Name"),
                "valid Herdr agent name",
            ),
            (lambda data: data.update(unexpected=True), "unknown field"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                data = _handoff()
                mutate(data)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    HandoffRecord.from_dict(data)


class ReviewRoundRecordTests(unittest.TestCase):
    def test_writer_and_reader_share_one_typed_round_record(self) -> None:
        request = ReviewRequest.from_dict(_request())
        written = ReviewRoundRecord.for_request(
            request,
            review_worktree=Path("/tmp/review"),
            reviewer_start_args=("--strict",),
            request_digest="f" * 64,
            warnings=("captured instruction warning",),
        )

        self.assertEqual(written.to_dict(), _round_record())
        read = ReviewRoundRecord.from_dict(
            written.to_dict(),
            label="round record",
        )
        self.assertEqual(read, written)
        self.assertIs(read.reviewer_kind, AgentKind.CLAUDE)
        self.assertIs(read.status, RoundStatus.REVIEWING)

    def test_record_rejects_invalid_shape_and_nested_values(self) -> None:
        cases = (
            (
                lambda data: data.update(schema_version=2),
                "schema_version must be 1",
            ),
            (lambda data: data.update(round=0), "round must be positive"),
            (
                lambda data: data["reviewer"].update(name="Reviewer Name"),
                "valid Herdr agent name",
            ),
            (
                lambda data: data["artifacts"]["request"].update(
                    path="input/request.json"
                ),
                "request artifact path must be request.json",
            ),
            (
                lambda data: data["artifacts"].update(
                    review_result={
                        "path": "review.json",
                        "sha256": "1" * 64,
                    }
                ),
                "cannot record result artifacts before classification",
            ),
            (
                lambda data: data.update(warnings=[""]),
                "must be a non-empty string",
            ),
            (lambda data: data.update(unexpected=True), "unknown field"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                data = copy.deepcopy(_round_record())
                mutate(data)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ReviewRoundRecord.from_dict(data, label="round record")

    def test_applied_round_requires_complete_approval_evidence(self) -> None:
        data = _round_record()
        data.update(
            result_id="abcdefab-1234-5678-9234-567812345678",
            verdict="approved",
            status="applied",
        )
        artifacts = data["artifacts"]
        assert isinstance(artifacts, dict)
        artifacts.update(
            review_result={"path": "review.json", "sha256": "1" * 64},
            review_markdown={"path": "review.md", "sha256": "2" * 64},
            review_marker={
                "path": "review-marker.json",
                "sha256": "3" * 64,
            },
            approval={"path": "approval.json", "sha256": "4" * 64},
            bundle_archive=[
                {
                    "path": "bundle/output/review.json",
                    "sha256": "1" * 64,
                }
            ],
        )

        record = ReviewRoundRecord.from_dict(data, label="round record")

        self.assertIs(record.status, RoundStatus.APPLIED)
        self.assertIs(record.verdict, ReviewVerdict.APPROVED)
        self.assertIsNotNone(record.approval)

        cases = (
            (
                "missing result",
                lambda value: value.update(result_id=None),
                "must record its applied result and verdict",
            ),
            (
                "missing result artifact",
                lambda value: value["artifacts"].update(
                    review_result=None
                ),
                "must record every applied result artifact",
            ),
            (
                "missing archive",
                lambda value: value["artifacts"].update(bundle_archive=[]),
                "must record the complete applied bundle archive",
            ),
            (
                "missing approval",
                lambda value: value["artifacts"].update(approval=None),
                "approval artifact must exist exactly",
            ),
        )
        for case, mutate, message in cases:
            with self.subTest(case=case):
                changed = copy.deepcopy(data)
                mutate(changed)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ReviewRoundRecord.from_dict(
                        changed,
                        label="round record",
                    )

    def test_stale_and_invalid_rounds_bind_classification_authority(
        self,
    ) -> None:
        stale = _round_record()
        stale.update(
            result_id="abcdefab-1234-5678-9234-567812345678",
            verdict="approved",
            status="stale",
            classification_reason=(
                "implementation HEAD advanced before review application"
            ),
            observed_head_oid="c" * 40,
        )
        stale_artifacts = stale["artifacts"]
        assert isinstance(stale_artifacts, dict)
        stale_artifacts.update(
            review_result={"path": "review.json", "sha256": "1" * 64},
            review_markdown={"path": "review.md", "sha256": "2" * 64},
            review_marker={
                "path": "review-marker.json",
                "sha256": "3" * 64,
            },
            bundle_archive=[
                {
                    "path": "bundle/output/review.json",
                    "sha256": "1" * 64,
                }
            ],
        )
        stale_record = ReviewRoundRecord.from_dict(
            stale,
            label="round record",
        )
        self.assertIs(stale_record.status, RoundStatus.STALE)
        self.assertEqual(stale_record.observed_head_oid, "c" * 40)

        invalid = _round_record()
        invalid.update(
            result_id="abcdefab-1234-5678-9234-567812345678",
            status="invalid",
            classification_reason=(
                "review result failed independent validation"
            ),
            diagnostic_id="e" * 64,
        )
        invalid_record = ReviewRoundRecord.from_dict(
            invalid,
            label="round record",
        )
        self.assertIs(invalid_record.status, RoundStatus.INVALID)
        self.assertEqual(invalid_record.diagnostic_id, "e" * 64)

        applied = copy.deepcopy(stale)
        applied.update(
            status="applied",
            classification_reason=None,
            observed_head_oid=None,
        )
        applied_artifacts = applied["artifacts"]
        assert isinstance(applied_artifacts, dict)
        applied_artifacts["approval"] = {
            "path": "approval.json",
            "sha256": "4" * 64,
        }
        superseded = _round_record()
        superseded.update(
            status="superseded",
            supersession={
                "created_at": superseded["updated_at"],
                "actor": "codex-main",
                "cause": "The requested revision is no longer relevant.",
            },
        )

        cases = (
            (
                "prepared classification details",
                {**_round_record(), "status": "prepared"},
                lambda value: value.update(classification_reason="unexpected"),
                "may record classification details only",
            ),
            (
                "reviewing classification details",
                _round_record(),
                lambda value: value.update(classification_reason="unexpected"),
                "may record classification details only",
            ),
            (
                "applied classification details",
                applied,
                lambda value: value.update(classification_reason="unexpected"),
                "may record classification details only",
            ),
            (
                "superseded classification details",
                superseded,
                lambda value: value.update(classification_reason="unexpected"),
                "may record classification details only",
            ),
            (
                "stale result ID",
                stale,
                lambda value: value.update(result_id=None),
                "must record its stale result and verdict",
            ),
            (
                "stale verdict",
                stale,
                lambda value: value.update(verdict=None),
                "must record its stale result and verdict",
            ),
            (
                "stale result artifact",
                stale,
                lambda value: value["artifacts"].update(
                    review_result=None
                ),
                "must record every stale result artifact",
            ),
            (
                "stale bundle archive",
                stale,
                lambda value: value["artifacts"].update(bundle_archive=[]),
                "must record the complete stale bundle archive",
            ),
            (
                "stale approval",
                stale,
                lambda value: value["artifacts"].update(
                    approval={"path": "approval.json", "sha256": "4" * 64}
                ),
                "cannot record approval authority for a stale result",
            ),
            (
                "stale reason",
                stale,
                lambda value: value.update(classification_reason=None),
                "must record its stale reason and observed HEAD",
            ),
            (
                "stale observed HEAD",
                stale,
                lambda value: value.update(observed_head_oid=None),
                "must record its stale reason and observed HEAD",
            ),
            (
                "stale matching observed HEAD",
                copy.deepcopy(stale),
                lambda value: value.update(observed_head_oid="b" * 40),
                "must differ from head_oid",
            ),
            (
                "stale diagnostic ID",
                stale,
                lambda value: value.update(diagnostic_id="d" * 64),
                "cannot record invalid-result diagnostics for a stale result",
            ),
            (
                "invalid reason",
                invalid,
                lambda value: value.update(classification_reason=None),
                "must record its invalid reason and diagnostic ID",
            ),
            (
                "invalid diagnostic ID",
                invalid,
                lambda value: value.update(diagnostic_id=None),
                "must record its invalid reason and diagnostic ID",
            ),
            (
                "invalid verdict",
                invalid,
                lambda value: value.update(verdict="approved"),
                "cannot record a verdict for an invalid result",
            ),
            (
                "invalid review result artifact",
                invalid,
                lambda value: value["artifacts"].update(
                    review_result={"path": "review.json", "sha256": "1" * 64}
                ),
                "must store invalid evidence only in its diagnostic archive",
            ),
            (
                "invalid review Markdown artifact",
                invalid,
                lambda value: value["artifacts"].update(
                    review_markdown={"path": "review.md", "sha256": "2" * 64}
                ),
                "must store invalid evidence only in its diagnostic archive",
            ),
            (
                "invalid review marker artifact",
                invalid,
                lambda value: value["artifacts"].update(
                    review_marker={
                        "path": "review-marker.json",
                        "sha256": "3" * 64,
                    }
                ),
                "must store invalid evidence only in its diagnostic archive",
            ),
            (
                "invalid approval artifact",
                invalid,
                lambda value: value["artifacts"].update(
                    approval={"path": "approval.json", "sha256": "4" * 64}
                ),
                "must store invalid evidence only in its diagnostic archive",
            ),
            (
                "invalid bundle archive",
                invalid,
                lambda value: value["artifacts"].update(
                    bundle_archive=[
                        {
                            "path": "bundle/output/review.json",
                            "sha256": "1" * 64,
                        }
                    ]
                ),
                "must store invalid evidence only in its diagnostic archive",
            ),
            (
                "invalid observed HEAD",
                invalid,
                lambda value: value.update(observed_head_oid="c" * 40),
                "cannot record an observed HEAD for an invalid result",
            ),
        )
        for case, base, mutate, message in cases:
            with self.subTest(case=case):
                data = copy.deepcopy(base)
                mutate(data)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ReviewRoundRecord.from_dict(data, label="round record")

    def test_superseded_round_requires_actor_timestamp_and_cause(self) -> None:
        data = _round_record()
        data.update(
            updated_at="2026-08-25T12:15:00Z",
            status="superseded",
            supersession={
                "created_at": "2026-08-25T12:15:00Z",
                "actor": "codex-main",
                "cause": "The requested revision is no longer relevant.",
            },
        )

        record = ReviewRoundRecord.from_dict(data, label="round record")

        self.assertEqual(
            record.supersession,
            ReviewSupersession(
                created_at="2026-08-25T12:15:00Z",
                actor="codex-main",
                cause="The requested revision is no longer relevant.",
            ),
        )
        self.assertEqual(record.to_dict(), data)

        cases = (
            (
                lambda value: value.update(supersession=None),
                "must record supersession authority",
            ),
            (
                lambda value: value["supersession"].update(cause=" "),
                "cause must contain non-whitespace text",
            ),
            (
                lambda value: value["supersession"].update(
                    created_at="2026-08-25T12:14:59Z"
                ),
                "timestamp must match updated_at",
            ),
            (
                lambda value: value.update(status="reviewing"),
                "round record: only a superseded round may record "
                "supersession authority",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                changed = copy.deepcopy(data)
                mutate(changed)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ReviewRoundRecord.from_dict(
                        changed,
                        label="round record",
                    )


class ApprovalRecordTests(unittest.TestCase):
    def test_record_binds_all_approval_authority(self) -> None:
        record = ApprovalRecord.from_dict(_approval_record())

        self.assertEqual(record.to_dict(), _approval_record())
        self.assertEqual(record.task_sha256, "c" * 64)
        self.assertEqual(record.head_oid, "b" * 40)
        self.assertEqual(record.reviewer_name, "asq-87654321-r001-reviewer")
        self.assertIs(record.reviewer_kind, AgentKind.CLAUDE)

    def test_record_rejects_invalid_or_ambiguous_authority(self) -> None:
        cases = (
            (
                lambda data: data.update(schema_version=2),
                "schema_version must be 1",
            ),
            (
                lambda data: data.update(task_sha256="A" * 64),
                "lowercase SHA-256",
            ),
            (
                lambda data: data.update(head_oid="b" * 39),
                "full lowercase sha1 object ID",
            ),
            (
                lambda data: data.update(result_id=data["request_id"]),
                "must be distinct",
            ),
            (
                lambda data: data["reviewer"].update(name="bad name"),
                "valid Herdr agent name",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                data = _approval_record()
                mutate(data)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ApprovalRecord.from_dict(data)


class DeveloperResolutionTests(unittest.TestCase):
    def test_resolution_round_trips_with_all_protocol_fields(self) -> None:
        resolution = DeveloperResolution.from_dict(
            _developer_resolution(),
            label="Developer resolution",
        )

        self.assertEqual(resolution.to_dict(), _developer_resolution())
        self.assertEqual(
            resolution.applies_to_finding_ids,
            ("REV-001", "REV-002"),
        )

    def test_resolution_rejects_invalid_protocol_values(self) -> None:
        cases = (
            (
                lambda data: data.update(schema_version=2),
                "schema_version must be 1",
            ),
            (
                lambda data: data.update(created_at="yesterday"),
                "RFC 3339 UTC timestamp",
            ),
            (
                lambda data: data.update(resolution_id="not-a-uuid"),
                "resolution_id must be a canonical UUID",
            ),
            (
                lambda data: data.update(run_id="not-a-uuid"),
                "run_id must be a canonical UUID",
            ),
            (
                lambda data: data.update(
                    resolves_escalation_id="not-a-uuid"
                ),
                "resolves_escalation_id must be a canonical UUID",
            ),
            (
                lambda data: data.update(applies_to_finding_ids="REV-001"),
                "applies_to_finding_ids must be a JSON array",
            ),
            (
                lambda data: data.update(applies_to_finding_ids=[""]),
                r"applies_to_finding_ids\[0\] must be a non-empty string",
            ),
            (
                lambda data: data.update(
                    applies_to_finding_ids=["REV-001", "REV-001"]
                ),
                "applies_to_finding_ids contains duplicates",
            ),
            (
                lambda data: data.update(
                    resolution_path="nested/001-resolution.md"
                ),
                "must name a companion file in the same bundle directory",
            ),
            (
                lambda data: data.update(resolution_sha256="A" * 64),
                "lowercase SHA-256 digest",
            ),
            (
                lambda data: data.update(additional_rounds_granted=True),
                "additional_rounds_granted must be an integer",
            ),
            (
                lambda data: data.update(additional_rounds_granted=-1),
                "additional_rounds_granted must not be negative",
            ),
            (
                lambda data: data.update(unexpected=True),
                "unknown field",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                data = copy.deepcopy(_developer_resolution())
                mutate(data)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    DeveloperResolution.from_dict(
                        data,
                        label="Developer resolution",
                    )


class ReviewRequestTests(unittest.TestCase):
    def test_first_round_request_round_trips(self) -> None:
        request = ReviewRequest.from_dict(_request())

        self.assertEqual(request.to_dict(), _request())
        self.assertEqual(request.round_number, 1)
        self.assertEqual(request.context_files[0].sha256, "e" * 64)

    def test_request_without_recovery_field_remains_compatible(self) -> None:
        value = _request()
        value.pop("recovery_round_path")

        request = ReviewRequest.from_dict(value)

        self.assertIsNone(request.recovery_round_path)

    def test_recovery_mode_depends_on_previous_applied_review(self) -> None:
        value = _request()
        value.update(
            round=2,
            mode="reconsideration",
            recovery_round_path="input/recovery-round.json",
            reviewer_name="asq-876543211234-r002-reviewer",
        )

        with self.assertRaisesRegex(
            ArtifactValidationError,
            "without a previous review must use mode new_revision",
        ):
            ReviewRequest.from_dict(value)

        value.update(
            previous_review_path="input/previous-review.json",
            previous_response_path="input/previous-response.json",
        )
        request = ReviewRequest.from_dict(value)

        self.assertIs(request.mode, SubmissionMode.RECONSIDERATION)
        self.assertEqual(
            request.recovery_round_path,
            "input/recovery-round.json",
        )

    def test_first_round_requires_new_revision_and_changed_head(self) -> None:
        cases = (
            ("mode", "reconsideration", "must use mode new_revision"),
            ("head_oid", "a" * 40, "head must differ"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                data = _request()
                data[field] = value
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ReviewRequest.from_dict(data)

    def test_paths_hashes_ids_and_unknown_fields_fail_closed(self) -> None:
        cases = (
            (
                lambda data: data["task"].update(path="../task.md"),
                "bundle-relative path",
            ),
            (
                lambda data: data["context_files"][0].update(
                    path="input/other.md"
                ),
                "below input/context",
            ),
            (
                lambda data: data["implementation_report"].update(
                    sha256="A" * 64
                ),
                "lowercase SHA-256",
            ),
            (
                lambda data: data.update(request_id="not-a-uuid"),
                "canonical UUID",
            ),
            (
                lambda data: data.update(reviewer_name="Reviewer Name"),
                "reviewer_name must match",
            ),
            (
                lambda data: data.update(
                    previous_review_path="input/previous-review.json"
                ),
                "first review round cannot reference previous",
            ),
            (
                lambda data: data.update(
                    resolution_paths=["input/not-resolutions/value.json"]
                ),
                "below input/resolutions",
            ),
            (
                lambda data: data.update(
                    allowed_generated_paths=["../outside"]
                ),
                "narrow repository-relative path",
            ),
            (
                lambda data: data.update(
                    allowed_generated_paths=["build", "build/"]
                ),
                "contains duplicate path",
            ),
            (
                lambda data: data.pop("allowed_generated_paths"),
                "missing required field",
            ),
            (
                lambda data: data.update(unexpected=True),
                "unknown field",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                data = copy.deepcopy(_request())
                mutate(data)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ReviewRequest.from_dict(data)

    def test_duplicate_context_and_resolution_paths_are_rejected(self) -> None:
        data = _request()
        data["context_files"] = [
            data["context_files"][0],
            data["context_files"][0],
        ]
        with self.assertRaisesRegex(
            ArtifactValidationError,
            "duplicate paths",
        ):
            ReviewRequest.from_dict(data)

        data = _request()
        data["resolution_paths"] = [
            "input/resolutions/001-resolution.json",
            "input/resolutions/001-resolution.json",
        ]
        with self.assertRaisesRegex(
            ArtifactValidationError,
            "duplicate paths",
        ):
            ReviewRequest.from_dict(data)


class ReviewResultTests(unittest.TestCase):
    def test_all_verdicts_round_trip_with_semantic_rules(self) -> None:
        cases = (
            ("changes_requested", _review_result()["findings"]),
            ("approved", []),
            ("needs_human", []),
        )
        for verdict, findings in cases:
            with self.subTest(verdict=verdict):
                data = _review_result()
                data["verdict"] = verdict
                data["findings"] = findings
                if verdict == "needs_human":
                    data["summary"] = "Decide whether compatibility wins."

                result = ReviewResult.from_dict(
                    data,
                    object_format="sha1",
                )

                self.assertEqual(result.to_dict(), data)
                self.assertIs(result.verdict, ReviewVerdict(verdict))

    def test_result_rejects_invalid_structure_and_semantics(self) -> None:
        cases = (
            (
                lambda data: data.update(schema_version=2),
                "schema_version must be 1",
            ),
            (
                lambda data: data.update(result_id="not-a-uuid"),
                "canonical UUID",
            ),
            (
                lambda data: data.update(result_id=data["request_id"]),
                "must be distinct",
            ),
            (
                lambda data: data["findings"][0].update(blocking=1),
                "must be a boolean",
            ),
            (
                lambda data: data["findings"][0].update(
                    line_start=8,
                    line_end=7,
                ),
                "line_end must be at least",
            ),
            (
                lambda data: data["findings"][0].update(file="../secret"),
                "repository-relative path",
            ),
            (
                lambda data: data["findings"].append(
                    copy.deepcopy(data["findings"][0])
                ),
                "duplicate finding IDs",
            ),
            (
                lambda data: data.update(verdict="approved"),
                "cannot contain blocking findings",
            ),
            (
                lambda data: (
                    data.update(verdict="changes_requested"),
                    data.update(findings=[]),
                ),
                "must contain at least one blocking finding",
            ),
            (
                lambda data: data.update(summary="  \n"),
                "non-whitespace text",
            ),
            (
                lambda data: data.update(
                    non_blocking_observations=[{"note": "optional"}]
                ),
                "must be a non-empty string",
            ),
            (
                lambda data: data.update(findings="REV-001"),
                "findings must be a JSON array",
            ),
            (
                lambda data: data.update(
                    non_blocking_observations="note"
                ),
                "non_blocking_observations must be a JSON array",
            ),
            (
                lambda data: data.update(unexpected=True),
                "unknown field",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                data = copy.deepcopy(_review_result())
                mutate(data)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ReviewResult.from_dict(data, object_format="sha1")


class ReviewResponseTests(unittest.TestCase):
    def test_fixed_response_round_trips_and_covers_blocking_findings(
        self,
    ) -> None:
        review = ReviewResult.from_dict(
            _review_result(),
            object_format="sha1",
        )
        response = ReviewResponse.from_dict(
            _review_response(),
            object_format="sha1",
        )

        validate_review_response(
            response,
            review,
            SubmissionMode.NEW_REVISION,
        )

        self.assertEqual(response.to_dict(), _review_response())
        self.assertIs(
            response.responses[0].disposition,
            ResponseDisposition.FIXED,
        )

    def test_response_rejects_incomplete_or_unsupported_dispositions(
        self,
    ) -> None:
        structural_cases = (
            (
                "non-string verification",
                lambda data: data["responses"][0].update(
                    verification=None
                ),
                "verification must be a string without null bytes",
            ),
            (
                "null-byte verification",
                lambda data: data["responses"][0].update(
                    verification="make test\x00ignored"
                ),
                "verification must be a string without null bytes",
            ),
            (
                "fixed without verification",
                lambda data: data["responses"][0].update(verification=" "),
                "verification is required when fixed",
            ),
            (
                "rejected without evidence",
                lambda data: data["responses"][0].update(
                    disposition="rejected",
                    evidence=[],
                ),
                "evidence is required when rejected",
            ),
            (
                "duplicate finding response",
                lambda data: data["responses"].append(
                    copy.deepcopy(data["responses"][0])
                ),
                "duplicate finding IDs",
            ),
        )
        for case, mutate, message in structural_cases:
            with self.subTest(case=case):
                data = _review_response()
                mutate(data)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ReviewResponse.from_dict(data, object_format="sha1")

        review = ReviewResult.from_dict(
            _review_result(),
            object_format="sha1",
        )
        semantic_cases = (
            (
                "missing blocking finding",
                lambda data: data.update(responses=[]),
                "missing blocking finding IDs: REV-001",
                SubmissionMode.NEW_REVISION,
            ),
            (
                "unknown finding",
                lambda data: data["responses"][0].update(
                    finding_id="REV-999"
                ),
                "unknown finding IDs: REV-999",
                SubmissionMode.NEW_REVISION,
            ),
            (
                "wrong result identity",
                lambda data: data.update(
                    review_result_id=(
                        "11111111-1111-4111-8111-111111111111"
                    )
                ),
                "review result ID does not match",
                SubmissionMode.NEW_REVISION,
            ),
            (
                "fixed reconsideration",
                lambda data: None,
                "reconsideration requires rejected dispositions",
                SubmissionMode.RECONSIDERATION,
            ),
        )
        for case, mutate, message, mode in semantic_cases:
            with self.subTest(case=case):
                data = _review_response()
                mutate(data)
                response = ReviewResponse.from_dict(
                    data,
                    object_format="sha1",
                )
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    validate_review_response(response, review, mode)

    def test_response_allows_disposition_specific_empty_fields(self) -> None:
        review = ReviewResult.from_dict(
            _review_result(),
            object_format="sha1",
        )

        fixed_data = _review_response()
        fixed_data["responses"][0]["changed_files"] = []
        fixed = ReviewResponse.from_dict(
            fixed_data,
            object_format="sha1",
        )
        validate_review_response(
            fixed,
            review,
            SubmissionMode.NEW_REVISION,
        )

        rejected_data = _review_response()
        rejected_data["responses"][0].update(
            disposition="rejected",
            changed_files=[],
            evidence=["feature.txt:1 already contains the required value"],
            verification="",
        )
        rejected = ReviewResponse.from_dict(
            rejected_data,
            object_format="sha1",
        )
        validate_review_response(
            rejected,
            review,
            SubmissionMode.RECONSIDERATION,
        )


class SubmissionSemanticsTests(unittest.TestCase):
    def test_recovery_exception_preserves_both_submission_modes(self) -> None:
        previous_head = "a" * 40
        recovery_head = "b" * 40

        validate_followup_submission_head(
            SubmissionMode.NEW_REVISION,
            head_oid=recovery_head,
            previous_reviewed_head_oid=previous_head,
            recovery_head_oid=recovery_head,
        )
        validate_followup_submission_head(
            SubmissionMode.RECONSIDERATION,
            head_oid=previous_head,
            previous_reviewed_head_oid=previous_head,
            recovery_head_oid=recovery_head,
        )

        with self.assertRaisesRegex(
            ArtifactValidationError,
            "new committed HEAD",
        ):
            validate_followup_submission_head(
                SubmissionMode.NEW_REVISION,
                head_oid=previous_head,
                previous_reviewed_head_oid=previous_head,
                recovery_head_oid=recovery_head,
            )
        with self.assertRaisesRegex(
            ArtifactValidationError,
            "exact previously reviewed HEAD",
        ):
            validate_followup_submission_head(
                SubmissionMode.RECONSIDERATION,
                head_oid=recovery_head,
                previous_reviewed_head_oid=previous_head,
                recovery_head_oid=recovery_head,
            )

    def test_unchanged_head_uses_reconsideration_response_semantics(
        self,
    ) -> None:
        previous_head = "a" * 40

        self.assertIs(
            response_validation_mode(
                SubmissionMode.NEW_REVISION,
                head_oid=previous_head,
                previous_reviewed_head_oid=previous_head,
            ),
            SubmissionMode.RECONSIDERATION,
        )
        self.assertIs(
            response_validation_mode(
                SubmissionMode.NEW_REVISION,
                head_oid="b" * 40,
                previous_reviewed_head_oid=previous_head,
            ),
            SubmissionMode.NEW_REVISION,
        )


class ReviewerLocalMarkerTests(unittest.TestCase):
    def test_marker_round_trips(self) -> None:
        marker = ReviewerLocalMarker.from_dict(_review_marker())

        self.assertEqual(marker.to_dict(), _review_marker())

    def test_marker_rejects_invalid_protocol_values(self) -> None:
        cases = (
            (
                lambda data: data.update(schema_version=2),
                "schema_version must be 1",
            ),
            (
                lambda data: data.update(status="draft"),
                "status must be result_submitted",
            ),
            (
                lambda data: data.update(review_json_path="../review.json"),
                "bundle-relative path",
            ),
            (
                lambda data: data.update(
                    review_json_path="output/other.json"
                ),
                "review_json_path must be output/review.json",
            ),
            (
                lambda data: data.update(review_sha256="A" * 64),
                "lowercase SHA-256",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                data = _review_marker()
                mutate(data)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    ReviewerLocalMarker.from_dict(data)


if __name__ == "__main__":
    unittest.main()
