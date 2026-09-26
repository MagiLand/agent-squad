# Agent Squad

Agent Squad coordinates an Implementer and an independent Reviewer through
Herdr. The GitHub pull request carries the Task, reviews, findings, dispositions,
Developer decisions, and approval. Each review identifies an exact commit and
runs in a fresh detached worktree. Starting an issue authorizes routine work through merge; higher-risk PRs wait
for the Developer's review before merging.

The implementation follows the [v0.6.0 specification delta](docs/agent-squad-v0.6.0-spec.md)
through Increment 2: GitHub supports dual and single identity; the Forgejo
adapter belongs to later increments.
See [workflow verification](docs/workflow-verification.md) for deterministic
coverage and the live-trial evidence required before releasing v0.5.0.

## Install and prepare a repository

Requirements: Python 3.11 or later, Git, GitHub CLI (`gh`), Herdr, Codex CLI,
Claude Code, and the installed `code-review` skill. Both agent integrations in
Herdr must be current. In the default dual-identity mode, authenticate `gh` as
two different GitHub accounts; the Reviewer account needs write permission on
the consuming repository. Single-identity mode uses one shared agent account
with push permission and a separate, person-operated approver account.
The Python package has no runtime dependencies outside the standard library.

Install from this checkout into your Python environment:

```bash
python -m pip install .
agent-squad --version
agent-squad skill install
```

The version is `0.5.0`. Skill installation copies the two role skills into
`~/.agents/skills` for Codex and links them from `~/.claude/skills` for Claude
Code. `--codex` selects the copies only; `--claude` selects the links only.
An existing differing file or link is refused. Inspect the difference before
using `--force` to replace it. Installation does not install `code-review`.

Start your interactive Implementer inside Herdr in the consuming repository's
primary checkout. Its live agent name must match `implementer.agent_name`
(default `implementer`). Then initialize, substituting the two account names:

```bash
agent-squad init --implementer-account <login> --reviewer-account <different-login>
agent-squad doctor
agent-squad doctor --live-reviewer
```

`init` derives the GitHub repository and default branch from `origin`, creates
schema 2 configuration at `.agent-squad/config.json`, and adds `.agent-squad/`
and `.agent-squad-review/` to Git's local exclusions. It leaves the committed
`.gitignore` alone. Existing configuration is validated and kept. Schema 1 is
refused: move that configuration aside and rerun `init`; no migration is offered.

