"""Each CLI process appends one content-free event to its store (#148)."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest

from tests.forge_support import ForgeFixture, ForgejoFixture
from agent_squad.cli import parser
from agent_squad.events import FIELDS, subcommands


def month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ForgeFixture()
        self.addCleanup(self.f.close)
        self.store = self.f.root / "state/agent-squad/events"

    def run_cli(self, *args: str,
                **env: str) -> subprocess.CompletedProcess[str]:
        saved = dict(self.f.env)
        self.f.env.update(env)
        try:
            return self.f.run([sys.executable, "-m", "agent_squad", *args],
                              cwd=self.f.worktree)
        finally:
            self.f.env = saved

    def events(self) -> list[dict]:
        return [
            json.loads(line)
            for path in sorted(self.store.glob("events-*.jsonl"))
            for line in path.read_text().splitlines()
        ]

    def project(self) -> str:
        forge = json.loads(
            (self.f.repo / ".agent-squad/config.json").read_text())["forge"]
        text = "\n".join((forge["kind"], forge.get("base_url", ""),
                          forge["owner"], forge["repo"]))
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def test_success_failure_and_refusal_each_append_one_event(self) -> None:
        self.f.initialize()
        self.f.candidate()
        self.f.create_pr()
        pr = 1  # The fixtures' first PR.
        project = self.project()
        for args, exit_code, fields in (
            (("issue", "view", "--issue", "1"), 0,
             {"group": "issue", "command": "view", "issue": 1, "pr": None,
              "role": None}),
            (("issue", "view", "--issue", "404"), 1,
             {"group": "issue", "command": "view", "issue": 404}),
            (("pr", "merge", "--as", "implementer", "--pr", str(pr)), 4,
             {"group": "pr", "command": "merge", "role": "implementer",
              "pr": pr, "issue": None}),
        ):
            with self.subTest(args=args):
                before = len(self.events())
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, exit_code, result.stderr)
                written = self.events()
                self.assertEqual(len(written), before + 1)
                event = written[-1]
                self.assertEqual(list(event), list(FIELDS))
                self.assertEqual(
                    {k: event[k] for k in fields}, fields)
                self.assertEqual(
                    (event["exit_code"], event["project"], event["forge"]),
                    (exit_code, project, "github"),
                )
        self.assertEqual(
            [(e["group"], e["command"], e["exit_code"])
             for e in self.events()[:2]],
            [("init", None, 0), ("pr", "create", 0)],
        )

    def test_usage_errors_are_recorded_and_help_is_not(self) -> None:
        for args in (("--help",), ("--version",), ("pr", "--help"),
                     ("pr", "merge", "--help")):
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(*args).returncode, 0)
        self.assertFalse(self.store.exists())
        result = self.run_cli("pr", "merge", "--pr", "1")
        self.assertEqual(result.returncode, 2)
        [event] = self.events()
        self.assertEqual(
            {k: event[k] for k in ("group", "command", "role", "pr",
                                   "issue", "exit_code", "project")},
            {"group": "pr", "command": "merge", "role": None, "pr": None,
             "issue": None, "exit_code": 2, "project": None},
        )

    def test_disabled_recording_appends_nothing(self) -> None:
        self.f.env["AGENT_SQUAD_EVENTS"] = "off"
        self.f.initialize()
        for args in (("issue", "view", "--issue", "1"),
                     ("issue", "view", "--issue", "404"), ("MARKER",)):
            self.run_cli(*args)
        self.assertFalse((self.f.root / "state").exists())

    def test_an_unwritable_store_leaves_every_run_unchanged(self) -> None:
        self.f.initialize()
        elsewhere = self.f.root / "elsewhere"
        elsewhere.mkdir()
        target = elsewhere / "target.jsonl"
        target.write_text("")
        blocked = self.f.root / "blocked"
        blocked.write_text("")
        name = f"events-{month()}.jsonl"

        def linked_directory() -> None:
            self.store.parent.mkdir(parents=True)
            self.store.symlink_to(elsewhere, target_is_directory=True)

        def linked_owner() -> None:
            self.store.parent.parent.mkdir(parents=True)
            self.store.parent.symlink_to(elsewhere, target_is_directory=True)

        def linked_file() -> None:
            self.store.mkdir(parents=True)
            (self.store / name).symlink_to(target)

        def fifo() -> None:
            self.store.mkdir(parents=True)
            os.mkfifo(self.store / name)

        breakages = {
            "state directory is a file": (lambda: None,
                                          {"XDG_STATE_HOME": str(blocked)}),
            "events directory is a link": (linked_directory, {}),
            "agent-squad directory is a link": (linked_owner, {}),
            "month file is a link": (linked_file, {}),
            "month file is a FIFO": (fifo, {}),
        }
        commands = (("issue", "view", "--issue", "1"),
                    ("issue", "view", "--issue", "404"),
                    ("pr", "merge", "--pr", "1"))
        expected = {
            args: self.run_cli(*args, AGENT_SQUAD_EVENTS="off")
            for args in commands
        }
        for label, (prepare, env) in breakages.items():
            with self.subTest(breakage=label):
                shutil.rmtree(self.f.root / "state", ignore_errors=True)
                prepare()
                for args in commands:
                    actual = self.run_cli(*args, **env)
                    self.assertEqual(
                        (actual.returncode, actual.stdout, actual.stderr),
                        (expected[args].returncode, expected[args].stdout,
                         expected[args].stderr),
                    )
                self.assertEqual(sorted(elsewhere.iterdir()), [target])
                self.assertEqual(target.read_text(), "")
                self.assertEqual(blocked.read_text(), "")
        self.assertEqual(month(), name[7:14], "the month changed mid-test")

    def test_store_modes_and_one_file_per_month(self) -> None:
        self.f.initialize()
        self.run_cli("issue", "view", "--issue", "1")
        files = list(self.store.iterdir())
        self.assertEqual([path.name for path in files],
                         [f"events-{month()}.jsonl"])
        self.assertEqual(len(files[0].read_text().splitlines()), 2)
        for path, mode in ((self.store.parent, 0o700), (self.store, 0o700),
                           (files[0], 0o600)):
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(path.lstat().st_mode), mode)

    def test_doctor_reports_the_store_and_never_fails_on_it(self) -> None:
        def row(result: dict) -> dict:
            [match] = [d for d in result["diagnostics"]
                       if d["check"] == "event store"]
            return match

        unconfigured = self.f.cli("doctor", expected=1)
        self.assertEqual(
            [d["check"] for d in unconfigured["diagnostics"]],
            ["repository and configuration", "event store"],
        )
        self.f.initialize()
        result = self.f.cli("doctor")
        self.assertTrue(result["ok"])
        self.assertEqual(row(result)["severity"], "pass")
        self.assertEqual(row(result)["detail"], f"writable: {self.store}")
        blocked = self.f.root / "blocked"
        blocked.write_text("")
        self.f.env["XDG_STATE_HOME"] = str(blocked)
        result = self.f.cli("doctor")
        self.assertTrue(result["ok"])
        self.assertEqual(row(result)["severity"], "warn")
        self.assertTrue(row(result)["detail"].startswith(
            f"not writable: {blocked}/agent-squad/events: "))
        self.f.env["AGENT_SQUAD_EVENTS"] = "off"
        result = self.f.cli("doctor")
        self.assertTrue(result["ok"])
        self.assertEqual(
            (row(result)["severity"], row(result)["detail"]),
            ("pass", "recording disabled by AGENT_SQUAD_EVENTS=off"),
        )

    def test_events_hold_no_argument_text(self) -> None:
        markers = {
            "account": "MarkerAccountQ7", "branch": "MarkerBranchQ7",
            "title": "MarkerTitleQ7", "path": "MarkerPathQ7",
            "word": "MarkerWordQ7", "role": "MarkerRoleQ7",
            "finding": "REV-424242",
        }
        body = self.f.write(markers["path"] + ".md", "Marker body.\n")
        self.run_cli("init", "--implementer-account", markers["account"],
                     "--reviewer-account", "reviewer",
                     "--base-branch", markers["branch"])
        self.f.initialize()
        head = self.f.candidate()
        self.f.create_pr()
        pr = "1"
        for args in (
            ("issue", "create", "--as", "implementer", "--title",
             markers["title"], "--body", body),
            ("issue", "comment", "--as", "implementer", "--issue", "1",
             "--body", body),
            ("review-worktree", "create", "--pr", pr, "--head", head),
            ("thread", "reply", "--as", "implementer", "--pr", pr,
             "--finding", markers["finding"], "--body", body),
            (markers["word"],), ("pr", markers["word"]),
            ("pr", "head", "--as", markers["role"], "--pr", pr),
            ("pr", "head", "--pr", markers["word"]),
        ):
            self.run_cli(*args)
        text = "".join(path.read_text()
                       for path in self.store.glob("events-*.jsonl"))
        for label, marker in {**markers, "head": head,
                              "root": str(self.f.root)}.items():
            with self.subTest(marker=label):
                self.assertNotIn(marker, text)
        groups = subcommands(parser())
        written = self.events()
        self.assertEqual(len(written), 11)
        for event in written:
            with self.subTest(event=event):
                self.assertIn(event["group"], [*groups, None])
                if event["group"] is not None:
                    self.assertIn(
                        event["command"],
                        [*subcommands(groups[event["group"]]), None])
                self.assertIn(event["role"], ("implementer", "reviewer", None))
                self.assertIn(event["forge"], ("github", "forgejo", None))
                for key in ("pr", "issue"):
                    self.assertIn(type(event[key]), (int, type(None)))


class ForgejoEventTests(unittest.TestCase):
    def test_forgejo_events_hash_the_configured_base_url(self) -> None:
        with ForgejoFixture() as f:
            f.initialize()
            result = f.run([sys.executable, "-m", "agent_squad", "issue",
                            "view", "--issue", "1"], cwd=f.repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            store = Path(f.env["XDG_STATE_HOME"]) / "agent-squad/events"
            [line] = (store / f"events-{month()}.jsonl").read_text(
            ).splitlines()
            event = json.loads(line)
            forge = json.loads(
                (f.repo / ".agent-squad/config.json").read_text())["forge"]
            self.assertTrue(forge["base_url"])
            text = "\n".join((forge["kind"], forge["base_url"],
                              forge["owner"], forge["repo"]))
            self.assertEqual(
                (event["forge"], event["project"]),
                ("forgejo", hashlib.sha256(text.encode()).hexdigest()[:16]),
            )


if __name__ == "__main__":
    unittest.main()
