"""Append one content-free event per CLI run to a user-level store.

The store is write-only: no command reads it, and nothing in it is authority
for any decision. ``docs/events.md`` is the committed schema.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import TYPE_CHECKING
import uuid

from . import __version__

if TYPE_CHECKING:
    from .initialization import ForgeConfiguration

SCHEMA_VERSION = 2
FIELDS = (
    "schema_version", "id", "time", "tool_version", "project", "forge",
    "group", "command", "role", "pr", "issue", "exit_code", "duration_ms",
    "outcome",
)
# The commands that describe what their write did, and the keys, in order,
# of each one's outcome (docs/events.md).
OUTCOME_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("review", "post"): (
        "verdict", "blocking", "optional", "pass", "resumed", "diff_lines",
        "diff_lines_since_previous", "findings_on_changed_lines",
    ),
    ("thread", "reply"): ("disposition", "verification", "rejection"),
    ("decision", "post"): (
        "kind", "merge_instruction", "recommendation", "budget",
        "task_amended", "reused",
    ),
    ("stop", "post"): ("reason",),
    ("reviewer", "launch"): (
        "reviewer_kind", "model", "effort", "reviewer_skill",
    ),
}
# The only argument values the store holds: a model or an effort level
# named in reviewer.start_args, and only in this form.
SETTING_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,63}")
# Per Reviewer kind and setting: the options naming it, and the Codex
# configuration key a -c or --config value names it with.
SETTING_OPTIONS = {
    "claude": {"model": ("--model",), "effort": ("--effort",)},
    "codex": {"model": ("--model", "-m"), "effort": ()},
}
CONFIG_KEYS = {"model": "model", "effort": "model_reasoning_effort"}
DISABLE_VARIABLE = "AGENT_SQUAD_EVENTS"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
FILE_FLAGS = (
    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    | os.O_CLOEXEC
)

Outcome = Callable[[], dict[str, object]]

# The forge part of the configuration the current run read, if any.
_observed: ForgeConfiguration | None = None
# What the run's write did, computed only when the event is recorded.
_outcome: Outcome | None = None


def observe(forge: ForgeConfiguration) -> None:
    """Remember the configuration this run read, for its event."""
    global _observed
    _observed = forge


def outcome(compute: Outcome) -> None:
    """Hand the recorder what this run's write did, once the write happened.

    ``compute`` runs only when the event is recorded, so a run with
    recording disabled computes nothing, and an error in it records
    ``outcome: null`` without changing the command.
    """
    global _outcome
    _outcome = compute


def forget() -> None:
    global _observed, _outcome
    _observed = None
    _outcome = None


def computed_outcome(
    group: str | None, command: str | None
) -> dict[str, object] | None:
    """Return the documented outcome, or None on any error or mismatch."""
    fields = OUTCOME_FIELDS.get((group, command))
    if _outcome is None or fields is None:
        return None
    try:
        value = _outcome()
    except Exception:
        return None
    if not isinstance(value, dict) or tuple(value) != fields or any(
        item is not None and type(item) not in (bool, int, str)
        for item in value.values()
    ):
        return None
    return value


def named_setting(
    kind: str, start_args: tuple[str, ...], setting: str
) -> str | None:
    """Read a model or effort level from the Reviewer's start arguments.

    Only the documented forms count, and only before ``--``: what follows
    it is positional, such as a prompt (REV-1). A setting named in none of
    them, or more than once, or whose value fails ``SETTING_VALUE``, is
    None; a profile or settings file is never read.
    """
    options = SETTING_OPTIONS.get(kind, {}).get(setting, ())
    config = CONFIG_KEYS[setting] + "=" if kind == "codex" else None
    values: list[str | None] = []
    # The separator ends the options, even where a value was due.
    tokens = iter(start_args[:list(start_args).index("--")]
                  if "--" in start_args else start_args)
    for token in tokens:
        if config is not None and token in ("-c", "--config"):
            item = next(tokens, None)
            if item is not None and item.startswith(config):
                value = item[len(config):]
                if len(value) >= 2 and value[0] == value[-1] and (
                    value[0] in "'\""
                ):
                    value = value[1:-1]
                values.append(value)
        elif token in options:
            values.append(next(tokens, None))
        elif token.startswith("--") and token.split("=", 1)[0] in options:
            values.append(token.split("=", 1)[1])
    if len(values) != 1 or values[0] is None:
        return None
    return values[0] if SETTING_VALUE.fullmatch(values[0]) else None


def skill_digest(path: Path) -> str | None:
    """Fingerprint a regular file, or None when it cannot be read."""
    try:
        # O_NONBLOCK keeps a FIFO at the path from blocking the command.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1 << 16):
            digest.update(chunk)
        return digest.hexdigest()[:16]
    except OSError:
        return None
    finally:
        os.close(descriptor)


def launch_outcome(kind: str, start_args: tuple[str, ...]) -> Outcome:
    """The Reviewer's configuration, for a run whose Reviewer started."""
    kind = str(kind)  # The configuration's StrEnum, as its plain word.

    def compute() -> dict[str, object]:
        try:
            home = Path.home()
        except (OSError, RuntimeError):
            skill = None
        else:
            skill = skill_digest(
                home / ".agents/skills/squad-reviewer/SKILL.md"
            )
        return {
            "reviewer_kind": kind,
            "model": named_setting(kind, start_args, "model"),
            "effort": named_setting(kind, start_args, "effort"),
            "reviewer_skill": skill,
        }
    return compute


