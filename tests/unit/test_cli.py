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


if __name__ == "__main__":
    unittest.main()
