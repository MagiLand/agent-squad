"""Exact handoff strings and installed Herdr contracts."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests._support import PROJECT_ROOT, add_src_to_path

add_src_to_path()

from agent_squad.herdr import (  # noqa: E402
    HerdrClient,
    HerdrError,
    HerdrSessionError,
    request_line,
    result_message,
    reviewer_name,
    stopped_message,
)
from agent_squad.initialization import AgentKind, AgentSquadError  # noqa: E402

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
                "Run agent-squad status --pr 43 --json and follow its derived"
                " next_action under the squad-implementer skill."
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

        approved = result_message(43, H, "approved")
        self.assertNotIn("do not merge", approved)
        self.assertNotIn("Developer's instruction", approved)

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
        from unittest.mock import Mock, call, patch
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
                # Only the Implementer's session was resolved (§8.3).
                self.assertEqual(client.mock_calls, [call.session()])

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


class SessionResolutionTests(unittest.TestCase):
    """The §8.2 rule against the fake's sessions and a real Git repository."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="agent-squad-herdr-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.primary = self.root / "repository"
        self.linked = self.root / "linked"
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.model = self.root / "herdr.json"
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("GIT_", "FAKE_", "HERDR_"))
        }
        environment.update(
            FAKE_HERDR_MODEL=str(self.model),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="Squad fixture",
            GIT_AUTHOR_EMAIL="squad@example.invalid",
            GIT_COMMITTER_NAME="Squad fixture",
            GIT_COMMITTER_EMAIL="squad@example.invalid",
        )
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for arguments in (
            ("init", "--initial-branch=main", str(self.primary)),
            ("-C", str(self.primary), "commit", "--allow-empty", "-m", "seed"),
            ("-C", str(self.primary), "worktree", "add", "--detach",
             str(self.linked)),
        ):
            subprocess.run(
                ["git", *arguments], check=True, capture_output=True,
                shell=False, timeout=30,
            )
        (self.primary / "sub/directory").mkdir(parents=True)
        self.repository = SimpleNamespace(
            primary=self.primary,
            configuration=SimpleNamespace(implementer=SimpleNamespace(
                agent_name="implementer", kind=AgentKind.CODEX,
            )),
        )

    def client(self, *, repository: bool = True) -> HerdrClient:
        return HerdrClient(
            self.primary,
            executable=PROJECT_ROOT / "tests/fixtures/fake_herdr.py",
            repository=self.repository if repository else None,
        )

    def sessions(self, fixture: dict, **others: dict) -> None:
        """Write each session's settings; `fixture` is the default one."""
        self.model.write_text(json.dumps({
            "settings": fixture,
            "sessions": {
                name: {"settings": settings}
                for name, settings in others.items()
            },
        }))

    def calls(self, name: str = "fixture") -> list:
        model = json.loads(self.model.read_text())
        return (model if name == "fixture" else model["sessions"][name])[
            "calls"
        ]

    def socket(self, name: str) -> str:
        return str(self.root / f"herdr-{name}.sock")

    def refusal(self) -> HerdrSessionError:
        with self.assertRaises(HerdrSessionError) as raised:
            self.client().session()
        return raised.exception

    def test_selects_the_single_match_and_addresses_every_call(self) -> None:
        self.sessions(
            {"implementer": {"cwd": str(self.outside)}},
            ours={"implementer": {}},
            idle={},
        )
        variables = (
            {},
            {"HERDR_SESSION": "fixture",
             "HERDR_SOCKET_PATH": self.socket("fixture")},
            {"HERDR_SESSION": "ours",
             "HERDR_SOCKET_PATH": self.socket("ours")},
            {"HERDR_SESSION": "absent",
             "HERDR_SOCKET_PATH": self.socket("absent")},
        )
        for inherited in variables:
            with self.subTest(inherited=inherited), patch.dict(
                os.environ, inherited
            ):
                client = self.client()
                session = client.session()
                self.assertEqual(
                    (session.name, session.running, session.socket_path),
                    ("ours", True, self.socket("ours")),
                )
                before = len(self.calls("ours"))
                self.assertIsNone(client.get_agent("reviewer-pr1-aaaaaaa"))
                self.assertEqual(client.snapshot()["agents"], [])
                self.assertEqual(self.calls("ours")[before:], [
                    ["agent", "get", "reviewer-pr1-aaaaaaa"],
                    ["api", "snapshot"],
                ])
                self.assertIs(client.session(), session)
        # One listing and one read per client; nothing else reaches the
        # other sessions.
        self.assertEqual(
            self.calls(),
            [["session", "list", "--json"], ["agent", "get", "implementer"]]
            * len(variables),
        )
        self.assertEqual(
            self.calls("idle"),
            [["agent", "get", "implementer"]] * len(variables),
        )

    def test_name_kind_and_directory_are_each_required(self) -> None:
        link = self.root / "link"
        link.symlink_to(self.primary / "sub")
        cycle = self.root / "cycle"
        cycle.symlink_to("cycle")
        for implementer, matches in (
            ({}, True),
            ({"cwd": str(self.primary / "sub/directory")}, True),
            ({"cwd": str(self.linked)}, True),
            ({"cwd": str(link / "directory")}, True),
            (False, False),
            ({"name": "other"}, False),
            ({"kind": "claude"}, False),
            ({"cwd": str(self.outside)}, False),
            ({"cwd": str(self.root)}, False),
            ({"cwd": str(self.primary) + "-sibling"}, False),
            ({"cwd": "repository"}, False),
            ({"cwd": str(cycle)}, False),
            ({"cwd": None}, False),
        ):
            with self.subTest(implementer=implementer):
                self.sessions({"implementer": implementer})
                if matches:
                    self.assertEqual(self.client().session().name, "fixture")
                    continue
                error = self.refusal()
                self.assertEqual(error.matches, ())
                self.assertEqual(
                    str(error),
                    "no running Herdr session holds Implementer"
                    " 'implementer' (kind 'codex') working in"
                    f" {self.primary}; sessions examined: fixture",
                )

    def test_a_record_with_another_name_does_not_match(self) -> None:
        self.sessions({})
        client = self.client()
        original = client._run

        def run(arguments, **kwargs):
            result = original(arguments, **kwargs)
            if arguments[:2] == ("agent", "get"):
                result.stdout = result.stdout.replace(
                    '"name": "implementer"', '"name": "renamed"'
                )
            return result

        with patch.object(client, "_run", side_effect=run):
            with self.assertRaises(HerdrSessionError):
                client.session()

    def test_sessions_that_are_not_running_are_not_examined(self) -> None:
        self.sessions(
            {"implementer": False},
            stopped={"running": False, "implementer": {}},
        )
        self.assertEqual(
            str(self.refusal()).rsplit("; ", 1)[1],
            "sessions examined: fixture",
        )
        self.assertEqual(self.calls("stopped"), [])
        self.sessions({"running": False}, stopped={"running": False})
        self.assertTrue(str(self.refusal()).endswith(
            "sessions examined: none"
        ))

    def test_unreadable_session_is_skipped_or_reported(self) -> None:
        self.sessions({"unreachable": True}, ours={"implementer": {}})
        self.assertEqual(self.client().session().name, "ours")
        self.sessions({"unreachable": True}, ours={"implementer": False})
        error = self.refusal()
        self.assertEqual(error.matches, ())
        self.assertTrue(str(error).endswith(
            "; sessions examined: fixture, ours; sessions that could not be"
            " read: fixture (could not inspect Implementer session: fixture"
            " Herdr is unreachable)"
        ), str(error))

    def test_several_matches_are_refused_with_their_names(self) -> None:
        self.sessions(
            {}, twin={"implementer": {"cwd": str(self.linked)}}, idle={},
        )
        error = self.refusal()
        self.assertEqual(error.matches, ("fixture", "twin"))
        self.assertEqual(
            str(error),
            "several running Herdr sessions hold Implementer 'implementer'"
            f" (kind 'codex') working in {self.primary}: fixture, twin;"
            " sessions examined: fixture, twin, idle",
        )

    def test_refusal_is_remembered_and_blocks_every_call(self) -> None:
        self.sessions({"implementer": False})
        client = self.client()
        for call in (
            client.session, client.snapshot,
            lambda: client.get_agent("implementer"),
            lambda: client.prompt("implementer", "text"),
        ):
            with self.assertRaises(HerdrSessionError):
                call()
        self.assertEqual(
            self.calls(),
            [["session", "list", "--json"], ["agent", "get", "implementer"]],
        )

    def test_malformed_session_listing_raises(self) -> None:
        entry = {
            "name": "fixture", "running": True,
            "socket_path": self.socket("fixture"),
        }
        for listing in (
            [], {}, {"sessions": None}, {"sessions": [None]},
            {"sessions": [{**entry, "name": ""}]},
            {"sessions": [{**entry, "name": 1}]},
            {"sessions": [{**entry, "running": "true"}]},
            {"sessions": [{**entry, "running": 1}]},
            {"sessions": [{**entry, "socket_path": None}]},
            {"sessions": [{**entry, "socket_path": "herdr.sock"}]},
            {"sessions": [entry, {"name": "partial"}]},
        ):
            with self.subTest(listing=listing):
                self.sessions({"session_list": listing})
                with self.assertRaises(HerdrError) as raised:
                    self.client().session()
                self.assertNotIsInstance(raised.exception, HerdrSessionError)
                self.assertIn("Herdr session", str(raised.exception))
                self.assertEqual(self.calls(), [["session", "list", "--json"]])
        self.sessions({"session_list": {"sessions": [entry]}})
        self.assertEqual(self.client().session().name, "fixture")

    def test_discovery_requires_listing_and_socket_addressing(self) -> None:
        self.sessions({}, other={})
        for repository in (True, False):
            with self.subTest(repository=repository):
                installation = self.client(repository=repository).discover(
                    AgentKind.CODEX, role="Reviewer"
                )
                self.assertEqual(installation.protocol, 20)
        model = json.loads(self.model.read_text())
        self.assertEqual(model["stray"], [["api", "snapshot"]] * 2)
        self.assertEqual(
            self.calls("other"), [["agent", "get", "implementer"]]
        )

        self.sessions({"ignore_socket_path": True})
        with self.assertRaisesRegex(
            HerdrError, "does not honour HERDR_SOCKET_PATH"
        ):
            self.client().discover(AgentKind.CODEX, role="Reviewer")
        self.sessions({"session_list_failure": True})
        with self.assertRaisesRegex(HerdrError, "herdr session list failed"):
            self.client(repository=False).discover(
                AgentKind.CODEX, role="Reviewer"
            )

    def test_child_environment_carries_only_the_resolved_socket(self) -> None:
        self.sessions({"implementer": False}, ours={"implementer": {}})
        inherited = {
            "HERDR_SESSION": "fixture",
            "HERDR_SOCKET_PATH": self.socket("fixture"),
            "HERDR_PANE_ID": "w1:p1",
        }
        seen = []
        original = subprocess.run

        def run(command, **kwargs):
            if Path(command[0]).name != "fake_herdr.py":
                return original(command, **kwargs)
            environment = kwargs["env"]
            seen.append((
                command[1:3],
                environment.get("HERDR_SESSION"),
                environment.get("HERDR_SOCKET_PATH"),
                environment.get("HERDR_PANE_ID"),
            ))
            return original(command, **kwargs)

        with (
            patch.dict(os.environ, inherited),
            patch("agent_squad.herdr.subprocess.run", side_effect=run),
        ):
            self.client().snapshot()
        self.assertEqual(seen, [
            (["session", "list"], None, None, "w1:p1"),
            (["agent", "get"], None, self.socket("fixture"), "w1:p1"),
            (["agent", "get"], None, self.socket("ours"), "w1:p1"),
            (["api", "snapshot"], None, self.socket("ours"), "w1:p1"),
        ])

    def test_inherited_fallback_follows_the_calling_process(self) -> None:
        self.sessions({"implementer": False}, other={})
        client = self.client()
        with self.assertRaises(HerdrSessionError):
            client.session()
        client.use_inherited_session()
        with patch.dict(os.environ, {"HERDR_SESSION": "other"}):
            client.snapshot()
        client.snapshot()
        self.assertEqual(self.calls("other")[-1], ["api", "snapshot"])
        self.assertEqual(self.calls()[-1], ["api", "snapshot"])
        with self.assertRaisesRegex(HerdrError, "no repository"):
            client.session()
