from __future__ import annotations

import copy
import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad.artifacts import (  # noqa: E402
    ArtifactValidationError,
    ReviewRequest,
)


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
