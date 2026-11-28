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

from tests.forge_support import (
    TASK, ForgeFixture, ForgejoFixture, finding,
)
from agent_squad.cli import parser
from agent_squad.events import FIELDS, OUTCOME_FIELDS, subcommands
from agent_squad.skills import packaged_skill


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
        # init records the configuration it wrote.
        self.assertEqual(
            [(e["group"], e["command"], e["exit_code"], e["project"])
             for e in self.events()[:2]],
            [("init", None, 0, project), ("pr", "create", 0, project)],
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
            "finding": "REV-424242", "directory": "MarkerDirectoryQ7",
            "secret": "MarkerSecretQ7", "profile": "MarkerProfileQ7",
        }
        body = self.f.write(markers["path"] + ".md", "Marker body.\n")
        self.run_cli("init", "--implementer-account", markers["account"],
                     "--reviewer-account", "reviewer",
                     "--base-branch", markers["branch"])
        self.f.initialize()
        # Only the model and the effort level of start_args are recorded.
        path = self.f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config["reviewer"].update(kind="codex", start_args=[
            "--add-dir", str(self.f.root / markers["directory"]),
            "-c", f'api_key="{markers["secret"]}"',
            "-c", f"model_provider={markers['secret']}",
            "-p", markers["profile"], "-m", "gpt-6.2-sol",
            "-c", "model_reasoning_effort=high",
        ])
        path.write_text(json.dumps(config))
        head = self.f.candidate()
        self.f.create_pr()
        pr = "1"
        self.run_cli("reviewer", "launch", "--pr", pr)
        self.f.review("changes_requested", [finding(), finding(
            "optional", markers["title"], line=3)])
        self.f.reply("REV-1", markers["word"], disposition="rejected")
        self.f.reply("REV-2", markers["word"], disposition="rejected",
                     deferred_to=1)
        self.f.decision(budget=4, body=markers["word"])
        self.run_cli("stop", "post", "--as", "reviewer", "--pr", pr,
                     "--head", head, "--reason", "scope", "--body", body)
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
        for label, marker in {**markers, "head": head, "base": self.f.base,
                              "root": str(self.f.root),
                              "any finding": "REV-"}.items():
            with self.subTest(marker=label):
                self.assertNotIn(marker, text)
        groups = subcommands(parser())
        written = self.events()
        self.assertEqual(len(written), 17)
        self.assertEqual(
            [e["command"] for e in written if e["outcome"] is not None],
            ["launch", "post", "reply", "reply", "post", "post"])
        launch = next(e for e in written if e["command"] == "launch")
        self.assertEqual(
            (launch["outcome"]["model"], launch["outcome"]["effort"]),
            ("gpt-6.2-sol", "high"))
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
                for value in (event["outcome"] or {}).values():
                    self.assertIn(type(value), (int, bool, str, type(None)))


