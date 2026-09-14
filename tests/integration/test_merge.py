"""Human-invoked merges against real Git history behind the fake forge."""

import json
import os
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests.forge_support import ForgeFixture
from agent_squad.initialization import (
    RetainedError, git_output, list_worktrees, load_initialized_repository,
)
from agent_squad.merging import (
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

    def assert_removed(self) -> None:
        f = self.f
        self.assertFalse(f.worktree.exists())
        self.assertEqual(
            f.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(f.git("branch", "--list", "issue-1"), "")
        self.assertEqual(
            f.git("ls-remote", "--heads", "origin", "issue-1"), "")
        self.assertFalse((f.repo / ".agent-squad/review-scratch/pr1").exists())
        self.assertEqual(f.git("rev-parse", "HEAD"), f.base)
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
        unrelated = f.repo / ".agent-squad/review-scratch/pr10"
        unrelated.mkdir()
        (unrelated / "keep").write_text("keep")
        result = self.merge(cwd=f.worktree)
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertNotEqual(result["merge_commit"], self.head)
        self.assertTrue(all(step["ok"] for step in result["cleanup"]))
        self.assertIn("--ff-only", result["fast_forward_command"])
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
        self.assertTrue(self.f.worktree.exists())
        self.assertNotEqual(self.f.git(
            "ls-remote", "--heads", "origin", "issue-1"), "")

    def test_wrong_squash_tree_keeps_branch_worktree_and_scratch(self) -> None:
        self.method("squash")
        self.f.settings(wrong_merge_tree=True)
        scratch = self.f.repo / ".agent-squad/review-scratch/pr1"
        scratch.mkdir(parents=True)
        result = self.merge(expected=1)
        self.assertIn("tree differs", result["integration"])
        self.assertTrue(scratch.exists())
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
