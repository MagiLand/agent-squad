"""Protocol grammar and authority tests without network or Git."""

from dataclasses import replace
from pathlib import Path
import unittest

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.conventions import (
    allocate_id,
    derive,
    parse_line,
    render_line,
    replace_section,
    validate_pr_body,
    validate_section,
)
from agent_squad.forge import (
    Comment,
    Evidence,
    PullRequest,
    Review,
    Snapshot,
    ThreadState,
)
from agent_squad.initialization import AgentSquadError, Configuration, Worktree
from tests.forge_support import TASK, REPORT, REVIEW, FINDING_BODY

A, H, J = "a" * 40, "b" * 40, "c" * 40


def config() -> Configuration:
    return Configuration.from_dict(
        {
            "schema_version": 2,
            "forge": {"kind": "github", "owner": "org", "repo": "repo"},
            "implementer": {
                "agent_name": "implementer",
                "kind": "codex",
                "forge_account": "dev",
            },
            "reviewer": {
                "kind": "claude",
                "start_args": [],
                "forge_account": "reviewer",
            },
            "developer_accounts": ["human"],
            "base_branch": "main",
            "max_review_passes": 3,
            "merge_method": "merge",
            "worktree_root": ".agent-squad/worktrees",
            "scratch_root": ".agent-squad/review-scratch",
        }
    )


def evidence(ident: int, body: str, author: str = "reviewer") -> Evidence:
    return Evidence(
        ident,
        author,
        f"2026-01-01T00:{ident // 60:02d}:{ident % 60:02d}Z",
        body,
    )


def review(
    ident: int,
    verdict: str = "approved",
    *,
    head: str = H,
    state: str | None = None,
    findings: str = "none",
    author: str = "reviewer",
    fallback: str = "",
) -> Review:
    body = (
        render_line("review", pr=1, head=head, base=A, verdict=verdict)
        + "\n\n"
    )
    body += REVIEW.replace("## Findings\n\nnone", "## Findings\n\n" + findings)
    if fallback:
        body += "\n## Unanchored findings\n\n" + fallback
    return Review(
        evidence(ident, body, author),
        head,
        state
        or {
            "approved": "APPROVED",
            "changes_requested": "CHANGES_REQUESTED",
            "needs_human": "COMMENTED",
        }[verdict],
    )


def root(
    ident: int = 11,
    *,
    fid: str = "REV-1",
    severity: str = "blocking",
    author: str = "reviewer",
) -> Comment:
    return Comment(
        evidence(
            ident,
            f"[{fid}][{severity}][tests] Finding\n\n" + FINDING_BODY,
            author,
        ),
        10,
        None,
        "example.py",
        2,
        None,
        "RIGHT",
    )


def reply(
    ident: int, body: str, *, author: str = "dev", root_id: int = 11
) -> Comment:
    return Comment(
        evidence(ident, body, author), 10, root_id, None, None, None, None
    )


def decision(
    ident: int,
    *,
    finding: str = "none",
    budget: int | None = None,
    body: str = "Scripted decision",
    author: str = "dev",
) -> Evidence:
    return evidence(
        ident,
        render_line("decision", finding=finding, budget=budget)
        + "\n\n"
        + body,
        author,
    )


def snapshot(
    *,
    reviews: tuple = (),
    comments: tuple = (),
    conversation: tuple = (),
    head: str = H,
    body: str = TASK + "\n" + REPORT,
    merged: bool = False,
    closed: bool = False,
) -> Snapshot:
    pr = PullRequest(
        evidence(1, body, "dev"),
        1,
        "Fixture",
        head,
        "issue-1",
        A,
        "main",
        "closed" if closed or merged else "open",
        merged,
        head if merged else None,
        "clean",
    )
    return Snapshot(
        pr, reviews, comments, conversation, (ThreadState(11, "T-11", True),)
    )


