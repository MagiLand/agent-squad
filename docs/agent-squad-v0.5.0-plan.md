# Agent Squad v0.5.0 development plan

**Status:** decisions settled with the Developer on 2026-09-07. Input to
Increment 0, the v0.5.0 specification delta.

This plan turns the Implementation Review Loop into a skill-driven workflow in
which the pull request is the authority. It supersedes the parts of
[Agent Squad v0.4.4](agent-squad-v0.4.4-spec.md) that keep local JSON state,
bundle hashes, and Reviewer markers as the source of truth. Everything the
v0.4.4 protocol learned about exact revisions, independent review, bounded
iteration, and human authority is carried over in PR form.

## 1. What changes from v0.4.4

| Area | v0.4.4 | v0.5.0 |
| --- | --- | --- |
| Source of truth | `.agent-squad/state.json`, round records, bundles, markers | The pull request: formal reviews, inline threads, replies, and tagged comments |
| Entry point | `agent-squad start --task file.md` run by the Developer | The Developer invokes `squad-implementer` in an interactive session and says "Let's start on issue #N" |
| Implementer launch | Never launched by the tool | Still never launched by the tool; the user's own session |
| Reviewer launch | `submit` launches a fresh round-scoped Reviewer through Herdr | `reviewer launch` launches a fresh Reviewer per review through Herdr, called by the Implementer skill |
| Push before review | Non-goal | Required; the PR exists before the first review and CI runs on every revision |
| Review identity | run ID, round, request ID, result ID | PR number plus full head commit SHA |
| Findings | `review.json` in the bundle | Inline threads on the PR with IDs and severity tags |
| Dispositions | `response.json` | Replies on each thread, verified by the next Reviewer |
| Human decisions | `escalate` and `resume --resolution` | Tagged PR comments; the Implementer's session is the console |
| Review budget | `max_completed_change_reviews` in state | Derived by counting tagged formal reviews on the PR |
| Merge | Never; the Developer integrates | Never automatic; the Implementer reports "approved at SHA" and merges only when the user says so |
| Forges | none | GitHub via `gh`; Forgejo deferred to v0.6.0 |

Unchanged non-goals from v0.4.4 Section 8 still apply: no swarm, no workflow
engine, no role registry, no automatic merge, no CI replacement, no security
boundary between same-user processes.

## 2. Roles and topology

- **Developer.** Starts one interactive agent inside Herdr, invokes the
  Implementer skill, approves the task statement, answers questions, decides
  escalations, and authorizes the merge. The Implementer's pane is the human
  console; nothing important is reported only in a Reviewer pane.
- **Implementer.** The Developer's session. Owns the implementation worktree,
  pushes, opens the PR, launches each Reviewer, evaluates findings, records
  dispositions, and relays stops and decisions to the Developer. Uses the
  Implementer forge identity (`patrickhe` on GitHub).
- **Reviewer.** A fresh session per review, launched by the Implementer in a
  detached worktree at the exact head SHA, named `reviewer-pr<N>-<sha7>`. Reads
  the PR and its history, reviews independently, publishes a formal review as
  the Reviewer forge identity (`patrick-magiland` on GitHub), hands off, and is
  closed by the Implementer. It never writes tracked files. Probe scripts and
  harnesses it builds go in the per-PR scratch directory, outside the
  worktree, so the next Reviewer can re-run them against the new head.

A fresh Reviewer per review is the decided model: the PR carries all
continuity, and a fresh session cannot inherit a stale worktree or its own
earlier assumptions. A per-PR Reviewer that persists across passes remains the
fallback only if Increment 2 finds a trust prompt on every pass that cannot be
avoided; the PR conventions below work for both.

## 3. The PR protocol

Every rule here is derivable from the PR, Git, and Herdr. The CLI stores no
protocol state.

### 3.1 Identity

A review target is `pr=<number> head=<full-sha> base=<full-sha>` where `base`
is the merge-base with the base branch at review time. Iterations are never
identified by round number.

### 3.2 PR body

The Implementer opens the PR with two fixed sections:

