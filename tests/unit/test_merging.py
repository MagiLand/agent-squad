"""The fixed approval, moved-base, and integration rules."""

from tests.unit.test_conventions import A, H
from agent_squad.merging import check_merge_gate, verify_integration
from agent_squad.initialization import AgentSquadError, GateError, Worktree
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from tests._support import add_src_to_path

add_src_to_path()

IMPLEMENTATION = Worktree(Path("/owned"), H, "refs/heads/feature")


def cleanup_repository():
    from types import SimpleNamespace

    return SimpleNamespace(
        primary=Path("/primary"),
        common=Path("/primary/.git"),
        configuration=SimpleNamespace(worktree_root="worktrees"),
        resolve_root=lambda value: Path("/primary") / value,
    )


class MergeRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "approval": {"reasons": [], "approved": True},
            "pr": {"state": "open", "merged": False},
            "target": {"base_tip": A},
            "reviews": [{"base": A}],
            "gates": {"unaddressed_findings": False},
            "unaddressed_findings": [],
        }

    def test_hold_acceptance_preserves_approval_and_moved_base_gates(
        self,
    ) -> None:
        self.state["merge_hold"] = {
            "review_id": 42, "text": "Item 3: rules", "released": True,
            "release": {"id": 43, "author": "dev", "created_at": "t"},
        }
        with self.assertRaisesRegex(GateError, "review 42"):
            check_merge_gate(self.state, accept_moved_base=False)
        self.assertFalse(check_merge_gate(
            self.state, accept_moved_base=False, accept_merge_hold=True))
        self.state["target"]["base_tip"] = H
        with self.assertRaisesRegex(GateError, "base branch moved"):
            check_merge_gate(self.state, accept_moved_base=False,
                             accept_merge_hold=True)
        self.state["approval"]["reasons"] = ["approval is stale"]
        with self.assertRaisesRegex(GateError, "approval is stale"):
            check_merge_gate(self.state, accept_moved_base=True,
                             accept_merge_hold=True)

    def test_accepting_a_hold_requires_its_recorded_release(self) -> None:
        self.state["merge_hold"] = {
            "review_id": 42, "text": "Item 3: rules", "released": False,
            "release": None,
        }
        for accept in (False, True):
            with self.subTest(accept_merge_hold=accept):
                with self.assertRaisesRegex(GateError, "review 42") as raised:
                    check_merge_gate(self.state, accept_moved_base=False,
                                     accept_merge_hold=accept)
                self.assertEqual(
                    "hold-release decision newer than that review: record"
                    " the Developer's release with decision post --finding"
                    " none --kind hold-release --recommendation"
                    " <followed|changed|none>"
                    in str(raised.exception), accept)

    def test_moved_base_is_the_approving_base_not_the_recomputed_merge_base(
        self,
    ) -> None:
        self.assertFalse(check_merge_gate(self.state, accept_moved_base=False))
        self.state["target"].update(base=A, base_tip=H)
        with self.assertRaisesRegex(GateError, "base branch moved"):
            check_merge_gate(self.state, accept_moved_base=False)
        self.assertTrue(check_merge_gate(self.state, accept_moved_base=True))

    def test_unaddressed_optional_threads_refuse_merge_and_name_each_id(
        self,
    ) -> None:
        self.state["gates"]["unaddressed_findings"] = True
        self.state["unaddressed_findings"] = ["REV-1", "REV-3"]
        for accept in (False, True):
            with self.subTest(accept_moved_base=accept):
                with self.assertRaisesRegex(
                    GateError, "unaddressed_findings: REV-1, REV-3"
                ):
                    check_merge_gate(self.state, accept_moved_base=accept)

    def test_approval_reasons_and_closed_pr_are_refused_even_with_flag(
        self,
    ) -> None:
        for mutation, reason in [
            ({"approval": {"reasons": [
             "Task was amended after the review"]}}, "Task"),
            ({"pr": {"state": "closed", "merged": False}}, "not open"),
            ({"pr": {"state": "closed", "merged": True}}, "not open"),
        ]:
            with self.subTest(mutation=mutation):
                state = deepcopy(self.state)
                state.update(mutation)
                with self.assertRaisesRegex(GateError, reason):
                    check_merge_gate(state, accept_moved_base=True)

    def test_squash_requires_unmoved_base_and_identical_tree(self) -> None:
        with patch("agent_squad.merging.is_ancestor", return_value=True):
            with patch("agent_squad.merging.git_output", side_effect=[A, A]):
                self.assertEqual(
                    verify_integration(None, "squash", H, A,
                                       A, moved_base=False),
                    "verified by tree identity",
                )
            with patch("agent_squad.merging.git_output", side_effect=[A, H]):
                with self.assertRaisesRegex(AgentSquadError, "tree differs"):
                    verify_integration(None, "squash", H, A,
                                       A, moved_base=False)
            with patch("agent_squad.merging.git_output") as git:
                with self.assertRaisesRegex(AgentSquadError, "not verifiable"):
                    verify_integration(None, "squash", H, A,
                                       A, moved_base=True)
                git.assert_not_called()

    def test_merge_requires_both_head_and_base_integration_ancestry(
        self,
    ) -> None:
        for ancestry, message in [
            ([False], "not an ancestor of base"),
            ([True, False], "approved head is not in merge ancestry"),
        ]:
            with self.subTest(ancestry=ancestry):
                with patch(
                    "agent_squad.merging.is_ancestor", side_effect=ancestry
                ):
                    with self.assertRaisesRegex(AgentSquadError, message):
                        verify_integration(
                            None, "merge", H, A, A, moved_base=False)
        with patch("agent_squad.merging.is_ancestor", return_value=True):
            self.assertEqual(
                verify_integration(None, "merge", H, A, A, moved_base=True),
                "verified by ancestry",
            )


