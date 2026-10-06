"""Forge commands using validated PR conventions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re

from .anchors import Anchor, commentable_lines, validate_anchor
from .conventions import (
    MERGE_INSTRUCTION,
    MERGE_WITHDRAWAL,
    PREFIXES,
    allocate_id,
    derive,
    first_line,
    headings,
    newer,
    parse_line,
    render_line,
    replace_section,
    section,
    task_from_issue,
    validate_merge_directive,
    validate_pr_body,
    validate_section,
)
from .forge import (
    ForgeError,
    Forge,
    ReviewComment,
    ReviewPublication,
    Snapshot,
    array,
    positive,
    requested_state,
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
    # The forge's PR base SHA can lag behind the branch. Ancestry and the
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
        client = herdr_client or HerdrClient(
            repository.primary, repository=repository
        )
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


def workflow_paths(
    repository: Repository, *, pr: int | None = None, issue: int | None = None
) -> dict:
    config = repository.configuration
    scratch = repository.resolve_root(config.scratch_root)
    return {
        "primary": str(repository.primary),
        "worktree_root": str(repository.resolve_root(config.worktree_root)),
        "scratch_root": str(scratch),
        "scratch": str(scratch / f"pr{pr}") if pr else None,
        "issue_scratch": str(scratch / f"issue-{issue}") if issue else None,
        "base_branch": config.base_branch,
    }


def find_finding(state: dict, fid: str) -> dict:
    for finding in state["findings"]:
        if finding["finding"] == fid:
            return finding
    raise AgentSquadError(f"unknown finding: {fid}")


def create_pr(
    repository: Repository,
    forge: Forge,
    issue: int,
    task: str | None,
    report: str,
    title: str | None,
) -> dict:
    issue_record = forge.issue(issue)
    task = validate_section(
        task if task is not None else task_from_issue(issue_record), "Task"
    )
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
    from .merging import implementation_identity, record_implementation

    metadata, identity = implementation_identity(repository, issue, branch)
    created = forge.create_pr(
        title or issue_record["title"], branch,
        repository.configuration.base_branch, body,
    )
    record_implementation(metadata, identity, created.number)
    return asdict(created)


def report_pr(forge: Forge, number: int, report: str) -> dict:
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


REVIEW_SECTIONS = (
    "Summary",
    "Verified dispositions",
    "Findings",
    "Merge hold",
    "Standards",
    "Spec",
    "Evidence",
)


def compose_review(
    sections: dict[str, str], inputs: list[FindingInput], ids: list[str]
) -> str:
    """Write the §7.3 headings in order around the supplied section texts."""
    texts = {}
    for name, text in sections.items():
        option = "--" + name.lower().replace(" ", "-")
        text = text.strip()
        if not text:
            raise AgentSquadError(f"{option} file must not be empty")
        # A heading or an open fence would hide the sections that follow.
        if [h[0] for h in headings(f"{text}\n\n## End\n")] != ["End"]:
            raise AgentSquadError(
                f"{option} file must hold section text without a level-2"
                " heading or an unclosed code fence"
            )
        texts[name] = text
    texts["Findings"] = (
        "\n".join(
            f"{fid} [{f.severity}] {f.title}" for f, fid in zip(inputs, ids)
        )
        or "none"
    )
    return "\n\n".join(
        f"## {name}\n\n{texts.get(name, 'none')}"
        for name in REVIEW_SECTIONS
        if name in texts or name != "Merge hold"
    )


def post_review(
    repository: Repository,
    forge: Forge,
    number: int,
    head: str,
    base: str,
    verdict: str,
    sections: dict[str, str],
    threads: object,
    resume: int | None = None,
    discard_draft: int | None = None,
) -> dict:
    header = render_line(
        "review", pr=number, head=head, base=base, verdict=verdict
    )
    inputs = load_threads(threads)
    lines = commentable_lines(repository.root, base, head)
    # All anchors are checked before even token resolution or a forge read.
    for item in inputs:
        validate_anchor(item.anchor, lines)
    seen = set()
    for item in inputs:
        key = (item.anchor.path, item.anchor.line)
        if key in seen:
            raise AgentSquadError("duplicate finding anchor")
        seen.add(key)
    forge.validate_anchors(tuple(item.anchor for item in inputs))
    compose_review(
        sections, inputs, [f"REV-{i + 1}" for i in range(len(inputs))]
    )
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
    complete_body = header + "\n\n" + compose_review(sections, inputs, ids)
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
        forge.prepare_review(
            number, reviews=snapshot.reviews, discard_draft=discard_draft,
        )
        comments = tuple(
            ReviewComment(item.anchor, item.root(fid))
            for item, fid in zip(inputs, ids)
        )
        publication = ReviewPublication(
            head,
            requested_state(verdict),
            complete_body,
            complete_body + "\n\n## Unanchored findings\n\n"
            + "\n\n".join(comment.body for comment in comments),
            comments, base,
        )
        review = forge.post_review(number, publication)
        review_id = review.evidence.id
    else:
        forge.prepare_review(
            number, reviews=snapshot.reviews, discard_draft=discard_draft,
        )
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
                number, head, review_id, item.anchor, root_body,
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


def compose_reply(
    body: str,
    *,
    disposition: str | None = None,
    sha: str | None = None,
    not_pursued: bool = False,
    deferred_to: int | None = None,
    verification: str | None = None,
) -> str:
    """Write the tagged first line and reason prefix from the options."""
    prose = body.strip()
    # Emphasis does not hide a hand-written tagged line from this check.
    start = first_line(prose).lstrip(" \t*_`")
    if start.startswith(PREFIXES):
        option = (
            "--disposition" if start.startswith("DISPOSITION")
            else "--verification"
            if start.startswith(("VERIFIED", "NOT FIXED"))
            else "--disposition or --verification"
        )
        raise AgentSquadError(
            f"reply body must start with prose; supply the tagged line with"
            f" {option}"
        )
    if start.startswith(("Not pursued:", "Deferred to #")):
        raise AgentSquadError(
            "reply body must start with prose; supply the reason prefix with"
            " --not-pursued or --deferred-to <issue>"
        )
    if not_pursued:
        if not prose:
            raise AgentSquadError("--not-pursued requires a reason in --body")
        prose = "Not pursued: " + prose
    elif deferred_to is not None:
        prose = f"Deferred to #{deferred_to}:" + (" " + prose if prose else "")
    if disposition is not None:
        line = render_line("disposition", disposition=disposition, sha=sha)
    elif verification is not None:
        line = render_line("verification", verification=verification)
    else:
        return prose
    return line + ("\n\n" + prose if prose else "")


def reply_thread(
    repository: Repository,
    forge: Forge,
    number: int,
    fid: str,
    body: str,
    *,
    disposition: str | None = None,
    sha: str | None = None,
    not_pursued: bool = False,
    deferred_to: int | None = None,
    verification: str | None = None,
) -> dict:
    body = compose_reply(
        body,
        disposition=disposition,
        sha=sha,
        not_pursued=not_pursued,
        deferred_to=deferred_to,
        verification=verification,
    )
    state = state_for(repository, forge.snapshot(number))
    finding = find_finding(state, fid)
    if finding["root"] is None:
        raise GateError("finding has no root; use thread open first")
    if forge.role == "implementer" and finding["severity"] == "optional":
        if disposition is None:
            raise AgentSquadError("optional thread requires --disposition")
        if deferred_to is not None:
            record = forge.issue(deferred_to)
            if record["is_pull_request"] or record["state"] != "open":
                raise AgentSquadError(
                    f"--deferred-to {deferred_to} requires an open issue"
                )
        elif disposition == "rejected" and not not_pursued:
            raise AgentSquadError(
                "optional rejection requires --not-pursued with a reason or"
                " --deferred-to <issue>"
            )
    elif not_pursued or deferred_to is not None:
        raise AgentSquadError(
            "--not-pursued and --deferred-to apply only to an optional thread"
        )
    if sha is not None:
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
    forge: Forge,
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
            number, finding["opening_head"], finding["opening_review"],
            anchor, finding["unanchored_body"],
        )
    )


def resolve_thread(
    repository: Repository, forge: Forge, number: int, fid: str
) -> dict:
    if not forge.can_resolve_threads:
        raise ForgeError("thread resolve is not supported on this forge")
    state = state_for(repository, forge.snapshot(number))
    finding = find_finding(state, fid)
    if finding["severity"] == "blocking" and not finding["settled"]:
        raise GateError("blocking finding is not settled")
    if finding["node_id"] is None:
        raise AgentSquadError("finding has no forge thread node ID")
    return forge.resolve(finding["node_id"])


def post_decision(
    repository: Repository,
    forge: Forge,
    number: int,
    finding: str,
    body: str,
    budget: int | None = None,
    task: str | None = None,
    merge_instruction: str | None = None,
) -> dict:
    header = render_line("decision", finding=finding, budget=budget)
    body = body.strip()
    if first_line(body).lstrip(" \t*_`").startswith(
        "Standing merge instruction"
    ):
        raise AgentSquadError(
            "decision body must not open with a standing merge sentence; use"
            " --merge-instruction record or withdraw"
        )
    if merge_instruction is not None:
        sentence = {"record": MERGE_INSTRUCTION, "withdraw": MERGE_WITHDRAWAL}
        body = sentence[merge_instruction] + ("\n\n" + body if body else "")
    if not body or parse_line(first_line(body)) is not None:
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
    validate_merge_directive(body, finding, budget, task)
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
        full_body = header + "\n\n" + body
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
    forge: Forge, number: int, head: str, reason: str, body: str
) -> dict:
    header = render_line("stop", head=head, reason=reason)
    if not body.strip() or parse_line(first_line(body)) is not None:
        raise AgentSquadError(
            "stop body must summarize the remaining problems without a"
            " protocol header"
        )
    return asdict(forge.comment(number, header + "\n\n" + body.strip()))
