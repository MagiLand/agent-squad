"""Command-line interface for Agent Squad."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from . import __version__
from .initialization import AgentSquadError, initialize_repository


def build_parser() -> argparse.ArgumentParser:
    """Build the public command-line parser."""

    parser = argparse.ArgumentParser(
        prog="agent-squad",
        description=(
            "Coordinate one implementation agent and one independent review "
            "agent in a local Git repository."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser(
        "init",
        help="initialize Agent Squad in the current Git worktree",
        description=(
            "Create or validate the canonical local Agent Squad configuration "
            "and add its runtime paths to Git's repository-local exclude file."
        ),
    )
    init_parser.set_defaults(handler=_run_init)
    return parser


def _run_init(_arguments: argparse.Namespace) -> int:
    result = initialize_repository(Path.cwd())
    configuration_action = (
        "created" if result.configuration_created else "validated"
    )
    exclusion_action = (
        "updated" if result.git_exclude_updated else "already configured"
    )

    print(f"Initialized Agent Squad in {result.repository_root}")
    print(f"Configuration {configuration_action}: {result.configuration_path}")
    print(f"Git local exclude {exclusion_action}: {result.git_exclude_path}")
    print("No active run was created.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Agent Squad command-line interface."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except AgentSquadError as error:
        print(f"agent-squad: error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
