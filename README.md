# Agent Squad

Agent Squad coordinates an Implementer and an independent Reviewer through
Herdr. The forge pull request carries the Task, reviews, findings, dispositions,
Developer decisions, and approval. Each review identifies an exact commit and
runs in a fresh detached worktree. Starting an issue authorizes routine work through merge; higher-risk PRs wait
for the Developer's review before merging.

The implementation follows the [Agent Squad specification](docs/agent-squad-spec.md)
for GitHub and Forgejo, with separate Implementer and Reviewer accounts. The
package version is `0.6.1`; the protocol tag remains `AGENT_SQUAD/0.5.0`.
Release readiness depends on the recorded checks and human-operated trials.
See [workflow verification](docs/workflow-verification.md) for deterministic
coverage and the live-trial evidence required before releasing v0.6.0.

## Install and prepare a repository

Requirements: Python 3.11 or later, Git, Herdr, Codex CLI,
Claude Code, and the installed `code-review` skill. Both agent integrations in
Herdr must be current. GitHub repositories also require GitHub CLI (`gh`).
On GitHub, authenticate `gh` as two different GitHub accounts, one for the
Implementer and one for the Reviewer; the Reviewer account needs write
permission on the consuming repository.
The Python package has no runtime dependencies outside the standard library.

Codex Reviewer delivery after startup is [verified on Herdr client and server
0.9.3 with Codex CLI 0.159.2](docs/verification/2026-09-30-issue-100.md).
Check the running server with `herdr status server`: updating the client does
not replace a running server. Resolve first-launch folder trust as a person
before expecting unattended reviews. This evidence does not establish a
minimum Herdr version, and `doctor` does not enforce a version cutoff.

Install from this checkout into your Python environment:

```bash
python -m pip install .
agent-squad --version
agent-squad skill install
```

The version is `0.6.1`. Skill installation copies the two role skills into
`~/.agents/skills` for Codex and links them from `~/.claude/skills` for Claude
Code. `--codex` selects the copies only; `--claude` selects the links only.
An existing differing file or link is refused. Inspect the difference before
using `--force` to replace it. Installation does not install `code-review`.

Start your interactive Implementer inside Herdr in the consuming repository's
primary checkout. Its live agent name must match `implementer.agent_name`
(default `implementer`) and its kind must match `implementer.kind`.

Herdr can run several named sessions. Agent Squad finds the one running
session whose agent has that name and kind and works inside this repository,
and sends every Herdr call there, whatever session the calling process
inherited. Commands that need Herdr refuse when no session or more than one
matches; `doctor` names the session it found.

Then initialize, substituting the two account names:

```bash
agent-squad init --implementer-account <login> --reviewer-account <different-login>
agent-squad doctor
agent-squad doctor --live-reviewer
```

`init` derives the repository and default branch from `origin`, creates
schema 2 configuration at `.agent-squad/config.json`, and adds `.agent-squad/`
and `.agent-squad-review/` to Git's local exclusions. It leaves the committed
`.gitignore` alone. Existing configuration is validated and kept. Schema 1 is
refused: move that configuration aside and rerun `init`; no migration is offered.
Configuration written by v0.6.x for two accounts loads unchanged. One written
for a shared account is refused: move it aside and rerun `init` with two
accounts.

