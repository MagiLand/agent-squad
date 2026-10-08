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
        self.state["merge_hold"] = {"review_id": 42, "text": "Item 3: rules"}
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
            "issue": 1,
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
    def merge(self, *, merged: bool, error: Exception | None = None):
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
        state = {"pr": {"merged": merged}, "target": {"head": H}}
        gate = (
            {"return_value": False} if error
            else {"side_effect": GateError("pr merge refused: x")}
        )
        with patch("agent_squad.merging.state_for", return_value=state), \
                patch("agent_squad.merging.check_merge_gate", **gate), \
                patch("agent_squad.merging.write_merge_record"):
            return merge_pr(repository, forge, 1), common

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
