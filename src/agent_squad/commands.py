"""Forge commands using validated PR conventions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re

from .anchors import Anchor, commentable_lines, validate_anchor
from .conventions import (
    EVENTS,
    PATTERNS,
    allocate_id,
    derive,
    first_line,
    headings,
    newer,
    parse_line,
    render_line,
    replace_section,
    review_findings,
    section,
    validate_pr_body,
    validate_review_body,
    validate_section,
)
from .forge import (
    ForgeError,
    GitHub,
    Snapshot,
    array,
    positive,
)
from .initialization import (
    AgentSquadError,
    GateError,
    Repository,
    RetainedError,
    git_output,
    list_worktrees,
    run_git,
)
from .validation import JsonValidator
from .herdr import HerdrClient, HerdrError, reviewer_name

V = JsonValidator(AgentSquadError)


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def is_ancestor(root: Path, earlier: str, later: str) -> bool:
    return (
        run_git(root, "merge-base", "--is-ancestor", earlier, later).returncode
        == 0
    )


def state_for(
    repository: Repository,
    snapshot: Snapshot,
    *,
    herdr_client: HerdrClient | None = None,
    include_reviewer: bool = False,
) -> dict:
    pr = snapshot.pr
    # Fetch the exact branches without updating any checked-out branch.
    git_output(
        repository.root,
        "fetch",
        "origin",
        f"+refs/heads/{pr.base_branch}:refs/remotes/origin/{pr.base_branch}",
    )
    if run_git(
        repository.root, "cat-file", "-e", f"{pr.head}^{{commit}}"
    ).returncode:
        git_output(repository.root, "fetch", "origin", pr.head)
    # Replaced heads still count toward the budget, even in a fresh clone.
    for review in snapshot.reviews:
        try:
            header = parse_line(first_line(review.evidence.body))
        except AgentSquadError:
            continue
        if (
            header is None
            or header.kind != "review"
            or review.evidence.author.casefold()
            != repository.configuration.reviewer.forge_account.casefold()
        ):
            continue
        for revision in (header.fields["head"], header.fields["base"]):
            if run_git(
                repository.root, "cat-file", "-e", f"{revision}^{{commit}}"
            ).returncode:
                git_output(repository.root, "fetch", "origin", revision)
    tip = git_output(
        repository.root,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{pr.base_branch}^{{commit}}",
    )
    # GitHub's PR base SHA can lag behind the branch. Ancestry and the
    # review target use the fetched tip, not that informational snapshot.
    base = git_output(repository.root, "merge-base", tip, pr.head)
    worktrees = list_worktrees(repository.root)
    implementation = next(
        (w for w in worktrees if w.branch == f"refs/heads/{pr.head_branch}"),
        None,
    )
    dirty = bool(
        implementation
        and git_output(
            implementation.root,
            "status",
            "--porcelain",
            "--untracked-files=no",
        )
    )
    live = False
    if include_reviewer:
        client = herdr_client or HerdrClient(repository.primary)
        try:
            live = (
                client.get_agent(reviewer_name(pr.number, pr.head)) is not None
            )
        except HerdrError:
            # Scheduling hints never make forge-derived status unavailable.
            pass
    state = derive(
        snapshot,
        repository.configuration,
        worktrees,
        base,
        lambda a, b: is_ancestor(repository.root, a, b),
        base_tip=tip,
        dirty=dirty,
        reviewer_live=live,
    )
    return {**state, "paths": workflow_paths(repository, pr=pr.number)}


def workflow_paths(repository: Repository, *, pr: int | None = None) -> dict:
    config = repository.configuration
    scratch = repository.resolve_root(config.scratch_root)
    return {
        "primary": str(repository.primary),
        "worktree_root": str(repository.resolve_root(config.worktree_root)),
        "scratch_root": str(scratch),
        "scratch": str(scratch / f"pr{pr}") if pr else None,
        "base_branch": config.base_branch,
    }


def find_finding(state: dict, fid: str) -> dict:
    for finding in state["findings"]:
        if finding["finding"] == fid:
            return finding
    raise AgentSquadError(f"unknown finding: {fid}")


def create_pr(
    repository: Repository,
    forge: GitHub,
    issue: int,
    task: str,
    report: str,
    title: str | None,
) -> dict:
    task = validate_section(task, "Task")
    report = validate_section(report, "Implementation report")
    body = f"{task}\n\n{report}\n\nCloses #{issue}\n"
    validate_pr_body(body)
    branch = git_output(
        repository.root, "symbolic-ref", "--quiet", "--short", "HEAD"
    )
    head = git_output(repository.root, "rev-parse", "HEAD")
    pushed = git_output(
        repository.root,
        "ls-remote",
        "--heads",
        "origin",
        f"refs/heads/{branch}",
    )
    if not pushed or pushed.split()[0] != head:
        raise GateError("branch HEAD has not been pushed to origin")
    if forge.branch_prs(branch):
        raise AgentSquadError("a PR already exists for this branch")
    issue_record = forge.issue(issue)
    from .merging import implementation_identity, record_implementation

    metadata, identity = implementation_identity(repository, issue, branch)
    created = forge.create_pr(
        {
            "title": title or issue_record["title"],
            "head": branch,
            "base": repository.configuration.base_branch,
            "body": body,
        }
    )
    record_implementation(metadata, identity, created.number)
    return asdict(created)


def report_pr(forge: GitHub, number: int, report: str) -> dict:
    validate_section(report, "Implementation report")
    pr = forge.pr(number)
    if pr.state != "open":
        raise AgentSquadError("PR is not open")
    return asdict(
        forge.update_body(
            number,
            replace_section(pr.evidence.body, "Implementation report", report),
        )
    )


@dataclass(frozen=True)
class FindingInput:
    severity: str
    category: str
    title: str
    anchor: Anchor
    body: str

    def root(self, fid: str) -> str:
        return (
            render_line(
                "finding",
                finding=fid,
                severity=self.severity,
                category=self.category,
                title=self.title,
            )
            + "\n\n"
            + self.body.strip()
        )


def load_threads(value: object) -> list[FindingInput]:
    result = []
    for item in array(value, "threads"):
        item = V.require_object(item, "thread")
        V.check_fields(
            item,
            required={"severity", "category", "title", "path", "line", "body"},
            optional={"start_line"},
            path="thread",
        )
        for key in ("severity", "category", "title", "path", "body"):
            V.require_string(item[key], f"thread.{key}")
        render_line(
            "finding",
            finding="REV-1",
            **{k: item[k] for k in ("severity", "category", "title")},
        )
        for label in (
            "Problem",
            "Evidence",
            "Impact",
            "Required change",
            "Verification",
        ):
            if (
                re.search(
                    rf"^\*\*{label}\*\*[: ]*[^\s:]",
                    item["body"],
                    re.MULTILINE,
                )
                is None
            ):
                raise AgentSquadError(
                    f"thread body requires **{label}** with its paragraph"
                )
        anchor = Anchor(
            item["path"],
            positive(item["line"], "thread.line"),
            (
                None
                if item.get("start_line") is None
                else positive(item["start_line"], "thread.start_line")
            ),
        )
        result.append(
            FindingInput(
                item["severity"],
                item["category"],
                item["title"],
                anchor,
                item["body"],
            )
        )
    return result


def compose_review(
    body: str, inputs: list[FindingInput], ids: list[str]
) -> str:
    if not body.startswith("## Summary\n"):
        raise AgentSquadError("review body file must start at ## Summary")
    validate_review_body(body)
    if section(body, "Unanchored findings") is not None:
        raise AgentSquadError(
            "Unanchored findings is generated by fallback, not supplied"
        )
    found = headings(body)
    i = next(i for i, h in enumerate(found) if h[0] == "Findings")
    end = found[i + 1][1] if i + 1 < len(found) else len(body)
    listing = (
        "\n".join(
            f"{fid} [{f.severity}] {f.title}" for f, fid in zip(inputs, ids)
        )
        or "none"
    )
    result = (
        body[: found[i][1]] + "## Findings\n\n" + listing + "\n\n" + body[end:]
    )
    validate_review_body(result)
    return result.strip()


def post_review(
    repository: Repository,
    forge: GitHub,
    number: int,
    head: str,
    base: str,
    verdict: str,
    body: str,
    threads: object,
    resume: int | None = None,
) -> dict:
    header = render_line(
        "review", pr=number, head=head, base=base, verdict=verdict
    )
    inputs = load_threads(threads)
    lines = commentable_lines(repository.root, base, head)
    # All anchors are checked before even token resolution or a forge read.
    for item in inputs:
        validate_anchor(item.anchor, lines)
    compose_review(body, inputs, [f"REV-{i + 1}" for i in range(len(inputs))])
    snapshot = forge.snapshot(number)
    state = state_for(repository, snapshot)
    if snapshot.pr.head != head:
        raise AgentSquadError("review head is not the current PR head")
    if not is_ancestor(repository.root, base, head) or not is_ancestor(
        repository.root, base, state["target"]["base_tip"]
    ):
        raise AgentSquadError(
            "review base must be an ancestor of head and the base branch"
        )
    existing = None
    if resume is not None:
        existing = next(
            (r for r in state["reviews"] if r["id"] == resume), None
        )
        if existing is None or first_line(existing["body"]) != header:
            raise AgentSquadError(
                "--resume must identify a tagged Reviewer review with the same"
                " header"
            )
        listed = existing["findings"]
        if len(listed) != len(inputs) or any(
            f["severity"] != i.severity or f["title"] != i.title
            for f, i in zip(listed, inputs)
        ):
            raise AgentSquadError(
                "--resume requires the same ordered Findings list"
            )
        ids = [f["finding"] for f in listed]
    else:
        start = allocate_id(snapshot)
        ids = [f"REV-{start + i}" for i in range(len(inputs))]
    complete_body = header + "\n\n" + compose_review(body, inputs, ids)
    if existing is None:
        if verdict == "approved" and (
            any(f.severity == "blocking" for f in inputs)
            or any(
                f["severity"] == "blocking" and not f["settled"]
                for f in state["findings"]
            )
        ):
            raise GateError(
                "approval requires all earlier blocking findings settled and"
                " no new blocking finding"
            )
        if verdict == "changes_requested" and not any(
            f.severity == "blocking" for f in inputs
        ):
            latest = state["reviews"][-1] if state["reviews"] else None
            if not any(
                f["latest_verification"]
                and f["latest_verification"]["value"] == "NOT FIXED"
                and newer(f["latest_verification"], latest)
                for f in state["findings"]
            ):
                raise AgentSquadError(
                    "changes_requested requires a blocking finding or NOT"
                    " FIXED during this pass"
                )
        # Preserve any stranded draft and its replies before the new review.
        for review in snapshot.reviews:
            if (
                review.state == "PENDING"
                and review.evidence.author.casefold()
                == forge.account.casefold()
            ):
                forge.submit_pending(number, review.evidence.id)
        comments = [
            {**f.anchor.payload(), "body": f.root(fid)}
            for f, fid in zip(inputs, ids)
        ]
        payload = {
            "commit_id": head,
            "event": EVENTS[verdict],
            "body": complete_body,
            "comments": comments,
        }
        try:
            review = forge.post_review(number, payload)
        except ForgeError as error:
            # Only a definite batch rejection permits the specified alternative
            # write.
            if error.status != 422 or not comments:
                raise
            lines = commentable_lines(repository.root, base, head)
            for item in inputs:
                validate_anchor(item.anchor, lines)
            payload["comments"] = []
            payload["body"] = (
                complete_body
                + "\n\n## Unanchored findings\n\n"
                + "\n\n".join(f.root(fid) for f, fid in zip(inputs, ids))
            )
            review = forge.post_review(number, payload)
        review_id = review.evidence.id
    else:
        review_id = existing["id"]
    # Read after publication: partial success is recoverable by this exact
    # review ID.
    current = state_for(repository, forge.snapshot(number))
    failed = []
    for fid, item in zip(ids, inputs):
        finding = find_finding(current, fid)
        if finding["root"] is not None:
            continue
        try:
            # For fallback/resumption the already published text is
            # authoritative.
            root_body = finding["unanchored_body"] or item.root(fid)
            forge.post_root(
                number,
                {
                    "commit_id": head,
                    **item.anchor.payload(),
                    "body": root_body,
                },
            )
        except ForgeError as error:
            failed.append(f"{fid}: {error}")
    if failed:
        raise ForgeError(
            f"review {review_id} published; missing roots:"
            f' {"; ".join(failed)}; use review post --resume {review_id} or'
            " thread open"
        )
    final = state_for(repository, forge.snapshot(number))
    missing = [fid for fid in ids if find_finding(final, fid)["unanchored"]]
    if missing:
        raise ForgeError(
            f"review {review_id} published; forge omitted roots:"
            f' {", ".join(missing)}; use --resume {review_id}'
        )
    return {
        "review_id": review_id,
        "head": head,
        "verdict": verdict,
        "findings": ids,
        "resumed": resume is not None,
    }


def reply_thread(
    repository: Repository, forge: GitHub, number: int, fid: str, body: str
) -> dict:
    parsed = parse_line(first_line(body))
    allowed = {"implementer": "disposition", "reviewer": "verification"}
    if parsed is not None and parsed.kind != allowed[forge.role]:
        raise AgentSquadError(
            "reply tagged line is not valid for the acting role"
        )
    state = state_for(repository, forge.snapshot(number))
    finding = find_finding(state, fid)
    if finding["root"] is None:
        raise GateError("finding has no root; use thread open first")
    if forge.role == "implementer" and finding["severity"] == "optional":
        if parsed is None:
            raise AgentSquadError("optional thread requires a DISPOSITION")
        if "rejected" in parsed.fields:
            lines = [line for line in body.splitlines() if line.strip()]
            reason = lines[1] if len(lines) > 1 else ""
            deferred = re.match(r"Deferred to #([1-9][0-9]*):", reason)
            if reason.startswith("Not pursued:") and reason[12:].strip():
                pass
            elif deferred:
                issue = int(deferred[1])
                if forge.issue(issue)["state"] != "open":
                    raise AgentSquadError(
                        f"Deferred to #{issue} requires an open issue"
                    )
            else:
                raise AgentSquadError(
                    "optional rejection requires a second non-empty line"
                    " starting with 'Not pursued: <reason>' or"
                    " 'Deferred to #<issue>:'"
                )
    if parsed and "sha" in parsed.fields:
        sha = parsed.fields["sha"]
        if run_git(
            repository.root, "cat-file", "-e", f"{sha}^{{commit}}"
        ).returncode or is_ancestor(
            repository.root, sha, finding["opening_head"]
        ):
            raise AgentSquadError(
                "fixed SHA must name a local commit absent from the finding"
                " opening head"
            )
    return asdict(forge.reply(number, finding["root"]["id"], body))


def open_thread(
    repository: Repository,
    forge: GitHub,
    number: int,
    fid: str,
    anchor: Anchor,
) -> dict:
    state = state_for(repository, forge.snapshot(number))
    finding = find_finding(state, fid)
    if finding["root"] is not None:
        raise AgentSquadError("finding already has a root")
    if not finding["unanchored_body"]:
        raise AgentSquadError(
            "review has no Unanchored findings text to recover"
        )
    validate_anchor(
        anchor,
        commentable_lines(
            repository.root, finding["opening_base"], finding["opening_head"]
        ),
    )
    return asdict(
        forge.post_root(
            number,
            {
                "commit_id": finding["opening_head"],
                **anchor.payload(),
                "body": finding["unanchored_body"],
            },
        )
    )


def resolve_thread(
    repository: Repository, forge: GitHub, number: int, fid: str
) -> dict:
    state = state_for(repository, forge.snapshot(number))
    finding = find_finding(state, fid)
    if finding["severity"] == "blocking" and not finding["settled"]:
        raise GateError("blocking finding is not settled")
    if finding["node_id"] is None:
        raise AgentSquadError("finding has no forge thread node ID")
    return forge.resolve(finding["node_id"])


def post_decision(
    repository: Repository,
    forge: GitHub,
    number: int,
    finding: str,
    body: str,
    budget: int | None = None,
    task: str | None = None,
) -> dict:
    header = render_line("decision", finding=finding, budget=budget)
    if not body.strip() or parse_line(first_line(body)) is not None:
        raise AgentSquadError(
            "decision body must contain decision prose without a protocol"
            " header"
        )
    if task is not None:
        task = validate_section(task, "Task")
        if finding != "none":
            raise AgentSquadError("Task amendment requires --finding none")
    if section(body, "Task") is not None:
        raise AgentSquadError("supply a Task amendment through --task")
    snapshot = forge.snapshot(number)
    state = state_for(repository, snapshot)
    if finding != "none":
        find_finding(state, finding)
    latest = state["decisions"][-1] if state["decisions"] else None
    # A repeated amendment repairs its mirror without a second decision or
    # budget effect.
    if task is not None and latest and latest.get("task") == task:
        decision = latest
        reused = True
    else:
        if budget is not None and budget <= state["budget"]["used"]:
            raise GateError(
                "decision budget must exceed the used review count"
            )
        full_body = header + "\n\n" + body.strip()
        if task is not None:
            full_body += "\n\n" + task
        decision = asdict(forge.comment(number, full_body))
        reused = False
    if task is not None:
        try:
            # Re-read before mirroring so a newer report is retained.
            pr = forge.pr(number)
            forge.update_body(
                number, replace_section(pr.evidence.body, "Task", task + "\n")
            )
        except AgentSquadError as error:
            raise RetainedError(
                f'decision {decision["id"]} is published; Task mirror failed:'
                f" {error}; rerun with the same --task"
            ) from None
    return {
        "decision": decision,
        "reused": reused,
        "task_mirrored": task is not None,
    }


def post_stop(
    forge: GitHub, number: int, head: str, reason: str, body: str
) -> dict:
    header = render_line("stop", head=head, reason=reason)
    if not body.strip() or parse_line(first_line(body)) is not None:
        raise AgentSquadError(
            "stop body must summarize the remaining problems without a"
            " protocol header"
        )
    return asdict(forge.comment(number, header + "\n\n" + body.strip()))
