"""Protocol grammar and authority tests without network or Git."""

from dataclasses import replace
from pathlib import Path
import unittest

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.conventions import (
    MERGE_INSTRUCTION,
    MERGE_WITHDRAWAL,
    allocate_id,
    derive,
    parse_line,
    render_line,
    replace_section,
    review_findings,
    section,
    task_from_issue,
    validate_pr_body,
    validate_review_body,
    validate_section,
)
from agent_squad.forge import (
    Comment,
    Evidence,
    PullRequest,
    Review,
    ReviewState,
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
            "approved": ReviewState.APPROVED,
            "changes_requested": ReviewState.CHANGES_REQUESTED,
            "needs_human": ReviewState.COMMENTED,
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
        pr, reviews, comments, conversation, (ThreadState(11, "T-11", True),),
        can_resolve_threads=True, can_read_thread_resolution=True,
        can_read_branch_rules=True,
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
        base_tip=A,
    )


class IssueTaskTests(unittest.TestCase):
    def issue(self, body: str, **overrides) -> dict:
        return dict(number=74, title="Keep exact wording", body=body,
                    state="open", is_pull_request=False) | overrides

    def test_heading_levels_and_line_endings_preserve_other_wording(
        self,
    ) -> None:
        body = "\r\n".join("#" * n + f" Heading {n}" for n in range(1, 7))
        task = task_from_issue(self.issue(body + "\r\nPlain text.\r\n"))
        expected = (
            '## Task\n\nThis Task is issue #74, "Keep exact wording", '
            'copied without rewording.\n\n'
            '### Heading 1\n### Heading 2\n#### Heading 3\n'
            '##### Heading 4\n###### Heading 5\n###### Heading 6\n'
            'Plain text.\n'
        )
        self.assertEqual(task, expected)
        self.assertEqual(validate_section(task, "Task"), task.strip())

    def test_fenced_headings_and_non_headings_are_copied_exactly(self) -> None:
        for marker in ("```", "~~~", "````", "~~~~"):
            with self.subTest(marker=marker):
                body = (
                    f"{marker}markdown\n# Literal\n## Literal\n"
                    f"{marker} not a closing fence\n## Still literal\n"
                    f"{marker}\n  ## Nested\n#\n#not-heading\n"
                    "####### not-heading\n    ## indented code\n"
                )
                task = task_from_issue(self.issue(body))
                self.assertTrue(task.endswith(body.replace(
                    "  ## Nested\n#\n", "  ### Nested\n###\n"
                )))
                validate_section(task, "Task")

    def test_fences_close_only_on_their_own_marker(self) -> None:
        for body, copied in (
            ("```\n~~~\n# inside\n```\n# after\n",
             "```\n~~~\n# inside\n```\n### after\n"),
            ("````\n```\n# inside\n````\n",
             "````\n```\n# inside\n````\n"),
            ("```x``` prose\n## Real\n", "```x``` prose\n### Real\n"),
        ):
            with self.subTest(body=body):
                task = task_from_issue(self.issue(body))
                self.assertTrue(task.endswith(copied))
                self.assertEqual(
                    section("## A\n\n" + body + "\n## B\n\nb\n", "B"),
                    "## B\n\nb",
                )

    def test_unclosed_fence_ends_with_the_copied_issue(self) -> None:
        for body, copied in (
            ("Logs:\n```\n## boom\n", "Logs:\n```\n## boom\n```\n"),
            ("~~~~\n# boom", "~~~~\n# boom\n~~~~\n"),
            ("```", "```\n```\n"),
        ):
            with self.subTest(body=body):
                task = task_from_issue(self.issue(body))
                self.assertTrue(task.endswith(copied))
                validate_pr_body(task + "\n\n" + REPORT)
                self.assertEqual(
                    section(task + "\n\n" + REPORT, "Implementation report"),
                    REPORT.strip(),
                )

    def test_closed_pull_request_and_empty_body_are_refused(self) -> None:
        for overrides, message in (
            ({"state": "closed"}, "open issue"),
            ({"is_pull_request": True}, "not a pull request"),
            ({"body": " \r\n"}, "must not be empty"),
        ):
            with self.subTest(overrides=overrides):
                issue = self.issue("Requirements") | overrides
                with self.assertRaisesRegex(AgentSquadError, message):
                    task_from_issue(issue)


