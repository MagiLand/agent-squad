from __future__ import annotations

from pathlib import Path
import tempfile
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

    def test_reviewer_notice_is_skipped_when_session_is_absent(self) -> None:
        client = HerdrClient(Path.cwd())
        with (
            mock.patch.object(client, "_get_agent", return_value=None),
            mock.patch.object(client, "_prompt_agent") as prompt_agent,
        ):
            sent = client.dispatch_reviewer_notice(
                reviewer_name="asq-12345678-r001-reviewer",
                reviewer_kind=AgentKind.CLAUDE,
                review_worktree=Path.cwd(),
                prompt="review superseded",
            )

        self.assertFalse(sent)
        prompt_agent.assert_not_called()

    def test_reviewer_notice_revalidates_prompted_session(self) -> None:
        review_worktree = Path.cwd()
        reviewer_name = "asq-12345678-r001-reviewer"
        agent = {
            "name": reviewer_name,
            "agent": "claude",
            "cwd": str(review_worktree),
        }
        client = HerdrClient(review_worktree)
        with (
            mock.patch.object(client, "_get_agent", return_value=agent),
            mock.patch.object(
                client,
                "_prompt_agent",
                return_value=agent,
            ) as prompt_agent,
            mock.patch.object(client, "_validate_agent") as validate_agent,
        ):
            sent = client.dispatch_reviewer_notice(
                reviewer_name=reviewer_name,
                reviewer_kind=AgentKind.CLAUDE,
                review_worktree=review_worktree,
                prompt="review superseded",
            )

        self.assertTrue(sent)
        prompt_agent.assert_called_once_with(
            reviewer_name,
            "review superseded",
        )
        self.assertEqual(validate_agent.call_count, 2)

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

    def test_history_probe_degrades_read_failure_to_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            review_worktree = Path(temporary_directory)
            reviewer_name = "asq-12345678-r001-reviewer"
            client = HerdrClient(review_worktree)
            existing = {
                "name": reviewer_name,
                "agent": "claude",
                "cwd": str(review_worktree),
            }
            process = mock.Mock(
                returncode=1,
                stdout="",
                stderr="history is unavailable",
            )

            with (
                mock.patch.object(
                    client,
                    "_get_agent",
                    return_value=existing,
                ),
                mock.patch.object(
                    client,
                    "_run",
                    return_value=process,
                ) as run_command,
            ):
                probe = client.probe_review_request(
                    reviewer_name=reviewer_name,
                    reviewer_kind=AgentKind.CLAUDE,
                    review_worktree=review_worktree,
                )

            self.assertIsNone(probe.history)
            run_command.assert_called_once_with(
                (
                    "agent",
                    "read",
                    reviewer_name,
                    "--source",
                    "recent-unwrapped",
                    "--lines",
                    "1000",
                    "--format",
                    "text",
                ),
                allow_failure=True,
            )

    def test_history_probe_degrades_execution_error_to_no_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            review_worktree = Path(temporary_directory)
            reviewer_name = "asq-12345678-r001-reviewer"
            client = HerdrClient(review_worktree)
            existing = {
                "name": reviewer_name,
                "agent": "claude",
                "cwd": str(review_worktree),
            }

            with (
                mock.patch.object(
                    client,
                    "_get_agent",
                    return_value=existing,
                ),
                mock.patch.object(
                    client,
                    "_run",
                    side_effect=HerdrError("history timed out"),
                ),
            ):
                probe = client.probe_review_request(
                    reviewer_name=reviewer_name,
                    reviewer_kind=AgentKind.CLAUDE,
                    review_worktree=review_worktree,
                )

            self.assertIsNone(probe.history)


if __name__ == "__main__":
    unittest.main()
