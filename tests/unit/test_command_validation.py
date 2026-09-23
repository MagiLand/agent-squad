"""Command input guards reject malformed protocol data before forge calls."""

import argparse
from dataclasses import replace
from pathlib import Path
import unittest

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.cli import positive_argument  # noqa: E402
from agent_squad.commands import load_threads, workflow_paths  # noqa: E402
from agent_squad.initialization import (  # noqa: E402
    AgentSquadError, Repository,
)
from tests.forge_support import finding  # noqa: E402
from tests.unit.test_conventions import config  # noqa: E402


class CommandValidationTests(unittest.TestCase):
    def test_workflow_paths_resolves_issue_scratch_from_configured_primary(
        self,
    ) -> None:
        primary = Path("/unused-primary").resolve()
        for scratch_root in (
            ".agent-squad/review-scratch", "/external-scratch",
        ):
            with self.subTest(scratch_root=scratch_root):
                repository = Repository(
                    primary / ".agent-squad/worktrees/issue-42",
                    primary, primary / ".git",
                    replace(config(), scratch_root=scratch_root),
                )
                expected = (primary / scratch_root).resolve()
                paths = workflow_paths(repository, issue=42, pr=7)
                self.assertEqual(
                    paths["issue_scratch"], str(expected / "issue-42"))
                self.assertEqual(paths["scratch"], str(expected / "pr7"))
                self.assertIsNone(workflow_paths(repository)["issue_scratch"])
                self.assertIsNone(
                    workflow_paths(repository, pr=7)["issue_scratch"])

    def test_thread_body_requires_each_evidence_paragraph(self) -> None:
        for label in (
            "Problem",
            "Evidence",
            "Impact",
            "Required change",
            "Verification",
        ):
            for replacement in ("", f"**{label}**: "):
                with self.subTest(label=label, replacement=replacement):
                    item = finding()
                    paragraphs = item["body"].split("\n\n")
                    item["body"] = "\n\n".join(
                        replacement if p.startswith(f"**{label}**") else p
                        for p in paragraphs
                    )
                    with self.assertRaisesRegex(
                        AgentSquadError, "thread body requires"
                    ):
                        load_threads([item])

    def test_positive_argument_is_canonical_decimal(self) -> None:
        self.assertEqual(positive_argument("12"), 12)
        for value in ("0", "-1", "01", "+1", " 1", "1 ", "1.0", "one"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    argparse.ArgumentTypeError, "positive decimal integer"
                ),
            ):
                positive_argument(value)