class StandingMergeTests(unittest.TestCase):
    def test_merge_requires_current_approval_and_no_active_stop(self) -> None:
        instruction = decision(11, body=MERGE_INSTRUCTION)
        stop = evidence(5, render_line("stop", head=H, reason="scope"))
        for reviews, conversation, expected in (
            ((), (instruction,), "launch_review"),
            ((review(10, head=J),), (instruction,), "launch_review"),
            ((review(10),), (stop, instruction), "approved"),
        ):
            with self.subTest(expected=expected, reviews=reviews):
                state = derive_state(snapshot(
                    reviews=reviews, conversation=conversation))
                self.assertIsNotNone(state["merge_instruction"])
                self.assertEqual(state["next_action"], expected)
                if stop in conversation:
                    self.assertTrue(state["gates"]["stopped"])
                    self.assertTrue(state["approval"]["approved"])

    def test_record_withdraw_and_record_again(self) -> None:
        recorded = decision(2, body=MERGE_INSTRUCTION + '\n\n> Start #1.')
        withdrawn = decision(11, body=MERGE_WITHDRAWAL)
        for conversation, expected in (
            ((), "approved"),
            ((recorded,), "merge"),
            ((recorded, withdrawn), "approved"),
            ((recorded, withdrawn, decision(12, body=MERGE_INSTRUCTION)),
             "merge"),
        ):
            with self.subTest(expected=expected, count=len(conversation)):
                state = derive_state(snapshot(
                    reviews=(review(10),), conversation=conversation))
                self.assertEqual(state["next_action"], expected)
                self.assertEqual(state["general_decisions"], [])
                if expected == "merge":
                    self.assertEqual(state["merge_instruction"], {
                        "id": conversation[-1].id, "author": "dev",
                        "created_at": conversation[-1].created_at,
                    })
                else:
                    self.assertIsNone(state["merge_instruction"])

    def test_stop_and_amendment_cancel_until_recorded_again(self) -> None:
        stop = evidence(12, render_line("stop", head=H, reason="design"))
        amendment = decision(12, body="Amend.\n\n## Task\n\nNew task.")
        for event in (stop, amendment):
            with self.subTest(event=event.body):
                conversation = (decision(2, body=MERGE_INSTRUCTION), event,
                                decision(13, body="Continue as decided."))
                state = derive_state(snapshot(
                    reviews=(review(10), review(14)),
                    conversation=conversation))
                self.assertIsNone(state["merge_instruction"])
                self.assertEqual(state["next_action"], "approved")
                state = derive_state(snapshot(
                    reviews=(review(10), review(14)),
                    conversation=(*conversation,
                                  decision(15, body=MERGE_INSTRUCTION))))
                self.assertEqual(state["next_action"], "merge")

    def test_neither_directive_lifts_stop_or_resolves_human_decision(
        self,
    ) -> None:
        for directive in (MERGE_INSTRUCTION, MERGE_WITHDRAWAL):
            with self.subTest(directive=directive):
                stopped = derive_state(snapshot(
                    reviews=(review(10),), conversation=(
                        evidence(11, render_line(
                            "stop", head=H, reason="judgement")),
                        decision(12, body=directive))))
                self.assertTrue(stopped["gates"]["stopped"])
                self.assertEqual(stopped["next_action"], "stopped")
                needs = derive_state(snapshot(
                    reviews=(review(10, "needs_human"),),
                    conversation=(decision(12, body=directive),)))
                self.assertTrue(needs["gates"]["needs_decision"])
                self.assertEqual(needs["next_action"], "needs_decision")
                thread = derive_state(snapshot(
                    reviews=(review(10, findings="REV-1 [optional] Finding"),),
                    comments=(root(severity="optional"), reply(
                        12, "DISPOSITION needs-human\nChoose.")),
                    conversation=(decision(13, body=directive),)))
                self.assertTrue(thread["gates"]["needs_decision"])
                self.assertNotEqual(thread["next_action"], "merge")

    def test_unauthorized_and_combined_decisions_have_no_effect(self) -> None:
        for directive in (MERGE_INSTRUCTION, MERGE_WITHDRAWAL):
            for kwargs in (
                {"author": "stranger"}, {"budget": 9},
                {"body": directive + "\n\n## Task\n\nSneaky task."},
                {"body": directive + "\nbudget=9"},
                {"finding": "REV-1"},
            ):
                with self.subTest(directive=directive, kwargs=kwargs):
                    invalid = decision(11, **({"body": directive} | kwargs))
                    state = derive_state(snapshot(
                        reviews=(review(10),), conversation=(invalid,)))
                    self.assertEqual(state["next_action"], "approved")
                    self.assertIsNone(state["merge_instruction"])
                    self.assertEqual(state["decisions"], [])
                    self.assertEqual(state["budget"]["effective"], 3)
                    self.assertEqual(state["effective_task"], TASK.strip())
                    self.assertIn(state["diagnostics"][0]["kind"],
                                  ("malformed", "unauthorized"))

    def test_developer_can_record_and_only_latest_review_hold_applies(
        self,
    ) -> None:
        held = review(10)
        held = replace(held, evidence=replace(
            held.evidence, body=held.evidence.body +
            "\n## Merge hold\n\nItem 3: changes merge authority.\n"))
        instruction = decision(2, body=MERGE_INSTRUCTION, author="human")
        state = derive_state(snapshot(
            reviews=(held,), conversation=(instruction,)))
        self.assertEqual(state["next_action"], "approved")
        self.assertTrue(state["approval"]["approved"])
        self.assertEqual(state["merge_hold"], {
            "review_id": 10, "text": "Item 3: changes merge authority."})
        state = derive_state(snapshot(
            reviews=(held, review(11)), conversation=(instruction,)))
        self.assertEqual(state["next_action"], "merge")
        self.assertIsNone(state["merge_hold"])

    def test_optional_dispositions_come_before_merge(self) -> None:
        state = derive_state(snapshot(
            reviews=(review(10, findings="REV-1 [optional] Finding"),),
            comments=(root(severity="optional"),),
            conversation=(decision(2, body=MERGE_INSTRUCTION),)))
        self.assertEqual(state["next_action"], "address_findings")
        self.assertTrue(state["approval"]["approved"])

    def test_merge_hold_section_requires_content_and_correct_position(
        self,
    ) -> None:
        hold = "## Merge hold\n\nTask: Developer review required.\n\n"
        self.assertEqual(validate_review_body(REVIEW + "\n" + hold), [])
        self.assertEqual(validate_review_body(
            REVIEW + "\n" + hold + "## Standards\n\nok\n"), [])
        for body in (
            hold + REVIEW,
            REVIEW.replace("## Findings", hold + "## Findings"),
            REVIEW + "\n## Merge hold\n\n",
            REVIEW + "\n## Standards\n\nok\n\n" + hold,
            REVIEW + "\n## Evidence\n\nProof.\n\n" + hold,
        ):
            with self.subTest(body=body):
                with self.assertRaises(AgentSquadError):
                    validate_review_body(body)


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

    def test_section_validators_reject_duplicate_empty_and_wrong_report(
        self,
    ) -> None:
        with self.assertRaisesRegex(AgentSquadError, "duplicate ## Task"):
            section(TASK + TASK, "Task")
        with self.assertRaisesRegex(AgentSquadError, "must not be empty"):
            validate_section("## Task\n\n", "Task")
        with self.assertRaisesRegex(AgentSquadError, "seven prescribed"):
            validate_section(
                "## Implementation report\n\nIncomplete.",
                "Implementation report",
            )
        with self.assertRaisesRegex(AgentSquadError, "list findings or say"):
            review_findings("## Findings\n\n")
        for listing in (
            "REV-1 [blocking] Finding\nREV-1 [blocking] Finding",
            "REV-1 [blocking]   ",
            "Not a finding",
        ):
            with self.subTest(listing=listing), self.assertRaisesRegex(
                AgentSquadError, "invalid or duplicate"
            ):
                review_findings("## Findings\n\n" + listing)


