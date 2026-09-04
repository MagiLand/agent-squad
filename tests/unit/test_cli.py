from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import cli  # noqa: E402


class InvocationDirectoryTests(unittest.TestCase):
    def test_commands_report_unavailable_invocation_directory(self) -> None:
        cases = (
            ("init",),
            ("start", "--task", "task.md"),
            ("status",),
            (
                "submit",
                "--report",
                "report.md",
                "--mode",
                "new_revision",
            ),
            ("review-submit",),
            ("apply-review",),
            ("supersede", "--reason", "review is obsolete"),
            ("retry-handoff",),
            ("escalate",),
            ("resume", "--resolution", "resolution.md"),
            ("complete",),
        )
        for arguments in cases:
            with self.subTest(command=arguments[0]):
                stdout = StringIO()
                stderr = StringIO()
                error = FileNotFoundError(
                    2,
                    "simulated missing current directory",
                )
                with (
                    mock.patch.object(Path, "cwd", side_effect=error),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    result = cli.main(arguments)

                self.assertEqual(result, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn(
                    "agent-squad: error: cannot determine the current "
                    "directory",
                    stderr.getvalue(),
                )
                self.assertIn(
                    "simulated missing current directory",
                    stderr.getvalue(),
                )
                self.assertNotIn("Traceback", stderr.getvalue())


class LocalStateErrorTests(unittest.TestCase):
    def test_commands_report_unexpected_operating_system_errors(self) -> None:
        cases = (
            (("init",), "initialize_repository"),
            (("start", "--task", "task.md"), "start_run"),
            (("status",), "inspect_status"),
            (
                (
                    "submit",
                    "--report",
                    "report.md",
                    "--mode",
                    "new_revision",
                ),
                "submit_candidate",
            ),
            (("review-submit",), "submit_review_result"),
            (("apply-review",), "apply_review"),
            (
                ("supersede", "--reason", "review is obsolete"),
                "supersede_review",
            ),
            (("retry-handoff",), "retry_handoff"),
            (("escalate",), "escalate_run"),
            (
                ("resume", "--resolution", "resolution.md"),
                "resume_run",
            ),
            (("complete",), "complete_run"),
        )
        for arguments, operation in cases:
            with self.subTest(command=arguments[0]):
                stdout = StringIO()
                stderr = StringIO()
                error = PermissionError(
                    13,
                    "simulated unreadable local state",
                )
                with (
                    mock.patch.object(cli, operation, side_effect=error),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    result = cli.main(arguments)

                self.assertEqual(result, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn(
                    "agent-squad: error: cannot access local state",
                    stderr.getvalue(),
                )
                self.assertIn(
                    "simulated unreadable local state",
                    stderr.getvalue(),
                )
                self.assertNotIn("Traceback", stderr.getvalue())


class ApplyReviewOutputTests(unittest.TestCase):
    def test_output_uses_the_typed_verdict_for_head_authority(self) -> None:
        base = {
            "run_id": "12345678-1234-5678-9234-567812345678",
            "round_number": 1,
            "result_id": "87654321-4321-6789-a234-678912345678",
            "classification": cli.RoundStatus.APPLIED,
            "head_oid": "a" * 40,
            "observed_head_oid": None,
            "bundle_archive": Path("/archive"),
            "diagnostic_path": None,
            "reason": None,
            "replayed": False,
            "next_action": "next",
            "cleanup_warnings": (),
        }
        cases = (
            (
                cli.ReviewVerdict.CHANGES_REQUESTED,
                Path("/unexpected-approval"),
                "Reviewed head:",
                "Approval authority:",
            ),
            (
                cli.ReviewVerdict.APPROVED,
                Path("/approval.json"),
                "Approval authority: /approval.json",
                "Reviewed head:",
            ),
        )
        for verdict, approval_path, expected, absent in cases:
            with self.subTest(verdict=verdict):
                stdout = StringIO()
                with (
                    mock.patch.object(
                        cli,
                        "apply_review",
                        return_value=SimpleNamespace(
                            **base,
                            verdict=verdict,
                            approval_path=approval_path,
                        ),
                    ),
                    redirect_stdout(stdout),
                ):
                    result = cli._run_apply_review(
                        SimpleNamespace(result_id=None)
                    )

                self.assertEqual(result, 0)
                self.assertIn(expected, stdout.getvalue())
                self.assertNotIn(absent, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
