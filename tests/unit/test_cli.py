from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
