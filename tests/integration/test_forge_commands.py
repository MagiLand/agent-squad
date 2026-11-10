"""Real command invocations against the committed fake forge and real Git."""

from dataclasses import replace
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.forge_support import (
    ForgeFixture, ForgejoFixture, REPORT, SUMMARY, TASK, finding,
)
from agent_squad.forge import ForgeError
from agent_squad.github import GitHub, parse_pullrequest, parse_review
from agent_squad.initialization import load_initialized_repository
from agent_squad.conventions import MERGE_INSTRUCTION, MERGE_WITHDRAWAL


class ForgeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ForgeFixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()

    def test_outdated_github_root_preserves_settlement_and_approval(
        self,
    ) -> None:
        f = self.f
        f.seed_read_scenario()
        self.assertEqual(f.status()["next_action"], "approved")
        model = f.read_model()
        root = next(c for c in model["prs"]["1"]["comments"]
                    if c["id"] == 103)
        root.update(line=None, original_line=2)
        for start in (None, 1):
            with self.subTest(original_start_line=start):
                root["original_start_line"] = start
                f.save_model(model)
                state = f.status()
                self.assertEqual(state["next_action"], "approved")
                self.assertTrue(state["approval"]["approved"])
                finding_state = state["findings"][0]
                self.assertEqual(finding_state["root"]["id"], 103)
                self.assertEqual(
                    [r["id"] for r in finding_state["replies"]], [104, 105]
                )
                self.assertTrue(finding_state["settled"])
                self.assertEqual(finding_state["anchor"]["start_line"], start)
                self.assertFalse(state["gates"]["unanchored_findings"])

    def test_automatic_task_and_issue_refusals(self) -> None:
        f = ForgeFixture()
        self.addCleanup(f.close)
        f.initialize()
        f.candidate()
        model = f.read_model()
        original = dict(model["issues"]["1"])
        for fields, message in (
            ({"state": "closed"}, "open issue"),
            ({"pull_request": {"url": "https://example.org/pr/1"}},
             "not a pull request"),
            ({"body": " \n"}, "must not be empty"),
        ):
            with self.subTest(fields=fields):
                model["issues"]["1"] = original | fields
                f.save_model(model)
                refused = f.cli(
                    "pr", "create", "--as", "implementer", "--issue", "1",
                    "--report", f.report, expected=1)
                self.assertIn(message, refused["error"])
                self.assertEqual(f.read_model()["prs"], {})
        model["issues"]["1"] = original | {
            "body": "# Objective\r\nBuild exactly this.\r\n"
                    "## Acceptance\r\nKeep wording.\r\n"
                    "```\r\n## Literal log\r\n"
        }
        f.save_model(model)
        f.create_pr(issue_task=True)
        state = f.status()
        self.assertEqual(state["task"], (
            '## Task\n\nThis Task is issue #1, "' + original["title"] +
            '", copied without rewording.\n\n### Objective\n'
            'Build exactly this.\n### Acceptance\nKeep wording.\n'
            '```\n## Literal log\n```'
        ))
        self.assertIn(REPORT.strip(), state["pr"]["evidence"]["body"])
        self.assertIsNone(state["merge_instruction"])
        self.assertIsNone(state["merge_hold"])

    def test_standing_decision_round_trip_and_combination_refusals(
        self,
    ) -> None:
        f = self.f
        f.review("approved")
        header = "AGENT_SQUAD/0.5.0 DECISION finding=none\n\n"
        recorded = f.decision(merge_instruction="record", body="> Start #1.")
        state = f.status()
        self.assertEqual(state["next_action"], "merge")
        self.assertEqual(state["merge_instruction"]["id"],
                         recorded["decision"]["id"])
        # The stored text is what the hand-written body produced before.
        self.assertEqual(state["decisions"][-1]["body"],
                         header + MERGE_INSTRUCTION + "\n\n> Start #1.")
        f.decision(merge_instruction="withdraw", body="\n")
        state = f.status()
        self.assertEqual(state["next_action"], "approved")
        self.assertIsNone(state["merge_instruction"])
        self.assertEqual(state["decisions"][-1]["body"],
                         header + MERGE_WITHDRAWAL)
        for directive in ("record", "withdraw"):
            for kwargs in ({"budget": 4}, {"task": TASK}, {"fid": "REV-1"}):
                with self.subTest(directive=directive, kwargs=kwargs):
                    before = f.read_model()["prs"]
                    refused = f.decision(merge_instruction=directive,
                                         body="", expected=1, **kwargs)
                    self.assertIn("standing merge decisions", refused["error"])
                    self.assertEqual(f.read_model()["prs"], before)
        for body in (
            MERGE_INSTRUCTION, MERGE_WITHDRAWAL + "\n\nA hold applies.",
            "Standing merge instruction: merge when approved",
            "**Standing merge instruction withdrawn.**",
        ):
            for directive in (None, "record"):
                with self.subTest(body=body, directive=directive):
                    before = f.read_model()
                    refused = f.decision(merge_instruction=directive,
                                         body=body, expected=1)
                    self.assertIn("--merge-instruction record or withdraw",
                                  refused["error"])
                    self.assertEqual(f.read_model(), before)

    def test_issue_comment_posts_one_marked_note_on_the_open_issue(
        self,
    ) -> None:
        f = self.f
        model = f.read_model()
        for number, state in (("2", "open"), ("3", "closed")):
            model["issues"][number] = dict(
                model["issues"]["1"], id=int(number), number=int(number),
                state=state, conversation=[],
            )
        f.save_model(model)

        def writes(calls: list[dict]) -> list[dict]:
            return [c for c in calls if c["arguments"][0] == "api"
                    and "--method" in c["arguments"]
                    and c["arguments"][c["arguments"].index("--method") + 1]
                    != "GET"]

        before = f.read_model()
        note = f.write("note.md", "\nRoot cause: cleanup races GitHub.\n")
        result = f.cli("issue", "comment", "--as", "implementer",
                       "--issue", "2", "--body", note)
        after = f.read_model()
        body = (
            "AGENT_SQUAD/0.5.0 NOTE role=implementer\n\n"
            "Root cause: cleanup races GitHub."
        )
        [stored] = after["issues"]["2"]["conversation"]
        self.assertEqual((stored["body"], stored["user"]["login"]),
                         (body, "developer"))
        self.assertEqual((result["issue"], result["id"], result["body"]),
                         (2, stored["id"], body))
        self.assertEqual(after["prs"], before["prs"])
        [write] = writes(after["calls"][len(before["calls"]):])
        self.assertEqual(write["arguments"][1],
                         "/repos/MagiLand/trial/issues/2/comments")
        self.assertEqual((write["account"], write["GH_TOKEN"]),
                         ("developer", "fake-token-developer"))

        # issue view tells the note from a comment the Developer wrote.
        after["issues"]["2"]["conversation"].append(dict(
            stored, id=stored["id"] + 1, body="Developer: also check X.",
        ))
        f.save_model(after)
        view = f.cli("issue", "view", "--issue", "2")
        self.assertEqual(
            [(c["id"], c["agent_note"]) for c in view["comments"]],
            [(stored["id"], True), (stored["id"] + 1, False)],
        )
        self.assertEqual(view["state"], "open")
        self.assertEqual(view["labels"], ["ready-for-agent"])
        human = f.run([sys.executable, "-m", "agent_squad", "issue", "view",
                       "--issue", "2"], cwd=f.worktree)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(
            [line for line in human.stdout.splitlines()
             if line.startswith("Comment ")],
            [f'Comment {stored["id"]} by developer is an Implementer agent'
             " note, not a Developer comment."],
        )

        empty = f.write("empty.md", " \n\n")
        tagged = f.write("tagged.md", body)
        for issue, file, message, forge_read in (
            ("2", empty, "must not be empty", False),
            ("2", tagged, "must start with prose", False),
            ("3", note, "requires open issue #3", True),
            ("999", note, "issue not found", True),
            # The fixtures' issue 1 and PR 1 share a number, which no forge
            # allows; the fake refuses the note instead of writing it to PR 1.
            ("1", note, "fixture issue and PR share number 1", True),
            # Last: without issue 1, number 1 names only PR 1.
            ("PR 1", note, "not pull request #1", True),
        ):
            with self.subTest(issue=issue, message=message):
                if issue == "PR 1":
                    model = f.read_model()
                    del model["issues"]["1"]
                    f.save_model(model)
                number = issue.removeprefix("PR ")
                before = f.read_model()
                refused = f.cli("issue", "comment", "--as", "implementer",
                                "--issue", number, "--body", file, expected=1)
                after = f.read_model()
                self.assertIn(message, refused["error"])
                self.assertEqual(
                    (after["issues"], after["prs"]),
                    (before["issues"], before["prs"]),
                )
                calls = after["calls"][len(before["calls"]):]
                if not forge_read:
                    self.assertEqual(calls, [])
                elif message.startswith("fixture"):
                    # The fake refused the POST; nothing was stored.
                    self.assertEqual(len(writes(calls)), 1)
                else:
                    self.assertEqual(writes(calls), [])
                    self.assertIn(
                        f"/repos/MagiLand/trial/issues/{number}",
                        [c["arguments"][1] for c in calls],
                    )

    def test_issue_create_files_one_marked_issue_for_triage(self) -> None:
        f = self.f
        model = f.read_model()
        # Neither a closed issue nor an open PR with the title is a
        # duplicate, and the new number is above every issue and PR.
        model["issues"]["4"] = dict(
            model["issues"]["1"], id=4, number=4, state="closed",
            title="Retry cleanup", conversation=[],
        )
        model["prs"]["1"]["title"] = "Retry cleanup"
        model["prs"]["9"] = dict(
            model["prs"]["1"], id=9, number=9, state="closed",
        )
        f.save_model(model)
        before = f.read_model()
        note = f.write("follow-up.md", "\nRetry the cleanup read.\n")
        result = f.cli("issue", "create", "--as", "implementer",
                       "--title", "  Retry cleanup ", "--body", note,
                       "--from-issue", "1", "--from-pr", "1")
        after = f.read_model()
        body = (
            "AGENT_SQUAD/0.5.0 NOTE role=implementer\n\n"
            "Follow-up from issue #1 and pull request #1.\n\n"
            "Retry the cleanup read."
        )
        self.assertEqual(result, {
            "issue": 10, "title": "Retry cleanup",
            "labels": ["needs-triage"],
        })
        stored = after["issues"]["10"]
        self.assertEqual(
            (stored["title"], stored["state"], stored["body"],
             stored["user"]["login"], stored["labels"]),
            ("Retry cleanup", "open", body, "developer",
             [{"name": "needs-triage"}]),
        )
        self.assertEqual(set(after["issues"]) - set(before["issues"]),
                         {"10"})
        self.assertEqual(after["prs"], before["prs"])
        [write] = [c for c in after["calls"][len(before["calls"]):]
                   if "POST" in c["arguments"]]
        self.assertEqual(
            (write["arguments"][1], write["account"], write["GH_TOKEN"],
             write["body"]),
            ("/repos/MagiLand/trial/issues", "developer",
             "fake-token-developer",
             {"title": "Retry cleanup", "body": body,
              "labels": ["needs-triage"]}),
        )
        # needs-triage keeps the new issue from being a Task (skill rule 1).
        view = f.cli("issue", "view", "--issue", "10")
        self.assertEqual(
            (view["state"], view["is_pull_request"], view["labels"]),
            ("open", False, ["needs-triage"]),
        )
        human = f.run([sys.executable, "-m", "agent_squad", "issue",
                       "create", "--as", "implementer", "--title", "Other",
                       "--body", note], cwd=f.worktree)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(human.stdout.splitlines()[0],
                         "Created issue #11 for triage.")

    def test_issue_create_refusals_create_nothing(self) -> None:
        f = self.f
        model = f.read_model()
        model["issues"]["4"] = dict(
            model["issues"]["1"], id=4, number=4, conversation=[],
            title="Retry cleanup",
        )
        f.save_model(model)
        note = f.write("follow-up.md", "Retry the cleanup read.\n")
        empty = f.write("empty.md", "\n \n")

        def create(*options: str, title: str = "New work",
                   body: str = note, expected: int = 1) -> dict:
            return f.cli("issue", "create", "--as", "implementer",
                         "--title", title, "--body", body, *options,
                         expected=expected)

        for options, kwargs, message, forge_read in (
            ((), {"title": " \t"}, "title must not be empty", False),
            ((), {"title": "New\nwork"}, "title must be one line", False),
            ((), {"body": empty}, "body must not be empty", False),
            (("--from-issue", "99"), {},
             "--from-issue 99 does not exist in the configured repository",
             True),
            (("--from-pr", "99"), {},
             "--from-pr 99 does not exist in the configured repository",
             True),
            # Issue 4 exists but is not a pull request.
            (("--from-pr", "4"), {}, "--from-pr 4 does not exist", True),
            ((), {"title": " Retry cleanup"},
             "open issue #4 has the same title", True),
            (("--as", "reviewer"), {"expected": 2}, "invalid choice", False),
            ("no label", {}, "repository has no needs-triage label", True),
        ):
            with self.subTest(message=message):
                if options == "no label":
                    model = f.read_model()
                    model["labels"] = [{"id": 1, "name": "Needs-Triage"}]
                    f.save_model(model)
                    options = ()
                before = f.read_model()
                refused = create(*options, **kwargs)
                after = f.read_model()
                self.assertIn(message, refused["error"])
                self.assertEqual((after["issues"], after["prs"]),
                                 (before["issues"], before["prs"]))
                calls = after["calls"][len(before["calls"]):]
                if forge_read:
                    self.assertTrue(calls)
                    self.assertEqual(
                        [c for c in calls if "--method" in c["arguments"]
                         and "GET" not in c["arguments"]], [])
                else:
                    self.assertEqual(calls, [])

    def test_issue_create_never_retries_an_issue_missing_its_label(
        self,
    ) -> None:
        # GitHub silently drops the labels of an account without push access.
        f = self.f
        f.settings(drop_issue_labels=True)
        before = f.read_model()
        refused = f.cli(
            "issue", "create", "--as", "implementer", "--title", "New work",
            "--body", f.write("follow-up.md", "Work.\n"), expected=3)
        after = f.read_model()
        self.assertIn(
            "issue #2 was created with labels [] instead of only"
            " needs-triage; correct its labels on the forge and do not rerun"
            " issue create", refused["error"])
        self.assertEqual(set(after["issues"]) - set(before["issues"]), {"2"})
        self.assertEqual(after["issues"]["2"]["labels"], [])
        self.assertEqual(
            [c["arguments"][1] for c in after["calls"][len(before["calls"]):]
             if "POST" in c["arguments"]],
            ["/repos/MagiLand/trial/issues"],
        )

    def test_optional_rejection_validates_reason_and_open_followup_issue(
        self,
    ) -> None:
        f = self.f
        f.review("approved", [finding("optional")])
        model = f.read_model()
        model["issues"]["2"] = dict(
            model["issues"]["1"], id=2, number=2, state="closed"
        )
        model["issues"]["3"] = dict(
            model["issues"]["1"], id=3, number=3,
            pull_request={
                "url": "https://api.github.com/repos/MagiLand/trial/pulls/3"
            },
        )
        f.save_model(model)
        rejected = {"disposition": "rejected"}
        for options, body, expected, message, issue, stored in (
            (rejected, "", 1, "--not-pursued with a reason", None, None),
            (rejected, "Unqualified reason.", 1, "--not-pursued with a reason",
             None, None),
            ({**rejected, "not_pursued": True}, "   ", 1, "requires a reason",
             None, None),
            (rejected, "Not pursued: typed by hand.", 1,
             "supply the reason prefix", None, None),
            ({**rejected, "deferred_to": "01"}, "", 2,
             "positive decimal integer", None, None),
            ({}, "Unstructured reply", 1, "requires --disposition", None,
             None),
            ({**rejected, "deferred_to": 2}, "follow-up.", 1,
             "requires an open issue", 2, None),
            ({**rejected, "deferred_to": 3}, "a pull request.", 1,
             "requires an open issue", 3, None),
            ({**rejected, "deferred_to": 999}, "", 1, "issue not found", 999,
             None),
            # The stored bodies are the hand-written ones accepted before.
            ({**rejected, "not_pursued": True}, "unnecessary here.\n", 0,
             None, None, "DISPOSITION rejected\n\nNot pursued: unnecessary"
             " here."),
            ({**rejected, "deferred_to": 1}, "follow-up.", 0, None, 1,
             "DISPOSITION rejected\n\nDeferred to #1: follow-up."),
        ):
            with self.subTest(options=options, body=body):
                before = f.read_model()
                result = f.reply("REV-1", body, expected=expected, **options)
                after = f.read_model()
                if expected:
                    self.assertIn(message, result["error"])
                    self.assertEqual(after["prs"], before["prs"])
                else:
                    self.assertEqual(
                        f.status()["optional_findings"][0]
                        ["disposition"]["body"], stored
                    )
                calls = after["calls"][len(before["calls"]):]
                issue_reads = [
                    c for c in calls
                    if len(c["arguments"]) > 1 and c["arguments"][1] in {
                        f"/repos/MagiLand/trial/issues/{n}"
                        for n in (1, 2, 3, 999)
                    }
                ]
                self.assertEqual(len(issue_reads), int(issue is not None))
                if issue is not None:
                    self.assertEqual(issue_reads[0]["arguments"][1],
                                     f"/repos/MagiLand/trial/issues/{issue}")
                    self.assertEqual(issue_reads[0]["account"], "developer")

    def test_reasonless_optional_rejection_reads_only_before_refusal(
        self,
    ) -> None:
        # Decided on PR #118 (option A): the severity read is the only forge
        # access before this refusal; no write and no issue read.
        f = self.f
        f.review("approved", [finding("optional")])
        before = f.read_model()
        refused = f.reply(
            "REV-1", "Unqualified reason.", disposition="rejected", expected=1
        )
        self.assertIn("--not-pursued with a reason", refused["error"])
        after = f.read_model()
        self.assertEqual(after["prs"], before["prs"])
        api = [
            c for c in after["calls"][len(before["calls"]):]
            if c["arguments"][0] == "api"
        ]
        self.assertTrue(api)
        for call in api:
            arguments = call["arguments"]
            with self.subTest(endpoint=arguments[1]):
                if arguments[1] == "graphql":
                    self.assertTrue(
                        call["body"]["query"].startswith("query ")
                    )
                else:
                    self.assertEqual(
                        arguments[arguments.index("--method") + 1], "GET"
                    )
                    self.assertNotRegex(arguments[1], r"/issues/[0-9]+$")

    def test_optional_disposition_gates_launch_and_merge_after_approval(
        self,
    ) -> None:
        f = self.f
        f.review("approved", [finding("optional")])
        state = f.status()
        self.assertTrue(state["approval"]["approved"])
        self.assertEqual(state["next_action"], "address_findings")
        refused = f.cli("reviewer", "launch", "--pr", "1", expected=4)
        self.assertIn("unaddressed_findings", refused["error"])
        refused = f.cli("pr", "merge", "--as", "implementer", "--pr", "1",
                        expected=4, cwd=f.repo)
        self.assertIn("unaddressed_findings: REV-1", refused["error"])
        f.reply("REV-1", "unnecessary here.", disposition="rejected",
                not_pursued=True)
        self.assertEqual(f.status()["next_action"], "approved")
        launched = f.cli("reviewer", "launch", "--pr", "1")
        self.assertEqual(launched["observed_state"], "working")
        f.cli("reviewer", "close", "--pr", "1")
        merged = f.cli("pr", "merge", "--as", "implementer", "--pr", "1",
                       cwd=f.repo)
        self.assertEqual(merged["integration"], "verified by ancestry")

    def test_optional_fixed_and_needs_human_keep_existing_rules(self) -> None:
        f = self.f
        f.review("approved", [finding("optional")])
        refused = f.reply(
            "REV-1", disposition="fixed", sha=self.head, expected=1
        )
        self.assertIn("absent from", refused["error"])
        f.reply("REV-1", "A Developer choice.", disposition="needs-human")
        refused = f.cli("reviewer", "launch", "--pr", "1", expected=4)
        self.assertIn("needs_decision", refused["error"])
        f.decision(fid="REV-1")
        self.assertFalse(f.status()["gates"]["needs_decision"])
        head = f.push("value = 1\nsecond = 20\nthird = 3\n")
        f.reply("REV-1", "Run the fixture probe.", disposition="fixed",
                sha=head)
        self.assertFalse(f.status()["gates"]["unaddressed_findings"])
        f.reply("REV-1", "Fixture checked.", verification="fixed")
        self.assertTrue(f.status()["findings"][0]["settled"])

    def test_moved_base_uses_fetched_tip_with_frozen_pr_base(self) -> None:
        f = self.f
        f.git("switch", "main")
        (f.repo / "base-only.txt").write_text("Base advanced.\n")
        f.git("add", "base-only.txt")
        f.git("commit", "-m", "test: advance main")
        tip = f.git("rev-parse", "HEAD")
        f.git("push", "origin", "main")
        state = f.status()
        self.assertEqual(f.read_model()["prs"]["1"]["base"]["sha"], f.base)
        self.assertNotEqual(tip, f.base)
        self.assertEqual(state["target"]["base"], f.base)
        self.assertEqual(state["target"]["base_tip"], tip)
        f.cli("pr", "head", "--pr", "1")
        f.cli("pr", "reviews", "--pr", "1")
        f.review("needs_human")
        # Incorporating the moved base makes it a valid review base even
        # though the forge's informational base.sha is still the old commit.
        f.git("merge", "--no-edit", "main", cwd=f.worktree)
        f.git("push", "origin", "HEAD", cwd=f.worktree)
        f.base = tip
        f.review("approved")
        self.assertEqual(f.status()["next_action"], "approved")
        self.assertEqual(f.status()["budget"]["used"], 2)

    def test_fixed_disposition_precedes_push_and_is_rederived(self) -> None:
        f = self.f
        f.review("changes_requested", [finding()])
        (f.worktree / "example.py").write_text("value = 2\nsecond = 2\n")
        f.git("add", "example.py", cwd=f.worktree)
        f.git("commit", "-m", "test: fix before push", cwd=f.worktree)
        head = f.git("rev-parse", "HEAD", cwd=f.worktree)
        f.reply("REV-1", disposition="fixed", sha=head)
        before = f.status()
        self.assertEqual(before["next_action"], "address_findings")
        self.assertIn(
            "invalid_disposition", [d["kind"] for d in before["diagnostics"]]
        )
        f.git("push", "origin", "HEAD", cwd=f.worktree)
        after = f.status()
        self.assertEqual(after["next_action"], "launch_review")
        self.assertEqual(
            after["findings"][0]["latest_disposition"]["sha"], head
        )

    def test_null_descriptions_and_browser_decision(self) -> None:
        f = self.f
        model = f.read_model()
        model["issues"]["1"]["body"] = None
        model["prs"] = {}
        f.save_model(model)
        self.assertEqual(f.cli("issue", "view", "--issue", "1")["body"], "")
        f.create_pr()
        f.review("needs_human")
        f.decision(budget=5, task=TASK.replace("Exercise", "Amend"))
        model = f.read_model()
        comment = model["prs"]["1"]["conversation"][0]
        comment["body"] = comment["body"].replace("\n", "\r\n")
        f.save_model(model)
        state = f.status()
        self.assertEqual(state["next_action"], "launch_review")
        self.assertEqual(state["budget"]["effective"], 5)
        self.assertIn("Amend", state["effective_task"])
        self.assertEqual(state["diagnostics"], [])
        model = f.read_model()
        model["prs"]["1"]["body"] = None
        f.save_model(model)
        state = f.status()
        self.assertIn(
            "malformed_pr_body", [d["kind"] for d in state["diagnostics"]]
        )

    def test_review_verdict_and_target_guards_refuse_before_writing(
        self,
    ) -> None:
        f = self.f
        child = f.git(
            "commit-tree",
            f.git("rev-parse", "HEAD^{tree}", cwd=f.worktree),
            "-p",
            self.head,
            "-m",
            "Unpushed child",
            cwd=f.worktree,
        )
        for options, expected, error in [
            (
                {"verdict": "approved", "threads": [finding()]},
                4,
                "approval requires",
            ),
            ({"verdict": "changes_requested"}, 1, "requires a blocking"),
            (
                {
                    "verdict": "changes_requested",
                    "threads": [finding("optional")],
                },
                1,
                "requires a blocking",
            ),
            (
                {"verdict": "approved", "head": f.base},
                1,
                "not the current PR head",
            ),
            (
                {"verdict": "approved", "base": self.head},
                1,
                "ancestor of head and the base branch",
            ),
            (
                {"verdict": "approved", "base": child},
                1,
                "ancestor of head and the base branch",
            ),
        ]:
            with self.subTest(options=options):
                result = f.review(**options, expected=expected)
                self.assertIn(error, result["error"])
                self.assertEqual(f.read_model()["prs"]["1"]["reviews"], [])
        f.review("changes_requested", [finding()])
        self.assertIn(
            "approval requires", f.review("approved", expected=4)["error"]
        )
        self.assertEqual(len(f.read_model()["prs"]["1"]["reviews"]), 1)
        f.reply("REV-1", verification="not-fixed")
        f.review("changes_requested")
        self.assertEqual(f.status()["budget"]["used"], 2)
        # A verification consumed by the preceding pass cannot justify another.
        self.assertIn(
            "during this pass",
            f.review("changes_requested", expected=1)["error"],
        )
        self.assertEqual(len(f.read_model()["prs"]["1"]["reviews"]), 2)

    def test_decision_report_and_recovery_guards_refuse_without_mutation(
        self,
    ) -> None:
        f = self.f
        f.review("changes_requested", [finding()])
        for body in ("", "DISPOSITION rejected"):
            with self.subTest(body=body):
                result = f.cli(
                    "decision",
                    "post",
                    "--as",
                    "implementer",
                    "--pr",
                    "1",
                    "--finding",
                    "none",
                    "--body",
                    f.write("invalid.md", body),
                    expected=1,
                )
                self.assertIn(
                    "decision body must contain decision prose",
                    result["error"],
                )
                self.assertEqual(
                    f.read_model()["prs"]["1"]["conversation"], []
                )
        result = f.decision(fid="REV-1", task=TASK, expected=1)
        self.assertIn("Task amendment requires", result["error"])
        self.assertEqual(f.read_model()["prs"]["1"]["conversation"], [])
        model = f.read_model()
        model["prs"]["1"]["comments"] = []
        f.save_model(model)
        result = f.cli(
            "thread",
            "open",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--finding",
            "REV-1",
            "--path",
            "example.py",
            "--line",
            "2",
            expected=1,
        )
        self.assertIn("no Unanchored findings text", result["error"])
        self.assertEqual(f.read_model()["prs"]["1"]["comments"], [])
        model = f.read_model()
        model["prs"]["1"]["state"] = "closed"
        f.save_model(model)
        result = f.cli(
            "pr",
            "report",
            "--as",
            "implementer",
            "--pr",
            "1",
            "--report",
            f.write(
                "changed-report.md",
                REPORT.replace("Scripted fixture.", "Do not publish."),
            ),
            expected=1,
        )
        self.assertIn("PR is not open", result["error"])
        self.assertNotIn("Do not publish", f.read_model()["prs"]["1"]["body"])

    def test_silently_omitted_root_is_recovered_without_another_review(
        self,
    ) -> None:
        f = self.f
        f.settings(reject_batch=True, drop_root=True)
        inputs = [finding()]
        result = f.review("changes_requested", inputs, expected=1)
        self.assertIn("forge omitted roots", result["error"])
        state = f.status()
        self.assertEqual(state["next_action"], "open_threads")
        ident = state["reviews"][0]["id"]
        f.settings(drop_root=False)
        f.review("changes_requested", inputs, resume=ident)
        final = f.status()
        self.assertEqual(final["budget"]["used"], 1)
        self.assertEqual(final["next_action"], "address_findings")
        self.assertEqual(len(final["findings"]), 1)
        self.assertIsNotNone(final["findings"][0]["root"])

    def test_interruption_before_write_repeats_and_counts_once(self) -> None:
        f = self.f
        f.settings(interrupt_before_review=True)
        self.assertIn(
            "before review write", f.review("approved", expected=1)["error"]
        )
        self.assertEqual(f.read_model()["prs"]["1"]["reviews"], [])
        self.assertEqual(f.status()["budget"]["used"], 0)
        f.review("approved")
        state = f.status()
        self.assertEqual(state["budget"]["used"], 1)
        self.assertEqual(state["next_action"], "approved")
        self.assertEqual(len(f.read_model()["prs"]["1"]["reviews"]), 1)

    def test_all_mutations_use_the_selected_identity_without_switching(
        self,
    ) -> None:
        f = self.f
        f.review("changes_requested", [finding()])
        f.reply("REV-1", "Evidence.", disposition="rejected")
        f.reply("REV-1", "Checked.", verification="rejection-accepted")
        f.cli(
            "thread",
            "resolve",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--finding",
            "REV-1",
        )
        f.decision()
        f.cli(
            "stop",
            "post",
            "--as",
            "implementer",
            "--pr",
            "1",
            "--head",
            self.head,
            "--reason",
            "design",
            "--body",
            f.write("stop.md", "Scripted decision needed."),
        )
        f.cli(
            "pr",
            "report",
            "--as",
            "implementer",
            "--pr",
            "1",
            "--report",
            f.report,
        )
        calls = f.read_model()["calls"]
        mutations = [
            c
            for c in calls
            if c["arguments"][0] == "api"
            and c["arguments"][c["arguments"].index("--method") + 1] != "GET"
            and not (
                c["arguments"][1] == "graphql"
                and c["body"]["query"].startswith("query ")
            )
        ]
        self.assertTrue(mutations)
        for call in mutations:
            self.assertEqual(call["GH_TOKEN"], "fake-token-" + call["account"])
            if (
                call["arguments"][1].endswith("/reviews")
                or call["arguments"][1] == "graphql"
            ):
                self.assertEqual(call["account"], "reviewer")
        self.assertFalse(
            any(c["arguments"][:2] == ["auth", "switch"] for c in calls)
        )
        self.assertNotIn("fake-token-", json.dumps(f.status()))
        self.assertNotIn(
            "fake-token-", (f.repo / ".agent-squad/config.json").read_text()
        )

    def test_pending_draft_is_submitted_as_comment_and_replies_are_enumerated(
        self,
    ) -> None:
        f = self.f
        model = f.read_model()
        model["prs"]["1"]["reviews"].append(
            {
                "id": 50,
                "body": "Existing draft notes",
                "user": {"login": "reviewer"},
                "commit_id": self.head,
                "state": "PENDING",
                "submitted_at": None,
            }
        )
        f.save_model(model)
        f.settings(omit_flat_replies=True, thread_page_size=1)
        f.review(
            "changes_requested",
            [finding(), finding("optional", "Optional", line=3)],
        )
        f.reply("REV-1", "Evidence from omitted flat listing.",
                disposition="rejected")
        f.reply("REV-1", "Checked.", verification="rejection-accepted")
        state = f.status()
        self.assertTrue(state["findings"][0]["settled"])
        self.assertEqual(len(state["findings"][0]["replies"]), 2)
        model = f.read_model()
        self.assertEqual(model["prs"]["1"]["reviews"][0]["state"], "COMMENTED")
        self.assertEqual(state["budget"]["used"], 1)
        self.assertTrue(
            any(
                "/reviews/50/events" in c["arguments"][1]
                for c in model["calls"]
                if c["arguments"][0] == "api"
            )
        )

    def test_fallback_resume_posts_only_missing_roots_and_preserves_one_review(
        self,
    ) -> None:
        f = self.f
        f.settings(reject_batch=True, fail_roots=["REV-1"])
        inputs = [finding(), finding("optional", "Optional", line=3)]
        result = f.review("changes_requested", inputs, expected=1)
        self.assertIn("REV-1", result["error"])
        state = f.status()
        self.assertEqual(state["next_action"], "open_threads")
        self.assertEqual(state["budget"]["used"], 1)
        ident = state["reviews"][0]["id"]
        original = state["reviews"][0]["body"]
        f.settings(fail_roots=[])
        f.review("changes_requested", inputs, resume=ident)
        f.review("changes_requested", inputs, resume=ident)
        final = f.status()
        self.assertEqual(final["budget"]["used"], 1)
        self.assertEqual(final["reviews"][0]["body"], original)
        self.assertEqual(
            len(
                [
                    c
                    for c in f.read_model()["prs"]["1"]["comments"]
                    if c["in_reply_to_id"] is None
                ]
            ),
            2,
        )
        f.review(
            "changes_requested",
            [finding(title="Different")],
            resume=ident,
            expected=1,
        )
        f.review("needs_human", inputs, resume=ident, expected=1)
        f.review("changes_requested", inputs, resume=99999, expected=1)

    def test_interruption_after_body_and_thread_open_by_either_identity(
        self,
    ) -> None:
        f = self.f
        f.settings(reject_batch=True, interrupt_after_body=True)
        inputs = [finding(), finding("optional", "Optional", line=3)]
        f.review("changes_requested", inputs, expected=1)
        state = f.status()
        self.assertEqual(state["next_action"], "open_threads")
        for fid, role in [("REV-1", "implementer"), ("REV-2", "reviewer")]:
            f.cli(
                "thread",
                "open",
                "--as",
                role,
                "--pr",
                "1",
                "--finding",
                fid,
                "--path",
                "example.py",
                "--line",
                "2",
            )
            calls = f.read_model()["calls"]
            writes = [
                c
                for c in calls
                if c["arguments"][0] == "api"
                and c["arguments"][1].endswith("/comments")
                and "POST" in c["arguments"]
            ]
            expected_account = (
                "developer" if role == "implementer" else "reviewer"
            )
            self.assertEqual(writes[-1]["account"], expected_account)
            self.assertEqual(
                writes[-1]["GH_TOKEN"], "fake-token-" + expected_account
            )
            self.assertFalse(f.status()["gates"]["unanchored_findings"])
        self.assertEqual(f.status()["budget"]["used"], 1)
        f.cli(
            "thread",
            "open",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--finding",
            "REV-1",
            "--path",
            "example.py",
            "--line",
            "2",
            expected=1,
        )

    def test_optional_unanchored_does_not_gate_approval(self) -> None:
        f = self.f
        f.settings(reject_batch=True, fail_roots=["REV-1"])
        f.review("approved", [finding("optional")], expected=1)
        state = f.status()
        self.assertEqual(state["next_action"], "approved")
        self.assertTrue(
            any(
                d["kind"] == "optional_unanchored"
                for d in state["diagnostics"]
            )
        )

    def test_same_head_publication_and_task_mirror_retry(
        self,
    ) -> None:
        f = self.f
        first = f.review("approved")
        f.settings(fail_mirror=True)
        new_task = TASK.replace(
            "Exercise the forge protocol", "Amend the forge objective"
        )
        f.decision(task=new_task, expected=3)
        state = f.status()
        self.assertEqual(state["next_action"], "launch_review")
        self.assertTrue(state["task_body_stale"])
        self.assertIn("Amend the forge objective", state["effective_task"])
        f.settings(fail_mirror=False)
        result = f.decision(task=new_task)
        self.assertTrue(result["reused"])
        self.assertEqual(len(f.status()["decisions"]), 1)
        self.assertFalse(f.status()["task_body_stale"])
        second = f.review("approved")
        self.assertNotEqual(first["review_id"], second["review_id"])
        state = f.status()
        self.assertEqual(state["next_action"], "approved")
        self.assertEqual(state["budget"]["used"], 2)
        f.decision(budget=2, expected=4)
        f.decision(budget=4)

    def test_invalid_anchor_causes_zero_forge_calls(self) -> None:
        f = self.f
        count = len(f.read_model()["calls"])
        f.review("changes_requested", [finding(line=999)], expected=1)
        self.assertEqual(len(f.read_model()["calls"]), count)

    def test_login_mismatch_refuses_mutation_and_never_prints_a_token(
        self,
    ) -> None:
        f = self.f
        f.settings(login_mismatch="wrong-account")
        result = f.review("approved", expected=1)
        self.assertIn("does not match", result["error"])
        self.assertNotIn("fake-token", result["error"])
        self.assertEqual(f.read_model()["prs"]["1"]["reviews"], [])

    def test_reply_roles_fixed_sha_validation_and_resolve_gate(self) -> None:
        f = self.f
        f.review("changes_requested", [finding()])
        f.reply("REV-1", disposition="fixed", sha=self.head, expected=1)
        f.reply("REV-1", disposition="fixed", sha="f" * 40, expected=1)
        f.cli(
            "thread",
            "resolve",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--finding",
            "REV-1",
            expected=4,
        )
        f.reply("REV-1", disposition="rejected")
        f.reply("REV-1", verification="rejection-accepted")
        f.cli(
            "thread",
            "resolve",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--finding",
            "REV-1",
        )
        self.assertTrue(f.read_model()["prs"]["1"]["threads"][0]["isResolved"])

    def test_reply_options_post_the_existing_tagged_lines(self) -> None:
        f = self.f
        f.review("changes_requested", [finding()])
        head = f.push("value = 2\nsecond = 2\nthird = 3\n")
        for options, prose, stored, field, value, settled in (
            ({"disposition": "needs-human"}, "A Developer choice.\n",
             "DISPOSITION needs-human\n\nA Developer choice.",
             "latest_disposition", "needs-human", False),
            ({"disposition": "rejected"}, "Evidence.",
             "DISPOSITION rejected\n\nEvidence.",
             "latest_disposition", "rejected", False),
            ({"disposition": "fixed", "sha": head}, "Run the probe.",
             f"DISPOSITION fixed {head}\n\nRun the probe.",
             "latest_disposition", "fixed", False),
            ({"verification": "not-fixed"}, "", "NOT FIXED",
             "latest_verification", "NOT FIXED", False),
            ({"verification": "rejection-accepted"}, "Checked.",
             "VERIFIED rejection accepted\n\nChecked.",
             "latest_verification", "VERIFIED rejection accepted", True),
            ({"verification": "fixed"}, "Probe passed.",
             "VERIFIED fixed\n\nProbe passed.",
             "latest_verification", "VERIFIED fixed", True),
        ):
            with self.subTest(options=options):
                f.reply("REV-1", prose, **options)
                thread = f.status()["findings"][0]
                self.assertEqual(thread["replies"][-1]["body"], stored)
                self.assertEqual(thread[field]["value"], value)
                self.assertEqual(thread["settled"], settled)

    def test_reply_refusals_make_no_forge_call(self) -> None:
        f = self.f
        f.review("changes_requested", [finding()])
        for role, options, body, expected, error in (
            ("implementer", {"disposition": "fixed"}, "", 2,
             "--disposition fixed requires --sha"),
            ("implementer", {"disposition": "rejected", "sha": self.head}, "",
             2, "--sha requires --disposition fixed"),
            ("implementer", {"disposition": "rejected", "not_pursued": True,
                             "deferred_to": 1}, "Reason.", 2,
             "not allowed with argument"),
            ("reviewer", {"disposition": "rejected"}, "", 2,
             "--disposition requires --as implementer"),
            ("implementer", {"verification": "fixed"}, "", 2,
             "--verification requires --as reviewer"),
            ("reviewer", {}, "VERIFIED fixed\nChecked.", 1,
             "supply the tagged line with --verification"),
            ("implementer", {"disposition": "rejected"},
             "DISPOSITION rejected\nEvidence.", 1,
             "supply the tagged line with --disposition"),
            ("reviewer", {"verification": "fixed"}, "VERIFIED: fixed", 1,
             "supply the tagged line with --verification"),
        ):
            with self.subTest(role=role, options=options, body=body):
                before = f.read_model()
                result = f.reply(
                    "REV-1", body, role, expected=expected, **options
                )
                self.assertIn(error, result["error"])
                self.assertEqual(f.read_model(), before)
        before = f.read_model()["prs"]
        refused = f.reply("REV-1", "Reason.", disposition="rejected",
                          not_pursued=True, expected=1)
        self.assertIn("apply only to an optional thread", refused["error"])
        self.assertEqual(f.read_model()["prs"], before)

    def test_review_sections_publish_the_required_body(self) -> None:
        f = self.f
        f.review(
            "changes_requested",
            [finding()],
            sections={
                "merge-hold": "Task: Developer review.\n",
                "standards": "Standards axis.",
                "spec": "Spec axis.",
                "evidence": "make test",
            },
        )
        state = f.status()
        self.assertEqual(state["reviews"][0]["body"], (
            f"AGENT_SQUAD/0.5.0 REVIEW pr=1 head={self.head} base={f.base}"
            " verdict=changes_requested\n\n"
            f"## Summary\n\n{SUMMARY}\n\n## Verified dispositions\n\nnone"
            "\n\n## Findings\n\nREV-1 [blocking] Fixture finding\n\n"
            "## Merge hold\n\nTask: Developer review.\n\n"
            "## Standards\n\nStandards axis.\n\n## Spec\n\nSpec axis.\n\n"
            "## Evidence\n\nmake test"
        ))
        self.assertEqual(
            state["merge_hold"]["text"], "Task: Developer review."
        )
        target = [
            "review", "post", "--as", "reviewer", "--pr", "1", "--head",
            self.head, "--base", f.base, "--verdict", "needs_human",
            "--threads", f.write("none.json", "[]"),
        ]
        before = f.read_model()
        for options, error in (
            (["--verified-dispositions", f.verified],
             "arguments are required: --summary"),
            (["--summary", f.summary],
             "arguments are required: --verified-dispositions"),
            (["--summary", f.summary, "--verified-dispositions", f.verified,
              "--body", f.summary], "unrecognized arguments: --body"),
        ):
            with self.subTest(options=options):
                refused = f.cli(*target, *options, expected=2)
                self.assertIn(error, refused["error"])
        refused = f.review(
            "needs_human", sections={"standards": "## Standards\n\nText."},
            expected=1,
        )
        self.assertIn("--standards file must hold section text",
                      refused["error"])
        refused = f.review(
            "changes_requested", [finding(title="Pasted\u2028title")],
            expected=1,
        )
        self.assertIn("## Findings entry", refused["error"])
        self.assertEqual(f.read_model(), before)

    def test_read_default_identity_in_registered_review_worktree(self) -> None:
        f = self.f
        path = (
            f.repo
            / ".agent-squad/worktrees"
            / ("reviewer-pr1-" + self.head[:7])
        )
        f.git("worktree", "add", "--detach", str(path), self.head)
        f.cli("pr", "head", "--pr", "1", cwd=path)
        calls = f.read_model()["calls"]
        self.assertEqual(calls[-1]["account"], "reviewer")
        f.cli("pr", "reviews", "--pr", "1", "--as", "implementer", cwd=path)
        self.assertEqual(f.read_model()["calls"][-1]["account"], "developer")

    def test_create_refusals_and_preserved_sections(
        self,
    ) -> None:
        f = self.f
        result = f.cli(
            "pr",
            "create",
            "--as",
            "implementer",
            "--issue",
            "1",
            "--task",
            f.task,
            "--report",
            f.report,
            expected=1,
        )
        self.assertIn("already exists", result["error"])
        f.git("switch", "-c", "unpublished", cwd=f.worktree)
        f.cli(
            "pr",
            "create",
            "--as",
            "implementer",
            "--issue",
            "1",
            "--task",
            f.task,
            "--report",
            f.report,
            expected=4,
        )
        f.git("switch", "issue-1", cwd=f.worktree)
        f.cli(
            "pr",
            "report",
            "--as",
            "implementer",
            "--pr",
            "1",
            "--report",
            f.write(
                "new-report.md",
                REPORT.replace("Scripted fixture.", "Updated report."),
            ),
        )
        body = f.read_model()["prs"]["1"]["body"]
        self.assertTrue(body.startswith(TASK))
        self.assertIn("Closes #1", body)
        self.assertIn("Updated report.", body)

    def test_replaced_head_counts_in_a_fresh_clone(self) -> None:
        f = self.f
        f.review("approved")
        tree = f.git("rev-parse", "HEAD^{tree}", cwd=f.worktree)
        replacement = f.git(
            "commit-tree",
            tree,
            "-p",
            f.base,
            "-m",
            "test: replace reviewed commit",
            cwd=f.worktree,
        )
        f.git(
            "push",
            "--force",
            "origin",
            replacement + ":refs/heads/issue-1",
            cwd=f.worktree,
        )
        fresh = f.root / "fresh"
        f.git(
            "clone",
            "--no-local",
            "--single-branch",
            "--branch",
            "issue-1",
            str(f.origin),
            str(fresh),
            cwd=f.root,
        )
        missing = f.run(["git", "cat-file", "-e", self.head], cwd=fresh)
        self.assertNotEqual(missing.returncode, 0)
        f.worktree = fresh
        f.initialize()
        state = f.status()
        self.assertEqual(state["budget"]["used"], 1)
        self.assertFalse(state["reviews"][0]["current"])
        self.assertEqual(state["next_action"], "launch_review")

    def test_timeout_and_response_validation(self) -> None:
        f = self.f
        with patch.dict("os.environ", f.env, clear=True):
            repository = load_initialized_repository(f.worktree)
            forge = GitHub(repository, "implementer", timeout=0.00001)
            with self.assertRaisesRegex(ForgeError, "timed out"):
                forge.token()
        for value in [None, {}, {"id": True}, {"number": "1"}]:
            with self.assertRaises(ForgeError):
                parse_pullrequest(value)
            with self.assertRaises(ForgeError):
                parse_review(value)


