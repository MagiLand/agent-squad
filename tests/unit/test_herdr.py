from __future__ import annotations

from pathlib import Path
import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad.herdr import (  # noqa: E402
    HerdrClient,
    HerdrError,
    format_herdr_error,
)
from agent_squad.initialization import AgentKind  # noqa: E402


class HerdrHelperTests(unittest.TestCase):
    def test_error_formatting_is_single_line_with_a_fallback(self) -> None:
        self.assertEqual(
            format_herdr_error("  first line\nsecond line  "),
            "first line second line",
        )
        self.assertEqual(format_herdr_error(" \n "), "unknown Herdr error")

    def test_agent_identity_requires_a_name_for_both_roles(self) -> None:
        client = HerdrClient(Path.cwd())
        for role in ("Reviewer", "Implementer"):
            with self.subTest(role=role):
                with self.assertRaisesRegex(
                    HerdrError,
                    rf"returned {role} name None",
                ):
                    client._validate_agent_identity(
                        {"agent": "codex"},
                        expected_name="codex-main",
                        expected_kind=AgentKind.CODEX,
                        role=role,
                    )


if __name__ == "__main__":
    unittest.main()
