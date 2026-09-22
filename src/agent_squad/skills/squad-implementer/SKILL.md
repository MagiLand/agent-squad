---
name: squad-implementer
description: "Implement an issue through the Agent Squad PR review loop when the Developer invokes this skill and says \"Let's start on issue #N\". Resume on its REVIEW_RESULT or STOPPED handoff, or an explicit instruction to check that PR."
---

# Squad implementer

You implement the approved Task, prepare each revision, address valid findings,
and merge only on the Developer's instruction. GitHub is authoritative for
implementation history, reviews, inline discussion, suggestions, dispositions,
decisions, stops, and budget. Obtain Task, PR, review, decision, stop, and budget
state through
`agent-squad issue view`, `agent-squad status --pr <N> --json`,
`agent-squad pr head --pr <N> --json`, and
`agent-squad pr reviews --pr <N> --json`.

For CI evidence and required-check metadata these commands do not expose,
use read-only `gh run list`, `gh run view` (including logs), `gh pr checks`,
or `gh api --method GET` against Actions, check, branch-protection, or branch-rule
endpoints. Scope queries to the configured repository. Match CI evidence to the
full current PR head SHA and event; match post-merge push evidence to the
integration commit. Record run IDs, results, and per-job durations when relevant.
Distinguish checks that ran from checks configured as required. Report missing
access or required-check configuration instead of inferring it from green CI.

This allowance does not permit `gh` mutations or using it for review, decision,
stop, or budget authority. Never retrieve credentials, read a token, switch forge
identities, or reconstruct authority from local files. Use existing authenticated
access; if it is insufficient, report the limitation.
Git commands and local code inspection remain part of implementation.
Every forge mutation below uses `--as implementer`; the CLI handles identity.

## 1. Agree the Task once

Run `agent-squad issue view --issue <N> --json`; read its title, body, labels,
and comments. Draft a file starting with `## Task` containing the objective,
acceptance criteria, constraints, and non-goals. Present it to the Developer
once and wait for approval before coding. Then work autonomously until the
first handback. Do not silently change the Task.

## 2. Implement and publish

Use the primary checkout and configured paths reported by `issue view`.
Create the dedicated `<worktree_root>/issue-<N>` worktree with Git, from the
fetched configured base branch. Follow the consuming repository's branch
naming policy, defaulting to `<type>/issue-<N>-<slug>`. Do not reuse an unrelated
checkout. Understand the issue, repository instructions, and relevant existing
code; implement the requested scope; run relevant tests, checks, and validation;
commit and push the branch. Keep the interactive session in the primary
checkout and execute implementation commands with the issue worktree as cwd.

Write a report file beginning with `## Implementation report`, with these
level-3 headings: `Summary`, `Scope`, `Files changed`, `Design decisions`,
`Validation performed`, `Known limitations`, and `Areas worth extra review`.
Include exact commands and observed results; list every changed agent
instruction or control-plane file under `Areas worth extra review`.

Run `agent-squad pr create --as implementer --issue <N> --task <task-file>
--report <report-file>` from the issue worktree. The CLI records that worktree's
identity for later cleanup. Read the returned PR number; issue and PR numbers
need not match. On every later push update the report with
`agent-squad pr report --as implementer --pr <PR> --report <report-file>`.

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
act on the derived `next_action`. Read the full reviews, inline threads,
suggestions, and decisions in that output. A notification is not evidence of
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

Reply on every unsettled blocking thread with
`agent-squad thread reply --as implementer --pr <PR> --finding REV-<n>
--body <file>`. The file's first line must be one of:

- `DISPOSITION fixed <full-sha>`: explain the fix and give the exact verification
  command. The commit must be reachable from the new head and not from the head
  where the finding was raised.
- `DISPOSITION rejected`: cite concrete existing code, tests, or evidence that
  demonstrates the finding is wrong.
- `DISPOSITION needs-human`: name the decision requiring Developer authority;
  follow section 7 before seeking another review.

After `NOT FIXED`, supply a new disposition. After fixes, run relevant tests,
commit, push, and update `pr report`. Use `reviewer close --pr <PR> --head
<finished-review-head>` and then `reviewer launch --pr <PR>`. A same-head review
is allowed when every unsettled blocking disposition is rejected, or after a
Task amendment; it is not a substitute for committing and pushing fixes.

## 6. Non-blocking and optional findings

   > Treat non-blocking and optional findings as advisory, not mandatory.
   >
   > Do not modify the code solely because an optional finding exists.
   >
   > Address an optional finding in the current PR only when the change is clearly beneficial, local, low-risk, directly relevant, and does not materially expand scope or create unnecessary review churn.
   >
   > Otherwise, leave the implementation unchanged and record an appropriate disposition on the GitHub review thread when useful.
   >
   > Do not automatically create a follow-up issue for an optional finding. Follow-up work should exist only when the finding has independently worthwhile engineering value, such as meaningful technical debt, a concrete future risk, an important test gap, or another improvement worth tracking separately.
   >
   > If the current PR itself primarily exists to address optional findings from an earlier PR, apply a higher threshold before creating further follow-up work. Avoid recursively generating cleanup work for minor polish or diminishing-return improvements.
   >
   > Unimplemented optional findings that have been reasonably dispositioned do not prevent the PR from being complete or approved.

## 7. Human decisions and Task amendments

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
approval no longer covers the Task. If that Reviewer was already closed, use
the CLI's reported resource state rather than attempting to remove it twice.

## 8. Approval and human-gated merge

Verify approval through `status`, never from the Herdr notification alone.
Report "approved at `<full-sha>`, ready to merge" and wait for the Developer's
merge instruction. Then run `agent-squad pr merge --as implementer --pr <PR>`
from the primary checkout. If the approved SHA is no longer the PR head, do
not merge on that approval; the newer head must be reviewed. If the base moved,
report it and ask the Developer to choose a fresh review or explicitly accept
the moved base; use `--accept-moved-base` only for that explicit choice.

Report visible human-approval or check requirements and any forge refusal.
After successful merge report the merge commit, method, integration check,
and each cleanup result. Print the CLI's suggested fast-forward command for
the Developer; do not change the base checkout. A failed integration check
retains resources; exit 3 means cleanup is incomplete and must be reported.
Squash after accepting a moved base is not verifiable by tree identity.

## 9. Manual intervention

On `STOPPED`, or when status reports `stopped`, make no further review-driven
changes and do not request another review automatically. Preserve the PR,
branch, commits, and worktree. Report the reason and remaining problems;
wait for the Developer to continue, change approach, or terminate. Record any
continuation with `decision post` before launching again. Optional findings
alone do not justify stopping the loop.

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
