"""The event store's location, fields, and silence on failure (#148)."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tests._support import PROJECT_ROOT, add_src_to_path

add_src_to_path()

from agent_squad import __version__, conventions, events  # noqa: E402
from agent_squad.cli import main, parser  # noqa: E402
from agent_squad.initialization import (  # noqa: E402
    AgentKind, ForgeConfiguration,
)


STOP = ("stop", "post", "--as", "reviewer", "--pr", "1", "--head", "h",
        "--reason")


def documented_section(name: str) -> str:
    text = (PROJECT_ROOT / "docs/events.md").read_text(encoding="utf-8")
    return text.split(f"## {name}\n", 1)[1].split("\n## ", 1)[0]


def documented_fields() -> list[str]:
    """The first column of the field table in docs/events.md."""
    return re.findall(r"^\| `([a-z_]+)` \|", documented_section("Fields"),
                      re.MULTILINE)


def documented_outcomes() -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Each command's outcome keys and value texts, in documented order."""
    rows: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for match in re.finditer(
        r"^\| `([a-z-]+) ([a-z-]+)` \| `([a-z_]+)` \| (.*) \|$",
        documented_section("Outcomes"), re.MULTILINE,
    ):
        rows.setdefault((match[1], match[2]), []).append((match[3], match[4]))
    return rows


def words(text: str) -> list[str]:
    """The fixed words a documented value names, without null or booleans."""
    return [
        value for value in re.findall(r"`([^`]+)`", text)
        if re.fullmatch(r"[a-z][a-z_-]*", value)
        and value not in ("null", "true", "false")
    ]


