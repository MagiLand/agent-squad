"""Real command invocations against the committed fake forge and real Git."""

from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.forge_support import ForgeFixture, TASK, REPORT, finding
from agent_squad.forge import ForgeError, GitHub, PullRequest, Review
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
                PullRequest.from_dict(value)
            with self.assertRaises(ForgeError):
                Review.from_dict(value)
