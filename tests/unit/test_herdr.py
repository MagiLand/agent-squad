"""Exact handoff strings and installed Herdr contracts."""

import unittest

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.herdr import (
    request_line,
    result_message,
    reviewer_name,
    stopped_message,
)
from agent_squad.initialization import AgentKind, AgentSquadError

H, B = "a" * 40, "b" * 40


class HandoffTemplateTests(unittest.TestCase):
    def test_both_request_prefixes_and_worktree_name(self) -> None:
        for kind, prefix in ((AgentKind.CLAUDE, "/"), (AgentKind.CODEX, "$")):
            self.assertEqual(
                request_line(kind, 43, H, B, "implementer"),
                f"{prefix}squad-reviewer pr=43 head={H} base={B}"
                " implementer=implementer",
            )
        self.assertEqual(reviewer_name(43, H), "reviewer-pr43-aaaaaaa")
        self.assertEqual(reviewer_name(1, "a" * 64), "reviewer-pr1-aaaaaaa")

    def test_result_sentences_are_verbatim(self) -> None:
        expected = {
            "approved": (
                "Run agent-squad status --pr 43, report the approval to the"
                " Developer, and do not merge without the Developer's"
                " instruction."
            ),
            "changes_requested": (
                "Run agent-squad status --pr 43, evaluate every blocking"
                " thread on the PR, and record dispositions before requesting"
                " another review."
            ),
            "needs_human": (
                "Run agent-squad status --pr 43 and relay the decision"
                " required to the Developer."
            ),
        }
        for verdict, sentence in expected.items():
            self.assertEqual(
                result_message(43, H, verdict),
                f"AGENT_SQUAD/0.5.0 REVIEW_RESULT pr=43 head={H}"
                f" verdict={verdict}\n{sentence}",
            )

    def test_stop_sentences_are_verbatim(self) -> None:
        for reason in (
            "budget",
            "repeat",
            "scope",
            "design",
            "ambiguity",
            "judgement",
        ):
            self.assertEqual(
                stopped_message(43, H, reason),
                f"AGENT_SQUAD/0.5.0 STOPPED pr=43 head={H}"
                f" reason={reason}\nAutomated review has stopped; run"
                " agent-squad status --pr 43 and relay the reason and the"
                " remaining problems to the Developer.",
            )

    def test_malformed_protocol_inputs_are_refused(self) -> None:
        for head in (
            H[:7],
            H.upper(),
            H + " extra",
            " " + H,
            H + "\n",
            "z" * 40,
        ):
            for render in (
                lambda: reviewer_name(1, head),
                lambda: result_message(1, head, "approved"),
                lambda: stopped_message(1, head, "budget"),
            ):
                with (
                    self.subTest(head=head),
                    self.assertRaises(AgentSquadError),
                ):
                    render()
        for pr in (0, -1, "01", "1", True, 10**30):
            with self.subTest(pr=pr), self.assertRaises(AgentSquadError):
                reviewer_name(pr, H)
        for base in ("main", B[:7], B.upper(), B + "\n"):
            with self.assertRaises(AgentSquadError):
                request_line(AgentKind.CODEX, 1, H, base, "implementer")
        for name in ("", "Implementer", "implementer extra", "x" * 33):
            with self.assertRaises(AgentSquadError):
                request_line(AgentKind.CODEX, 1, H, B, name)
        for verdict in ("APPROVED", "approve", "approved extra", "approved\n"):
            with self.assertRaises(AgentSquadError):
                result_message(1, H, verdict)
        for reason in ("other", "Budget", "budget extra", "budget\n"):
            with self.assertRaises(AgentSquadError):
                stopped_message(1, H, reason)


class LaunchGateTests(unittest.TestCase):
    def test_every_launch_gate_refuses_before_resource_creation(self) -> None:
        from unittest.mock import Mock, patch
        from agent_squad.initialization import GateError
        from agent_squad.reviewer import launch

        names = (
            "not_pushed",
            "stopped",
            "needs_decision",
            "unanchored_findings",
            "unaddressed_findings",
            "same_head_requires_rejections",
            "budget_exhausted",
            "reviewer_live",
        )
        for name in names:
            gates = {g: g == name for g in names}
            gates["task_amended"] = False
            state = {"gates": gates, "pr": {"merged": False, "state": "open"}}
            client = Mock()
            with (
                self.subTest(gate=name),
                patch("agent_squad.reviewer.state_for", return_value=state),
                patch("agent_squad.reviewer.ReviewWorktree.create") as create,
            ):
                with self.assertRaisesRegex(GateError, name):
                    launch(Mock(), Mock(), 1, client=client)
                create.assert_not_called()
                self.assertEqual(client.mock_calls, [])

    def test_task_amendment_only_bypasses_same_head_gate(self) -> None:
        from unittest.mock import Mock, patch
        from agent_squad.initialization import GateError
        from agent_squad.reviewer import launch

        state = {
            "gates": {
                "same_head_requires_rejections": True,
                "task_amended": True,
                "budget_exhausted": False,
                "reviewer_live": False,
            },
            "target": {"base": B, "head": H},
            "pr": {"merged": False, "state": "open"},
        }
        with (
            patch("agent_squad.reviewer.state_for", return_value=state),
            patch("agent_squad.reviewer.git_output", return_value=""),
        ):
            with self.assertRaisesRegex(GateError, "scope is empty"):
                launch(Mock(), Mock(), 1, client=Mock())
            state["gates"]["budget_exhausted"] = True
            with self.assertRaisesRegex(GateError, "budget_exhausted"):
                launch(Mock(), Mock(), 1, client=Mock())

    def test_busy_shell_retry_is_bounded(self) -> None:
        from unittest.mock import Mock, patch
        from agent_squad.herdr import HerdrCommandError
        from agent_squad.reviewer import start_reviewer

        client = Mock()
        client.start_agent.side_effect = HerdrCommandError(
            "busy", "agent_pane_busy"
        )
        with patch("agent_squad.reviewer.time.monotonic", side_effect=[0, 6]):
            with self.assertRaises(HerdrCommandError):
                start_reviewer(Mock(), client, {"pane_id": "p1"}, ())
        client.start_agent.assert_called_once()
