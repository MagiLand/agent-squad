from __future__ import annotations

from pathlib import Path, PurePosixPath
import tempfile
import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import review_submissions  # noqa: E402


class ReviewSubmissionHelperTests(unittest.TestCase):
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
