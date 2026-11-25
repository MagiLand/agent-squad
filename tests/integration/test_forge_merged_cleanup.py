"""#175: pr cleanup finishes a PR that the forge merged without pr merge."""

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests.forge_support import (
    ForgeFixture, ForgejoFixture, finding, is_write,
)
from agent_squad.forge import make_forge
from agent_squad.initialization import load_initialized_repository
from agent_squad.merging import cleanup_pr

COMMAND = "agent-squad pr cleanup --as implementer --pr 1"


def call_path(call: dict) -> str:
    """The API path of one fake-forge call: an HTTP request or a gh run."""
    if "path" in call:
        return call["path"]
    arguments = call["arguments"]
    return arguments[1] if len(arguments) > 1 else ""


class ForgeMergedCleanup:
    """Cases run on each fake forge; a subclass names the fixture."""

    fixture: type[ForgeFixture] = ForgeFixture
    # Forgejo's merge request asks the forge to delete the head branch.
    forge_deletes_branch = False

    def setUp(self) -> None:
        self.f = self.fixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()
        self.record = self.f.repo / ".git/agent-squad-merge-pr1.json"
        root = self.f.repo / ".agent-squad/review-scratch"
        self.scratch, self.issue_scratch = root / "pr1", root / "issue-1"
        for directory in (self.scratch, self.issue_scratch):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "probe.txt").write_text("scratch")

    def configure(self, method: str) -> None:
        path = self.f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config["merge_method"] = method
        path.write_text(json.dumps(config))

    def forge_merge(self, method: str, head: str | None = None) -> str:
        """Merge on the forge, as a merge train would, with no pr merge."""
        with patch.dict(os.environ, self.f.env, clear=True):
            repository = load_initialized_repository(self.f.repo)
            merged = make_forge(repository, "implementer").merge(
                1, head or self.head, method)
        return merged["sha"]

    def edit_pr(self, **values: object) -> None:
        """Change the fake forge's record of PR #1 directly."""
        model = self.f.read_model()
        pr = model["prs"]["1"]
        for key, value in values.items():
            if key in ("head_sha", "head_ref", "base_ref"):
                side, field = key.split("_")
                pr[side]["sha" if field == "sha" else "ref"] = value
            else:
                pr[key] = value
        self.f.save_model(model)

    def clone(self, name: str) -> Path:
        path = self.f.root / name
        self.f.git("clone", "--quiet", str(self.f.origin), str(path))
        return path

    def land_on_main(self, *commits: str) -> str:
        """Put commits on the remote base as a forge would, then mark PR #1
        merged at the last one."""
        f = self.f
        other = self.clone("lander")
        for commit in commits:
            f.git("cherry-pick", "--allow-empty", commit, cwd=other)
            # A new message keeps each rebased SHA distinct even when the
            # pick lands within the same second on the same parent.
            f.git("commit", "--amend", "--allow-empty", "-m",
                  f"test: rebased {commit}", cwd=other)
        f.git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=other)
        merge_commit = f.git("rev-parse", "HEAD", cwd=other)
        self.edit_pr(merged=True, state="closed",
                     merge_commit_sha=merge_commit)
        return merge_commit

    def merge_requests(self) -> int:
        return sum(call_path(c).endswith("/merge")
                   for c in self.f.read_model()["calls"])

    def writes(self) -> int:
        return sum(map(is_write, self.f.read_model()["calls"]))

    def pr(self, command: str, expected: int) -> dict:
        result = self.f.run([
            sys.executable, "-m", "agent_squad", "pr", command,
            "--as", "implementer", "--pr", "1", "--json",
        ], cwd=self.f.repo)
        self.assertEqual(result.returncode, expected,
                         result.stdout + result.stderr)
        return (
            json.loads(result.stdout) if result.stdout
            else {"error": result.stderr}
        )

    def cleanup(self, expected: int = 0) -> dict:
        merges = self.merge_requests()
        result = self.pr("cleanup", expected)
        self.assertEqual(self.merge_requests(), merges)
        return result

    def assert_finished(
        self, result: dict, merge_commit: str,
        fast_forward: str = "fast-forwarded",
    ) -> None:
        f = self.f
        self.assertEqual(result["exit_code"], 0)
        self.assertIs(result["merged"], True)
        self.assertIs(result["merged_outside_pr_merge"], True)
        self.assertEqual(result["head"], self.head)
        self.assertEqual(result["merge_commit"], merge_commit)
        self.assertIs(result["moved_base"], False)
        self.assertTrue(all(s["ok"] for s in result["cleanup"]),
                        result["cleanup"])
        self.assertEqual(result["merge_record"]["result"], "deleted")
        self.assertIsNone(result["cleanup_command"])
        self.assertIsNone(result["merge_answer"])
        self.assertFalse(self.record.exists())
        self.assertEqual(result["fast_forward"]["result"], fast_forward)
        self.assertEqual(result["fast_forward"]["to"], merge_commit)
        self.assertFalse(f.worktree.exists())
        self.assertEqual(
            f.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(f.git("branch", "--list", "issue-1"), "")
        self.assertEqual(f.git("branch", "-r", "--list", "origin/issue-1"), "")
        self.assertEqual(
            f.git("ls-remote", "--heads", "origin", "issue-1"), "")
        self.assertFalse(self.scratch.exists())
        self.assertFalse(self.issue_scratch.exists())
        self.assertEqual(f.git("rev-parse", "HEAD"), merge_commit)
        self.assertEqual(f.git("status", "--porcelain"), "")

    def assert_retained(self, result: dict) -> None:
        self.assertFalse(self.record.exists())
        self.assertIsNone(result["merged_head"])
        self.assert_retained_unrecorded(result)

    def assert_retained_unrecorded(self, result: dict) -> None:
        f = self.f
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["cleanup"], [])
        self.assertTrue(result["resources_retained"])
        self.assertEqual(result["cleanup_command"], COMMAND)
        self.assertEqual(result["fast_forward"]["result"], "skipped")
        self.assertEqual(result["merge_record"]["result"], "not written")
        self.assertNotEqual(
            f.git("ls-remote", "--heads", "origin", "issue-1")
            if not self.forge_deletes_branch else "kept", "")
        self.assertTrue(f.worktree.exists())
        self.assertNotEqual(f.git("branch", "--list", "issue-1"), "")
        self.assertTrue(self.scratch.exists())
        self.assertTrue(self.issue_scratch.exists())
        self.assertEqual(f.git("rev-parse", "HEAD"), f.base)

    def test_merge_commit_is_cleaned_up_and_flagged(self) -> None:
        # The configured method is not the one the forge used.
        self.configure("squash")
        merge_commit = self.forge_merge("merge")
        result = self.cleanup()
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["merge_method"], "merge")
        self.assertIsNone(result["approved_head"])
        self.assertIs(result["closes_issue"], True)
        self.assert_finished(result, merge_commit)

    def test_squash_merge_is_cleaned_up_and_flagged(self) -> None:
        self.configure("merge")
        merge_commit = self.forge_merge("squash")
        result = self.cleanup()
        self.assertEqual(result["integration"], "verified by tree identity")
        self.assertEqual(result["merge_method"], "squash")
        self.assertIsNone(result["approved_head"])
        self.assert_finished(result, merge_commit)

    def test_approved_head_names_the_approving_review(self) -> None:
        self.f.review("approved")
        merge_commit = self.forge_merge("merge")
        result = self.cleanup()
        self.assertEqual(result["approved_head"], self.head)
        self.assert_finished(result, merge_commit)

    def test_approved_head_can_be_older_than_the_merged_head(self) -> None:
        f = self.f
        f.review("approved")
        approved = self.head
        self.head = f.push("value = 1\nsecond = 2\nthird = 3\nlater = 4\n")
        merge_commit = self.forge_merge("merge")
        result = self.cleanup()
        self.assertEqual(result["approved_head"], approved)
        self.assertNotEqual(result["approved_head"], result["head"])
        self.assert_finished(result, merge_commit)

    def test_latest_of_several_approvals_is_the_approved_head(self) -> None:
        f = self.f
        f.review("approved")
        second = f.push("value = 1\nsecond = 2\nthird = 3\nmore = 4\n")
        f.review("approved")
        self.head = f.push(
            "value = 1\nsecond = 2\nthird = 3\nmore = 4\nlast = 5\n")
        f.review("changes_requested", [finding()])
        merge_commit = self.forge_merge("merge")
        result = self.cleanup()
        self.assertEqual(result["approved_head"], second)
        self.assert_finished(result, merge_commit)

    def test_merged_head_is_the_merge_commit_second_parent(self) -> None:
        f = self.f
        f.review("approved")
        other = self.clone("second-clone")
        f.git("switch", "issue-1", cwd=other)
        (other / "unreviewed.txt").write_text("unreviewed\n")
        f.git("add", "unreviewed.txt", cwd=other)
        f.git("commit", "-m", "test: push on top", cwd=other)
        f.git("push", "--quiet", "origin", "issue-1", cwd=other)
        pushed = f.git("rev-parse", "HEAD", cwd=other)
        self.forge_merge("merge", head=pushed)
        if not self.forge_deletes_branch:
            # Otherwise cleanup keeps the branch, which no longer points at
            # the local head.
            f.git("push", "--quiet", "origin", ":refs/heads/issue-1",
                  cwd=other)
        result = self.cleanup()
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["head"], self.head)
        self.assertEqual(result["approved_head"], self.head)
        self.assertEqual(result["merged_head"], pushed)
        self.assertEqual(result["forge_head_branch"], "issue-1")
        self.assertIs(result["merged_outside_pr_merge"], True)

    def test_merged_head_after_squash_is_the_head(self) -> None:
        merge_commit = self.forge_merge("squash")
        result = self.cleanup()
        self.assertEqual(result["merged_head"], self.head)
        self.assert_finished(result, merge_commit)

    def test_merged_head_is_null_when_the_source_is_unknown(self) -> None:
        # A one-parent merge commit above the head: ancestry holds, but no
        # commit is proved to be the merged source.
        f = self.f
        f.commit("value = 1\nsecond = 2\nthird = 3\nabove = 4\n")
        above = f.git("rev-parse", "HEAD", cwd=f.worktree)
        f.git("push", "--quiet", "origin", "HEAD:refs/heads/main",
              cwd=f.worktree)
        f.git("reset", "--hard", self.head, cwd=f.worktree)
        self.edit_pr(merged=True, state="closed", merge_commit_sha=above)
        merge_commit = above
        result = self.cleanup()
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertIsNone(result["merged_head"])
        self.assertEqual(result["merge_commit"], merge_commit)
        self.assertEqual(result["exit_code"], 0)

    def test_stale_forge_head_changes_no_cleanup(self) -> None:
        """REV-2: a merged PR reporting a head the remote does not serve."""
        f = self.f
        f.review("approved")
        (f.worktree / "untracked.txt").write_text("retain")
        merge_commit = self.forge_merge("merge")
        stale = "0123456789abcdef" * 2 + "01234567"
        self.edit_pr(head_sha=stale, head_ref="synthetic-pull-ref")
        first = self.cleanup(expected=3)
        self.assertEqual(first["cleanup"][-1]["step"],
                         "implementation worktree")
        self.assertEqual(first["forge_head"], stale)
        (f.worktree / "untracked.txt").unlink()
        second = self.cleanup()
        self.assertEqual(second["forge_head"], stale)
        self.assertEqual(second["forge_head_branch"], "synthetic-pull-ref")
        self.assertEqual(second["approved_head"], self.head)
        self.assertEqual(second["head"], self.head)
        self.assert_finished(second, merge_commit, "up to date")

    def test_merged_pr_without_merge_commit_or_on_another_base_is_refused(
        self,
    ) -> None:
        f = self.f
        self.forge_merge("merge")
        f.git("push", "--quiet", "origin", "main:refs/heads/release")
        model = f.read_model()
        original = dict(model["prs"]["1"], base=dict(
            model["prs"]["1"]["base"]))
        for case, values, text in (
            ("no merge commit", {"merge_commit_sha": None},
             "reports no merge commit"),
            ("another base", {"base_ref": "release"},
             "base branch release is not the configured base branch main"),
        ):
            with self.subTest(case=case):
                self.edit_pr(**values)
                worktrees = f.git("worktree", "list", "--porcelain")
                writes = self.writes()
                result = self.cleanup(expected=4)
                self.assertIn(text, result["error"])
                self.assertEqual(
                    f.git("worktree", "list", "--porcelain"), worktrees)
                self.assertEqual(self.writes(), writes)
                self.assertFalse(self.record.exists())
                self.assertEqual(f.git("rev-parse", "HEAD"), f.base)
                model = f.read_model()
                model["prs"]["1"] = dict(original, base=dict(
                    original["base"]))
                f.save_model(model)
        self.assertEqual(self.cleanup()["exit_code"], 0)

    def test_unwritable_record_retains_everything_until_a_retry(self) -> None:
        f = self.f
        merge_commit = self.forge_merge("merge")
        writes = self.writes()
        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            with patch("agent_squad.merging.write_merge_record",
                       side_effect=OSError("disk full")):
                result = cleanup_pr(
                    repository, make_forge(repository, "implementer"), 1)
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["merge_record"]["reason"], "disk full")
        self.assertEqual(self.writes(), writes)
        self.assertFalse(self.record.exists())
        self.assert_retained_unrecorded(result)
        self.assert_finished(self.cleanup(), merge_commit)

    def test_unpushed_file_neutral_commit_after_a_merge_is_retained(
        self,
    ) -> None:
        f = self.f
        self.forge_merge("merge")
        f.git("commit", "--allow-empty", "-m", "test: unpushed",
              cwd=f.worktree)
        result = self.cleanup(expected=1)
        self.assertIn("not the last pushed head", result["integration"])
        self.assert_retained(result)

    def test_squash_after_a_file_neutral_base_advance_is_retained(
        self,
    ) -> None:
        f = self.f
        advance = self.clone("advance")
        f.git("commit", "--allow-empty", "-m", "test: advance base",
              cwd=advance)
        f.git("push", "--quiet", "origin", "main", cwd=advance)
        self.forge_merge("squash")
        result = self.cleanup(expected=1)
        self.assertIn("parent is not an ancestor", result["integration"])
        self.assert_retained(result)

    def test_rebase_of_two_commits_onto_an_unmoved_base_is_retained(
        self,
    ) -> None:
        f = self.f
        first = self.head
        self.head = f.push("value = 1\nsecond = 2\nthird = 3\nmore = 4\n")
        self.land_on_main(first, self.head)
        result = self.cleanup(expected=1)
        self.assertIn("parent is not an ancestor", result["integration"])
        self.assert_retained(result)

    def test_squash_without_a_remote_tracking_ref_is_retained(self) -> None:
        f = self.f
        self.forge_merge("squash")
        f.git("update-ref", "-d", "refs/remotes/origin/issue-1")
        result = self.cleanup(expected=1)
        self.assertIn("not the last pushed head", result["integration"])
        self.assert_retained(result)

    def test_record_outside_the_worktree_root_is_ignored(self) -> None:
        f = self.f
        outside = f.root / "outside-root"
        f.git("worktree", "add", "-b", "outside", str(outside))
        admin = Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        record = (admin / "agent-squad-implementation.json").read_text()
        outside_admin = Path(
            f.git("rev-parse", "--absolute-git-dir", cwd=outside))
        (outside_admin / "agent-squad-implementation.json").write_text(record)
        merge_commit = self.forge_merge("merge")
        result = self.cleanup()
        self.assertTrue(outside.exists())
        self.assertIn(str(outside), f.git("worktree", "list", "--porcelain"))
        self.assertEqual(result["merge_commit"], merge_commit)
        self.assertTrue(all(s["ok"] for s in result["cleanup"]))

    def test_unproven_ownership_is_refused_without_change(self) -> None:
        f = self.f

        def assert_refused(text: str) -> None:
            worktrees = f.git("worktree", "list", "--porcelain")
            writes = self.writes()
            result = self.cleanup(expected=4)
            self.assertIn("pr cleanup refused", result["error"])
            self.assertIn(text, result["error"])
            self.assertEqual(
                f.git("worktree", "list", "--porcelain"), worktrees)
            self.assertEqual(self.writes(), writes)
            self.assertFalse(self.record.exists())
            self.assertTrue(self.scratch.exists())
            self.assertEqual(f.git("rev-parse", "HEAD"), f.base)
            self.assertNotEqual(
                f.git("ls-remote", "--heads", "origin", "issue-1")
                if not self.forge_deletes_branch else "kept", "")

        with self.subTest(case="open PR"):
            assert_refused("only finishes a merge started by pr merge")
        self.forge_merge("merge")
        admin = Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        owner = admin / "agent-squad-implementation.json"
        original = owner.read_text()
        with self.subTest(case="no implementation record"):
            owner.unlink()
            assert_refused("no implementation record names PR #1")
            owner.write_text(original)
        with self.subTest(case="record names another PR"):
            owner.write_text(json.dumps(dict(json.loads(original), pr=2)))
            assert_refused("no implementation record names PR #1")
            owner.write_text(original)
        with self.subTest(case="record names the PR by a boolean"):
            owner.write_text(json.dumps(dict(json.loads(original), pr=True)))
            assert_refused("no implementation record names PR #1")
            owner.write_text(original)
        with self.subTest(case="record names another Git directory"):
            owner.write_text(json.dumps(
                dict(json.loads(original), common=str(f.origin))))
            assert_refused("cannot prove implementation ownership")
            owner.write_text(original)
        with self.subTest(case="worktree on another branch"):
            f.git("switch", "-c", "elsewhere", cwd=f.worktree)
            assert_refused("implementation branch or HEAD changed")
            f.git("switch", "issue-1", cwd=f.worktree)
            f.git("branch", "-D", "elsewhere")
        with self.subTest(case="two records name the PR"):
            other = f.worktree.parent / "issue-2"
            f.git("worktree", "add", "-b", "issue-2", str(other))
            other_admin = Path(
                f.git("rev-parse", "--absolute-git-dir", cwd=other))
            (other_admin / "agent-squad-implementation.json").write_text(
                original)
            assert_refused("2 implementation records name PR #1")
            f.git("worktree", "remove", "--force", str(other))
        # With ownership restored, the same PR is finished.
        self.assertEqual(self.cleanup()["exit_code"], 0)

    def test_local_commit_after_the_merged_one_is_retained(self) -> None:
        self.forge_merge("merge")
        local = self.f.commit("value = 1\nsecond = 2\nthird = 3\nmore = 4\n")
        result = self.cleanup(expected=1)
        self.assertIn("contains neither", result["integration"])
        self.assertEqual(result["head"], local)
        self.assertIs(result["merged_outside_pr_merge"], True)
        self.assert_retained(result)
        self.assertEqual(
            self.f.git("rev-parse", "HEAD", cwd=self.f.worktree), local)

    def test_squash_onto_a_moved_base_is_retained(self) -> None:
        f = self.f
        advance = f.root / "advance"
        f.git("clone", str(f.origin), str(advance))
        (advance / "base-only.txt").write_text("base changed\n")
        f.git("add", "base-only.txt", cwd=advance)
        f.git("commit", "-m", "test: advance base", cwd=advance)
        f.git("push", "origin", "main", cwd=advance)
        self.forge_merge("squash")
        result = self.cleanup(expected=1)
        self.assertIn("contains neither", result["integration"])
        self.assert_retained(result)

    def test_interrupted_run_finishes_and_keeps_the_flag(self) -> None:
        f = self.f
        (f.worktree / "untracked.txt").write_text("retain")
        merge_commit = self.forge_merge("squash")
        first = self.cleanup(expected=3)
        self.assertEqual(
            [(s["step"], s["ok"]) for s in first["cleanup"]], [
                ("implementation ownership", True), ("remote branch", True),
                ("remote-tracking ref", True),
                ("implementation worktree", False),
            ])
        self.assertIs(first["merged_outside_pr_merge"], True)
        self.assertEqual(first["merge_record"]["result"], "kept")
        self.assertEqual(first["cleanup_command"], COMMAND)
        # Step 5 runs whatever the cleanup's outcome.
        self.assertEqual(first["fast_forward"]["result"], "fast-forwarded")
        self.assertEqual(json.loads(self.record.read_text()), {
            "schema_version": 1, "common": str(f.repo / ".git"), "pr": 1,
            "head": self.head, "head_branch": "issue-1",
            "base_branch": "main", "merge_method": "squash",
            "moved_base": False, "issue": 1, "outside_pr_merge": True,
        })
        (f.worktree / "untracked.txt").unlink()
        second = self.cleanup()
        self.assertEqual(second["integration"], "verified by tree identity")
        self.assertEqual(second["merge_method"], "squash")
        self.assertEqual(second["cleanup"][0]["detail"], str(f.worktree))
        self.assert_finished(second, merge_commit, "up to date")

    def test_branch_the_forge_deleted_counts_as_done(self) -> None:
        merge_commit = self.forge_merge("merge")
        if not self.forge_deletes_branch:
            self.f.git("push", "origin", ":refs/heads/issue-1")
        self.assertEqual(
            self.f.git("ls-remote", "--heads", "origin", "issue-1"), "")
        start = len(self.f.read_model()["calls"])
        result = self.cleanup()
        self.assertEqual(result["cleanup"][1], {
            "step": "remote branch", "ok": True,
            "detail": "confirmed absent: issue-1",
        })
        self.assertFalse(any(
            "DELETE" in (c.get("method"), *c.get("arguments", []))
            for c in self.f.read_model()["calls"][start:]))
        self.assert_finished(result, merge_commit)

    def test_moved_remote_branch_is_kept_and_reported(self) -> None:
        f = self.f
        self.forge_merge("merge")
        other = f.root / "other"
        f.git("clone", str(f.origin), str(other))
        (other / "moved.txt").write_text("moved\n")
        f.git("add", "moved.txt", cwd=other)
        f.git("commit", "-m", "test: move the head branch", cwd=other)
        f.git("push", "--force", "origin", "HEAD:refs/heads/issue-1",
              cwd=other)
        moved = f.git("rev-parse", "HEAD", cwd=other)
        result = self.cleanup(expected=3)
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["cleanup"][-1]["step"], "remote branch")
        self.assertFalse(result["cleanup"][-1]["ok"])
        self.assertIn("no longer matches", result["cleanup"][-1]["detail"])
        self.assertEqual(result["cleanup_command"], COMMAND)
        self.assertTrue(self.record.exists())
        self.assertTrue(f.worktree.exists())
        self.assertEqual(
            f.git("ls-remote", "--heads", "origin", "issue-1").split()[0],
            moved)


