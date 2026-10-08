"""Forgejo write/recovery contracts through the CLI."""

import json
import sys
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from tests.forge_support import (
    ForgeFixture, ForgejoFixture, REPORT, TASK, finding,
)
from tests.fixtures.fake_forgejo import Handler


class ForgejoMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ForgejoFixture(path_prefix="/instance")
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()

    def writes(self, start=0):
        return [c for c in self.f.read_model()["calls"][start:]
                if c["method"] != "GET"]

    def seed_draft(self, ident=999, account=None, state="PENDING"):
        model = self.f.read_model()
        model["prs"]["1"]["reviews"].append({
            "id": ident, "user": {"login": account or "reviewer"},
            "body": "Human draft content", "state": state,
            "commit_id": self.head, "dismissed": False,
            "submitted_at": "2026-01-01T00:00:01Z",
            "created_at": "2026-01-01T00:00:01Z",
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
        items = [
            finding(line=1),
            dict(finding(title="Range", line=3), start_line=2),
        ]
        result = f.review("changes_requested", items)
        writes = self.writes()
        review = next(c for c in writes if c["path"].endswith("/reviews"))
        roots = [c for c in writes if c["path"].endswith("/comments")]
        self.assertNotIn("comments", review["body"])
        self.assertIn("## Unanchored findings", review["body"]["body"])
        self.assertIn("**Verification**", review["body"]["body"])
        self.assertEqual(review["body"]["event"], "REQUEST_CHANGES")
        self.assertEqual([c["body"]["new_position"] for c in roots], [1, 2])
        self.assertEqual(
            [c["body"]["extra_lines_count"] for c in roots], [0, 1])
        self.assertEqual(result["findings"], ["REV-1", "REV-2"])
        state = f.status()
        self.assertEqual(state["next_action"], "address_findings")
        self.assertEqual(
            [x["anchor"]["line"] for x in state["findings"]], [1, 3])
        # Replies must use the stored wire position, not the displayed line.
        model = f.read_model()
        for root in model["prs"]["1"]["comments"]:
            root["position"] += 100
        f.save_model(model)
        for fid in result["findings"]:
            f.reply(fid, "Existing evidence is correct.",
                    disposition="rejected")
            f.reply(fid, "Checked evidence.",
                    verification="rejection-accepted")
        state = f.status()
        self.assertTrue(all(x["settled"] for x in state["findings"]))
        self.assertTrue(all(len(x["replies"]) == 2 for x in state["findings"]))
        replies = [
            c for c in self.writes()
            if c["body"]["body"].startswith(("DISPOSITION", "VERIFIED"))
        ]
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
                expected = "reviewer" if is_review else "developer"
            self.assertEqual(
                call["Authorization"], "token fake-token-" + expected)
        self.assertNotIn("fake-token-", json.dumps(state))
        self.assertTrue(
            all("GH_TOKEN" not in c for c in f.read_model()["calls"]))
        f.review("approved")
        self.assertEqual(f.status()["next_action"], "approved")

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
                                    (997, "reviewer", "COMMENT")):
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

    def test_interruptions_and_resume_preserve_one_review_and_existing_roots(
        self,
    ) -> None:
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
        for verdict, items in (
            ("needs_human", [finding(), finding(line=3)]),
            ("changes_requested", [finding(title="Changed")]),
        ):
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
                f.reply(fid, "Evidence already correct.",
                        disposition="rejected")
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
        model = f.read_model()
        model["prs"]["1"]["user"]["login"] = "reviewer"
        f.save_model(model)
        error = f.review("approved", discard_draft=rid, expected=1)
        from tests.fixtures.fake_forgejo import recording
        self.assertIn(recording("042-e8-approved.json")["message"],
                      error["error"])

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
        report = f.write(
            "report-next.md", REPORT.replace("Scripted", "Updated"),
        )
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

    def test_issue_comment_posts_one_marked_note_on_the_open_issue(self):
        f = self.f
        model = f.read_model()
        for number, state in (("2", "open"), ("3", "closed")):
            model["issues"][number] = dict(
                model["issues"]["1"], id=int(number), number=int(number),
                state=state, conversation=[],
            )
        f.save_model(model)
        before = f.read_model()
        note = f.write("note.md", "Root cause: Forgejo deletes in-merge.\n")
        result = f.cli("issue", "comment", "--as", "implementer",
                       "--issue", "2", "--body", note)
        after = f.read_model()
        body = (
            "AGENT_SQUAD/0.5.0 NOTE role=implementer\n\n"
            "Root cause: Forgejo deletes in-merge."
        )
        [stored] = after["issues"]["2"]["conversation"]
        self.assertEqual((stored["body"], stored["user"]["login"]),
                         (body, "developer"))
        self.assertEqual((result["issue"], result["id"]), (2, stored["id"]))
        self.assertEqual(after["prs"], before["prs"])
        [write] = self.writes(len(before["calls"]))
        self.assertEqual(
            (write["method"], write["path"], write["Authorization"]),
            ("POST", "/instance/api/v1/repos/MagiLand/trial/issues/2/comments",
             "token fake-token-developer"),
        )
        after["issues"]["2"]["conversation"].append(dict(
            stored, id=stored["id"] + 1, body="Developer: also check X.",
        ))
        f.save_model(after)
        self.assertEqual(
            [(c["id"], c["agent_note"])
             for c in f.cli("issue", "view", "--issue", "2")["comments"]],
            [(stored["id"], True), (stored["id"] + 1, False)],
        )
        for issue, message in (
            ("3", "requires open issue #3"),
            ("1", "fixture issue and PR share number 1"),
            ("PR 1", "not pull request #1"),
        ):
            with self.subTest(issue=issue):
                if issue == "PR 1":
                    model = f.read_model()
                    del model["issues"]["1"]
                    f.save_model(model)
                before = f.read_model()
                refused = f.cli(
                    "issue", "comment", "--as", "implementer", "--issue",
                    issue.removeprefix("PR "), "--body", note, expected=1)
                after = f.read_model()
                self.assertIn(message, refused["error"])
                self.assertEqual((after["issues"], after["prs"]),
                                 (before["issues"], before["prs"]))

    def test_issue_create_files_one_marked_issue_for_triage(self):
        f = self.f
        model = f.read_model()
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
        note = f.write("follow-up.md", "Retry the cleanup read.\n")
        result = f.cli("issue", "create", "--as", "implementer",
                       "--title", "Retry cleanup", "--body", note,
                       "--from-pr", "1")
        after = f.read_model()
        body = (
            "AGENT_SQUAD/0.5.0 NOTE role=implementer\n\n"
            "Follow-up from pull request #1.\n\n"
            "Retry the cleanup read."
        )
        self.assertEqual(result, {
            "issue": 10, "title": "Retry cleanup",
            "labels": ["needs-triage"],
        })
        stored = after["issues"]["10"]
        self.assertEqual(
            (stored["state"], stored["body"], stored["user"]["login"],
             stored["labels"]),
            ("open", body, "developer", [{"id": 41, "name": "needs-triage"}]),
        )
        self.assertEqual(after["prs"], before["prs"])
        [write] = self.writes(len(before["calls"]))
        self.assertEqual(
            (write["method"], write["path"], write["Authorization"],
             write["body"]),
            ("POST", "/instance/api/v1/repos/MagiLand/trial/issues",
             "token fake-token-developer",
             {"title": "Retry cleanup", "body": body, "labels": [41]}),
        )
        self.assertIn(
            "/instance/api/v1/repos/MagiLand/trial/issues?state=open"
            "&type=issues&limit=50&page=1",
            [c["path"] for c in after["calls"][len(before["calls"]):]],
        )
        self.assertEqual(f.cli("issue", "view", "--issue", "10")["labels"],
                         ["needs-triage"])
        for options, expected, message in (
            (("--title", " Retry cleanup "), 1,
             "open issue #10 has the same title"),
            (("--title", "New work", "--from-pr", "4"), 1,
             "--from-pr 4 does not exist in the configured repository"),
            (("--title", "New work", "--from-issue", "99"), 1,
             "--from-issue 99 does not exist in the configured repository"),
            ("no label", 1, "repository has no needs-triage label"),
            # F16 CreateIssue drops labels without issue write permission.
            ("dropped", 3, "issue #11 was created with labels [] instead of"
             " only needs-triage"),
        ):
            with self.subTest(message=message):
                if options in ("no label", "dropped"):
                    model = f.read_model()
                    if options == "no label":
                        model["labels"] = [{"id": 1, "name": "bug"}]
                    else:
                        model["labels"] = before["labels"]
                        model["settings"]["drop_issue_labels"] = True
                    f.save_model(model)
                    options = ("--title", "New work")
                start = f.read_model()
                refused = f.cli("issue", "create", "--as", "implementer",
                                *options, "--body", note, expected=expected)
                self.assertIn(message, refused["error"])
                end = f.read_model()
                writes = self.writes(len(start["calls"]))
                if expected == 3:
                    self.assertEqual(
                        [(w["method"], w["path"]) for w in writes],
                        [("POST",
                          "/instance/api/v1/repos/MagiLand/trial/issues")])
                    self.assertEqual(end["issues"]["11"]["labels"], [])
                else:
                    self.assertEqual(writes, [])
                    self.assertEqual(end["issues"], start["issues"])

    def historical_base(self):
        f = self.f
        f.git("push", "origin", "main:refs/heads/historical-base")
        model = f.read_model()
        model["prs"]["1"].update(state="closed")
        model["prs"]["1"]["base"]["ref"] = "historical-base"
        f.save_model(model)

    def pull_response(self, suffix):
        request = Request(
            self.f.server.base_url + "/api/v1/repos/MagiLand/trial/pulls"
            + suffix,
            headers={"Authorization": "token fake-token-developer"},
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_fake_reports_live_base_for_open_and_closed_prs(self):
        f = self.f
        self.historical_base()
        for state in ("open", "closed"):
            model = f.read_model()
            model["prs"]["1"]["state"] = state
            f.save_model(model)
            for tip in (f.base, self.head, None):
                with self.subTest(state=state, tip=tip):
                    if tip is None:
                        f.git("push", "origin", ":refs/heads/historical-base")
                    else:
                        f.git("push", "origin",
                              f"{tip}:refs/heads/historical-base", "--force")
                    listed = self.pull_response("?state=all")[0]
                    single = self.pull_response("/1")
                    for row in (listed, single):
                        self.assertEqual(row["base"]["sha"], tip or "")
                        self.assertEqual(row["merge_base"], f.base)

    def test_create_ignores_unrelated_pr_after_its_base_is_deleted(self):
        f = self.f
        self.historical_base()
        f.git("push", "origin", ":refs/heads/historical-base")
        model = f.read_model()
        # Simulate a missing head repository as well as the deleted base.
        model["prs"]["1"]["head"].pop("sha")
        model["prs"]["1"]["merge_base"] = ""
        model["issues"]["2"] = dict(model["issues"]["1"], id=2, number=2)
        f.save_model(model)
        f.worktree = f.repo / ".agent-squad/worktrees/issue-2"
        f.git("worktree", "add", "-b", "new-work", str(f.worktree), self.head)
        f.git("push", "-u", "origin", "HEAD", cwd=f.worktree)
        before = len(f.read_model()["calls"])
        created = f.cli("pr", "create", "--as", "implementer", "--issue",
                        "2", "--task", f.task, "--report", f.report)
        self.assertEqual(created["number"], 2)
        writes = self.writes(before)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["method"], "POST")
        self.assertTrue(writes[0]["path"].endswith("/pulls"))

    def test_create_refuses_matching_prs_with_incomplete_metadata(self):
        f = self.f
        self.historical_base()
        f.git("push", "origin", ":refs/heads/historical-base")
        for state in ("open", "closed"):
            for missing in (False, True):
                with self.subTest(state=state, missing=missing):
                    model = f.read_model()
                    row = model["prs"]["1"]
                    row["state"] = state
                    if missing:
                        row["head"].pop("sha", None)
                        row.pop("merge_base", None)
                    else:
                        row["head"]["sha"] = ""
                        row["merge_base"] = ""
                    f.save_model(model)
                    before = len(model["calls"])
                    error = f.cli(
                        "pr", "create", "--as", "implementer", "--issue",
                        "1", "--report", f.report, expected=1,
                    )
                    self.assertIn("a PR already exists for this branch",
                                  error["error"])
                    self.assertEqual(self.writes(before), [])

    def test_create_refuses_malformed_branch_identity_without_post(self):
        f = self.f
        dispatch = Handler.dispatch
        for row, message in (
            (None, "pull request"), ({}, "pull request head"),
            ({"head": []}, "pull request head"),
            ({"head": {}}, "head.ref"),
            ({"head": {"ref": ""}}, "head.ref"),
            ({"head": {"ref": 1}}, "head.ref"),
        ):
            def malformed(handler, model, auth, body):
                status, result = dispatch(handler, model, auth, body)
                if "/pulls?" in handler.path:
                    return 200, [row]
                return status, result

            with self.subTest(row=row), patch.object(
                Handler, "dispatch", malformed,
            ):
                before = len(f.read_model()["calls"])
                error = f.cli(
                    "pr", "create", "--as", "implementer", "--issue", "1",
                    "--report", f.report, expected=1,
                )
                self.assertIn(message, error["error"])
                self.assertEqual(self.writes(before), [])

    def test_merge_refusals_retain_resources_and_report_hidden_protection(
        self,
    ) -> None:
        f = self.f
        f.review("approved")
        before = f.git("worktree", "list", "--porcelain")
        for fault in ("head_race_409", "merge_405", "merge_422"):
            f.settings(**{fault: True}, protection_403=True)
            result = self.merge(1)
            self.assertIs(result["merged"], False)
            self.assertEqual(
                result["branch_rules"], {"visibility": "not visible"})
            self.assertEqual(f.git("worktree", "list", "--porcelain"), before)
            self.assertFalse(f.status()["pr"]["merged"])
            f.settings(**{fault: False})

    def test_failed_confirmation_read_keeps_merge_outcome_unknown(self):
        f = self.f
        f.review("approved")
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
        self.assertEqual(
            f.cli("status", "--pr", "1", cwd=f.repo)["next_action"], "merged",
        )

    def test_squash_and_explicit_branch_delete(self):
        f = self.f
        path = f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config["merge_method"] = "squash"
        path.write_text(json.dumps(config))
        f.review("approved")
        f.settings(leave_branch=True)
        result = self.merge()
        self.assertEqual(result["integration"], "verified by tree identity")
        deleted = [c for c in self.writes() if c["method"] == "DELETE"]
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0]["path"].endswith("/branches/issue-1"))
        self.assertEqual(f.git("branch", "-r", "--list", "origin/issue-1"), "")

    def test_concurrent_deletion_answered_500_counts_as_absent(self):
        """#120: another deletion lands between the read and the DELETE."""
        f = self.f
        f.review("approved")
        f.settings(leave_branch=True, branch_removed_before_delete=True)
        start = len(f.read_model()["calls"])
        result = self.merge()
        self.assertTrue(all(s["ok"] for s in result["cleanup"]))
        self.assertFalse(f.worktree.exists())
        self.assertEqual(f.git("branch", "-r", "--list", "origin/issue-1"), "")
        calls = f.read_model()["calls"][start:]
        deleted = [c for c in calls if c["method"] == "DELETE"]
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0]["path"].endswith("/branches/issue-1"))
        self.assertFalse(any(
            c["path"].partition("?")[0].endswith("/repos/MagiLand/trial")
            for c in calls
        ))

    def test_branch_deletion_failure_retains_tracking_and_worktree(self):
        f = self.f
        f.review("approved")
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
        f.git("update-ref", "refs/remotes/origin/issue-1", f.base)
        result = self.merge(3)
        self.assertEqual(result["cleanup"][-1]["step"], "remote-tracking ref")
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), f.base)
        self.assertTrue(f.worktree.exists())

    def test_unreadable_remote_branch_retains_tracking_ref(self) -> None:
        f = self.f
        f.review("approved")
        f.settings(branch_read_403=True)
        result = self.merge(3)
        self.assertIs(result["merged"], True)
        self.assertEqual(result["cleanup"][-1]["step"], "remote branch")
        self.assertIn("unreadable", result["cleanup"][-1]["detail"])
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), self.head)
        self.assertTrue(f.worktree.exists())
        self.assertFalse(any(c["method"] == "DELETE" for c in self.writes()))

    def test_ignored_branch_deletion_is_reported_not_trusted(self) -> None:
        f = self.f
        f.review("approved")
        f.settings(leave_branch=True, branch_delete_ignored=True)
        result = self.merge(3)
        self.assertEqual(result["cleanup"][-1]["step"], "remote branch")
        self.assertIn("remote branch remains", result["cleanup"][-1]["detail"])
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), self.head)
        self.assertTrue(f.worktree.exists())

    def test_thread_open_refuses_a_location_held_by_another_root(self) -> None:
        f = self.f
        f.settings(fail_roots=["REV-2"])
        f.review("changes_requested", [finding(), finding(line=3)], expected=1)
        self.assertEqual(f.status()["next_action"], "open_threads")
        f.settings(fail_roots=[])
        before = len(f.read_model()["calls"])
        error = f.cli(
            "thread", "open", "--as", "reviewer", "--pr", "1",
            "--finding", "REV-2", "--path", "example.py", "--line", "2",
            expected=1,
        )
        self.assertIn("already has a root", error["error"])
        self.assertEqual(self.writes(before), [])
        f.cli("thread", "open", "--as", "reviewer", "--pr", "1",
              "--finding", "REV-2", "--path", "example.py", "--line", "3")
        state = f.status()
        self.assertEqual(state["next_action"], "address_findings")
        self.assertEqual(
            [(item["finding"], item["anchor"]["line"])
             for item in state["findings"]],
            [("REV-1", 2), ("REV-2", 3)],
        )

    def test_moved_base_refuses_despite_live_api_base(self):
        f = self.f
        f.review("approved")
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

    def test_two_pass_loop_reads_back_dismissal_without_mismatch(self):
        f = self.f
        fid = f.review("changes_requested", [finding(line=1)])["findings"][0]
        head = f.push("value = 10\nsecond = 2\nthird = 3\n")
        f.reply(fid, "Changed the fixture line.", disposition="fixed",
                sha=head)
        f.reply(fid, "Checked the new head.", verification="fixed")
        f.review("approved")
        state = f.status()
        first, second = (r["id"] for r in state["reviews"])
        rows = {r["id"]: r for r in review_rows(f)}
        self.assertEqual((rows[first]["state"], rows[first]["dismissed"]),
                         ("REQUEST_CHANGES", True))
        self.assertEqual((rows[second]["state"], rows[second]["dismissed"]),
                         ("APPROVED", False))
        self.assertEqual(state["next_action"], "approved")
        self.assertEqual(state["diagnostics"], [])
        result = self.merge()
        self.assertIs(result["merged"], True)
        self.assertEqual(result["head"], head)


