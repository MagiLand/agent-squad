"""Real command invocations against the committed fake forge and real Git."""

from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.forge_support import ForgeFixture, TASK, REPORT, finding
from agent_squad.forge import ForgeError, GitHub, PullRequest, Review
from agent_squad.initialization import load_initialized_repository


class ForgeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ForgeFixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()

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
