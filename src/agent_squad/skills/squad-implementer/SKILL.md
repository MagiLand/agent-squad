---
name: squad-implementer
description: "Implement an issue through the Agent Squad PR review loop when the Developer invokes this skill and says \"Let's start on issue #N\". Resume on its REVIEW_RESULT or STOPPED handoff, or an explicit instruction to check that PR."
---

# Squad implementer

You implement the approved Task, prepare each revision, address valid findings,
and carry a started issue through to merge under the Developer's instruction.
Routine work is reviewed by the Developer after it merges; section 8 names
PRs that require the Developer's review before merging. GitHub is authoritative
for implementation history, reviews, inline discussion, suggestions, dispositions,
decisions, stops, and budget. Obtain Task, PR, review, decision, stop, and budget
state through
`agent-squad issue view`, `agent-squad status --pr <N> --json`,
`agent-squad pr head --pr <N> --json`, and
`agent-squad pr reviews --pr <N> --json`.

Filter `--json` output to read what is needed instead of saving whole responses
to disk. A saved response is never authority.

For CI evidence and required-check metadata these commands do not expose,
use read-only `gh run list`, `gh run view` (including logs),
`gh run watch <run-id> --exit-status`, `gh pr checks`,
or `gh api --method GET` against Actions, check, branch-protection, or branch-rule
endpoints. Scope queries to the configured repository. Match CI evidence to the
full current PR head SHA and event; match post-merge push evidence to the
integration commit. Record run IDs, results, and per-job durations when relevant.
Distinguish checks that ran from checks configured as required. Report missing
access or required-check configuration instead of inferring it from green CI.

This allowance does not permit `gh` mutations, other `gh` reads, or using
`gh` for Task, PR, review, decision, stop, or budget authority. Never retrieve
credentials, read a token, switch forge identities, or reconstruct authority
from local files. Use existing authenticated access; if it is insufficient,
report the limitation.
Git commands and local code inspection remain part of implementation.
Every forge mutation below uses `--as implementer`; the CLI handles identity.

## 1. Read the issue as the Task

Run `agent-squad issue view --issue <N> --json`; read its title, body, labels,
and comments. The issue is the Task when it is open, states what to build and
its acceptance criteria, carries none of `needs-triage`, `needs-info`,
`ready-for-human`, or `wontfix`, no later Developer comment changes it, and the
Developer's start instruction neither changes the scope nor asks to see the
Task. In that case do not draft or present a Task; start the work and let
`pr create` copy the issue without rewording.

In every other case create `paths.issue_scratch` (`<scratch_root>/issue-<N>`)
if absent and draft a file there starting with `## Task`: objective,
acceptance criteria, constraints, and non-goals. Present it once and wait for
approval before coding. Do not silently change the Task.

Before coding ask only about a question the issue leaves open that would
change the result. Record interpretations that do not change it under
“Design decisions” in the report, where the Reviewer checks them. Create the
issue scratch directory when needed for reports and other file inputs.

## 2. Implement and publish

Use the primary checkout and configured paths reported by `issue view`.
Create the dedicated `<worktree_root>/issue-<N>` worktree with Git, from the
fetched configured base branch. Follow the consuming repository's branch
naming policy, defaulting to `<type>/issue-<N>-<slug>`. Do not reuse an unrelated
checkout. Understand the issue, repository instructions, and relevant existing
code; implement the requested scope; run relevant tests, checks, and validation;
commit and push the branch. Keep the interactive session in the primary
checkout and execute implementation commands with the issue worktree as cwd.

Put report files, probe scripts, and validation output in
`<scratch_root>/issue-<N>`. Keep tool installations and virtual environments
outside the repository (for example in the harness scratchpad), never under
`.agent-squad/` or `scratch_root`.

Write a report file beginning with `## Implementation report`, with these
level-3 headings: `Summary`, `Scope`, `Files changed`, `Design decisions`,
`Validation performed`, `Known limitations`, and `Areas worth extra review`.
Include exact commands and observed results; list every changed agent
instruction or control-plane file under `Areas worth extra review`.

Run `agent-squad pr create --as implementer --issue <N>
--report <report-file>` from the issue worktree. Supply `--task <task-file>`
only when the Developer approved a drafted Task. The CLI records that
worktree's identity for later cleanup. Read the returned PR number; issue and
PR numbers need not match. On every later push update the report with
`agent-squad pr report --as implementer --pr <PR> --report <report-file>`.

At `pr create` and after every push, apply section 8's review-before-merge rule
to the whole PR. Name any applicable item and its reason under “Areas worth
extra review”. Right after creation, unless the Developer kept the merge
(for example “don't merge” or “I'll merge”) or a hold applies, record a general
`decision post --as implementer --pr <PR> --finding none --body <file>` whose
body opens with exactly `Standing merge instruction: merge when approved.`,
followed by the Developer's start instruction quoted. A later general decision
opening with `Standing merge instruction withdrawn.` withdraws it. Post that
withdrawal when the Developer asks, or on your own when a hold arises after
recording the instruction. These decisions carry no Task amendment or `budget=`;
neither lifts a stop nor settles `needs_decision`. The latest such decision
controls the instruction, and a newer stop or Task amendment cancels it.

