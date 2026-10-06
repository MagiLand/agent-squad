"""Command input guards reject malformed protocol data before forge calls."""

import argparse
import contextlib
from dataclasses import replace
import io
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.cli import main, positive_argument  # noqa: E402
from agent_squad.commands import (  # noqa: E402
    comment_issue, compose_reply, compose_review, create_issue, load_threads,
    view_issue, workflow_paths,
)
from agent_squad.conventions import validate_review_body  # noqa: E402
from agent_squad.forge import Evidence, ForgeError  # noqa: E402
from agent_squad.initialization import (  # noqa: E402
    AgentSquadError, Repository, RetainedError,
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


class StubIssueForge:
    """Record issue reads and comment writes without a transport."""

    def __init__(self, role: str = "implementer", **record: object) -> None:
        self.role = role
        self.record = {
            "number": 5, "title": "Issue", "state": "open",
            "is_pull_request": False, "body": "Body", "labels": [],
            "comments": (),
        } | record
        self.calls: list[tuple] = []

    def issue(self, number: int) -> dict:
        self.calls.append(("issue", number))
        return self.record

    def comment(self, number: int, body: str) -> Evidence:
        self.calls.append(("comment", number, body))
        return Evidence(7, "dev", "2026-01-01T00:00:07Z", body)


class IssueCommentTests(unittest.TestCase):
    NOTE = "AGENT_SQUAD/0.5.0 NOTE role=implementer"

    def test_note_line_precedes_the_stripped_prose(self) -> None:
        forge = StubIssueForge()
        result = comment_issue(forge, 5, "\n  Root cause: a race.\n\nFix.\n")
        body = self.NOTE + "\n\nRoot cause: a race.\n\nFix."
        self.assertEqual(forge.calls, [("issue", 5), ("comment", 5, body)])
        self.assertEqual(result, {
            "issue": 5, "id": 7, "author": "dev",
            "created_at": "2026-01-01T00:00:07Z", "body": body,
        })

    def test_empty_or_tagged_bodies_are_refused_before_any_forge_call(
        self,
    ) -> None:
        for body, message in (
            ("", "must not be empty"),
            (" \n\t\n", "must not be empty"),
            (self.NOTE + "\n\nCopied marker.", "must start with prose"),
            ("**" + self.NOTE + "**", "must start with prose"),
            ("\n  _AGENT_SQUAD/0.5.0 DECISION finding=none_", "must start"),
            ("AGENT_SQUAD/0.5.0 NOTE", "must start with prose"),
            ("`DISPOSITION fixed`", "must start with prose"),
            ("VERIFIED fixed", "must start with prose"),
            ("NOT FIXED yet", "must start with prose"),
            ("[REV-1][blocking][tests] Copied", "must start with prose"),
        ):
            with self.subTest(body=body):
                forge = StubIssueForge()
                with self.assertRaisesRegex(AgentSquadError, message):
                    comment_issue(forge, 5, body)
                self.assertEqual(forge.calls, [])

    def test_pull_requests_and_closed_issues_are_refused_after_the_read(
        self,
    ) -> None:
        for record, message in (
            ({"is_pull_request": True}, "not pull request #5"),
            ({"state": "closed"}, "requires open issue #5"),
            ({"is_pull_request": True, "state": "closed"}, "pull request"),
        ):
            with self.subTest(record=record):
                forge = StubIssueForge(**record)
                with self.assertRaisesRegex(AgentSquadError, message):
                    comment_issue(forge, 5, "Root cause.")
                self.assertEqual(forge.calls, [("issue", 5)])

    def test_the_grammar_admits_no_reviewer_note(self) -> None:
        forge = StubIssueForge(role="reviewer")
        with self.assertRaises(AgentSquadError):
            comment_issue(forge, 5, "Root cause.")
        self.assertEqual(forge.calls, [])

    def test_issue_view_marks_exactly_the_note_comments(self) -> None:
        primary = Path("/unused-primary").resolve()
        repository = Repository(primary, primary, primary / ".git", config())
        comments = (
            Evidence(1, "dev", "2026-01-01T00:00:01Z",
                     self.NOTE + "\n\nRoot cause."),
            Evidence(2, "dev", "2026-01-01T00:00:02Z", "Developer words."),
            Evidence(3, "dev", "2026-01-01T00:00:03Z", "**" + self.NOTE),
            Evidence(4, "dev", "2026-01-01T00:00:04Z", self.NOTE + " x"),
        )
        view = view_issue(
            repository, StubIssueForge(comments=comments), 5)
        self.assertEqual(
            [(c["id"], c["agent_note"]) for c in view["comments"]],
            [(1, True), (2, False), (3, False), (4, False)],
        )
        self.assertEqual(view["comments"][0]["body"], comments[0].body)
        self.assertEqual(view["paths"]["issue_scratch"],
                         str(primary / ".agent-squad/review-scratch/issue-5"))


class IssueCommentOptionTests(unittest.TestCase):
    def test_only_the_implementer_with_a_body_reaches_execution(self) -> None:
        base = ["issue", "comment", "--issue", "5"]
        for options, error in (
            (("--as", "reviewer", "--body", "b.md"), "invalid choice"),
            (("--body", "b.md"), "the following arguments are required: --as"),
            (("--as", "implementer"), "required: --body"),
            (("--as", "implementer", "--body", "b.md", "--issue", "05"),
             "positive decimal integer"),
        ):
            stderr = io.StringIO()
            with (
                self.subTest(options=options),
                patch("agent_squad.cli.execute") as execute,
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main([*base, *options])
            self.assertEqual(raised.exception.code, 2)
            execute.assert_not_called()
            self.assertIn(error, stderr.getvalue())
        with (
            patch("agent_squad.cli.execute", return_value={}) as execute,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main([*base, "--as", "implementer", "--body", "b.md"]), 0)
            execute.assert_called_once()


class StubCreateForge(StubIssueForge):
    """Record the reads and the one write of issue create."""

    def __init__(
        self, *, labels: tuple[str, ...] = ("bug", "needs-triage"),
        open_issues: dict[int, str] | None = None,
        created_labels: list[str] | None = None,
        missing: tuple[str, ...] = (), **record: object,
    ) -> None:
        super().__init__(**record)
        self.repository_labels = labels
        self.open = {} if open_issues is None else open_issues
        self.created_labels = created_labels
        self.missing = missing
        self.status = 404

    def issue(self, number: int) -> dict:
        if "issue" in self.missing:
            self.calls.append(("issue", number))
            raise ForgeError("issue not found", self.status)
        return super().issue(number)

    def pr(self, number: int) -> object:
        self.calls.append(("pr", number))
        if "pr" in self.missing:
            raise ForgeError("pull request not found", self.status)
        return object()

    def labels(self) -> tuple[str, ...]:
        self.calls.append(("labels",))
        return self.repository_labels

    def open_issues(self) -> dict[int, str]:
        self.calls.append(("open_issues",))
        return self.open

    def create_issue(self, title: str, body: str, label: str) -> dict:
        self.calls.append(("create_issue", title, body, label))
        return {
            "number": 12, "title": title, "state": "open",
            "is_pull_request": False, "body": body,
            "labels": (
                [label] if self.created_labels is None
                else self.created_labels
            ),
            "comments": (),
        }


class IssueCreateTests(unittest.TestCase):
    NOTE = "AGENT_SQUAD/0.5.0 NOTE role=implementer"
    READS = [("labels",), ("open_issues",)]

    def test_note_and_origin_lines_precede_the_prose_under_one_label(
        self,
    ) -> None:
        for origin, line in (
            ({}, None),
            ({"from_issue": 5}, "Follow-up from issue #5."),
            ({"from_pr": 9}, "Follow-up from pull request #9."),
            ({"from_issue": 5, "from_pr": 9},
             "Follow-up from issue #5 and pull request #9."),
        ):
            with self.subTest(origin=origin):
                forge = StubCreateForge()
                result = create_issue(
                    forge, "  Retry cleanup \t", "\n Work.\n\nWhy.\n",
                    **origin,
                )
                body = "\n\n".join(
                    part for part in (self.NOTE, line, "Work.\n\nWhy.")
                    if part
                )
                reads = (
                    ([("issue", 5)] if "from_issue" in origin else [])
                    + ([("pr", 9)] if "from_pr" in origin else [])
                )
                self.assertEqual(forge.calls, [
                    *reads, *self.READS,
                    ("create_issue", "Retry cleanup", body, "needs-triage"),
                ])
                self.assertEqual(result, {
                    "issue": 12, "title": "Retry cleanup",
                    "labels": ["needs-triage"],
                })

    def test_title_and_body_are_refused_before_any_forge_call(self) -> None:
        for title, body, message in (
            ("", "Work.", "title must not be empty"),
            (" \t\n", "Work.", "title must not be empty"),
            ("Retry\ncleanup", "Work.", "title must be one line"),
            ("Retry\u2028cleanup", "Work.", "title must be one line"),
            ("Retry cleanup", " \n\n", "issue create body must not be empty"),
            ("Retry cleanup", self.NOTE + "\n\nCopied.",
             "issue create body must start with prose"),
            ("Retry cleanup", "**[REV-1][optional][tests] Copied",
             "must start with prose"),
        ):
            with self.subTest(title=title, body=body):
                forge = StubCreateForge()
                with self.assertRaisesRegex(AgentSquadError, message):
                    create_issue(forge, title, body, from_issue=5)
                self.assertEqual(forge.calls, [])
        reviewer = StubCreateForge(role="reviewer")
        with self.assertRaises(AgentSquadError):
            create_issue(reviewer, "Retry cleanup", "Work.")
        self.assertEqual(reviewer.calls, [])

    def test_missing_origin_label_or_a_duplicate_title_creates_nothing(
        self,
    ) -> None:
        for forge, origin, message, calls in (
            (StubCreateForge(missing=("issue",)), {"from_issue": 5},
             "--from-issue 5 does not exist in the configured repository",
             [("issue", 5)]),
            (StubCreateForge(missing=("pr",)), {"from_pr": 9},
             "--from-pr 9 does not exist in the configured repository",
             [("pr", 9)]),
            (StubCreateForge(is_pull_request=True), {"from_issue": 5},
             "--from-issue 5 is a pull request; use --from-pr",
             [("issue", 5)]),
            (StubCreateForge(labels=("bug", "Needs-Triage")), {},
             "repository has no needs-triage label", [("labels",)]),
            (StubCreateForge(open_issues={3: "Other", 8: " Retry cleanup "}),
             {}, "open issue #8 has the same title", self.READS),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(AgentSquadError, message):
                    create_issue(forge, "Retry cleanup", "Work.", **origin)
                self.assertEqual(forge.calls, calls)
        # Only an exact match after trimming counts as the same title.
        forge = StubCreateForge(open_issues={8: "retry cleanup"})
        self.assertEqual(
            create_issue(forge, "Retry cleanup", "Work.")["issue"], 12)
        # A closed origin is still an origin; other read failures propagate.
        forge = StubCreateForge(state="closed")
        create_issue(forge, "Retry cleanup", "Work.", from_issue=5)
        for missing in ("issue", "pr"):
            forge = StubCreateForge(missing=(missing,))
            forge.status = 403
            with (
                self.subTest(status=403, missing=missing),
                self.assertRaisesRegex(ForgeError, "not found") as raised,
            ):
                create_issue(forge, "Retry cleanup", "Work.",
                             from_issue=5, from_pr=9)
            self.assertEqual(raised.exception.status, 403)

    def test_a_created_issue_without_exactly_the_label_is_not_retried(
        self,
    ) -> None:
        for labels, shown in (
            ([], "[]"), (["needs-triage", "bug"], "[needs-triage, bug]"),
            (["bug"], "[bug]"),
        ):
            with self.subTest(labels=labels):
                forge = StubCreateForge(created_labels=labels)
                with self.assertRaisesRegex(RetainedError, re.escape(
                    f"issue #12 was created with labels {shown} instead"
                    " of only needs-triage; correct its labels on the forge"
                    " and do not rerun issue create"
                )):
                    create_issue(forge, "Retry cleanup", "Work.")
                self.assertEqual(
                    [c[0] for c in forge.calls],
                    ["labels", "open_issues", "create_issue"],
                )


class IssueCreateOptionTests(unittest.TestCase):
    def test_only_the_implementer_with_title_and_body_reaches_execution(
        self,
    ) -> None:
        base = ["issue", "create"]
        complete = ("--title", "T", "--body", "b.md")
        for options, error in (
            (("--as", "reviewer", *complete), "invalid choice"),
            (complete, "the following arguments are required: --as"),
            (("--as", "implementer", "--body", "b.md"), "required: --title"),
            (("--as", "implementer", "--title", "T"), "required: --body"),
            (("--as", "implementer", *complete, "--from-issue", "0"),
             "positive decimal integer"),
            (("--as", "implementer", *complete, "--from-pr", "x"),
             "positive decimal integer"),
            (("--as", "implementer", *complete, "--issue", "5"),
             "unrecognized arguments"),
            (("--as", "implementer", *complete, "--label", "bug"),
             "unrecognized arguments"),
        ):
            stderr = io.StringIO()
            with (
                self.subTest(options=options),
                patch("agent_squad.cli.execute") as execute,
                contextlib.redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main([*base, *options])
            self.assertEqual(raised.exception.code, 2)
            execute.assert_not_called()
            self.assertIn(error, stderr.getvalue())
        stdout = io.StringIO()
        with (
            patch("agent_squad.cli.execute", return_value={
                "issue": 12, "title": "T", "labels": ["needs-triage"],
            }) as execute,
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(main([
                *base, "--as", "implementer", *complete,
                "--from-issue", "5", "--from-pr", "9",
            ]), 0)
        args = execute.call_args.args[0]
        self.assertEqual(
            (args.role, args.title, args.body, args.from_issue, args.from_pr),
            ("implementer", "T", "b.md", 5, 9),
        )
        self.assertTrue(
            stdout.getvalue().startswith("Created issue #12 for triage.\n"))
