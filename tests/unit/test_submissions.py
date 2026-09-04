from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import submissions  # noqa: E402
from agent_squad.initialization import (  # noqa: E402
    is_agent_squad_runtime_path,
    matches_allowed_generated_path,
)


class SubmissionHelperTests(unittest.TestCase):
    def test_failure_normalization_preserves_interruptions(self) -> None:
        ordinary = OSError("disk full")
        with self.assertRaisesRegex(
            submissions.SubmissionError,
            "could not persist",
        ) as raised:
            submissions._raise_submission_failure(
                ordinary,
                "could not persist",
            )
        self.assertIs(raised.exception.__cause__, ordinary)

        interruption = KeyboardInterrupt("stop")
        with self.assertRaises(KeyboardInterrupt) as interrupted:
            submissions._raise_submission_failure(
                interruption,
                "rollback context",
            )
        self.assertIs(interrupted.exception, interruption)
        self.assertIn(
            "rollback context",
            interrupted.exception.__notes__,
        )

    def test_followup_uses_the_latest_applied_historical_review(self) -> None:
        authority = SimpleNamespace(
            round_directory=Path("/run/rounds/001"),
            round_record=SimpleNamespace(
                round_number=1,
                verdict=submissions.ReviewVerdict.CHANGES_REQUESTED,
            ),
            review=SimpleNamespace(head_oid="a" * 40),
            review_bytes=b"review\n",
        )
        active = SimpleNamespace(
            current_round=2,
            current_head_oid="c" * 40,
            active_round=SimpleNamespace(
                request_id="current-request",
                result_id="current-result",
                status=submissions.RoundStatus.INVALID,
            ),
            run_id="12345678-1234-5678-9234-567812345678",
            base_oid="b" * 40,
            git_object_format="sha1",
        )
        with mock.patch.object(
            submissions.runs,
            "find_latest_applied_review_before",
            return_value=authority,
        ) as latest:
            result = submissions._load_applied_review(
                Path("/run"),
                active,
            )

        self.assertEqual(result.round_directory, authority.round_directory)
        self.assertIs(result.review, authority.review)
        latest.assert_called_once_with(
            run_directory=Path("/run"),
            run_id=active.run_id,
            current_round=3,
            base_oid=active.base_oid,
            object_format=active.git_object_format,
        )

    def test_generated_path_matching_uses_path_components(self) -> None:
        configured = ("build/", "coverage/report/")

        self.assertTrue(
            matches_allowed_generated_path(
                "build/output.bin",
                configured,
            )
        )
        self.assertTrue(
            matches_allowed_generated_path(
                "coverage/report/index.html",
                configured,
            )
        )
        self.assertFalse(
            matches_allowed_generated_path(
                "building/output.bin",
                configured,
            )
        )

        self.assertTrue(
            is_agent_squad_runtime_path(
                ".agent-squad/runs/run-id/run.json"
            )
        )
        self.assertFalse(
            is_agent_squad_runtime_path(
                "nested/.agent-squad/data.json"
            )
        )
        self.assertFalse(
            matches_allowed_generated_path(
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
            ".github/copilot-instructions.md",
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

    def test_resolved_needs_human_review_requires_one_linked_resolution(
        self,
    ) -> None:
        result_id = "11111111-1111-4111-8111-111111111111"
        escalation_id = "22222222-2222-4222-8222-222222222222"
        previous = SimpleNamespace(
            review=SimpleNamespace(
                verdict=submissions.ReviewVerdict.NEEDS_HUMAN,
                result_id=result_id,
            )
        )
        escalation = SimpleNamespace(
            record=SimpleNamespace(
                escalation_id=escalation_id,
                source_result_id=result_id,
            )
        )
        resolution = SimpleNamespace(
            record=SimpleNamespace(
                resolves_escalation_id=escalation_id,
            )
        )
        active = SimpleNamespace(
            escalations=(escalation,),
            resolutions=(resolution,),
        )
        submissions._validate_resolved_needs_human_review(active, previous)

        cases = (
            (
                SimpleNamespace(
                    review=SimpleNamespace(
                        verdict=submissions.ReviewVerdict.CHANGES_REQUESTED,
                        result_id=result_id,
                    )
                ),
                active,
                "expected an applied needs_human review",
            ),
            (
                previous,
                SimpleNamespace(escalations=(), resolutions=()),
                "does not have exactly one linked Developer escalation",
            ),
            (
                previous,
                SimpleNamespace(
                    escalations=(escalation, escalation),
                    resolutions=(resolution,),
                ),
                "does not have exactly one linked Developer escalation",
            ),
            (
                previous,
                SimpleNamespace(
                    escalations=(escalation,),
                    resolutions=(),
                ),
                "has not been resolved by the Developer",
            ),
        )
        for candidate, run_status, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    submissions.SubmissionError,
                    message,
                ):
                    submissions._validate_resolved_needs_human_review(
                        run_status,
                        candidate,
                    )


if __name__ == "__main__":
    unittest.main()
