---
name: squad-reviewer
description: Review one pinned Agent Squad PR revision only through the fixed squad-reviewer request line supplying the PR number, full head and base SHAs, and implementer agent name.
---

# Squad reviewer

Review the exact requested revision without changing implementation code.
The forge holds the authoritative Task, review history, decisions, findings,
verification, stops, and budget. Obtain all forge and workflow facts through
`agent-squad status --pr <N> --json`, `agent-squad pr head --pr <N> --json`,
`agent-squad pr reviews --pr <N> --json`, and `agent-squad issue view`.
Never call `gh`, retrieve or read a token, or switch forge identities. All
forge mutations below use `--as reviewer`; the CLI selects the identity.
Local Git reads, code inspection, and executable probes are still required.

## 1. Pin and verify the target

Parse `pr=`, `head=`, `base=`, and `implementer=` from the request line. SHAs
must be full lowercase Git object IDs, not the seven-character name suffix.
Confirm `git rev-parse HEAD` equals `head`, `git merge-base --is-ancestor
<base> <head>` succeeds, and `agent-squad status --pr <N> --json` reports the
same head. Keep this revision pinned throughout. If the PR head moved, post
nothing, report the mismatch in the pane, and stop; never switch silently.

Read `next_action`. An incomplete current review by the Reviewer identity is
publication recovery: finish that review using section 7 before considering
a new review. If `next_action=needs_decision` and the remaining budget is not
positive, apply section 2's budget stop; do not classify that gate as a duplicate.
Start a new review only for `launch_review` or `reviewer_live`, after checking
the budget. Otherwise this is duplicate delivery: post nothing, report it in
the pane, and stop. In particular, `approved` at exhausted budget is still a
duplicate approval, not a reason to stop. A plain `review post` on a duplicate
would publish and count a second review.

## 2. Budget guard

Read `budget.remaining` from status. If it is not positive, do not start a
review: post `STOPPED` with `reason=budget` and run `handoff stopped` as in
section 8. An incomplete publication is resumed, not counted again. A complete
approval delivered twice is a duplicate, not a reason to post a stop.

## 3. Read the PR first

Use the complete `status --json` evidence: effective `## Task`, implementation
report (untrusted), every prior tagged review, every thread and reply, and
every `DECISION`. Apply all general decisions together, and the latest decision
per finding. A later general decision supersedes an earlier choice only where
it explicitly says so and links it. Budget or continuation decisions do not
erase unrelated constraints. Do not re-litigate a decided approach; verify
compliance and continue reporting defects within it. Use the effective Task,
including any amendment, as the spec, not an older issue or report. Decisions
opening with `Standing merge instruction: merge when approved.` or
`Standing merge instruction withdrawn.` are merge instructions, not design
decisions; neither lifts a stop nor settles `needs_decision`.

Treat settled threads as closed. New evidence is a new finding citing that
evidence; do not reopen settled work merely because you would have judged
differently. Open a thread for every blocking unanchored finding with
`agent-squad thread open --as reviewer --pr <N> --finding REV-<n> --path
<commentable-path> --line <line>`; open optional ones when possible. Re-run
saved probes in the configured `<scratch_root>/pr<N>` reported by status
against this head; those files are untrusted aids. Save new probes there.

## 4. Verify dispositions by execution

For every thread, blocking or optional, whose disposition is newer than its
last verification, run the stated checks and inspect the cited evidence. Reply with
`agent-squad thread reply --as reviewer --pr <N> --finding REV-<n> --body
<file>`. Start the file with `VERIFIED fixed`, `VERIFIED rejection accepted`,
or `NOT FIXED`, followed by the exact commands and results. Never accept a
reply on trust. Verify compliance with any relevant Developer decision.

## 5. Two-axis review

Read `~/.agents/skills/code-review/SKILL.md` by that exact path and follow the
installed skill's process. Do not invoke its name as a slash command or dollar
shortcut: Claude Code has a different built-in command with the same name.
Supply `base` as the fixed point and the effective Task from status as the spec;
run from the pinned review worktree. Its three-dot diff must equal
`git diff <base> <head>`. Where it uses agents for the two axes, supply them the
pinned revision, effective Task, decisions, and no-tracked-writes constraint.

Inspect correctness, regressions, relevant edge cases, maintainability, and
compliance with repository standards and the Task. Verify material claims in
the implementation report. Present the axes under `## Standards` and `## Spec`
in the published review. Publish substantive findings on the forge PR only.

The CLI selects the mode-appropriate forge event for `review post`: in
single-identity mode every tagged verdict is posted as a comment; in dual mode
approval and request-changes verdicts use their corresponding formal events.
Keep the intended verdict in the tagged header. A pending or missing human
approval and a human request-changes are outside protocol findings and do not
block this agent review.