def is_write(call: dict) -> bool:
    """Whether a recorded fake-forge call could change the forge."""
    if "method" in call:  # One HTTP request to the Forgejo fake.
        return call["method"] != "GET"
    arguments = call["arguments"]  # One process of the fake gh.
    if arguments[:2] == ["api", "graphql"]:
        # The snapshot read is a GraphQL query sent with POST.
        return not call["body"]["query"].lstrip().startswith("query ")
    return (
        arguments[0] == "api" and "--method" in arguments
        and arguments[arguments.index("--method") + 1] != "GET"
    )


class PrCreateIssueTests(unittest.TestCase):
    """#151: every pr create refuses a pull request and a closed issue."""

    def test_task_file_does_not_bypass_the_issue_refusals(self) -> None:
        for fixture in (ForgeFixture, ForgejoFixture):
            with self.subTest(forge=fixture.__name__):
                f = fixture()
                self.addCleanup(f.close)
                self.check_refusals_and_created_bodies(f)

    def check_refusals_and_created_bodies(self, f: ForgeFixture) -> None:
        f.initialize()
        f.candidate()
        ownership = Path(
            f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree)
        ) / "agent-squad-implementation.json"
        original = dict(f.read_model()["issues"]["1"])
        for fields, message in (
            ({"state": "closed"}, "Task requires an open issue"),
            ({"pull_request": {"url": "https://example.org/pr/1"}},
             "Task requires an issue, not a pull request"),
        ):
            for task in ((), ("--task", f.task)):
                with self.subTest(fields=fields, task=bool(task)):
                    model = f.read_model()
                    model["issues"]["1"] = original | fields
                    f.save_model(model)
                    refused = f.cli(
                        "pr", "create", "--as", "implementer", "--issue",
                        "1", *task, "--report", f.report, expected=1,
                    )
                    self.assertEqual(refused["error"], "error: " + message)
                    after = f.read_model()
                    self.assertEqual(after["prs"], {})
                    calls = after["calls"][len(model["calls"]):]
                    self.assertTrue(calls)
                    self.assertEqual([c for c in calls if is_write(c)], [])
                    self.assertFalse(ownership.exists())
        model = f.read_model()
        model["issues"]["1"] = original
        # --task never copies the issue, so its body may be empty.
        model["issues"]["2"] = original | {
            "id": 2, "number": 2, "body": " \n",
        }
        f.save_model(model)
        numbers = {}
        for issue in (1, 2):
            with self.subTest(issue=issue):
                if issue == 2:
                    f.worktree = f.repo / ".agent-squad/worktrees/issue-2"
                    f.git("worktree", "add", "-b", "issue-2",
                          str(f.worktree))
                    f.push("value = 2\n")
                created = f.cli(
                    "pr", "create", "--as", "implementer", "--issue",
                    str(issue), "--task", f.task, "--report", f.report,
                )
                numbers[issue] = created["number"]
                row = f.read_model()["prs"][str(created["number"])]
                self.assertEqual(row["body"], (
                    TASK.strip() + "\n\n" + REPORT.strip()
                    + f"\n\nCloses #{issue}\n"
                ))
                self.assertEqual(row["title"], original["title"])
        record = json.loads(ownership.read_text())
        self.assertEqual((record["issue"], record["pr"]), (1, numbers[1]))