class DerivedStateTests(unittest.TestCase):
    def test_optional_disposition_gates_readiness_but_not_approval(
        self,
    ) -> None:
        for budget in (1, 3):
            with self.subTest(budget=budget):
                configuration = replace(config(), max_review_passes=budget)
                s = snapshot(
                    reviews=(review(10, findings="REV-1 [optional] Finding"),),
                    comments=(root(severity="optional"),),
                )
                state = derive_state(s, configuration=configuration)
                self.assertTrue(state["approval"]["approved"])
                self.assertEqual(state["next_action"], "address_findings")
                self.assertEqual(state["unaddressed_findings"], ["REV-1"])
                self.assertIn("REV-1", " ".join(state["reasons"]))
                # Forge resolution is not a disposition.
                self.assertTrue(state["findings"][0]["resolved"])
                self.assertIsNone(
                    state["optional_findings"][0]["disposition"]
                )
                body = "DISPOSITION rejected\n\nNot pursued: outside scope."
                s = replace(s, comments=(*s.comments, reply(12, body)))
                state = derive_state(s, configuration=configuration)
                self.assertEqual(state["next_action"], "approved")
                self.assertFalse(state["gates"]["unaddressed_findings"])
                optional = state["optional_findings"][0]
                self.assertEqual(optional["title"], "Finding")
                self.assertEqual(optional["disposition"]["body"], body)
                for verification, action, settled in (
                    ("VERIFIED rejection accepted", "approved", True),
                    ("NOT FIXED", "address_findings", False),
                ):
                    verified = replace(s, comments=(
                        *s.comments, reply(13, verification, author="reviewer")
                    ))
                    state = derive_state(verified, configuration=configuration)
                    self.assertEqual(state["next_action"], action)
                    self.assertEqual(state["findings"][0]["settled"], settled)
                    self.assertTrue(state["approval"]["approved"])

    def test_optional_needs_human_requires_a_finding_decision(self) -> None:
        s = snapshot(
            reviews=(review(10, "needs_human",
                            findings="REV-1 [optional] Finding"),),
            comments=(root(severity="optional"),
                      reply(12, "DISPOSITION needs-human")),
            conversation=(decision(13),),
        )
        self.assertTrue(derive_state(s)["gates"]["needs_decision"])
        s = replace(s, conversation=(*s.conversation,
                                     decision(14, finding="REV-1")))
        self.assertFalse(derive_state(s)["gates"]["needs_decision"])

    def test_review_verdict_gates_are_rederived_from_untrusted_forge(
        self,
    ) -> None:
        opened = review(
            10, "changes_requested", findings="REV-1 [blocking] Finding"
        )
        cases = [
            (
                (review(10, findings="REV-1 [blocking] Finding"),),
                (),
                "unsettled blocking",
            ),
            ((opened, review(12)), (root(),), "unsettled blocking"),
            ((review(10, "changes_requested"),), (), "no blocking finding"),
            (
                (
                    review(
                        10,
                        "changes_requested",
                        findings="REV-1 [optional] Finding",
                    ),
                ),
                (),
                "no blocking finding",
            ),
            (
                (
                    opened,
                    review(
                        12,
                        "changes_requested",
                        findings="REV-1 [blocking] Finding",
                    ),
                ),
                (root(),),
                "listed by more than one",
            ),
        ]
        for reviews, comments, detail in cases:
            with self.subTest(detail=detail, reviews=reviews):
                state = derive_state(
                    snapshot(reviews=reviews, comments=comments)
                )
                self.assertEqual(state["budget"]["used"], len(reviews) - 1)
                self.assertFalse(state["approval"]["approved"])
                self.assertTrue(
                    any(
                        d["kind"] == "malformed_review"
                        and detail in d["detail"]
                        for d in state["diagnostics"]
                    )
                )

    def test_changes_requested_needs_verification_during_this_pass(
        self,
    ) -> None:
        opened = review(
            10, "changes_requested", findings="REV-1 [blocking] Finding"
        )
        verify = reply(12, "NOT FIXED", author="reviewer")
        s = snapshot(
            reviews=(opened, review(13, "changes_requested")),
            comments=(root(), verify),
        )
        self.assertEqual(derive_state(s)["budget"]["used"], 2)
        for ident in (9, 14):
            with self.subTest(verification_time=ident):
                state = derive_state(
                    replace(
                        s,
                        comments=(
                            root(),
                            reply(ident, "NOT FIXED", author="reviewer"),
                        ),
                    )
                )
                self.assertEqual(state["budget"]["used"], 1)
                self.assertEqual(
                    state["diagnostics"][0]["kind"], "malformed_review"
                )

    def test_review_target_and_submission_validity(self) -> None:
        good = review(10)
        cases = [
            replace(good, state=ReviewState.PENDING),
            replace(good, commit_id=J),
            replace(
                good,
                evidence=replace(
                    good.evidence,
                    body=good.evidence.body.replace("pr=1", "pr=2"),
                ),
            ),
            replace(
                good,
                evidence=replace(
                    good.evidence,
                    body=good.evidence.body.replace("base=" + A, "base=" + J),
                ),
            ),
            replace(
                good,
                evidence=replace(
                    good.evidence,
                    body=good.evidence.body.replace("base=" + A, "base=" + H),
                ),
            ),
        ]
        for invalid in cases:
            with self.subTest(review=invalid):
                state = derive_state(snapshot(reviews=(invalid,)))
                self.assertEqual(state["budget"]["used"], 0)
                self.assertEqual(
                    state["diagnostics"][0]["kind"], "malformed_review"
                )

    def test_duplicate_roots_and_implementer_recovery_text_are_not_authority(
        self,
    ):
        opening = root()
        reviewed = review(
            10,
            "changes_requested",
            findings="REV-1 [blocking] Finding",
            fallback=opening.evidence.body,
        )
        wrong_copy = replace(
            opening,
            evidence=replace(
                opening.evidence,
                author="dev",
                body=opening.evidence.body + " Edit.",
            ),
        )
        duplicate = replace(opening, evidence=replace(opening.evidence, id=12))
        wrong_title = replace(
            opening,
            evidence=replace(
                opening.evidence,
                body=opening.evidence.body.replace("Finding", "Other", 1),
            ),
        )
        for comments, diagnostic in [
            ((wrong_copy,), "unauthorized"),
            ((opening, duplicate), "duplicate_finding"),
            ((wrong_title,), "malformed_finding"),
        ]:
            with self.subTest(diagnostic=diagnostic):
                state = derive_state(
                    snapshot(reviews=(reviewed,), comments=comments)
                )
                self.assertEqual(state["next_action"], "open_threads")
                self.assertIsNone(state["findings"][0]["root"])
                self.assertIn(
                    diagnostic, [d["kind"] for d in state["diagnostics"]]
                )

    def test_specific_decision_cannot_amend_task_or_budget(self) -> None:
        invalid = decision(
            11,
            finding="REV-1",
            budget=5,
            body="Decided.\n\n## Task\n\nUnapproved objective.",
        )
        state = derive_state(snapshot(conversation=(invalid,)))
        self.assertEqual(state["decisions"], [])
        self.assertEqual(state["budget"]["effective"], 3)
        self.assertNotIn("Unapproved", state["effective_task"])
        self.assertEqual(state["diagnostics"][0]["kind"], "malformed")

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

    def test_decided_needs_human_review_can_repeat_without_a_commit(
        self,
    ) -> None:
        s = snapshot(
            reviews=(review(10, "needs_human"),),
            conversation=(decision(11),),
        )
        state = derive_state(s)
        self.assertFalse(state["gates"]["same_head_requires_rejections"])
        self.assertEqual(state["next_action"], "launch_review")

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
            derive_state(replace(
                s, reviews=(review(10, state=ReviewState.COMMENTED),),
            )),
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