class GitHubForgeMergedCleanupTests(ForgeMergedCleanup, unittest.TestCase):
    fixture = ForgeFixture

    def test_running_reviewer_is_closed_and_listed(self) -> None:
        f = self.f
        f.herdr_settings(implementer={"cwd": str(f.worktree)})
        review = Path(f.cli("reviewer", "launch", "--pr", "1")["worktree"])
        model = f.herdr_model()
        self.assertEqual(
            [a["agent_status"] for a in model["agents"]
             if a["name"].startswith("reviewer-pr1-")], ["working"])
        merge_commit = self.forge_merge("merge")
        result = self.cleanup()
        closed = [s for s in result["cleanup"]
                  if s["step"] == "review worktree"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["detail"]["removed"], str(review))
        self.assertFalse(review.exists())
        self.assertEqual(f.herdr_model()["workspaces"], [])
        self.assert_finished(result, merge_commit)

    def test_reviewer_without_worktree_registration_is_listed(self) -> None:
        """REV-5: Herdr no longer reports the workspace's worktree."""
        f = self.f
        f.herdr_settings(implementer={"cwd": str(f.worktree)})
        review = Path(f.cli("reviewer", "launch", "--pr", "1")["worktree"])
        model = f.herdr_model()
        workspace = next(w for w in model["workspaces"]
                         if (w.get("worktree") or {}).get("checkout_path")
                         == str(review))
        workspace["worktree"] = None
        f.save_herdr(model)
        merge_commit = self.forge_merge("merge")
        result = self.cleanup()
        closed = [s for s in result["cleanup"]
                  if s["step"] == "review worktree"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["detail"]["removed"], str(review))
        self.assertEqual(closed[0]["detail"]["workspace_id"],
                         workspace["workspace_id"])
        self.assertEqual(f.herdr_model()["workspaces"], [])
        self.assert_finished(result, merge_commit)

    def test_pr_merge_names_cleanup_only_while_cleanup_can_run(self) -> None:
        self.f.review("approved")
        merge_commit = self.forge_merge("merge")
        refused = self.pr("merge", 4)
        self.assertIn("PR is not open", refused["error"])
        self.assertIn(COMMAND, refused["error"])
        self.assertIn("merged outside pr merge", refused["error"])
        self.assert_finished(self.cleanup(), merge_commit)
        refused = self.pr("merge", 4)
        self.assertIn("PR is not open", refused["error"])
        self.assertNotIn("pr cleanup", refused["error"])
        self.assertEqual(self.merge_requests(), 1)

    def test_pr_merge_reports_its_own_merge_and_approved_head(self) -> None:
        self.f.review("approved")
        result = self.pr("merge", 0)
        self.assertIs(result["merged_outside_pr_merge"], False)
        self.assertEqual(result["approved_head"], self.head)

    def test_record_from_an_earlier_version_loads_as_pr_merge(self) -> None:
        f = self.f
        f.review("approved")
        f.settings(branch_delete_403=True)
        stopped = self.pr("merge", 3)
        self.assertIs(stopped["merged_outside_pr_merge"], False)
        record = json.loads(self.record.read_text())
        self.assertIs(record.pop("outside_pr_merge"), False)
        self.record.write_text(json.dumps(record))
        f.settings(branch_delete_403=False)
        result = self.cleanup()
        self.assertIs(result["merged_outside_pr_merge"], False)
        self.assertEqual(result["approved_head"], self.head)
        self.assertEqual(result["merge_record"]["result"], "deleted")

    def test_record_with_an_invalid_flag_is_refused(self) -> None:
        f = self.f
        f.review("approved")
        f.settings(branch_delete_403=True)
        self.pr("merge", 3)
        record = json.loads(self.record.read_text())
        f.settings(branch_delete_403=False)
        for value in ("true", 1, None):
            with self.subTest(value=value):
                self.record.write_text(
                    json.dumps(dict(record, outside_pr_merge=value)))
                calls = len(f.read_model()["calls"])
                result = self.cleanup(expected=1)
                self.assertIn("malformed merge record", result["error"])
                self.assertEqual(len(f.read_model()["calls"]), calls)
        self.assertTrue(f.worktree.exists())