- `## Task` — objective and acceptance criteria derived from the issue and
  approved by the Developer before coding. This is the v0.4.4 task snapshot.
- `## Implementation report` — summary, scope, files changed, design
  decisions, validation performed, known limitations, areas worth extra review
  (v0.4.4 Section 28.3). Updated on each push. The Reviewer treats it as an
  untrusted aid.

### 3.3 Formal review

Each review pass is one formal PR review submitted by the Reviewer identity
against the exact head SHA. Its body starts with a header line:

```text
AGENT_SQUAD/0.5.0 REVIEW pr=<N> head=<full-sha> base=<full-sha> verdict=<approved|changes_requested|needs_human>
```

The header is the authoritative verdict. When the Reviewer identity is
distinct from the Implementer identity, as on GitHub with `patrick-magiland`,
the forge review state mirrors it: approve, request changes, or comment for
`needs_human`. When only one identity is available, as on the client's Forgejo
instance planned for v0.6.0, the review is submitted as a comment and formal
approval comes from a human colleague. The body summarizes; findings live in
threads.

### 3.4 Findings

Each substantive or optional finding is one inline thread anchored to a diff
line, validated locally before posting because GitHub silently drops anchors
outside the diff. The first line is:

```text
[REV-<n>][blocking|optional][<category>] <title>
```

followed by problem, evidence, impact, required change, and verification, the
v0.4.4 Section 28.4 fields. IDs are sequential across the PR; the CLI allocates
the next number from existing threads. Optional findings never block approval.

### 3.5 Dispositions

The Implementer replies on every blocking thread before requesting the next
review:

```text
DISPOSITION fixed <full-sha>      + rationale and the verification command
DISPOSITION rejected              + concrete evidence
DISPOSITION needs-human           + the decision required
```

The next Reviewer verifies each disposition by execution, replies
`VERIFIED fixed`, `VERIFIED rejection accepted`, or `NOT FIXED`, and resolves
verified threads where the forge API allows it. Optional findings may receive a
one-line disposition or none. A thread marked `VERIFIED`, and any question
settled by a DECISION, is closed for later Reviewers unless new evidence
appears; a later Reviewer does not reopen it merely because it would have
judged differently.

### 3.6 Decisions

A Developer decision is a PR comment posted by the Developer, or by the
Implementer quoting the Developer, with the header:

```text
AGENT_SQUAD/0.5.0 DECISION finding=<REV-n|none> [budget=<n>]
```

The latest decision on a question is authoritative. Every Reviewer reads all
decisions before reviewing and does not re-litigate decided questions.
`budget=` raises the review budget for this PR.

### 3.7 Stop

Whichever role stops the loop posts a comment:

```text
AGENT_SQUAD/0.5.0 STOPPED head=<full-sha> reason=<budget|repeat|scope|design|ambiguity|judgement>
```

with a summary of the remaining problems. While the latest STOPPED is newer
than the latest DECISION, the Implementer skill refuses to launch another
review. The Reviewer's early-stop conditions from the current prompt are kept
verbatim as the `reason` vocabulary.

### 3.8 Budget

Effective budget = `max_review_passes` from configuration (default 3), or the
latest `budget=` value on the PR. Used = number of tagged formal reviews by the
Reviewer identity. Both skills check before launching or starting a review.

### 3.9 Approval validity and merge

A revision is agent-approved when the latest tagged review's header has
verdict `approved`, its forge `commit_id` equals the current PR head, that head
equals the Implementer worktree `HEAD`, and no STOPPED is newer than it. When a
distinct Reviewer identity is configured, the review's forge state must also
be approved; a mismatch is reported, not repaired. The Implementer reports
"approved at <sha>, ready to merge" and waits. Where the repository also
requires a human approval, the report says so. If the base branch moved since
the review's merge-base, the report says the merged tree would be an
unreviewed combination and leaves the choice to the Developer. On the
Developer's instruction it merges with the configured method, `merge` by
default so the approved SHA stays an ancestor of the base branch; with
`squash` the tool verifies integration by tree identity instead of ancestry.
It then deletes the branch and removes its worktree. Nothing merges
automatically.