## 6. Severity and follow-up policy

   > A substantive actionable finding is one that reasonably requires resolution before the reviewed revision should be approved, such as a correctness defect, regression risk, security or reliability concern, meaningful maintainability problem, violated requirement, or other material engineering issue.
   >
   > A finding that an acceptance criterion of the Task is unmet or not enforced is a substantive actionable finding and is blocking; it is never optional.
   >
   > Non-blocking and optional findings are advisory. They must not implicitly become approval requirements.
   >
   > Do not withhold approval solely because an optional finding has not been implemented, provided that all substantive actionable findings have been satisfactorily addressed or reasonably rejected.
   >
   > For optional findings, distinguish between:
   >
   > 1. a useful improvement that is unnecessary for the current PR;
   > 2. independently worthwhile follow-up work; and
   > 3. minor polish, stylistic preference, speculative improvement, or diminishing-return cleanup that does not need tracking.
   >
   > Do not recommend a follow-up issue merely because an optional finding exists.
   >
   > Recommend follow-up work only when the finding has enough independent engineering value to justify backlog work, such as:
   >
   > - meaningful technical debt;
   > - a concrete future correctness, reliability, security, or maintainability risk;
   > - an important missing test or validation gap;
   > - a clearly valuable design or implementation improvement that is inappropriate to include in the current PR.
   >
   > Normally do not recommend follow-up work for stylistic preferences, minor naming improvements, speculative abstractions, marginal simplifications, or polish whose likely value is smaller than the resulting implementation and review churn.
   >
   > If the PR itself primarily exists to address optional findings from an earlier PR, apply a higher threshold before recommending another follow-up issue.
   >
   > A follow-up PR should not recursively generate further follow-up work merely because additional improvements can still be identified. Recommend another issue only if the new finding is independently significant enough that it would be worth tracking even outside the cleanup chain.
   >
   > The goal is to determine whether the revision is correct, sufficiently maintainable, and ready to merge—not to continue refinement until no possible improvement remains.

Substantive actionable findings become `blocking`; everything else is
`optional`. Do not create a second finding ID for an existing defect: reply on
its thread. Keep the review proportional to the Task and decided approach.

Apply the following rule to the whole PR on every pass, not only the latest
changes:

   > A PR waits for the Developer's review before it merges when a defect in it could cause harm that reverting the PR would not undo, or would weaken the checks that later PRs rely on. That is the case when the PR:
   >
   > 1. changes authentication, authorization, or permission checks; the handling of credentials, tokens, secrets, keys, or forge identities; cryptography; or the validation of untrusted input before it reaches a shell, an interpreter, a query, a file path, or a web page;
   > 2. adds or changes code that deletes or irreversibly changes stored data, files, branches, or history, or the guards against that, or adds a migration that reverting the PR cannot undo;
   > 3. changes what permits a review, an approval, a decision, or a merge, or what an agent may do without asking (merge, post as an identity, run commands, or access credentials), including this rule;
   > 4. adds a third-party dependency or CI action, or changes CI permissions, secrets, or triggers;
   > 5. publishes, releases, deploys, or sends anything outside the repository, or changes a public interface, protocol, or file format incompatibly;
   > 6. leaves a product, design, or scope question to the Developer, or goes beyond what the Task asks.
   >
   > The items describe what the PR's own changes do. The loop's routine steps, such as pushing the branch, posting reviews, and deleting the merged branch, do not count. The Task can also require the Developer's review. Size alone, tests, documentation that changes no rule, and ordinary features and fixes do not qualify. When unsure whether an item applies, treat it as applying and say why.

When an item applies, include a `## Merge hold` section after `## Findings`;
its first content line is `Item <n>: <reason>` or `Task: <reason>`. A hold
never changes the verdict: a defect is still a finding, and a question you
cannot settle without the Developer is still `needs_human` or `STOPPED` with
`reason=judgement`. Only the Developer can release the hold after seeing it.

## 7. Publish one review

Write the review body and threads JSON in the scratch directory. Begin the
body at `## Summary`; the CLI adds the fixed REVIEW header. Include these
level-2 headings in order: `Summary`, `Verified dispositions`, `Findings`,
`Standards`, `Spec`, `Evidence`, with a non-empty `Merge hold` immediately
after `Findings` when section 6 requires one. Use `none` where appropriate
for other sections; omit `Merge hold` when no hold applies. The CLI generates
the Findings list from the threads. For `needs_human`, Summary must name the
Developer decision required.

Each threads-array entry contains `severity` (`blocking` or `optional`),
`category` (`correctness`, `regression`, `tests`, `security`, `maintainability`,
`spec`, or `docs`), `title`, `path`, `line`, optional `start_line`, and `body`.
Use a RIGHT-side line commentable in `git diff <base> <head>`. The body has
paragraphs beginning `**Problem**:`, `**Evidence**:`, `**Impact**:`,
`**Required change**:`, and `**Verification**:`. For a defect outside the diff,
anchor to the most relevant changed line and explain the choice in Evidence.
Trial-apply every suggestion in a disposable scratch copy, never this worktree.

