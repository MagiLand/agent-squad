"""Exact handoff strings and installed Herdr contracts."""

import errno
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


class PaneDirectoryTests(unittest.TestCase):
    """#175: a directory Herdr reports either is in the checkout, is not,
    or cannot be placed, and then the checkout is kept. Every case reads a
    real tree; nothing about the file system is patched."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "checkout"
        (self.checkout / "deep" / "deeper").mkdir(parents=True)
        self.other = self.root / "other"
        self.other.mkdir()

    def inside(self, path: Path | str) -> bool:
        from agent_squad.reviewer import (
            directory_in_checkout, placed_directory,
        )

        return directory_in_checkout(
            str(path), "Herdr pane directory",
            placed_directory(str(self.checkout)))

    def refused(self, path: Path | str) -> str:
        from agent_squad.initialization import RetainedError

        with self.assertRaises(RetainedError) as caught:
            self.inside(path)
        self.assertIn(f"cannot resolve Herdr pane directory {path}: ",
                      str(caught.exception))
        return str(caught.exception)

    def unreadable(self, directory: Path) -> None:
        """Take away the right to search ``directory`` until cleanup."""
        if os.geteuid() == 0:
            self.skipTest("root reads every directory")
        directory.chmod(0)
        self.addCleanup(directory.chmod, 0o700)

    def test_directories_are_placed_by_name_and_through_symlinks(
        self,
    ) -> None:
        from agent_squad.reviewer import placed_directory

        link = self.root / "link"
        link.symlink_to(self.checkout)
        for path in (self.checkout, self.checkout / "deep", link,
                     link / "deep", self.checkout / "deep" / "..",
                     link / "deep" / "deeper" / ".." / ".."):
            with self.subTest(path=path.relative_to(self.root)):
                self.assertTrue(self.inside(path))
        # Absent names below an existing directory say where they would be.
        self.assertTrue(self.inside(self.checkout / "gone" / "deeper"))
        self.assertTrue(self.inside(link / "gone"))
        status, spelling, absent = placed_directory(
            str(link / "gone" / "deeper"))
        self.assertTrue(os.path.samestat(status, os.stat(self.checkout)))
        self.assertEqual((spelling, absent), (str(link), ("gone", "deeper")))
        # A name in the checkout that leads out of it is where it leads.
        out = self.checkout / "out"
        out.symlink_to(self.other)
        for path in (self.other, self.other / "gone", self.root,
                     self.root / "checkout-2", self.checkout / "..",
                     out, out / "gone"):
            with self.subTest(path=path.relative_to(self.root)):
                self.assertFalse(self.inside(path))
        # The spelling is read as reported: separators and "." included.
        for text in (f"{link}/", f"{link}//deep/.", f"{link}/gone/"):
            with self.subTest(text=text):
                self.assertTrue(self.inside(text))
        self.assertFalse(self.inside(f"{self.other}/gone//"))
        # A checkout that is gone is still named by the paths below it.
        self.checkout.rename(self.root / "moved")
        self.assertTrue(self.inside(self.checkout))
        self.assertTrue(self.inside(self.checkout / "deep"))
        self.assertTrue(self.inside(
            self.other / ".." / "checkout" / "deep"))
        self.assertFalse(self.inside(self.other))
        self.assertFalse(self.inside(self.root / "moved"))
        self.assertFalse(self.inside(self.root / "checkout-2" / "deep"))
        # The same missing names under another directory are elsewhere.
        self.assertFalse(self.inside(self.other / "checkout" / "deep"))

    def test_wrong_case_spelling_is_inside_where_the_file_system_folds(
        self,
    ) -> None:
        shouted = self.root / "CHECKOUT"
        if not shouted.exists():
            self.skipTest("the file system is case-sensitive")
        self.assertTrue(self.inside(shouted))
        self.assertTrue(self.inside(shouted / "deep"))
        self.assertTrue(self.inside(shouted / "gone" / "deeper"))
        self.assertFalse(self.inside(self.root / "OTHER"))

    def changes(self) -> dict:
        """What the name ``hop``, a link into the checkout, can become so
        that the path through it no longer leads anywhere readable."""
        def unsearchable(hop: Path) -> None:
            hop.mkdir()
            self.unreadable(hop)

        def loop(hop: Path) -> None:
            hop.symlink_to(hop.name)

        def dangling(hop: Path) -> None:
            hop.symlink_to(hop.parent / "absent")

        return {
            "a directory that cannot be searched": unsearchable,
            "a file": Path.touch,
            "a loop": loop,
            "a link to nowhere": dangling,
        }

    def test_path_that_led_into_the_checkout_is_never_placed_outside(
        self,
    ) -> None:
        """REV-18: every path here named a directory in the checkout
        through the link ``hop`` before ``hop`` became something that the
        operating system does not read through. Each must then be refused,
        and so keep the checkout; none may read as outside. A reading that
        works the path out itself ends at the readable decoys instead."""
        # Where ``hop`` sits: the reported spelling, and the link that the
        # spelling's last name is, if it is one.
        places = {
            "the final name": ("hop", None),
            "the final name, with a separator": ("hop/", None),
            "inside the spelling": ("hop/deeper", None),
            "before .. in the spelling": ("hop/../deep", None),
            "inside a link target": ("alias", "hop/deeper"),
            "before .. in a link target": ("alias", "hop/../deep"),
            "before .. in an absolute link target": (
                "alias", "{case}/hop/../deep"),
            "below a link target": ("alias/deeper", "hop"),
            "behind two links": ("alias", "second"),
        }
        number = 0
        for place, (reported, target) in places.items():
            for change, become in self.changes().items():
                number += 1
                case = self.root / f"case-{number}"
                # Decoys: what "hop/../deep" and the like name when ".."
                # is taken off the text instead of read from the system.
                (case / "deep" / "deeper").mkdir(parents=True)
                (case / "deeper").mkdir()
                hop = case / "hop"
                hop.symlink_to(self.checkout / "deep")
                if target is not None:
                    (case / "alias").symlink_to(target.format(case=case))
                    (case / "second").symlink_to("hop/../deep")
                path = f"{case}/{reported}"
                with self.subTest(place=place, change=change):
                    self.assertTrue(self.inside(path))
                    hop.unlink()
                    become(hop)
                    with self.assertRaises(OSError):
                        os.stat(os.path.join(path, "."))
                    self.refused(path)
                    # Below it nothing can be placed either.
                    self.refused(f"{path}/gone")

    def test_changed_link_target_is_never_placed_outside(self) -> None:
        """REV-18: the link stays and what it names changes. A link whose
        target is no longer a directory is refused rather than set aside
        as a missing name, which would place the pane beside the link."""
        target = self.checkout / "target"
        target.mkdir()
        alias = self.other / "alias"
        alias.symlink_to(target)
        self.assertTrue(self.inside(alias))
        target.rmdir()
        for path in (alias, alias / "below", f"{alias}/"):
            with self.subTest("removed", path=path):
                self.assertIn(
                    f"{alias} leads to a path that is missing",
                    self.refused(path))
        self.refused(f"{alias}/.")
        target.touch()
        for path in (alias, alias / "below", f"{alias}/", f"{alias}/."):
            with self.subTest("a file", path=path):
                self.refused(path)
        self.assertIn(f"{alias} is not a directory", self.refused(alias))

    def test_path_that_is_not_a_directory_is_refused(self) -> None:
        """A working directory cannot be a file, so a path that names one
        says nothing about where the pane is."""
        file = self.other / "file"
        file.touch()
        self.assertIn(f"{file} is not a directory", self.refused(file))
        self.refused(file / "below")
        self.refused(self.checkout / ".." / "other" / "file")

    def test_directory_that_cannot_be_searched_is_refused(self) -> None:
        """The directories above one that cannot be searched cannot be
        read from the operating system, so it is not placed. The checkout
        itself needs nothing above it."""
        closed = self.other / "closed"
        closed.mkdir()
        self.unreadable(closed)
        self.refused(closed)
        self.refused(closed / "below")
        self.unreadable(self.checkout)
        self.assertTrue(self.inside(self.checkout))
        self.refused(self.checkout / "deep")
        self.assertFalse(self.inside(self.other))

    def test_path_that_cannot_be_placed_is_refused(self) -> None:
        loop = self.root / "loop"
        loop.symlink_to(loop.name)
        self.assertIn(os.strerror(errno.ELOOP), self.refused(loop))
        self.refused(loop / "below")
        self.refused(self.root / "nul\x00")
        self.refused(self.root / ("long" * 100) / ("name" * 100))
        # Names that climb out of a directory that is gone: where they
        # would lead depends on what that directory was.
        self.assertIn("is below a path that is missing", self.refused(
            self.checkout / "gone" / ".." / ".." / "other"))
        self.assertIn("is below a path that is missing", self.refused(
            f"{self.checkout}/gone/."))
        self.assertIn("leads to a path that is missing", self.refused(
            self.dangling()))
        self.assertIn("leads to a path that is missing", self.refused(
            f"{self.dangling()}/"))

    def dangling(self) -> Path:
        link = self.root / "nowhere"
        if not link.is_symlink():
            link.symlink_to(self.root / "absent")
        return link

    def unused(self, path: Path, **inventory: list):
        from unittest.mock import Mock
        from agent_squad.reviewer import remove_unused_checkout

        worktree = Mock(path=path)
        return worktree, lambda: remove_unused_checkout(
            worktree,
            {"workspaces": [], "panes": [], "agents": [], **inventory})

    def test_checkout_is_removed_only_when_nothing_uses_it(self) -> None:
        from agent_squad.initialization import RetainedError

        loop = self.root / "loop"
        loop.symlink_to(loop.name)
        alias = self.other / "alias"
        alias.symlink_to(self.checkout)
        cases = {
            "pane in the checkout": (
                self.checkout, {"panes": [{"cwd": str(alias / "deep")}]},
                "an unowned Herdr pane still uses this worktree"),
            "pane that cannot be placed": (
                self.checkout, {"panes": [{"cwd": str(loop)}]},
                f"cannot resolve Herdr pane directory {loop}"),
            # REV-25: each directory the inventory reports is placed, not
            # the pane's own working directory alone.
            "foreground process in the checkout": (
                self.checkout,
                {"panes": [{"cwd": str(self.other),
                            "foreground_cwd": str(alias / "deep")}]},
                "an unowned Herdr pane still uses this worktree"),
            "foreground process that cannot be placed": (
                self.checkout,
                {"panes": [{"cwd": str(self.other),
                            "foreground_cwd": str(loop)}]},
                f"cannot resolve Herdr pane directory {loop}"),
            "agent in the checkout": (
                self.checkout, {"agents": [{"cwd": str(alias)}]},
                "an unowned Herdr agent still uses this worktree"),
            "agent's foreground process in the checkout": (
                self.checkout,
                {"agents": [{"cwd": None,
                             "foreground_cwd": str(self.checkout / "gone")}]},
                "an unowned Herdr agent still uses this worktree"),
            "agent that cannot be placed": (
                self.checkout, {"agents": [{"cwd": str(loop)}]},
                f"cannot resolve Herdr agent directory {loop}"),
            "registration's repository in the checkout": (
                self.checkout,
                {"workspaces": [{"worktree": {
                    "checkout_path": str(self.other),
                    "repo_root": str(alias / "deep")}}]},
                "an unowned Herdr workspace still uses this worktree"),
            "registration's repository that cannot be placed": (
                self.checkout,
                {"workspaces": [{"worktree": {
                    "checkout_path": str(self.other),
                    "repo_root": str(loop)}}]},
                f"cannot resolve Herdr worktree registration {loop}"),
            "registration by another name": (
                self.checkout,
                {"workspaces": [{"worktree": {"checkout_path": str(alias)}}]},
                "an unowned Herdr workspace still uses this worktree"),
            "registration that cannot be placed": (
                self.checkout,
                {"workspaces": [{"worktree": {"checkout_path": str(loop)}}]},
                f"cannot resolve Herdr worktree registration {loop}"),
            "checkout that cannot be placed": (
                loop, {}, f"cannot resolve review worktree {loop}"),
            "checkout by another name": (
                alias, {"panes": [{"cwd": str(self.checkout / "deep")}]},
                "an unowned Herdr pane still uses this worktree"),
        }
        for name, (path, inventory, message) in cases.items():
            with self.subTest(name):
                worktree, remove = self.unused(path, **inventory)
                with self.assertRaises(RetainedError) as caught:
                    remove()
                self.assertIn(message, str(caught.exception))
                worktree.remove.assert_not_called()
        outside = {"cwd": str(self.other), "foreground_cwd": str(self.root)}
        elsewhere = {
            "panes": [outside, {"cwd": "relative"},
                      {"cwd": None, "foreground_cwd": None}, {}],
            "agents": [outside, {"foreground_cwd": "relative"}, {}],
            "workspaces": [{"worktree": None}, {},
                           {"worktree": {"checkout_path": str(self.other),
                                         "repo_root": str(self.root)}}],
        }
        worktree, remove = self.unused(self.checkout, **elsewhere)
        self.assertIs(remove(), worktree.remove.return_value)

    def test_incomplete_inventory_removes_nothing(self) -> None:
        from agent_squad.herdr import HerdrError
        from agent_squad.reviewer import remove_unused_checkout
        from unittest.mock import Mock

        complete = {"workspaces": [], "panes": [], "agents": []}
        for missing in complete:
            with self.subTest(missing=missing):
                worktree = Mock(path=self.checkout)
                partial = {k: v for k, v in complete.items() if k != missing}
                with self.assertRaises(HerdrError):
                    remove_unused_checkout(worktree, partial)
                worktree.remove.assert_not_called()


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