## 4. Herdr handoff

- **Request.** `reviewer launch` creates the detached worktree, opens it with
  `herdr worktree open --cwd <repo> --path <worktree> --no-focus`, starts the
  agent with `herdr agent start <name> --kind <kind> --pane <pane>`, and sends
  one fixed line that invokes the Reviewer skill with the harness prefix:
  `/squad-reviewer pr=<N> head=<sha> base=<sha> implementer=<name>` for Claude
  Code, `$squad-reviewer ...` for Codex. Increment 2 tests whether a pasted
  slash command runs as a command; the fallback is passing the request as the
  agent's initial prompt argument after `--`.
- **Result.** `handoff review-result --pr N --head SHA --verdict V` sends the
  Implementer one fixed line: `AGENT_SQUAD/0.5.0 REVIEW_RESULT pr=<N>
  head=<sha> verdict=<V>` plus one sentence on what to do next. Posted on the
  PR first, sent second.
- **Stop.** Same shape with `STOPPED`. The Implementer relays it to the user.
- **Async discipline.** Both skills keep the current prompts' rule: after a
  successful handoff, stop and go idle; never wait on, read, or poll the other
  agent; resume only on a Herdr prompt or a user instruction.
- **Blocked detection.** After launch, the Implementer reads the Reviewer's
  state once. If `blocked`, it tells the user which pane needs a trust or
  permission answer. It never sends keys.
- **Lost notification.** `agent-squad status --pr N` derives the current state
  from the PR. "Check the PR" from the user is the recovery path.
- **Cleanup.** After consuming a result, the Implementer runs `reviewer close`,
  which verifies the workspace still holds only the Reviewer, closes it, and
  removes the worktree. Review worktrees hold nothing authoritative, so forced
  removal of a squad-created worktree is acceptable. The per-PR scratch
  directory survives across passes and is removed after the merge.

## 5. Components

### 5.1 Skills

Two skill directories, one `SKILL.md` each, installed under
`~/.agents/skills/<name>` with symlinks from `~/.claude/skills/<name>`, which
is how the Herdr and code-review skills are installed on this machine today.
Installation is an explicit `agent-squad skill install` command; the tool never
touches the Herdr skill or any `AGENTS.md`/`CLAUDE.md`.

- `squad-implementer` — derived from the current Implementer prompt: task
  statement and approval, worktree per issue, implement and validate, PR body
  sections, `reviewer launch`, disposition rules, optional-finding policy,
  stop handling, merge gate, async discipline.
- `squad-reviewer` — derived from the current Reviewer prompt: verify the
  target, read decisions and prior threads, re-run saved probes, verify
  dispositions by execution, then run the existing two-axis `code-review`
  skill from the detached worktree with the merge-base as the fixed point and
  the PR's Task section as the spec, apply the severity and follow-up policy,
  `review post`, budget and early-stop guards, `handoff review-result`, async
  discipline. The wrapper invokes the installed skill unambiguously, because
  Claude Code ships a built-in command of the same name, and `doctor` checks
  that the skill is installed.

Skills are control-plane content under v0.4.4 Section 16.3: changes to them
are reviewed manually.

### 5.2 CLI

`agent-squad` stays a standard-library Python package but shrinks to
mechanics the skills should not improvise. Kept and adapted: `herdr.py`,
`initialization.py`, `doctor.py`, `preflight.py`, `cli.py`. Removed: run and
round state, storage locks, bundle artifacts, submissions, review submissions
and applications, marker recovery.

| Group | Commands |
| --- | --- |
| Setup | `init`, `doctor [--live-reviewer]`, `skill install [--claude] [--codex]` |
| Forge | `pr head`, `pr reviews`, `review post`, `thread reply`, `thread resolve`, `decision post`, `stop post`, `issue view` |
| Derived state | `status --pr N` |
| Reviewer lifecycle | `review-worktree create/remove`, `reviewer launch/adopt/close` |
| Handoff | `handoff review-result`, `handoff stopped` |