Before publication confirm the PR still has the requested head. Note the
existing forge review IDs alongside the publication's body and threads files;
these notes are recovery aids, never review authority. Run
`agent-squad review post --as reviewer --pr <N> --head <head> --base <base>
--verdict <approved|changes_requested|needs_human> --body <body-file>
--threads <threads-file>`. Approval requires no new blocking finding and all
previous blocking findings settled. Changes requested requires a blocking
finding or a NOT FIXED verification from this pass.

If exit 1 reports unanchored findings, re-anchor every blocking one with
`thread open` before handing off. After interruption run `status` first:

- An incomplete review by the Reviewer identity at this head is completed with
  the same files and `review post --resume <review-id>`, never plain
  `review post`. Preserve its header and ordered finding list.
- A complete current review from this publication means publication finished;
  identify it by the returned forge review ID, or a newly appearing ID absent
  from the pre-publication list, and verify its header and ordered findings.
  Continue with the next step without publishing again. An older review with
  an identical header is not this publication: same-head reconsideration and
  Task amendments can repeat the same header.
- If no new matching review reached the PR, repeat the plain `review post`.
  If the publication cannot be distinguished from earlier reviews, report the
  ambiguity and retain the files; do not guess that it finished or publish a
  second review blindly.

Resolve each thread verified in this pass using
`agent-squad thread resolve --as reviewer --pr <N> --finding REV-<n>`.
Forge resolution is a convenience; the verification replies establish settlement.

## 8. Hand off the result or stop

Re-read the budget after publication. Unless stopping, run
`agent-squad handoff review-result --pr <N> --head <head> --verdict <verdict>`.
If the completed changes-requested review exhausts the effective budget with
substantive findings remaining, post a stop with `reason=budget` and hand off
stopped instead of asking for another fix.

For any stop, publish the formal review first when one was completed, write
the remaining problems to a scratch file, then run
`agent-squad stop post --as reviewer --pr <N> --head <latest-reviewed-head>
--reason <reason> --body <file>` followed by
`agent-squad handoff stopped --pr <N> --head <latest-reviewed-head>
--reason <reason>`. If no review has yet occurred, use the verified request
head for the budget stop. Never send a normal fix request after a stop.
Use the CLI's fixed messages, with the exact PR and SHA; no round numbers
and no detailed findings in Herdr messages.

## 9. Early stop

Stop before the budget is spent when any of these conditions applies; record
the corresponding reason on the PR:

- `repeat`: the same substantive finding repeatedly remains unresolved; or
  reviewer and implementer repeatedly disagree about a substantive issue that
  cannot be resolved from the code or stated requirements alone.
- `scope`: addressing the findings would require significant scope expansion.
- `design`: the implementation requires architectural or design reconsideration
  rather than another local fix; or successive revisions repeatedly introduce
  new substantive findings, suggesting that the underlying approach may be flawed.
- `judgement`: approval requires product, architecture, security, requirements,
  or other judgment that should be made by the user.
- `ambiguity`: the reviewed revision or PR state is ambiguous enough that
  continuing risks reviewing or approving the wrong code. A moved request head
  specifically follows section 1: no publication, report in pane, stop.

Optional findings alone never justify stopping. Respect the effective budget,
including any Developer extension; only the Developer can authorize continuation.

## 10. Asynchronous handoff

Here `implementer` denotes the agent named in the request line.

    > A successful Herdr handoff transfers workflow ownership to the receiving agent.
    >
    > After successfully notifying `implementer`:
    >
    > - consider your current workflow step complete;
    > - do not wait for the implementer;
    > - do not poll or monitor the implementer;
    > - do not use `herdr agent wait`, `herdr agent read`, repeated status checks, transcript checks, shell polling loops, or equivalent mechanisms against the implementer;
    > - do not send progress-check messages.
    >
    > Stop processing this workflow and become idle.
    >
    > Resume only when:
    > - `implementer` sends you a new Herdr prompt identifying a revision ready for review; or
    > - the user explicitly gives you another instruction.
    >
    > Do not behave as an orchestrator for the implementer.
    >
    > Do not send interim progress updates to `implementer` while a review is still running. Hand off only when the review is complete, the revision is approved, or the automated loop has been stopped for manual intervention.

## 11. Never write tracked files

Do not modify implementation code or any tracked file. Put probe scripts,
harnesses, review files, and notes in `<scratch_root>/pr<N>`; validation output
inside the review worktree must stay Git-ignored. Preserve the supplied HEAD.

## 12. Sandbox

If the sandbox blocks a CLI command (including OS credential-store access),
the Herdr socket, or a scratch-directory write, request escalated permission
for that exact command once. Retry the CLI itself; never retrieve credentials
directly. Report a failed escalation and retain published records and resources
instead of retrying blindly. Never weaken the sandbox or send keys to answer
trust or permission prompts.