def pattern_words(kind: str, group: str) -> list[str]:
    """The alternatives of one named group of a tagged-line pattern."""
    source = conventions.PATTERNS[kind].pattern
    return re.search(rf"\(\?P<{group}>([a-z_|-]+)\)", source)[1].split("|")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environ = {"XDG_STATE_HOME": str(self.root / "state")}
        self.store = self.root / "state/agent-squad/events"

    def run_cli(self, *arguments: str, environ: dict | None = None,
                result: object = None) -> tuple[object, str, str]:
        """Run main in-process against this test's store."""
        stdout, stderr = io.StringIO(), io.StringIO()
        execute = (
            patch("agent_squad.cli.execute", side_effect=result)
            if isinstance(result, BaseException) or callable(result)
            else patch("agent_squad.cli.execute", return_value=result or {})
        )
        values = dict(self.environ if environ is None else environ)
        with (
            patch.dict(os.environ, values),
            execute,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            # Only the given variables choose the store, never the caller's.
            for key in ("AGENT_SQUAD_EVENTS", "XDG_STATE_HOME"):
                if key not in values:
                    os.environ.pop(key, None)
            try:
                code: object = main(list(arguments))
            except SystemExit as stop:
                code = ("exit", stop.code)
        return code, stdout.getvalue(), stderr.getvalue()

    def events(self) -> list[dict]:
        return [
            json.loads(line)
            for path in sorted(self.store.glob("events-*.jsonl"))
            for line in path.read_text().splitlines()
        ]

    def test_suite_runs_with_the_store_off(self) -> None:
        self.assertEqual(os.environ.get("AGENT_SQUAD_EVENTS"), "off")

    def test_documented_values_equal_the_cli_lists(self) -> None:
        text = (PROJECT_ROOT / "docs/events.md").read_text(encoding="utf-8")
        rows = {
            match[1]: [
                value for value in re.findall(r"`([a-z-]+)`", match[2])
                if value != "null"
            ]
            for match in re.finditer(
                r"^\| `([a-z_]+)` \| (.*) \|$", documented_section("Fields"),
                re.MULTILINE)
        }
        self.assertIn("schema version 2", text)
        groups = events.subcommands(parser())
        self.assertEqual(rows["group"], list(groups))
        roles = {
            choice
            for group in groups.values()
            for leaf in (events.subcommands(group) or {"": group}).values()
            for action in leaf._actions if "--as" in action.option_strings
            for choice in action.choices
        }
        self.assertEqual(
            [value for value in rows["role"] if not value.startswith("-")],
            sorted(roles),
        )
        self.assertEqual(rows["forge"], ["github", "forgejo"])
        self.assertEqual(
            sorted(name for name, p in groups.items()
                   if not events.subcommands(p)),
            ["doctor", "init", "status"],
        )

    def test_documented_outcomes_equal_the_cli_fields_and_words(
        self,
    ) -> None:
        documented = documented_outcomes()
        self.assertEqual(
            {command: [key for key, _ in rows]
             for command, rows in documented.items()},
            {command: list(keys)
             for command, keys in events.OUTCOME_FIELDS.items()},
        )
        groups = events.subcommands(parser())
        for group, command in events.OUTCOME_FIELDS:
            self.assertIn(command, events.subcommands(groups[group]))
        choices = {
            action.dest: list(action.choices)
            for group in groups.values()
            for leaf in events.subcommands(group).values()
            for action in leaf._actions if action.choices
        }
        values = {
            (command, key): words(text)
            for command, rows in documented.items() for key, text in rows
        }
        for key, expected in (
            (("review", "post", "verdict"), list(conventions.VERDICTS)),
            (("thread", "reply", "disposition"), choices["disposition"]),
            (("thread", "reply", "verification"), choices["verification"]),
            (("thread", "reply", "rejection"), ["not-pursued", "deferred"]),
            (("decision", "post", "kind"),
             ["finding", *conventions.GENERAL_KINDS]),
            (("decision", "post", "merge_instruction"),
             choices["merge_instruction"]),
            (("decision", "post", "recommendation"),
             choices["recommendation"]),
            (("stop", "post", "reason"), pattern_words("stop", "reason")),
            (("reviewer", "launch", "reviewer_kind"),
             [kind.value for kind in AgentKind]),
        ):
            with self.subTest(key=key):
                self.assertEqual(values[(key[0], key[1]), key[2]], expected)
        self.assertEqual(pattern_words("review", "verdict"),
                         list(conventions.VERDICTS))
        self.assertEqual(choices["recommendation"],
                         list(conventions.RECOMMENDATIONS))
        self.assertEqual(list(conventions.DISPOSITIONS),
                         choices["disposition"])
        self.assertEqual(choices["kind"], list(conventions.GENERAL_KINDS))
        # The documented forms and pattern are the ones the parser applies.
        configuration = documented_section("Outcomes").split(
            "### Reviewer configuration\n", 1)[1]
        forms = {
            match[1]: (match[2], match[3])
            for match in re.finditer(
                r"^\| `([a-z]+)` \| (.*) \| (.*) \|$", configuration,
                re.MULTILINE)
        }
        self.assertEqual(sorted(forms), sorted(events.SETTING_OPTIONS))
        for kind, texts in forms.items():
            for setting, text in zip(("model", "effort"), texts):
                with self.subTest(kind=kind, setting=setting):
                    for form in re.findall(r"`([^`]+)`", text):
                        arguments = tuple(form.replace("=X", "=v1").replace(
                            " X", " v1").split(" "))
                        self.assertEqual(events.named_setting(
                            kind, arguments, setting), "v1")
        self.assertIn(
            f"`^{events.SETTING_VALUE.pattern}$`", configuration)

    def test_location_follows_the_xdg_state_directory(self) -> None:
        home = self.root / "home"
        with patch.dict(os.environ, {"HOME": str(home)}):
            for value, expected in (
                (str(self.root / "s"), self.root / "s/agent-squad/events"),
                (None, home / ".local/state/agent-squad/events"),
                ("", home / ".local/state/agent-squad/events"),
                # A relative value is invalid under XDG and never lands the
                # store in the current repository.
                ("relative/state", home / ".local/state/agent-squad/events"),
            ):
                with self.subTest(value=value):
                    environ = {} if value is None else {
                        "XDG_STATE_HOME": value}
                    self.assertEqual(events.store_directory(environ),
                                     expected)

    def test_project_hashes_the_documented_recipe(self) -> None:
        self.assertEqual(events.project_id(ForgeConfiguration(
            "github", "MagiLand", "agent-squad")), "bd476f319d518727")
        self.assertEqual(events.project_id(ForgeConfiguration(
            "forgejo", "team", "project",
            "https://forge.example.org/api/v1")), "935c0db9b7c57f9c")

    def test_command_names_come_only_from_the_parser(self) -> None:
        root = parser()
        for arguments, expected in (
            ([], (None, None)),
            (["MARKER"], (None, None)),
            (["--as", "implementer"], (None, None)),
            (["status", "--pr", "MARKER"], ("status", None)),
            (["pr"], ("pr", None)),
            (["pr", "MARKER"], ("pr", None)),
            (["pr", "--as", "merge"], ("pr", None)),
            (["pr", "merge", "--pr", "MARKER"], ("pr", "merge")),
            (["skill", "install", "MARKER"], ("skill", "install")),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(events.command_names(root, arguments),
                                 expected)

    def test_one_event_per_run_with_the_documented_fields(self) -> None:
        self.assertEqual(self.run_cli("pr", "head", "--pr", "7")[0], 0)
        written = self.events()
        self.assertEqual(len(written), 1)
        self.assertEqual(list(written[0]), list(events.FIELDS))
        self.assertEqual(documented_fields(), list(events.FIELDS))
        event = written[0]
        self.assertEqual(
            {k: event[k] for k in events.FIELDS
             if k not in ("id", "time", "duration_ms")},
            {"schema_version": 2, "tool_version": __version__,
             "project": None, "forge": None,
             "group": "pr", "command": "head", "role": None, "pr": 7,
             "issue": None, "exit_code": 0, "outcome": None},
        )
        self.assertRegex(event["id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4")
        self.assertRegex(
            event["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(
            sorted(self.store.iterdir()),
            [self.store / f"events-{event['time'][:7]}.jsonl"],
        )
        self.assertIsInstance(event["duration_ms"], int)

    def test_role_and_numbers_come_only_from_their_options(self) -> None:
        printed = {"issue": 9, "comments": []}
        self.run_cli("issue", "create", "--as", "implementer", "--title",
                     "t", "--body", "b.md", "--from-issue", "3",
                     "--from-pr", "4", result=printed)
        self.run_cli("issue", "view", "--issue", "5", result=printed)
        self.assertEqual(
            [(e["role"], e["pr"], e["issue"]) for e in self.events()],
            [("implementer", None, None), (None, None, 5)],
        )

    def test_usage_errors_are_recorded_and_help_is_not(self) -> None:
        for arguments in (["--help"], ["--version"], ["pr", "--help"],
                          ["pr", "merge", "--help"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_cli(*arguments)[0], ("exit", 0))
        self.assertEqual(self.events(), [])
        for arguments in (["pr", "merge", "--as", "MARKER"], ["MARKER"],
                          ["pr", "create", "--as", "implementer", "--issue",
                           "2", "--report", "r.md", "--partial"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_cli(*arguments)[0], ("exit", 2))
        self.assertEqual(
            [(e["group"], e["command"], e["role"], e["issue"],
              e["exit_code"]) for e in self.events()],
            [("pr", "merge", None, None, 2), (None, None, None, None, 2),
             # Found after parsing, so the parsed options are known.
             ("pr", "create", "implementer", 2, 2)],
        )

    def test_an_uncaught_exception_is_recorded_and_still_raised(
        self,
    ) -> None:
        with self.assertRaises(RuntimeError):
            self.run_cli("status", "--pr", "1", result=RuntimeError("x"))
        with self.assertRaises(KeyboardInterrupt):
            self.run_cli("status", "--pr", "1", result=KeyboardInterrupt())
        self.assertEqual([e["exit_code"] for e in self.events()], [1, 130])

    def test_each_run_forgets_the_previous_runs_configuration(self) -> None:
        events.observe(ForgeConfiguration("github", "owner", "repo"))
        self.run_cli("MARKER")
        self.assertEqual([e["project"] for e in self.events()], [None])

    def test_a_fifo_with_a_reader_is_not_written(self) -> None:
        self.store.mkdir(parents=True)
        name = "events-2026-01.jsonl"
        os.mkfifo(self.store / name)
        reader = os.open(self.store / name, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        with self.assertRaisesRegex(OSError, "not a regular file"):
            events.open_store(self.store, name)

    def test_disabled_recording_writes_nothing(self) -> None:
        environ = {**self.environ, "AGENT_SQUAD_EVENTS": "off"}
        self.run_cli("pr", "head", "--pr", "1", environ=environ)
        self.run_cli("MARKER", environ=environ)
        self.assertFalse((self.root / "state").exists())

    def test_a_failing_store_changes_no_output_or_exit_code(self) -> None:
        disabled = {**self.environ, "AGENT_SQUAD_EVENTS": "off"}
        failures = {
            "write": patch("agent_squad.events.os.write",
                           side_effect=OSError("disk full")),
            "event": patch("agent_squad.events.run_event",
                           side_effect=ValueError("bad")),
            "home": patch("agent_squad.events.Path.home",
                          side_effect=RuntimeError("no home")),
        }
        for name, failure in failures.items():
            for arguments, result in (
                (("pr", "head", "--pr", "1"), {"head": "x"}),
                (("pr", "head", "--pr", "1"), ValueError("forge failed")),
                (("pr", "head"), None),
            ):
                with self.subTest(failure=name, arguments=arguments):
                    expected = self.run_cli(
                        *arguments, environ=disabled, result=result)
                    with failure:
                        actual = self.run_cli(
                            *arguments,
                            environ={"HOME": str(self.root / "h")}
                            if name == "home" else self.environ,
                            result=result)
                    self.assertEqual(actual, expected)
        self.assertEqual(self.events(), [])

    def test_an_outcome_is_recorded_only_for_its_documented_command(
        self,
    ) -> None:
        reason = {"reason": "scope"}

        def stopped(args: object) -> dict:
            events.outcome(lambda: dict(reason))
            return {"id": 1}

        self.run_cli(*STOP, "scope", "--body", "b.md", result=stopped)
        # A command without an outcome ignores one handed over anyway.
        self.run_cli("pr", "head", "--pr", "1", result=stopped)
        # The next run starts without the previous run's outcome.
        self.run_cli(*STOP, "scope", "--body", "b.md", result={"id": 2})
        self.assertEqual([e["outcome"] for e in self.events()],
                         [reason, None, None])

    def test_an_outcome_of_the_wrong_shape_is_null(self) -> None:
        for value in (
            {"reason": "scope", "extra": 1}, {}, ["scope"], None,
            {"reason": 1.5}, {"reason": ["scope"]}, {"reason": {"x": 1}},
            # A string subclass, such as the configuration's StrEnum.
            {"reason": AgentKind.CLAUDE},
        ):
            with self.subTest(value=value):
                events.forget()
                events.outcome(lambda: value)
                self.assertIsNone(events.computed_outcome("stop", "post"))
        events.outcome(lambda: {"reason": None})
        self.assertEqual(events.computed_outcome("stop", "post"),
                         {"reason": None})
        self.assertIsNone(events.computed_outcome("status", None))
        events.forget()
        self.assertIsNone(events.computed_outcome("stop", "post"))

    def test_an_outcome_is_computed_last_and_only_when_recorded(
        self,
    ) -> None:
        clock = [0.0]
        calls = []

        def compute() -> dict:
            calls.append(True)
            clock[0] = 100.0  # Ten seconds of git would not be counted.
            return {"reason": "budget"}

        def stopped(args: object) -> dict:
            events.outcome(compute)
            return {}

        arguments = (*STOP, "budget", "--body", "b.md")
        disabled = {**self.environ, "AGENT_SQUAD_EVENTS": "off"}
        with patch("agent_squad.events.time.monotonic",
                   side_effect=lambda: clock[0]), \
                patch("agent_squad.cli.time.monotonic",
                      side_effect=lambda: clock[0]):
            self.run_cli(*arguments, environ=disabled, result=stopped)
            self.assertEqual(calls, [])
            self.run_cli(*arguments, result=stopped)
        self.assertEqual(calls, [True])
        [event] = self.events()
        self.assertEqual((event["duration_ms"], event["outcome"]),
                         (0, {"reason": "budget"}))

    def test_a_failing_outcome_changes_no_output_or_exit_code(self) -> None:
        disabled = {**self.environ, "AGENT_SQUAD_EVENTS": "off"}

        def failing(error: BaseException, value: object) -> object:
            def compute() -> dict:
                raise error

            def run(args: object) -> object:
                events.outcome(compute)
                if isinstance(value, BaseException):
                    raise value
                return value
            return run

        arguments = (*STOP, "repeat", "--body", "b.md")
        for error in (OSError("git failed"), ValueError("bad"),
                      RuntimeError("no home")):
            for value in ({"id": 1}, ValueError("forge failed")):
                with self.subTest(error=error, value=value):
                    expected = self.run_cli(
                        *arguments, environ=disabled,
                        result=failing(error, value))
                    actual = self.run_cli(
                        *arguments, result=failing(error, value))
                    self.assertEqual(actual, expected)
                    self.assertIsNone(self.events()[-1]["outcome"])
        self.assertEqual(
            [e["exit_code"] for e in self.events()], [0, 1] * 3)

    def test_named_settings_take_only_the_documented_forms(self) -> None:
        for kind, arguments, model, effort in (
            ("claude", ("--model", "opus", "--effort", "high"),
             "opus", "high"),
            ("claude", ("--model=claude-opus-5-5", "--effort=max"),
             "claude-opus-5-5", "max"),
            ("codex", ("--model", "gpt-6.2-sol"), "gpt-6.2-sol", None),
            ("codex", ("--model=o3",), "o3", None),
            ("codex", ("-m", "o3", "-c", "model_reasoning_effort=high"),
             "o3", "high"),
            ("codex", ("-c", 'model="o3"', "--config",
                       "model_reasoning_effort='low'"), "o3", "low"),
            ("codex", ("--config", "model='vendor/m:1@v+2'"),
             "vendor/m:1@v+2", None),
            # A profile or settings file beside a named setting is ignored.
            ("codex", ("-p", "fast", "-m", "o3"), "o3", None),
            ("claude", ("--settings", "s.json", "--model", "opus"),
             "opus", None),
            # Other options and their values are skipped, never recorded.
            ("codex", ("--add-dir", "/secret", "-c", "api_key=SECRET",
                       "-m", "o3"), "o3", None),
            # The kind decides the forms.
            ("claude", ("-m", "o3", "-c", "model=o3", "--config",
                        "model_reasoning_effort=high"), None, None),
            ("codex", ("--effort", "high", "--effort=high"), None, None),
            # Not named, or named another way.
            ("codex", (), None, None),
            ("codex", ("-p", "fast"), None, None),
            ("codex", ("--profile", "fast"), None, None),
            ("claude", ("--settings", "s.json"), None, None),
            ("codex", ("--config=model=o3", "-m=o3", "-mo3", "-c",
                       "model =o3"), None, None),
            ("claude", ("--model-x=o3", "--modelo3"), None, None),
            # Named more than once, even with the same value.
            ("codex", ("-m", "o3", "--model", "o3"), None, None),
            ("codex", ("-c", "model=o3", "-c", "model=o3"), None, None),
            ("claude", ("--effort", "high", "--effort=high"), "x", None),
            # A missing value.
            ("claude", ("--model",), None, None),
            ("codex", ("-c",), None, None),
            # After the separator every argument is positional (REV-1).
            ("claude", ("--", "--model", "opus", "--effort=high"),
             None, None),
            ("codex", ("--", "-m", "o3", "-c", "model=o3", "--config",
                       "model_reasoning_effort=high"), None, None),
            ("claude", ("--model", "opus", "--effort", "high", "--",
                        "--model=other", "--effort", "low"), "opus", "high"),
            ("codex", ("-m", "o3", "--", "-m", "o4"), "o3", None),
            # The separator ends the options even where a value was due.
            ("claude", ("--model", "--", "opus"), None, None),
            ("codex", ("-c", "--", "model=o3"), None, None),
            ("codex", ("--config", "--", "model_reasoning_effort=high"),
             None, None),
        ):
            if model == "x":  # Only the effort is repeated here.
                arguments = (*arguments, "--model", "x")
            with self.subTest(kind=kind, arguments=arguments):
                self.assertEqual(
                    (events.named_setting(kind, arguments, "model"),
                     events.named_setting(kind, arguments, "effort")),
                    (model, effort),
                )

    def test_a_named_setting_must_match_the_value_pattern(self) -> None:
        for value, expected in (
            ("a", "a"), ("A" * 64, "A" * 64), ("A" * 65, None),
            ("", None), ("-high", None), (".o3", None), ("o3\n", None),
            ("o3 high", None), ("o3\tx", None), ("model_x", "model_x"),
            ("o3;rm", None), ("tête", None), ("o3=4", None),
            ("~/.secret", None), ("/etc/token", None),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    events.named_setting("claude", ("--model", value),
                                         "model"), expected)
        # Quotes are removed once, and only from a -c or --config value.
        for item, expected in (
            ('model="o3"', "o3"), ("model='o3'", "o3"),
            ("model=\"'o3'\"", None), ("model=\"o3'", None),
            ('model="', None), ('model=""', None), ("model=o3", "o3"),
        ):
            with self.subTest(item=item):
                self.assertEqual(events.named_setting(
                    "codex", ("-c", item), "model"), expected)
        self.assertIsNone(events.named_setting(
            "claude", ("--model", '"o3"'), "model"))
        self.assertIsNone(events.named_setting("other", ("--model", "o3"),
                                               "model"))

    def test_the_skill_fingerprint_reads_only_a_regular_file(self) -> None:
        path = self.root / "SKILL.md"
        path.write_bytes(b"skill text\n")
        self.assertEqual(
            events.skill_digest(path),
            hashlib.sha256(b"skill text\n").hexdigest()[:16])
        self.assertIsNone(events.skill_digest(self.root / "missing.md"))
        self.assertIsNone(events.skill_digest(self.root))
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        self.assertIsNone(events.skill_digest(fifo))
        home = self.root / "home"
        installed = home / ".agents/skills/squad-reviewer/SKILL.md"
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"installed\n")
        compute = events.launch_outcome(
            AgentKind.CODEX, ("-m", "o3", "--add-dir", "/secret"))
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(compute(), {
                "reviewer_kind": "codex", "model": "o3", "effort": None,
                "reviewer_skill":
                    hashlib.sha256(b"installed\n").hexdigest()[:16],
            })
            self.assertIs(type(compute()["reviewer_kind"]), str)
            installed.unlink()
            self.assertIsNone(compute()["reviewer_skill"])
        with patch("agent_squad.events.Path.home",
                   side_effect=RuntimeError("no home")):
            self.assertIsNone(compute()["reviewer_skill"])

    def test_doctor_row_reports_writable_unwritable_and_disabled(
        self,
    ) -> None:
        blocked = self.root / "file"
        blocked.write_text("not a directory\n")
        for environ, severity, detail in (
            (self.environ, "pass", f"writable: {self.store}"),
            ({"XDG_STATE_HOME": str(blocked)}, "warn",
             f"not writable: {blocked}/agent-squad/events: "),
            # The temporary location keeps a broken check out of HOME.
            ({**self.environ, "AGENT_SQUAD_EVENTS": "off"}, "pass",
             "recording disabled by AGENT_SQUAD_EVENTS=off"),
        ):
            with self.subTest(severity=severity, detail=detail):
                shutil.rmtree(self.root / "state", ignore_errors=True)
                result = events.diagnose(environ)
                self.assertEqual(result[0], severity)
                self.assertTrue(result[1].startswith(detail), result[1])
                self.assertEqual((self.root / "state").exists(),
                                 detail.startswith("writable"))


if __name__ == "__main__":
    unittest.main()