def derive_state(
    s: Snapshot,
    *,
    configuration: Configuration | None = None,
    local_head: str | None = None,
    missing_worktree: bool = False,
) -> dict:
    worktrees = (
        ()
        if missing_worktree
        else (
            Worktree(
                Path("/repo/issue"),
                local_head or s.pr.head,
                "refs/heads/issue-1",
            ),
        )
    )
    return derive(
        s,
        configuration or config(),
        worktrees,
        A,
        lambda a, b: a == b or a == A or (a == H and b == J),
    )


class GrammarTests(unittest.TestCase):
    def test_all_tagged_lines_round_trip(self) -> None:
        lines = [
            render_line("review", pr=1, head=H, base=A, verdict="approved"),
            render_line("decision", finding="REV-1", budget=4),
            render_line("stop", head=H, reason="design"),
            render_line(
                "finding",
                finding="REV-1",
                severity="blocking",
                category="tests",
                title="Finding",
            ),
            f"DISPOSITION fixed {J}",
            "DISPOSITION rejected",
            "DISPOSITION needs-human",
            "VERIFIED fixed",
            "VERIFIED rejection accepted",
            "NOT FIXED",
        ]
        for line in lines:
            with self.subTest(line=line):
                self.assertIsNotNone(parse_line(line))
                for malformed in [
                    line + " extra",
                    " " + line,
                    line + " ",
                    line.replace(" ", "  ", 1),
                ]:
                    # Finding titles legitimately accept arbitrary additional
                    # words/spaces.
                    if line.startswith("[REV-") and malformed in [
                        line + " extra",
                        line + " ",
                        line.replace(" ", "  ", 1),
                    ]:
                        continue
                    if malformed == "NOT  FIXED":
                        self.assertIsNone(parse_line(malformed))
                        continue
                    with self.assertRaises(AgentSquadError):
                        parse_line(malformed)

    def test_malformed_tag_variants(self) -> None:
        line = render_line("review", pr=1, head=H, base=A, verdict="approved")
        for invalid in [
            line.replace("0.5.0", "0.4.4"),
            line.replace("pr=1", "pr=01"),
            line.replace(H, H[:7]),
            line.replace(H, H.upper()),
            line.replace(" base=" + A, ""),
            line + "\r",
            "DISPOSITION fixed " + J[:12],
            "VERIFIED rejected",
            "NOT FIXED yet",
            "[REV-01][blocking][tests] Finding",
            "[REV-1][blocking][Tests] Finding",
        ]:
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(AgentSquadError),
            ):
                parse_line(invalid)
        self.assertIsNone(parse_line("Ordinary prose."))
        self.assertIsNotNone(
            parse_line(
                render_line(
                    "review",
                    pr=1,
                    head="a" * 64,
                    base="b" * 64,
                    verdict="needs_human",
                )
            )
        )

    def test_pr_sections_and_report_replacement_preserve_task_and_footer(
        self,
    ) -> None:
        body = (
            TASK + "\n" + REPORT + "\nCloses #42\n\n## Notes\n\nKeep this.\n"
        )
        replaced = replace_section(
            body,
            "Implementation report",
            REPORT.replace("Scripted fixture.", "Updated."),
        )
        self.assertTrue(replaced.startswith(TASK))
        self.assertIn("Closes #42", replaced)
        self.assertTrue(replaced.endswith("## Notes\n\nKeep this.\n"))
        for invalid in [REPORT + TASK, TASK, TASK + TASK + REPORT, REPORT]:
            with self.assertRaises(AgentSquadError):
                validate_pr_body(invalid)
        with self.assertRaises(AgentSquadError):
            validate_section("## Task\n\nOne\n\n## Extra\nTwo\n", "Task")


