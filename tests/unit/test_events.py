"""The event store's location, fields, and silence on failure (#148)."""

import contextlib
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

from agent_squad import __version__, events  # noqa: E402
from agent_squad.cli import main, parser  # noqa: E402
from agent_squad.initialization import ForgeConfiguration  # noqa: E402


def documented_fields() -> list[str]:
    """The first column of the field table in docs/events.md."""
    text = (PROJECT_ROOT / "docs/events.md").read_text(encoding="utf-8")
    table = text.split("## Fields\n", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| `([a-z_]+)` \|", table, re.MULTILINE)


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
            if isinstance(result, BaseException)
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
                r"^\| `([a-z_]+)` \| (.*) \|$", text, re.MULTILINE)
        }
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
            {"schema_version": 1, "tool_version": __version__,
             "project": None, "forge": None,
             "group": "pr", "command": "head", "role": None, "pr": 7,
             "issue": None, "exit_code": 0},
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