def disabled(environ: Mapping[str, str]) -> bool:
    return environ.get(DISABLE_VARIABLE) == "off"


def store_directory(environ: Mapping[str, str]) -> Path:
    """Return the events directory under the XDG state directory.

    A relative XDG_STATE_HOME is invalid under the XDG specification and is
    ignored, so a relative value never places the store in a repository.
    """
    value = environ.get("XDG_STATE_HOME", "")
    base = Path(value) if os.path.isabs(value) else (
        Path.home() / ".local/state"
    )
    if not base.is_absolute():
        raise OSError(f"state directory is not absolute: {base}")
    return base / "agent-squad/events"


def file_name(when: datetime) -> str:
    return when.strftime("events-%Y-%m.jsonl")


def open_owned_directory(name: str, parent: int) -> int:
    """Create or open one store directory without following a link."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    return os.open(name, DIRECTORY_FLAGS | os.O_NOFOLLOW, dir_fd=parent)


def open_store(directory: Path, name: str) -> int:
    """Open the month's file for appending, creating what is missing.

    The state directory itself may be reached through links; the two
    directories this tool owns and the file are never followed.
    """
    os.makedirs(directory.parent.parent, mode=0o700, exist_ok=True)
    descriptors = [os.open(directory.parent.parent, DIRECTORY_FLAGS)]
    try:
        for part in (directory.parent.name, directory.name):
            descriptors.append(open_owned_directory(part, descriptors[-1]))
        # O_NONBLOCK keeps a FIFO at the path from blocking the command.
        descriptor = os.open(name, FILE_FLAGS, 0o600, dir_fd=descriptors[-1])
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise OSError(f"not a regular file: {directory / name}")
        return descriptor
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def project_id(forge: ForgeConfiguration) -> str:
    """Hash the configured forge identity; no part of it is stored."""
    text = "\n".join(
        (forge.kind, forge.base_url or "", forge.owner, forge.repo)
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def subcommands(
    current: argparse.ArgumentParser,
) -> dict[str, argparse.ArgumentParser]:
    for action in current._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def command_names(
    root: argparse.ArgumentParser, arguments: list[str]
) -> tuple[str | None, str | None]:
    """Name the group and command only where the CLI defines the words."""
    groups = subcommands(root)
    if not arguments or arguments[0] not in groups:
        return None, None
    commands = subcommands(groups[arguments[0]])
    if len(arguments) > 1 and arguments[1] in commands:
        return arguments[0], arguments[1]
    return arguments[0], None


def run_event(
    root: argparse.ArgumentParser,
    arguments: list[str],
    args: argparse.Namespace | None,
    exit_code: int,
    started: float,
    ended: datetime,
) -> dict[str, object]:
    if args is None:
        group, command = command_names(root, arguments)
        role = pr = issue = None
    else:
        group, command = args.group, getattr(args, "command", None)
        role = getattr(args, "role", None)
        pr, issue = getattr(args, "pr", None), getattr(args, "issue", None)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "time": ended.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{ended.microsecond // 1000:03d}Z",
        "tool_version": __version__,
        "project": None if _observed is None else project_id(_observed),
        "forge": None if _observed is None else _observed.kind,
        "group": group,
        "command": command,
        "role": role,
        "pr": pr,
        "issue": issue,
        "exit_code": exit_code,
        "duration_ms": round((time.monotonic() - started) * 1000),
        # Last, so computing it never counts toward the run's duration.
        "outcome": computed_outcome(group, command),
    }


def record(
    root: argparse.ArgumentParser,
    arguments: list[str],
    args: argparse.Namespace | None,
    exit_code: int,
    started: float,
    environ: Mapping[str, str] = os.environ,
) -> None:
    """Append the run's event; any failure leaves the command unchanged."""
    try:
        if disabled(environ):
            return
        ended = datetime.now(timezone.utc)
        event = run_event(root, arguments, args, exit_code, started, ended)
        line = (json.dumps(event, separators=(",", ":")) + "\n").encode()
        descriptor = open_store(store_directory(environ), file_name(ended))
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)
    except Exception:
        pass


def diagnose(environ: Mapping[str, str] = os.environ) -> tuple[str, str]:
    """Return doctor's severity and detail; never a failure."""
    if disabled(environ):
        return "pass", f"recording disabled by {DISABLE_VARIABLE}=off"
    directory: object = "the state directory"
    try:
        directory = store_directory(environ)
        os.close(open_store(
            directory, file_name(datetime.now(timezone.utc))
        ))
    except (OSError, RuntimeError, ValueError) as error:
        return "warn", (
            f"not writable: {directory}: {error}; commands still run and"
            f" record nothing; set {DISABLE_VARIABLE}=off to stop recording"
        )
    return "pass", f"writable: {directory}"