The defaults are Codex implementing, Claude Code reviewing, three review
passes, and a merge commit. To reverse the agents, edit the existing
`implementer.kind` and `reviewer.kind` configuration fields before the trial.
See [configuration](docs/agent-squad-spec.md#9-configuration-schema-version-2)
for account, branch, merge method, and directory settings.

`doctor` checks both forge identities, Reviewer permission, the remote base
branch, Herdr contracts and integrations, installed skills, writable roots,
disposable worktree creation/removal, and orphaned resources. An absent
Implementer is a warning. `--live-reviewer` additionally starts a disposable
Reviewer, checks readiness and trust behavior, and closes it; it sends no
review request. If startup needs a human answer, follow the reported pane and
resource information. Never have an agent answer a trust or permission dialog.

### Forgejo setup

Use Forgejo 16.0.0 or later; the local setup checks were exercised on 16.0.3.
Forgejo uses the standard-library HTTP client and does not require `gh` or `fj`.
Prepare ordinary Git access to `origin` separately. An SSH alias supplies the
repository path, not the API host: always pass the instance's explicit base URL.
HTTPS is required except for `http://127.0.0.1`, `http://[::1]`, or
`http://localhost` in local tests. Instance path prefixes are preserved;
queries, fragments, and embedded credentials are refused.

Create each role's token in Forgejo with exactly `write:repository`,
`write:issue`, and `read:user`. Store it as one non-empty line in a regular
`0600` file outside every worktree and the `.agent-squad` control directory.
Do not use a symlink. The CLI stores only the path, checks the file on each
configuration load and token read, and never creates or repairs token files.

Forgejo is implemented and covered by the fake server. One supervised live
review loop with two accounts ran on a local Forgejo 16.0.3 instance, with
Claude Code implementing and Codex reviewing. It covered a blocking review,
verified fixes, an exact-head approval that satisfied a one-approval branch
rule, a merge and cleanup; see the
[#98 record](docs/verification/2026-10-07-issue-98.md). HTTPS and SSH
transport, other Forgejo versions, the opposite agent direction, squash merges
and explicit branch deletion remain **unverified** with two accounts. Grant
the Reviewer account repository write access:

```bash
agent-squad init --forge forgejo --base-url https://forge.example/instance \
  --implementer-account <implementer-login> --reviewer-account <reviewer-login> \
  --implementer-token-file /private/agent-squad/implementer.token \
  --reviewer-token-file /private/agent-squad/reviewer.token
agent-squad doctor
```

Existing configuration is validated and kept; rerunning `init` reports
differences, including the forge fields.
After local validation, Forgejo initialization makes one API read of the
repository with the Implementer token. API requests stay at the configured
base URL; Git continues to use `origin`.

Doctor reports `forge client` with the observed server version, verifies both
token files and logins, reads repository access, and checks Reviewer write
permission. Herdr, skills, Git and orphan-resource checks apply to both forges.
Doctor reports problems without repairing configuration or removing orphaned
resources.

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
   [review-before-merge rule](docs/agent-squad-spec.md#122-squad-implementer-mandatory-rules)
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

Follow-up work the Implementer finds outside the Task becomes a new issue
through `issue create`, labelled `needs-triage` and opened with the
Implementer's `NOTE` line. It waits for your triage; the Implementer never
starts work on it and lists it in its report. The repository needs a label
named exactly `needs-triage`; without one the command creates nothing.

A successful Herdr handoff ends the sending agent's step. It becomes idle;
there is no polling of the receiving agent. The Reviewer posts on the configured forge
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

The [PR conventions](docs/agent-squad-spec.md#7-pull-request-conventions)
define the exact grammar. In summary:

- PR bodies contain `## Task` and `## Implementation report`.
- Formal reviews begin with `AGENT_SQUAD/0.5.0 REVIEW`, identify full head and
  base SHAs, and use `approved`, `changes_requested`, or `needs_human`.
- Findings have PR-wide `REV-<n>` identifiers and `blocking` or `optional`
  severity. The Implementer replies with `thread reply --disposition fixed
  --sha <full-sha>`, `--disposition rejected`, or `--disposition needs-human`;
  the Reviewer records verification with `--verification`. The CLI writes the
  tagged first line and refuses a body file that begins with one. Resolving a
  GitHub thread alone does not settle its finding.
- Agents supply review sections, dispositions, verifications, and standing
  merge instructions as command options and prose files; the CLI composes the
  protocol text stored on the forge (`review post --summary`,
  `--verified-dispositions`, and optional section files; `decision post
  --merge-instruction record|withdraw`).
- `DECISION` records human choices, Task amendments, or review-budget
  extensions, plus standing merge instructions and withdrawals. `STOPPED`
  halts automatic review until a later authorized continuation; standing merge
  instructions and withdrawals cannot provide that continuation. A new commit or Task amendment invalidates the earlier approval.

Forge writes require `--as implementer` or `--as reviewer`, subject to the
command's role. GitHub tokens are selected only for the child process; Forgejo tokens are
read from the configured role files for authenticated HTTP requests. The CLI
does not change `gh`'s active account or expose a token. Read commands default to
the Reviewer in its review worktree and to the Implementer elsewhere.

If the workflow appears stalled, tell the Implementer **“check the PR”**.
It reconstructs the next action through `status`; terminal recollection is
not the authority. When startup reports `agent_not_ready` (exit 3) and retains
the Reviewer name, answer the dialog in person, then instruct the Implementer
to use `reviewer adopt --pr <N>`. In the [issue #100 trial](docs/verification/2026-09-30-issue-100.md),
Herdr 0.9.3 classified Codex's trust prompt as `unknown`; startup timed out
and removed the name, so adoption could not recover it. After the person
answered trust and exited Codex to the shell, guarded `reviewer close`
succeeded and a fresh launch worked. Retain resources when cleanup refuses;
do not resend the request or bypass the occupant check.

If GitHub rejects a batch review, the full findings are published before
individual roots are attempted. `status` reports missing roots and the review
ID. `thread open` can restore a finding from PR data, and
`review post --resume <review-id>` completes that publication without creating
a second review. See the [command table](docs/agent-squad-spec.md#102-command-table)
for the required arguments; repeating plain `review post` creates a new review.

Forgejo posts the review body first, followed by each finding root. An empty
`diff_hunk` in the root read-back leaves the finding unanchored; `thread open`
recovers it at a valid location. Interrupted publication uses the same
`review post --resume <review-id>` flow and original section and thread files.
`thread resolve` is unsupported on Forgejo and returns exit 1. Reviewer
verification replies settle findings without changing forge thread state.

A pending review draft on the posting account blocks Forgejo `review post`
with exit 4 and the `pending_draft` gate. Inspect the named draft before
explicitly discarding it. Reissue the complete publication command with
`--discard-draft <review-id>` (and `--resume <published-review-id>` when
recovering an interrupted publication). Only the selected account's exact
pending draft can be deleted; unrelated or submitted reviews are refused.
The flag is unsupported on GitHub. Never discard a person's work implicitly.

| Exit | Meaning | Next step |
| --- | --- | --- |
| 0 | Success | Follow the reported next action. |
| 1 | Validation, Git, forge, or Herdr failure | Read the error and recheck state before retrying. |
| 2 | Command usage error | Correct the arguments. |
| 3 | Resources retained or a partial step needs attention | Inspect the reported paths/pane; preserve them until resolved. |
| 4 | Protocol gate refused the action | Address the stated decision, budget, disposition, or approval gate. |

## Command reference

Use `agent-squad <command> --help` for all required arguments. The
[full command table](docs/agent-squad-spec.md#102-command-table) defines
role restrictions and forge-specific behavior; this inventory covers every
shipped command path.

| Commands | Purpose |
| --- | --- |
| `init`, `doctor`, `skill install` | Configure the consuming repository, diagnose prerequisites, install role skills. |
| `issue view`, `issue comment`, `issue create` | Read the governing issue, comments, labels, and configured paths; post a marked Implementer note, such as a root cause, on an open issue; file follow-up work as a new issue labelled `needs-triage`. |
| `pr create`, `pr report` | Publish the Task and implementation report; update the report on an open PR. |
| `pr head`, `pr reviews`, `status` | Read exact revision identities, reviews, and derived workflow state. |
| `pr merge`, `pr cleanup` | Merge an approved revision and verify integration and owned cleanup; finish a merge that `pr merge` started but could not complete. |
| `review-worktree create`, `review-worktree remove` | Manage the detached checkout for one exact revision. |
| `reviewer launch`, `reviewer adopt`, `reviewer close` | Start a fresh Reviewer, adopt after a human startup answer, or close owned resources. |
| `review post` | Publish or resume the formal review and findings. |
| `thread open`, `thread reply`, `thread resolve` | Recover an anchor, post a disposition or verification, resolve a supported forge thread. |
| `decision post`, `stop post` | Record Developer authority or stop the review loop. |
| `handoff review-result`, `handoff stopped` | Deliver fixed notifications after the corresponding forge record exists. |

The [end-to-end example](docs/agent-squad-example.md) shows publication,
approval, recovery, and merge.

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
directories. On GitHub, when the repository deletes merged head branches
automatically, it first waits up to 30 seconds for that deletion and deletes
the branch itself only if GitHub has not. It confirms the remote branch is
absent, then removes its local `refs/remotes/origin/<branch>` only if that ref
still equals the approved head.
A changed ref or uncertain remote result retains resources and is reported. After cleanup, even if some resources were retained, it
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

Before it sends the merge request, `pr merge` saves the identities cleanup
needs in a merge record in the repository's common Git directory: the
approved head, both branches, the merge method, and the issue number. If the
merge outcome is unknown, integration could not be verified, or a cleanup step
failed, the result names the command that finishes the job:

```bash
agent-squad pr cleanup --as implementer --pr 43
```

It never merges again. It verifies integration against the recorded head,
runs the remaining cleanup steps with the same guards (a resource that is
already gone counts as done), fast-forwards the primary checkout, and deletes
the record once every step succeeded. The Implementer runs it once without
asking and reports a second failure to you. A PR merged outside `pr merge` has
no record; `doctor` reports its leftover resources.

## Verify this project

```bash
make venv
make test
make lint
make smoke
make doctor
```

`make venv` needs [`uv`](https://docs.astral.sh/uv/). It creates `.venv` with
Python 3.11, which CI's pull-request jobs use, and installs setuptools so the
packaging test runs instead of being skipped. It also installs the
pycodestyle version that CI pins for `make lint`, which checks PEP 8. When
`.venv` exists, the other targets use its interpreter; set `PYTHON` to choose
another one.

The automated suite runs the twelve-step smoke scenario twice, on fake GitHub
and on a loopback fake Forgejo server, each with two accounts. Both use
temporary repositories and fake Herdr; they never call models or a real forge.
Source-export smoke runs both forges without `.git`; CI reserves that duplicate
export run for main pushes and the weekly schedule.
`make doctor` checks your real configured environment. Live reviews, decisions,
and merges are separate evidence in [workflow verification](docs/workflow-verification.md).
Agent Squad's own implementation PRs use this review loop. Release acceptance
remains a separate Developer decision based on the recorded live-trial evidence.

Licensed under [Apache 2.0](LICENSE).
