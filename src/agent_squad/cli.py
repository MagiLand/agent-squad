"""Command-line interface for Agent Squad."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from . import __version__
from .initialization import AgentKind, AgentSquadError, initialize_repository
from .runs import inspect_status, start_run


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

    start_parser = commands.add_parser(
        "start",
        help="start one authoritative Agent Squad run",
        description=(
            "Capture an approved task and explicit context, fix the Git base, "
            "and create one authoritative local run."
        ),
    )
    start_parser.add_argument(
        "--task",
        required=True,
        type=Path,
        metavar="PATH",
        help="UTF-8 Markdown task specification to capture",
    )
    start_parser.add_argument(
        "--context",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="explicit context file to capture; may be repeated",
    )
    start_parser.add_argument(
        "--implementer",
        metavar="AGENT",
        help=(
            "Implementer agent name within the configured Implementer kind; "
            "defaults to repository configuration"
        ),
    )
    start_parser.add_argument(
        "--reviewer",
        choices=[kind.value for kind in AgentKind],
        help="Reviewer kind; defaults to repository configuration",
    )
    start_parser.add_argument(
        "--base",
        metavar="REF",
        help="Git base reference; defaults to repository configuration",
    )
    start_parser.set_defaults(handler=_run_start)

    status_parser = commands.add_parser(
        "status",
        help="inspect the authoritative local run",
        description=(
            "Report whether this worktree has an active run and show its "
            "authoritative identity, phase, budget, and next action."
        ),
    )
    status_parser.set_defaults(handler=_run_status)
    return parser


def _run_init(_arguments: argparse.Namespace) -> int:
    result = initialize_repository(_invocation_directory())
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


def _invocation_directory() -> Path:
    try:
        return Path.cwd()
    except OSError as error:
        raise AgentSquadError(
            "cannot determine the current directory; it may have been "
            f"removed or become unreadable: {error}"
        ) from error


def _run_start(arguments: argparse.Namespace) -> int:
    reviewer_kind = None
    if arguments.reviewer is not None:
        reviewer_kind = AgentKind(arguments.reviewer)
    result = start_run(
        _invocation_directory(),
        task_path=arguments.task,
        context_paths=arguments.context,
        implementer_agent=arguments.implementer,
        reviewer_kind=reviewer_kind,
        base_ref=arguments.base,
    )
    print(f"Started Agent Squad run {result.run_id}")
    print(f"Phase: {result.phase.value}")
    print(f"Task: {result.task_path}")
    print(f"Repository ID: {result.repository_id}")
    print(f"Base: {result.base_ref} -> {result.base_oid}")
    print(f"Next action: {result.next_action}")
    return 0


def _run_status(_arguments: argparse.Namespace) -> int:
    status = inspect_status(_invocation_directory())
    print(f"Repository: {status.repository_root}")
    print(f"Repository ID: {status.repository_id}")
    print(f"Git common directory: {status.git_common_dir}")
    print(f"Worktree Git directory: {status.worktree_git_dir}")
    if status.active_run is None:
        print("Active run: none")
        print(f"Next action: {status.next_action}")
        return 0

    run = status.active_run
    branch = run.repository.start_branch_ref or "detached HEAD"
    print(f"Active run: {run.run_id}")
    print(f"Phase: {run.phase.value}")
    print(f"Task: {run.task_path}")
    print(f"Task SHA-256: {run.task_sha256}")
    print(f"Captured context files: {run.context_count}")
    print(
        f"Implementer: {run.implementer_agent} "
        f"({run.implementer_kind.value})"
    )
    print(f"Reviewer: {run.reviewer_kind.value}")
    print(f"Base: {run.base_ref} -> {run.base_oid}")
    print(f"Git object format: {run.git_object_format}")
    print(f"Started from: {branch}")
    print(
        "Current round: "
        f"{run.current_round if run.current_round else 'none'}"
    )
    print(f"Round status: {run.round_status or 'none'}")
    print(f"Submission mode: {run.submission_mode or 'none'}")
    print(f"Current requested head: {run.current_head_oid or 'none'}")
    print(f"Approved head: {run.approved_head_oid or 'none'}")
    print(f"Active escalation: {run.active_escalation_id or 'none'}")
    print(f"Request handoff: {run.handoff_status or 'none'}")
    print(f"Review worktree: {run.review_worktree or 'none'}")
    budget = run.review_budget
    print(
        "Review budget: "
        f"{budget.completed_change_reviews}/{budget.effective_limit} "
        "completed change reviews "
        f"(original {budget.original_limit}, "
        f"additional {budget.additional_rounds_granted})"
    )
    print(f"Next action: {run.next_action}")
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