class TrackingCleanupTests(unittest.TestCase):
    def test_remote_recreation_refuses_before_local_ref_removal(self):
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from agent_squad.merging import cleanup_merge

        repository = cleanup_repository()
        forge = Mock()
        forge.branch_head.return_value = H
        with patch("agent_squad.merging.owned_implementation",
                   return_value=(Path("/owned"), 1)), patch(
                       "agent_squad.merging.list_worktrees",
                       return_value=[IMPLEMENTATION]), patch(
                       "agent_squad.merging.run_git") as git:
            result = cleanup_merge(repository, 1, H, "feature", 1, forge)
        self.assertEqual(result[-1]["step"], "remote-tracking ref")
        self.assertFalse(result[-1]["ok"])
        self.assertIn("remote branch remains", result[-1]["detail"])
        git.assert_not_called()

    def test_compare_and_delete_uses_approved_sha_and_stops_on_a_race(self):
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock
        from agent_squad.merging import cleanup_merge

        repository = cleanup_repository()
        forge = Mock()
        forge.branch_head.return_value = None
        with patch("agent_squad.merging.owned_implementation",
                   return_value=(Path("/owned"), 1)), patch(
                       "agent_squad.merging.list_worktrees",
                       return_value=[IMPLEMENTATION]), patch(
                       "agent_squad.merging.run_git",
                       return_value=SimpleNamespace(returncode=0)), patch(
                           "agent_squad.merging.git_output",
                           side_effect=AgentSquadError("ref moved")) as git:
            result = cleanup_merge(repository, 1, H, "feature", 1, forge)
        git.assert_called_once_with(
            Path("/primary"), "update-ref", "--no-deref", "-d",
            "refs/remotes/origin/feature", H,
        )
        self.assertEqual(result[-1]["step"], "remote-tracking ref")
        self.assertFalse(result[-1]["ok"])
        self.assertEqual(result[-1]["detail"], "ref moved")

    def test_ownership_of_another_issue_stops_before_any_deletion(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from agent_squad.merging import cleanup_merge

        forge = Mock()
        with patch("agent_squad.merging.owned_implementation",
                   return_value=(Path("/owned"), 2)), patch(
                       "agent_squad.merging.list_worktrees",
                       return_value=[IMPLEMENTATION]):
            result = cleanup_merge(
                cleanup_repository(), 1, H, "feature",
                1, forge,
            )
        self.assertEqual(result, [{
            "step": "implementation ownership", "ok": False,
            "detail": "implementation ownership names issue #2, the merge"
            " record #1: /owned",
        }])
        forge.delete_branch.assert_not_called()


class CleanupMarkerTests(unittest.TestCase):
    """#175: the marker goes into the proved Git directory, held open."""

    def setUp(self) -> None:
        import os
        from tempfile import TemporaryDirectory
        from agent_squad.merging import DIRECTORY_FLAGS

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "worktrees" / "issue-1"
        self.directory.mkdir(parents=True)
        self.descriptor = os.open(self.directory, DIRECTORY_FLAGS)
        self.addCleanup(os.close, self.descriptor)

    def markers(self, *directories: Path) -> list[Path]:
        return [m for d in directories or (self.directory,)
                for m in d.glob("agent-squad-cleanup-*")]

    def refused(self, descriptor: int | None = -1) -> None:
        from agent_squad.initialization import RetainedError
        from agent_squad.merging import create_cleanup_marker

        with self.assertRaisesRegex(
            RetainedError, "^implementation Git directory changed$",
        ):
            create_cleanup_marker(
                self.descriptor if descriptor == -1 else descriptor,
                self.directory,
            )

    def test_marker_is_created_in_the_directory_held_open(self) -> None:
        from agent_squad.merging import (
            create_cleanup_marker, remove_cleanup_marker, same_directory,
        )

        self.assertTrue(same_directory(self.descriptor, self.directory))
        marker = create_cleanup_marker(self.descriptor, self.directory)
        self.assertEqual(self.markers(), [marker])
        self.assertRegex(marker.name, r"^agent-squad-cleanup-[0-9a-f]{32}$")
        self.assertTrue(marker.is_file() and not marker.is_symlink())
        self.assertEqual(marker.read_bytes(), b"")
        self.assertNotEqual(
            create_cleanup_marker(self.descriptor, self.directory), marker)
        remove_cleanup_marker(self.descriptor, marker.name)
        self.assertFalse(marker.exists())
        # Taking back a marker that is already gone is not an error.
        remove_cleanup_marker(self.descriptor, marker.name)

    def test_directory_put_in_its_place_gets_no_marker(self) -> None:
        from agent_squad.merging import same_directory

        parked = self.root / "parked"
        self.directory.rename(parked)
        self.assertFalse(same_directory(self.descriptor, self.directory))
        self.refused()
        self.directory.mkdir()
        self.assertFalse(same_directory(self.descriptor, self.directory))
        self.refused()
        # A symlink to the proved directory is not that directory.
        self.directory.rmdir()
        self.directory.symlink_to(parked)
        self.assertFalse(same_directory(self.descriptor, self.directory))
        self.refused()
        self.assertEqual(self.markers(parked), [])
        self.refused(None)

    def test_removed_directory_gets_no_marker(self) -> None:
        self.directory.rmdir()
        self.refused()

    def test_marker_is_taken_back_when_the_directory_changes_meanwhile(
        self,
    ) -> None:
        """Replaced between the check and the creation: the marker goes
        into the directory held open, never into the one now at the path,
        and is removed again."""
        from agent_squad import merging

        parked = self.root / "parked"
        real = merging.same_directory
        seen = []

        def replaced_after_the_check(descriptor: int, path: Path) -> bool:
            same = real(descriptor, path)
            if not seen:
                self.directory.rename(parked)
                self.directory.mkdir()
            seen.append((same, len(self.markers(parked)),
                         len(self.markers(self.directory))))
            return same

        with patch("agent_squad.merging.same_directory",
                   side_effect=replaced_after_the_check):
            self.refused()
        # Checked: the same directory. Rechecked: not the same, with the
        # marker in the directory held open and none in its replacement.
        self.assertEqual(seen, [(True, 0, 0), (False, 1, 0)])
        self.assertEqual(self.markers(parked, self.directory), [])


class SavedMarkerPathTests(unittest.TestCase):
    """#175: the saved marker path is checked as text, when it is read."""

    def setUp(self) -> None:
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.common = Path(temporary.name).resolve()
        self.repository = SimpleNamespace(common=self.common)
        self.name = "agent-squad-cleanup-" + "0123456789abcdef" * 2
        self.slots = f"{self.common}/worktrees"

    def accepted(self, value: object) -> bool:
        from agent_squad.merging import marker_in_worktree_slot

        return marker_in_worktree_slot(self.repository, value)

    def test_exact_marker_path_is_accepted(self) -> None:
        for slot in ("issue-1", "issue-11", "a b", "...", "issue-1.lock"):
            with self.subTest(slot=slot):
                self.assertTrue(
                    self.accepted(f"{self.slots}/{slot}/{self.name}"))

    def test_any_other_text_is_refused(self) -> None:
        slots, name = self.slots, self.name
        for value in (
            f"{slots}/./issue-1/{name}", f"{slots}/issue-1/./{name}",
            f"{slots}/../worktrees/issue-1/{name}",
            f"{slots}/a/../issue-1/{name}", f"{slots}/../{name}",
            f"{slots}/./{name}", f"{slots}//{name}", f"{slots}/{name}",
            f"{slots}//issue-1/{name}", f"{slots}/issue-1//{name}",
            f"{slots}/issue-1/{name}/", f"{slots}/issue-1/{name}/.",
            f"{slots}/issue-1/x/{name}", f"{slots}/issue\x00-1/{name}",
            f"\x00{slots}/issue-1/{name}", f"{slots}/issue-1/{name}\x00",
            f"{slots}/issue-1/{name}\n", f"{slots}/issue-1/ {name}",
            f"{slots}/issue-\ud800/{name}", f"{slots}/issue-1/{name[:-1]}",
            f"{slots}/issue-1/{name}0", f"{slots}/issue-1/{name.upper()}",
            f"{slots}/issue-1/agent-squad-implementation.json",
            f"{self.common}/worktrees-2/issue-1/{name}",
            f"{self.common}/issue-1/{name}", f"worktrees/issue-1/{name}",
            f"/{slots}/issue-1/{name}", f"{slots}/issue-1", "", None, 1,
            Path(f"{slots}/issue-1/{name}"), [f"{slots}/issue-1/{name}"],
        ):
            with self.subTest(value=value):
                self.assertFalse(self.accepted(value))

    def test_saved_text_must_be_able_to_name_a_file(self) -> None:
        from agent_squad.merging import file_name_text

        for value in ("main", "feature/issue-1", "a b", "é"):
            with self.subTest(value=value):
                self.assertTrue(file_name_text(value))
        for value in ("", "a\x00b", "\x00", "x\ud800", None, 1, b"main",
                      ["main"]):
            with self.subTest(value=value):
                self.assertFalse(file_name_text(value))

    def test_marker_is_gone_only_when_its_name_is_absent(self) -> None:
        from agent_squad.merging import entry_remains

        slot = self.common / "worktrees" / "issue-1"
        slot.mkdir(parents=True)
        marker = slot / self.name
        self.assertFalse(entry_remains(marker))
        marker.touch()
        self.assertTrue(entry_remains(marker))
        marker.unlink()
        # A marker that is a symlink, even to nothing, is still there.
        marker.symlink_to(slot / "absent")
        self.assertTrue(entry_remains(marker))
        marker.unlink()
        slot.rmdir()
        self.assertFalse(entry_remains(marker))
        # The worktree directory replaced by a file: no marker in it.
        slot.write_text("x")
        self.assertFalse(entry_remains(marker))
        slot.unlink()
        # A directory that cannot be read says nothing: the step fails.
        slot.symlink_to(slot.name)
        with self.assertRaises(OSError):
            entry_remains(marker)

    def test_unreadable_marker_fails_the_ownership_step(self) -> None:
        from unittest.mock import Mock
        from agent_squad.merging import cleanup_merge

        repository = cleanup_repository()
        repository.common = self.common
        slot = self.common / "worktrees" / "issue-1"
        slot.parent.mkdir()
        slot.symlink_to(slot.name)
        forge = Mock()
        with patch("agent_squad.merging.list_worktrees", return_value=[]):
            steps = cleanup_merge(
                repository, 2, H, "feature", 1, forge,
                cleanup_marker=slot / self.name,
            )
        self.assertEqual([s["step"] for s in steps],
                         ["implementation ownership"])
        self.assertFalse(steps[0]["ok"])
        self.assertIn("symbolic links", steps[0]["detail"])
        forge.delete_branch.assert_not_called()


class CompleteMergeTests(unittest.TestCase):
    def test_merged_pr_without_merge_commit_is_not_verified(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from agent_squad.merging import complete_merge

        record = {"pr": 1, "base_branch": "main"}
        with patch("agent_squad.merging.git_output") as git:
            result = complete_merge(
                SimpleNamespace(primary=Path("/primary")), Mock(), record,
                None, {"merge_record": {"result": "kept"}},
            )
        git.assert_not_called()
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["integration"],
                         "the merged PR reports no merge commit")
        self.assertEqual(result["cleanup"], [])
        self.assertEqual(result["merge_record"], {"result": "kept"})
        self.assertEqual(result["cleanup_command"],
                         "agent-squad pr cleanup --as implementer --pr 1")

    def test_failed_record_deletion_keeps_cleanup_incomplete(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from agent_squad.merging import complete_merge

        record = {
            "pr": 1, "base_branch": "main", "merge_method": "merge",
            "head": H, "head_branch": "feature", "moved_base": False,
            "issue": 1, "outside_pr_merge": False,
            "implementation_record": None,
        }
        path = Mock()
        path.unlink.side_effect = OSError("record busy")
        repository = SimpleNamespace(
            primary=Path("/primary"),
            configuration=SimpleNamespace(base_branch="main"),
        )
        with patch("agent_squad.merging.git_output", return_value=A), patch(
            "agent_squad.merging.verify_integration",
            return_value="verified by ancestry",
        ), patch(
            "agent_squad.merging.cleanup_merge",
            return_value=[{"step": "s", "ok": True, "detail": "done"}],
        ), patch("agent_squad.merging.fast_forward_primary"), patch(
            "agent_squad.merging.merge_record_path", return_value=path,
        ):
            result = complete_merge(
                repository, Mock(), record, A,
                {"merge_record": {"result": "kept", "reason": None}},
            )
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["merge_record"],
                         {"result": "kept", "reason": "record busy"})
        self.assertEqual(result["cleanup_command"],
                         "agent-squad pr cleanup --as implementer --pr 1")


class MergeRecordRefusalTests(unittest.TestCase):
    def merge(
        self, *, merged: bool, error: Exception | None = None,
        snapshot_error: Exception | None = None,
        pr_reads: list | None = None,
    ):
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import Mock
        from agent_squad.forge import Forge
        from agent_squad.merging import merge_pr

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        common = Path(temporary.name)
        (common / "agent-squad-merge-pr1.json").write_text("{}")
        repository = SimpleNamespace(
            common=common,
            configuration=SimpleNamespace(merge_method="merge"),
        )
        forge = Mock(spec=Forge)
        forge.can_read_branch_rules = False
        forge.merge.side_effect = error
        # merge_pr refuses a merged PR from the PR read alone, before the
        # snapshot (#175).
        forge.pr.return_value.merged = merged
        forge.snapshot.return_value.pr.merged = merged
        forge.snapshot.side_effect = snapshot_error
        if pr_reads is not None:
            forge.pr.side_effect = pr_reads
        self.forge = forge
        state = {"pr": {"merged": merged}, "target": {"head": H},
                 "closes_issue": True, "reviews": []}
        gate = (
            {"return_value": False} if error
            else {"side_effect": GateError("pr merge refused: x")}
        )
        with patch("agent_squad.merging.state_for", return_value=state), \
                patch("agent_squad.merging.check_merge_gate", **gate), \
                patch("agent_squad.merging.write_merge_record"):
            return merge_pr(repository, forge, 1), common

    def test_merged_pr_is_refused_from_the_pr_read_alone(self):
        """#175: a merged PR is refused before the branch rules and the
        snapshot, whose reads of reviews and conversation may fail."""
        from agent_squad.forge import ForgeError

        with self.assertRaises(GateError) as caught:
            self.merge(merged=True, snapshot_error=ForgeError("unreadable"))
        self.assertIn("pr cleanup --as implementer --pr 1",
                      str(caught.exception))
        self.forge.pr.assert_called_once_with(1)
        self.forge.snapshot.assert_not_called()
        self.forge.branch_rules.assert_not_called()

    def test_pr_merged_during_the_rules_read_is_refused(self):
        """#175: the snapshot read after the branch rules is checked for
        the merged state before any live state is derived."""
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import Mock
        from agent_squad.forge import Forge
        from agent_squad.merging import merge_pr

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        common = Path(temporary.name)
        (common / "agent-squad-merge-pr1.json").write_text("{}")
        repository = SimpleNamespace(
            common=common,
            configuration=SimpleNamespace(merge_method="merge"),
        )
        forge = Mock(spec=Forge)
        forge.can_read_branch_rules = True
        forge.branch_rules.return_value = {}
        forge.pr.return_value.merged = False
        forge.snapshot.return_value.pr.merged = True
        with patch("agent_squad.merging.state_for",
                   side_effect=AssertionError("live state derived")):
            with self.assertRaises(GateError) as caught:
                merge_pr(repository, forge, 1)
        self.assertIn("PR is not open", str(caught.exception))
        self.assertIn("pr cleanup --as implementer --pr 1",
                      str(caught.exception))
        forge.branch_rules.assert_called_once()
        forge.snapshot.assert_called_once_with(1)
        forge.merge.assert_not_called()

    def test_failed_evidence_read_looks_at_the_pr_again(self):
        """#175: the forge merged the PR after the first read, and the read
        of its reviews and conversation then fails: the PR alone is read
        once more, and the refusal names the command."""
        from types import SimpleNamespace
        from agent_squad.forge import ForgeError

        unreadable = ForgeError("conversation unreadable")
        opened = SimpleNamespace(merged=False, base_branch="main")
        merged = SimpleNamespace(merged=True, base_branch="main")
        with self.assertRaises(GateError) as caught:
            self.merge(merged=False, snapshot_error=unreadable,
                       pr_reads=[opened, merged])
        self.assertIn("PR is not open", str(caught.exception))
        self.assertIn("pr cleanup --as implementer --pr 1",
                      str(caught.exception))
        self.assertEqual(self.forge.pr.call_count, 2)
        self.forge.snapshot.assert_called_once_with(1)
        self.forge.merge.assert_not_called()
        # Still open: the failed read is the answer.
        with self.assertRaises(ForgeError) as caught:
            self.merge(merged=False, snapshot_error=unreadable,
                       pr_reads=[opened, opened])
        self.assertIs(caught.exception, unreadable)
        self.assertEqual(self.forge.pr.call_count, 2)
        self.forge.merge.assert_not_called()

    def test_refusal_names_pr_cleanup_only_for_a_merged_pr(self):
        for merged in (True, False):
            with self.subTest(merged=merged):
                with self.assertRaises(GateError) as caught:
                    self.merge(merged=merged)
                self.assertEqual(
                    "pr cleanup --as implementer --pr 1" in str(
                        caught.exception),
                    merged,
                )

    def test_record_that_cannot_be_deleted_after_a_refusal_is_reported(
        self,
    ):
        from agent_squad.forge import ForgeError

        with patch("pathlib.Path.unlink", side_effect=OSError("busy")):
            result, common = self.merge(
                merged=False, error=ForgeError("refused", 405))
        self.assertIs(result["merged"], False)
        self.assertEqual(result["merge_record"]["result"], "kept")
        self.assertEqual(result["merge_record"]["reason"], "busy")
        self.assertIsNone(result["cleanup_command"])
        self.assertTrue((common / "agent-squad-merge-pr1.json").exists())

    def test_read_confirmed_refusal_deletes_the_record_whatever_its_status(
        self,
    ):
        """#142: the Forgejo adapter's read classified the answer."""
        from agent_squad.forge import MergeRefused

        result, common = self.merge(
            merged=False, error=MergeRefused("injected", 500))
        self.assertIs(result["merged"], False)
        self.assertEqual(result["error"], "injected")
        self.assertEqual(result["merge_record"]["result"], "deleted")
        self.assertIsNone(result["cleanup_command"])
        self.assertIsNone(result["merge_answer"])
        self.assertFalse((common / "agent-squad-merge-pr1.json").exists())

    def test_unclassified_server_error_stays_unknown_without_a_read(self):
        """GitHub's answers are classified by status alone, as before."""
        from agent_squad.forge import ForgeError

        result, common = self.merge(
            merged=False, error=ForgeError("server error", 500))
        self.assertIsNone(result["merged"])
        self.assertEqual(result["merge_record"]["result"], "kept")
        self.assertEqual(result["cleanup_command"],
                         "agent-squad pr cleanup --as implementer --pr 1")
        self.assertTrue((common / "agent-squad-merge-pr1.json").exists())
        # Only the read that opens merge_pr; none follows the answer.
        self.forge.pr.assert_called_once_with(1)


class SurvivingOwnerTests(unittest.TestCase):
    def cleanup(self, owner: object) -> list:
        import json
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock
        from agent_squad.merging import cleanup_merge

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = cleanup_repository()
        repository.common = Path(temporary.name)
        admin = repository.common / "worktrees/moved"
        admin.mkdir(parents=True)
        (admin / "agent-squad-implementation.json").write_text(
            json.dumps(owner))
        forge = Mock()
        forge.branch_head.return_value = H
        with patch("agent_squad.merging.list_worktrees", return_value=[]):
            return cleanup_merge(repository, 2, H, "feature", 1, forge)

    def test_boolean_pr_is_not_another_prs_number(self):
        steps = self.cleanup({"pr": True})
        self.assertEqual(len(steps), 1)
        self.assertFalse(steps[0]["ok"])
        self.assertIn("ownership record remains", steps[0]["detail"])

    def test_valid_number_of_another_pr_is_ignored(self):
        steps = self.cleanup({"pr": 3})
        self.assertEqual(steps[0], {
            "step": "implementation ownership", "ok": True,
            "detail": "already removed",
        })

    def scan(self, arrange) -> dict:
        """The ownership step for a pr merge record, with no worktree
        registered, after ``arrange`` set up ``<common>/worktrees``."""
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock
        from agent_squad.merging import cleanup_merge

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = cleanup_repository()
        repository.common = Path(temporary.name).resolve()
        arrange(repository.common / "worktrees")
        forge = Mock()
        forge.branch_head.return_value = H
        with patch("agent_squad.merging.list_worktrees", return_value=[]):
            steps = cleanup_merge(repository, 2, H, "feature", 1, forge)
        if not steps[0]["ok"]:
            self.assertEqual(len(steps), 1)
            forge.delete_branch.assert_not_called()
        return steps[0]

    def test_only_an_absent_record_counts_as_gone(self):
        """#175: a record, or a directory holding records, that cannot be
        read is not a record that is gone; the step fails."""
        def loop(path: Path) -> None:
            path.symlink_to(path.name)

        def no_directory(slots: Path) -> None:
            pass

        def no_record(slots: Path) -> None:
            (slots / "other").mkdir(parents=True)

        def stray_file(slots: Path) -> None:
            slots.mkdir()
            (slots / "stray").write_text("x")

        def dangling_record(slots: Path) -> None:
            (slots / "moved").mkdir(parents=True)
            (slots / "moved/agent-squad-implementation.json").symlink_to(
                slots / "absent")

        def looping_entry(slots: Path) -> None:
            slots.mkdir()
            loop(slots / "moved")

        for arrange in (no_directory, no_record, stray_file):
            with self.subTest(arrange.__name__):
                self.assertEqual(self.scan(arrange), {
                    "step": "implementation ownership", "ok": True,
                    "detail": "already removed",
                })
        step = self.scan(dangling_record)
        self.assertFalse(step["ok"])
        self.assertIn("ownership record remains", step["detail"])
        for arrange in (looping_entry, loop):
            with self.subTest(arrange.__name__):
                step = self.scan(arrange)
                self.assertFalse(step["ok"])
                self.assertIn("symbolic links", step["detail"])