class PartialPrTests(unittest.TestCase):
    """#146: a partial PR ends with Ref #<issue> and leaves the issue open."""

    CREATE = ("pr", "create", "--as", "implementer", "--issue", "1")

    def each_fake(self, check) -> None:
        for fixture in (ForgeFixture, ForgejoFixture):
            with self.subTest(forge=fixture.__name__):
                f = fixture()
                self.addCleanup(f.close)
                f.initialize()
                f.candidate()
                check(f)

    def refused(self, f: ForgeFixture, args: tuple, message: str) -> None:
        """A refusal with exit 1 that writes nothing to the forge."""
        start = len(f.read_model()["calls"])
        prs = f.read_model()["prs"]
        error = f.cli(*args, expected=1)
        self.assertIn(message, error["error"])
        after = f.read_model()
        self.assertEqual(after["prs"], prs)
        self.assertEqual(
            [c for c in after["calls"][start:] if is_write(c)], [])

    def body(self, f: ForgeFixture, pr: int = 1) -> str:
        return f.read_model()["prs"][str(pr)]["body"]

    def test_partial_create_report_and_amendment_keep_ref(self) -> None:
        self.each_fake(self.check_partial_texts)

    def check_partial_texts(self, f: ForgeFixture) -> None:
        ownership = Path(
            f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree)
        ) / "agent-squad-implementation.json"
        start = len(f.read_model()["calls"])
        error = f.cli(
            *self.CREATE, "--partial", "--report", f.report, expected=2,
        )
        self.assertIn("--partial requires --task", error["error"])
        self.assertEqual(len(f.read_model()["calls"]), start)
        self.assertFalse(ownership.exists())
        task = TASK.strip() + "\n\nFixes #2 as well.\n"
        for name, text in (
            ("task", TASK + "\nThis CLOSES #1 for now.\n"),
            ("task", TASK + "\nfixes:\n#1\n"),
            ("report", REPORT.replace(
                "### Scope\n\nScripted fixture.",
                "### Scope\n\nResolved: #1 in part.")),
        ):
            with self.subTest(refused=text[-30:]):
                files = {"task": f.task, "report": f.report}
                files[name] = f.write(f"closing-{name}.md", text)
                self.refused(f, (
                    *self.CREATE, "--partial", "--task", files["task"],
                    "--report", files["report"],
                ), "must not close issue #1")
                self.assertFalse(ownership.exists())
        created = f.cli(
            *self.CREATE, "--partial", "--task", f.write("task-2.md", task),
            "--report", f.report,
        )
        self.assertEqual(created["number"], 1)
        self.assertEqual(
            self.body(f),
            task.strip() + "\n\n" + REPORT.strip() + "\n\nRef #1\n",
        )
        self.assertNotIn("Closes #1", self.body(f))
        self.assertIs(f.status()["closes_issue"], False)
        report = ("pr", "report", "--as", "implementer", "--pr", "1")
        before = self.body(f)
        self.refused(f, (*report, "--report", f.write(
            "closing-report.md", REPORT.replace("fixture.", "fix #1.", 1),
        )), "must not close issue #1")
        self.assertEqual(self.body(f), before)
        updated = REPORT.replace("Scripted fixture.", "Fixes #12 too.")
        f.cli(*report, "--report", f.write("updated-report.md", updated))
        self.assertEqual(
            self.body(f),
            task.strip() + "\n\n" + updated.strip() + "\n\nRef #1\n",
        )
        decisions = len(f.status()["decisions"])
        self.refused(f, (
            "decision", "post", "--as", "implementer", "--pr", "1",
            "--finding", "none", "--body", f.write("decision.md", "Amend."),
            "--task", f.write("closing-amendment.md", TASK + "\nCloses: #1\n"),
        ), "must not close issue #1")
        self.assertEqual(len(f.status()["decisions"]), decisions)
        amended = TASK.replace("Exercise", "Amend")
        f.decision(task=amended)
        state = f.status()
        self.assertEqual(
            self.body(f),
            amended.strip() + "\n\n" + updated.strip() + "\n\nRef #1\n",
        )
        self.assertFalse(state["task_body_stale"])
        self.assertIs(state["closes_issue"], False)
        # Without a footer the PR neither closes nor leaves the issue open.
        model = f.read_model()
        model["prs"]["1"]["body"] = before.removesuffix("\n\nRef #1\n")
        f.save_model(model)
        self.assertIsNone(f.status()["closes_issue"])

    def test_partial_merge_leaves_the_issue_open_for_a_later_pr(self) -> None:
        self.each_fake(self.check_two_stages)

    def merge_and_cleanup(self, f: ForgeFixture, pr: int) -> list[dict]:
        """Merge with cleanup held back by a scratch symlink, then finish."""
        scratch = f.repo / f".agent-squad/review-scratch/pr{pr}"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        target = f.root / f"held-scratch-{pr}"
        target.mkdir()
        scratch.symlink_to(target, target_is_directory=True)
        command = ("--as", "implementer", "--pr", str(pr))
        result = f.run([
            sys.executable, "-m", "agent_squad", "pr", "merge", *command,
            "--json",
        ], cwd=f.repo)
        self.assertEqual(result.returncode, 3, result.stderr)
        merged = json.loads(result.stdout)
        self.assertTrue(merged["merged"])
        self.assertEqual(merged["cleanup"][-1]["step"], "scratch directory")
        scratch.unlink()
        cleaned = f.cli("pr", "cleanup", *command, cwd=f.repo)
        self.assertTrue(all(s["ok"] for s in cleaned["cleanup"]))
        return [merged, cleaned]

    def check_two_stages(self, f: ForgeFixture) -> None:
        f.cli(*self.CREATE, "--partial", "--task", f.task,
              "--report", f.report)
        issue_scratch = f.repo / ".agent-squad/review-scratch/issue-1"
        issue_scratch.mkdir(parents=True)
        (issue_scratch / "report.md").write_text("first stage")
        f.review("approved")
        results = self.merge_and_cleanup(f, 1)
        self.assertEqual([r["closes_issue"] for r in results], [False, False])
        issue = f.read_model()["issues"]["1"]
        self.assertEqual(issue["state"], "open")
        self.assertEqual(issue["labels"], [{"name": "ready-for-agent"}])
        self.assertFalse(f.worktree.exists())
        self.assertEqual(f.git("branch", "--list", "issue-1"), "")
        self.assertEqual(
            f.git("ls-remote", "--heads", "origin", "issue-1"), "")
        self.assertFalse(issue_scratch.exists())
        self.assertEqual(
            f.git("worktree", "list", "--porcelain").count("worktree "), 1)
        # The next stage starts from a fresh issue worktree; its branch must
        # be new, because pr create refuses a branch that already had a PR.
        f.git("worktree", "add", "-b", "issue-1", str(f.worktree))
        f.push("value = 1\nstage = 2\n")
        task = f.write("closing-task.md", TASK + "\nFixes #1 at last.\n")
        self.refused(f, (*self.CREATE, "--task", task, "--report", f.report),
                     "a PR already exists for this branch")
        f.git("branch", "-m", "issue-1-stage-2", cwd=f.worktree)
        f.git("push", "-u", "origin", "HEAD", cwd=f.worktree)
        created = f.cli(*self.CREATE, "--task", task, "--report", f.report)
        self.assertEqual(created["number"], 2)
        self.assertEqual(self.body(f, 2), (
            (TASK + "\nFixes #1 at last.\n").strip() + "\n\n"
            + REPORT.strip() + "\n\nCloses #1\n"
        ))
        self.assertIs(
            f.cli("status", "--pr", "2")["closes_issue"], True)
        head = f.git("rev-parse", "HEAD", cwd=f.worktree)
        f.cli(
            "review", "post", "--as", "reviewer", "--pr", "2",
            "--head", head, "--base", f.git("rev-parse", "origin/main"),
            "--verdict", "approved", "--summary", f.summary,
            "--verified-dispositions", f.verified,
            "--threads", f.write("threads.json", "[]"),
        )
        results = self.merge_and_cleanup(f, 2)
        self.assertEqual([r["closes_issue"] for r in results], [True, True])
        self.assertEqual(f.read_model()["issues"]["1"]["state"], "closed")
        self.assertFalse(f.worktree.exists())


