"""Command input guards reject malformed protocol data before forge calls."""

import argparse
import unittest

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.cli import positive_argument  # noqa: E402
from agent_squad.commands import load_threads  # noqa: E402
from agent_squad.initialization import AgentSquadError  # noqa: E402
from tests.forge_support import finding  # noqa: E402


class CommandValidationTests(unittest.TestCase):
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
