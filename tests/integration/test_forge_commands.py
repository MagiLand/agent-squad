"""Real command invocations against the committed fake forge and real Git."""

from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.forge_support import ForgeFixture, TASK, REPORT, finding
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
        recorded = f.decision(body=MERGE_INSTRUCTION + '\n\n> Start #1.')
        state = f.status()
        self.assertEqual(state["next_action"], "merge")
        self.assertEqual(state["merge_instruction"]["id"],
                         recorded["decision"]["id"])
        f.decision(body=MERGE_WITHDRAWAL)
        self.assertEqual(f.status()["next_action"], "approved")
        for directive in (MERGE_INSTRUCTION, MERGE_WITHDRAWAL):
            for kwargs in ({"budget": 4}, {"task": TASK}, {"fid": "REV-1"}):
                with self.subTest(directive=directive, kwargs=kwargs):
                    before = f.read_model()["prs"]
                    refused = f.decision(body=directive, expected=1, **kwargs)
                    self.assertIn("standing merge decisions", refused["error"])
                    self.assertEqual(f.read_model()["prs"], before)

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
        for body, expected, message, issue in (
            ("DISPOSITION rejected", 1, "second non-empty line", None),
            ("DISPOSITION rejected\nNot pursued:   ", 1,
             "second non-empty line", None),
            ("DISPOSITION rejected\nUnqualified reason.", 1,
             "second non-empty line", None),
            ("DISPOSITION rejected\nDeferred to #01:", 1,
             "second non-empty line", None),
            ("DISPOSITION rejected\nDeferred to other/repo#1:", 1,
             "second non-empty line", None),
            ("Unstructured reply", 1, "requires a DISPOSITION", None),
            ("DISPOSITION rejected\n\nDeferred to #2: follow-up.", 1,
             "requires an open issue", 2),
            ("DISPOSITION rejected\nDeferred to #3: a pull request.", 1,
             "requires an open issue", 3),
            ("DISPOSITION rejected\nDeferred to #999:", 1,
             "issue not found", 999),
            ("DISPOSITION rejected\n\nNot pursued: unnecessary here.",
             0, None, None),
            ("DISPOSITION rejected\n\nDeferred to #1: follow-up.",
             0, None, 1),
        ):
            with self.subTest(body=body):
                before = f.read_model()
                result = f.reply("REV-1", body, expected=expected)
                after = f.read_model()
                if expected:
                    self.assertIn(message, result["error"])
                    self.assertEqual(after["prs"], before["prs"])
                else:
                    self.assertEqual(
                        f.status()["optional_findings"][0]
                        ["disposition"]["body"], body
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
        f.reply(
            "REV-1", "DISPOSITION rejected\nNot pursued: unnecessary here."
        )
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
            "REV-1", f"DISPOSITION fixed {self.head}", expected=1
        )
        self.assertIn("absent from", refused["error"])
        f.reply("REV-1", "DISPOSITION needs-human\nA Developer choice.")
        refused = f.cli("reviewer", "launch", "--pr", "1", expected=4)
        self.assertIn("needs_decision", refused["error"])
        f.decision(fid="REV-1")
        self.assertFalse(f.status()["gates"]["needs_decision"])
        head = f.push("value = 1\nsecond = 20\nthird = 3\n")
        f.reply("REV-1", f"DISPOSITION fixed {head}\nRun the fixture probe.")
        self.assertFalse(f.status()["gates"]["unaddressed_findings"])
        f.reply("REV-1", "VERIFIED fixed\nFixture checked.", "reviewer")
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
        f.reply("REV-1", f"DISPOSITION fixed {head}")
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
        f.reply("REV-1", "NOT FIXED", "reviewer")
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
        f.reply("REV-1", "DISPOSITION rejected\n\nEvidence.")
        f.reply("REV-1", "VERIFIED rejection accepted\n\nChecked.", "reviewer")
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
                and "query SquadThreads" in c["body"]["query"]
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
            "changes_requested", [finding(), finding("optional", "Optional")]
        )
        f.reply(
            "REV-1",
            "DISPOSITION rejected\n\nEvidence from omitted flat listing.",
        )
        f.reply("REV-1", "VERIFIED rejection accepted\n\nChecked.", "reviewer")
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
        inputs = [finding(), finding("optional", "Optional")]
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
        inputs = [finding(), finding("optional", "Optional")]
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
        f.reply("REV-1", "VERIFIED fixed", expected=1)
        f.reply("REV-1", "DISPOSITION rejected", "reviewer", expected=1)
        f.reply("REV-1", f"DISPOSITION fixed {self.head}", expected=1)
        f.reply("REV-1", "DISPOSITION fixed abc123", expected=1)
        f.reply("REV-1", "DISPOSITION fixed " + "f" * 40, expected=1)
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
        f.reply("REV-1", "DISPOSITION rejected")
        f.reply("REV-1", "VERIFIED rejection accepted", "reviewer")
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


class SingleForgeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ForgeFixture()
        self.addCleanup(self.f.close)
        self.f.single_identity()
        self.head = self.f.candidate()
        self.f.create_pr()

    def test_shared_comment_approval_human_wait_and_guarded_merge(
        self,
    ) -> None:
        import sys
        f = self.f
        f.review('approved')
        waiting = f.status()
        self.assertEqual(waiting['next_action'], 'await_human_approval')
        refused = f.cli('pr', 'merge', '--as', 'implementer',
                        '--pr', '1', expected=4)
        self.assertIn('human', refused['error'])
        text = f.run(
            [sys.executable, '-m', 'agent_squad', 'status', '--pr', '1'])
        self.assertEqual(text.returncode, 0)
        self.assertTrue(text.stdout.startswith('await_human_approval:'))
        self.assertIn('human', text.stdout.splitlines()[0])
        calls = f.read_model()['calls']
        submitted = [c for c in calls if (c.get('body') or {}).get('event')]
        self.assertEqual(submitted[-1]['body']['event'], 'COMMENT')
        self.assertTrue(all(c['account'] == 'developer' for c in calls
                            if c['arguments'][0] == 'api'))
        token_args = ['auth', 'token', '--user', 'developer']
        self.assertTrue(any(c['arguments'] == token_args for c in calls))
        human = f.human_review('APPROVE')
        approved = f.status()
        self.assertTrue(approved['approval']['approved'])
        self.assertEqual(approved['human_approvals'][0]['id'], human['id'])
        self.assertEqual(approved['budget']['used'], 1)
        f.decision(body=MERGE_INSTRUCTION)
        self.assertEqual(f.status()['next_action'], 'merge')
        merged = f.cli('pr', 'merge', '--as', 'implementer',
                       '--pr', '1', cwd=f.repo)
        self.assertEqual(merged['integration'], 'verified by ancestry')

    def test_human_request_changes_does_not_gate_launch_and_replacement_wins(
        self,
    ) -> None:
        f = self.f
        f.human_review('APPROVE')
        f.review('approved')
        requested = f.human_review('REQUEST_CHANGES')
        state = f.status()
        self.assertFalse(state['approval']['approved'])
        self.assertEqual(state['human_request_changes']
                         [0]['id'], requested['id'])
        self.assertTrue(state['human_approvals'][0]['dismissed'])
        self.assertFalse(state['gates']['needs_decision'])
        f.push('value = 2\nsecond = 2\nthird = 3\n')
        f.cli('reviewer', 'launch', '--pr', '1')
        f.review('approved')
        self.assertEqual(f.status()['next_action'], 'await_human_approval')
        f.human_review('APPROVE')
        self.assertTrue(f.status()['approval']['approved'])
        self.assertEqual(f.status()['human_request_changes'], [])

    def test_shared_findings_dispositions_and_verifications(self) -> None:
        f = self.f
        f.review('changes_requested', [finding()])
        self.assertEqual(f.status()['next_action'], 'address_findings')
        f.reply('REV-1',
                'DISPOSITION rejected\nAlready correct in this fixture.')
        f.reply('REV-1', 'VERIFIED rejection accepted\nScripted check.',
                role='reviewer')
        f.review('approved')
        state = f.status()
        self.assertTrue(state['findings'][0]['settled'])
        self.assertEqual(state['next_action'], 'await_human_approval')
        self.assertEqual([r['state'] for r in state['reviews']],
                         ['COMMENTED', 'COMMENTED'])
        f.review('needs_human')
        self.assertEqual(f.status()['next_action'], 'needs_decision')


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

    def test_every_mutation_is_refused_before_protocol_or_http_work(
        self,
    ) -> None:
        f = self.f
        before = f.read_model()["calls"]
        commands = [
            ("pr", "create", "--issue", "1", "--report", f.report),
            ("pr", "report", "--pr", "1", "--report", f.report),
            ("pr", "merge", "--pr", "1"),
            (
                "thread",
                "reply",
                "--pr",
                "1",
                "--finding",
                "REV-1",
                "--body",
                f.report,
            ),
            (
                "thread",
                "open",
                "--pr",
                "1",
                "--finding",
                "REV-1",
                "--path",
                "example.py",
                "--line",
                "2",
            ),
            (
                "decision",
                "post",
                "--pr",
                "1",
                "--finding",
                "none",
                "--body",
                f.report,
            ),
            (
                "stop",
                "post",
                "--pr",
                "1",
                "--head",
                self.head,
                "--reason",
                "manual",
                "--body",
                f.report,
            ),
        ]
        for args in commands:
            with self.subTest(command=args):
                result = f.cli(*args, "--as", "implementer", expected=1)
                self.assertIn(
                    "not implemented until Increment 4", result["error"]
                )
        result = f.review("approved", expected=1)
        self.assertIn("not implemented until Increment 4", result["error"])
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
