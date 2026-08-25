from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import submissions  # noqa: E402


class SubmissionHelperTests(unittest.TestCase):
    def test_deterministic_reviewer_name_is_stable_and_safe(self) -> None:
        run_id = "12345678-1234-5678-9234-567812345678"

        name = submissions.deterministic_reviewer_name(run_id, 1)

        self.assertEqual(name, "asq-123456781234-r001-reviewer")
        self.assertLessEqual(len(name), 32)
        self.assertEqual(
            submissions.deterministic_reviewer_name(run_id, 1),
            name,
        )

    def test_generated_path_matching_uses_path_components(self) -> None:
        configured = ("build/", "coverage/report/")

        self.assertTrue(
            submissions._matches_allowed_generated_path(
                "build/output.bin",
                configured,
            )
        )
        self.assertTrue(
            submissions._matches_allowed_generated_path(
                "coverage/report/index.html",
                configured,
            )
        )
        self.assertFalse(
            submissions._matches_allowed_generated_path(
                "building/output.bin",
                configured,
            )
        )

        self.assertTrue(
            submissions._is_agent_squad_runtime_path(
                ".agent-squad/runs/run-id/run.json"
            )
        )
        self.assertFalse(
            submissions._is_agent_squad_runtime_path(
                "nested/.agent-squad/data.json"
            )
        )
        self.assertFalse(
            submissions._matches_allowed_generated_path(
                "coverage/other.txt",
                configured,
            )
        )

    def test_sensitive_path_detection_is_narrow_and_deterministic(
        self,
    ) -> None:
        sensitive = (
            "AGENTS.md",
            "src/feature/AGENTS.md",
            ".agents/reviewer.md",
            "src/agent_squad/cli.py",
            "scripts/herdr_bridge.sh",
            "prompts/reviewer-prompt.md",
        )
        for path in sensitive:
            with self.subTest(path=path):
                self.assertTrue(submissions._is_sensitive_path(path))
        for path in ("src/product.py", "docs/design.md", "herdrology.txt"):
            with self.subTest(path=path):
                self.assertFalse(submissions._is_sensitive_path(path))

    def test_report_requires_nonempty_utf8_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            valid = root / "valid.md"
            valid.write_bytes(b"# Report\n")
            report = submissions._capture_report(valid, root)
            self.assertEqual(report.content, b"# Report\n")

            empty = root / "empty.md"
            empty.write_text(" \n", encoding="utf-8")
            binary = root / "binary.md"
            binary.write_bytes(b"\xff")
            cases = (
                (empty, "must not be empty"),
                (binary, "must contain UTF-8"),
                (root / "missing.md", "cannot read"),
                (root, "regular file"),
            )
            for path, message in cases:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(
                        submissions.SubmissionError,
                        message,
                    ):
                        submissions._capture_report(path, root)

    def test_report_fifo_is_rejected_before_opening(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fifo = root / "report.pipe"
            os.mkfifo(fifo)

            with mock.patch.object(
                Path,
                "open",
                side_effect=AssertionError("FIFO must not be opened"),
            ) as open_file:
                with self.assertRaisesRegex(
                    submissions.SubmissionError,
                    "regular file",
                ):
                    submissions._capture_report(fifo, root)

            open_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
