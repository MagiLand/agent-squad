"""Command-line interface for Agent Squad."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from . import __version__
from .artifacts import HandoffStatus, SubmissionMode
from .initialization import AgentKind, AgentSquadError, initialize_repository
from .review_applications import apply_review, complete_run
from .review_submissions import submit_review_result
from .runs import inspect_status, start_run
from .submissions import submit_candidate


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

    submit_parser = commands.add_parser(
        "submit",
        help="submit one exact committed revision for independent review",
        description=(
            "Validate the active run and committed candidate, create an "
            "immutable detached review worktree and self-contained bundle, "
            "then notify a deterministic round-scoped Reviewer through "
            "Herdr."
        ),
    )
    submit_parser.add_argument(
        "--report",
        required=True,
        type=Path,
        metavar="PATH",
        help="UTF-8 Markdown implementation report for this candidate",
    )
    submit_parser.add_argument(
        "--mode",
        required=True,
        choices=[mode.value for mode in SubmissionMode],
        help=(
            "relationship between this candidate and prior review; the "
            "first round requires new_revision"
        ),
    )
    submit_parser.set_defaults(handler=_run_submit)

    review_submit_parser = commands.add_parser(
        "review-submit",
        help="validate and submit one Reviewer result",
        description=(
            "Run from the detached review worktree, validate the exact "
            "request, revision, bundle, and review artifacts, persist the "
            "Reviewer-local result marker, then notify the Implementer."
        ),
    )
    review_submit_parser.set_defaults(handler=_run_review_submit)

    apply_review_parser = commands.add_parser(
        "apply-review",
        help="validate and apply the active Reviewer result",
        description=(
            "Independently revalidate the active marker-confirmed result, "
            "archive its complete evidence, and apply an exact-revision "
            "approval idempotently."
        ),
    )
    apply_review_parser.add_argument(
        "--result-id",
        metavar="UUID",
        help=(
            "expected Reviewer result ID; defaults to the active "
            "Reviewer-local marker"
        ),
    )
    apply_review_parser.set_defaults(handler=_run_apply_review)

    complete_parser = commands.add_parser(
        "complete",
        help="complete the exact approved revision",
        description=(
            "Verify exact approved-head equality and worktree cleanliness, "
            "then complete the run, release its active slot, and clean only "
            "owned review resources."
        ),
    )
    complete_parser.set_defaults(handler=_run_complete)
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
    active_round = run.active_round
    if active_round is None:
        print("Round status: none")
        print("Submission mode: none")
        print("Request ID: none")
        print("Reviewer session: none")
        review_worktree_text = "none"
    else:
        print(f"Round status: {active_round.status.value}")
        print(f"Submission mode: {active_round.mode.value}")
        print(f"Request ID: {active_round.request_id}")
        print(f"Reviewer session: {active_round.reviewer_name}")
        review_worktree_text = str(active_round.review_worktree)
    print(f"Current requested head: {run.current_head_oid or 'none'}")
    print(f"Approved head: {run.approved_head_oid or 'none'}")
    print(f"Active escalation: {run.active_escalation_id or 'none'}")
    handoff = run.handoff
    if handoff is None:
        print("Request handoff: none")
    else:
        print(f"Request handoff: {handoff.status.value}")
        print(f"Request handoff target: {handoff.target}")
        if handoff.last_error is not None:
            print(f"Request handoff error: {handoff.last_error}")
    print(f"Review worktree: {review_worktree_text}")
    if run.review_worktree_available is not None:
        availability = "yes" if run.review_worktree_available else "no"
        print(f"Review worktree available: {availability}")
    ready = run.unapplied_result
    if run.phase.value == "reviewing":
        if ready is None:
            print("Marker-confirmed unapplied result: none")
        else:
            print("Marker-confirmed unapplied result: ready")
            print(f"Result ID: {ready.result_id}")
            print(f"Result verdict: {ready.verdict.value}")
            print(f"Result path: {ready.result_path}")
            print("Result authoritative: no")
            print(
                "Apply command: agent-squad apply-review --result-id "
                f"{ready.result_id}"
            )
    budget = run.review_budget
    print(
        "Review budget: "
        f"{budget.completed_change_reviews}/{budget.effective_limit} "
        "completed change reviews "
        f"(original {budget.original_limit}, "
        f"additional {budget.additional_rounds_granted})"
    )
    print(f"Next action: {status.next_action}")
    return 0


def _run_submit(arguments: argparse.Namespace) -> int:
    result = submit_candidate(
        _invocation_directory(),
        report_path=arguments.report,
        mode=SubmissionMode(arguments.mode),
    )
    for warning in result.warnings:
        print(f"agent-squad: warning: {warning}", file=sys.stderr)
    print(
        f"Prepared Agent Squad review round {result.round_number} "
        f"for run {result.run_id}"
    )
    print(f"Request ID: {result.request_id}")
    print(f"Revision: {result.base_oid}..{result.head_oid}")
    print(f"Review worktree: {result.review_worktree}")
    print(f"Reviewer: {result.reviewer_name}")
    print(f"Request handoff: {result.handoff_status.value}")
    if result.handoff_status is HandoffStatus.FAILED:
        print(
            "The durable review round was preserved for recovery.",
            file=sys.stderr,
        )
        print(
            "agent-squad: error: review request handoff failed: "
            f"{result.handoff_error}",
            file=sys.stderr,
        )
        return 1
    print("Next action: wait for the Reviewer result")
    return 0


def _run_review_submit(_arguments: argparse.Namespace) -> int:
    result = submit_review_result(_invocation_directory())
    marker_action = "created" if result.marker_created else "reused"
    print(
        f"Marker-confirmed review result {result.result_id} for run "
        f"{result.run_id}"
    )
    print(f"Round: {result.round_number}")
    print(f"Revision: {result.head_oid}")
    print(f"Verdict: {result.verdict.value}")
    print(f"Reviewer-local marker {marker_action}: {result.marker_path}")
    if not result.notification_sent:
        print("Result notification: failed")
        print(
            "The marker-confirmed result remains valid and can be "
            "rediscovered or sent again.",
            file=sys.stderr,
        )
        print(
            "agent-squad: error: review result notification failed: "
            f"{result.notification_error}",
            file=sys.stderr,
        )
        return 1
    print("Result notification: sent")
    print("Next action: wait for the Implementer to apply the result")
    return 0


def _run_apply_review(arguments: argparse.Namespace) -> int:
    result = apply_review(
        _invocation_directory(),
        result_id=arguments.result_id,
    )
    action = "already applied" if result.replayed else "applied"
    print(
        f"Review result {result.result_id} {action} for run "
        f"{result.run_id}"
    )
    print(f"Round: {result.round_number}")
    print(f"Verdict: {result.verdict.value}")
    print(f"Approved head: {result.head_oid}")
    print(f"Approval authority: {result.approval_path}")
    print(f"Archived review bundle: {result.bundle_archive}")
    print(f"Next action: {result.next_action}")
    return 0


def _run_complete(_arguments: argparse.Namespace) -> int:
    result = complete_run(_invocation_directory())
    if result.already_completed:
        print(f"Agent Squad run {result.run_id} is already completed")
    else:
        print(f"Completed Agent Squad run {result.run_id}")
    print(f"Approved revision: {result.head_oid}")
    print("Active run slot: released")
    for warning in result.cleanup_warnings:
        print(f"agent-squad: warning: {warning}", file=sys.stderr)
    return 0


def _invocation_directory() -> Path:
    try:
        return Path.cwd()
    except OSError as error:
        raise AgentSquadError(
            "cannot determine the current directory; it may have been "
            f"removed or become unreadable: {error}"
        ) from error


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Agent Squad command-line interface."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except AgentSquadError as error:
        print(f"agent-squad: error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(
            f"agent-squad: error: cannot access local state: {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