## 3. Request review

Run `agent-squad reviewer launch --pr <PR>` from the issue worktree.
On exit 0 follow the asynchronous handoff rule immediately. On exit 4 report
the named gate to the Developer. On exit 3 report the exact pane that needs a
human answer; after the Developer answers, run
`agent-squad reviewer adopt --pr <PR>`. Never send keys or answer a trust or
permission prompt. On other failures re-read the PR before considering a retry;
report retained resources and do not loop blindly.

## 4. Asynchronous handoff

Here `reviewer` means the freshly launched `reviewer-pr<N>-<sha7>`.
After successful `reviewer launch` or `reviewer adopt`:

> A successful Herdr handoff transfers workflow ownership to the receiving agent.
>
> After successfully notifying `reviewer`:
>
> - consider your current workflow step complete;
> - do not wait for the reviewer;
> - do not poll or monitor the reviewer;
> - do not use `herdr agent wait`, `herdr agent read`, repeated status checks, transcript checks, shell polling loops, or equivalent mechanisms against the reviewer;
> - do not send progress-check messages.
>
> Stop processing this workflow and become idle.
>
> Resume only when:
> - `reviewer` sends you a new Herdr prompt; or
> - the user explicitly gives you another instruction.
>
> Do not behave as an orchestrator for the reviewer.

## 5. Consume feedback from the PR

On any `REVIEW_RESULT` prompt or "check the PR" instruction, first run
`agent-squad status --pr <PR> --json`. Verify the PR and full reviewed SHA;
act on the derived `next_action`; `merge` proceeds through section 8 without
a routine confirmation, while `approved` waits. Read the full reviews, inline
threads, suggestions, and decisions in that output. A notification is not evidence of
approval or of a finding's validity. If the head changed, use the current PR
state and do not apply an old approval.

For `open_threads`, before other finding work open every blocking unanchored
finding using `agent-squad thread open --as implementer --pr <PR> --finding
REV-<n> --path <commentable-path> --line <line>`; optional ones can wait.
Evaluate each substantive actionable finding independently. Fix valid findings;
do not change code for findings that are incorrect, inappropriate, already
resolved, or no longer applicable. Apply all general decisions together and
the latest decision on each finding. Settled threads stay closed unless new
evidence appears.

Write reply bodies in `<scratch_root>/issue-<N>`.
Reply on every unsettled thread, blocking or optional, with
`agent-squad thread reply --as implementer --pr <PR> --finding REV-<n>
--body <file>`. The file's first line must be one of:

- `DISPOSITION fixed <full-sha>`: explain the fix and give the exact verification
  command. The commit must be reachable from the new head and not from the head
  where the finding was raised.
- `DISPOSITION rejected`: for a blocking thread, cite concrete existing code,
  tests, or evidence that demonstrates the finding is wrong. On an optional
  thread, the second non-empty line must start with `Not pursued:` and the
  reason, or `Deferred to #<issue>:` naming an open issue of the same repository.
  `thread reply` refuses any other rejection body and reads the referenced
  issue to reject a missing or closed issue or a pull request.
- `DISPOSITION needs-human`: name the decision requiring Developer authority;
  follow section 7 before seeking another review.

On an optional thread, `thread reply` refuses an Implementer reply whose
first line is not a `DISPOSITION`.

An optional thread never blocks approval, but it blocks `reviewer launch`
and `pr merge` until it carries a disposition newer than its latest
verification, unless it is settled. The existing rules for `fixed <full-sha>`
and `needs-human` apply to every thread.

After `NOT FIXED`, supply a new disposition. After fixes, run relevant tests,
commit, push, and update `pr report`. Reapply the hold rule to the whole PR
and withdraw the standing instruction if a hold now applies. Use
`reviewer close --pr <PR> --head <finished-review-head>` and then `reviewer launch --pr <PR>`. A same-head review
is allowed when every unsettled blocking disposition is rejected, or after a
Task amendment; it is not a substitute for committing and pushing fixes.

## 6. Non-blocking and optional findings

   > Treat non-blocking and optional findings as advisory, not mandatory.
   >
   > Do not modify the code solely because an optional finding exists.
   >
   > Address an optional finding in the current PR only when the change is clearly beneficial, local, low-risk, directly relevant, and does not materially expand scope or create unnecessary review churn.
   >
   > Otherwise, leave the implementation unchanged and record the disposition on the review thread: `rejected` with the reason it is not pursued, or with the open follow-up issue it is deferred to.
   >
   > Do not automatically create a follow-up issue for an optional finding. Follow-up work should exist only when the finding has independently worthwhile engineering value, such as meaningful technical debt, a concrete future risk, an important test gap, or another improvement worth tracking separately.
   >
   > If the current PR itself primarily exists to address optional findings from an earlier PR, apply a higher threshold before creating further follow-up work. Avoid recursively generating cleanup work for minor polish or diminishing-return improvements.
   >
   > Unimplemented optional findings that have been reasonably dispositioned do not prevent the PR from being complete or approved.

