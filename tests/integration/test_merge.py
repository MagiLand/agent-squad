"""Human-invoked merges against real Git history behind the fake forge."""

import json
import os
from dataclasses import replace
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

from tests._support import FakeClock
from tests.forge_support import ForgeFixture
from agent_squad.github import GitHub
from agent_squad.initialization import (
    AgentSquadError, RetainedError, git_output, list_worktrees,
    load_initialized_repository, run_git,
)
from agent_squad.merging import (
    cleanup_merge, fast_forward_primary, merge_pr,
    implementation_identity, implementation_metadata, owned_implementation,
)


def run_pr(
    test: unittest.TestCase, f: ForgeFixture, command: str, expected: int,
) -> dict:
    result = f.run([
        sys.executable, "-m", "agent_squad", "pr", command,
        "--as", "implementer", "--pr", "1", "--json",
    ], cwd=f.repo)
    test.assertEqual(result.returncode, expected,
                     result.stdout + result.stderr)
    return (
        json.loads(result.stdout) if result.stdout
        else {"error": result.stderr}
    )


class ExternalWorktreeMergeTests(unittest.TestCase):
    def external(self) -> tuple[ForgeFixture, Path]:
        """The Implementer works in an issue worktree outside the primary."""
        f = ForgeFixture()
        self.addCleanup(f.close)
        f.initialize()
        path = f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config["worktree_root"] = str(f.root / "external-worktrees")
        path.write_text(json.dumps(config))
        f.worktree = Path(config["worktree_root"]) / "issue-1"
        f.git("worktree", "add", "-b", "issue-1", str(f.worktree))
        f.push("value = 1\nsecond = 2\nthird = 3\n")
        f.create_pr()
        f.herdr_settings(implementer={"cwd": str(f.worktree)})
        review = Path(f.cli("reviewer", "launch", "--pr", "1")["worktree"])
        f.review("approved")
        return f, review

    def test_cleanup_resolves_session_before_removing_implementation(
        self,
    ) -> None:
        """PR #108 REV-1: the Implementer works in the issue worktree."""
        f, review = self.external()
        model = f.herdr_model()
        model["calls"] = []
        f.save_herdr(model)
        result = f.cli("pr", "merge", "--as", "implementer", "--pr", "1",
                       cwd=f.repo)
        self.assertTrue(result["merged"])
        self.assertEqual(
            [s for s in result["cleanup"] if not s["ok"]], [])
        self.assertFalse(f.worktree.exists())
        self.assertFalse(review.exists())
        model = f.herdr_model()
        self.assertEqual(model["workspaces"], [])
        self.assertEqual(
            sum(c == ["session", "list", "--json"] for c in model["calls"]),
            1,
        )

    def test_resumed_cleanup_finds_the_implementer_in_its_removed_worktree(
        self,
    ) -> None:
        """PR #134 REV-2: only the record's vacant issue path is admitted."""
        f, review = self.external()
        f.herdr_settings(snapshot_failure=True)
        result = run_pr(self, f, "merge", 3)
        self.assertEqual(result["cleanup"][-1]["step"], "review worktree")
        self.assertFalse(f.worktree.exists())
        f.herdr_settings(snapshot_failure=False)
        sibling = f.worktree.parent / "issue-2"
        for cwd, recreate in ((sibling, False), (f.worktree, True)):
            with self.subTest(cwd=cwd.name, recreated=recreate):
                if recreate:
                    f.worktree.mkdir()
                f.herdr_settings(implementer={"cwd": str(cwd)})
                result = run_pr(self, f, "cleanup", 3)
                self.assertEqual(
                    result["cleanup"][-1]["step"], "review worktree")
                self.assertIn("no running Herdr session",
                              result["cleanup"][-1]["detail"])
                self.assertTrue(review.exists())
        f.worktree.rmdir()
        result = run_pr(self, f, "cleanup", 0)
        self.assertTrue(all(s["ok"] for s in result["cleanup"]))
        self.assertEqual(result["merge_record"]["result"], "deleted")
        self.assertFalse(review.exists())
        self.assertEqual(f.herdr_model()["workspaces"], [])
        self.assertFalse((f.repo / ".git/agent-squad-merge-pr1.json").exists())


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ForgeFixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()
        self.f.review("approved")

    def test_tracking_ref_change_retains_local_resources(self) -> None:
        f = self.f
        f.git("update-ref", "refs/remotes/origin/issue-1", f.base)
        result = self.merge(expected=3)
        self.assertTrue(result["merged"])
        self.assertEqual(result["cleanup"][-1]["step"], "remote-tracking ref")
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), f.base)
        self.assertTrue(f.worktree.exists())

    def test_absent_tracking_ref_is_a_successful_noop(self) -> None:
        f = self.f
        f.git("update-ref", "-d", "refs/remotes/origin/issue-1")
        result = self.merge()
        step = next(s for s in result["cleanup"]
                    if s["step"] == "remote-tracking ref")
        self.assertTrue(step["ok"])
        self.assertEqual(step["detail"], "already absent")

    def test_branch_delete_refusal_keeps_tracking_ref_and_worktree(
        self,
    ) -> None:
        f = self.f
        f.settings(branch_delete_403=True)
        result = self.merge(expected=3)
        self.assertEqual(result["cleanup"][-1]["step"], "remote branch")
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), self.head)
        self.assertTrue(f.worktree.exists())

    def test_unreadable_remote_branch_retains_tracking_ref(self) -> None:
        f = self.f
        f.settings(branch_read_403=True)
        result = self.merge(expected=3)
        self.assertIs(result["merged"], True)
        self.assertEqual(result["cleanup"][-1]["step"], "remote branch")
        self.assertIn("unreadable", result["cleanup"][-1]["detail"])
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), self.head)
        self.assertTrue(f.worktree.exists())
        self.assertFalse(any("DELETE" in c["arguments"]
                             for c in f.read_model()["calls"]))

    def test_hold_names_review_and_requires_explicit_acceptance(self) -> None:
        f = self.f
        f.review("approved", sections={"merge-hold": "Item 3: merge rules."})
        f.decision(merge_instruction="record", body="")
        state = f.status()
        hold = state["merge_hold"]
        self.assertEqual(hold["text"], "Item 3: merge rules.")
        self.assertEqual(state["next_action"], "approved")
        for options in ((), ("--accept-moved-base",)):
            refused = self.merge(*options, expected=4)
            self.assertIn(f'merge hold in review {hold["review_id"]}',
                          refused["error"])
            self.assertTrue(f.worktree.exists())
            self.assertFalse(f.read_model()["prs"]["1"]["merged"])
        merged = self.merge("--accept-merge-hold")
        self.assertTrue(merged["merged"])
        self.assertEqual(merged["integration"], "verified by ancestry")

    def merge(
        self, *options: str, expected: int = 0, cwd: Path | None = None
    ) -> dict:
        f = self.f
        result = f.run([
            sys.executable, "-m", "agent_squad", "pr", "merge",
            "--as", "implementer", "--pr", "1", "--json", *options,
        ], cwd=cwd or f.repo)
        self.assertEqual(result.returncode, expected,
                         result.stdout + result.stderr)
        return (
            json.loads(result.stdout) if result.stdout
            else {"error": result.stderr}
        )

    def method(self, method: str) -> None:
        path = self.f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config["merge_method"] = method
        path.write_text(json.dumps(config))

    def test_cli_reports_configured_paths_without_reading_local_files(
        self,
    ) -> None:
        issue = self.f.cli("issue", "view", "--issue", "1")
        state = self.f.status()
        self.assertEqual(issue["paths"]["primary"], str(self.f.repo))
        self.assertEqual(
            issue["paths"]["worktree_root"], str(self.f.worktree.parent)
        )
        self.assertEqual(
            state["paths"]["scratch"],
            str(self.f.repo / ".agent-squad/review-scratch/pr1"),
        )
        self.assertEqual(
            issue["paths"]["issue_scratch"],
            str(self.f.repo / ".agent-squad/review-scratch/issue-1"),
        )
        self.assertIsNone(state["paths"]["issue_scratch"])

    def test_issue_scratch_uses_owned_issue_number_not_pr_number(self) -> None:
        f = ForgeFixture()
        self.addCleanup(f.close)
        f.initialize()
        f.candidate()
        moved = f.worktree.parent / "issue-42"
        f.git("worktree", "move", str(f.worktree), str(moved))
        f.worktree = moved
        model = f.read_model()
        model["issues"]["42"] = dict(model["issues"]["1"], id=42, number=42)
        f.save_model(model)
        f.cli(
            "pr", "create", "--as", "implementer", "--issue", "42",
            "--task", f.task, "--report", f.report,
        )
        f.review("approved")
        root = f.repo / ".agent-squad/review-scratch"
        scratch = root / "issue-42"
        scratch.mkdir()
        (scratch / "report.md").write_text("draft")
        unrelated = root / "issue-1"
        unrelated.mkdir()
        (unrelated / "keep").write_text("keep")
        result = f.cli(
            "pr", "merge", "--as", "implementer", "--pr", "1", cwd=f.repo)
        self.assertFalse(scratch.exists())
        self.assertEqual((unrelated / "keep").read_text(), "keep")
        self.assertEqual(result["cleanup"][-1], {
            "step": "issue scratch directory", "ok": True,
            "detail": str(scratch),
        })

    def test_implementation_record_mismatches_retain_resources(self) -> None:
        f = self.f
        admin = Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        metadata = admin / "agent-squad-implementation.json"
        original = metadata.read_text()
        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.worktree)
            for key, value in (
                ("pr", 10), ("path", str(f.repo)), ("common", str(f.origin)),
                ("branch", "main"), ("issue", True), ("issue", 2),
                ("schema_version", 2),
            ):
                with self.subTest(key=key, value=value):
                    owner = json.loads(original)
                    owner[key] = value
                    metadata.write_text(json.dumps(owner))
                    with self.assertRaises(RetainedError):
                        owned_implementation(
                            repository, 1, "issue-1", self.head
                        )
                    self.assertTrue(f.worktree.exists())
            metadata.write_text(original)
            foreign = f.root / "foreign-owner.json"
            foreign.write_text(original)
            metadata.unlink()
            metadata.symlink_to(foreign)
            with self.assertRaises(RetainedError):
                owned_implementation(repository, 1, "issue-1", self.head)
            self.assertEqual(foreign.read_text(), original)

    def test_implementation_git_identity_guards(self) -> None:
        f = self.f
        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.worktree)
            registered = list_worktrees(f.repo)
            metadata = implementation_metadata(
                repository, f.worktree, "issue-1"
            )
            admin = metadata.parent
            alias = admin.parent / "symlink-admin"
            alias.symlink_to(admin, target_is_directory=True)
            for command, value in (
                ("--absolute-git-dir", str(f.root / "foreign-admin")),
                ("--git-common-dir", str(f.root / "foreign-common")),
                ("--absolute-git-dir", str(alias)),
            ):
                with self.subTest(command=command, value=value):
                    def altered(root, *arguments):
                        if command in arguments:
                            return value
                        return git_output(root, *arguments)

                    with patch(
                        "agent_squad.merging.git_output", side_effect=altered
                    ), self.assertRaisesRegex(RetainedError, "Git identity"):
                        implementation_metadata(
                            repository, f.worktree, "issue-1"
                        )
            alias.unlink()
            backlink = admin / "gitdir"
            original = backlink.read_text()
            foreign = f.root / "backlink.txt"
            foreign.write_text(original)
            try:
                with patch(
                    "agent_squad.merging.list_worktrees",
                    return_value=registered,
                ):
                    backlink.unlink()
                    backlink.symlink_to(foreign)
                    with self.assertRaisesRegex(RetainedError, "Git identity"):
                        implementation_metadata(
                            repository, f.worktree, "issue-1"
                        )
                    backlink.unlink()
                    backlink.write_text(str(f.root / "foreign/.git"))
                    with self.assertRaisesRegex(RetainedError, "Git identity"):
                        implementation_metadata(
                            repository, f.worktree, "issue-1"
                        )
            finally:
                backlink.unlink(missing_ok=True)
                backlink.write_text(original)
            with self.assertRaises(RetainedError):
                implementation_metadata(
                    replace(repository, primary=f.worktree),
                    f.worktree, "issue-1",
                )
            with self.assertRaises(RetainedError):
                implementation_metadata(repository, f.worktree, "main")
            with self.assertRaises(RetainedError):
                implementation_identity(
                    replace(repository, root=f.repo), 1, "issue-1"
                )
            self.assertTrue(f.worktree.exists())

    def test_unowned_review_worktree_is_retained_after_verified_merge(
        self,
    ) -> None:
        f = self.f
        path = f.worktree.parent / f"reviewer-pr1-{self.head[:7]}"
        f.git("worktree", "add", "--detach", str(path), self.head)
        scratch = f.repo / ".agent-squad/review-scratch/pr1"
        scratch.mkdir(parents=True)
        result = self.merge(expected=3)
        self.assertTrue(result["merged"])
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertEqual(result["cleanup"][-1]["step"], "review worktree")
        self.assertTrue(path.exists())
        self.assertTrue(scratch.exists())

    def test_scratch_symlink_is_retained_without_deleting_its_target(
        self,
    ) -> None:
        f = self.f
        scratch = f.repo / ".agent-squad/review-scratch/pr1"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        foreign = f.root / "foreign-scratch"
        foreign.mkdir()
        (foreign / "keep").write_text("retained")
        scratch.symlink_to(foreign, target_is_directory=True)
        result = self.merge(expected=3)
        self.assertEqual(result["cleanup"][-1]["step"], "scratch directory")
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertTrue(scratch.is_symlink())
        self.assertEqual((foreign / "keep").read_text(), "retained")

    def advance_base(self) -> str:
        f = self.f
        advance = f.root / "advance"
        f.git("clone", str(f.origin), str(advance))
        (advance / "base-only.txt").write_text("base changed\n")
        f.git("add", "base-only.txt", cwd=advance)
        f.git("commit", "-m", "test: advance base", cwd=advance)
        f.git("push", "origin", "main", cwd=advance)
        return f.git("rev-parse", "HEAD", cwd=advance)

    def test_issue_scratch_symlink_is_retained_with_its_target(self) -> None:
        f = self.f
        scratch = f.repo / ".agent-squad/review-scratch/issue-1"
        target = f.root / "foreign-scratch"
        target.mkdir()
        (target / "keep").write_text("retained")
        scratch.symlink_to(target, target_is_directory=True)
        result = self.merge(expected=3)
        self.assertTrue(result["merged"])
        self.assertEqual(result["cleanup"][-1]["step"],
                         "issue scratch directory")
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertIn("symlink", result["cleanup"][-1]["detail"])
        self.assertTrue(scratch.is_symlink())
        self.assertEqual((target / "keep").read_text(), "retained")

    def test_issue_scratch_containing_worktree_is_retained(self) -> None:
        f = self.f
        scratch = f.repo / ".agent-squad/review-scratch/issue-1"
        nested = scratch / "foreign-worktree"
        f.git("worktree", "add", "--detach", str(nested), self.head)
        (scratch / "keep").write_text("retained")
        result = self.merge(expected=3)
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["cleanup"][-1]["step"],
                         "issue scratch directory")
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertIn("contains a worktree", result["cleanup"][-1]["detail"])
        self.assertEqual((scratch / "keep").read_text(), "retained")
        self.assertEqual(f.git("rev-parse", "HEAD", cwd=nested), self.head)
        self.assertIn(str(nested), f.git("worktree", "list", "--porcelain"))

    def assert_removed(self) -> None:
        f = self.f
        self.assertFalse(f.worktree.exists())
        self.assertEqual(
            f.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(f.git("branch", "--list", "issue-1"), "")
        self.assertEqual(
            f.git("ls-remote", "--heads", "origin", "issue-1"), "")
        self.assertFalse((f.repo / ".agent-squad/review-scratch/pr1").exists())
        self.assertFalse(
            (f.repo / ".agent-squad/review-scratch/issue-1").exists())
        self.assertEqual(f.git("rev-parse", "HEAD"),
                         f.git("rev-parse", "origin/main"))
        self.assertEqual(f.git("status", "--porcelain"), "")

    def test_merge_verifies_ancestry_and_removes_all_owned_resources(
        self,
    ) -> None:
        f = self.f
        # Create a Reviewer resource after approval solely as cleanup residue.
        f.cli("review-worktree", "create", "--pr", "1", "--head", self.head)
        scratch = f.repo / ".agent-squad/review-scratch/pr1"
        scratch.mkdir(parents=True)
        (scratch / "probe.py").write_text("saved probe")
        issue_scratch = scratch.parent / "issue-1"
        issue_scratch.mkdir()
        (issue_scratch / "report.md").write_text("draft report")
        unrelated = f.repo / ".agent-squad/review-scratch/pr10"
        unrelated.mkdir()
        (unrelated / "keep").write_text("keep")
        result = self.merge(cwd=f.worktree)
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertNotEqual(result["merge_commit"], self.head)
        self.assertTrue(all(step["ok"] for step in result["cleanup"]))
        self.assertEqual(result["fast_forward"], {
            "result": "fast-forwarded", "from": f.base,
            "to": result["merge_commit"], "reason": None, "command": None,
        })
        self.assert_removed()
        self.assertEqual((unrelated / "keep").read_text(), "keep")
        calls = f.read_model()["calls"]
        merges = [c for c in calls if c["arguments"][1].endswith("/merge")]
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["account"], "developer")
        self.assertEqual(merges[0]["body"], {
                         "sha": self.head, "merge_method": "merge"})

    def test_squash_verifies_tree_identity_and_forge_branch_deletion(
        self,
    ) -> None:
        self.method("squash")
        self.f.settings(delete_branch_on_merge=True)
        result = self.merge()
        self.assertEqual(result["integration"], "verified by tree identity")
        self.assertIn("confirmed absent", str(result["cleanup"]))
        self.assertEqual(
            self.f.git("branch", "-r", "--list", "origin/issue-1"), "")
        self.assertFalse(any("DELETE" in c["arguments"]
                             for c in self.f.read_model()["calls"]))
        self.assert_removed()

    def merge_on_clock(self, clock: FakeClock) -> dict:
        """Merge in process so the deletion wait runs on a virtual clock."""
        model = self.f.read_model()
        model["calls"] = []
        self.f.save_model(model)
        with patch.dict(os.environ, self.f.env, clear=True):
            repository = load_initialized_repository(self.f.repo)
            with patch("agent_squad.github.time", clock):
                return merge_pr(
                    repository, GitHub(repository, "implementer"), 1
                )

    def branch_calls(self) -> tuple[int, int]:
        """Count repository reads and branch DELETEs in the fake's log."""
        calls = self.f.read_model()["calls"]
        return (
            sum(c["arguments"][1] == "/repos/MagiLand/trial"
                for c in calls if c["arguments"][:1] == ["api"]),
            sum("DELETE" in c["arguments"] for c in calls),
        )

    def assert_retained(self, result: dict, detail: str) -> None:
        self.assertEqual(result["exit_code"], 3)
        self.assertTrue(result["merged"])
        self.assertEqual(result["cleanup"][-1]["step"], "remote branch")
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertIn(detail, result["cleanup"][-1]["detail"])
        self.assertEqual(self.f.git("rev-parse", "origin/issue-1"), self.head)
        self.assertTrue(self.f.worktree.exists())
        self.assertNotEqual(self.f.git("branch", "--list", "issue-1"), "")

    def test_automatic_deletion_during_the_wait_needs_no_delete(self) -> None:
        """#120: the branch disappears on the second read of the wait."""
        self.f.settings(delete_branch_on_merge=True, auto_deletion=3)
        clock = FakeClock()
        result = self.merge_on_clock(clock)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(all(s["ok"] for s in result["cleanup"]))
        self.assertEqual(self.branch_calls(), (1, 0))
        self.assertEqual(clock.sleeps, [1.0, 1.0])
        self.assert_removed()

    def test_skipped_automatic_deletion_gets_one_delete_after_the_wait(
        self,
    ) -> None:
        self.f.settings(delete_branch_on_merge=True, auto_deletion="never")
        clock = FakeClock()
        result = self.merge_on_clock(clock)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(all(s["ok"] for s in result["cleanup"]))
        self.assertEqual(self.branch_calls(), (1, 1))
        self.assertEqual(clock.sleeps, [1.0] * 30)
        self.assert_removed()

    def test_refused_delete_after_the_wait_retains_resources(self) -> None:
        self.f.settings(delete_branch_on_merge=True, auto_deletion="never",
                        branch_delete_403=True)
        clock = FakeClock()
        result = self.merge_on_clock(clock)
        self.assert_retained(result, "branch deletion forbidden (HTTP 403)")
        self.assertEqual(self.branch_calls(), (1, 1))
        self.assertEqual(clock.sleeps, [1.0] * 30)

    def test_head_change_during_the_wait_is_refused_without_delete(
        self,
    ) -> None:
        f = self.f
        f.settings(delete_branch_on_merge=True, auto_deletion="never")
        clock = FakeClock(on_sleep=lambda: f.git(
            "update-ref", "refs/heads/issue-1", f.base, cwd=f.origin))
        result = self.merge_on_clock(clock)
        self.assert_retained(
            result, "remote branch no longer matches approved head")
        self.assertEqual(self.branch_calls(), (1, 0))
        self.assertEqual(clock.sleeps, [1.0])
        self.assertEqual(
            f.git("rev-parse", "refs/heads/issue-1", cwd=f.origin), f.base)

    def test_pr_118_race_answered_404_completes_cleanup(self) -> None:
        """#120: GitHub deleted the branch between the read and the DELETE."""
        self.f.settings(delete_branch_on_merge=False,
                        branch_removed_before_delete=True,
                        absent_delete_status=404)
        clock = FakeClock()
        result = self.merge_on_clock(clock)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(all(s["ok"] for s in result["cleanup"]))
        self.assertEqual(self.branch_calls(), (1, 1))
        self.assertEqual(clock.sleeps, [])
        self.assert_removed()

    def test_race_answered_422_without_the_setting_completes_cleanup(
        self,
    ) -> None:
        self.f.settings(branch_removed_before_delete=True)
        clock = FakeClock()
        result = self.merge_on_clock(clock)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(all(s["ok"] for s in result["cleanup"]))
        self.assertEqual(self.branch_calls(), (1, 1))
        self.assertEqual(clock.sleeps, [])
        self.assert_removed()

    def test_unreadable_setting_deletes_without_waiting(self) -> None:
        self.f.settings(delete_branch_on_merge=True, auto_deletion="never",
                        repository_denied=["developer"])
        clock = FakeClock()
        result = self.merge_on_clock(clock)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.branch_calls(), (1, 1))
        self.assertEqual(clock.sleeps, [])
        self.assert_removed()

    def test_delete_answered_422_while_present_fails_with_its_message(
        self,
    ) -> None:
        self.f.settings(branch_delete_status=422)
        clock = FakeClock()
        result = self.merge_on_clock(clock)
        self.assert_retained(result, "Reference does not exist (HTTP 422)")
        self.assertEqual(self.branch_calls(), (1, 1))
        self.assertEqual(clock.sleeps, [])

    def test_moved_base_refuses_then_merges_only_with_flag(self) -> None:
        self.advance_base()
        result = self.merge(expected=4)
        self.assertIn("base branch moved", result["error"])
        self.assertFalse(self.f.read_model()["prs"]["1"]["merged"])
        self.assertTrue(self.f.worktree.exists())
        result = self.merge("--accept-moved-base")
        self.assertTrue(result["moved_base"])
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assert_removed()

    def test_squash_on_accepted_moved_base_retains_unverifiable_resources(
        self,
    ) -> None:
        self.method("squash")
        self.advance_base()
        self.merge(expected=4)
        result = self.merge("--accept-moved-base", expected=1)
        self.assertTrue(result["merged"])
        self.assertEqual(result["integration"],
                         "integration not verifiable by tree identity")
        self.assertEqual(result["cleanup"], [])
        self.assertEqual(result["fast_forward"]["result"], "skipped")
        self.assertIn("not been verified", result["fast_forward"]["reason"])
        self.assertIsNone(result["fast_forward"]["command"])
        self.assertEqual(self.f.git("rev-parse", "HEAD"), self.f.base)
        self.assertTrue(self.f.worktree.exists())
        self.assertNotEqual(self.f.git(
            "ls-remote", "--heads", "origin", "issue-1"), "")

    def test_wrong_squash_tree_keeps_branch_worktree_and_scratch(self) -> None:
        self.method("squash")
        self.f.settings(wrong_merge_tree=True)
        scratch = self.f.repo / ".agent-squad/review-scratch/pr1"
        scratch.mkdir(parents=True)
        issue_scratch = scratch.parent / "issue-1"
        issue_scratch.mkdir()
        result = self.merge(expected=1)
        self.assertIn("tree differs", result["integration"])
        self.assertEqual(result["fast_forward"]["result"], "skipped")
        self.assertEqual(self.f.git("rev-parse", "HEAD"), self.f.base)
        self.assertTrue(scratch.exists())
        self.assertTrue(issue_scratch.exists())
        self.assertTrue(self.f.worktree.exists())
        self.assertNotEqual(self.f.git("branch", "--list", "issue-1"), "")

    def test_forge_refusal_and_head_race_leave_local_resources_unchanged(
        self,
    ) -> None:
        f = self.f
        before = f.git("worktree", "list", "--porcelain")
        for settings, message in [
            ({"merge_refusal": "Required human approval is missing"},
             "human approval"),
            ({"merge_refusal": None, "head_race": True},
             "Head branch was modified"),
            ({"head_race": False, "merge_not_confirmed": True},
             "Merge is blocked"),
        ]:
            with self.subTest(settings=settings):
                f.settings(**settings)
                result = self.merge(expected=1)
                self.assertIn(message, result["error"])
                self.assertIs(result["merged"], False)
                self.assertEqual(
                    f.git("worktree", "list", "--porcelain"), before)
                self.assertEqual(
                    f.git("status", "--porcelain", cwd=f.worktree), "")
                self.assertFalse(f.read_model()["prs"]["1"]["merged"])

    def test_cleanup_failure_reports_completed_steps_and_retains_local_work(
        self,
    ) -> None:
        (self.f.worktree / "uncommitted.txt").write_text("retain")
        result = self.merge(expected=3)
        self.assertTrue(result["merged"])
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["cleanup"][-1]["step"],
                         "implementation worktree")
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertTrue((self.f.worktree / "uncommitted.txt").exists())
        self.assertNotEqual(self.f.git("branch", "--list", "issue-1"), "")
        self.assertEqual(result["fast_forward"]["result"], "fast-forwarded")
        self.assertEqual(self.f.git("rev-parse", "HEAD"),
                         result["fast_forward"]["to"])

    def assert_fallback(self, result: dict, before: str) -> None:
        forward = result["fast_forward"]
        self.assertEqual(forward["from"], before)
        self.assertEqual(forward["to"], result["merge_commit"])
        self.assertEqual(shlex.split(forward["command"]), [
            "git", "-C", str(self.f.repo), "merge", "--ff-only",
            "--no-overwrite-ignore", result["merge_commit"],
        ])
        self.assertEqual(self.f.git("rev-parse", "HEAD"), before)

    def test_unstaged_tracked_change_skips_fast_forward(self) -> None:
        f = self.f
        (f.repo / "example.py").write_text("local change\n")
        status = f.git("status", "--porcelain")
        result = self.merge()
        self.assertEqual(result["fast_forward"]["result"], "skipped")
        self.assertIn("tracked files", result["fast_forward"]["reason"])
        self.assert_fallback(result, f.base)
        self.assertEqual(f.git("status", "--porcelain"), status)
        self.assertEqual((f.repo / "example.py").read_text(), "local change\n")

    def test_staged_tracked_change_skips_fast_forward(self) -> None:
        f = self.f
        (f.repo / "example.py").write_text("staged change\n")
        f.git("add", "example.py")
        status = f.git("status", "--porcelain")
        index = f.git("write-tree")
        result = self.merge()
        self.assertEqual(result["fast_forward"]["result"], "skipped")
        self.assertIn("tracked files", result["fast_forward"]["reason"])
        self.assert_fallback(result, f.base)
        self.assertEqual(f.git("status", "--porcelain"), status)
        self.assertEqual(f.git("write-tree"), index)
        self.assertEqual((f.repo / "example.py").read_text(),
                         "staged change\n")

    def test_another_primary_branch_skips_without_a_command(self) -> None:
        f = self.f
        f.git("checkout", "-b", "other")
        result = self.merge()
        self.assertEqual(result["fast_forward"]["result"], "skipped")
        self.assertIn("refs/heads/other", result["fast_forward"]["reason"])
        self.assertIsNone(result["fast_forward"]["command"])
        self.assertEqual(f.git("symbolic-ref", "HEAD"), "refs/heads/other")
        self.assertEqual(f.git("rev-parse", "HEAD"), f.base)
        self.assertEqual(f.git("rev-parse", "main"), f.base)
        self.assertEqual(f.git("status", "--porcelain"), "")

    def test_detached_primary_skips_without_a_command(self) -> None:
        f = self.f
        f.git("checkout", "--detach")
        result = self.merge()
        self.assertEqual(result["fast_forward"]["result"], "skipped")
        self.assertIn("detached HEAD", result["fast_forward"]["reason"])
        self.assertIsNone(result["fast_forward"]["command"])
        self.assertEqual(f.git("rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(f.git("rev-parse", "HEAD"), f.base)
        self.assertEqual(f.git("rev-parse", "main"), f.base)
        self.assertEqual(f.git("status", "--porcelain"), "")

    def test_divergent_primary_base_reports_git_refusal(self) -> None:
        f = self.f
        (f.repo / "local.txt").write_text("local commit\n")
        f.git("add", "local.txt")
        f.git("commit", "-m", "test: local divergence")
        before = f.git("rev-parse", "HEAD")
        result = self.merge()
        self.assertEqual(result["fast_forward"]["result"], "refused")
        self.assertIn("fatal:", result["fast_forward"]["reason"])
        self.assert_fallback(result, before)
        self.assertEqual((f.repo / "local.txt").read_text(), "local commit\n")
        self.assertEqual(f.git("status", "--porcelain"), "")

    def test_untracked_collision_reports_git_refusal_and_preserves_file(
        self,
    ) -> None:
        self.assert_collision_preserved()

    def test_ignored_collision_reports_git_refusal_and_preserves_file(
        self,
    ) -> None:
        self.assert_collision_preserved(ignored=True)

    def assert_collision_preserved(self, *, ignored: bool = False) -> None:
        f = self.f
        (f.worktree / "new.txt").write_text("incoming\n")
        f.git("add", "new.txt", cwd=f.worktree)
        f.git("commit", "-m", "test: add incoming file", cwd=f.worktree)
        f.git("push", "origin", "issue-1", cwd=f.worktree)
        f.review("approved")
        if ignored:
            exclude = f.repo / ".git/info/exclude"
            exclude.write_text(exclude.read_text() + "\nnew.txt\n")
        (f.repo / "new.txt").write_text("local untracked\n")
        result = self.merge()
        self.assertEqual(result["fast_forward"]["result"], "refused")
        self.assertIn("untracked", result["fast_forward"]["reason"])
        self.assertIn("new.txt", result["fast_forward"]["reason"])
        self.assert_fallback(result, f.base)
        self.assertEqual((f.repo / "new.txt").read_text(), "local untracked\n")

    def test_unrelated_untracked_file_does_not_prevent_fast_forward(
        self,
    ) -> None:
        f = self.f
        (f.repo / "keep.txt").write_text("unrelated\n")
        result = self.merge()
        self.assertEqual(result["fast_forward"]["result"], "fast-forwarded")
        self.assertEqual(f.git("rev-parse", "HEAD"), result["merge_commit"])
        self.assertEqual((f.repo / "keep.txt").read_text(), "unrelated\n")

    def test_already_current_primary_reports_up_to_date(self) -> None:
        f = self.f
        with patch.dict(os.environ, f.env, clear=True):
            result = fast_forward_primary(f.repo, "main", f.base)
        self.assertEqual(result, {
            "result": "up to date", "from": f.base, "to": f.base,
            "reason": None, "command": None,
        })
        self.assertEqual(f.git("rev-parse", "HEAD"), f.base)
        self.assertEqual(f.git("status", "--porcelain"), "")

    def test_remote_tracking_ref_change_after_verification_uses_pinned_tip(
        self,
    ) -> None:
        f = self.f

        def move_tracking_ref(*args):
            steps = cleanup_merge(*args)
            f.git("update-ref", "refs/remotes/origin/main", f.base)
            return steps

        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            with patch("agent_squad.merging.cleanup_merge",
                       side_effect=move_tracking_ref):
                result = merge_pr(
                    repository, GitHub(repository, "implementer"), 1
                )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["fast_forward"]["to"], result["merge_commit"])
        self.assertEqual(f.git("rev-parse", "HEAD"), result["merge_commit"])
        self.assertEqual(f.git("rev-parse", "origin/main"), f.base)

    def test_fast_forward_follows_cleanup_to_the_verified_tip(self) -> None:
        f = self.f
        forge_merge = GitHub.merge
        tips: list[str] = []
        heads: list[str] = []

        def merge_then_advance_base(forge, *args):
            merged = forge_merge(forge, *args)
            tips.append(self.advance_base())
            return merged

        def record_head(*args):
            heads.append(f.git("rev-parse", "HEAD"))
            return cleanup_merge(*args)

        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            with patch.object(GitHub, "merge", merge_then_advance_base):
                with patch("agent_squad.merging.cleanup_merge",
                           side_effect=record_head):
                    result = merge_pr(
                        repository, GitHub(repository, "implementer"), 1
                    )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(heads, [f.base])
        self.assertNotEqual(tips[0], result["merge_commit"])
        self.assertEqual(result["fast_forward"]["to"], tips[0])
        self.assertEqual(f.git("rev-parse", "HEAD"), tips[0])

    def test_retargeted_pr_does_not_update_its_checked_out_base(self) -> None:
        self.assert_retargeted_base_skips("release")

    def test_retargeted_pr_does_not_update_the_configured_base(self) -> None:
        self.assert_retargeted_base_skips("main")

    def assert_retargeted_base_skips(self, checkout: str) -> None:
        f = self.f
        f.git("branch", "release", f.base)
        f.git("push", "origin", "release")
        f.git("checkout", checkout)
        model = f.read_model()
        model["prs"]["1"]["base"] = {"ref": "release", "sha": f.base}
        f.save_model(model)
        result = self.merge()
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["fast_forward"], {
            "result": "skipped", "from": f.base,
            "to": result["merge_commit"],
            "reason": "PR base branch release differs from "
                      "configured base branch main",
            "command": None,
        })
        self.assertEqual(f.git("symbolic-ref", "HEAD"),
                         f"refs/heads/{checkout}")
        self.assertEqual(f.git("rev-parse", "HEAD"), f.base)
        self.assertEqual(f.git("rev-parse", "main"), f.base)
        self.assertEqual(f.git("rev-parse", "release"), f.base)
        self.assertEqual(f.git("status", "--porcelain"), "")

    def test_merge_timeout_after_update_reports_fast_forwarded(self) -> None:
        f = self.f

        def timeout_after_merge(root, *arguments):
            completed = run_git(root, *arguments)
            if arguments[:1] == ("merge",):
                raise AgentSquadError("git merge timed out after update")
            return completed

        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            with patch("agent_squad.merging.run_git",
                       side_effect=timeout_after_merge):
                result = merge_pr(
                    repository, GitHub(repository, "implementer"), 1
                )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["fast_forward"], {
            "result": "fast-forwarded", "from": f.base,
            "to": result["merge_commit"], "command": None,
            "reason": "git merge timed out after update",
        })
        self.assert_removed()

    def test_merge_timeout_before_update_reports_refused(self) -> None:
        f = self.f

        def timeout_before_merge(root, *arguments):
            if arguments[:1] == ("merge",):
                raise AgentSquadError("git merge timed out before update")
            return run_git(root, *arguments)

        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            with patch("agent_squad.merging.run_git",
                       side_effect=timeout_before_merge):
                result = merge_pr(
                    repository, GitHub(repository, "implementer"), 1
                )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["fast_forward"]["result"], "refused")
        self.assertEqual(result["fast_forward"]["reason"],
                         "git merge timed out before update")
        self.assert_fallback(result, f.base)

    def test_post_merge_head_failure_recovers_completed_fast_forward(
        self,
    ) -> None:
        f = self.f
        head_reads = 0

        def fail_head_once(root, *arguments):
            nonlocal head_reads
            if arguments == ("rev-parse", "HEAD"):
                head_reads += 1
                if head_reads == 2:
                    raise AgentSquadError("cannot read HEAD after merge")
            return git_output(root, *arguments)

        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            with patch("agent_squad.merging.git_output",
                       side_effect=fail_head_once):
                result = merge_pr(
                    repository, GitHub(repository, "implementer"), 1
                )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(head_reads, 3)
        self.assertEqual(result["fast_forward"], {
            "result": "fast-forwarded", "from": f.base,
            "to": result["merge_commit"], "command": None,
            "reason": "cannot read HEAD after merge",
        })
        self.assert_removed()

    def test_interrupted_merge_with_unreadable_head_reports_refused(
        self,
    ) -> None:
        f = self.f
        attempted = False

        def interrupt(root, *arguments):
            nonlocal attempted
            if arguments[:1] == ("merge",):
                attempted = True
                raise AgentSquadError("git merge interrupted")
            return run_git(root, *arguments)

        def fail_recovery_read(root, *arguments):
            if attempted and arguments == ("rev-parse", "HEAD"):
                raise AgentSquadError("cannot read HEAD during recovery")
            return git_output(root, *arguments)

        with patch.dict(os.environ, f.env, clear=True):
            with patch("agent_squad.merging.run_git", side_effect=interrupt):
                with patch("agent_squad.merging.git_output",
                           side_effect=fail_recovery_read):
                    result = fast_forward_primary(f.repo, "main", self.head)
        self.assertEqual(result["result"], "refused")
        self.assertEqual(result["reason"], "git merge interrupted")
        self.assertEqual(result["from"], f.base)
        self.assertEqual(result["to"], self.head)
        self.assertEqual(f.git("rev-parse", "HEAD"), f.base)
        self.assertEqual(shlex.split(result["command"])[-1], self.head)

    def test_interrupted_already_current_merge_reports_up_to_date(
        self,
    ) -> None:
        f = self.f

        def interrupt(root, *arguments):
            if arguments[:1] == ("merge",):
                raise AgentSquadError("git merge interrupted")
            return run_git(root, *arguments)

        with patch.dict(os.environ, f.env, clear=True):
            with patch("agent_squad.merging.run_git", side_effect=interrupt):
                result = fast_forward_primary(f.repo, "main", f.base)
        self.assertEqual(result, {
            "result": "up to date", "from": f.base, "to": f.base,
            "reason": "git merge interrupted", "command": None,
        })

    def test_cleanup_inventory_error_still_attempts_fast_forward(self) -> None:
        f = self.f
        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            with patch("agent_squad.merging.cleanup_merge",
                       side_effect=AgentSquadError("inventory failed")):
                result = merge_pr(
                    repository, GitHub(repository, "implementer"), 1
                )
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["cleanup"][0]["detail"], "inventory failed")
        self.assertEqual(result["fast_forward"]["result"], "fast-forwarded")
        self.assertEqual(f.git("rev-parse", "HEAD"), result["merge_commit"])
        self.assertTrue(f.worktree.exists())

    def test_unreadable_tracked_status_skips_without_changing_merge_exit(
        self,
    ) -> None:
        f = self.f

        def fail_status(root, *arguments):
            if arguments[:1] == ("status",):
                raise AgentSquadError("cannot inspect tracked files")
            return git_output(root, *arguments)

        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            with patch("agent_squad.merging.git_output",
                       side_effect=fail_status):
                result = merge_pr(
                    repository, GitHub(repository, "implementer"), 1
                )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["fast_forward"]["result"], "skipped")
        self.assertEqual(result["fast_forward"]["reason"],
                         "cannot inspect tracked files")
        self.assert_fallback(result, f.base)

    def test_missing_implementation_ownership_never_deletes_by_path_alone(
        self,
    ) -> None:
        """#121: unproven ownership leaves no issue for the merge record."""
        admin = Path(self.f.git(
            "rev-parse", "--absolute-git-dir", cwd=self.f.worktree))
        (admin / "agent-squad-implementation.json").unlink()
        result = self.merge(expected=1)
        self.assertIs(result["merged"], False)
        self.assertIn("no merge request sent", result["error"])
        self.assertIn("cannot prove implementation ownership",
                      result["error"])
        self.assertEqual(result["merge_record"]["result"], "not written")
        self.assertIsNone(result["cleanup_command"])
        self.assertFalse(Path(result["merge_record"]["path"]).exists())
        self.assertFalse(any(c["arguments"][1].endswith("/merge")
                             for c in self.f.read_model()["calls"]))
        self.assertFalse(self.f.read_model()["prs"]["1"]["merged"])
        self.assertTrue(self.f.worktree.exists())
        self.assertNotEqual(self.f.git(
            "ls-remote", "--heads", "origin", "issue-1"), "")

    def test_visible_rules_are_reported_and_unexposed_plan_is_not_an_error(
        self,
    ) -> None:
        f = self.f
        f.settings(
            protection={"required_pull_request_reviews": {
                "required_approving_review_count": 2}},
            rules=[{"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": True}}],
            merge_refusal="Required checks have not passed",
        )
        result = self.merge(expected=1)
        self.assertIn("required_pull_request_reviews",
                      result["branch_rules"]["protection"])
        self.assertEqual(result["mergeable_state"], "clean")
        f.settings(rules_status=403,
                   rules_message="Upgrade to GitHub Pro", merge_refusal=None)
        result = self.merge()
        self.assertEqual(result["branch_rules"]["rules"], {
                         "visibility": "no rule visible"})
        self.assert_removed()

    def test_rule_authentication_or_rate_limit_failure_is_not_hidden(
        self,
    ) -> None:
        self.f.settings(rules_status=403,
                        rules_message="API rate limit exceeded")
        result = self.merge(expected=1)
        self.assertIn("rate limit", result["error"])
        self.assertFalse(self.f.read_model()["prs"]["1"]["merged"])

    def test_new_stop_task_amendment_or_head_invalidates_merge(self) -> None:
        f = self.f
        model = f.read_model()
        review = model["prs"]["1"]["reviews"][0]
        review["state"] = "COMMENTED"
        f.save_model(model)
        self.assertIn("forge review state", self.merge(expected=4)["error"])
        review["state"] = "APPROVED"
        f.save_model(model)
        f.decision(task="## Task\n\nAmended Task.\n")
        self.assertIn("Task was amended", self.merge(expected=4)["error"])
        f.review("approved")
        f.cli(
            "stop", "post", "--as", "reviewer", "--pr", "1",
            "--head", self.head, "--reason", "design", "--body",
            f.write("stopped.md", "Stop for design"),
        )
        self.assertIn("STOPPED", self.merge(expected=4)["error"])
        f.push("value = 20\nsecond = 2\nthird = 3\n")
        self.assertIn("not at the PR head", self.merge(expected=4)["error"])
        self.assertFalse(f.read_model()["prs"]["1"]["merged"])


