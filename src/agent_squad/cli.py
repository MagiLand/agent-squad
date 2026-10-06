"""The PR-authoritative forge and Reviewer lifecycle commands."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import re
import sys

from . import __version__
from .anchors import Anchor
from . import commands
from . import reviewer
from . import skills
from .conventions import DISPOSITIONS, SHA, VERIFICATIONS
from .doctor import diagnose
from .forge import make_forge
from .herdr import HerdrClient
from .initialization import (
    AgentSquadError,
    GateError,
    RetainedError,
    decode_json,
    initialize_repository,
    load_initialized_repository,
)


def positive_argument(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError(
            "must be a positive decimal integer without leading zeros"
        )
    return int(value)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="agent-squad")
    root.add_argument("--version", action="version", version=__version__)
    groups = root.add_subparsers(dest="group", required=True)
    init = groups.add_parser("init")
    init.add_argument("--implementer-account", required=True)
    init.add_argument("--reviewer-account", required=True)
    init.add_argument(
        "--forge", choices=("github", "forgejo"), default="github",
    )
    for name in ("base-url", "implementer-token-file", "reviewer-token-file"):
        init.add_argument("--" + name)
    for name in ("owner", "repo", "base-branch"):
        init.add_argument("--" + name)
    init.add_argument("--json", action="store_true")
    doctor = groups.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--live-reviewer", action="store_true")

    def command(
        group: str, names: tuple[str, ...]
    ) -> dict[str, argparse.ArgumentParser]:
        parent = groups.add_parser(group)
        sub = parent.add_subparsers(dest="command", required=True)
        return {name: sub.add_parser(name) for name in names}

    def common(
        p: argparse.ArgumentParser,
        *,
        role: str | None = None,
        number: str = "pr",
        mutation: bool = False,
    ) -> None:
        p.set_defaults(forge_mutation=mutation)
        p.add_argument(
            "--as",
            dest="role",
            choices=[role] if role else ["implementer", "reviewer"],
            required=mutation,
        )
        p.add_argument("--" + number, type=positive_argument, required=True)
        p.add_argument("--json", action="store_true")

    issue = command("issue", ("view", "comment"))
    common(issue["view"], number="issue")
    common(
        issue["comment"], role="implementer", number="issue", mutation=True
    )
    issue["comment"].add_argument("--body", required=True)
    skill = command("skill", ("install",))["install"]
    for name in ("claude", "codex", "force", "json"):
        skill.add_argument("--" + name, action="store_true")
    pr = command("pr", ("head", "reviews", "create", "report", "merge"))
    for name in ("head", "reviews"):
        common(pr[name])
    common(pr["create"], role="implementer", number="issue", mutation=True)
    pr["create"].add_argument("--task")
    pr["create"].add_argument("--report", required=True)
    pr["create"].add_argument("--title")
    common(pr["report"], role="implementer", mutation=True)
    pr["report"].add_argument("--report", required=True)
    common(pr["merge"], role="implementer", mutation=True)
    pr["merge"].add_argument("--accept-moved-base", action="store_true")
    pr["merge"].add_argument("--accept-merge-hold", action="store_true")
    review = command("review", ("post",))["post"]
    common(review, role="reviewer", mutation=True)
    for name in (
        "head", "base", "verdict", "summary", "verified-dispositions",
        "threads",
    ):
        review.add_argument("--" + name, required=True)
    for name in ("merge-hold", "standards", "spec", "evidence"):
        review.add_argument("--" + name)
    review.add_argument("--resume", type=positive_argument)
    review.add_argument("--discard-draft", type=positive_argument)
    threads = command("thread", ("reply", "open", "resolve"))
    for name, p in threads.items():
        common(
            p, role="reviewer" if name == "resolve" else None, mutation=True
        )
        p.add_argument("--finding", required=True)
    threads["reply"].add_argument("--body", required=True)
    tag = threads["reply"].add_mutually_exclusive_group()
    tag.add_argument("--disposition", choices=DISPOSITIONS)
    tag.add_argument("--verification", choices=tuple(VERIFICATIONS))
    threads["reply"].add_argument("--sha")
    reason = threads["reply"].add_mutually_exclusive_group()
    reason.add_argument("--not-pursued", action="store_true")
    reason.add_argument("--deferred-to", type=positive_argument)
    threads["open"].add_argument("--path", required=True)
    threads["open"].add_argument(
        "--line", type=positive_argument, required=True
    )
    threads["open"].add_argument("--start-line", type=positive_argument)
    decision = command("decision", ("post",))["post"]
    common(decision, role="implementer", mutation=True)
    decision.add_argument("--finding", required=True)
    decision.add_argument("--budget", type=positive_argument)
    decision.add_argument("--task")
    decision.add_argument(
        "--merge-instruction", choices=("record", "withdraw")
    )
    decision.add_argument("--body", required=True)
    stop = command("stop", ("post",))["post"]
    common(stop, mutation=True)
    for name in ("head", "reason", "body"):
        stop.add_argument("--" + name, required=True)
    status = groups.add_parser("status")
    common(status)
    worktrees = command("review-worktree", ("create", "remove"))
    for p in worktrees.values():
        common(p)
        p.add_argument("--head", required=True)
    reviewers = command("reviewer", ("launch", "adopt", "close"))
    for p in reviewers.values():
        common(p)
    reviewers["close"].add_argument("--head")
    handoffs = command("handoff", ("review-result", "stopped"))
    for p in handoffs.values():
        common(p)
        p.add_argument("--head", required=True)
    handoffs["review-result"].add_argument("--verdict", required=True)
    handoffs["stopped"].add_argument("--reason", required=True)
    return root


def execute(args: argparse.Namespace) -> dict:
    cwd = Path.cwd()
    if args.group == "skill":
        return skills.install(
            codex=args.codex, claude=args.claude, force=args.force
        )
    if args.group == "init":
        return initialize_repository(
            cwd,
            implementer_account=args.implementer_account,
            reviewer_account=args.reviewer_account,
            owner=args.owner,
            repo=args.repo,
            base_branch=args.base_branch,
            forge=args.forge,
            base_url=args.base_url,
            implementer_token_file=args.implementer_token_file,
            reviewer_token_file=args.reviewer_token_file,
        )
    # doctor reports invalid configuration as one of its prerequisite failures.
    if args.group == "doctor":
        return diagnose(cwd, live_reviewer=args.live_reviewer)
    repository = load_initialized_repository(cwd)
    role = args.role or repository.default_role()
    forge = make_forge(repository, role)
    key = (args.group, getattr(args, "command", None))
    if key == ("thread", "resolve") and not forge.can_resolve_threads:
        raise AgentSquadError("thread resolve is not supported on this forge")
    if getattr(args, "forge_mutation", False) and not forge.can_mutate:
        raise AgentSquadError(forge.mutation_unavailable_message)
    if args.group == "review-worktree":
        worktree = reviewer.ReviewWorktree.for_pr(
            repository, args.pr, args.head
        )
        return (
            worktree.create()
            if args.command == "create"
            else worktree.remove()
        )
    if key == ("reviewer", "launch"):
        return reviewer.launch(repository, forge, args.pr)
    if key == ("reviewer", "adopt"):
        return reviewer.adopt(repository, forge, args.pr)
    if key == ("reviewer", "close"):
        worktree = reviewer.ReviewWorktree.for_pr(
            repository, args.pr, args.head or forge.pr(args.pr).head
        )
        return reviewer.close_reviewer(
            worktree, HerdrClient(repository.primary, repository=repository)
        )
    if args.group == "handoff":
        return reviewer.handoff(
            repository,
            forge,
            args.pr,
            args.head,
            verdict=getattr(args, "verdict", None),
            reason=getattr(args, "reason", None),
        )
    if key == ("issue", "view"):
        return commands.view_issue(repository, forge, args.issue)
    if key == ("issue", "comment"):
        return commands.comment_issue(
            forge, args.issue, commands.read_file(args.body)
        )
    if key == ("pr", "create"):
        return commands.create_pr(
            repository,
            forge,
            args.issue,
            commands.read_file(args.task) if args.task else None,
            commands.read_file(args.report),
            args.title,
        )
    if key == ("pr", "report"):
        return commands.report_pr(
            forge, args.pr, commands.read_file(args.report)
        )
    if key == ("pr", "merge"):
        from .merging import merge_pr

        return merge_pr(
            repository, forge, args.pr,
            accept_moved_base=args.accept_moved_base,
            accept_merge_hold=args.accept_merge_hold,
        )
    if key == ("review", "post"):
        sections = {
            "Summary": args.summary,
            "Verified dispositions": args.verified_dispositions,
            "Merge hold": args.merge_hold,
            "Standards": args.standards,
            "Spec": args.spec,
            "Evidence": args.evidence,
        }
        return commands.post_review(
            repository,
            forge,
            args.pr,
            args.head,
            args.base,
            args.verdict,
            {
                name: commands.read_file(path)
                for name, path in sections.items()
                if path is not None
            },
            decode_json(commands.read_file(args.threads)),
            args.resume,
            args.discard_draft,
        )
    if key == ("thread", "reply"):
        return commands.reply_thread(
            repository,
            forge,
            args.pr,
            args.finding,
            commands.read_file(args.body),
            disposition=args.disposition,
            sha=args.sha,
            not_pursued=args.not_pursued,
            deferred_to=args.deferred_to,
            verification=args.verification,
        )
    if key == ("thread", "open"):
        return commands.open_thread(
            repository,
            forge,
            args.pr,
            args.finding,
            Anchor(args.path, args.line, args.start_line),
        )
    if key == ("thread", "resolve"):
        return commands.resolve_thread(
            repository, forge, args.pr, args.finding
        )
    if key == ("decision", "post"):
        return commands.post_decision(
            repository,
            forge,
            args.pr,
            args.finding,
            commands.read_file(args.body),
            args.budget,
            commands.read_file(args.task) if args.task else None,
            args.merge_instruction,
        )
    if key == ("stop", "post"):
        return commands.post_stop(
            forge,
            args.pr,
            args.head,
            args.reason,
            commands.read_file(args.body),
        )
    state = commands.state_for(
        repository,
        forge.snapshot(args.pr),
        include_reviewer=args.group == "status",
    )
    if key == ("pr", "head"):
        return {
            **state["target"],
            **{k: state["pr"][k] for k in ("state", "merged", "merge_commit")},
        }
    if key == ("pr", "reviews"):
        return {
            "reviews": state["reviews"],
            "diagnostics": state["diagnostics"],
        }
    return state


def reply_option_error(args: argparse.Namespace) -> str | None:
    """Refuse option combinations before any forge call."""
    if args.disposition and args.role != "implementer":
        return "--disposition requires --as implementer"
    if args.verification and args.role != "reviewer":
        return "--verification requires --as reviewer"
    if args.disposition == "fixed" and args.sha is None:
        return "--disposition fixed requires --sha <full-sha>"
    if args.disposition != "fixed" and args.sha is not None:
        return "--sha requires --disposition fixed"
    if args.sha is not None and re.fullmatch(SHA, args.sha) is None:
        return "--sha must be a full lowercase commit SHA"
    if args.disposition != "rejected" and (
        args.not_pursued or args.deferred_to is not None
    ):
        return "--not-pursued and --deferred-to require --disposition rejected"
    return None


def json_default(value: object) -> object:
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.group == "init" and args.forge == "forgejo":
        for name in (
            "base_url", "implementer_token_file", "reviewer_token_file",
        ):
            if getattr(args, name) is None:
                root.error(
                    "--forge forgejo requires --" + name.replace("_", "-"),
                )
    if (args.group, getattr(args, "command", None)) == ("thread", "reply"):
        error = reply_option_error(args)
        if error:
            root.error(error)
    try:
        result = execute(args)
        if args.json:
            print(json.dumps(result, default=json_default))
        elif args.group == "status":
            print(result["next_action"] + ": " + "; ".join(result["reasons"]))
            if result["current_review_unacted"]:
                review = result["current_review_unacted"]
                print(
                    "Current review awaiting Implementer action:"
                    f' {review["id"]} {review["verdict"]} at {review["head"]}'
                )
            print(json.dumps(result, indent=2, default=json_default))
        elif args.group == "doctor":
            for d in result["diagnostics"]:
                print(f'{d["severity"].upper()} {d["check"]}: {d["detail"]}')
        elif (args.group, getattr(args, "command", None)) == (
            "issue", "view",
        ):
            for c in result["comments"]:
                if c["agent_note"]:
                    print(
                        f'Comment {c["id"]} by {c["author"]} is an'
                        " Implementer agent note, not a Developer comment."
                    )
            print(json.dumps(result, indent=2, default=json_default))
        else:
            print(json.dumps(result, indent=2, default=json_default))
        return result.get(
            "exit_code",
            1 if args.group == "doctor" and not result["ok"] else 0,
        )
    except (AgentSquadError, OSError, ValueError) as error:
        print("error: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return (
            4
            if isinstance(error, GateError)
            else 3 if isinstance(error, RetainedError) else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())