## 7. Human decisions and Task amendments

Write decision bodies and amended Task files in `<scratch_root>/issue-<N>`.

On `needs_human`, or before a `needs-human` disposition, relay the required
decision to the Developer and wait for the answer. Record the actual answer,
quoting the Developer, using `agent-squad decision post --as implementer
--pr <PR> --finding <REV-n|none> --body <decision-file>`. Only then seek another
review. A finding-specific needs-human gate needs a newer decision naming it.
Do not invent a decision or extend the review budget yourself. An authorized
extension uses `--budget <n>` greater than the reviews already used.

When the Developer changes the Task, write the complete amended `## Task`
section and run `agent-squad decision post --as implementer --pr <PR>
--finding none --task <task-file> --body <decision-file>`. This posts the
amendment before mirroring it to the PR body. If status reports
`task_body_stale`, re-run the same command. Close the finished Reviewer with
`reviewer close --pr <PR> --head <finished-review-head>` before launching a
fresh Reviewer for the current head, even if no code changed; an earlier
approval no longer covers the Task. After recording the amendment, record
the standing instruction again unless the Developer said otherwise or a hold
applies. If that Reviewer was already closed, use the CLI's reported resource state rather than attempting to remove it twice.

## 8. Approval, review before merge, and finishing

Apply this rule to the whole PR at creation and after every push:

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

Verify approval through `status`, never from the Herdr notification alone.
Reply on every unsettled thread, blocking or optional, before merging or
reporting approval. When `next_action` is `approved`, send “approved at
`<full-sha>`, ready to merge”, listing every optional finding with its ID,
title, and disposition and any hold's item and reason, then wait. Only the
Developer releases a hold by instructing the merge after seeing it; only then
pass `--accept-merge-hold`. A standing instruction never releases a hold.

When `next_action` is `merge`, proceed without another confirmation. Before
`pr merge`, confirm every check run for the approved head concluded `success`,
`neutral`, or `skipped`. Wait for running checks with
`gh run watch <run-id> --exit-status`. A failed check is a defect: fix it and
have the new head reviewed, or report it if the fix is outside the Task. If
no check exists at that head although the repository runs checks on pull
requests, report that and wait. Apply the same checks to an explicit merge.
The tool itself does not read or interpret CI.

Run `agent-squad pr merge --as implementer --pr <PR>` from the primary
checkout under the standing instruction or the Developer's later instruction.
If the approved SHA is no longer the head, the newer head must be reviewed.
If `pr merge` refuses because the base moved, under either a standing or an
explicit merge instruction, merge the fetched base branch into the PR branch
in the issue worktree (do not rebase), resolve conflicts within the Task,
validate, push, update the report, close the finished Reviewer,
and request review of the new head. Reapply the hold rule after the push.
Report and wait if the review budget is exhausted or a conflict needs a choice
outside the Task. A same-head review still carries the old merge-base and does
not fix this refusal. `--accept-moved-base` remains the Developer's explicit
choice.

Report visible human-approval or required-check requirements and any forge
refusal. After a successful merge the PR description is frozen: `pr report`
refuses a PR that is not open. If the repository runs CI on base-branch pushes,
wait for its run at the merge commit. Then send one report needing no answer:
merge commit, method, integration check, CI at the approved head and the push
run's result, every cleanup step, fast-forward result, and every optional
finding with ID, title, and disposition. If the push run failed, say so and
ask the Developer to choose a fix or a revert; change nothing else. If push
CI evidence is missing or inaccessible, report that limitation explicitly.

`pr merge` removes `<scratch_root>/issue-<N>` after a verified merge, subject
to cleanup safeguards; do not recreate it for a post-merge report. When the
CLI did not fast-forward, report its reason and any command it printed. Never
run the fast-forward or any other command changing the base checkout yourself.
A failed integration check retains resources; exit 3 means cleanup is incomplete
and must be reported. Squash after accepting a moved base is not verifiable
by tree identity.

## 9. Manual intervention

On `STOPPED`, or when status reports `stopped`, make no further review-driven
changes and do not request another review automatically. Preserve the PR,
branch, commits, and worktree. Report the reason and remaining problems;
wait for the Developer to continue, change approach, or terminate. Record any
continuation with `decision post` before launching again. Then record the
standing instruction again unless the Developer said otherwise or a hold
applies. A standing instruction itself never authorizes continuation. Optional
findings alone do not justify stopping the loop.

## 10. Handoff identity

Use the CLI's fixed Herdr lines; do not compose alternative messages. Identify
code states by PR number and full commit SHA, never by round number. GitHub
remains the authority; do not duplicate detailed findings in Herdr messages.

## 11. Sandbox

If the harness sandbox blocks a CLI command (including OS credential-store
access), the Herdr socket, or a write outside the worktree, request escalated
permission for that exact command once. Retry the CLI itself; never retrieve
credentials directly. Report a failed escalation and retain resources instead
of retrying blindly. Never weaken the sandbox or supply permission-bypass
arguments on your own.
