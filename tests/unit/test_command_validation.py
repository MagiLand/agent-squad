"""Command input guards reject malformed protocol data before forge calls."""

import argparse
import contextlib
from dataclasses import replace
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.cli import main, positive_argument  # noqa: E402
from agent_squad.commands import (  # noqa: E402
    compose_reply, compose_review, load_threads, workflow_paths,
)
from agent_squad.conventions import validate_review_body  # noqa: E402
from agent_squad.initialization import (  # noqa: E402
    AgentSquadError, Repository,
)
from tests.forge_support import REVIEW, SUMMARY, finding  # noqa: E402
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


SHA = "c" * 40


class ReplyCompositionTests(unittest.TestCase):
    def test_options_write_the_hand_written_tagged_lines(self) -> None:
        for options, prose, expected in (
            ({"disposition": "fixed", "sha": SHA}, "Run the probe.\n",
             f"DISPOSITION fixed {SHA}\n\nRun the probe."),
            ({"disposition": "rejected"}, "\nEvidence.\n",
             "DISPOSITION rejected\n\nEvidence."),
            ({"disposition": "rejected", "not_pursued": True},
             "unnecessary here.\n\nDetail.",
             "DISPOSITION rejected\n\nNot pursued: unnecessary here.\n\n"
             "Detail."),
            ({"disposition": "rejected", "deferred_to": 7}, "follow-up.",
             "DISPOSITION rejected\n\nDeferred to #7: follow-up."),
            ({"disposition": "rejected", "deferred_to": 7}, " \n",
             "DISPOSITION rejected\n\nDeferred to #7:"),
            ({"disposition": "needs-human"}, "A Developer choice.",
             "DISPOSITION needs-human\n\nA Developer choice."),
            ({"verification": "fixed"}, "", "VERIFIED fixed"),
            ({"verification": "rejection-accepted"}, "Checked.",
             "VERIFIED rejection accepted\n\nChecked."),
            ({"verification": "not-fixed"}, "Still fails.",
             "NOT FIXED\n\nStill fails."),
            ({}, "Ordinary prose.\n", "Ordinary prose."),
            ({}, "Verified fixed by hand.", "Verified fixed by hand."),
        ):
            with self.subTest(options=options, prose=prose):
                self.assertEqual(compose_reply(prose, **options), expected)

    def test_protocol_text_in_the_body_names_the_option(self) -> None:
        for body, option in (
            ("VERIFIED fixed", "--verification"),
            ("VERIFIED fixed.", "--verification"),
            ("VERIFIED: fixed", "--verification"),
            ("**VERIFIED fixed**", "--verification"),
            ("\n  NOT FIXED\nDetail.", "--verification"),
            ("DISPOSITION rejected", "--disposition"),
            ("DISPOSITION fixed", "--disposition"),
            ("`DISPOSITION rejected: not needed`", "--disposition"),
            ("[REV-1][blocking][tests] Copied root", "--disposition or"),
            ("AGENT_SQUAD/0.5.0 STOPPED", "--disposition or"),
            ("Not pursued: unnecessary.", "--not-pursued or --deferred-to"),
            ("_Deferred to #3: later._", "--not-pursued or --deferred-to"),
        ):
            for options in ({}, {"disposition": "rejected"},
                            {"verification": "fixed"}):
                with (
                    self.subTest(body=body, options=options),
                    self.assertRaisesRegex(
                        AgentSquadError, "must start with prose.*" + option
                    ),
                ):
                    compose_reply(body, **options)

    def test_not_pursued_requires_a_reason(self) -> None:
        with self.assertRaisesRegex(AgentSquadError, "requires a reason"):
            compose_reply(
                " \n", disposition="rejected", not_pursued=True
            )

    def test_invalid_sha_never_composes_a_line(self) -> None:
        for sha in (SHA[:12], SHA.upper(), None):
            with (
                self.subTest(sha=sha),
                self.assertRaises(AgentSquadError),
            ):
                compose_reply("x", disposition="fixed", sha=sha)


