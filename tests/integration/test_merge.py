"""Human-invoked merges against real Git history behind the fake forge."""

import json
import os
from dataclasses import replace
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

from tests.forge_support import ForgeFixture
from agent_squad.forge import GitHub
from agent_squad.initialization import (
    AgentSquadError, RetainedError, git_output, list_worktrees,
    load_initialized_repository, run_git,
)
from agent_squad.merging import (
    cleanup_merge, fast_forward_primary, merge_pr,
    implementation_identity, implementation_metadata, owned_implementation,
)


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ForgeFixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()
        self.f.review("approved")

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
        self.assertIn("already removed by forge", str(result["cleanup"]))
        self.assert_removed()

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
                self.assertFalse(result["merged"])
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
        admin = Path(self.f.git(
            "rev-parse", "--absolute-git-dir", cwd=self.f.worktree))
        (admin / "agent-squad-implementation.json").unlink()
        result = self.merge(expected=3)
        self.assertEqual(result["cleanup"][-1]["step"],
                         "implementation ownership")
        self.assertFalse(result["cleanup"][-1]["ok"])
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
