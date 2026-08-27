from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path, install_fake_herdr


add_src_to_path()

from agent_squad.herdr import HerdrClient, HerdrError  # noqa: E402
from agent_squad.initialization import AgentKind  # noqa: E402


class FakeHerdrAdapterTests(unittest.TestCase):
    def test_discovers_launches_prompts_and_adopts_exact_reviewer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, environment = install_fake_herdr(root)
            review_worktree = root / "review-worktree"
            review_worktree.mkdir()

            with mock.patch.dict(os.environ, environment):
                client = HerdrClient(root)
                installation = client.discover(
                    AgentKind.CLAUDE,
                    role="Reviewer",
                )
                launched = client.dispatch_review_request(
                    reviewer_name="asq-12345678-r001-reviewer",
                    reviewer_kind=AgentKind.CLAUDE,
                    start_args=("--profile", "careful"),
                    review_worktree=review_worktree,
                    prompt="AGENT_SQUAD/0.4.4 REVIEW_REQUEST",
                )
                adopted = client.dispatch_review_request(
                    reviewer_name="asq-12345678-r001-reviewer",
                    reviewer_kind=AgentKind.CLAUDE,
                    start_args=("--profile", "careful"),
                    review_worktree=review_worktree,
                    prompt="AGENT_SQUAD/0.4.4 REVIEW_REQUEST",
                )

            self.assertEqual(installation.protocol, 20)
            self.assertEqual(installation.version, "herdr test-0.8.2")
            self.assertFalse(launched.adopted)
            self.assertTrue(adopted.adopted)
            self.assertEqual(launched.workspace_id, "w-test")
            events = [
                json.loads(line)
                for line in (
                    root / "fake-herdr-state/invocations.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            starts = [
                event
                for event in events
                if event["arguments"][:2] == ["agent", "start"]
                and event["arguments"] != ["agent", "start", "--help"]
            ]
            prompts = [
                event
                for event in events
                if event["arguments"][:2] == ["agent", "prompt"]
                and event["arguments"] != ["agent", "prompt", "--help"]
            ]
            self.assertEqual(len(starts), 1)
            self.assertEqual(len(prompts), 2)
            self.assertIn("--profile", starts[0]["arguments"])

    def test_missing_installed_capability_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, environment = install_fake_herdr(root)
            environment["FAKE_HERDR_SCHEMA_MISSING"] = "agent.prompt"

            with mock.patch.dict(os.environ, environment):
                with self.assertRaisesRegex(
                    HerdrError,
                    "missing required method contract agent.prompt",
                ):
                    HerdrClient(root).discover(
                        AgentKind.CLAUDE,
                        role="Reviewer",
                    )

    def test_stale_integration_error_identifies_the_role(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, environment = install_fake_herdr(root)
            environment["FAKE_HERDR_STALE_KIND"] = "codex"

            for role in ("Reviewer", "Implementer"):
                with self.subTest(role=role):
                    with mock.patch.dict(os.environ, environment):
                        with self.assertRaisesRegex(
                            HerdrError,
                            rf"integration for {role} kind 'codex'",
                        ):
                            HerdrClient(root).discover(
                                AgentKind.CODEX,
                                role=role,
                            )


if __name__ == "__main__":
    unittest.main()