# Every list the GitHub snapshot reads through GraphQL (#149).
SNAPSHOT_LISTS = (
    "PullRequest.reviews", "PullRequestReview.comments",
    "PullRequest.reviewThreads", "PullRequestReviewThread.comments",
    "PullRequest.comments", "Commit.checkSuites", "CheckSuite.checkRuns",
)


def check_run(name: str, suite: int) -> dict:
    return {
        "name": name, "status": "completed", "conclusion": "success",
        "check_suite": {"id": suite}, "app": {"slug": "github-actions"},
        "html_url": f"https://github.example/runs/{name}",
        "started_at": "2026-11-09T10:00:00Z",
        "completed_at": "2026-11-09T10:05:00Z",
    }


class GitHubSnapshotReadTests(unittest.TestCase):
    """One GraphQL read stands for the REST reads behind ``status``."""

    def setUp(self) -> None:
        self.f = ForgeFixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.f.candidate()
        self.f.create_pr()
        self.f.seed_read_scenario()

    def gh_calls(self, *args: str) -> list[dict]:
        start = len(self.f.read_model()["calls"])
        self.f.cli(*args)
        return self.f.read_model()["calls"][start:]

    def snapshot(self):
        with patch.dict("os.environ", self.f.env, clear=True):
            forge = GitHub(
                load_initialized_repository(self.f.worktree), "implementer"
            )
            return forge.snapshot(1, with_checks=True)

    def seed_reviews(self, count: int) -> None:
        """``count`` reviews, each with one inline comment in its thread."""
        model = self.f.read_model()
        pr = model["prs"]["1"]
        template = next(
            c for c in pr["comments"] if c["in_reply_to_id"] is None
        )
        pr["reviews"] = [
            dict(pr["reviews"][0], id=200 + n, state="COMMENTED",
                 body=f"Review {n}.")
            for n in range(count)
        ]
        pr["comments"] = [
            dict(template, id=300 + n, pull_request_review_id=200 + n,
                 body=f"Note {n}.")
            for n in range(count)
        ]
        pr["threads"] = [
            {"id": f"T-{300 + n}", "isResolved": False,
             "comments": {"nodes": [{"databaseId": 300 + n}]}}
            for n in range(count)
        ]
        self.f.save_model(model)

    def seed_every_list(self) -> None:
        """Two or more entries in every list the snapshot reads."""
        model = self.f.read_model()
        pr = model["prs"]["1"]
        root = next(c for c in pr["comments"] if c["id"] == 103)
        pr["comments"].append(dict(
            root, id=107, pull_request_review_id=106, line=3,
            body="Note on line 3.", created_at="2026-01-01T00:00:07Z",
        ))
        pr["threads"].append({
            "id": "T-107", "isResolved": False,
            "comments": {"nodes": [{"databaseId": 107}]},
        })
        pr["conversation"] = [
            {"id": 108 + n, "body": f"Conversation {n}.",
             "user": {"login": "developer"},
             "created_at": f"2026-01-01T00:00:0{8 + n}Z"}
            for n in range(2)
        ]
        model.setdefault("checks", {})[pr["head"]["sha"]] = {
            "check_runs": [check_run("lint", 1), check_run("test", 1),
                           check_run("deploy", 2)],
            "workflow_runs": [
                {"check_suite_id": 1, "event": "pull_request"},
                {"check_suite_id": 2, "event": "push"},
            ],
        }
        self.f.save_model(model)

    def test_status_starts_at_most_three_gh_processes(self) -> None:
        for count in (1, 5):
            with self.subTest(reviews=count):
                self.seed_reviews(count)
                calls = self.gh_calls("status", "--pr", "1")
                self.assertLessEqual(
                    len(calls), 3, [c["arguments"][:2] for c in calls]
                )
                snapshot = self.snapshot()
                self.assertEqual(len(snapshot.reviews), count)
                self.assertEqual(len(snapshot.comments), count)

    def test_every_list_is_read_past_its_first_page(self) -> None:
        self.seed_every_list()
        baseline = self.snapshot()
        self.assertEqual(
            [len(baseline.reviews), len(baseline.comments),
             len(baseline.threads), len(baseline.conversation),
             len(baseline.head_checks)],
            [2, 4, 2, 2, 3],
        )
        for sizes in [{key: 1} for key in SNAPSHOT_LISTS] + [
            dict.fromkeys(SNAPSHOT_LISTS, 1)
        ]:
            with self.subTest(sizes=sizes):
                self.f.settings(page_sizes=sizes)
                start = len(self.f.read_model()["calls"])
                self.assertEqual(self.snapshot(), baseline)
                queries = [
                    (c["body"] or {}).get("query", "")
                    for c in self.f.read_model()["calls"][start:]
                ]
                for key in sizes:
                    owner, field = key.split(".")
                    self.assertTrue(any(
                        q.startswith("query SquadPage")
                        and f"... on {owner} {{ {field}(" in q
                        for q in queries
                    ), key)

    def test_status_equals_the_rest_shaped_evidence(self) -> None:
        # Replies in the flat listing carry the thread's side (#149).
        self.seed_every_list()
        state = self.f.status()
        self.assertEqual(state["ci"]["summary"], "passed")
        self.assertEqual(
            [(c["name"], c["event"]) for c in state["ci"]["checks"]],
            [("deploy", "push"), ("lint", "pull_request"),
             ("test", "pull_request")],
        )
        self.assertEqual(
            [r["id"] for r in state["findings"][0]["replies"]], [104, 105]
        )


class ForgejoReadCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.forge_support import ForgejoFixture

        self.f = ForgejoFixture(path_prefix="/instance/prefix")
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.seed_read_scenario()

    def test_read_commands_select_role_tokens_without_user_version_or_gh(
        self,
    ) -> None:
        f = self.f
        for args in (
            ("issue", "view", "--issue", "1"),
            ("pr", "head", "--pr", "1"),
            ("pr", "reviews", "--pr", "1"),
            ("status", "--pr", "1"),
        ):
            with self.subTest(command=args):
                before = len(f.read_model()["calls"])
                result = f.cli(*args)
                calls = f.read_model()["calls"][before:]
                self.assertTrue(calls)
                self.assertTrue(
                    all(
                        c["Authorization"] == "token fake-token-developer"
                        for c in calls
                    )
                )
                self.assertNotIn("fake-token-", json.dumps(result))
        reviewer = (
            f.repo / f".agent-squad/worktrees/reviewer-pr1-{self.head[:7]}"
        )
        f.git("worktree", "add", "--detach", str(reviewer), self.head)
        for cwd, extra in ((reviewer, ()), (f.repo, ("--as", "reviewer"))):
            before = len(f.read_model()["calls"])
            f.cli("status", "--pr", "1", *extra, cwd=cwd)
            self.assertTrue(
                all(
                    c["Authorization"] == "token fake-token-reviewer"
                    for c in f.read_model()["calls"][before:]
                )
            )
        calls = f.read_model()["calls"]
        self.assertTrue(all(c["method"] == "GET" for c in calls))
        self.assertFalse(any("arguments" in c for c in calls))
        self.assertFalse(
            any(c["path"].endswith(("/user", "/version")) for c in calls)
        )

    def test_shared_scenario_matches_github_derived_state_and_resolution(
        self,
    ) -> None:
        from agent_squad.conventions import derive
        from agent_squad.forge import make_forge

        other = ForgeFixture()
        self.addCleanup(other.close)
        other.initialize()
        other.candidate()
        # The shared scenario uses this fixture's real Git identities. Reusing
        # its PR model removes unrelated commit-time variation from comparison.
        model = other.read_model()
        model["prs"] = self.f.read_model()["prs"]
        model.pop("origin")
        other.save_model(model)
        first_repo = load_initialized_repository(self.f.repo)
        second_repo = load_initialized_repository(other.repo)
        first = make_forge(first_repo, "implementer").snapshot(1)
        with patch.dict("os.environ", other.env, clear=True):
            second = make_forge(second_repo, "implementer").snapshot(1)
        # Neutral snapshots deliberately have different display labels and
        # transport-specific opaque node IDs. Compare the derived decisions.
        from agent_squad.initialization import Worktree

        kwargs = dict(
            worktrees=(
                Worktree(self.f.worktree, self.head, "refs/heads/issue-1"),
            ),
            base=self.f.base,
            base_tip=self.f.base,
            ancestor=lambda old, new: old in (self.f.base, new),
        )
        first_state = derive(first, first_repo.configuration, **kwargs)
        second_state = derive(second, second_repo.configuration, **kwargs)
        for key in (
            "next_action",
            "approval",
            "task",
            "gates",
            "budget",
            "diagnostics",
        ):
            self.assertEqual(first_state[key], second_state[key], key)
        self.assertTrue(first_state["findings"][0]["resolved"])
        self.assertEqual(
            first_state["findings"][0]["settled"],
            second_state["findings"][0]["settled"],
        )
        state = self.f.status()
        self.assertEqual(
            {
                k: state[k]
                for k in (
                    "can_resolve_threads",
                    "can_read_thread_resolution",
                    "can_read_branch_rules",
                )
            },
            {
                "can_resolve_threads": False,
                "can_read_thread_resolution": True,
                "can_read_branch_rules": True,
            },
        )

    def test_unsupported_resolution_refuses_before_any_request(self) -> None:
        f = self.f
        before = f.read_model()["calls"]
        result = f.cli(
            "thread",
            "resolve",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--finding",
            "REV-1",
            expected=1,
        )
        self.assertIn("not supported on this forge", result["error"])
        self.assertEqual(f.read_model()["calls"], before)

    def test_faults_pending_requested_rows_shuffling_and_empty_hunks(
        self,
    ) -> None:
        f = self.f
        f.settings(
            pending_draft=True, request_review_rows=True, shuffle_comments=True
        )
        for _ in range(3):
            state = f.cli("status", "--pr", "1", "--as", "reviewer")
            self.assertEqual(state["findings"][0]["root"]["id"], 103)
            self.assertEqual(
                [r["id"] for r in state["findings"][0]["replies"]], [104, 105]
            )
        from agent_squad.forge import make_forge

        forge = make_forge(load_initialized_repository(f.repo), "reviewer")
        self.assertTrue(any(r.state == "pending" for r in forge.reviews(1)))
        self.assertFalse(any(r.evidence.id == 1 for r in forge.reviews(1)))
        f.settings(empty_hunk_paths=["example.py"])
        self.assertTrue(f.status()["findings"][0]["unanchored"])
        for call in f.read_model()["calls"]:
            if "/comments" in call["path"]:
                self.assertNotIn("?", call["path"])

    def test_fake_faults_for_recorded_mutation_responses_and_identity_gates(
        self,
    ) -> None:
        from agent_squad.forge import make_forge
        from tests.fixtures.fake_forgejo import recording

        f = self.f
        repo = load_initialized_repository(f.repo)
        prefix = "/repos/MagiLand/trial/pulls/1"
        f.settings(author_approval_422=True)
        forge = make_forge(repo, "implementer")
        for event, name in (
            ("APPROVED", "042-e8-approved.json"),
            ("REQUEST_CHANGES", "043-e8-request_changes.json"),
        ):
            with self.assertRaises(ForgeError) as caught:
                forge.api(
                    prefix + "/reviews", method="POST", body={"event": event}
                )
            self.assertEqual(caught.exception.status, 422)
            self.assertEqual(str(caught.exception), recording(name)["message"])
        f.settings(head_out_of_date_409=True)
        with self.assertRaises(ForgeError) as caught:
            forge.api(prefix + "/merge", method="POST", body={})
        self.assertEqual(caught.exception.status, 409)
        f.settings(unknown_event_pending=True)
        value = forge.api(
            prefix + "/reviews",
            method="POST",
            body={"event": "UNKNOWN", "commit_id": self.head},
        )
        self.assertEqual(value["state"], "PENDING")
        for settings, message in (
            ({"login_mismatch": "wrong"}, "does not match"),
            ({"version": "15.0.9"}, "16.0.0"),
        ):
            f.settings(login_mismatch="developer", version="16.0.3")
            f.settings(**settings)
            before = len(f.read_model()["calls"])
            forge = make_forge(repo, "implementer")
            with self.assertRaisesRegex(ForgeError, message):
                forge.api(prefix + "/merge", method="POST", body={})
            self.assertTrue(
                all(
                    c["method"] == "GET"
                    for c in f.read_model()["calls"][before:]
                )
            )

    def test_read_token_failure_and_identity_failure_do_not_leak_tokens(
        self,
    ) -> None:
        from agent_squad.forge import make_forge

        f = self.f
        repo = load_initialized_repository(f.repo)
        for setting in ("token_failure", "user_failure"):
            f.settings(token_failure=[], user_failure=[])
            f.settings(**{setting: ["developer"]})
            with self.assertRaises(ForgeError) as caught:
                make_forge(repo, "implementer").verify_identity()
            self.assertEqual(caught.exception.status, 401)
            self.assertNotIn("fake-token-developer", str(caught.exception))
        result = f.cli("doctor", expected=1)
        self.assertNotIn("fake-token-", json.dumps(result))

    def test_review_pagination_and_unpaginated_comments_over_fifty(
        self,
    ) -> None:
        f = self.f
        model = f.read_model()
        pr = model["prs"]["1"]
        review = pr["reviews"][0]
        pr["reviews"] = [
            dict(review, id=200 + i, body="untagged") for i in range(51)
        ]
        pr["comments"] = []
        comment = dict(model["issues"]["1"], body="conversation")
        pr["conversation"] = [
            dict(comment, id=300 + i) for i in reversed(range(51))
        ]
        f.save_model(model)
        from agent_squad.forge import make_forge

        forge = make_forge(load_initialized_repository(f.repo), "implementer")
        snapshot = forge.snapshot(1)
        self.assertEqual(len(snapshot.reviews), 51)
        self.assertEqual(
            [c.id for c in snapshot.conversation], list(range(300, 351))
        )
        review_paths = [
            c["path"]
            for c in f.read_model()["calls"]
            if "/reviews?" in c["path"]
        ]
        self.assertEqual(len(review_paths), 2)
        self.assertTrue(review_paths[0].endswith("limit=50&page=1"))
        self.assertTrue(review_paths[1].endswith("limit=50&page=2"))

    def test_server_uses_loopback_and_close_removes_listening_socket(
        self,
    ) -> None:
        import socket

        server = self.f.server
        address = server.server.server_address
        self.assertEqual(address[0], "127.0.0.1")
        server.close()
        self.assertFalse(server.thread.is_alive())
        with self.assertRaises(OSError):
            socket.create_connection(address, timeout=0.2)

    def test_redirect_never_forwards_the_authorization_header(self) -> None:
        from agent_squad.forge import make_forge
        from tests.forge_support import ForgejoFixture

        target = ForgejoFixture()
        self.addCleanup(target.close)
        target.initialize()
        repo = load_initialized_repository(self.f.repo)
        for destination in (
            target.server.base_url + "/api/v1/user",
            self.f.server.base_url + "/api/v1/user",
        ):
            with self.subTest(destination=destination):
                self.f.settings(redirect_to=destination)
                before = len(self.f.read_model()["calls"])
                with self.assertRaises(ForgeError) as caught:
                    make_forge(repo, "implementer").repository_record()
                self.assertEqual(caught.exception.status, 302)
                self.assertEqual(len(self.f.read_model()["calls"]), before + 1)
                self.assertEqual(target.read_model()["calls"], [])
