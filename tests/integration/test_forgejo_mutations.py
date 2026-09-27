"""Forgejo write/recovery contracts through the CLI in both identity modes."""

import json
from pathlib import Path
import sys
import unittest

from tests.forge_support import ForgeFixture, ForgejoFixture, REPORT, TASK, finding


class ForgejoMutationCases:
    single = False

    def setUp(self) -> None:
        self.f = ForgejoFixture(path_prefix="/instance")
        self.addCleanup(self.f.close)
        if self.single:
            self.f.single_identity()
        else:
            self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()

    @property
    def reviewer(self):
        return "developer" if self.single else "reviewer"

    def writes(self, start=0):
        return [c for c in self.f.read_model()["calls"][start:]
                if c["method"] != "GET"]

    def seed_draft(self, ident=999, account=None, state="PENDING"):
        model = self.f.read_model()
        model["prs"]["1"]["reviews"].append({
            "id": ident, "user": {"login": account or self.reviewer},
            "body": "Human draft content", "state": state,
            "commit_id": self.head, "dismissed": False,
            "submitted_at": "2026-01-01T00:00:01Z",
            "created_at": "2026-01-01T00:00:01Z",
        })
        self.f.save_model(model)

    def human_approve(self):
        if self.single:
            # Synthetic person-operated evidence, not an agent identity call.
            model = self.f.read_model()
            model["prs"]["1"]["reviews"].append({
                "id": 900, "user": {"login": "human"}, "body": "Approved",
                "state": "APPROVED", "commit_id": self.head,
                "dismissed": False, "submitted_at": "2026-01-01T01:00:00Z",
                "created_at": "2026-01-01T01:00:00Z",
            })
            self.f.save_model(model)

    def merge(self, expected=0, *options):
        result = self.f.run([
            sys.executable, "-m", "agent_squad", "pr", "merge", "--as",
            "implementer", "--pr", "1", "--json", *options,
        ], cwd=self.f.repo)
        self.assertEqual(result.returncode, expected,
                         result.stdout + result.stderr)
        self.assertNotIn("fake-token-", result.stdout + result.stderr)
        return json.loads(result.stdout) if result.stdout else {
            "error": result.stderr,
        }

    def test_body_first_ranges_replies_tokens_and_resolution(self):
        f = self.f
        f.settings(random_comment_order=True)
        items = [finding(line=1), dict(finding(title="Range", line=3),
                                      start_line=2)]
        result = f.review("changes_requested", items)
        writes = self.writes()
        review = next(c for c in writes if c["path"].endswith("/reviews"))
        roots = [c for c in writes if c["path"].endswith("/comments")]
        self.assertNotIn("comments", review["body"])
        self.assertIn("## Unanchored findings", review["body"]["body"])
        self.assertIn("**Verification**", review["body"]["body"])
        self.assertEqual(review["body"]["event"],
                         "COMMENT" if self.single else "REQUEST_CHANGES")
        self.assertEqual([c["body"]["new_position"] for c in roots], [1, 2])
        self.assertEqual([c["body"]["extra_lines_count"] for c in roots], [0, 1])
        self.assertEqual(result["findings"], ["REV-1", "REV-2"])
        state = f.status()
        self.assertEqual(state["next_action"], "address_findings")
        self.assertEqual([x["anchor"]["line"] for x in state["findings"]], [1, 3])
        # Replies must use the stored wire position, not the displayed line.
        model = f.read_model()
        for root in model["prs"]["1"]["comments"]:
            root["position"] += 100
        f.save_model(model)
        for fid in result["findings"]:
            f.reply(fid, "DISPOSITION rejected\nExisting evidence is correct.")
            f.reply(fid, "VERIFIED rejection accepted\nChecked evidence.",
                    role="reviewer")
        state = f.status()
        self.assertTrue(all(x["settled"] for x in state["findings"]))
        self.assertTrue(all(len(x["replies"]) == 2 for x in state["findings"]))
        replies = [c for c in self.writes()
                   if c["body"]["body"].startswith(("DISPOSITION", "VERIFIED"))]
        self.assertEqual([c["body"]["new_position"] for c in replies],
                         [101, 101, 102, 102])
        self.assertFalse(state["capabilities"]["can_resolve_threads"])
        before = len(f.read_model()["calls"])
        error = f.cli("thread", "resolve", "--as", "reviewer", "--pr", "1",
                      "--finding", "REV-1", expected=1)
        self.assertIn("not supported on this forge", error["error"])
        self.assertEqual(len(f.read_model()["calls"]), before)
        for call in self.writes():
            is_review = "/reviews" in call["path"]
            if is_review and call["body"]["body"].startswith("DISPOSITION"):
                expected = "developer"
            else:
                expected = self.reviewer if is_review else "developer"
            self.assertEqual(call["Authorization"], "token fake-token-" + expected)
        self.assertNotIn("fake-token-", json.dumps(state))
        self.assertTrue(all("GH_TOKEN" not in c for c in f.read_model()["calls"]))
        f.review("approved")
        self.assertEqual(f.status()["next_action"],
                         "await_human_approval" if self.single else "approved")

    def test_pending_gate_exact_discard_and_resume_gate(self):
        f = self.f
        self.seed_draft()
        f.settings(absorb_pending_draft=True)
        before = len(f.read_model()["calls"])
        error = f.review("approved", expected=4)
        self.assertIn("pending_draft", error["error"])
        self.assertIn("999", error["error"])
        self.assertEqual(self.writes(before), [])
        state = f.cli("status", "--pr", "1", "--as", "reviewer")
        self.assertEqual(state["pending_drafts"], [999])
        self.assertTrue(state["pending_draft"])
        for ident, owner, state in ((998, "other", "PENDING"),
                                    (997, self.reviewer, "COMMENT")):
            self.seed_draft(ident, owner, state)
        for ident in (998, 997, 996):
            error = f.review("approved", discard_draft=ident, expected=1)
            self.assertIn("not a PENDING review", error["error"])
        self.assertEqual(self.writes(before), [])
        result = f.review("approved", discard_draft=999)
        deleted = [c for c in self.writes(before) if c["method"] == "DELETE"]
        self.assertEqual([c["path"].split("/")[-1] for c in deleted], ["999"])
        self.assertEqual(f.status()["budget"]["used"], 1)
        self.seed_draft(995)
        error = f.review("approved", resume=result["review_id"], expected=4)
        self.assertIn("pending_draft", error["error"])
        f.review("approved", resume=result["review_id"], discard_draft=995)
        self.assertEqual(f.status()["budget"]["used"], 1)

    def test_invalid_anchors_and_duplicates_precede_even_discard_reads(self):
        f = self.f
        for items in (
            [finding(line=900)], [finding(), finding(title="Duplicate")],
            [dict(finding(line=3), start_line=2), finding(title="Same start")],
        ):
            before = len(f.read_model()["calls"])
            f.review("changes_requested", items, discard_draft=999, expected=1)
            self.assertEqual(len(f.read_model()["calls"]), before)
        self.assertEqual(f.status()["budget"]["used"], 0)

    def test_interruptions_and_resume_preserve_one_review_and_existing_roots(self):
        f = self.f
        f.settings(interrupt_before_review=True)
        f.review("changes_requested", [finding()], expected=1)
        self.assertEqual(f.status()["budget"]["used"], 0)
        f.settings(interrupt_before_review=False, interrupt_after_body=True)
        f.review("changes_requested", [finding(), finding(line=3)], expected=1)
        state = f.status()
        self.assertEqual(state["next_action"], "open_threads")
        self.assertEqual(state["budget"]["used"], 1)
        rid = state["reviews"][0]["id"]
        f.settings(interrupt_after_body=False, fail_roots=["REV-2"])
        f.review("changes_requested", [finding(), finding(line=3)],
                 resume=rid, expected=1)
        state = f.status()
        first_root = state["findings"][0]["root"]["id"]
        for verdict, items in (("needs_human", [finding(), finding(line=3)]),
                               ("changes_requested", [finding(title="Changed")])):
            before = len(f.read_model()["calls"])
            f.review(verdict, items, resume=rid, expected=1)
            self.assertEqual(self.writes(before), [])
        f.settings(fail_roots=[])
        f.review("changes_requested", [finding(), finding(line=3)], resume=rid)
        state = f.status()
        self.assertEqual(state["budget"]["used"], 1)
        self.assertEqual(state["findings"][0]["root"]["id"], first_root)
        self.assertEqual(state["next_action"], "address_findings")

    def test_drop_and_unusable_roots_recover_by_thread_open(self):
        f = self.f
        for fault in ("drop_root", "out_of_diff_accepted"):
            with self.subTest(fault=fault):
                f.settings(**{fault: True})
                f.review("changes_requested", [finding()], expected=1)
                state = f.status()
                self.assertEqual(state["next_action"], "open_threads")
                fid = state["findings"][-1]["finding"]
                f.settings(**{fault: False})
                f.cli("thread", "open", "--as", "implementer", "--pr", "1",
                      "--finding", fid, "--path", "example.py", "--line", "2")
                current = f.status()["findings"][-1]
                self.assertFalse(current["unanchored"])
                f.reply(fid, "DISPOSITION rejected\nEvidence already correct.")
                self.assertEqual(len(f.status()["findings"][-1]["replies"]), 1)
        self.assertEqual(f.status()["budget"]["used"], 2)

    def test_unknown_event_readback_and_author_approval_refusal(self):
        f = self.f
        f.settings(unknown_event_pending=True)
        error = f.review("approved", expected=1)
        self.assertIn("read-back failed", error["error"])
        model = f.read_model()
        rid = model["prs"]["1"]["reviews"][-1]["id"]
        self.assertIn(str(rid), error["error"])
        self.assertFalse(f.status()["approval"]["approved"])
        f.settings(unknown_event_pending=False, author_approval_422=True)
        if not self.single:
            model = f.read_model()
            model["prs"]["1"]["user"]["login"] = "reviewer"
            f.save_model(model)
            error = f.review("approved", discard_draft=rid, expected=1)
            from tests.fixtures.fake_forgejo import recording
            self.assertIn(recording("042-e8-approved.json")["message"],
                          error["error"])
        else:
            f.review("approved", discard_draft=rid)
            self.assertEqual(f.status()["next_action"], "await_human_approval")

    def test_task_mirror_retry_report_preservation_and_stop_validation(self):
        f = self.f
        f.review("approved")
        changed = TASK.replace("Exercise", "Amend")
        f.settings(fail_mirror=True)
        f.decision(task=changed, expected=3)
        state = f.status()
        self.assertTrue(state["task_body_stale"])
        self.assertEqual(state["next_action"], "launch_review")
        f.settings(fail_mirror=False)
        self.assertTrue(f.decision(task=changed)["reused"])
        report = f.write("report-next.md", REPORT.replace("Scripted", "Updated"))
        f.cli("pr", "report", "--as", "implementer", "--pr", "1",
              "--report", report)
        state = f.status()
        self.assertFalse(state["task_body_stale"])
        self.assertEqual(len(state["decisions"]), 1)
        self.assertIn("Amend", state["task"])
        self.assertIn("Updated", state["implementation_report"])
        f.settings(body_update_ignored=True)
        f.cli("pr", "report", "--as", "implementer", "--pr", "1",
              "--report", f.report, expected=1)
        before = len(f.read_model()["calls"])
        stop = ["stop", "post", "--as", "implementer", "--pr", "1",
                "--body", f.write("stop.md", "Preserve remaining work.")]
        f.cli(*stop, "--head", self.head, "--reason", "unknown", expected=1)
        f.cli(*stop, "--head", "a" * 7, "--reason", "ambiguity", expected=1)
        self.assertEqual(self.writes(before), [])
        f.cli(*stop, "--head", self.head, "--reason", "ambiguity")
        self.assertEqual(f.status()["next_action"], "stopped")

    def test_create_refusals_and_verbatim_bodies(self):
        f = self.f
        before = len(f.read_model()["calls"])
        error = f.cli("pr", "create", "--as", "implementer", "--issue", "1",
                      "--report", f.report, expected=1)
        self.assertIn("already exists", error["error"])
        f.commit("value = 4\n")
        f.cli("pr", "create", "--as", "implementer", "--issue", "1",
              "--report", f.report, expected=4)
        self.assertEqual(self.writes(before), [])
        body = "Exact prose.\n\n`literal` café  \nsecond line"
        f.decision(body=body)
        wire = self.writes()[-1]["body"]["body"]
        self.assertIn(body, wire)
        self.assertIn(body, f.status()["decisions"][-1]["body"])
        self.assertIn("Closes #1", f.status()["pr"]["evidence"]["body"])

    def test_merge_refusals_retain_resources_and_report_hidden_protection(self):
        f = self.f
        f.review("approved")
        self.human_approve()
        before = f.git("worktree", "list", "--porcelain")
        for fault in ("head_race_409", "merge_405", "merge_422"):
            f.settings(**{fault: True}, protection_403=True)
            result = self.merge(1)
            self.assertFalse(result["merged"])
            self.assertEqual(result["branch_rules"], {"visibility": "not visible"})
            self.assertEqual(f.git("worktree", "list", "--porcelain"), before)
            self.assertFalse(f.status()["pr"]["merged"])
            f.settings(**{fault: False})

    def test_failed_confirmation_read_keeps_merge_outcome_unknown(self):
        f = self.f
        f.review("approved")
        self.human_approve()
        f.settings(merge_confirmation_403=True)
        result = self.merge(1)
        self.assertIsNone(result["merged"])
        self.assertTrue(result["resources_retained"])
        self.assertIn("merge submitted; confirmation failed", result["error"])
        self.assertTrue(f.worktree.exists())
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), self.head)
        self.assertTrue(f.read_model()["prs"]["1"]["merged"])

    def test_merge_ancestry_and_tracking_cleanup_use_premerge_identity(self):
        f = self.f
        f.review("approved")
        self.human_approve()
        f.settings(post_merge_head_discrepancy=True)
        result = self.merge()
        self.assertEqual(result["head"], self.head)
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertTrue(all(s["ok"] for s in result["cleanup"]))
        self.assertFalse(f.worktree.exists())
        self.assertEqual(f.git("branch", "-r", "--list", "origin/issue-1"), "")
        writes = self.writes()
        merge = next(c for c in writes if c["path"].endswith("/merge"))
        self.assertEqual(merge["body"], {
            "Do": "merge", "head_commit_id": self.head,
            "delete_branch_after_merge": True,
        })
        self.assertFalse(any(c["method"] == "DELETE" for c in writes))
        self.assertEqual(f.cli("status", "--pr", "1", cwd=f.repo)["next_action"],
                         "merged")

    def test_squash_and_explicit_branch_delete(self):
        f = self.f
        path = f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config["merge_method"] = "squash"
        path.write_text(json.dumps(config))
        f.review("approved")
        self.human_approve()
        f.settings(leave_branch=True)
        result = self.merge()
        self.assertEqual(result["integration"], "verified by tree identity")
        deleted = [c for c in self.writes() if c["method"] == "DELETE"]
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0]["path"].endswith("/branches/issue-1"))
        self.assertEqual(f.git("branch", "-r", "--list", "origin/issue-1"), "")

    def test_branch_deletion_failure_retains_tracking_and_worktree(self):
        f = self.f
        f.review("approved")
        self.human_approve()
        f.settings(leave_branch=True, branch_delete_403=True)
        result = self.merge(3)
        self.assertTrue(result["merged"])
        self.assertEqual(result["cleanup"][-1]["step"], "remote branch")
        self.assertIn("forbidden", result["cleanup"][-1]["detail"])
        self.assertTrue(f.worktree.exists())
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), self.head)
        self.assertEqual(len([c for c in self.writes()
                              if c["method"] == "DELETE"]), 1)
        self.assertEqual(f.status()["next_action"], "merged")

    def test_changed_tracking_ref_is_retained(self):
        f = self.f
        f.review("approved")
        self.human_approve()
        f.git("update-ref", "refs/remotes/origin/issue-1", f.base)
        result = self.merge(3)
        self.assertEqual(result["cleanup"][-1]["step"], "remote-tracking ref")
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), f.base)
        self.assertTrue(f.worktree.exists())

    def test_moved_base_refuses_despite_live_api_base(self):
        f = self.f
        f.review("approved")
        self.human_approve()
        (f.repo / "base.txt").write_text("base advanced\n")
        f.git("add", "base.txt")
        f.git("commit", "-m", "test: advance base")
        f.git("push", "origin", "main")
        before = len(self.writes())
        error = self.merge(4)
        self.assertIn("base branch moved", error["error"])
        self.assertEqual(len(self.writes()), before)
        state = f.status()
        self.assertEqual(state["target"]["base"], f.base)
        self.assertNotEqual(state["target"]["base_tip"], f.base)


class DualForgejoMutationTests(ForgejoMutationCases, unittest.TestCase):
    pass


class SingleForgejoMutationTests(ForgejoMutationCases, unittest.TestCase):
    single = True


class SharedMutationGuards(unittest.TestCase):
    def test_github_duplicate_and_unsupported_discard(self):
        with ForgeFixture() as f:
            f.initialize()
            f.candidate()
            f.create_pr()
            before = len(f.read_model()["calls"])
            f.review("changes_requested", [finding(), finding()], expected=1)
            self.assertEqual(len(f.read_model()["calls"]), before)
            error = f.review("approved", discard_draft=999, expected=1)
            self.assertIn("not supported on this forge", error["error"])
            self.assertEqual(f.status()["budget"]["used"], 0)