class ReviewCompositionTests(unittest.TestCase):
    def test_sections_compose_in_the_required_order(self) -> None:
        inputs = load_threads([
            finding(), finding("optional", "Second finding", line=3),
        ])
        body = compose_review(
            {
                "Evidence": "make test\n",
                "Spec": "Spec axis.",
                "Standards": "Standards axis.",
                "Merge hold": "Item 3: merge rules.",
                "Verified dispositions": "REV-1: VERIFIED fixed",
                "Summary": "\nChanges requested.\n",
            },
            inputs,
            ["REV-4", "REV-5"],
        )
        self.assertEqual(body, (
            "## Summary\n\nChanges requested.\n\n"
            "## Verified dispositions\n\nREV-1: VERIFIED fixed\n\n"
            "## Findings\n\nREV-4 [blocking] Fixture finding\n"
            "REV-5 [optional] Second finding\n\n"
            "## Merge hold\n\nItem 3: merge rules.\n\n"
            "## Standards\n\nStandards axis.\n\n"
            "## Spec\n\nSpec axis.\n\n"
            "## Evidence\n\nmake test"
        ))
        self.assertEqual(len(validate_review_body(body)), 2)

    def test_omitted_sections_say_none_and_hold_is_absent(self) -> None:
        body = compose_review(
            {"Summary": SUMMARY, "Verified dispositions": "none"}, [], []
        )
        # The existing fixture is the hand-written body without the three
        # recommended sections, which the CLI now always writes.
        self.assertEqual(body, REVIEW.strip() + (
            "\n\n## Standards\n\nnone\n\n## Spec\n\nnone\n\n"
            "## Evidence\n\nnone"
        ))
        self.assertEqual(validate_review_body(body), [])
        self.assertNotIn("## Merge hold", body)

    def test_section_files_hold_text_only(self) -> None:
        base = {"Summary": SUMMARY, "Verified dispositions": "none"}
        for name, text, error in (
            ("Summary", " \n", "--summary file must not be empty"),
            ("Standards", "", "--standards file must not be empty"),
            ("Merge hold", "\n", "--merge-hold file must not be empty"),
            ("Summary", "## Summary\n\nText.", "--summary file must hold"),
            ("Spec", "Text.\n\n## Findings\n\nnone", "--spec file must"),
            ("Evidence", "## Unanchored findings\n\nx", "--evidence file"),
            ("Verified dispositions", "```\nunclosed",
             "--verified-dispositions file must hold"),
        ):
            with (
                self.subTest(name=name, text=text),
                self.assertRaisesRegex(AgentSquadError, error),
            ):
                compose_review({**base, name: text}, [], [])
        fenced = compose_review(
            {**base, "Evidence": "```text\n## not a heading\n```"}, [], []
        )
        self.assertEqual(validate_review_body(fenced), [])

    def test_generated_findings_list_is_validated(self) -> None:
        base = {"Summary": SUMMARY, "Verified dispositions": "none"}
        # The finding grammar excludes only CR and LF; splitlines() also
        # breaks the Findings list on these, so the whole body is checked.
        for separator in "\v\f\x1c\x1d\x1e\x85\u2028\u2029":
            inputs = load_threads([finding(title=f"Pasted{separator}title")])
            with (
                self.subTest(separator=separator),
                self.assertRaisesRegex(AgentSquadError, "## Findings entry"),
            ):
                compose_review(base, inputs, ["REV-1"])


class ReplyOptionTests(unittest.TestCase):
    def refuse(self, *options: str, role: str = "implementer") -> str:
        """Usage errors exit 2 before execute reads configuration or forge."""
        stderr = io.StringIO()
        with (
            patch("agent_squad.cli.execute") as execute,
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main([
                "thread", "reply", "--as", role, "--pr", "1", "--finding",
                "REV-1", "--body", "unused.md", *options,
            ])
        self.assertEqual(raised.exception.code, 2)
        execute.assert_not_called()
        return stderr.getvalue()

    def test_option_combinations_are_usage_errors(self) -> None:
        for options, role, error in (
            (("--disposition", "fixed"), "implementer",
             "--disposition fixed requires --sha"),
            (("--disposition", "rejected", "--sha", SHA), "implementer",
             "--sha requires --disposition fixed"),
            (("--sha", SHA), "implementer",
             "--sha requires --disposition fixed"),
            (("--disposition", "fixed", "--sha", SHA[:7]), "implementer",
             "full lowercase commit SHA"),
            (("--disposition", "rejected", "--not-pursued", "--deferred-to",
              "2"), "implementer", "not allowed with argument"),
            (("--disposition", "fixed", "--sha", SHA, "--not-pursued"),
             "implementer", "require --disposition rejected"),
            (("--deferred-to", "2"), "implementer",
             "require --disposition rejected"),
            (("--disposition", "rejected", "--deferred-to", "02"),
             "implementer", "positive decimal integer"),
            (("--disposition", "rejected"), "reviewer",
             "--disposition requires --as implementer"),
            (("--verification", "fixed"), "implementer",
             "--verification requires --as reviewer"),
            (("--verification", "fixed", "--disposition", "rejected"),
             "reviewer", "not allowed with argument"),
            (("--verification", "verified"), "reviewer", "invalid choice"),
        ):
            with self.subTest(options=options, role=role):
                self.assertIn(error, self.refuse(*options, role=role))

    def test_valid_combinations_reach_execution(self) -> None:
        for options, role in (
            ((), "implementer"),
            (("--disposition", "fixed", "--sha", SHA), "implementer"),
            (("--disposition", "rejected", "--not-pursued"), "implementer"),
            (("--disposition", "rejected", "--deferred-to", "2"),
             "implementer"),
            (("--disposition", "needs-human"), "implementer"),
            (("--verification", "not-fixed"), "reviewer"),
        ):
            with (
                self.subTest(options=options),
                patch("agent_squad.cli.execute", return_value={}) as execute,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main([
                    "thread", "reply", "--as", role, "--pr", "1",
                    "--finding", "REV-1", "--body", "unused.md", *options,
                ]), 0)
                execute.assert_called_once()