class ForgejoForgeMergedCleanupTests(ForgeMergedCleanup, unittest.TestCase):
    fixture = ForgejoFixture
    forge_deletes_branch = True

    def test_branch_the_forge_kept_is_deleted_by_cleanup(self) -> None:
        f = self.f
        f.settings(leave_branch=True)
        merge_commit = self.forge_merge("merge")
        self.assertNotEqual(
            f.git("ls-remote", "--heads", "origin", "issue-1"), "")
        start = len(f.read_model()["calls"])
        result = self.cleanup()
        deletes = [c["path"] for c in f.read_model()["calls"][start:]
                   if c["method"] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertTrue(deletes[0].endswith("/branches/issue-1"), deletes)
        self.assert_finished(result, merge_commit)

    def test_synthetic_head_after_the_merge_is_not_trusted(self) -> None:
        """E9: the merged PR reports an older head and a synthetic branch."""
        f = self.f
        f.review("approved")
        f.settings(post_merge_head_discrepancy=True)
        merge_commit = self.forge_merge("merge")
        reported = f.cli("pr", "head", "--pr", "1", cwd=f.repo)
        self.assertEqual(reported["head_branch"], "synthetic-pull-ref")
        self.assertNotEqual(reported["head"], self.head)
        result = self.cleanup()
        self.assertEqual(result["head"], self.head)
        self.assertEqual(result["approved_head"], self.head)
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assert_finished(result, merge_commit)


if __name__ == "__main__":
    unittest.main()