class DerivedStateTests(unittest.TestCase):
    def test_approval_precedes_budget_gates_at_each_limit(self) -> None:
        for count in (1, 3, 4):
            with self.subTest(count=count):
                c = replace(config(), max_review_passes=min(count, 3))
                decisions = (decision(2, budget=4),) if count == 4 else ()
                s = snapshot(
                    reviews=tuple(review(10 + i) for i in range(count)),
                    conversation=decisions,
                )
                result = derive_state(s, configuration=c)
                self.assertEqual(result["next_action"], "approved")
                self.assertEqual(result["budget"]["remaining"], 0)

    def test_budget_exhaustion_with_and_without_stop(self) -> None:
        reviews = (
            review(8),
            review(9),
            review(
                10, "changes_requested", findings="REV-1 [blocking] Finding"
            ),
        )
        s = snapshot(reviews=reviews, comments=(root(),))
        self.assertEqual(derive_state(s)["next_action"], "needs_decision")
        stop = evidence(
            12, render_line("stop", head=H, reason="budget"), "dev"
        )
        self.assertEqual(
            derive_state(replace(s, conversation=(stop,)))["next_action"],
            "stopped",
        )
        result = derive_state(
            replace(s, conversation=(stop, decision(13, budget=4)))
        )
        self.assertEqual(result["next_action"], "address_findings")
        self.assertEqual(result["budget"]["remaining"], 1)

    def test_task_amendment_and_fresh_same_head_approval(
        self,
    ) -> None:
        s = snapshot(reviews=(review(10),))
        report_change = replace(
            s,
            pr=replace(
                s.pr,
                evidence=replace(
                    s.pr.evidence,
                    body=TASK + REPORT.replace("Scripted", "Updated"),
                ),
            ),
        )
        self.assertEqual(
            derive_state(report_change)["next_action"], "approved"
        )
        amendment = decision(
            11,
            body=(
                "Task approved by Developer.\n\n## Task\n\nAmended objective."
            ),
        )
        s = replace(s, conversation=(amendment,))
        result = derive_state(s)
        self.assertEqual(result["next_action"], "launch_review")
        self.assertTrue(result["task_body_stale"])
        self.assertIn("Amended objective.", result["effective_task"])
        self.assertEqual(
            derive_state(replace(s, reviews=(review(10), review(12))))[
                "next_action"
            ],
            "approved",
        )

    def test_authors_general_decisions_and_equal_timestamp_order(self) -> None:
        stop = evidence(10, render_line("stop", head=H, reason="design"))
        decisions = [
            decision(11, body="Choose approach A."),
            decision(12, body="Retain requirement B.", author="human"),
            decision(13, budget=4),
            decision(
                14, body="Revise approach A only; supersedes #issuecomment-11."
            ),
        ]
        ignored = decision(
            15, budget=50, body="## Task\n\nMalicious task", author="bot"
        )
        s = snapshot(conversation=(stop, *decisions, ignored))
        result = derive_state(s)
        self.assertEqual(len(result["general_decisions"]), 4)
        self.assertEqual(result["budget"]["effective"], 4)
        self.assertNotIn("Malicious", result["effective_task"])
        self.assertFalse(result["gates"]["stopped"])
        tied = replace(decisions[0], created_at=stop.created_at)
        self.assertFalse(
            derive_state(snapshot(conversation=(stop, tied)))["gates"][
                "stopped"
            ]
        )
        self.assertTrue(
            derive_state(snapshot(conversation=(stop, replace(tied, id=9))))[
                "gates"
            ]["stopped"]
        )
        self.assertTrue(
            any(d["kind"] == "unauthorized" for d in result["diagnostics"])
        )

    def test_invalid_reviews_do_not_count_and_old_heads_do(self) -> None:
        valid = review(10, head=H)
        wrong_author = review(11, author="bot")
        malformed = replace(
            review(12), evidence=evidence(12, "AGENT_SQUAD/0.5.0 REVIEW bad")
        )
        wrong_commit = replace(review(13), commit_id=J)
        result = derive_state(
            snapshot(
                reviews=(valid, wrong_author, malformed, wrong_commit), head=J
            )
        )
        self.assertEqual(result["budget"]["used"], 1)
        self.assertEqual(result["next_action"], "launch_review")
        self.assertFalse(result["reviews"][0]["current"])

    def test_thread_dispositions_not_fixed_and_reconsideration(self) -> None:
        s = snapshot(
            reviews=(
                review(
                    10,
                    "changes_requested",
                    findings="REV-1 [blocking] Finding",
                ),
            ),
            comments=(root(),),
        )
        result = derive_state(s)
        self.assertEqual(result["next_action"], "address_findings")
        self.assertTrue(result["current_review_unacted"])
        rejected = reply(12, "DISPOSITION rejected\n\nEvidence.")
        s = replace(s, comments=(*s.comments, rejected))
        self.assertEqual(derive_state(s)["next_action"], "launch_review")
        not_fixed = reply(13, "NOT FIXED\n\nProbe failed.", author="reviewer")
        s = replace(s, comments=(*s.comments, not_fixed))
        self.assertEqual(derive_state(s)["next_action"], "address_findings")
        s = replace(
            s, comments=(*s.comments, reply(14, "DISPOSITION rejected"))
        )
        self.assertEqual(derive_state(s)["next_action"], "launch_review")
        s = replace(
            s,
            comments=(
                *s.comments,
                reply(15, "VERIFIED rejection accepted", author="reviewer"),
            ),
        )
        self.assertTrue(derive_state(s)["findings"][0]["settled"])
        approved = replace(s, reviews=(*s.reviews, review(16)))
        self.assertEqual(derive_state(approved)["next_action"], "approved")

    def test_fixed_commit_ancestry_and_authorship(self) -> None:
        base = snapshot(
            reviews=(
                review(
                    10,
                    "changes_requested",
                    findings="REV-1 [blocking] Finding",
                ),
            ),
            comments=(root(),),
            head=J,
        )
        for body, author in [
            (f"DISPOSITION fixed {H}", "dev"),
            (f'DISPOSITION fixed {"d" * 40}', "dev"),
            (f"DISPOSITION fixed {J}", "bot"),
        ]:
            with self.subTest(body=body, author=author):
                self.assertEqual(
                    derive_state(
                        replace(
                            base,
                            comments=(root(), reply(12, body, author=author)),
                        )
                    )["next_action"],
                    "address_findings",
                )
        result = derive_state(
            replace(
                base, comments=(root(), reply(12, f"DISPOSITION fixed {J}"))
            )
        )
        self.assertEqual(result["next_action"], "launch_review")

    def test_needs_human_disposition_requires_a_newer_specific_decision(
        self,
    ) -> None:
        s = snapshot(
            reviews=(
                review(
                    10,
                    "changes_requested",
                    findings="REV-1 [blocking] Finding",
                ),
            ),
            comments=(root(), reply(12, "DISPOSITION needs-human")),
        )
        self.assertEqual(derive_state(s)["next_action"], "needs_decision")
        self.assertTrue(
            derive_state(replace(s, conversation=(decision(13),)))["gates"][
                "needs_decision"
            ]
        )
        self.assertFalse(
            derive_state(
                replace(s, conversation=(decision(13, finding="REV-1"),))
            )["gates"]["needs_decision"]
        )
        self.assertTrue(
            derive_state(
                replace(s, conversation=(decision(11, finding="REV-1"),))
            )["gates"]["needs_decision"]
        )

    def test_needs_human_review_requires_a_newer_decision(self) -> None:
        s = snapshot(reviews=(review(10, "needs_human"),))
        self.assertEqual(derive_state(s)["next_action"], "needs_decision")
        self.assertFalse(
            derive_state(replace(s, conversation=(decision(11),)))["gates"][
                "needs_decision"
            ]
        )

    def test_fallback_association_unanchored_and_optional(self) -> None:
        blocking = root()
        s = snapshot(
            reviews=(
                review(
                    10,
                    "changes_requested",
                    findings="REV-1 [blocking] Finding",
                    fallback=blocking.evidence.body,
                ),
            )
        )
        result = derive_state(s)
        self.assertEqual(result["next_action"], "open_threads")
        self.assertEqual(result["budget"]["used"], 1)
        self.assertIn(FINDING_BODY, result["findings"][0]["unanchored_body"])
        recovered = replace(
            blocking,
            review_id=99,
            evidence=replace(blocking.evidence, author="dev"),
        )
        result = derive_state(replace(s, comments=(recovered,)))
        self.assertEqual(result["next_action"], "address_findings")
        self.assertEqual(result["findings"][0]["opening_review"], 10)
        optional = snapshot(
            reviews=(
                review(
                    10,
                    findings="REV-1 [optional] Finding",
                    fallback=root(severity="optional").evidence.body,
                ),
            )
        )
        result = derive_state(optional)
        self.assertEqual(result["next_action"], "approved")
        self.assertFalse(result["gates"]["unanchored_findings"])

    def test_approval_conditions_fail_independently(self) -> None:
        s = snapshot(reviews=(review(10),))
        cases = [
            derive_state(replace(s, reviews=(review(10, "needs_human"),))),
            derive_state(replace(s, pr=replace(s.pr, head=J))),
            derive_state(s, local_head=J),
            derive_state(s, missing_worktree=True),
            derive_state(
                replace(
                    s,
                    conversation=(
                        evidence(
                            11, render_line("stop", head=H, reason="design")
                        ),
                    ),
                )
            ),
            derive_state(replace(s, reviews=(review(10, state="COMMENTED"),))),
            derive_state(
                replace(
                    s, conversation=(decision(11, body="## Task\n\nChanged."),)
                )
            ),
        ]
        for result in cases:
            self.assertFalse(result["approval"]["approved"])
            self.assertTrue(result["approval"]["reasons"])
        self.assertTrue(
            any(
                d["kind"] == "forge_state_mismatch"
                for d in cases[-2]["diagnostics"]
            )
        )

    def test_settled_blocking_findings_are_required_at_approval_time(
        self,
    ) -> None:
        s = snapshot(
            reviews=(
                review(
                    10,
                    "changes_requested",
                    findings="REV-1 [blocking] Finding",
                ),
                review(12),
            ),
            comments=(root(), reply(13, "VERIFIED fixed", author="reviewer")),
        )
        result = derive_state(s)
        self.assertEqual(result["budget"]["used"], 1)
        self.assertFalse(result["approval"]["approved"])

    def test_id_allocation_uses_other_authors_and_resolved_threads(
        self,
    ) -> None:
        s = snapshot(
            reviews=(
                review(10, findings="REV-8 [optional] Other", author="bot"),
            ),
            comments=(root(fid="REV-6", author="bot"),),
        )
        self.assertEqual(allocate_id(s), 9)
        s = replace(
            s,
            reviews=(
                review(
                    10,
                    findings="none",
                    fallback="[REV-12][optional][tests] Orphan",
                    author="bot",
                ),
            ),
        )
        self.assertEqual(allocate_id(s), 13)

    def test_merged_and_closed_precede_all_gates(self) -> None:
        stop = evidence(12, render_line("stop", head=H, reason="design"))
        self.assertEqual(
            derive_state(snapshot(merged=True, conversation=(stop,)))[
                "next_action"
            ],
            "merged",
        )
        self.assertEqual(
            derive_state(snapshot(closed=True, conversation=(stop,)))[
                "next_action"
            ],
            "closed",
        )
        self.assertEqual(
            derive_state(snapshot(), local_head=J)["next_action"], "push"
        )

    def test_evidence_contains_full_bodies_and_metadata(self) -> None:
        s = snapshot(
            reviews=(
                review(
                    10,
                    "changes_requested",
                    findings="REV-1 [blocking] Finding",
                ),
            ),
            comments=(
                root(),
                reply(
                    12, "DISPOSITION rejected\n\nDetailed execution evidence."
                ),
            ),
            conversation=(decision(13, body="Detailed Developer decision."),),
        )
        result = derive_state(s)
        for item in (
            result["pr"]["evidence"],
            result["reviews"][0],
            result["findings"][0]["root"],
            result["findings"][0]["replies"][0],
            result["decisions"][0],
        ):
            self.assertTrue(
                all(k in item for k in ("id", "author", "created_at", "body"))
            )
        self.assertIn(
            "Detailed execution", result["findings"][0]["replies"][0]["body"]
        )