class ResumedCleanupTests(unittest.TestCase):
    """#121: pr cleanup finishes a merge that pr merge started."""

    COMMAND = "agent-squad pr cleanup --as implementer --pr 1"

    def setUp(self) -> None:
        self.f = ForgeFixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()
        self.f.review("approved")
        self.record = self.f.repo / ".git/agent-squad-merge-pr1.json"

    def pr(self, command: str, expected: int = 0) -> dict:
        return run_pr(self, self.f, command, expected)

    def merge(self, expected: int = 0) -> dict:
        return self.pr("merge", expected)

    def merge_calls(self) -> int:
        return sum(c["arguments"][1].endswith("/merge")
                   for c in self.f.read_model()["calls"])

    def writes(self) -> list:
        return [
            c for c in self.f.read_model()["calls"]
            if "--method" in c["arguments"]
            and c["arguments"][c["arguments"].index("--method") + 1] != "GET"
        ]

    def cleanup(self, expected: int = 0) -> dict:
        merges = self.merge_calls()
        result = self.pr("cleanup", expected)
        self.assertEqual(self.merge_calls(), merges)
        return result

    def assert_incomplete(self, result: dict, step: str) -> None:
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["cleanup"][-1]["step"], step)
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertEqual(result["merge_record"]["result"], "kept")
        self.assertEqual(result["cleanup_command"], self.COMMAND)
        self.assertTrue(self.record.exists())

    def assert_finished(self, result: dict) -> None:
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(all(s["ok"] for s in result["cleanup"]))
        self.assertEqual(result["merge_record"]["result"], "deleted")
        self.assertIsNone(result["cleanup_command"])
        self.assertFalse(self.record.exists())
        MergeTests.assert_removed(self)

    def assert_unchanged(self, worktrees: str, writes: int) -> None:
        self.assertEqual(
            self.f.git("worktree", "list", "--porcelain"), worktrees)
        self.assertEqual(len(self.writes()), writes)
        self.assertEqual(
            self.f.git("rev-parse", "refs/heads/issue-1"), self.head)

    def stop_at_remote_branch(self) -> None:
        self.f.settings(branch_delete_403=True)
        self.assert_incomplete(self.merge(expected=3), "remote branch")
        self.f.settings(branch_delete_403=False)

    def test_record_keeps_premerge_identities_until_cleanup_finishes(
        self,
    ) -> None:
        f = self.f
        f.settings(branch_delete_403=True)
        self.assert_incomplete(self.merge(expected=3), "remote branch")
        self.assertEqual(json.loads(self.record.read_text()), {
            "schema_version": 1, "common": str(f.repo / ".git"), "pr": 1,
            "head": self.head, "head_branch": "issue-1",
            "base_branch": "main", "merge_method": "merge",
            "moved_base": False, "issue": 1,
        })
        refused = self.merge(expected=4)
        self.assertIn("PR is not open", refused["error"])
        self.assertIn(self.COMMAND, refused["error"])
        f.settings(branch_delete_403=False)
        result = self.cleanup()
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["head"], self.head)
        self.assertEqual(result["merge_commit"],
                         f.read_model()["prs"]["1"]["merge_commit_sha"])
        self.assertEqual(result["fast_forward"]["result"], "up to date")
        self.assert_finished(result)
        self.assertNotIn("pr cleanup", self.merge(expected=4)["error"])

    def test_resumes_after_untracked_file_blocked_the_worktree(self) -> None:
        f = self.f
        (f.worktree / "untracked.txt").write_text("retain")
        self.assert_incomplete(
            self.merge(expected=3), "implementation worktree")
        (f.worktree / "untracked.txt").unlink()
        result = self.cleanup()
        self.assertEqual(
            [s["detail"] for s in result["cleanup"][:5]],
            [str(f.worktree), "confirmed absent: issue-1", "already absent",
             str(f.worktree), "issue-1"],
        )
        self.assert_finished(result)

    def test_resumes_after_the_ownership_record_was_removed(self) -> None:
        """The triage probe: a PR scratch symlink after branch removal."""
        f = self.f
        root = f.repo / ".agent-squad/review-scratch"
        scratch, issue_scratch = root / "pr1", root / "issue-1"
        issue_scratch.mkdir(parents=True)
        (issue_scratch / "report.md").write_text("draft")
        target = f.root / "foreign-scratch"
        target.mkdir()
        scratch.symlink_to(target, target_is_directory=True)
        self.assert_incomplete(self.merge(expected=3), "scratch directory")
        self.assertFalse(f.worktree.exists())
        self.assertEqual(f.git("branch", "--list", "issue-1"), "")
        self.assertTrue(issue_scratch.exists())
        scratch.unlink()
        result = self.cleanup()
        self.assertEqual([s["detail"] for s in result["cleanup"]], [
            "already removed", "confirmed absent: issue-1",
            "already absent", "already absent", "already absent",
            str(scratch), str(issue_scratch),
        ])
        self.assertTrue(target.exists())
        self.assert_finished(result)

    def test_resumes_after_herdr_refused_the_reviewer_cleanup(self) -> None:
        f = self.f
        review = f.cli(
            "review-worktree", "create", "--pr", "1", "--head", self.head)
        f.herdr_settings(snapshot_failure=True)
        self.assert_incomplete(self.merge(expected=3), "review worktree")
        self.assertTrue(Path(review["path"]).exists())
        f.herdr_settings(snapshot_failure=False)
        result = self.cleanup()
        self.assertEqual(result["cleanup"][5]["step"], "review worktree")
        self.assertFalse(Path(review["path"]).exists())
        self.assert_finished(result)

    def test_resumes_after_the_merge_answer_was_lost(self) -> None:
        f = self.f
        f.settings(merge_response_lost="after")
        result = self.merge(expected=1)
        self.assertIsNone(result["merged"])
        self.assertEqual(result["cleanup_command"], self.COMMAND)
        self.assertEqual(result["merge_record"]["result"], "kept")
        self.assertTrue(f.read_model()["prs"]["1"]["merged"])
        self.assertTrue(f.worktree.exists())
        self.assertEqual(f.git("rev-parse", "HEAD"), f.base)
        result = self.cleanup()
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["fast_forward"]["result"], "fast-forwarded")
        self.assertEqual(result["fast_forward"]["to"], result["merge_commit"])
        self.assert_finished(result)

    def test_resumes_after_the_fetch_failed(self) -> None:
        f = self.f

        def fail_fetch(root, *arguments):
            if arguments[:1] == ("fetch",):
                raise AgentSquadError("fetch failed")
            return git_output(root, *arguments)

        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            with patch("agent_squad.merging.git_output",
                       side_effect=fail_fetch):
                result = merge_pr(
                    repository, GitHub(repository, "implementer"), 1
                )
        self.assertEqual(result["exit_code"], 1)
        self.assertIs(result["merged"], True)
        self.assertEqual(result["integration"], "fetch failed")
        self.assertEqual(result["cleanup"], [])
        self.assertEqual(result["cleanup_command"], self.COMMAND)
        self.assertTrue(self.record.exists())
        self.assertTrue(f.worktree.exists())
        result = self.cleanup()
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["fast_forward"]["result"], "fast-forwarded")
        self.assert_finished(result)

    def test_missing_record_refuses_without_changes(self) -> None:
        worktrees = self.f.git("worktree", "list", "--porcelain")
        writes = len(self.writes())
        result = self.cleanup(expected=4)
        self.assertIn("only finishes a merge started by pr merge",
                      result["error"])
        self.assert_unchanged(worktrees, writes)
        self.assertFalse(self.f.read_model()["prs"]["1"]["merged"])

    def test_open_pr_refuses_and_a_later_merge_replaces_the_record(
        self,
    ) -> None:
        f = self.f
        f.settings(merge_response_lost="before")
        result = self.merge(expected=1)
        self.assertIsNone(result["merged"])
        self.assertEqual(result["cleanup_command"], self.COMMAND)
        stale = self.record.read_text()
        worktrees = f.git("worktree", "list", "--porcelain")
        writes = len(self.writes())
        refused = self.cleanup(expected=4)
        self.assertIn("PR #1 is not merged; merge record kept",
                      refused["error"])
        self.assertEqual(self.record.read_text(), stale)
        self.assert_unchanged(worktrees, writes)
        f.settings(merge_response_lost=None)
        self.assertEqual(self.merge()["merge_record"]["result"], "deleted")
        self.assertFalse(self.record.exists())

    def test_failed_integration_retains_resources_and_record(self) -> None:
        f = self.f
        MergeTests.method(self, "squash")
        f.settings(wrong_merge_tree=True)
        result = self.merge(expected=1)
        self.assertIn("tree differs", result["integration"])
        self.assertEqual(result["cleanup_command"], self.COMMAND)
        worktrees = f.git("worktree", "list", "--porcelain")
        writes = len(self.writes())
        result = self.cleanup(expected=1)
        self.assertIn("tree differs", result["integration"])
        self.assertEqual(result["cleanup"], [])
        self.assertTrue(result["resources_retained"])
        self.assertEqual(result["fast_forward"]["result"], "skipped")
        self.assertEqual(result["merge_record"]["result"], "kept")
        self.assertTrue(self.record.exists())
        self.assert_unchanged(worktrees, writes)
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), self.head)
        self.assertEqual(f.git("rev-parse", "HEAD"), f.base)

    def test_resumed_run_retains_a_moved_tracking_ref(self) -> None:
        f = self.f
        self.stop_at_remote_branch()
        f.git("update-ref", "refs/remotes/origin/issue-1", f.base)
        self.assert_incomplete(self.cleanup(expected=3),
                               "remote-tracking ref")
        self.assertEqual(f.git("rev-parse", "origin/issue-1"), f.base)
        self.assertTrue(f.worktree.exists())

    def test_resumed_run_retains_a_moved_local_branch(self) -> None:
        f = self.f
        self.stop_at_remote_branch()
        f.git("worktree", "remove", str(f.worktree))
        f.git("branch", "-f", "issue-1", f.base)
        self.assert_incomplete(self.cleanup(expected=3), "local branch")
        self.assertEqual(f.git("rev-parse", "refs/heads/issue-1"), f.base)

    def test_resumed_run_retains_an_unowned_implementation_worktree(
        self,
    ) -> None:
        f = self.f
        self.stop_at_remote_branch()
        f.git("worktree", "remove", str(f.worktree))
        unowned = f.root / "unowned"
        f.git("worktree", "add", str(unowned), "issue-1")
        result = self.cleanup(expected=3)
        self.assert_incomplete(result, "implementation ownership")
        self.assertEqual(len(result["cleanup"]), 1)
        self.assertTrue(unowned.exists())
        self.assertEqual(f.git("rev-parse", "refs/heads/issue-1"), self.head)

    def test_resumed_run_retains_a_detached_or_switched_worktree(
        self,
    ) -> None:
        """PR #134 REV-1: the issue path still holds a registered worktree."""
        f = self.f
        self.stop_at_remote_branch()
        writes = len(self.writes())
        for args in (("checkout", "--detach"), ("switch", "-c", "other")):
            with self.subTest(args=args):
                f.git(*args, cwd=f.worktree)
                result = self.cleanup(expected=3)
                self.assert_incomplete(result, "implementation ownership")
                self.assertEqual(len(result["cleanup"]), 1)
                self.assertTrue(f.worktree.exists())
                self.assertEqual(
                    f.git("rev-parse", "refs/heads/issue-1"), self.head)
                self.assertEqual(len(self.writes()), writes)
                f.git("switch", "issue-1", cwd=f.worktree)
        self.assert_finished(self.cleanup())

    def test_resumed_run_retains_an_unowned_review_worktree(self) -> None:
        f = self.f
        self.stop_at_remote_branch()
        path = f.worktree.parent / f"reviewer-pr1-{self.head[:7]}"
        f.git("worktree", "add", "--detach", str(path), self.head)
        self.assert_incomplete(self.cleanup(expected=3), "review worktree")
        self.assertTrue(path.exists())

    def test_resumed_run_retains_a_scratch_symlink(self) -> None:
        f = self.f
        self.stop_at_remote_branch()
        scratch = f.repo / ".agent-squad/review-scratch/pr1"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        target = f.root / "foreign-scratch"
        target.mkdir()
        (target / "keep").write_text("retained")
        scratch.symlink_to(target, target_is_directory=True)
        self.assert_incomplete(self.cleanup(expected=3), "scratch directory")
        self.assertTrue(scratch.is_symlink())
        self.assertEqual((target / "keep").read_text(), "retained")

    def test_unusable_record_is_refused_before_any_forge_call(self) -> None:
        f = self.f
        self.stop_at_remote_branch()
        original = json.loads(self.record.read_text())
        foreign = f.root / "foreign-record.json"
        foreign.write_text(json.dumps(original))
        variants = {
            "not JSON": "{",
            "not an object": "[]",
            "missing field": {
                k: v for k, v in original.items() if k != "issue"},
            "extra field": dict(original, extra=1),
        }
        for key, value in (
            ("schema_version", 2), ("schema_version", True),
            ("common", str(f.origin)), ("common", None), ("pr", 2),
            ("pr", True),
            ("head", self.head[:7]), ("head", self.head.upper()),
            ("head_branch", ""), ("head_branch", "a..b"),
            ("base_branch", "main.lock"), ("merge_method", "rebase"),
            ("moved_base", "false"), ("issue", 0), ("issue", True),
        ):
            variants[f"{key}={value!r}"] = dict(original, **{key: value})
        worktrees = f.git("worktree", "list", "--porcelain")
        for name, value in [("symlink", None), *variants.items()]:
            with self.subTest(record=name):
                self.record.unlink()
                if value is None:
                    self.record.symlink_to(foreign)
                else:
                    self.record.write_text(
                        value if isinstance(value, str)
                        else json.dumps(value))
                before = os.readlink(self.record) if value is None else (
                    self.record.read_text())
                calls = len(f.read_model()["calls"])
                result = self.cleanup(expected=1)
                self.assertIn("pr cleanup refused", result["error"])
                self.assertEqual(len(f.read_model()["calls"]), calls)
                self.assertEqual(
                    f.git("worktree", "list", "--porcelain"), worktrees)
                self.assertEqual(
                    os.readlink(self.record) if value is None
                    else self.record.read_text(), before)
        self.assertEqual(json.loads(foreign.read_text()), original)
        self.record.unlink()
        self.record.write_text(json.dumps(original))
        self.assert_finished(self.cleanup())

    def test_refused_merge_and_complete_cleanup_leave_no_record(self) -> None:
        f = self.f
        f.settings(merge_refusal="Required human approval is missing")
        result = self.merge(expected=1)
        self.assertIs(result["merged"], False)
        self.assertEqual(result["merge_record"]["result"], "deleted")
        self.assertIsNone(result["cleanup_command"])
        self.assertFalse(self.record.exists())
        f.settings(merge_refusal=None)
        self.assert_finished(self.merge())

    def test_unwritable_record_sends_no_merge_request(self) -> None:
        f = self.f
        self.record.mkdir()
        result = self.merge(expected=1)
        self.assertIs(result["merged"], False)
        self.assertIn("no merge request sent", result["error"])
        self.assertEqual(result["merge_record"]["result"], "not written")
        self.assertEqual(self.merge_calls(), 0)
        self.assertFalse(f.read_model()["prs"]["1"]["merged"])
        self.assertEqual(list(self.record.parent.glob(
            f".{self.record.name}.*")), [])
        self.assertTrue(f.worktree.exists())

    def test_merge_replaces_an_earlier_record_without_following_it(
        self,
    ) -> None:
        f = self.f
        foreign = f.root / "foreign-record.json"
        foreign.write_text("keep")
        self.record.symlink_to(foreign)
        self.assert_finished(self.merge())
        self.assertEqual(foreign.read_text(), "keep")