class SingleIdentityTests(unittest.TestCase):
    def configuration(self):
        c = config()
        return replace(
            c, identity_mode='single', approver_accounts=('human', 'other'),
            reviewer=replace(c.reviewer, forge_account='dev'),
        )

    def approval(self, ident=20, *, state=ReviewState.APPROVED,
                 head=H, login='human', dismissed=False):
        from agent_squad.forge import Approval
        return Approval(login, state, head, dismissed,
                        evidence(ident, '').created_at, ident)

    def state(self, approvals=(), *, reviews=None, configuration=None,
              **snapshot_args):
        if reviews is None:
            reviews = (review(10, author='dev', state=ReviewState.COMMENTED),)
        s = replace(snapshot(reviews=reviews, **snapshot_args),
                    human_approvals=tuple(approvals))
        return derive_state(
            s, configuration=configuration or self.configuration(),
        )

    def test_missing_old_dismissed_and_unconfigured_approvals(self) -> None:
        for approvals in ((), (self.approval(head=A),),
                          (self.approval(dismissed=True),),
                          (self.approval(login='stranger'),)):
            with self.subTest(approvals=approvals):
                state = self.state(approvals)
                self.assertFalse(state['approval']['approved'])
                self.assertEqual(state['next_action'], 'await_human_approval')
                self.assertIn(H, ' '.join(state['reasons']))
                self.assertIn('human', ' '.join(state['reasons']))
                self.assertEqual(state['budget']['used'], 1)
        self.assertEqual(self.state((self.approval(login='stranger'),))[
            'human_approvals'], [])

    def test_either_arrival_order_and_casefolded_login(self) -> None:
        for ident in (5, 20):
            state = self.state((self.approval(ident, login='HUMAN'),))
            self.assertTrue(state['approval']['approved'])
            self.assertEqual(state['next_action'], 'approved')

    def test_latest_selected_before_dismissal_or_head_filter(self) -> None:
        for newest in (self.approval(21, dismissed=True),
                       self.approval(21, head=A),
                       self.approval(21, state=ReviewState.CHANGES_REQUESTED)):
            state = self.state((newest, self.approval()))
            self.assertFalse(state['approval']['approved'])
        requests = self.approval(21, head=A, dismissed=True,
                                 state=ReviewState.CHANGES_REQUESTED)
        state = self.state((self.approval(), requests,
                            self.approval(22, login='other')))
        self.assertEqual(state['next_action'], 'await_human_approval')
        self.assertEqual(state['human_request_changes'][0]['id'], 21)
        self.assertIn(A, ' '.join(state['reasons']))
        state = self.state((requests, self.approval(22)))
        self.assertTrue(state['approval']['approved'])
        self.assertEqual(state['human_request_changes'], [])

    def test_timestamp_offsets_and_id_ties_order_latest(self) -> None:
        old = replace(self.approval(), timestamp='2026-01-01T01:00:00+01:00')
        newer = replace(self.approval(21, state=ReviewState.CHANGES_REQUESTED),
                        timestamp='2026-01-01T00:00:00Z')
        self.assertFalse(self.state((newer, old))['approval']['approved'])
        newest = replace(self.approval(1), timestamp='2026-01-01T00:00:01Z')
        state = self.state((newer, newest, old))
        self.assertTrue(state['approval']['approved'])

    def test_mirror_rule_for_every_verdict_and_dismissed_agent(self) -> None:
        for state in (ReviewState.APPROVED, ReviewState.CHANGES_REQUESTED,
                      ReviewState.PENDING):
            result = self.state((self.approval(),), reviews=(review(
                10, author='dev', state=state),))
            self.assertFalse(result['approval']['approved'])
            self.assertIn('forge_state_mismatch',
                          [d['kind'] for d in result['diagnostics']])
        agent = replace(review(10, author='dev', state=ReviewState.COMMENTED),
                        dismissed=True)
        self.assertFalse(self.state((self.approval(),), reviews=(agent,))[
            'approval']['approved'])
        dual = derive_state(snapshot(
            reviews=(review(10, state=ReviewState.COMMENTED),),
        ))
        self.assertFalse(dual['approval']['approved'])
        self.assertNotIn('human_approvals', dual)
        self.assertEqual(dual['approval']['reasons'],
                         ['forge review state is not approved'])

    def test_wait_precedes_budget_and_merge_but_not_stop_or_decision(
        self,
    ) -> None:
        c = replace(self.configuration(), max_review_passes=1)
        self.assertEqual(self.state(configuration=c)['next_action'],
                         'await_human_approval')
        instruction = decision(12, body=MERGE_INSTRUCTION)
        self.assertEqual(
            self.state(conversation=(instruction,))['next_action'],
            'await_human_approval',
        )
        state = self.state((self.approval(),), conversation=(instruction,))
        self.assertEqual(state['next_action'], 'merge')
        stop = evidence(25, render_line('stop', head=H, reason='scope'), 'dev')
        self.assertEqual(self.state(conversation=(stop,))
                         ['next_action'], 'stopped')
        amended = decision(
            25, body='Approved amendment\n\n## Task\n\nNew Task')
        self.assertEqual(self.state(conversation=(amended,))['next_action'],
                         'launch_review')

    def test_optional_disposition_precedes_wait(self) -> None:
        agent = review(10, author='dev', state=ReviewState.COMMENTED,
                       findings='REV-1 [optional] Finding')
        comment = root(author='dev', severity='optional')
        state = self.state(reviews=(agent,), comments=(comment,))
        self.assertEqual(state['next_action'], 'address_findings')
        state = self.state(reviews=(agent,), comments=(comment, reply(
            12, 'DISPOSITION needs-human\nDeveloper must decide.')))
        self.assertEqual(state['next_action'], 'needs_decision')

    def test_agent_worktree_head_and_human_reviews_are_independent(
        self,
    ) -> None:
        agent = review(10, author='dev', state=ReviewState.COMMENTED)
        s = replace(snapshot(reviews=(agent,)),
                    human_approvals=(self.approval(),))
        for kwargs in ({'local_head': J}, {'missing_worktree': True}):
            state = derive_state(
                s, configuration=self.configuration(), **kwargs)
            self.assertFalse(state['approval']['approved'])
            self.assertNotEqual(state['next_action'], 'await_human_approval')
        state = self.state((self.approval(),), reviews=(
            review(10, author='dev', state=ReviewState.COMMENTED),
            review(20, author='human'),))
        self.assertEqual(state['budget']['used'], 1)
        self.assertEqual(state['findings'], [])