Every forge mutation takes `--as implementer|reviewer` and resolves that
identity's token itself, so no skill prose handles tokens and no command ever
runs `gh auth switch`.

### 5.3 Forge adapter

- **GitHub** via `gh api`. Reviews with inline comments are posted through
  the REST reviews endpoint with `commit_id`, with the existing per-comment
  fallback on a batch rejection. Thread replies use the replies endpoint;
  resolution uses GraphQL. Reply enumeration goes per review, because the flat
  comment listing can omit replies. Stranded pending drafts are submitted, not
  deleted.
- **Forgejo is deferred to v0.6.0.** Facts gathered on 2026-09-07 for that
  release: the installed `fj` cannot create formal reviews or inline comments,
  so those need the Gitea-compatible REST endpoints with a token; Forgejo's API
  refuses approve or request-changes from the PR author with HTTP 422
  (`preparePullReviewType`), and its UI disables those buttons; `fj` stores one
  credential per host. The client's instance allows one account per person and
  requires formal approval from a separate human colleague, so v0.6.0 needs a
  single-identity mode in which the agent's verdict lives only in the review
  header and the human approval is checked separately. The v0.5.0 conventions
  in Section 3 keep the header authoritative for exactly that reason.
  Codeberg remains available for public trials if needed.
- **Anchor validation** is the existing `anchors.py` logic, productized:
  parse the unified diff into commentable right-side lines and refuse to post
  an anchor outside them.
- A **fake forge** executable with scripted responses serves the tests, like
  the committed fake Herdr.

### 5.4 Configuration

```json
{
  "schema_version": 2,
  "forge": {"kind": "github", "owner": "MagiLand", "repo": "agent-squad"},
  "implementer": {"agent_name": "implementer", "kind": "codex", "forge_account": "patrickhe"},
  "reviewer": {"kind": "claude", "start_args": [], "forge_account": "patrick-magiland"},
  "base_branch": "main",
  "max_review_passes": 3,
  "merge_method": "merge",
  "worktree_root": ".agent-squad/worktrees",
  "scratch_root": ".agent-squad/review-scratch"
}
```

`forge` is derived from the origin remote at `init` and may be overridden.
Only `github` is accepted in v0.5.0; the field exists so v0.6.0 can add
`forgejo` without a schema change.
Worktrees default to inside the repository under the excluded `.agent-squad/`
directory so a sandboxed harness whose writable root is its launch directory
can still write to them.

## 6. Harness specifics

- **Claude Code:** skill prefix `/`; pasted slash command needs the Increment 2
  test.
- **Codex:** skill prefix `$`; the sandbox may block the Herdr socket, as seen
  in the 2026-09-05 trials, so the skill says to request escalated permission
  for `agent-squad reviewer launch` and `handoff` and reports the failure
  rather than retrying blindly. Worktrees under the launch directory keep
  writes inside the sandbox.
- **Both:** trust and permission prompts are answered by a person. The tool
  never injects keys (v0.4.4 Section 16.4). In the 2026-09-05 trials neither
  harness recorded a trust entry for the review worktrees, only for the
  repositories they belonged to, which suggests both inherit trust for linked
  worktrees. Increment 2 confirms this before relying on it.

## 7. Testing

- **Unit:** header and ID parsing, budget derivation, approval validity,
  STOPPED/DECISION ordering, anchor validation, prompt templates, config.
- **Integration:** fake forge and fake Herdr; worktree creation and removal;
  launch, adopt, close; token selection by role.
- **Smoke:** a temporary repository with the fake forge runs the whole loop:
  first review with blocking findings, dispositions, second review approves,
  a later push invalidates that approval, third review, budget stop, decision
  with budget extension, approval, human-gated merge. Plus lost notification
  discovered through `status`.
- **Live trials:** a disposable repository under the MagiLand organization,
  both directions (Codex implements and Claude reviews, then the reverse),
  using `patrickhe` and `patrick-magiland`. Evidence recorded as in
  `docs/verification/`.
- **Self-review:** PRs to Agent Squad itself keep the current manual review
  pipeline until v0.5.0 has passed its own live trials.

