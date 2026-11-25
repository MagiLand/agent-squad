"""#175: pr cleanup finishes a PR that the forge merged without pr merge."""

from datetime import datetime, timedelta, timezone
import json
import shutil
import stat
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests.forge_support import (
    REVIEW, ForgeFixture, ForgejoFixture, finding, is_write,
)
from agent_squad import merging
from agent_squad.forge import ForgeError, make_forge
from agent_squad.initialization import (
    GateError, load_initialized_repository,
)
from agent_squad.conventions import approved_review_head, render_line
from agent_squad.herdr import HerdrError
from agent_squad.merging import cleanup_pr

COMMAND = "agent-squad pr cleanup --as implementer --pr 1"


def open_directories() -> int:
    """How many directories this process holds open.

    Only directories are counted: the in-process Forgejo fake closes each
    request's socket on its own thread, some time after the answer.
    """
    count = 0
    for name in os.listdir("/dev/fd"):
        try:
            count += stat.S_ISDIR(os.fstat(int(name)).st_mode)
        except OSError:
            # The listing's own descriptor, closed again by now.
            pass
    return count


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
    # GitHub reports a dismissed review as DISMISSED (§7.3).
    dismissed = False

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

    def test_unavailable_earlier_review_head_changes_no_cleanup(self) -> None:
        """REV-8: an earlier approval names a commit Git cannot fetch."""
        f = self.f
        f.review("approved")
        replaced = self.head
        self.head = f.push("value = 1\nsecond = 2\nthird = 3\nmore = 4\n")
        f.review("approved")
        missing = "ab" * 20
        model = f.read_model()
        earlier = [r for r in model["prs"]["1"]["reviews"]
                   if r["commit_id"] == replaced
                   and "verdict=approved" in r["body"]]
        self.assertEqual(len(earlier), 1)
        earlier[0]["commit_id"] = missing
        earlier[0]["body"] = earlier[0]["body"].replace(replaced, missing)
        f.save_model(model)
        (f.worktree / "untracked.txt").write_text("retain")
        merge_commit = self.forge_merge("merge")
        first = self.cleanup(expected=3)
        self.assertEqual(first["approved_head"], self.head)
        self.assertEqual(first["cleanup"][-1]["step"],
                         "implementation worktree")
        (f.worktree / "untracked.txt").unlink()
        second = self.cleanup()
        self.assertEqual(second["approved_head"], self.head)
        self.assert_finished(second, merge_commit, "up to date")

    def test_pr_merge_names_cleanup_despite_a_stale_forge_head(self) -> None:
        """REV-9: the refusal needs no read of the reported head."""
        merge_commit = self.forge_merge("merge")
        self.edit_pr(head_sha="0123456789abcdef" * 2 + "01234567",
                     head_ref="synthetic-pull-ref")
        requests = self.merge_requests()
        refused = self.pr("merge", 4)
        self.assertIn("PR is not open", refused["error"])
        self.assertIn("merged outside pr merge", refused["error"])
        self.assertIn(COMMAND, refused["error"])
        self.assert_finished(self.cleanup(), merge_commit)
        refused = self.pr("merge", 4)
        self.assertIn("PR is not open", refused["error"])
        self.assertNotIn("pr cleanup", refused["error"])
        self.assertEqual(self.merge_requests(), requests)

    def retry_past(self, unrelated: Path) -> None:
        """A first run stops after removing the worktree; the retry finishes
        although ``unrelated`` holds a record that names the PR."""
        f = self.f
        target = f.root / "scratch-target"
        target.mkdir()
        scratch = self.scratch
        shutil.rmtree(scratch)
        scratch.symlink_to(target)
        merge_commit = self.forge_merge("merge")
        first = self.cleanup(expected=3)
        self.assertEqual(first["cleanup"][-1]["step"], "scratch directory")
        self.assertFalse(f.worktree.exists())
        scratch.unlink()
        second = self.cleanup()
        # The unrelated checkout is kept; with it gone, all else is done.
        self.assertTrue(unrelated.exists())
        self.assertIn(str(unrelated), f.git("worktree", "list"))
        f.git("worktree", "remove", "--force", str(unrelated))
        self.assert_finished(second, merge_commit, "up to date")

    def unrelated_owner(self, path: Path, **values: object) -> Path:
        f = self.f
        f.git("worktree", "add", "-b", path.name, str(path))
        admin = Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        record = json.loads(
            (admin / "agent-squad-implementation.json").read_text())
        other = Path(f.git("rev-parse", "--absolute-git-dir", cwd=path))
        (other / "agent-squad-implementation.json").write_text(
            json.dumps(dict(record, **values)))
        return path

    def test_record_outside_the_root_does_not_block_a_retry(self) -> None:
        """REV-10: an exact copy outside worktree_root."""
        self.retry_past(self.unrelated_owner(self.f.root / "outside-root"))

    def test_boolean_record_does_not_block_a_retry(self) -> None:
        """REV-10: a record under worktree_root naming the PR by true."""
        self.retry_past(self.unrelated_owner(
            self.f.worktree.parent / "issue-9", pr=True))

    def test_reused_git_directory_does_not_block_a_retry(self) -> None:
        """REV-10: Git gives a new checkout the removed one's directory."""
        f = self.f
        target = f.root / "scratch-target"
        target.mkdir()
        shutil.rmtree(self.scratch)
        self.scratch.symlink_to(target)
        admin = Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        record = (admin / "agent-squad-implementation.json").read_text()
        merge_commit = self.forge_merge("merge")
        first = self.cleanup(expected=3)
        self.assertEqual(first["cleanup"][-1]["step"], "scratch directory")
        self.assertFalse(admin.exists())
        unrelated = f.root / "elsewhere" / f.worktree.name
        unrelated.parent.mkdir()
        f.git("worktree", "add", "-b", "elsewhere", str(unrelated))
        reused = Path(f.git("rev-parse", "--absolute-git-dir", cwd=unrelated))
        self.assertEqual(reused, admin)
        (reused / "agent-squad-implementation.json").write_text(record)
        self.scratch.unlink()
        second = self.cleanup()
        self.assertTrue(unrelated.exists())
        f.git("worktree", "remove", "--force", str(unrelated))
        self.assert_finished(second, merge_commit, "up to date")

    def replace_checkout(self) -> tuple[Path, Path]:
        """Park the original checkout, detached, and put a new worktree with
        a copy of its ownership record at the issue path, on the same branch
        and head. Return the parked path and the cleanup marker."""
        f = self.f
        marker = Path(
            json.loads(self.record.read_text())["implementation_record"])
        self.assertTrue(marker.is_file())
        owner = "agent-squad-implementation.json"
        text = (marker.parent / owner).read_text()
        parked = f.root / "parked-original"
        f.git("worktree", "move", str(f.worktree), str(parked))
        f.git("switch", "--detach", cwd=parked)
        f.git("worktree", "add", str(f.worktree), "issue-1")
        admin = Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        self.assertNotEqual(admin.resolve(), marker.parent)
        (admin / owner).write_text(text)
        self.assertEqual(f.git("rev-parse", "HEAD", cwd=f.worktree),
                         self.head)
        return parked, marker

    def assert_replacement_refused(self, parked: Path, marker: Path) -> None:
        f = self.f
        writes = self.writes()
        refused = self.cleanup(expected=3)
        self.assertEqual(
            [s["step"] for s in refused["cleanup"]],
            ["implementation ownership"])
        self.assertIn("does not hold the merge record's cleanup marker",
                      refused["cleanup"][0]["detail"])
        self.assertEqual(self.writes(), writes)
        self.assertTrue(parked.exists())
        self.assertTrue(marker.is_file())
        self.assertTrue(f.worktree.exists())
        self.assertTrue(self.record.exists())
        self.assertTrue(self.scratch.exists())
        self.assertTrue(self.issue_scratch.exists())
        self.assertNotEqual(f.git("branch", "--list", "issue-1"), "")

    def restore_checkout(self, parked: Path) -> None:
        f = self.f
        f.git("worktree", "remove", "--force", str(f.worktree))
        f.git("switch", "issue-1", cwd=parked)
        f.git("worktree", "move", str(parked), str(f.worktree))

    def test_replacement_checkout_without_the_marker_is_refused(
        self,
    ) -> None:
        """REV-17: a new worktree at the issue path, on the branch and head,
        with a copied ownership record is not the proved checkout."""
        f = self.f
        merge_commit = self.forge_merge("merge")
        blocker = f.worktree / "untracked.txt"
        blocker.write_text("retain")
        first = self.cleanup(expected=3)
        self.assertEqual(first["cleanup"][-1]["step"],
                         "implementation worktree")
        parked, marker = self.replace_checkout()
        self.assert_replacement_refused(parked, marker)
        # A symlink to the marker in the replacement's Git directory is not
        # the marker either.
        admin = Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        record = json.loads(self.record.read_text())
        linked = admin.resolve() / marker.name
        linked.symlink_to(marker)
        self.record.write_text(json.dumps(
            {**record, "implementation_record": str(linked)}))
        self.assert_replacement_refused(parked, marker)
        self.record.write_text(json.dumps(record))
        self.restore_checkout(parked)
        (f.worktree / "untracked.txt").unlink()
        self.assert_finished(self.cleanup(), merge_commit, "up to date")

    def test_recreated_checkout_in_the_reused_git_directory_is_refused(
        self,
    ) -> None:
        """REV-17: after the proved checkout was removed, a new worktree at
        the issue path, on a recreated branch, gets the same Git directory
        but not the marker."""
        f = self.f
        target = f.root / "scratch-target"
        target.mkdir()
        shutil.rmtree(self.scratch)
        self.scratch.symlink_to(target)
        admin = Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        owner = (admin / "agent-squad-implementation.json").read_text()
        merge_commit = self.forge_merge("merge")
        first = self.cleanup(expected=3)
        self.assertEqual(first["cleanup"][-1]["step"], "scratch directory")
        marker = Path(
            json.loads(self.record.read_text())["implementation_record"])
        self.assertEqual(marker.parent, admin.resolve())
        self.assertFalse(admin.exists())
        f.git("worktree", "add", "-b", "issue-1", str(f.worktree), self.head)
        self.assertEqual(
            Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree)),
            admin)
        (admin / "agent-squad-implementation.json").write_text(owner)
        writes = self.writes()
        refused = self.cleanup(expected=3)
        self.assertEqual([s["step"] for s in refused["cleanup"]],
                         ["implementation ownership"])
        self.assertIn("does not hold the merge record's cleanup marker",
                      refused["cleanup"][0]["detail"])
        self.assertEqual(self.writes(), writes)
        self.assertTrue(f.worktree.exists())
        self.assertTrue(self.record.exists())
        f.git("worktree", "remove", "--force", str(f.worktree))
        f.git("branch", "-D", "issue-1")
        self.scratch.unlink()
        self.assert_finished(self.cleanup(), merge_commit, "up to date")

    def break_conversation(self) -> None:
        """Make only the read of the PR's ordinary conversation fail."""
        raise NotImplementedError

    def conversation_failure(self, *, retry: bool) -> None:
        """REV-19: neither the pr merge refusal nor pr cleanup reads the
        PR's ordinary conversation after the forge merged."""
        f = self.f
        f.review("approved", [finding("optional")])
        merge_commit = self.forge_merge("merge")
        if retry:
            blocker = f.worktree / "untracked.txt"
            blocker.write_text("retain")
            first = self.cleanup(expected=3)
            self.assertEqual(first["cleanup"][-1]["step"],
                             "implementation worktree")
            self.assertTrue(self.record.exists())
            blocker.unlink()
        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            forge = make_forge(repository, "implementer")
            whole = forge.snapshot(1)
            self.break_conversation()
            with self.assertRaises(ForgeError):
                forge.snapshot(1)
            partial = forge.snapshot(1, conversation=False)
        self.assertEqual(partial.conversation, ())
        self.assertEqual(partial.pr, whole.pr)
        self.assertEqual(partial.reviews, whole.reviews)
        self.assertEqual(partial.comments, whole.comments)
        self.assertTrue(whole.reviews and whole.comments)
        self.assertEqual(f.run([
            sys.executable, "-m", "agent_squad", "status", "--pr", "1",
            "--json",
        ], cwd=f.repo).returncode, 1)
        writes = self.writes()
        calls = len(f.read_model()["calls"])
        refused = self.pr("merge", 4)
        self.assertIn("PR is not open", refused["error"])
        self.assertIn(COMMAND, refused["error"])
        self.assertEqual(self.writes(), writes)
        # The refusal reads the PR and nothing else; gh's token lookup is
        # not a forge read.
        reads = [call_path(c) for c in f.read_model()["calls"][calls:]
                 if call_path(c) != "token"]
        self.assertEqual(len(reads), 1, reads)
        self.assertTrue(reads[0].endswith("/pulls/1"), reads)
        calls = len(f.read_model()["calls"])
        result = self.cleanup()
        self.assertFalse(any(
            call_path(c).endswith("/issues/1/comments")
            for c in f.read_model()["calls"][calls:]))
        self.assertEqual(result["approved_head"], self.head)
        self.assert_finished(
            result, merge_commit, "up to date" if retry else "fast-forwarded")

    def test_failed_conversation_read_does_not_stop_a_first_run(
        self,
    ) -> None:
        self.conversation_failure(retry=False)

    def test_failed_conversation_read_does_not_stop_a_retry(self) -> None:
        self.conversation_failure(retry=True)

    def test_failed_review_read_stops_cleanup_before_any_change(
        self,
    ) -> None:
        """The approval evidence is still read on every run."""
        f = self.f
        f.review("approved")
        merge_commit = self.forge_merge("merge")
        writes = self.writes()
        with patch.dict(os.environ, f.env, clear=True):
            repository = load_initialized_repository(f.repo)
            forge = make_forge(repository, "implementer")
            with patch.object(
                type(forge), "reviews",
                side_effect=ForgeError("reviews unreadable"),
            ), patch.object(
                type(forge), "graphql", create=True,
                side_effect=ForgeError("reviews unreadable"),
            ):
                descriptors = open_directories()
                with self.assertRaisesRegex(ForgeError, "reviews unreadable"):
                    cleanup_pr(repository, forge, 1)
                # The proved Git directory is not left open.
                self.assertEqual(open_directories(), descriptors)
        self.assertEqual(self.writes(), writes)
        self.assertFalse(self.record.exists())
        self.assertTrue(f.worktree.exists())
        self.assertTrue(self.scratch.exists())
        self.assert_finished(self.cleanup(), merge_commit)

    def recreate_checkout_on(self, target: str):
        """Patch ``merging.<target>`` so that its first call first removes
        the implementation worktree and creates it again at the same place,
        branch, and head, with a copy of its ownership record. Git gives
        the new worktree the old Git directory's name."""
        f = self.f
        directory = Path(
            f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        owner = directory / "agent-squad-implementation.json"
        text = owner.read_text()
        original = directory.stat()
        real = getattr(merging, target)
        done = []

        def recreate(*args, **options):
            if not done and (target != "git_output" or args[1] == "fetch"):
                done.append(True)
                self.assertFalse(self.record.exists())
                f.git("worktree", "remove", str(f.worktree))
                f.git("worktree", "add", str(f.worktree), "issue-1")
                self.assertEqual(Path(f.git(
                    "rev-parse", "--absolute-git-dir", cwd=f.worktree,
                )), directory)
                # The command still holds the removed directory open, so
                # its identity cannot have been given to the new one.
                self.assertFalse(os.path.samestat(directory.stat(), original))
                owner.write_text(text)
            return real(*args, **options)

        return directory, done, patch.object(
            merging, target, side_effect=recreate)

    def assert_nothing_marked(self, directory: Path, writes: int) -> None:
        f = self.f
        self.assertEqual(self.writes(), writes)
        self.assertFalse(self.record.exists())
        self.assertEqual(list(directory.glob("agent-squad-cleanup-*")), [])
        self.assertTrue(f.worktree.exists())
        self.assertNotEqual(f.git("branch", "--list", "issue-1"), "")
        self.assertTrue(self.scratch.exists())
        self.assertTrue(self.issue_scratch.exists())
        self.assertEqual(f.git("rev-parse", "HEAD"), f.base)

    def test_checkout_replaced_during_the_fetch_is_refused(self) -> None:
        """REV-20: the worktree is removed and created again between the
        ownership proof and the marker; the new Git directory has the old
        name but is not the proved one, and gets neither marker nor record."""
        f = self.f
        merge_commit = self.forge_merge("merge")
        writes, descriptors = self.writes(), open_directories()
        directory, done, recreated = self.recreate_checkout_on("git_output")
        with patch.dict(os.environ, f.env, clear=True), recreated:
            repository = load_initialized_repository(f.repo)
            result = cleanup_pr(
                repository, make_forge(repository, "implementer"), 1)
        self.assertTrue(done)
        self.assertEqual(result["integration"], "verified by ancestry")
        self.assertEqual(result["merge_record"]["reason"],
                         "implementation Git directory changed")
        self.assert_retained_unrecorded(result)
        self.assert_nothing_marked(directory, writes)
        self.assertEqual(open_directories(), descriptors)
        # A new run proves the checkout that is there now.
        self.assert_finished(self.cleanup(), merge_commit)

    def test_checkout_replaced_during_the_ownership_proof_is_refused(
        self,
    ) -> None:
        """REV-20: the proof must be about the directory held open."""
        f = self.f
        merge_commit = self.forge_merge("merge")
        writes, descriptors = self.writes(), open_directories()
        directory, done, recreated = self.recreate_checkout_on(
            "owned_implementation")
        with patch.dict(os.environ, f.env, clear=True), recreated:
            repository = load_initialized_repository(f.repo)
            with self.assertRaisesRegex(
                GateError, "no merge record for PR #1; implementation Git"
                " directory changed",
            ):
                cleanup_pr(
                    repository, make_forge(repository, "implementer"), 1)
        self.assertTrue(done)
        self.assert_nothing_marked(directory, writes)
        self.assertEqual(open_directories(), descriptors)
        self.assert_finished(self.cleanup(), merge_commit)

    def approval_after_a_blocking_finding(self, verified: str) -> None:
        """REV-21: an approval at the next head after a review with a
        blocking finding; ``verified`` says when the Reviewer verified the
        fix: "before" the approval, "after" it, or "never"."""
        f = self.f
        f.review("changes_requested", [finding()])
        self.head = f.push("value = 1\nsecond = 2\nthird = 3\nmore = 4\n")
        f.reply("REV-1", disposition="fixed", sha=self.head)
        if verified == "before":
            f.reply("REV-1", verification="fixed")
        # Placed in the model directly: review post refuses this approval
        # while the finding is unsettled, but a forge can hold one.
        model = f.read_model()
        reviews = model["prs"]["1"]["reviews"]
        ident = model["next_id"]
        model["next_id"] += 1
        stamp = (datetime(2026, 1, 1, tzinfo=timezone.utc)
                 + timedelta(seconds=ident)).isoformat().replace(
                     "+00:00", "Z")
        reviews.append({
            **reviews[0], "id": ident, "commit_id": self.head,
            "state": "APPROVED", "created_at": stamp, "submitted_at": stamp,
            "body": render_line(
                "review", pr=1, head=self.head, base=f.base,
                verdict="approved",
            ) + "\n\n" + REVIEW,
        })
        f.save_model(model)
        if verified == "after":
            f.reply("REV-1", verification="fixed")
        valid = verified == "before"
        status = f.status()
        self.assertEqual(any(
            d["detail"] == "approved review has unsettled blocking findings"
            for d in status["diagnostics"]), not valid)
        self.assertEqual(
            [r["head"] for r in status["reviews"]
             if r["verdict"] == "approved"], [self.head] if valid else [])
        merge_commit = self.forge_merge("merge")
        result = self.cleanup()
        self.assertEqual(result["approved_head"],
                         self.head if valid else None)
        self.assertEqual(result["merged_head"], self.head)
        self.assert_finished(result, merge_commit)

    def test_approval_with_an_unsettled_blocking_finding_is_not_reported(
        self,
    ) -> None:
        self.approval_after_a_blocking_finding("never")

    def test_approval_after_a_verified_fix_is_reported(self) -> None:
        self.approval_after_a_blocking_finding("before")

    def test_approval_verified_only_afterwards_is_not_reported(self) -> None:
        self.approval_after_a_blocking_finding("after")

    def test_retry_after_pr_merge_reports_the_recorded_approval(
        self,
    ) -> None:
        """REV-23: pr merge verified the approval and recorded its head;
        the retry reports that head and reads no review, whatever the
        forge says about the review by then."""
        f = self.f
        f.review("approved")
        blocker = f.worktree / "untracked.txt"
        blocker.write_text("retain")
        stopped = self.pr("merge", 3)
        self.assertEqual(stopped["cleanup"][-1]["step"],
                         "implementation worktree")
        self.assertEqual(stopped["approved_head"], self.head)
        self.assertEqual(stopped["merged_head"], self.head)
        blocker.unlink()
        self.edit_review(self.head, state="COMMENTED")
        calls = len(f.read_model()["calls"])
        result = self.cleanup()
        reads = [call_path(c) for c in f.read_model()["calls"][calls:]]
        self.assertFalse(
            any(p == "graphql" or "/reviews" in p for p in reads), reads)
        self.assertIs(result["merged_outside_pr_merge"], False)
        self.assertEqual(result["approved_head"], self.head)
        self.assertEqual(result["merged_head"], self.head)
        self.assertEqual(result["merge_record"]["result"], "deleted")
        self.assertFalse(f.worktree.exists())

    def edit_review(self, head: str, **values: str) -> None:
        """Change the one approving review at ``head`` in the fake model."""
        model = self.f.read_model()
        reviews = [r for r in model["prs"]["1"]["reviews"]
                   if r["commit_id"] == head
                   and "verdict=approved" in r["body"]]
        self.assertEqual(len(reviews), 1)
        reviews[0].update(values)
        self.f.save_model(model)

    def test_approval_tag_without_approved_state_is_not_reported(
        self,
    ) -> None:
        """REV-13: the newer tag sits on a review the forge did not
        record as an approval."""
        f = self.f
        f.review("approved")
        genuine = self.head
        self.head = f.push("value = 1\nsecond = 2\nthird = 3\nmore = 4\n")
        f.review("approved")
        states = ["COMMENTED"] + (["DISMISSED"] if self.dismissed else [])
        merge_commit = self.forge_merge("merge")
        for state in states:
            with self.subTest(state=state):
                self.edit_review(self.head, state=state)
                self.assertIn("forge_state_mismatch", json.dumps(
                    f.cli("status", "--pr", "1")["diagnostics"]))
                with patch.dict(os.environ, f.env, clear=True):
                    repository = load_initialized_repository(f.repo)
                    forge = make_forge(repository, "implementer")
                    self.assertEqual(approved_review_head(
                        forge.snapshot(1), repository.configuration),
                        genuine)
        (f.worktree / "untracked.txt").write_text("retain")
        self.assertEqual(self.cleanup(expected=3)["approved_head"], genuine)
        self.edit_review(self.head, state="APPROVED")
        (f.worktree / "untracked.txt").unlink()
        second = self.cleanup()
        self.assertEqual(second["approved_head"], self.head)
        self.assert_finished(second, merge_commit, "up to date")

    def test_invalid_changes_request_does_not_hide_the_approval(
        self,
    ) -> None:
        """REV-14: a changes_requested review with only an optional
        finding and no NOT FIXED is invalid, so it reserves no ID."""
        f = self.f
        f.review("approved", [finding("optional")])
        first = self.head
        model = f.read_model()
        original = next(r for r in model["prs"]["1"]["reviews"]
                        if r["commit_id"] == first
                        and "verdict=approved" in r["body"])["body"]
        self.edit_review(first, body=original.replace(
            "verdict=approved", "verdict=changes_requested", 1))
        self.head = f.push("value = 1\nsecond = 2\nthird = 3\nmore = 4\n")
        f.review("approved")
        self.edit_review(self.head, body=original.replace(first, self.head))
        status = f.cli("status", "--pr", "1")
        self.assertIn("changes_requested has no blocking finding",
                      json.dumps(status["diagnostics"]))
        self.assertEqual(
            [r["head"] for r in status["reviews"]
             if r["verdict"] == "approved"], [self.head])
        merge_commit = self.forge_merge("merge")
        result = self.cleanup()
        self.assertEqual(result["approved_head"], self.head)
        self.assertEqual(result["merged_head"], self.head)
        self.assert_finished(result, merge_commit)

    def test_malformed_newer_approval_is_not_the_approved_head(self) -> None:
        """REV-11: an approval whose Unanchored findings repeat an entry."""
        f = self.f
        f.review("approved")
        valid = self.head
        self.head = f.push("value = 1\nsecond = 2\nthird = 3\nmore = 4\n")
        f.review("approved", [finding("optional")])
        status = f.cli("status", "--pr", "1")
        root = status["findings"][-1]["root"]["body"]
        entry = root.splitlines()[0]
        model = f.read_model()
        newer = [r for r in model["prs"]["1"]["reviews"]
                 if r["commit_id"] == self.head
                 and "verdict=approved" in r["body"]]
        self.assertEqual(len(newer), 1)
        body = newer[0]["body"].rstrip("\n")
        if "\n## Unanchored findings\n" not in body:
            # On Forgejo the section already carries the entry once.
            body += f"\n\n## Unanchored findings\n\n{entry}\n\nOne."
        newer[0]["body"] = body + f"\n\n{entry}\n\nTwo.\n"
        f.save_model(model)
        self.assertIn("duplicate Unanchored findings entry", json.dumps(
            f.cli("status", "--pr", "1")["diagnostics"]))
        merge_commit = self.forge_merge("merge")
        result = self.cleanup()
        self.assertEqual(result["approved_head"], valid)
        self.assert_finished(result, merge_commit)

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
        admin = Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        self.assertEqual(list(admin.glob("agent-squad-cleanup-*")), [])
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
        with self.subTest(case="Git directory links back elsewhere"):
            # The worktree's Git identity differs: its Git directory names
            # another checkout, so it is not a candidate at all.
            backlink = admin / "gitdir"
            linked = backlink.read_text()
            worktrees = f.git("worktree", "list", "--porcelain")
            writes = self.writes()
            backlink.write_text(str(f.root / "elsewhere" / ".git") + "\n")
            refused = self.cleanup(expected=4)
            backlink.write_text(linked)
            self.assertIn("no implementation record names PR #1",
                          refused["error"])
            self.assertEqual(
                f.git("worktree", "list", "--porcelain"), worktrees)
            self.assertEqual(self.writes(), writes)
            self.assertFalse(self.record.exists())
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
        with self.subTest(case="a detached checkout also names the PR"):
            other = f.worktree.parent / "issue-3"
            f.git("worktree", "add", "--detach", str(other))
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
        admin = Path(f.git("rev-parse", "--absolute-git-dir", cwd=f.worktree))
        saved = json.loads(self.record.read_text())
        marker = Path(saved.pop("implementation_record"))
        self.assertEqual(marker.parent, admin.resolve())
        self.assertRegex(marker.name, r"^agent-squad-cleanup-[0-9a-f]{32}$")
        self.assertTrue(marker.is_file())
        self.assertEqual(saved, {
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
    dismissed = True

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

    def test_closed_reviewer_workspace_does_not_block_a_retry(self) -> None:
        """REV-12: Git refuses the checkout after Herdr closed its
        workspace; the retry finishes once Git allows it."""
        f = self.f
        f.herdr_settings(implementer={"cwd": str(f.worktree)})
        review = Path(f.cli("reviewer", "launch", "--pr", "1")["worktree"])
        model = f.herdr_model()
        workspace = next(w for w in model["workspaces"]
                         if (w.get("worktree") or {}).get("checkout_path")
                         == str(review))
        workspace["worktree"] = None
        f.save_herdr(model)
        f.git("worktree", "lock", str(review))
        merge_commit = self.forge_merge("merge")
        first = self.cleanup(expected=3)
        failed = first["cleanup"][-1]
        self.assertEqual(failed["step"], "review worktree")
        self.assertIn(
            f"Herdr workspace {workspace['workspace_id']} closed",
            failed["detail"])
        self.assertEqual(f.herdr_model()["workspaces"], [])
        self.assertTrue(review.exists())
        f.git("worktree", "unlock", str(review))
        second = self.cleanup()
        self.assertFalse(review.exists())
        self.assert_finished(second, merge_commit, "up to date")

    def unregistered_reviewer(self) -> tuple[Path, dict]:
        f = self.f
        f.herdr_settings(implementer={"cwd": str(f.worktree)})
        review = Path(f.cli("reviewer", "launch", "--pr", "1")["worktree"])
        model = f.herdr_model()
        workspace = next(w for w in model["workspaces"]
                         if (w.get("worktree") or {}).get("checkout_path")
                         == str(review))
        workspace["worktree"] = None
        f.save_herdr(model)
        return review, workspace

    def test_failed_owner_write_after_closure_is_recovered(self) -> None:
        """REV-12: the ownership rewrite fails after Herdr closed the
        workspace; the retry confirms the closure and finishes."""
        f = self.f
        review, workspace = self.unregistered_reviewer()
        merge_commit = self.forge_merge("merge")
        with patch.dict(os.environ, f.env, clear=True), patch(
            "agent_squad.reviewer.ReviewWorktree.save_owner",
            side_effect=OSError("disk full"),
        ):
            repository = load_initialized_repository(f.repo)
            first = cleanup_pr(
                repository, make_forge(repository, "implementer"), 1)
        failed = first["cleanup"][-1]
        self.assertEqual(failed["step"], "review worktree")
        self.assertIn(
            f"Herdr workspace {workspace['workspace_id']} closed; then"
            " disk full", failed["detail"])
        self.assertEqual(f.herdr_model()["workspaces"], [])
        self.assertTrue(review.exists())
        second = self.cleanup()
        closed = [s for s in second["cleanup"]
                  if s["step"] == "review worktree"]
        self.assertEqual(closed[0]["detail"]["workspace"], "already closed")
        self.assertEqual(closed[0]["detail"]["workspace_id"],
                         workspace["workspace_id"])
        self.assertFalse(review.exists())
        self.assert_finished(second, merge_commit, "up to date")

    def test_failed_lookup_keeps_a_listed_workspace(self) -> None:
        """REV-12: a failed workspace lookup removes the checkout only when
        a fresh inventory with valid identities no longer lists it."""
        f = self.f
        review, workspace = self.unregistered_reviewer()
        merge_commit = self.forge_merge("merge")
        stray = {"workspace_id": "", "label": "stray", "tab_count": 0,
                 "pane_count": 0, "worktree": None}
        for name, extra in (("still listed", None),
                            ("invalid identity", stray)):
            with self.subTest(name):
                model = f.herdr_model()
                if extra is not None:
                    model["workspaces"] = [extra]
                    f.save_herdr(model)
                with patch.dict(os.environ, f.env, clear=True), patch(
                    "agent_squad.herdr.HerdrClient.workspace",
                    side_effect=HerdrError("lookup failed"),
                ):
                    repository = load_initialized_repository(f.repo)
                    result = cleanup_pr(
                        repository, make_forge(repository, "implementer"), 1)
                failed = result["cleanup"][-1]
                self.assertEqual(failed["step"], "review worktree")
                self.assertIn("lookup failed", failed["detail"])
                self.assertTrue(review.exists())
        model = f.herdr_model()
        model["workspaces"] = [workspace]
        f.save_herdr(model)
        self.assert_finished(self.cleanup(), merge_commit, "up to date")

    def test_pane_in_the_checkout_keeps_it_on_a_retry(self) -> None:
        """REV-15: an unowned pane works inside the closed Reviewer's
        checkout, in a workspace with no worktree registration."""
        f = self.f
        review, workspace = self.unregistered_reviewer()
        f.git("worktree", "lock", str(review))
        merge_commit = self.forge_merge("merge")
        self.cleanup(expected=3)
        model = f.herdr_model()
        self.assertEqual(model["workspaces"], [])
        model["workspaces"].append({
            "workspace_id": "w-other", "label": "other", "tab_count": 1,
            "pane_count": 1, "active_tab_id": "w-other:t1",
            "worktree": None,
        })
        model["tabs"].append(
            {"workspace_id": "w-other", "tab_id": "w-other:t1"})
        model["panes"].append({
            "workspace_id": "w-other", "tab_id": "w-other:t1",
            "pane_id": "w-other:p1", "terminal_id": "w-other-terminal",
            "cwd": str(review / "src"), "agent": None,
            "agent_status": "unknown",
        })
        f.save_herdr(model)
        f.git("worktree", "unlock", str(review))
        kept = self.cleanup(expected=3)
        self.assertEqual(kept["cleanup"][-1]["step"], "review worktree")
        self.assertIn("an unowned Herdr pane still uses this worktree",
                      kept["cleanup"][-1]["detail"])
        self.assertTrue(review.exists())
        model = f.herdr_model()
        for key in ("workspaces", "tabs", "panes"):
            model[key] = [x for x in model[key]
                          if x["workspace_id"] != "w-other"]
        f.save_herdr(model)
        self.assert_finished(self.cleanup(), merge_commit, "up to date")

    def test_pr_merge_names_cleanup_despite_failing_branch_rules(
        self,
    ) -> None:
        """REV-16: the merged-PR refusal comes before the rules read."""
        f = self.f
        self.forge_merge("merge")
        f.settings(protection="incomplete branch rule response")
        calls = len(f.read_model()["calls"])
        writes = self.writes()
        refused = self.pr("merge", 4)
        self.assertIn("merged outside pr merge", refused["error"])
        self.assertIn(COMMAND, refused["error"])
        self.assertEqual(self.writes(), writes)
        self.assertFalse(any(
            "protection" in call_path(c) or "/rules/" in call_path(c)
            for c in f.read_model()["calls"][calls:]))

    def break_conversation(self) -> None:
        f = self.f
        model = f.read_model()
        model["prs"]["1"]["conversation"] = [{
            "id": 12345, "user": {"login": "contributor"},
            "body": "Maintenance note",
            "created_at": "2026-11-25T02:00:00Z",
        }]
        f.save_model(model)
        # A page that never advances: the adapter refuses a repeated cursor.
        f.settings(page_sizes={"PullRequest.comments": 0})

    def test_replacement_checkout_is_refused_before_any_deletion(
        self,
    ) -> None:
        """REV-17: with the remote branch still present, the ownership step
        refuses the replacement before the first deleting step."""
        f = self.f
        f.settings(branch_delete_403=True)
        merge_commit = self.forge_merge("merge")
        first = self.cleanup(expected=3)
        self.assertEqual(first["cleanup"][-1]["step"], "remote branch")
        f.settings(branch_delete_403=False)
        parked, marker = self.replace_checkout()
        self.assert_replacement_refused(parked, marker)
        self.assertNotEqual(
            f.git("ls-remote", "--heads", "origin", "issue-1"), "")
        self.restore_checkout(parked)
        self.assert_finished(self.cleanup(), merge_commit, "up to date")

    def pane_keeps_the_checkout(self, place) -> None:
        """A retry after the Reviewer was closed keeps its checkout while
        an unowned pane works in the directory ``place`` makes for it;
        ``place`` returns that directory and the refusal to expect."""
        f = self.f
        review, workspace = self.unregistered_reviewer()
        f.git("worktree", "lock", str(review))
        merge_commit = self.forge_merge("merge")
        self.cleanup(expected=3)
        f.git("worktree", "unlock", str(review))
        cwd, refusal = place(review)
        model = f.herdr_model()
        self.assertEqual(model["workspaces"], [])
        model["workspaces"].append({
            "workspace_id": "w-stale", "label": "stale", "tab_count": 1,
            "pane_count": 1, "active_tab_id": "w-stale:t1",
            "worktree": None,
        })
        model["tabs"].append(
            {"workspace_id": "w-stale", "tab_id": "w-stale:t1"})
        model["panes"].append({
            "workspace_id": "w-stale", "tab_id": "w-stale:t1",
            "pane_id": "w-stale:p1", "terminal_id": "w-stale-terminal",
            "cwd": str(cwd), "agent": None, "agent_status": "unknown",
        })
        f.save_herdr(model)
        kept = self.cleanup(expected=3)
        self.assertEqual(kept["cleanup"][-1]["step"], "review worktree")
        self.assertIn(refusal, kept["cleanup"][-1]["detail"])
        self.assertTrue(review.exists())
        self.assertTrue(self.record.exists())
        model = f.herdr_model()
        for key in ("workspaces", "tabs", "panes"):
            model[key] = [x for x in model[key]
                          if x["workspace_id"] != "w-stale"]
        f.save_herdr(model)
        self.assert_finished(self.cleanup(), merge_commit, "up to date")

    def test_unresolvable_pane_directory_keeps_the_checkout(self) -> None:
        """REV-18: a pane whose directory leads through a symlink loop
        leaves the checkout's use unknown; the step fails with a report."""
        def place(review: Path) -> tuple[Path, str]:
            loop = self.f.root / "stale-pane-directory"
            loop.symlink_to(loop.name)
            return loop, f"cannot resolve Herdr pane directory {loop}"

        self.pane_keeps_the_checkout(place)

    @unittest.skipIf(os.geteuid() == 0, "root reads every directory")
    def test_inaccessible_pane_directory_keeps_the_checkout(self) -> None:
        """REV-18: the pane's directory is another name for the checkout,
        below a parent that cannot be searched, so it cannot be placed."""
        def place(review: Path) -> tuple[Path, str]:
            blocked = self.f.root / "inaccessible-pane-parent"
            blocked.mkdir()
            alias = blocked / "pane-directory"
            alias.symlink_to(review, target_is_directory=True)
            self.assertTrue(alias.samefile(review))
            blocked.chmod(0)
            self.addCleanup(blocked.chmod, 0o700)
            with self.assertRaises(PermissionError):
                alias.stat()
            return alias, f"cannot resolve Herdr pane directory {alias}"

        # Registered before the fixture's own cleanup, so it runs after
        # the permissions are restored.
        self.pane_keeps_the_checkout(place)
        (self.f.root / "inaccessible-pane-parent").chmod(0o700)

    @unittest.skipIf(os.geteuid() == 0, "root reads every directory")
    def test_pane_directory_through_an_unreadable_target_keeps_the_checkout(
        self,
    ) -> None:
        """REV-18: the pane's directory is a plain path whose symlink
        target climbs out of a directory reached through one that cannot
        be searched. A reading that skips that part ends at a readable
        directory elsewhere."""
        def place(review: Path) -> tuple[Path, str]:
            blocked = self.f.root / "blocked"
            blocked.mkdir()
            target = review / "deep" / "deeper"
            target.mkdir(parents=True)
            internal = blocked / "internal"
            internal.symlink_to(target)
            reported = self.f.root / "pane-directory"
            reported.symlink_to(internal / ".." / "..")
            self.assertTrue(reported.samefile(review))
            blocked.chmod(0)
            self.addCleanup(blocked.chmod, 0o700)
            with self.assertRaises(PermissionError):
                reported.stat()
            return reported, f"cannot resolve Herdr pane directory {reported}"

        self.pane_keeps_the_checkout(place)
        (self.f.root / "blocked").chmod(0o700)

    def pane_directory_past_a_changed_name(self, become, error) -> None:
        """REV-18: the pane's directory is a link to ``hop/../leaf``, and
        ``hop`` led into the checkout, so the pane worked in the checkout's
        ``leaf``. ``become`` then makes ``hop`` something the operating
        system does not read through, and reading the pane's directory
        fails with ``error``. Taking ``hop/..`` off the text instead names
        the readable ``leaf`` beside the link, outside the checkout."""
        def place(review: Path) -> tuple[Path, str]:
            root = self.f.root
            (review / "sub").mkdir()
            (review / "leaf").mkdir()
            (root / "leaf").mkdir()
            hop = root / "hop"
            hop.symlink_to(review / "sub", target_is_directory=True)
            reported = root / "pane-directory"
            reported.symlink_to("hop/../leaf", target_is_directory=True)
            self.assertTrue(reported.samefile(review / "leaf"))
            hop.unlink()
            become(hop)
            with self.assertRaises(error):
                reported.stat()
            return reported, f"cannot resolve Herdr pane directory {reported}"

        self.pane_keeps_the_checkout(place)

    @unittest.skipIf(os.geteuid() == 0, "root reads every directory")
    def test_pane_directory_past_an_unsearchable_name_keeps_the_checkout(
        self,
    ) -> None:
        def become(hop: Path) -> None:
            hop.mkdir()
            hop.chmod(0)
            self.addCleanup(hop.chmod, 0o700)

        self.pane_directory_past_a_changed_name(become, PermissionError)
        (self.f.root / "hop").chmod(0o700)

    def test_pane_directory_past_a_file_keeps_the_checkout(self) -> None:
        self.pane_directory_past_a_changed_name(
            Path.touch, NotADirectoryError)

    def test_pane_directory_that_became_a_file_keeps_the_checkout(
        self,
    ) -> None:
        """REV-18: the pane's directory is a link to a directory in the
        checkout that a file replaced. A working directory cannot be a
        file, so the pane is not placed beside the link."""
        def place(review: Path) -> tuple[Path, str]:
            target = review / "sub"
            target.mkdir()
            reported = self.f.root / "pane-directory"
            reported.symlink_to(target, target_is_directory=True)
            self.assertTrue(reported.samefile(target))
            target.rmdir()
            target.touch()
            return reported, f"cannot resolve Herdr pane directory {reported}"

        self.pane_keeps_the_checkout(place)

    def test_pane_directory_whose_target_is_gone_keeps_the_checkout(
        self,
    ) -> None:
        """REV-18: the pane's directory is a link to a directory in the
        checkout that was removed. The link's own name is not absent, so
        the pane is not placed beside the link."""
        def place(review: Path) -> tuple[Path, str]:
            target = review / "sub"
            target.mkdir()
            reported = self.f.root / "pane-directory"
            reported.symlink_to(target, target_is_directory=True)
            self.assertTrue(reported.samefile(target))
            target.rmdir()
            return reported, f"cannot resolve Herdr pane directory {reported}"

        self.pane_keeps_the_checkout(place)

    def test_pane_directory_linked_below_the_checkout_keeps_it(self) -> None:
        """Control for the cases above: while every name can be read, a
        link whose target climbs with ``..`` to a directory below the
        checkout is in the checkout, although its text names a directory
        beside the link."""
        def place(review: Path) -> tuple[Path, str]:
            root = self.f.root
            (review / "sub").mkdir()
            (review / "leaf").mkdir()
            (root / "leaf").mkdir()
            (root / "hop").symlink_to(
                review / "sub", target_is_directory=True)
            reported = root / "pane-directory"
            reported.symlink_to("hop/../leaf", target_is_directory=True)
            self.assertTrue(reported.samefile(review / "leaf"))
            return reported, "an unowned Herdr pane still uses this worktree"

        self.pane_keeps_the_checkout(place)

    def test_pane_directory_that_is_gone_does_not_keep_the_checkout(
        self,
    ) -> None:
        """Control: a pane whose directory outside the checkout was
        removed is still placed by its name, and the retry finishes."""
        f = self.f
        review, workspace = self.unregistered_reviewer()
        f.git("worktree", "lock", str(review))
        merge_commit = self.forge_merge("merge")
        self.cleanup(expected=3)
        f.git("worktree", "unlock", str(review))
        model = f.herdr_model()
        model["workspaces"].append({
            "workspace_id": "w-stale", "label": "stale", "tab_count": 1,
            "pane_count": 1, "active_tab_id": "w-stale:t1",
            "worktree": None,
        })
        model["tabs"].append(
            {"workspace_id": "w-stale", "tab_id": "w-stale:t1"})
        model["panes"].append({
            "workspace_id": "w-stale", "tab_id": "w-stale:t1",
            "pane_id": "w-stale:p1", "terminal_id": "w-stale-terminal",
            "cwd": str(f.root / "removed" / "directory"), "agent": None,
            "agent_status": "unknown",
        })
        f.save_herdr(model)
        self.assert_finished(self.cleanup(), merge_commit, "up to date")
        self.assertFalse(review.exists())

    def test_pane_directory_in_another_case_keeps_the_checkout(self) -> None:
        """REV-18: on a case-insensitive file system, another spelling of
        the checkout's name is the same directory."""
        def place(review: Path) -> tuple[Path, str]:
            shouted = review.parent / review.name.upper()
            if not shouted.exists():
                self.skipTest("the file system is case-sensitive")
            # A directory below it that is gone: only an ancestor is the
            # same directory as the checkout.
            return (shouted / "gone" / "deeper",
                    "an unowned Herdr pane still uses this worktree")

        self.pane_keeps_the_checkout(place)

    def test_moved_original_checkout_still_blocks_a_retry(self) -> None:
        """REV-10 control: the proved record survives a moved checkout."""
        f = self.f
        f.settings(branch_delete_403=True)
        self.forge_merge("merge")
        first = self.cleanup(expected=3)
        self.assertEqual(first["cleanup"][-1]["step"], "remote branch")
        moved = f.root / "moved-implementation"
        f.git("worktree", "move", str(f.worktree), str(moved))
        f.git("switch", "--detach", cwd=moved)
        f.settings(branch_delete_403=False)
        second = self.cleanup(expected=3)
        self.assertEqual(second["cleanup"][-1]["step"],
                         "implementation ownership")
        self.assertIn("ownership record remains",
                      second["cleanup"][-1]["detail"])
        self.assertTrue(moved.exists())
        self.assertTrue(self.record.exists())

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
        self.assertEqual(result["merged_head"], self.head)
        self.assertEqual(result["forge_head"], self.head)
        self.assertEqual(result["forge_head_branch"], "issue-1")

    def test_pr_merge_by_squash_reports_the_head_as_merged(self) -> None:
        self.configure("squash")
        self.f.review("approved")
        result = self.pr("merge", 0)
        self.assertEqual(result["integration"], "verified by tree identity")
        self.assertIs(result["merged_outside_pr_merge"], False)
        self.assertEqual(result["approved_head"], self.head)
        self.assertEqual(result["merged_head"], self.head)
        self.assertEqual(result["forge_head"], self.head)

    def test_record_from_an_earlier_version_loads_as_pr_merge(self) -> None:
        f = self.f
        f.review("approved")
        f.settings(branch_delete_403=True)
        stopped = self.pr("merge", 3)
        self.assertIs(stopped["merged_outside_pr_merge"], False)
        record = json.loads(self.record.read_text())
        self.assertIs(record.pop("outside_pr_merge"), False)
        self.assertIsNone(record.pop("implementation_record"))
        self.record.write_text(json.dumps(record))
        f.settings(branch_delete_403=False)
        # Such a record is pr merge's, so its head is the approved one
        # whatever happened to the review since.
        self.edit_review(self.head, state="DISMISSED")
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
        owner = str(self.f.repo / ".git/worktrees/issue-1"
                    / "agent-squad-implementation.json")
        marker = "agent-squad-cleanup-" + "0" * 32
        climbing = str(self.f.repo / ".git/worktrees/.." / marker)
        slots = record["common"] + "/worktrees"
        # The saved text is checked as written: a path object would drop
        # the "." and the doubled separator, and read a NUL as absent.
        malformed = [
            f"{slots}/./issue-1/{marker}", f"{slots}/issue-1/./{marker}",
            f"{slots}/a/../issue-1/{marker}", f"{slots}//issue-1/{marker}",
            f"{slots}/issue-1//{marker}", f"{slots}/issue-1/{marker}/",
            f"{slots}/issue-1/{marker}/.", f"{slots}/issue-1/x/{marker}",
            f"{slots}/issue\x00-1/{marker}", f"{slots}/issue-1/{marker}\x00",
            f"{slots}/issue-1/{marker}\n", f"{slots}/issue-\ud800/{marker}",
            f"{slots}/issue-1/{marker.upper()}", f"{slots}/{marker}",
            f"worktrees/issue-1/{marker}", f"/{slots}/issue-1/{marker}",
            ["x"],
        ]
        for values in (
            {"outside_pr_merge": "true"}, {"outside_pr_merge": 1},
            {"outside_pr_merge": None},
            {"outside_pr_merge": True},
            {"outside_pr_merge": True, "implementation_record": "x.json"},
            {"outside_pr_merge": True, "implementation_record": owner},
            {"outside_pr_merge": True, "implementation_record": climbing},
            *({"outside_pr_merge": True, "implementation_record": value}
              for value in malformed),
            {"implementation_record": owner},
            {"head_branch": "issue\x00-1"}, {"base_branch": "main\x00"},
            {"head_branch": "issue-\ud800"}, {"base_branch": ""},
        ):
            with self.subTest(values=values):
                self.record.write_text(json.dumps(dict(record, **values)))
                calls = len(f.read_model()["calls"])
                result = self.cleanup(expected=1)
                self.assertIn("malformed merge record", result["error"])
                self.assertEqual(len(f.read_model()["calls"]), calls)
        self.assertTrue(f.worktree.exists())
        # The well-formed text is accepted: the record loads, and the
        # ownership step then finds that this checkout holds no marker.
        self.record.write_text(json.dumps(dict(
            record, outside_pr_merge=True,
            implementation_record=f"{slots}/issue-1/{marker}")))
        accepted = self.cleanup(expected=3)
        self.assertIn("cleanup marker", accepted["cleanup"][0]["detail"])


class ForgejoForgeMergedCleanupTests(ForgeMergedCleanup, unittest.TestCase):
    fixture = ForgejoFixture
    forge_deletes_branch = True

    def break_conversation(self) -> None:
        self.f.settings(conversation_read_status=500)

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
