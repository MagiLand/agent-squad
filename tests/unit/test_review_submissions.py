from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import review_submissions  # noqa: E402


class ReviewSubmissionHelperTests(unittest.TestCase):
    def test_retired_result_id_is_bound_to_its_original_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory)
            request = SimpleNamespace(
                run_id="12345678-1234-5678-9234-567812345678",
                round_number=1,
                request_id="87654321-4321-6789-a234-678912345678",
            )
            result_id = "11111111-1111-4111-8111-111111111111"
            original_digest = "a" * 64

            with mock.patch.object(
                review_submissions,
                "utc_timestamp",
                return_value="2026-08-31T14:00:00Z",
            ) as timestamp:
                review_submissions.record_retired_review_identity(
                    bundle,
                    request=request,
                    result_id=result_id,
                    review_sha256=original_digest,
                )
                review_submissions.record_retired_review_identity(
                    bundle,
                    request=request,
                    result_id=result_id,
                    review_sha256=original_digest,
                )

            self.assertEqual(timestamp.call_count, 1)
            ledger = json.loads(
                (
                    bundle
                    / review_submissions.RETIRED_RESULTS_PATH.name
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                ledger,
                {
                    "schema_version": 1,
                    "created_at": "2026-08-31T14:00:00Z",
                    "run_id": request.run_id,
                    "round": 1,
                    "request_id": request.request_id,
                    "results": [
                        {
                            "retired_at": "2026-08-31T14:00:00Z",
                            "result_id": result_id,
                            "review_sha256": original_digest,
                        }
                    ],
                },
            )
            retired = review_submissions._load_retired_review_ledger(
                bundle,
                request=request,
            )
            review_submissions._assert_retired_result_reuse(
                retired,
                result_id=result_id,
                review_digest=original_digest,
            )
            with self.assertRaisesRegex(
                review_submissions.ReviewSubmissionError,
                "already bound to a different digest",
            ):
                review_submissions.record_retired_review_identity(
                    bundle,
                    request=request,
                    result_id=result_id,
                    review_sha256="b" * 64,
                )
            with self.assertRaisesRegex(
                review_submissions.ReviewSubmissionError,
                "corrected review content needs a new result ID",
            ):
                review_submissions._assert_retired_result_reuse(
                    retired,
                    result_id=result_id,
                    review_digest="b" * 64,
                )

    def test_retired_identity_ledger_rejects_invalid_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory)
            request = SimpleNamespace(
                run_id="12345678-1234-5678-9234-567812345678",
                round_number=1,
                request_id="87654321-4321-6789-a234-678912345678",
            )
            review_submissions.record_retired_review_identity(
                bundle,
                request=request,
                result_id="11111111-1111-4111-8111-111111111111",
                review_sha256="a" * 64,
            )
            path = bundle / review_submissions.RETIRED_RESULTS_PATH.name
            valid = json.loads(path.read_text(encoding="utf-8"))
            cases: tuple[tuple[str, object, str], ...] = (
                (
                    "schema_version",
                    2,
                    "schema_version must be 1",
                ),
                (
                    "results",
                    [],
                    "results must be a non-empty JSON array",
                ),
                (
                    "results",
                    [valid["results"][0], valid["results"][0]],
                    "contains duplicate result IDs",
                ),
                (
                    "run_id",
                    "99999999-9999-4999-8999-999999999999",
                    "run ID does not match the request",
                ),
            )
            for field, value, message in cases:
                with self.subTest(field=field, message=message):
                    invalid = copy.deepcopy(valid)
                    invalid[field] = value
                    path.write_text(
                        f"{json.dumps(invalid, indent=2)}\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        review_submissions.ReviewSubmissionError,
                        message,
                    ):
                        review_submissions._load_retired_review_ledger(
                            bundle,
                            request=request,
                        )

    def test_retired_identity_write_failures_leave_no_artifact(self) -> None:
        request = SimpleNamespace(
            run_id="12345678-1234-5678-9234-567812345678",
            round_number=1,
            request_id="87654321-4321-6789-a234-678912345678",
        )
        for failure, error_type, message in (
            (OSError("disk full"), OSError, "disk full"),
            (
                None,
                review_submissions.ReviewSubmissionError,
                "persisted retired review identities differ",
            ),
        ):
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    bundle = Path(temporary_directory)
                    patch = (
                        mock.patch.object(
                            review_submissions,
                            "atomic_write",
                            side_effect=failure,
                        )
                        if failure is not None
                        else mock.patch.object(
                            review_submissions,
                            "atomic_write",
                            return_value=None,
                        )
                    )
                    with patch, self.assertRaisesRegex(
                        error_type,
                        message,
                    ):
                        review_submissions.record_retired_review_identity(
                            bundle,
                            request=request,
                            result_id=(
                                "11111111-1111-4111-8111-111111111111"
                            ),
                            review_sha256="a" * 64,
                        )
                    self.assertFalse(
                        (
                            bundle
                            / review_submissions.RETIRED_RESULTS_PATH.name
                        ).exists()
                    )

    def test_blob_oid_matches_known_git_hashes(self) -> None:
        content = b"exact symlink target\n"
        expected_oids = {
            "sha1": "03a38e004027bae0ea3d8590da040688ca158986",
            "sha256": (
                "060a6873b14c8cf1983c516baddc17217907d69e3b145945"
                "af0a2daded20bd21"
            ),
        }
        for object_format, expected in expected_oids.items():
            with self.subTest(object_format=object_format):
                self.assertEqual(
                    review_submissions._blob_oid(content, object_format),
                    expected,
                )

        with self.assertRaisesRegex(
            review_submissions.ReviewSubmissionError,
            "unsupported Git object format",
        ):
            review_submissions._blob_oid(b"content", "unknown")

    def test_tracked_path_reports_uninspectable_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            (repository / "nested").mkdir()

            with self.assertRaisesRegex(
                review_submissions.ReviewSubmissionError,
                "cannot inspect tracked review directory nested/deeper: ",
            ):
                review_submissions._tracked_path(
                    repository,
                    "nested/deeper/leaf/feature.txt",
                )

    def test_case_collision_detection_uses_casefolded_paths(self) -> None:
        with self.assertRaisesRegex(
            review_submissions.ReviewSubmissionError,
            "case-colliding paths",
        ):
            review_submissions._reject_case_collisions(
                {
                    PurePosixPath("context/Plan.md"),
                    PurePosixPath("context/plan.md"),
                },
                "fixture",
            )

        review_submissions._reject_case_collisions(
            {
                PurePosixPath("context/Plan.md"),
                PurePosixPath("context/notes.md"),
            },
            "fixture",
        )

    def test_only_untracked_configured_outputs_are_ignored(self) -> None:
        output = "\0".join(
            (
                "?? build/output.bin",
                "?? building/output.bin",
                " M build/tracked.bin",
                "",
            )
        )

        self.assertEqual(
            review_submissions._unexpected_worktree_entries(
                output,
                ("build/",),
            ),
            ("?? building/output.bin", " M build/tracked.bin"),
        )


if __name__ == "__main__":
    unittest.main()