## 8. Increments

Each increment becomes one issue under milestone v0.5.0.

0. **Specification delta.** `docs/agent-squad-v0.5.0-spec.md` freezes the PR
   conventions, prompt templates, configuration, CLI surface, and the list of
   superseded v0.4.4 sections. Done when merged.
1. **GitHub adapter and derived state.** GitHub adapter, PR convention
   parsing, anchor validation, `review post`, thread commands,
   `status`, fake forge. Done when the smoke scenario's forge steps pass
   against the fake and `review post` has been exercised once on a real
   disposable GitHub PR as `patrick-magiland`.
2. **Reviewer lifecycle.** Review worktree, `reviewer launch/adopt/close`,
   templates, blocked detection, delivery experiment (prompt after start
   versus initial prompt). Done when fake-Herdr tests pass, one real Claude
   Reviewer and one real Codex Reviewer have been launched and closed, and
   trust inheritance for linked worktrees is confirmed or the stable-path
   fallback adopted.
3. **Skills.** Both `SKILL.md` files and `skill install`. Done when a real
   loop on the disposable repository reaches approval and a human-gated merge
   from a single interactive session.
4. **Doctor and preflight.** Two forge identities distinct and authorized,
   Herdr schema, socket reachability from the configured Implementer kind,
   worktree root, orphaned Reviewers and worktrees. Done when `doctor` catches
   each misconfiguration the live trials hit.
5. **Smoke, docs, removal, release.** Smoke runner, README and workflow guide
   rewritten, v0.4.4 machinery removed, version 0.5.0, verification record.

Forgejo support, including the single-identity mode, is v0.6.0.

## 9. Decisions

All settled with the Developer on 2026-09-07.

- The PR is the authority; the CLI stores no protocol state.
- v0.5.0 is GitHub only. Forgejo, including a single-identity mode for the
  client's instance where a human colleague gives formal approval, is v0.6.0.
  The review header is the authoritative verdict so that mode needs no
  convention change.
- Two skills, `squad-implementer` and `squad-reviewer`, installed for both
  Claude Code and Codex from one shared location.
- Fresh Reviewer per review pass, with a per-PR scratch directory for probes
  and the settled-thread rule. Per-PR persistence is the fallback only if
  trust prompts cannot be avoided.
- `squad-reviewer` wraps the existing two-axis `code-review` skill.
- Merge commits by default, configurable per repository; squash verified by
  tree identity.
- The Implementer confirms the task statement once, then works autonomously
  until the first handback. The merge waits for the Developer.
- `patrickhe` implements and `patrick-magiland` reviews on GitHub; the
  Reviewer account has write access and resolves verified threads.

## 10. Where the current prompts land

| Current prompt section | v0.5.0 home | Change |
| --- | --- | --- |
| Implementer: implementation workflow | `squad-implementer` | Adds the approved task statement and PR body sections |
| Implementer: notify `reviewer` via Herdr skill | `agent-squad reviewer launch` | Fresh named Reviewer per review; fixed template; no Herdr skill prose |
| Implementer: handling review feedback | `squad-implementer` | Dispositions become tagged thread replies; `status` before acting |
| Implementer: optional findings and follow-ups | `squad-implementer` | Kept verbatim |
| Implementer: approval and merge | `squad-implementer` | Approval verified on the PR, not from the Herdr message; merge waits for the user |
| Implementer: manual-intervention guard | `squad-implementer` | Reads STOPPED on the PR; relays to the user |
| Reviewer: review workflow | `squad-reviewer` | Runs from the detached worktree; reads decisions and prior threads first |
| Reviewer: severity and follow-up policy | `squad-reviewer` | Kept verbatim; severity tags in thread headers |
| Reviewer: handoff after review | `agent-squad review post` then `handoff review-result` | Formal review state on the PR, posted before notifying |
| Reviewer: review-loop guard and early stop | `squad-reviewer` plus `stop post` | Budget derived from the PR; stop recorded on the PR |
| Both: asynchronous handoff and discipline | Both skills | Kept verbatim |
