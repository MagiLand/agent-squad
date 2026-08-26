from __future__ import annotations

import copy
from pathlib import Path
import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad.artifacts import (  # noqa: E402
    ActiveRoundRecord,
    ArtifactValidationError,
    HandoffRecord,
    HandoffStatus,
    ReviewRequest,
    ReviewRoundRecord,
    RoundStatus,
    SubmissionMode,
)
from agent_squad.initialization import AgentKind  # noqa: E402


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
        "resolution_paths": [],
        "review_output_path": "output/review.json",
        "review_markdown_path": "output/review.md",
        "implementer_agent": "codex-main",
        "implementer_kind": "codex",
        "reviewer_kind": "claude",
        "reviewer_name": "asq-87654321-r001-reviewer",
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
        "base_oid": "a" * 40,
        "head_oid": "b" * 40,
        "git_object_format": "sha1",
        "status": "reviewing",
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
        },
        "warnings": ["captured instruction warning"],
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


class ReviewRequestTests(unittest.TestCase):
    def test_first_round_request_round_trips(self) -> None:
        request = ReviewRequest.from_dict(_request())

        self.assertEqual(request.to_dict(), _request())
        self.assertEqual(request.round_number, 1)
        self.assertEqual(request.context_files[0].sha256, "e" * 64)

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


if __name__ == "__main__":
    unittest.main()
