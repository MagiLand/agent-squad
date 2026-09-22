"""The fixed approval, moved-base, and integration rules."""

from tests.unit.test_conventions import A, H
from agent_squad.merging import check_merge_gate, verify_integration
from agent_squad.initialization import AgentSquadError, GateError
from copy import deepcopy
import unittest
from unittest.mock import patch

from tests._support import add_src_to_path

add_src_to_path()


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
