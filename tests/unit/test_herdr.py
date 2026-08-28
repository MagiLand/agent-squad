from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

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

    def test_prompt_agent_requires_an_agent_object(self) -> None:
        client = HerdrClient(Path.cwd())
        process = mock.Mock(
            stdout=(
                '{"id":"fake:response","result":'
                '{"type":"agent_prompted"}}'
            )
        )
        with mock.patch.object(
            client,
            "_run",
            return_value=process,
        ) as run_command:
            with self.assertRaisesRegex(
                HerdrError,
                "Herdr prompt response has no agent object",
            ):
                client._prompt_agent("codex-main", "result ready")

        run_command.assert_called_once_with(
            ("agent", "prompt", "codex-main", "result ready")
        )

    def test_agent_lookup_failure_identifies_the_role(self) -> None:
        client = HerdrClient(Path.cwd())
        process = mock.Mock(
            returncode=2,
            stdout="",
            stderr="injected lookup failure",
        )
        with mock.patch.object(
            client,
            "_run",
            return_value=process,
        ) as run_command:
            with self.assertRaisesRegex(
                HerdrError,
                "could not inspect Implementer session: "
                "injected lookup failure",
            ):
                client._get_agent(
                    "codex-main",
                    role="Implementer",
                )

        run_command.assert_called_once_with(
            ("agent", "get", "codex-main"),
            allow_failure=True,
        )


if __name__ == "__main__":
    unittest.main()