def read_events(store: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in sorted(store.glob("events-*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


class OutcomeTests:
    """Each command's outcome, on the fake forge ``fixture`` names."""

    fixture = ForgeFixture

    def setUp(self) -> None:
        self.f = self.fixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()
        self.store = Path(self.f.env["XDG_STATE_HOME"]) / "agent-squad/events"

    def last(self, group: str, command: str | None,
             exit_code: int = 0) -> dict | None:
        """The latest event's outcome, checking its command and exit."""
        event = read_events(self.store)[-1]
        self.assertEqual(list(event), list(FIELDS))
        self.assertEqual(event["schema_version"], 2)
        self.assertEqual(
            (event["group"], event["command"], event["exit_code"]),
            (group, command, exit_code),
        )
        if event["outcome"] is not None:
            self.assertEqual(list(event["outcome"]),
                             list(OUTCOME_FIELDS[group, command]))
        return event["outcome"]

    def configure_reviewer(self, kind: str, start_args: list[str]) -> None:
        path = self.f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config["reviewer"].update(kind=kind, start_args=start_args)
        path.write_text(json.dumps(config))

    def stop(self, reason: str, expected: int = 0) -> dict:
        return self.f.cli(
            "stop", "post", "--as", "reviewer", "--pr", "1", "--head",
            self.head, "--reason", reason, "--body",
            self.f.write("stop.md", "Scripted remaining problems."),
            expected=expected,
        )

    def test_each_command_records_its_outcome(self) -> None:
        f = self.f
        self.configure_reviewer(
            "claude", ["--model", "claude-opus-5-5", "--effort=high"])
        f.cli("reviewer", "launch", "--pr", "1")
        self.assertEqual(self.last("reviewer", "launch"), {
            "reviewer_kind": "claude", "model": "claude-opus-5-5",
            "effort": "high",
            "reviewer_skill": digest(packaged_skill("squad-reviewer")),
        })
        f.review("changes_requested", [
            finding(), finding("optional", "Optional", line=3)])
        self.assertEqual(self.last("review", "post"), {
            "verdict": "changes_requested", "blocking": 1, "optional": 1,
            "pass": 1, "resumed": False, "diff_lines": 2,
            "diff_lines_since_previous": None,
            "findings_on_changed_lines": None,
        })
        for fid, options, expected in (
            ("REV-1", {"disposition": "rejected"},
             {"disposition": "rejected", "verification": None,
              "rejection": None}),
            ("REV-1", {"verification": "rejection-accepted"},
             {"disposition": None, "verification": "rejection-accepted",
              "rejection": None}),
            ("REV-2", {"disposition": "rejected", "not_pursued": True},
             {"disposition": "rejected", "verification": None,
              "rejection": "not-pursued"}),
            ("REV-2", {"disposition": "rejected", "deferred_to": 1},
             {"disposition": "rejected", "verification": None,
              "rejection": "deferred"}),
            # A reply without a tagged line still has an outcome.
            ("REV-1", {},
             {"disposition": None, "verification": None,
              "rejection": None}),
        ):
            with self.subTest(fid=fid, options=options):
                f.reply(fid, "Scripted reply prose.", **options)
                self.assertEqual(self.last("thread", "reply"), expected)
        f.decision(fid="REV-1")
        self.assertEqual(self.last("decision", "post"), {
            "kind": "finding", "merge_instruction": None,
            "recommendation": "followed", "budget": None,
            "task_amended": False, "reused": False,
        })
        f.decision(budget=5, recommendation="changed")
        self.assertEqual(self.last("decision", "post"), {
            "kind": "budget", "merge_instruction": None,
            "recommendation": "changed", "budget": 5,
            "task_amended": False, "reused": False,
        })
        f.decision(merge_instruction="record")
        self.assertEqual(self.last("decision", "post"), {
            "kind": None, "merge_instruction": "record",
            "recommendation": None, "budget": None,
            "task_amended": False, "reused": False,
        })
        self.stop("scope")
        self.assertEqual(self.last("stop", "post"), {"reason": "scope"})
        # Refused runs record no outcome.
        f.cli("reviewer", "launch", "--pr", "1", expected=4)
        self.assertIsNone(self.last("reviewer", "launch", 4))
        f.decision(budget=6, expected=4)
        self.assertIsNone(self.last("decision", "post", 4))
        f.review("approved", [finding()], expected=4)
        self.assertIsNone(self.last("review", "post", 4))
        # So do a usage error and a command without an outcome.
        f.cli("stop", "post", "--as", "reviewer", "--pr", "1", expected=2)
        self.assertIsNone(self.last("stop", "post", 2))
        f.status()
        self.assertIsNone(self.last("status", None))
        f.cli("reviewer", "close", "--pr", "1")
        self.assertIsNone(self.last("reviewer", "close"))


class GitHubOutcomeTests(OutcomeTests, unittest.TestCase):
    def push_lines(self, *lines: str) -> str:
        self.head = self.f.push("".join(line + "\n" for line in lines))
        return self.head

    def test_a_second_pass_measures_the_change_since_the_first(self) -> None:
        f = self.f
        self.push_lines("value = 1", "second = 2", "third = 3", "fourth = 4")
        f.review("changes_requested", [finding()])
        self.push_lines("value = 1", "second = 2", "third = 30",
                        "fourth = 4")
        f.review("changes_requested", [
            # A changed line, an unchanged one beside it, and a range whose
            # start_line alone changed.
            finding(title="Changed", line=3),
            finding("optional", "Beside", line=2),
            {**finding(title="Range", line=4), "start_line": 3},
            finding("optional", "Unchanged", line=1),
        ])
        self.assertEqual(self.last("review", "post"), {
            "verdict": "changes_requested", "blocking": 2, "optional": 2,
            "pass": 2, "resumed": False, "diff_lines": 3,
            "diff_lines_since_previous": 2,
            "findings_on_changed_lines": 2,
        })

    def test_an_equal_or_unrelated_previous_head(self) -> None:
        f = self.f
        f.review("approved")
        f.review("approved")
        self.assertEqual(self.last("review", "post"), {
            "verdict": "approved", "blocking": 0, "optional": 0, "pass": 2,
            "resumed": False, "diff_lines": 2,
            "diff_lines_since_previous": 0, "findings_on_changed_lines": 0,
        })
        # A rewritten branch: the reviewed head is no longer an ancestor.
        f.git("reset", "--hard", f.base, cwd=f.worktree)
        (f.worktree / "example.py").write_text("value = 1\nother = 2\n")
        f.git("commit", "-am", "test: rewrite", cwd=f.worktree)
        f.git("push", "--force", "origin", "HEAD", cwd=f.worktree)
        f.review("needs_human")
        self.assertEqual(self.last("review", "post"), {
            "verdict": "needs_human", "blocking": 0, "optional": 0,
            "pass": 3, "resumed": False, "diff_lines": 1,
            "diff_lines_since_previous": None,
            "findings_on_changed_lines": None,
        })

    def test_a_binary_file_counts_zero(self) -> None:
        f = self.f
        (f.worktree / "data.bin").write_bytes(b"\x00\x01\x02\n\x00")
        f.git("add", "data.bin", cwd=f.worktree)
        f.git("commit", "-m", "test: add binary", cwd=f.worktree)
        f.git("push", "origin", "HEAD", cwd=f.worktree)
        f.review("approved")
        self.assertEqual(self.last("review", "post")["diff_lines"], 2)
        (f.worktree / "data.bin").write_bytes(b"\x00\x03\n\x00\x04\n")
        f.git("commit", "-am", "test: change binary", cwd=f.worktree)
        f.git("push", "origin", "HEAD", cwd=f.worktree)
        f.review("approved")
        outcome = self.last("review", "post")
        self.assertEqual(
            (outcome["diff_lines"], outcome["diff_lines_since_previous"]),
            (2, 0))

    def test_a_review_whose_root_fails_and_its_resumption(self) -> None:
        f = self.f
        f.review("changes_requested", [finding()])
        self.push_lines("value = 1", "second = 2", "third = 30")
        f.settings(reject_batch=True, fail_roots=["REV-2"])
        failed = f.review("changes_requested", [finding(line=3)],
                          expected=1)
        self.assertIn("REV-2", failed["error"])
        expected = {
            "verdict": "changes_requested", "blocking": 1, "optional": 0,
            "pass": 2, "resumed": False, "diff_lines": 2,
            "diff_lines_since_previous": 2, "findings_on_changed_lines": 1,
        }
        self.assertEqual(self.last("review", "post", 1), expected)
        f.settings(fail_roots=[])
        ident = f.status()["reviews"][1]["id"]
        f.review("changes_requested", [finding(line=3)], resume=ident)
        self.assertEqual(self.last("review", "post"),
                         {**expected, "resumed": True})
        # A refused resumption describes nothing.
        f.review("changes_requested", [finding(title="Other", line=3)],
                 resume=ident, expected=1)
        self.assertIsNone(self.last("review", "post", 1))

    def test_a_failed_task_mirror_and_its_reused_amendment(self) -> None:
        f = self.f
        f.settings(fail_mirror=True)
        task = TASK.replace("Exercise", "Amend")
        f.decision(task=task, expected=3)
        expected = {
            "kind": "task-amendment", "merge_instruction": None,
            "recommendation": "followed", "budget": None,
            "task_amended": True, "reused": False,
        }
        self.assertEqual(self.last("decision", "post", 3), expected)
        f.settings(fail_mirror=False)
        f.decision(task=task)
        self.assertEqual(self.last("decision", "post"),
                         {**expected, "reused": True})
        f.decision(merge_instruction="withdraw")
        self.assertEqual(self.last("decision", "post"), {
            "kind": None, "merge_instruction": "withdraw",
            "recommendation": None, "budget": None,
            "task_amended": False, "reused": False,
        })

    def test_a_blocked_reviewer_records_its_launch(self) -> None:
        f = self.f
        self.configure_reviewer("codex", ["-m", "gpt-6.2-sol"])
        for settings in ({"start_blocked_success": True},
                         {"start_not_ready": True}):
            with self.subTest(settings=settings):
                f.herdr_settings(**{"start_blocked_success": False,
                                    "start_not_ready": False, **settings})
                f.cli("reviewer", "launch", "--pr", "1", expected=3)
                self.assertEqual(self.last("reviewer", "launch", 3), {
                    "reviewer_kind": "codex", "model": "gpt-6.2-sol",
                    "effort": None,
                    "reviewer_skill": digest(
                        packaged_skill("squad-reviewer")),
                })
                f.cli("reviewer", "close", "--pr", "1")
        # A start that times out has no Reviewer for adopt: no outcome.
        f.herdr_settings(start_blocked_success=False, start_not_ready=False,
                         start_state="working")
        f.cli("reviewer", "launch", "--pr", "1", expected=1)
        self.assertIsNone(self.last("reviewer", "launch", 1))


class GitHubLaunchFormTests(unittest.TestCase):
    """Every accepted form through a real launch with the fake Herdr."""

    def test_reviewer_launch_records_only_accepted_forms(self) -> None:
        f = ForgeFixture()
        self.addCleanup(f.close)
        f.initialize()
        f.candidate()
        f.create_pr()
        store = Path(f.env["XDG_STATE_HOME"]) / "agent-squad/events"
        skill = f.home / ".agents/skills/squad-reviewer/SKILL.md"
        path = f.repo / ".agent-squad/config.json"
        for kind, start_args, model, effort in (
            ("claude", ["--model", "opus-a", "--effort", "low"],
             "opus-a", "low"),
            ("claude", ["--model=opus-b", "--effort=max"], "opus-b", "max"),
            ("codex", ["--model", "c1", "-c", "model_reasoning_effort=high"],
             "c1", "high"),
            ("codex", ["--model=c2", "--config",
                       "model_reasoning_effort='low'"], "c2", "low"),
            ("codex", ["-m", "c3"], "c3", None),
            ("codex", ["-c", 'model="c4"'], "c4", None),
            ("codex", ["--config", "model='c5'"], "c5", None),
            # A value outside the pattern and a repeated form.
            ("claude", ["--model", "/etc/x", "--effort", "a",
                        "--effort", "a"], None, None),
            # A profile or settings file alone, and absent arguments.
            ("codex", ["-p", "fast"], None, None),
            ("claude", ["--settings", "s.json"], None, None),
            ("claude", [], None, None),
        ):
            with self.subTest(kind=kind, start_args=start_args):
                config = json.loads(path.read_text())
                config["reviewer"].update(kind=kind, start_args=start_args)
                path.write_text(json.dumps(config))
                if not start_args:
                    skill.unlink()  # The last case: no installed skill.
                f.cli("reviewer", "launch", "--pr", "1")
                event = read_events(store)[-1]
                self.assertEqual(
                    (event["command"], event["exit_code"]), ("launch", 0))
                self.assertEqual(event["outcome"], {
                    "reviewer_kind": kind, "model": model, "effort": effort,
                    "reviewer_skill": None if not start_args else digest(
                        packaged_skill("squad-reviewer")),
                })
                f.cli("reviewer", "close", "--pr", "1")


class ForgejoOutcomeTests(OutcomeTests, unittest.TestCase):
    fixture = ForgejoFixture


class OutcomeSideEffectTests(unittest.TestCase):
    """An outcome's own git work never shows in the command (item 6)."""

    def test_a_failing_diff_and_disabled_recording_change_nothing(
        self,
    ) -> None:
        f = ForgeFixture()
        self.addCleanup(f.close)
        f.initialize()
        f.candidate()
        f.create_pr()
        store = Path(f.env["XDG_STATE_HOME"]) / "agent-squad/events"
        log = f.root / "git.log"
        real = shutil.which("git", path=f.env["PATH"])
        wrapper = f.bin / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$OUTCOME_GIT_LOG"\n'
            'case " $* " in *" --numstat "*)\n'
            '  [ -n "$OUTCOME_GIT_FAIL" ] && exit 1;;\n'
            "esac\n"
            f'exec "{real}" "$@"\n'
        )
        wrapper.chmod(0o755)
        f.env["OUTCOME_GIT_LOG"] = str(log)
        f.settings(reject_batch=True, fail_roots=["REV-1"])
        f.review("changes_requested", [finding()], expected=1)
        f.settings(fail_roots=[])
        ident = f.status()["reviews"][0]["id"]
        f.review("changes_requested", [finding()], resume=ident)
        self.assertIn("--numstat", log.read_text())
        arguments = f.history[-1]["command"][1:]

        def run(**env: str) -> tuple[int, str, str]:
            saved = dict(f.env)
            f.env.update(env)
            log.write_text("")
            try:
                result = f.run(
                    [sys.executable, "-m", "agent_squad", *arguments,
                     "--json"], cwd=f.worktree)
            finally:
                f.env = saved
            return result.returncode, result.stdout, result.stderr

        before = len(read_events(store))
        disabled = run(AGENT_SQUAD_EVENTS="off")
        calls = log.read_text()
        self.assertEqual(len(read_events(store)), before)
        self.assertNotIn("--numstat", calls)
        self.assertNotIn("--unified=0", calls)
        failing = run(OUTCOME_GIT_FAIL="1")
        self.assertIn("--numstat", log.read_text())
        self.assertEqual(failing, disabled)
        self.assertEqual(disabled[0], 0)
        event = read_events(store)[-1]
        self.assertEqual((event["command"], event["exit_code"],
                          event["outcome"]), ("post", 0, None))


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
