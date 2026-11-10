"""Append one content-free event per CLI run to a user-level store.

The store is write-only: no command reads it, and nothing in it is authority
for any decision. ``docs/events.md`` is the committed schema.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import TYPE_CHECKING
import uuid

from . import __version__

if TYPE_CHECKING:
    from .initialization import ForgeConfiguration

SCHEMA_VERSION = 1
FIELDS = (
    "schema_version", "id", "time", "tool_version", "project", "forge",
    "group", "command", "role", "pr", "issue", "exit_code", "duration_ms",
)
DISABLE_VARIABLE = "AGENT_SQUAD_EVENTS"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
FILE_FLAGS = (
    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    | os.O_CLOEXEC
)

# The forge part of the configuration the current run read, if any.
_observed: ForgeConfiguration | None = None


def observe(forge: ForgeConfiguration) -> None:
    """Remember the configuration this run read, for its event."""
    global _observed
    _observed = forge


def forget() -> None:
    global _observed
    _observed = None


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
