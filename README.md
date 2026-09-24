# Agent Squad

Agent Squad coordinates an Implementer and an independent Reviewer through
Herdr. The GitHub pull request carries the Task, reviews, findings, dispositions,
Developer decisions, and approval. Each review identifies an exact commit and
runs in a fresh detached worktree. The Developer decides when to merge.

The implementation follows the [v0.5.0 specification delta](docs/agent-squad-v0.5.0-spec.md).
See [workflow verification](docs/workflow-verification.md) for deterministic
coverage and the live-trial evidence required before releasing v0.5.0.

## Install and prepare a repository

Requirements: Python 3.11 or later, Git, GitHub CLI (`gh`), Herdr, Codex CLI,
Claude Code, and the installed `code-review` skill. Both agent integrations in
Herdr must be current. Authenticate `gh` as two different GitHub accounts; the
Reviewer account needs write permission on the consuming repository.
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
See [configuration](docs/agent-squad-v0.5.0-spec.md#9-configuration-schema-version-2)
for account, branch, merge method, and directory settings.

`doctor` checks both forge identities, Reviewer permission, the remote base
branch, Herdr contracts and integrations, installed skills, writable roots,
disposable worktree creation/removal, and orphaned resources. An absent
Implementer is a warning. `--live-reviewer` additionally starts a disposable
Reviewer, checks readiness and trust behavior, and closes it; it sends no
review request. If startup needs a human answer, follow the reported pane and
resource information. Never have an agent answer a trust or permission dialog.

## Work through an issue

Invoke `$squad-implementer` in Codex or `/squad-implementer` in Claude Code and
say “Let's start on issue #42” (use your issue number).

1. The Implementer reads the issue and presents the Task for your approval.
2. After approval, it creates the issue worktree, implements, validates,
   commits, pushes, and opens a PR with the Task and implementation report.
3. It launches a fresh Reviewer at the full PR HEAD SHA. The Reviewer uses
   `code-review` to check repository standards and the approved Task, then
   publishes its review and inline findings on GitHub.
4. The Implementer reads the PR, evaluates findings, records dispositions,
   fixes valid problems, validates, and requests a fresh review. Reviewers
   verify the dispositions before settling blocking threads.
5. Decisions and stops come back to you. The Implementer records your answer
   on the PR before continuing. Optional suggestions do not require changes
   merely because they were raised.
6. When the current revision is approved, the Implementer reports its full
   SHA and waits for your merge instruction.

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
reviews, findings and replies, decisions, stops, budget, approval, paths, and
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
  extensions. `STOPPED` halts automatic review until a later authorized
  decision. A new commit or Task amendment invalidates the earlier approval.

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

After your explicit instruction, the Implementer runs this from the primary
checkout, using the actual approved PR number:

```bash
agent-squad pr merge --as implementer --pr 43
```

The command verifies current approval, guards the merge with the full head,
and uses the configured `merge` or `squash` method. If the base branch moved,
it refuses until you choose a fresh review or explicitly accept the moved
base. Only for the latter choice does the Implementer add `--accept-moved-base`.
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

The automated suite and twelve-step smoke scenario use temporary repositories,
a fake GitHub executable, and fake Herdr; they never call models or GitHub.
`make doctor` checks your real configured environment. Live reviews, decisions,
and merges are separate evidence in [workflow verification](docs/workflow-verification.md).
Agent Squad's own PRs retain the manual review pipeline until the live trials
pass and the Developer switches the pipeline.

Licensed under [Apache 2.0](LICENSE).
