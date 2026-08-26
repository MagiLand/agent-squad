from __future__ import annotations

from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import unittest

from tests._support import (
    add_src_to_path,
    initialize_git_repository,
    run,
)


add_src_to_path()

from agent_squad import review_submissions  # noqa: E402


class ReviewSubmissionHelperTests(unittest.TestCase):
    def test_blob_oid_matches_git_for_both_object_formats(self) -> None:
        content = b"exact symlink target\n"
        for object_format in ("sha1", "sha256"):
            with self.subTest(object_format=object_format):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    repository = Path(temporary_directory) / "repository"
                    try:
                        initialize_git_repository(
                            repository,
                            object_format=object_format,
                        )
                    except subprocess.CalledProcessError:
                        if object_format == "sha256":
                            continue
                        raise
                    payload = repository / "payload.bin"
                    payload.write_bytes(content)
                    expected = run(
                        ["git", "hash-object", "payload.bin"],
                        cwd=repository,
                    ).stdout.strip()

                    self.assertEqual(
                        review_submissions._blob_oid(
                            content,
                            object_format,
                        ),
                        expected,
                    )

        with self.assertRaisesRegex(
            review_submissions.ReviewSubmissionError,
            "unsupported Git object format",
        ):
            review_submissions._blob_oid(b"content", "unknown")

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