def post_review(f, account, event, head):
    request = Request(
        f.server.base_url + "/api/v1/repos/MagiLand/trial/pulls/1/reviews",
        data=json.dumps({"body": f"{account} {event}", "event": event,
                         "commit_id": head}).encode(),
        headers={"Authorization": "token fake-token-" + account,
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def review_rows(f, account="developer"):
    request = Request(
        f.server.base_url + "/api/v1/repos/MagiLand/trial/pulls/1/reviews",
        headers={"Authorization": "token fake-token-" + account},
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def decisions(rows, login):
    return [(r["state"], r["dismissed"], r["official"]) for r in rows
            if r["user"]["login"] == login and r["state"] != "COMMENT"]


class FakeForgejoSupersededReviewTests(unittest.TestCase):
    """Forgejo 16.0.3 dismissal of superseded decisions (#54, E9)."""

    def test_new_decision_dismisses_only_the_accounts_earlier_decisions(
        self,
    ) -> None:
        from tests.fixtures.fake_forgejo import recording

        def recorded(name):
            return decisions(recording(name), "approver")

        with ForgejoFixture() as f:
            f.initialize()
            head = f.candidate()
            f.create_pr()
            comment = post_review(f, "reviewer", "COMMENT", head)
            post_review(f, "reviewer", "APPROVED", head)
            post_review(f, "reviewer", "REQUEST_CHANGES", head)
            self.assertEqual(
                decisions(review_rows(f), "reviewer"),
                recorded("053-e9-after-request-changes.json"),
            )
            head = f.push("value = 10\nsecond = 2\nthird = 3\n")
            post_review(f, "reviewer", "APPROVED", head)
            expected = recorded("059-e9-after-new-approval.json")
            self.assertEqual(decisions(review_rows(f), "reviewer"), expected)
            later = post_review(f, "reviewer", "COMMENT", head)
            rows = {r["id"]: r for r in review_rows(f)}
            for ident in (comment["id"], later["id"]):
                self.assertEqual(
                    (rows[ident]["dismissed"], rows[ident]["official"]),
                    (False, False),
                )
            self.assertEqual(decisions(rows.values(), "reviewer"), expected)
            other = post_review(f, "human", "APPROVED", head)
            self.assertEqual((other["dismissed"], other["official"]),
                             (False, True))
            self.assertEqual(decisions(review_rows(f), "reviewer"), expected)
            model = f.read_model()
            model["prs"]["1"]["reviews"].append({
                "id": 999, "user": {"login": "reviewer"}, "body": "Draft",
                "state": "PENDING", "commit_id": head, "dismissed": False,
                "submitted_at": None, "created_at": "2026-01-01T01:00:00Z",
            })
            f.save_model(model)
            f.settings(absorb_pending_draft=True)
            absorbed = post_review(f, "reviewer", "REQUEST_CHANGES", head)
            self.assertEqual(absorbed["id"], 999)
            rows = review_rows(f, "reviewer")
            self.assertEqual(
                [dismissed for _, dismissed, _ in decisions(rows, "reviewer")],
                [True, True, False, False],
            )
            self.assertEqual(decisions(rows, "human"),
                             [("APPROVED", False, True)])


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