The defaults are Codex implementing, Claude Code reviewing, three review
passes, and a merge commit. To reverse the agents, edit the existing
`implementer.kind` and `reviewer.kind` configuration fields before the trial.
See [configuration](docs/agent-squad-v0.6.0-spec.md#9-configuration-schema-version-2)
for account, branch, merge method, and directory settings.

`doctor` checks both forge identities, Reviewer permission, the remote base
branch, Herdr contracts and integrations, installed skills, writable roots,
disposable worktree creation/removal, and orphaned resources. An absent
Implementer is a warning. `--live-reviewer` additionally starts a disposable
Reviewer, checks readiness and trust behavior, and closes it; it sends no
review request. If startup needs a human answer, follow the reported pane and
resource information. Never have an agent answer a trust or permission dialog.

## Single identity with human approval

For a shared agent account, initialize with a distinct human approver:

```bash
agent-squad init --implementer-account <agent-login> --reviewer-account <agent-login> \
  --identity-mode single --approver-account <human-login>
```

Repeat `--approver-account` to allow additional people. The configuration stores
`identity_mode` and `approver_accounts`; old schema 2 files without these fields
continue to load as `dual` with no human-approval requirement. `doctor` checks
the shared account's push permission and the existence of every approver.

Both agents post under the shared login. Their role separation is by convention:
the forge cannot establish which agent wrote a review, finding, or decision.
The Reviewer posts comment reviews whose tagged verdict still records approval
or required changes. Merge additionally requires a configured human's latest
approval at exactly the PR head, not dismissed, and no configured human's latest
approve-or-request-changes review may request changes, even at an older head.
An older approval never becomes valid again after a later dismissed review.
Agent and human approvals may arrive in either order.

When only human approval is missing, `status` reports `await_human_approval`
and names the approvers. The Implementer reports and goes idle without polling.
Post human reviews by hand, then tell the Implementer to “check the PR”. A human
request-changes is reported to the Developer for an instruction; it does not
become a protocol finding or prevent a fresh agent review. Existing decisions,
stops, review budgets, CI checks, and merge holds continue to apply. Configure
branch protection separately if the forge must enforce these requirements.

## Work through an issue

Invoke `$squad-implementer` in Codex or `/squad-implementer` in Claude Code and
say “Let's start on issue #42” (use your issue number).

1. The Implementer reads the issue, including comments. An open, specified
   issue with acceptance criteria and no blocking triage labels becomes the
   Task unchanged, unless your start instruction changes scope or requests a
   Task. Otherwise it drafts a Task and asks for approval once. It asks about
   unresolved questions only when the answer would change the result.
2. It creates the issue worktree, implements, validates, commits, pushes, and
   opens a PR with the copied issue (or approved drafted Task) and report.
   Unless you said “don't merge”, kept the merge, or a hold applies, it records
   your start instruction as a standing instruction to merge when approved.
3. A fresh Reviewer checks the full PR HEAD SHA against repository standards
   and the effective Task, then publishes its review and inline findings.
4. The Implementer evaluates findings, records dispositions, fixes valid
   problems, validates, and requests a fresh review. Optional suggestions
   remain advisory; each receives a disposition.
5. Decisions and stops come back to you and your answer is recorded on the PR.
   The Implementer and Reviewer apply the
   [review-before-merge rule](docs/agent-squad-v0.5.0-spec.md#122-squad-implementer-mandatory-rules)
   to the whole PR: security, irreversible changes, authority changes, new
   dependencies or CI authority, publication or incompatible interfaces, and
   unresolved scope or design choices require your review before merging.
   The Reviewer records a `## Merge hold` with the applicable item and reason.
6. With approval and a standing instruction, the Implementer checks CI for
   that exact head, merges, waits for configured base-push CI, and reports the
   result, cleanup, and optional dispositions. You review routine work after
   merge. A hold or absence of an instruction makes it report approval and
   wait for you. Only your merge instruction after seeing a hold releases it.

You can withdraw a standing instruction by telling the Implementer not to
merge. A stop or Task amendment cancels it; after your continuation or
amendment is recorded, it is recorded again unless you said otherwise or a
hold applies. Neither standing instruction nor withdrawal lifts a stop or
answers a pending human decision.

A successful Herdr handoff ends the sending agent's step. It becomes idle;
there is no polling of the receiving agent. The Reviewer posts on GitHub
before notifying the Implementer, so a missing notification loses no review.
The CLI does not orchestrate CI or certify that checks ran; any repository
review-readiness policy must be satisfied separately for the exact revision.

The following reads help you inspect a PR; issue and PR numbers can differ:

```bash
agent-squad issue view --issue 42 --json
agent-squad pr head --pr 43 --json
agent-squad pr reviews --pr 43 --json
agent-squad status --pr 43 --json
```

`status` reports the next action, reasons, full review target, effective Task,
reviews, findings and replies, decisions, stops, budget, approval,
`merge_instruction`, `merge_hold`, paths, and
diagnostics. Without `--json`, it prints the next action and reasons first,
followed by the same detailed state.

## PR conventions and recovery

The [PR conventions](docs/agent-squad-v0.5.0-spec.md#7-pull-request-conventions)
define the exact grammar. In summary:

- PR bodies contain `## Task` and `## Implementation report`.
- Formal reviews begin with `AGENT_SQUAD/0.5.0 REVIEW`, identify full head and
  base SHAs, and use `approved`, `changes_requested`, or `needs_human`.
- Findings have PR-wide `REV-<n>` identifiers and `blocking` or `optional`
  severity. The Implementer replies with `DISPOSITION fixed <full-sha>`,
  `DISPOSITION rejected`, or `DISPOSITION needs-human`; the Reviewer records
  verification. Resolving a GitHub thread alone does not settle its finding.
- `DECISION` records human choices, Task amendments, or review-budget
  extensions, plus standing merge instructions and withdrawals. `STOPPED`
  halts automatic review until a later authorized continuation; standing merge
  instructions and withdrawals cannot provide that continuation. A new commit or Task amendment invalidates the earlier approval.

Forge writes require `--as implementer` or `--as reviewer`, subject to the
command's role. Tokens are selected only for the child process; the CLI does
not change `gh`'s active account or expose a token. Read commands default to
the Reviewer in its review worktree and to the Implementer elsewhere.

If the workflow appears stalled, tell the Implementer **“check the PR”**.
It reconstructs the next action through `status`; terminal recollection is
not the authority. On blocked Reviewer startup, answer the reported dialog
in person, then instruct the Implementer to use `reviewer adopt --pr <N>`.

If GitHub rejects a batch review, the full findings are published before
individual roots are attempted. `status` reports missing roots and the review
ID. `thread open` can restore a finding from PR data, and
`review post --resume <review-id>` completes that publication without creating
a second review. See the [command table](docs/agent-squad-v0.5.0-spec.md#102-command-table)
for the required arguments; repeating plain `review post` creates a new review.

| Exit | Meaning | Next step |
| --- | --- | --- |
| 0 | Success | Follow the reported next action. |
| 1 | Validation, Git, forge, or Herdr failure | Read the error and recheck state before retrying. |
| 2 | Command usage error | Correct the arguments. |
| 3 | Resources retained or a partial step needs attention | Inspect the reported paths/pane; preserve them until resolved. |
| 4 | Protocol gate refused the action | Address the stated decision, budget, disposition, or approval gate. |

## Merge and cleanup

Under your standing instruction or a later merge instruction, the Implementer
checks CI at the approved head and runs this from the primary checkout:

```bash
agent-squad pr merge --as implementer --pr 43
```

The command verifies current approval, guards the merge with the full head,
and uses the configured `merge` or `squash` method. If the base branch moved,
it refuses. Under a standing or explicit merge instruction, the Implementer
merges the base into the PR branch, validates, pushes, and has the new head
reviewed. It asks you when budget is exhausted or resolving a conflict needs
a choice outside the Task. Only your explicit acceptance permits
`--accept-moved-base`.
A latest-review hold also refuses merge; only your instruction after seeing
that hold permits `--accept-merge-hold`. Neither flag relaxes other checks.
Merge commits are checked by ancestry; a squash on an unmoved base is checked
by tree identity. Squash after accepting a moved base cannot use that tree
check and retains resources when integration cannot be verified.

After verified integration, the command removes only its owned implementation
branch/worktree, Reviewer resources, and per-PR and per-issue scratch
directories. After cleanup, even if some resources were retained, it
fast-forwards the primary checkout to the exact verified base commit when the
PR targets the configured base branch, that branch is checked out, and tracked
files have no staged or unstaged changes. It reports the starting and target
commits and whether the checkout was fast-forwarded, was already up to date,
was skipped, or Git refused. Another PR base branch, another checked-out
branch, detached `HEAD`, or tracked changes cause a skip. Git refuses
divergent history (`--ff-only`) and untracked files the update would
overwrite; `--no-overwrite-ignore` extends that protection to ignored files.
A skip or refusal leaves the merge/cleanup exit status unchanged and includes
the reason. A fallback command is supplied only when both the PR and checkout
use the configured base branch. The Implementer reports the result and never
runs the fallback command itself.
The command never switches branches, creates a local merge commit, runs
`git clean`, or removes an unrelated worktree.

## Verify this project

```bash
make test
make smoke
make doctor
```

The automated suite and thirteen-step smoke scenario use temporary repositories,
a fake GitHub executable, and fake Herdr; they never call models or GitHub.
`make doctor` checks your real configured environment. Live reviews, decisions,
and merges are separate evidence in [workflow verification](docs/workflow-verification.md).
Agent Squad's own PRs retain the manual review pipeline until the live trials
pass and the Developer switches the pipeline.

Licensed under [Apache 2.0](LICENSE).
