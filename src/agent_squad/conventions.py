"""Exact PR grammars and pure derivation of review authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Callable

from .forge import Comment, Evidence, Snapshot
from .initialization import AgentSquadError, Configuration, Worktree

TAG = "AGENT_SQUAD/0.5.0"
SHA = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
NUMBER = r"[1-9][0-9]*"
FINDING_ID = rf"REV-{NUMBER}"
VERDICTS = ("approved", "changes_requested", "needs_human")
EVENTS = {
    "approved": "APPROVE",
    "changes_requested": "REQUEST_CHANGES",
    "needs_human": "COMMENT",
}
STATES = {
    "approved": "APPROVED",
    "changes_requested": "CHANGES_REQUESTED",
    "needs_human": "COMMENTED",
}
PREFIXES = ("AGENT_SQUAD/", "[REV-", "DISPOSITION", "VERIFIED", "NOT FIXED")
PATTERNS = {
    "review": re.compile(
        rf"{re.escape(TAG)} REVIEW pr=(?P<pr>{NUMBER}) head=(?P<head>{SHA})"
        rf" base=(?P<base>{SHA})"
        r" verdict=(?P<verdict>approved|changes_requested|needs_human)"
    ),
    "decision": re.compile(
        rf"{re.escape(TAG)} DECISION finding=(?P<finding>{FINDING_ID}|none)(?:"
        rf" budget=(?P<budget>{NUMBER}))?"
    ),
    "stop": re.compile(
        rf"{re.escape(TAG)} STOPPED head=(?P<head>{SHA})"
        r" reason=(?P<reason>budget|repeat|scope|design|ambiguity|judgement)"
    ),
    "finding": re.compile(
        rf"\[(?P<finding>{FINDING_ID})\]"
        r"\[(?P<severity>blocking|optional)\]"
        r"\[(?P<category>[a-z][a-z0-9-]*)\]"
        r" (?P<title>[^\r\n]+)"
    ),
    "disposition": re.compile(
        r"DISPOSITION (?:(?P<fixed>fixed)"
        rf" (?P<sha>{SHA})|(?P<rejected>rejected)|"
        r"(?P<needs_human>needs-human))"
    ),
    "verification": re.compile(
        r"(?P<verification>VERIFIED fixed|VERIFIED rejection accepted|NOT"
        r" FIXED)"
    ),
}
LIST_PATTERN = re.compile(
    rf"(?P<finding>{FINDING_ID}) \[(?P<severity>blocking|optional)\]"
    r" (?P<title>[^\r\n]+)"
)


@dataclass(frozen=True)
class TaggedLine:
    kind: str
    fields: dict[str, str]


def first_line(body: str) -> str:
    return body.partition("\n")[0]


def parse_line(line: str) -> TaggedLine | None:
    for kind, pattern in PATTERNS.items():
        match = pattern.fullmatch(line)
        if match and (kind != "finding" or match["title"].strip()):
            return TaggedLine(
                kind,
                {k: v for k, v in match.groupdict().items() if v is not None},
            )
    if line.lstrip().startswith(PREFIXES):
        raise AgentSquadError(f"malformed tagged line: {line}")
    return None


def render_line(kind: str, **fields: object) -> str:
    if kind == "review":
        line = (
            f'{TAG} REVIEW pr={fields["pr"]} head={fields["head"]}'
            f' base={fields["base"]} verdict={fields["verdict"]}'
        )
    elif kind == "decision":
        line = f'{TAG} DECISION finding={fields["finding"]}'
        if fields.get("budget") is not None:
            line += f' budget={fields["budget"]}'
    elif kind == "stop":
        line = f'{TAG} STOPPED head={fields["head"]} reason={fields["reason"]}'
    elif kind == "finding":
        line = (
            f'[{fields["finding"]}][{fields["severity"]}]'
            f'[{fields["category"]}]'
            f' {fields["title"]}'
        )
    else:
        raise AgentSquadError(f"cannot compose {kind} header")
    parsed = parse_line(line)
    if parsed is None or parsed.kind != kind:
        raise AgentSquadError(f"invalid {kind} header")
    return line


def headings(body: str) -> list[tuple[str, int, int]]:
    result = []
    offset = 0
    fence = None
    for line in body.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", text)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        elif fence is None and re.fullmatch(r"## [^\r\n]+", text):
            result.append((text[3:], offset, offset + len(line)))
        offset += len(line)
    return result


def section(body: str, name: str) -> str | None:
    found = headings(body)
    matches = [i for i, (heading, _, _) in enumerate(found) if heading == name]
    if not matches:
        return None
    if len(matches) != 1:
        raise AgentSquadError(f"duplicate ## {name} section")
    i = matches[0]
    end = found[i + 1][1] if i + 1 < len(found) else len(body)
    return body[found[i][1] : end].strip()


def section_content(body: str, name: str) -> str:
    value = section(body, name)
    if value is None:
        raise AgentSquadError(f"missing ## {name} section")
    return value.partition("\n")[2].strip()


def validate_pr_body(body: str) -> None:
    names = [h[0] for h in headings(body)]
    if names.count("Task") != 1 or names.count("Implementation report") != 1:
        raise AgentSquadError(
            "PR body needs exactly one ## Task and ## Implementation report"
        )
    if names.index("Task") > names.index("Implementation report"):
        raise AgentSquadError("## Task must precede ## Implementation report")


def validate_section(body: str, name: str) -> str:
    if [h[0] for h in headings(body)] != [name] or not body.startswith(
        f"## {name}\n"
    ):
        raise AgentSquadError(
            f"file must contain one complete ## {name} section"
        )
    if not section_content(body, name):
        raise AgentSquadError(f"## {name} must not be empty")
    if name == "Implementation report":
        expected = [
            "Summary",
            "Scope",
            "Files changed",
            "Design decisions",
            "Validation performed",
            "Known limitations",
            "Areas worth extra review",
        ]
        names = re.findall(r"^### (.+)$", body, re.MULTILINE)
        if names != expected:
            raise AgentSquadError(
                "Implementation report must contain the seven prescribed"
                " level-3 fields in order"
            )
    return body.strip()


def replace_section(body: str, name: str, replacement: str) -> str:
    validate_pr_body(body)
    replacement = validate_section(replacement, name)
    found = headings(body)
    i = next(i for i, h in enumerate(found) if h[0] == name)
    start = found[i][1]
    end = found[i + 1][1] if i + 1 < len(found) else len(body)
    # pr create's closing reference follows the report, outside its content.
    if name == "Implementation report":
        footer = re.search(
            r"^Closes #[1-9][0-9]*\s*$", body[start:end], re.MULTILINE
        )
        if footer:
            end = start + footer.start()
    return body[:start] + replacement + "\n\n" + body[end:]


def review_findings(body: str) -> list[dict[str, str]]:
    content = section_content(body, "Findings")
    if content == "none":
        return []
    result = []
    seen = set()
    for line in content.splitlines():
        match = LIST_PATTERN.fullmatch(line)
        if not match or not match["title"].strip() or match["finding"] in seen:
            raise AgentSquadError("invalid or duplicate ## Findings entry")
        seen.add(match["finding"])
        result.append(match.groupdict())
    if not result:
        raise AgentSquadError("## Findings must list findings or say none")
    return result


def validate_review_body(body: str) -> list[dict[str, str]]:
    names = [h[0] for h in headings(body)]
    allowed = [
        "Summary",
        "Verified dispositions",
        "Findings",
        "Standards",
        "Spec",
        "Evidence",
        "Unanchored findings",
    ]
    if names[:3] != allowed[:3] or len(names) != len(set(names)):
        raise AgentSquadError(
            "review requires Summary, Verified dispositions, Findings in order"
        )
    if any(n not in allowed for n in names) or names != sorted(
        names, key=allowed.index
    ):
        raise AgentSquadError("review sections are unknown or out of order")
    for name in allowed[:2]:
        if not section_content(body, name):
            raise AgentSquadError(f"## {name} must not be empty")
    return review_findings(body)


def unanchored_text(body: str) -> dict[str, str]:
    value = section(body, "Unanchored findings")
    if value is None:
        return {}
    result = {}
    current = None
    for line in value.partition("\n")[2].splitlines():
        match = PATTERNS["finding"].fullmatch(line)
        if match:
            current = match["finding"]
            if current in result:
                raise AgentSquadError("duplicate Unanchored findings entry")
            result[current] = line + "\n"
        elif current is not None:
            result[current] += line + "\n"
    return {k: v.strip() for k, v in result.items()}


def allocate_id(snapshot: Snapshot) -> int:
    """Reserve IDs across authors and resolved threads."""
    maximum = 0
    for comment in snapshot.comments:
        if comment.in_reply_to_id is None:
            match = PATTERNS["finding"].fullmatch(
                first_line(comment.evidence.body)
            )
            if match:
                maximum = max(maximum, int(match["finding"][4:]))
    for review in snapshot.reviews:
        try:
            line = parse_line(first_line(review.evidence.body))
            if line is None or line.kind != "review":
                continue
            for name in ("Findings", "Unanchored findings"):
                value = section(review.evidence.body, name) or ""
                for text in value.splitlines():
                    match = (
                        LIST_PATTERN
                        if name == "Findings"
                        else PATTERNS["finding"]
                    ).fullmatch(text)
                    if match:
                        maximum = max(maximum, int(match["finding"][4:]))
        except AgentSquadError:
            continue
    return maximum + 1


def order(evidence: Evidence | dict) -> tuple[datetime, int]:
    if isinstance(evidence, Evidence):
        return (
            datetime.fromisoformat(evidence.created_at.replace("Z", "+00:00")),
            evidence.id,
        )
    return (
        datetime.fromisoformat(evidence["created_at"].replace("Z", "+00:00")),
        evidence["id"],
    )


def newer(a: Evidence | dict | None, b: Evidence | dict | None) -> bool:
    return a is not None and (b is None or order(a) > order(b))


def evidence_dict(evidence: Evidence) -> dict:
    return asdict(evidence)


def derive(
    snapshot: Snapshot,
    config: Configuration,
    worktrees: tuple[Worktree, ...],
    base: str,
    ancestor: Callable[[str, str], bool],
    *,
    base_tip: str,
    dirty: bool = False,
    reviewer_live: bool = False,
) -> dict:
    """Reconstruct all §7.9 facts without reading or writing protocol files."""
    pr = snapshot.pr
    diagnostics = []
    implementer = config.implementer.forge_account.casefold()
    reviewer = config.reviewer.forge_account.casefold()
    developers = {a.casefold() for a in config.developer_accounts} | {
        implementer
    }

    def diagnostic(
        kind: str, evidence: Evidence, detail: str, repair: str | None = None
    ) -> None:
        diagnostics.append(
            {
                "kind": kind,
                "id": evidence.id,
                "detail": detail,
                "repair": repair,
            }
        )

    def tagged(e: Evidence, kinds: set[str]) -> TaggedLine | None:
        try:
            parsed = parse_line(first_line(e.body))
        except AgentSquadError as error:
            diagnostic("malformed", e, str(error))
            return None
        if parsed is None:
            return None
        authors = {
            "review": {reviewer},
            "finding": {reviewer, implementer},
            "disposition": {implementer},
            "verification": {reviewer},
            "decision": developers,
            "stop": {reviewer, implementer},
        }
        if (
            parsed.kind not in kinds
            or e.author.casefold() not in authors[parsed.kind]
        ):
            diagnostic(
                "unauthorized",
                e,
                f"{parsed.kind} is not valid here for {e.author}",
            )
            return None
        return parsed

    decisions = []
    stops = []
    for comment in sorted(snapshot.conversation, key=order):
        parsed = tagged(comment, {"decision", "stop"})
        if parsed:
            entry = {**evidence_dict(comment), **parsed.fields}
            if parsed.kind == "decision":
                if "budget" in entry:
                    entry["budget"] = int(entry["budget"])
                try:
                    task = section(comment.body, "Task")
                    if task is not None:
                        if entry["finding"] != "none":
                            raise AgentSquadError(
                                "a Task amendment must be a general decision"
                            )
                        validate_section(task + "\n", "Task")
                    entry["task"] = task
                except AgentSquadError as error:
                    diagnostic("malformed", comment, str(error))
                    continue
                decisions.append(entry)
            else:
                stops.append(entry)

    parsed_roots: dict[str, list[tuple[Comment, TaggedLine]]] = {}
    parsed_replies: dict[int, list[tuple[Evidence, TaggedLine]]] = {}
    for comment in sorted(snapshot.comments, key=lambda c: order(c.evidence)):
        root = comment.in_reply_to_id is None
        parsed = tagged(
            comment.evidence,
            {"finding"} if root else {"disposition", "verification"},
        )
        if parsed:
            if root:
                parsed_roots.setdefault(parsed.fields["finding"], []).append(
                    (comment, parsed)
                )
            else:
                parsed_replies.setdefault(comment.in_reply_to_id, []).append(
                    (comment.evidence, parsed)
                )
    resolution = {t.root_id: t for t in snapshot.threads}
    reviews = []
    findings = []
    seen_ids = set()

    def thread_state(finding: dict, until: Evidence | None = None) -> dict:
        root = finding["root"]
        disposition = None
        verification = None
        for e, parsed in parsed_replies.get(root["id"] if root else -1, []):
            if until is not None and newer(e, until):
                continue
            entry = {**evidence_dict(e), **parsed.fields}
            if parsed.kind == "disposition":
                entry["value"] = (
                    "fixed"
                    if "fixed" in parsed.fields
                    else (
                        "rejected"
                        if "rejected" in parsed.fields
                        else "needs-human"
                    )
                )
                if entry["value"] == "fixed" and (
                    not ancestor(entry["sha"], pr.head)
                    or ancestor(entry["sha"], finding["opening_head"])
                ):
                    if until is None:
                        diagnostic(
                            "invalid_disposition",
                            e,
                            "fixed SHA must be reachable from current head and"
                            " absent from opening head",
                        )
                    continue
                disposition = entry
            else:
                entry["value"] = parsed.fields["verification"]
                verification = entry
        decision = next(
            (
                d
                for d in reversed(decisions)
                if d["finding"] == finding["finding"]
                and (until is None or not newer(d, until))
            ),
            None,
        )
        settled = bool(verification and verification["value"] != "NOT FIXED")
        return {
            "latest_disposition": disposition,
            "latest_verification": verification,
            "latest_decision": decision,
            "settled": settled,
        }

    for review in sorted(snapshot.reviews, key=lambda r: order(r.evidence)):
        e = review.evidence
        parsed = tagged(e, {"review"})
        if parsed is None:
            continue
        fields = parsed.fields
        try:
            listed = validate_review_body(e.body)
            if (
                int(fields["pr"]) != pr.number
                or fields["head"] != review.commit_id
                or review.state == "PENDING"
                or not ancestor(fields["base"], fields["head"])
                or not ancestor(fields["base"], base_tip)
            ):
                raise AgentSquadError(
                    "review target, commit, base ancestry, or submission is"
                    " invalid"
                )
            if any(f["finding"] in seen_ids for f in listed):
                raise AgentSquadError(
                    "finding ID is listed by more than one tagged review"
                )
            verdict = fields["verdict"]
            if verdict == "approved" and (
                any(f["severity"] == "blocking" for f in listed)
                or any(
                    f["severity"] == "blocking"
                    and not thread_state(f, e)["settled"]
                    for f in findings
                )
            ):
                raise AgentSquadError(
                    "approved review has unsettled blocking findings"
                )
            prior = reviews[-1] if reviews else None
            not_fixed = any(
                p.fields.get("verification") == "NOT FIXED"
                and not newer(reply, e)
                and newer(reply, prior)
                for entries in parsed_replies.values()
                for reply, p in entries
            )
            if (
                verdict == "changes_requested"
                and not any(f["severity"] == "blocking" for f in listed)
                and not not_fixed
            ):
                raise AgentSquadError(
                    "changes_requested has no blocking finding or NOT FIXED in"
                    " this pass"
                )
            fallback = unanchored_text(e.body)
        except AgentSquadError as error:
            diagnostic("malformed_review", e, str(error))
            continue
        entry = {
            **evidence_dict(e),
            **fields,
            "pr": pr.number,
            "commit_id": review.commit_id,
            "state": review.state,
            "current": review.commit_id == pr.head,
            "findings": listed,
        }
        reviews.append(entry)
        if review.state != STATES[fields["verdict"]]:
            diagnostic(
                "forge_state_mismatch",
                e,
                f'{review.state} does not mirror {fields["verdict"]}',
            )
        incomplete = False
        for listed_finding in listed:
            fid = listed_finding["finding"]
            seen_ids.add(fid)
            candidates = parsed_roots.get(fid, [])
            eligible = []
            for candidate, line in candidates:
                if (
                    line.fields["severity"] != listed_finding["severity"]
                    or line.fields["title"] != listed_finding["title"]
                ):
                    diagnostic(
                        "malformed_finding",
                        candidate.evidence,
                        "root differs from its review Findings list",
                    )
                    continue
                if (
                    candidate.evidence.author.casefold() == implementer
                    and candidate.evidence.body.strip() != fallback.get(fid)
                ):
                    diagnostic(
                        "unauthorized",
                        candidate.evidence,
                        "Implementer roots must copy the Unanchored findings"
                        " entry",
                    )
                    continue
                eligible.append((candidate, line))
            if len(eligible) > 1:
                diagnostic(
                    "duplicate_finding", e, f"{fid} has multiple root comments"
                )
                eligible = []
            comment, line = eligible[0] if eligible else (None, None)
            root = evidence_dict(comment.evidence) if comment else None
            resolution_state = (
                resolution.get(comment.evidence.id) if comment else None
            )
            text = fallback.get(fid)
            if line is None and text:
                line = parse_line(first_line(text))
            replies = [
                evidence_dict(c.evidence)
                for c in snapshot.comments
                if comment and c.in_reply_to_id == comment.evidence.id
            ]
            finding = {
                **listed_finding,
                "category": line.fields["category"] if line else None,
                "opening_review": e.id,
                "opening_head": fields["head"],
                "opening_base": fields["base"],
                "root": root,
                "replies": sorted(replies, key=order),
                "unanchored_body": text,
                "anchor": (
                    {
                        "path": comment.path,
                        "line": comment.line,
                        "start_line": comment.start_line,
                        "side": comment.side,
                    }
                    if comment
                    else None
                ),
                "node_id": (
                    resolution_state.node_id if resolution_state else None
                ),
                "resolved": (
                    resolution_state.resolved if resolution_state else False
                ),
                "unanchored": comment is None,
            }
            findings.append(finding)
            if comment is None:
                incomplete = True
                if finding["severity"] == "optional":
                    diagnostic(
                        "optional_unanchored",
                        e,
                        f"{fid} has no root; advisory only",
                        "agent-squad thread open --as reviewer --pr"
                        f" {pr.number} --finding {fid} --path <path> --line"
                        " <line>",
                    )
        if incomplete:
            repair = (
                f"agent-squad review post --as reviewer --pr {pr.number}"
                f' --resume {e.id} --head {fields["head"]} --base'
                f' {fields["base"]} --verdict {fields["verdict"]} --body'
                " <file> --threads <file>"
                if entry["current"]
                else (
                    "agent-squad thread open --as implementer --pr"
                    f" {pr.number} --finding <REV-n> --path <path> --line"
                    " <line>"
                )
            )
            diagnostic(
                "incomplete_review",
                e,
                "review has a listed finding without a root",
                repair,
            )
    for finding in findings:
        finding.update(thread_state(finding))

    latest = reviews[-1] if reviews else None
    latest_decision = decisions[-1] if decisions else None
    latest_stop = stops[-1] if stops else None
    amendments = [d for d in decisions if d.get("task")]
    amendment = amendments[-1] if amendments else None
    try:
        validate_pr_body(pr.evidence.body)
        task = section(pr.evidence.body, "Task")
        report = section(pr.evidence.body, "Implementation report")
    except AgentSquadError as error:
        diagnostic("malformed_pr_body", pr.evidence, str(error))
        task = report = None
    effective_task = amendment["task"] if amendment else task
    task_stale = task != effective_task
    if task_stale:
        diagnostic(
            "task_body_stale",
            pr.evidence,
            "PR Task differs from the latest amendment",
            f"agent-squad decision post --as implementer --pr {pr.number}"
            " --finding none --task <same-task-file> --body <decision-file>",
        )
    used = len(reviews)
    effective = next(
        (d["budget"] for d in reversed(decisions) if "budget" in d),
        config.max_review_passes,
    )
    implementation = next(
        (w for w in worktrees if w.branch == f"refs/heads/{pr.head_branch}"),
        None,
    )
    blocking = [
        f for f in findings if f["severity"] == "blocking" and not f["settled"]
    ]
    needs_decision = bool(
        latest
        and latest["verdict"] == "needs_human"
        and not newer(latest_decision, latest)
    )
    needs_decision |= any(
        f["latest_disposition"]
        and f["latest_disposition"]["value"] == "needs-human"
        and not newer(f["latest_decision"], f["latest_disposition"])
        for f in blocking
    )
    unaddressed = any(
        not f["unanchored"]
        and not newer(f["latest_disposition"], f["latest_verification"])
        for f in blocking
    )
    same_head = bool(
        latest
        and latest["head"] == pr.head
        and blocking
        and not all(
            f["latest_disposition"]
            and f["latest_disposition"]["value"] == "rejected"
            for f in blocking
        )
    )
    gates = {
        "stopped": newer(latest_stop, latest_decision),
        "needs_decision": bool(needs_decision),
        "unanchored_findings": any(f["unanchored"] for f in blocking),
        "unaddressed_findings": unaddressed,
        "same_head_requires_rejections": same_head,
        "task_amended": newer(amendment, latest),
        "budget_exhausted": used >= effective,
        "not_pushed": (
            implementation is None or implementation.head != pr.head or dirty
        ),
        "reviewer_live": reviewer_live,
    }
    approval_reasons = []
    conditions = [
        (
            latest is not None and latest["verdict"] == "approved",
            "latest tagged review is not approved",
        ),
        (
            latest is not None and latest["commit_id"] == pr.head,
            "latest review is not at the PR head",
        ),
        (
            implementation is not None and implementation.head == pr.head,
            "implementation worktree HEAD differs or is unavailable",
        ),
        (
            not newer(latest_stop, latest),
            "a STOPPED comment is newer than the review",
        ),
        (
            latest is not None and latest["state"] == "APPROVED",
            "forge review state is not APPROVED",
        ),
        (not newer(amendment, latest), "Task was amended after the review"),
    ]
    approval_reasons.extend(
        reason for passed, reason in conditions if not passed
    )
    approved = not approval_reasons
    action_conditions = [
        ("merged", pr.merged),
        ("closed", pr.state == "closed"),
        ("approved", approved),
        ("stopped", gates["stopped"]),
        (
            "needs_decision",
            gates["needs_decision"] or gates["budget_exhausted"],
        ),
        ("open_threads", gates["unanchored_findings"]),
        ("address_findings", gates["unaddressed_findings"]),
        (
            "push",
            gates["not_pushed"] or (same_head and not gates["task_amended"]),
        ),
        (
            "reviewer_live",
            reviewer_live and not any(r["current"] for r in reviews),
        ),
        ("launch_review", True),
    ]
    action = next(a for a, applies in action_conditions if applies)
    reasons = {
        "merged": ["PR is merged"],
        "closed": ["PR is closed without a merge"],
        "approved": ["all six approval conditions hold"],
        "stopped": ["STOPPED is newer than the latest DECISION"],
        "needs_decision": [
            "a Developer decision or budget extension is required"
        ],
        "open_threads": ["a blocking finding has no root comment"],
        "address_findings": ["a blocking finding needs a new disposition"],
        "push": [
            "push a clean changed revision matching the PR branch and head"
        ],
        "launch_review": ["review launch gates are clear"],
        "reviewer_live": [
            "Reviewer for the current head is live; no current review exists"
        ],
    }[action]
    acted = bool(
        latest
        and (
            any(newer(d, latest) for d in decisions)
            or any(newer(f["latest_disposition"], latest) for f in findings)
        )
    )
    return {
        "next_action": action,
        "reasons": reasons,
        "target": {
            "pr": pr.number,
            "head": pr.head,
            "head_branch": pr.head_branch,
            "base": base,
            "base_tip": base_tip,
            "base_branch": pr.base_branch,
        },
        "pr": asdict(pr),
        "task": task,
        "implementation_report": report,
        "effective_task": effective_task,
        "task_body_stale": task_stale,
        "reviews": reviews,
        "current_review_unacted": (
            latest if latest and latest["current"] and not acted else None
        ),
        "findings": findings,
        "decisions": decisions,
        "general_decisions": [d for d in decisions if d["finding"] == "none"],
        "stops": stops,
        "budget": {
            "effective": effective,
            "used": used,
            "remaining": effective - used,
        },
        "gates": gates,
        "approval": {"approved": approved, "reasons": approval_reasons},
        "worktrees": [
            {"path": str(w.root), "head": w.head, "branch": w.branch}
            for w in worktrees
        ],
        "diagnostics": diagnostics,
    }
