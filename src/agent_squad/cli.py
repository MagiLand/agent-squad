"""Command-line interface for Agent Squad."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

from . import __version__
from .artifacts import (
    HandoffStatus,
    ReviewVerdict,
    RoundStatus,
    SubmissionMode,
)
from .handoffs import HandoffRecoveryAction, retry_handoff
from .initialization import AgentKind, AgentSquadError, initialize_repository
from .review_applications import apply_review, complete_run, supersede_review
from .review_submissions import submit_review_result
from .runs import (
    IncompleteReviewOutput,
    InvalidUnappliedReviewResult,
    RETRY_HANDOFF_NEXT_ACTION,
    RunPhase,
    inspect_status,
    start_run,
)
from .submissions import submit_candidate


_RECOVERY_ACTION_LABELS = {
    HandoffRecoveryAction.ADOPTED: "adopted existing request",
    HandoffRecoveryAction.REPROMPTED: "re-prompted Reviewer",
    HandoffRecoveryAction.RELAUNCHED: "relaunched Reviewer",
    HandoffRecoveryAction.RESULT_READY: "use marker-confirmed result",
}


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
    submit_parser.add_argument(
        "--response",
        type=Path,
        metavar="PATH",
        help=(
            "versioned JSON response to the prior applied "
            "changes-requested review"
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
            "archive its complete evidence, and apply its exact-revision "
            "outcome idempotently."
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

    supersede_parser = commands.add_parser(
        "supersede",
        help="invalidate the active review and return to implementation",
        description=(
            "Permanently supersede the active reviewing round, record its "
            "actor and cause, preserve marker-confirmed late evidence, and "
            "remove only safely disposable review resources."
        ),
    )
    supersede_parser.add_argument(
        "--reason",
        required=True,
        metavar="TEXT",
        help="single-line cause explaining why the review is obsolete",
    )
    supersede_parser.set_defaults(handler=_run_supersede)

    retry_handoff_parser = commands.add_parser(
        "retry-handoff",
        help="recover the current durable review handoff",
        description=(
            "Probe the current round's local result evidence, deterministic "
            "Reviewer session, and terminal history before adopting, "
            "re-prompting, or relaunching that same logical request."
        ),
    )
    retry_handoff_parser.set_defaults(handler=_run_retry_handoff)

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
    if run.review_bundle_intact is not None:
        integrity = "yes" if run.review_bundle_intact else "no"
        print(f"Review bundle intact: {integrity}")
    if run.review_bundle_error is not None:
        print(f"Review bundle warning: {run.review_bundle_error}")
    unapplied_review = run.unapplied_review
    if run.phase is RunPhase.REVIEWING:
        if isinstance(unapplied_review, InvalidUnappliedReviewResult):
            print(
                "Marker-confirmed unapplied result: present but invalid: "
                f"{unapplied_review.reason}"
            )
            print(f"Recovery command: {RETRY_HANDOFF_NEXT_ACTION}")
        elif isinstance(unapplied_review, IncompleteReviewOutput):
            print("Marker-confirmed unapplied result: none")
            print("Unmarked review output: present and incomplete")
            print(
                "Incomplete output paths: "
                + ", ".join(
                    str(path) for path in unapplied_review.output_paths
                )
            )
            print(f"Recovery command: {RETRY_HANDOFF_NEXT_ACTION}")
        elif unapplied_review is None:
            print("Marker-confirmed unapplied result: none")
            print("Unmarked review output: none")
            print(f"Recovery command: {RETRY_HANDOFF_NEXT_ACTION}")
        else:
            print("Marker-confirmed unapplied result: ready")
            print(f"Result ID: {unapplied_review.result_id}")
            print(f"Result verdict: {unapplied_review.verdict.value}")
            print(f"Result path: {unapplied_review.result_path}")
            print("Result authoritative: no")
            print(f"Apply command: {status.next_action}")
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
        response_path=arguments.response,
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
        return _report_handoff_failure(
            preservation_notice=(
                "The durable review round was preserved for recovery."
            ),
            error_prefix="review request handoff failed",
            error=result.handoff_error,
        )
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
    if result.classification is not RoundStatus.APPLIED:
        action = "already classified" if result.replayed else "classified"
        print(
            f"Review result {result.result_id} {action} "
            f"{result.classification.value} for run {result.run_id}"
        )
        print(f"Round: {result.round_number}")
        print(f"Classification: {result.classification.value}")
        print(f"Reviewed head: {result.head_oid}")
        if result.observed_head_oid is not None:
            print(f"Observed implementation head: {result.observed_head_oid}")
        if result.bundle_archive is not None:
            print(f"Archived review bundle: {result.bundle_archive}")
        if result.diagnostic_path is not None:
            print(f"Validation diagnostics: {result.diagnostic_path}")
        if result.reason is not None:
            print(f"Reason: {result.reason}")
        print(f"Next action: {result.next_action}")
        for warning in result.cleanup_warnings:
            print(f"agent-squad: warning: {warning}", file=sys.stderr)
        return 0

    action = "already applied" if result.replayed else "applied"
    print(
        f"Review result {result.result_id} {action} for run "
        f"{result.run_id}"
    )
    print(f"Round: {result.round_number}")
    verdict = result.verdict
    if verdict is None:
        raise AgentSquadError("applied result is missing its verdict")
    print(f"Verdict: {verdict.value}")
    if verdict is ReviewVerdict.APPROVED:
        print(f"Approved head: {result.head_oid}")
        print(f"Approval authority: {result.approval_path}")
    else:
        print(f"Reviewed head: {result.head_oid}")
    if result.bundle_archive is None:
        raise AgentSquadError("applied result is missing its bundle archive")
    print(f"Archived review bundle: {result.bundle_archive}")
    print(f"Next action: {result.next_action}")
    for warning in result.cleanup_warnings:
        print(f"agent-squad: warning: {warning}", file=sys.stderr)
    return 0


def _run_supersede(arguments: argparse.Namespace) -> int:
    result = supersede_review(
        _invocation_directory(),
        reason=arguments.reason,
    )
    print(
        f"Superseded review round {result.round_number} for run "
        f"{result.run_id}"
    )
    print(f"Revision: {result.head_oid}")
    print(f"Actor: {result.actor}")
    print(f"Cause: {result.cause}")
    if result.reviewer_notice_sent:
        print("Reviewer notice: sent")
    else:
        print("Reviewer notice: not sent")
        print(
            "agent-squad: warning: Reviewer supersede notice failed: "
            f"{result.reviewer_notice_error}",
            file=sys.stderr,
        )
    if result.late_result_id is not None:
        print(f"Archived late result: {result.late_result_id}")
    for warning in result.cleanup_warnings:
        print(f"agent-squad: warning: {warning}", file=sys.stderr)
    print("Next action: continue implementing the captured task")
    return 0


def _run_retry_handoff(_arguments: argparse.Namespace) -> int:
    result = retry_handoff(_invocation_directory())
    print(
        f"Recovered review handoff for run {result.run_id}, "
        f"round {result.round_number}"
    )
    print(f"Request ID: {result.request_id}")
    print(f"Reviewer: {result.reviewer_name}")
    print(f"Request handoff: {result.handoff_status.value}")
    if result.action is not None:
        print(
            "Recovery action: "
            f"{_RECOVERY_ACTION_LABELS[result.action]}"
        )
    if result.diagnostic_id is not None:
        print(f"Preserved invalid-result diagnostics: {result.diagnostic_id}")
    if result.result_id is not None:
        next_action = (
            "agent-squad apply-review --result-id "
            f"{result.result_id}"
        )
        print(f"Result ID: {result.result_id}")
        print(f"Next action: {next_action}")
        return 0
    if result.handoff_status is HandoffStatus.FAILED:
        return _report_handoff_failure(
            preservation_notice=(
                "The durable review round remains available for recovery."
            ),
            error_prefix="review handoff recovery failed",
            error=result.handoff_error,
        )
    print("Next action: wait for the Reviewer result")
    return 0


def _report_handoff_failure(
    *,
    preservation_notice: str,
    error_prefix: str,
    error: str | None,
) -> int:
    """Report one recoverable request-handoff failure consistently."""

    print(preservation_notice, file=sys.stderr)
    print(
        f"agent-squad: error: {error_prefix}: {error}",
        file=sys.stderr,
    )
    print(f"Next action: {RETRY_HANDOFF_NEXT_ACTION}")
    return 1


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
