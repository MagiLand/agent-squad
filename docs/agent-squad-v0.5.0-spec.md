# Agent Squad v0.5.0

## Specification Delta: The Pull Request as Authority

- **Version:** 0.5.0
- **Status:** Specification delta; the output of Increment 0 of the [v0.5.0 development plan](agent-squad-v0.5.0-plan.md)
- **Baseline:** [Agent Squad v0.4.4](agent-squad-v0.4.4-spec.md)
- **Primary runtime:** Herdr
- **Supported coding agents:** Codex CLI and Claude Code
- **Supported forge:** GitHub through `gh`
- **Reference implementation target:** Python 3.11 or later on macOS and Linux

---

## 1. How to Read This Delta

This document turns the Implementation Review Loop of v0.4.4 into a skill-driven workflow in which the GitHub pull request is the authority. It is a delta, not a rewrite. Section 2 assigns every v0.4.4 section one of three dispositions:

- **Retained.** The v0.4.4 text applies unchanged and is not repeated here.
- **Amended.** The v0.4.4 text applies except where the referenced section of this delta says otherwise.
- **Superseded.** The referenced section of this delta replaces the v0.4.4 section entirely.

The normative language of v0.4.4 Section 3 applies to this delta. **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, and **MAY** have the meanings defined there, and sections labelled *Informative* are not independently normative.

The single input to this delta is the decided plan. Section 20 lists every point where this delta departs from the plan's decisions, or states that there are none. A departure that is not listed there is a defect in this document.

Notation: *v0.4.4 §N* refers to a section of the baseline, and every further item in a list or range that starts with *v0.4.4 §* refers to the baseline as well; *§N* alone refers to a section of this delta. Where this delta quotes a rule from the current Implementer or Reviewer prompt without change, the rule is marked **[verbatim]**; adapted rules are marked **[adapted]**; rules with no counterpart in the current prompts are marked **[new]**.

---

## 2. Disposition of v0.4.4 Sections

| v0.4.4 section | Disposition | Where in this delta |
| --- | --- | --- |
| 1 Executive Summary | Amended | §3.1: the loop runs on the PR; the Developer merges through the Implementer, on instruction |
| 2 Intended Use of This Specification | Retained (informative) | — |
| 3 Normative Language | Retained | §1 |
| 4 Problem Statement | Retained (informative) | — |
| 5 Product Definition | Amended | §3.1: "local run state" and "structured artifacts" in 5.1 become the PR conventions of §7 |
| 6 Scope and Operating Assumptions | Amended | §3.3 adds the forge assumptions |
| 7 Goals | Amended | §3.2: 7.4 names the PR as the durable record; 7.5 keeps final integration with the Developer, who now instructs the merge |
| 8 Explicit Non-goals | Retained | §19 restates and extends the list |
| 9 Design Principles | Amended | §3.2: 9.4 and 9.5 are restated for the PR and Herdr; 9.8 retained |
| 10 Relationship with Herdr | Amended | §8: the adapter surface changes; "Herdr status is a hint" is retained |
| 11 Repository Independence | Retained | — |
| 12 Reference Implementation Constraints | Amended | §5.6: no state lock; `gh` is an external executable |
| 13 Suggested Agent Squad Repository Layout | Amended | §5.6 |
| 14 Target-Environment Discovery | Amended | §8.2 and §11.1 add `gh` discovery |
| 15 Roles and Authority | Amended | §4 |
| 16 Trust and Security Model | Retained | §4.4 and §12.1 apply 16.3 to the skills |
| 17 Consuming-Repository Runtime Layout | Superseded | §5 |
| 18 Repository and Worktree Identity | Superseded | §5.1 and §6: there is no run identity; worktree identity is derived from Git each time |
| 19 Configuration | Superseded | §9 |
| 20 Preflight and Capability Verification | Amended | §10.3: the checks change; the live preflight launches a Reviewer without a bundle; 20.3 is dropped |
| 21 Task and Context Capture | Superseded | §7.2: the `## Task` section is the task snapshot; 21.5 survives in §7.6 |
| 22 Git Revision Model | Amended | §6: 22.2 fixed base becomes the merge-base at review time; 22.6 approval scope becomes §7.10; 22.10 permits push, merge, and branch deletion on instruction |
| 23 Review Worktree and Self-contained Bundle | Superseded | §5.2, §5.3, and §8: one detached worktree per review remains; bundle and marker are gone |
| 24 Authoritative Local State | Superseded | §7.9: state is derived from the PR; nothing is stored |
| 25 Run and Round State Model | Superseded | §7.9 |
| 26 Protocol Lifecycle | Superseded | §7 and §8 |
| 27 Superseding, Stale, and Invalid Results | Superseded | §7.9: a review whose `commit_id` is not the PR head is simply not current |
| 28 Artifact Contracts | Superseded | §7: 28.3 survives as the `## Implementation report` section; 28.4 finding fields survive in thread bodies |
| 29 Review Policy | Retained | §12.3 adds the severity and follow-up policy |
| 30 Herdr Handoff Protocol | Superseded | §8 |
| 31 Local Delivery, Discoverability, and Idempotency | Superseded | §7.9 and §14 |
| 32 CLI Surface | Superseded | §10 |
| 33 Failure and Recovery Semantics | Superseded | §14 |
| 34 Cleanup Policy | Superseded | §15 |
| 35 Shared Agent Instructions | Superseded | §12: the two skills replace the fragments |
| 36 Testing Strategy | Superseded | §16 |
| 37 Implementation Increments | Superseded | §17 |
| 38 Double Dubs as Dogfood | Retained | §16.4 keeps a consumer trial |
| 39 Reliability Guarantees | Superseded | §18.1 |
| 40 Explicit Non-guarantees | Amended | §18.2 |
| 41 Deliberate Simplifications and Rejected Overdesign | Amended | §18.3: 41.1 to 41.4 no longer apply; 41.5 to 41.9 are retained |
| 42 Future Experiments | Retained | — |
| 43 Acceptance Criteria | Superseded | §17: done criteria per increment |
| 44 Definition of Done | Superseded | §17.6 |
| 45 Instructions to the Implementing Agent | Amended | §17.7 |
| Appendix A Disposition of Review Findings | Retained (informative, historical) | — |
| Appendix B Example End-to-End Run | Superseded | Appendix A of this delta |

---

## 3. What Changes

### 3.1 The loop

```text
Developer tells the Implementer session "Let's start on issue #N"
        ↓
Implementer drafts the task statement from the issue; the Developer approves it once
        ↓
Implementer implements in a per-issue worktree, validates, pushes, opens the PR
        ↓
Implementer launches a fresh Reviewer at the exact PR head through Herdr
        ↓
Reviewer verifies the target, reads decisions and prior threads, reviews,
posts one formal review with tagged threads, hands off, and goes idle
        ↓
Implementer records dispositions, fixes, pushes, launches the next Reviewer
        ↓
approved at <sha>   |   needs_human → Developer DECISION   |   STOPPED → Developer
        ↓
Developer says "merge"; Implementer merges with the configured method and cleans up
```

Every fact the loop depends on is derived from the pull request, Git, and Herdr. The CLI stores no protocol state. The plan's Section 1 table of what changes from v0.4.4 is incorporated by reference; the following v0.4.4 invariants are carried over in PR form:

- every review identifies the exact base and head object IDs (v0.4.4 §7.3 and §22.1);
- the Reviewer reads a detached worktree fixed at the reviewed head, never the Implementer's mutable tree (v0.4.4 §7.2);
- the Reviewer is independent and fresh for each review pass (v0.4.4 §9.3);
- iteration is bounded and the bound is visible (v0.4.4 §25.8);
- human authority over requirements, design, risk, and integration is preserved (v0.4.4 §7.5);
- a record is written before the corresponding Herdr message is sent (v0.4.4 §9.5);
- Herdr agent status is a scheduling hint, never protocol truth (v0.4.4 §10);
- no permission or trust prompt is ever answered by the tool (v0.4.4 §16.4);
- only the Implementer writes production code in the implementation worktree (v0.4.4 §9.8).

### 3.2 Principles restated for the PR

- **Post before notify.** A review, disposition, decision, or stop MUST exist on the PR before any Herdr message about it is sent (v0.4.4 §9.5 applied to the forge).
- **The PR over terminal memory.** The PR and its threads are the authoritative record; terminal prose and Herdr prompts are control messages (v0.4.4 §9.4).
- **Derive, do not store.** Any command MUST be able to reconstruct the loop's state from the PR, Git, and Herdr alone. No local file records a verdict, a disposition, a decision, a stop, or a budget.
- **Exact revision.** Every review and every approval is bound to a full head SHA; a newer head is unreviewed until reviewed.
- **Human authority.** Decisions are recorded on the PR; stops are relayed to the Developer; nothing merges without the Developer's instruction.
- **Keep the tool small.** The CLI provides mechanics the skills should not improvise and nothing else (v0.4.4 §7.6).

### 3.3 Operating assumptions

v0.4.4 §6 applies, with these additions and replacements:

- the target repository has a GitHub remote, and `gh` is installed and logged in to two GitHub accounts: the Implementer identity and the Reviewer identity;
- the Reviewer identity has write access to the repository;
- GitHub is reachable while the loop runs;
- the Developer's own interactive session is the Implementer, started inside Herdr in the repository's primary checkout;
- one pull request is driven per Implementer session at a time; several PRs MAY be in flight in separate issue worktrees, each with its own Reviewers;
- the Developer answers trust and permission prompts in person.

v0.5.0 is not designed for forge outages, for shared PRs edited by several Implementer sessions concurrently, or for repositories whose base branch forbids the configured merge method.

### 3.4 Terms

| Term | Meaning |
| --- | --- |
| Review target | The triple `pr=<N> head=<full-sha> base=<full-sha>` (§6.1) |
| Tagged review | A formal PR review whose body's first line is a valid `REVIEW` header (§7.3) posted by the Reviewer identity |
| Current review | A tagged review whose `commit_id` equals the PR head (§7.9) |
| Finding, thread | One inline review thread whose root comment's first line is a valid finding line (§7.4) |
| Disposition | The Implementer's reply on a thread whose first line is a `DISPOSITION` line (§7.5) |
| Verification | The Reviewer's reply on a thread whose first line is a `VERIFIED` or `NOT FIXED` line (§7.5) |
| Settled thread | A thread closed for later Reviewers under §7.5 |
| Decision, general decision, Task amendment | A PR conversation comment whose first line is a valid `DECISION` header; a general decision has `finding=none`; a Task amendment is a general decision whose body contains the amended `## Task` section (§7.6) |
| Effective Task | The `## Task` section of the latest Task amendment, or the PR body's section when there is none (§7.6) |
| Unanchored finding | A finding listed by a tagged review that has no thread yet (§7.4) |
| Incomplete review | A tagged review with at least one unanchored finding (§7.4) |
| Stop | A PR conversation comment whose first line is a valid `STOPPED` header (§7.7) |
| Budget | The effective and used review counts derived under §7.8 |
| Agent-approved | The condition defined in §7.10 |
| Implementer identity, Reviewer identity | The configured forge accounts (§4.5) |
| Control root | `<primary-worktree>/.agent-squad/` (§5.1) |
| Worktree root, scratch root | The configured roots for worktrees and per-PR scratch directories (§5.2, §5.3) |

---

## 4. Roles, Identities, and Authority

v0.4.4 §15 is amended as follows. v0.4.4 §15.4 (no autonomous project manager) is retained.

### 4.1 Developer

The Developer:

- starts one interactive agent inside Herdr in the repository's primary checkout and invokes the `squad-implementer` skill;
- says which issue to start and approves the task statement once, before coding;
- answers questions, trust prompts, and permission prompts;
- decides escalations by having a `DECISION` posted (§7.6);
- decides whether to continue after a `STOPPED` (§7.7);
- instructs the merge (§7.10) and decides what happens when the base branch moved.

The Implementer's pane is the human console. Nothing important is reported only in a Reviewer pane.

### 4.2 Implementer

The Implementer is the Developer's session. It:

- owns the issue worktree and is its only writer;
- implements, validates, commits, and pushes;
- opens the PR with the two fixed body sections (§7.2) and updates the report on each push;
- launches one fresh Reviewer per review pass and closes it after consuming its result;
- evaluates every finding independently, records dispositions on the threads, and fixes valid findings;
- relays `needs_human` verdicts, `needs-human` dispositions, and stops to the Developer;
- reports "approved at `<sha>`, ready to merge" and merges only on the Developer's instruction;
- acts on the forge as the Implementer identity.

### 4.3 Reviewer

The Reviewer is a fresh session per review pass, launched by the Implementer in a detached worktree at the exact head and named as in §8.1. It:

- verifies the review target before relying on anything else;
- reads the PR body, every decision, every prior tagged review, and every thread;
- re-runs the probes saved in the per-PR scratch directory against the new head;
- verifies each disposition by execution, never by trusting the reply;
- reviews independently along the two axes of the installed `code-review` skill (§12.4);
- posts one formal review as the Reviewer identity, then hands off (§8.5) and goes idle;
- never modifies a tracked file; writes only inside the per-PR scratch directory and Git-ignored validation output;
- is closed by the Implementer; it does not outlive its review pass.

A per-PR Reviewer that persists across passes is the fallback only if Increment 2 finds a trust prompt on every pass that cannot be avoided (§13.3). The conventions of §7 work unchanged for both models.

### 4.4 Trust and security

v0.4.4 §16 is retained. In particular the skills, the CLI, and the handoff templates are control-plane content (v0.4.4 §16.3): changes to them are reviewed manually, and pull requests to Agent Squad itself keep the current manual review pipeline until v0.5.0 has passed its own live trials (§16.4 of this delta). The tool never answers a trust or permission prompt and never sends keys to an agent UI (v0.4.4 §16.4).

### 4.5 Forge identities

- The configuration names two forge accounts: `implementer.forge_account` and `reviewer.forge_account` (§9). In v0.5.0 they MUST differ; `doctor` fails otherwise.
- Every forge mutation is performed by exactly one of them, selected with `--as implementer` or `--as reviewer` (§10.1).
- The CLI resolves the token for an identity by running `gh auth token --user <account>` and passing it to `gh` through the child environment variable `GH_TOKEN`. It MUST NOT print a token, write it to a file, or run `gh auth switch`.
- Before its first mutation in a process, the CLI MUST verify that `GET /user` with the resolved token returns the configured login; a mismatch is a failure, not a warning.
- Git pushes use the repository's ordinary Git transport and credentials; they are not forge mutations in the sense of this section.

---

## 5. Runtime Layout

This section supersedes v0.4.4 §17 and the run-identity parts of §18.

### 5.1 Control root

The control root is `<primary-worktree>/.agent-squad/`, where the primary worktree is the working tree that contains the Git common directory (`git rev-parse --git-common-dir`). It holds `config.json` (§9) and, by default, the two roots below. It holds no state file, no lock, no run directory, and no artifact.

Every `agent-squad` command MAY run from any worktree of the repository, including a review worktree; it discovers the control root through the common directory. A bare repository is unsupported. A command MUST refuse to run when the control root is missing or the configuration is invalid, with guidance to run `agent-squad init`.

The suggested layout is:

```text
<primary-worktree>/.agent-squad/
├── config.json
├── worktrees/
│   ├── issue-<N>/                      implementation worktree for issue N
│   └── reviewer-pr<N>-<sha7>/          detached review worktree for one review pass
└── review-scratch/
    └── pr<N>/                          per-PR scratch directory for Reviewer probes
```

### 5.2 Worktree root and path conventions

`worktree_root` (default `.agent-squad/worktrees`, resolved against the primary worktree when relative) holds every worktree the tool creates:

- the implementation worktree for issue `<N>` is `<worktree_root>/issue-<N>`;
- the review worktree for a review target is `<worktree_root>/reviewer-pr<N>-<sha7>`, where `<sha7>` is the first seven hexadecimal characters of the head SHA; the directory name equals the Reviewer name of §8.1.

Worktrees nested under the excluded control directory are ordinary linked Git worktrees: the outer `git status` stays clean, `git worktree list` registers them, and `git worktree remove --force` removes them. The default keeps them inside the launch directory so a sandboxed harness whose writable root is its launch directory can still write to them. An absolute `worktree_root` outside the repository is permitted.

### 5.3 Scratch root

`scratch_root` (default `.agent-squad/review-scratch`) holds one directory per PR, `<scratch_root>/pr<N>`. `reviewer launch` creates it. Reviewers write probe scripts, harnesses, and notes there so the next Reviewer can re-run them against the new head. It survives across review passes and is removed by `pr merge` after a successful merge (§15). Its contents are never authoritative; a later Reviewer treats them as an untrusted aid, exactly like the implementation report.

### 5.4 Git exclusion

`agent-squad init` MUST add `.agent-squad/` and `.agent-squad-review/` to the repository's local Git exclude (the common directory's `info/exclude`) when absent, as in v0.4.4 §17.4. Because the exclude lives in the common directory it covers every linked worktree, including nested ones. `.agent-squad-review/` is retained as the Git-excluded location for any Reviewer-local validation output inside a review worktree. The committed `.gitignore` is not modified.

### 5.5 Root validation

When a resolved root lies inside any worktree of the repository, it MUST be under that worktree's `.agent-squad/` directory. `worktree_root` and `scratch_root` MUST differ and neither MAY contain the other. `init` and `doctor` prove both roots creatable and writable.

### 5.6 Implementation constraints and layout

v0.4.4 §12 and §13 are amended:

- no `fcntl` lock and no atomic state files are required; `config.json` is still written with a temporary file and `os.replace()`;
- `gh` is invoked as an external executable found on `PATH`, with argument arrays and `shell=False`; the installed runtime still requires no third-party library;
- kept and adapted: `cli.py`, `initialization.py`, `herdr.py`, `doctor.py`, `preflight.py`;
- added (names are suggestions): `forge.py` (GitHub adapter, §11), `conventions.py` (grammars, parsing, derived state, §7), `anchors.py` (§11.2), `worktrees.py`, `templates.py` (handoff lines, §8), and the packaged skill files `skills/squad-implementer/SKILL.md` and `skills/squad-reviewer/SKILL.md` (§12);
- removed in Increment 5: run and round state, storage locks, bundle artifacts, submissions, review submissions and applications, marker recovery, and their tests.

The `make test`, `make smoke`, and `make doctor` targets are preserved.

---

## 6. Git Revision Model

v0.4.4 §22 is amended as follows.

### 6.1 Review target

A review target is:

```text
pr=<N> head=<full-sha> base=<full-sha>
```

- `head` is the PR head SHA as reported by the forge at launch time.
- `base` is `git merge-base origin/<base_branch> <head>`, computed by `reviewer launch` after `git fetch origin <base_branch>`; it is the merge-base at review time, not a base fixed at the start of the loop (this supersedes v0.4.4 §22.2).
- Iterations are never identified by round number.

### 6.2 Exact object IDs

v0.4.4 §22.1 is retained: every stored or posted SHA is the full object ID in the repository's object format (40 or 64 lowercase hexadecimal characters). `<sha7>` appears only in Reviewer names and worktree directory names and is never used for comparison.

### 6.3 Reviewed scope and ancestry

The reviewed scope is `git diff <base> <head>`, which equals the forge's three-dot diff between the base branch and the head. `base` is an ancestor of `head` by construction; `reviewer launch` MUST refuse a target whose base equals its head (an empty scope). If the base branch advanced since an earlier review, the next review still uses the current merge-base; the moved base matters only at merge time (§7.10).

### 6.4 Push and cleanliness

Before `reviewer launch`:

- the implementation worktree MUST have no uncommitted tracked changes (v0.4.4 §22.7 retained);
- its `HEAD` MUST equal the PR head SHA reported by the forge, which proves the push;
- its checked-out branch MUST be the PR's head branch.

Force-pushing a PR branch is not forbidden, but every rule in §7 operates on whatever the current head is; a `DISPOSITION fixed <sha>` whose SHA is no longer reachable from the head is invalid (§7.5).

### 6.5 Permitted Git operations

v0.4.4 §22.10 is amended. The tool and the Implementer skill MAY:

- push the PR branch;
- merge the PR through the forge, on the Developer's instruction (§7.10);
- delete the merged branch locally and remotely after a verified merge;
- remove worktrees the tool created, with force when needed (§15).

Everything else in v0.4.4 §22.10 remains forbidden: no reset, rebase, or branch switch of a checkout the tool did not create, no broad destructive cleanup, and no networked submodule initialization. v0.4.4 §22.9 (submodules) is retained: `doctor` warns.

---

## 7. Pull Request Conventions

This section supersedes v0.4.4 §21, §23.6, §24 to §28, and §31. Every rule here is derivable from the PR, Git, and Herdr.

### 7.1 Tagged lines

All protocol lines share these rules:

- The protocol tag is `AGENT_SQUAD/0.5.0`. Only this exact tag is recognized in v0.5.0.
- A tagged line MUST be the first line of the review body, comment body, or reply body that carries it. Tokens are separated by exactly one ASCII space; the line has no leading whitespace and no text after its last token.
- Grammar notation: `<N>` and `<n>` are positive decimal integers without leading zeros; `<full-sha>` is a full object ID (§6.2); alternatives are written `a|b`; square brackets in a grammar line mark an optional token, except in the finding line of §7.4, where they are literal characters.
- Parsers MUST match the whole line and MUST be exact. A first line that starts with `AGENT_SQUAD/`, `[REV-`, `DISPOSITION`, `VERIFIED`, or `NOT FIXED` but does not parse is *malformed*. Malformed lines are reported by `status` and `pr reviews` as diagnostics and are ignored for every derivation.
- Authorship is part of validity. A `REVIEW` header counts only when posted by the Reviewer identity; a finding thread root counts when posted by the Reviewer identity, or by either identity through `thread open` for a finding that a tagged review lists (§7.4); `DISPOSITION` lines count only from the Implementer identity; `VERIFIED` and `NOT FIXED` lines count only from the Reviewer identity; a `DECISION` counts only from the Implementer identity or from a login listed in `developer_accounts` (§9), which is how the Developer's own login is authorized when it differs from the Implementer identity; a `STOPPED` counts from either identity. Tagged lines by any other author, including collaborators and bots, are diagnostics and have no effect on the budget, the gates, or the Task.
- "Newer" and "latest" compare the forge's creation timestamps (`submitted_at` for reviews, `created_at` for comments); equal timestamps are ordered by ascending forge ID.
- The CLI composes every header it posts from command arguments (§10.2), so a header written by a skill never reaches the forge unvalidated.

### 7.2 PR body

The Implementer opens the PR with two fixed level-2 sections, in this order, with these exact headings:

```markdown
## Task

## Implementation report
```

- `## Task` holds the objective and acceptance criteria derived from the issue, plus constraints and non-goals when the issue states them, in the shape of v0.4.4 §21.1. The Developer approves it once, before coding. It is the task snapshot of v0.4.4 §21.2 and MUST NOT change silently. A material change to the objective or acceptance criteria is made only on the Developer's instruction, with `decision post --task`, which posts a `DECISION` with `finding=none` containing the complete amended section and mirrors it into this section (§7.6). Reviews older than that decision reviewed the earlier Task: they still count towards the budget, and an approval among them is no longer valid (§7.10).
- `## Implementation report` holds, under level-3 headings, the v0.4.4 §28.3 fields: `Summary`, `Scope`, `Files changed`, `Design decisions`, `Validation performed`, `Known limitations`, and `Areas worth extra review`. The Implementer updates it on each push with `pr report`. The Reviewer treats it as an untrusted aid and verifies material claims.
- `pr create` composes the body from the two section files, appends `Closes #<issue>` so the forge closes the issue on merge, and uses the issue title unless `--title` is given. Further content MAY follow the two sections.
- `pr create` and `pr report` MUST refuse a body in which either heading is missing, duplicated, or out of order.

### 7.3 Formal review

Each review pass is exactly one formal PR review submitted by the Reviewer identity against the exact head, with `commit_id` equal to the head. Its body starts with the header:

```text
AGENT_SQUAD/0.5.0 REVIEW pr=<N> head=<full-sha> base=<full-sha> verdict=<verdict>
```

```text
<verdict> = approved | changes_requested | needs_human
```

Validity rules, enforced by `review post` before posting and by `status` when reading:

- `head` MUST equal the review's forge `commit_id` and, at posting time, the PR head; otherwise the review is malformed or refused.
- `base` MUST be an ancestor of `head` and of the current tip of the base branch; it is the merge-base the Reviewer was launched with (§6.1).
- `approved` requires that this review lists no blocking finding and that every blocking finding from earlier tagged reviews is settled (§7.5).
- `changes_requested` requires at least one blocking finding listed by this review or at least one `NOT FIXED` verification posted during this pass.
- `needs_human` requires that the body's `## Summary` names the Developer decision that is required.

**The header is the authoritative verdict.** When the Reviewer identity is distinct from the Implementer identity, as it is in v0.5.0, the forge review state MUST mirror the verdict: `APPROVE` for `approved`, `REQUEST_CHANGES` for `changes_requested`, and `COMMENT` for `needs_human`. `status` reports a review whose forge state does not mirror its header as a mismatch and treats the review as not approving; nothing repairs the mismatch automatically. When only one identity is available, as in the single-identity mode deferred to v0.6.0 (§19), the review is submitted as `COMMENT` and formal approval comes from a human colleague; the header needs no change for that mode.

`review post` composes the header from its arguments and prepends it to the body file, which starts at `## Summary`. The body contains these level-2 sections in this order; the first three are REQUIRED:

```markdown
## Summary
## Verified dispositions
## Findings
## Standards
## Spec
## Evidence
```

- `## Summary` states the verdict in prose and, for `needs_human`, the decision required.
- `## Verified dispositions` lists every earlier blocking thread with its verification line (`REV-<n>: VERIFIED fixed`, `VERIFIED rejection accepted`, or `NOT FIXED`), mirroring the thread replies of §7.5; it says `none` when there is nothing to verify.
- `## Findings` lists each finding opened by this review as `REV-<n> [blocking|optional] <title>`, or says `none`; `review post` generates it from the threads it posts. This list is the authoritative association between a review and its findings (§7.4): a finding belongs to the tagged review that lists its ID, whether its thread was created together with the review or afterwards.
- `## Standards` and `## Spec` carry the two-axis summaries of the installed `code-review` skill (§12.4); `## Evidence` lists the commands the Reviewer ran. These three are RECOMMENDED.
- `## Unanchored findings` is present only when the review was published through the fallback of §11.1. It holds the complete text of every finding of that review, starting with each finding line, and is written in the same forge call as the rest of the body.

A review body MUST NOT be edited after submission. Findings live in threads, not in the body; the `## Unanchored findings` section that the fallback of §11.1 writes together with the review is the exception that keeps a finding's full text on the PR until its thread exists (§7.4).

### 7.4 Findings

Each substantive or optional finding is one inline review thread anchored to a diff line of the reviewed scope. The root comment's first line is:

```text
[REV-<n>][<severity>][<category>] <title>
```

```text
<severity> = blocking | optional
<category> = [a-z][a-z0-9-]*
<title>    = one non-empty line
```

Recommended category values: `correctness`, `regression`, `security`, `reliability`, `spec`, `standards`, `tests`, `docs`, `scope`, `maintainability`, `performance`.

The rest of the root comment carries the v0.4.4 §28.4 fields as bold labels, one paragraph each: **Problem**, **Evidence**, **Impact**, **Required change**, **Verification**. For an optional finding, **Required change** describes the suggested change. GitHub `suggestion` blocks MAY be included; each SHOULD have been trial-applied and validated before posting.

**Anchors.** A thread anchors to a right-side line of `git diff <base> <head>`, that is an added or context line, identified by `path`, `line`, and optionally `start_line` for a range. `review post` MUST validate every anchor locally against that diff and MUST refuse to post an anchor outside the commentable set, because the forge silently drops such comments (§11.2). A finding about content that is not in the diff is anchored to the most relevant commentable line and says so under **Evidence**.

**ID allocation.** Finding IDs are sequential across the PR. `review post` allocates the next ID as one plus the largest `<n>` among all finding IDs on the PR, whether they appear in a root comment's finding line or in a tagged review's `## Findings` or `## Unanchored findings` list, regardless of author, review, or resolution state; the first finding on a PR is `REV-1`. The Reviewer supplies severity, category, title, anchor, and body for each thread in the `--threads` file; `review post` prepends the finding line with the allocated ID. Under `--resume <review-id>` no ID is allocated: each `--threads` entry is matched in order to the identified review's `## Findings` list and keeps the ID listed there, so the "same list" check of §11.1 compares count, severity, and title. The same defect MUST NOT receive a second ID: a later Reviewer that revisits an existing finding replies on its thread.

**Association.** A finding belongs to the tagged review whose `## Findings` list names its ID (§7.3), not to the forge review record that happens to hold its root comment. Its thread is the root comment on the PR whose finding line carries that ID; there is at most one such root. This holds whether the root was created together with the review, by the fallback of §11.1, or by `thread open`.

**Unanchored findings.** A finding is *unanchored* when a tagged review lists it but no root comment on the PR carries its ID; the state is derived from the missing root, never from the body text. It arises only through the fallback of §11.1, which writes the complete text of every finding of that review, starting with each finding line, under `## Unanchored findings` in the same forge call that publishes the review, before any root is attempted, so the full problem, evidence, impact, required change, and verification survive an interruption at any later point. An unanchored finding keeps its ID, severity, and place in the review's list and is never dropped. When blocking, it counts as an unsettled blocking finding: it blocks approval and, having no thread for a disposition, blocks the next launch. When optional, it blocks nothing and stays visible and recoverable, as §7.5 requires. The recovery route is `thread open --pr <N> --finding REV-<n> --path <path> --line <line> [--start-line <line>]`, run by the Reviewer identity in the same pass or by either identity later, which validates the anchor locally and posts a root comment consisting of the finding line and the body copied from the review's `## Unanchored findings` entry; from then on the finding has an ordinary thread. `status` lists every unanchored finding and reports `open_threads` as the next action only while a blocking one exists (§7.9).

**Severity.** Blocking findings are the substantive actionable findings of §12.3; optional findings are advisory and never block approval.

### 7.5 Dispositions and verification

Before requesting the next review, the Implementer MUST reply on every blocking thread that is not settled. The reply's first line is one of:

```text
DISPOSITION fixed <full-sha>
DISPOSITION rejected
DISPOSITION needs-human
```

- `fixed` is followed by the rationale and the exact verification command. `<full-sha>` names a commit that contains the fix; it MUST be reachable from the head of the next review and MUST NOT be reachable from the head the finding was raised against.
- `rejected` is followed by concrete evidence: the existing code or test that already satisfies the finding, or the reason the finding is wrong.
- `needs-human` is followed by the decision that requires Developer authority. It MUST NOT be used to obtain another automatic review: `reviewer launch` refuses while such a disposition has no newer `DECISION` naming that finding (§7.6).

`thread reply` validates the first line against this grammar and the role rules of §7.1 before posting.

The next Reviewer verifies each disposition by execution and replies on the thread with a first line of:

```text
VERIFIED fixed
VERIFIED rejection accepted
NOT FIXED
```

followed by what it ran. After `NOT FIXED`, the Implementer MUST post a new disposition before the next launch. The latest disposition on a thread governs, and the latest verification governs.

**Settled threads.** A thread is settled when its latest verification is `VERIFIED fixed` or `VERIFIED rejection accepted`, or when a `DECISION` with `finding=REV-<n>` for that thread is newer than its latest disposition and a later Reviewer has replied `VERIFIED fixed` or `VERIFIED rejection accepted` after checking compliance with the decision. A settled thread is closed for later Reviewers unless new evidence appears; a later Reviewer MUST NOT reopen it merely because it would have judged differently. New evidence is raised as a new finding that cites it. `thread resolve` marks settled threads resolved on the forge where the API allows it; resolution state is a convenience for readers, never authority.

**Same-head reconsideration.** When every open blocking thread's latest disposition is `rejected` and no code changed, the next review targets the same head. `reviewer launch` accepts a head equal to the latest tagged review's head only in that case, or when a Task amendment (§7.6) is newer than that review, because the amended Task must be reviewed even if the code did not change; a `fixed` disposition requires a changed revision (v0.4.4 §26.5 and §26.13, retained in PR form).

**Optional threads.** Optional findings MAY receive a one-line disposition or none. An optional thread never blocks a launch or an approval. After a merge the Implementer SHOULD reply "not pursued" or point to the follow-up issue on any optional thread left open, and `thread resolve` MAY resolve it.

### 7.6 Decisions

A Developer decision is a PR conversation comment, posted by the Developer or by the Implementer quoting the Developer, with the header:

```text
AGENT_SQUAD/0.5.0 DECISION finding=<REV-n|none> [budget=<n>]
```

- `finding=REV-<n>` settles the question raised by that thread; the thread is the question, and the latest decision naming it is authoritative for it.
- `finding=none` records a *general decision*: an approach, an interpretation, a Task amendment (§7.2), a continuation after a stop (§7.7), or a budget-only extension. General decisions are cumulative: every general decision remains in force, and a later general decision supersedes an earlier one only where its body says so explicitly and links the earlier decision comment. A budget-only or continuation decision therefore never erases an unrelated design or task decision, and a Reviewer applies all general decisions together.
- The body records the Developer's actual decision and any constraints. A general decision that amends the Task (§7.2) MUST contain the complete amended `## Task` section, so the amendment and its time are derivable from the PR without consulting edit history: a decision body that contains a level-2 `## Task` heading is a *Task amendment*. The *effective Task* is the `## Task` section of the latest Task amendment, or the PR body's section when there is none; Reviewers review against the effective Task. `decision post --task` posts the amendment first and then mirrors the section into the PR body for readers (§10.2); a mirror that lags behind is reported by `status` as `task_body_stale` together with the command to re-run, and never changes the effective Task.
- `budget=<n>` sets the effective review budget for this PR to `<n>` (§7.8). `decision post` MUST refuse a value that is not greater than the number of reviews already used.
- Every Reviewer reads all decisions before reviewing, treats the latest decision on a question as authoritative for that question, verifies that the implementation complies with it, continues to report defects within the decided approach, and does not re-litigate the decided choice (v0.4.4 §26.17 and §29, retained).
- A decision MUST NOT silently redefine the task (v0.4.4 §21.5): a material task change is an explicit edit of `## Task` accompanied by a `finding=none` decision that says so.

### 7.7 Stop

Whichever role stops the loop posts a PR conversation comment with the header:

```text
AGENT_SQUAD/0.5.0 STOPPED head=<full-sha> reason=<reason>
```

```text
<reason> = budget | repeat | scope | design | ambiguity | judgement
```

followed by a summary of the remaining problems or recurring issues. `head` is the latest reviewed head. The reason vocabulary is the current Reviewer prompt's review-loop guard and early-stop conditions, kept verbatim:

| `reason` | Current prompt condition **[verbatim]** |
| --- | --- |
| `budget` | "If the third completed review still has substantive actionable findings that would require another implementation/review cycle" — with "third" read as the effective budget of §7.8 |
| `repeat` | "the same substantive finding repeatedly remains unresolved"; "reviewer and implementer repeatedly disagree about a substantive issue that cannot be resolved from the code or stated requirements alone" |
| `scope` | "addressing the findings would require significant scope expansion" |
| `design` | "the implementation requires architectural or design reconsideration rather than another local fix"; "successive revisions repeatedly introduce new substantive findings, suggesting that the underlying approach may be flawed" |
| `judgement` | "approval requires product, architecture, security, requirements, or other judgment that should be made by the user" |
| `ambiguity` | "the reviewed revision or PR state is ambiguous enough that continuing risks reviewing or approving the wrong code" |

Rules:

- While the latest `STOPPED` is newer than the latest `DECISION`, `reviewer launch` MUST refuse. Any `DECISION` posted after the stop, including one with `finding=none`, lifts the gate; that is how the Developer's decision to continue is recorded.
- A Reviewer that stops posts its formal review first when it completed one, then the `STOPPED` comment, then `handoff stopped` (§8.5). It MUST NOT send a normal fix request.
- The Implementer, on receiving a stop or discovering one through `status`, makes no further review-driven changes, does not request another review automatically, preserves the PR, branch, commits, and worktree, and waits for the Developer's decision about whether to continue, change approach, or terminate the work (the current manual-intervention guard, **[verbatim]** in substance; §12.2 rule 9).
- Optional findings alone never justify a stop.

### 7.8 Budget

```text
effective = the budget= value of the latest DECISION that carries one, else max_review_passes (default 3)
used      = the number of tagged reviews by the Reviewer identity on the PR, any verdict, any head
remaining = effective - used
```

- `reviewer launch` MUST refuse when `remaining` is not positive, with the message that a `DECISION` with `budget=` is required.
- The Reviewer skill runs `status` before starting and MUST NOT start a review when `remaining` is not positive; it posts `STOPPED` with `reason=budget` instead and hands off.
- When a review with verdict `changes_requested` makes `used` equal to `effective`, the Reviewer MUST post `STOPPED` with `reason=budget` after its review and hand off with `handoff stopped` rather than `handoff review-result`.
- The count includes reviews at heads that were later replaced and reviews at the same head (reconsideration). It never includes malformed reviews, reviews by other authors, or plain comments.

### 7.9 Derived state

`status --pr <N>` derives the loop's state from these inputs and nothing else: the PR record (head SHA, head branch, base branch, open, merged, merge commit), the local worktrees (`git worktree list --porcelain`), the configuration, the PR's reviews and their comments, the PR's review threads, the PR's conversation comments, and the live Herdr agents when Herdr is reachable.

Derived facts:

- **Target.** `pr`, the current head, the head branch, and `base` recomputed as in §6.1.
- **Tagged reviews.** Every valid `REVIEW` header with its forge ID, `commit_id`, state, verdict, and whether it is *current* (`commit_id` equals the PR head).
- **Threads.** For each finding: ID, severity, category, title, anchor, the opening review, the latest disposition, the latest verification, whether it is settled, and the forge resolution state.
- **Decisions and stops** in timestamp order.
- **Budget** as in §7.8.
- **Evidence.** The complete PR body with its `## Task` and `## Implementation report` sections; the full body of every tagged review; every finding with its root comment body and every reply body; every decision and stop body; each with author login, forge ID, and timestamp, so that a Reviewer can perform every check in §12.3 through this output alone.
- **Gates.** `stopped` (the latest `STOPPED` is newer than the latest `DECISION`); `needs_decision` (the latest tagged review has verdict `needs_human` with no newer `DECISION`, or an unsettled blocking thread's latest disposition is `needs-human` with no newer `DECISION` naming it); `unanchored_findings` (a blocking finding is unanchored, §7.4); `unaddressed_findings` (an unsettled blocking thread has no disposition newer than its latest verification); `same_head_requires_rejections` (§7.5); `task_amended` (a Task amendment is newer than the latest tagged review, §7.6); `budget_exhausted`; `not_pushed` (§6.4); `reviewer_live` (a live Herdr agent is named for the current head).
- **Approval** as in §7.10, with the list of reasons when the head is not agent-approved.
- **Diagnostics.** Malformed tagged lines, forge-state mismatches, optional unanchored findings, incomplete reviews (a tagged review with an unanchored finding, §7.4, reported with its forge ID; the repair is `review post --resume` while the review is current and `thread open` otherwise), and `task_body_stale` (the PR body's `## Task` differs from the effective Task, §7.6), each with the command that repairs it where one exists; diagnostics never gate an action.
- **Next action**, the first of these whose condition holds: `merged` (the PR is merged); `closed` (closed without merge); `approved` (agent-approved); `stopped`; `needs_decision` (that gate, or `budget_exhausted` when another review would be needed); `open_threads` (`unanchored_findings`); `address_findings` (`unaddressed_findings`); `push` (`not_pushed`, or `same_head_requires_rejections` without `task_amended`); `reviewer_live` (a Reviewer for the current head is live and no current review exists); `launch_review` (otherwise). Approval is evaluated before the gates that only prohibit launching another review: an approving review that is the first of one, the third of three, or the fourth of four after an extension yields `approved`, whereas a `changes_requested` review that exhausts the budget yields `stopped` once the Reviewer has posted its `STOPPED` (§7.8) and `needs_decision` if it has not.

`status` prints the next action first and the reasons that led to it. It MUST make a current review that the Implementer has not acted on prominent; that is how a lost Herdr notification is discovered (§8.7). `--json` prints the same facts as one object.

Every command re-derives the state when it runs. A review's forge ID identifies its publication; the review target and header do not uniquely identify it, because a fresh review at the same head is legitimate under §7.5. Re-running `review post --resume <review-id>` completes only that identified publication and never creates a second logical review (§11.1). A plain `review post` publishes a new formal review and increases the used budget, even when its header matches an earlier review (§7.8). A tagged review whose `commit_id` is not the PR head is not current; it is neither stale, invalid, nor superseded in the v0.4.4 sense, and no classification machinery exists for it.

### 7.10 Approval validity and merge

A revision is **agent-approved** when all of the following hold:

1. the latest tagged review has `verdict=approved`;
2. that review's forge `commit_id` equals the current PR head;
3. the PR head equals the `HEAD` of the implementation worktree, which is the registered worktree whose checked-out branch is the PR's head branch;
4. no `STOPPED` is newer than that review;
5. because the Reviewer identity is distinct in v0.5.0, that review's forge state is `APPROVED`; a review whose header says `approved` but whose state is not `APPROVED` is reported as a mismatch and is not repaired;
6. no Task amendment (§7.6) is newer than that review, because the review evaluated the earlier Task; an edit that touches only the `## Implementation report` section has no effect on approval. After a Task amendment the same head MAY be reviewed again without a code change (§7.5), and `status` reports `launch_review` when the budget permits (§7.8) and `needs_decision` otherwise.

The Implementer reports "approved at `<full-sha>`, ready to merge" and waits. Nothing merges automatically.

**Human approval.** Where the repository also requires a human approval or passing checks to merge, the report says so. `pr merge` derives this from the forge: it reads the PR's `mergeable_state` and the branch rules of the base branch when the forge exposes them (a plan that does not expose rules is treated as "no rule visible", not as an error). A merge the forge refuses for that reason is reported with the forge's message; the Developer approves on the forge and the merge is retried.

**Moved base.** If the base branch moved since the approving review's merge-base, the merged tree would be an unreviewed combination. `pr merge` MUST refuse unless invoked with `--accept-moved-base`; the Implementer reports the situation and leaves the choice to the Developer, who may instead ask for another review at the current merge-base.

**Merge.** On the Developer's instruction the Implementer runs `pr merge --as implementer --pr <N>`, which:

1. verifies the agent-approved condition and the moved-base rule at that moment;
2. merges through the forge with the configured `merge_method`, passing the approved head SHA as the SHA the PR head must still match, so the forge refuses a head that moved in between;
3. fetches and verifies integration: with `merge` (the default) the approved head MUST be an ancestor of the merge commit, so the approved SHA stays an ancestor of the base branch; with `squash` the tree of the merge commit MUST equal the tree of the approved head, which holds exactly when the base had not moved, so a squash merge accepted under `--accept-moved-base` is reported as "integration not verifiable by tree identity" rather than verified;
4. deletes the remote branch if the forge did not already, removes the implementation worktree, deletes the local branch, removes any remaining review worktrees for the PR, and removes the per-PR scratch directory;
5. reports each step and never modifies the base-branch checkout; it prints the fast-forward command for the Developer.

The `rebase` merge method is not supported in v0.5.0.

---

## 8. Herdr Handoff

This section supersedes v0.4.4 §23.9, §30, and §31, and amends v0.4.4 §10 and §14.

### 8.1 Reviewer name, worktree, and scratch directory

For a review target the Reviewer name is:

```text
reviewer-pr<N>-<sha7>
```

It satisfies Herdr's agent-name rule (`[a-z][a-z0-9_-]{0,31}`), is unique per review pass, and is the label of the Herdr worktree workspace. The review worktree is `<worktree_root>/reviewer-pr<N>-<sha7>` (§5.2) and the scratch directory is `<scratch_root>/pr<N>` (§5.3). A same-head reconsideration reuses the same name and path after the previous Reviewer has been closed.

### 8.2 Herdr surface

The adapter uses these installed Herdr commands and result types and verifies them against `herdr api schema --json` and the `--help` texts, as in v0.4.4 §14: `worktree open` (`worktree_opened`), `worktree remove` (`worktree_removed`), `agent start` (`agent_started`), `agent prompt` (`agent_prompted`), `agent get` (`agent_info`), `workspace get` (`workspace_info`), `workspace close` (`ok`), and `api snapshot` (`session_snapshot`). Agent lifecycle states are `idle`, `working`, `blocked`, `done`, and `unknown`. Herdr status is a scheduling hint; `agent prompt --wait` is never used as proof that a message was processed (v0.4.4 §10 retained). Observed Herdr versions are informative only and are not hard-coded.

### 8.3 Request: `reviewer launch`

`agent-squad reviewer launch --pr <N>` performs, in order:

1. fetches the base branch, derives the state (§7.9), and refuses on any gate: `not_pushed`, `stopped`, `needs_decision`, `unanchored_findings` (blocking findings only), `unaddressed_findings`, `same_head_requires_rejections` (unless `task_amended`), `budget_exhausted`; refuses when a live agent already carries the Reviewer name (use `reviewer adopt` or `reviewer close`);
2. computes the target (§6.1) and refuses an empty scope;
3. creates the detached review worktree at the head, or reuses an existing clean one at that head (`review-worktree create`), verifies its `HEAD`, and creates the scratch directory;
4. opens the worktree in Herdr and verifies that the opened path equals the review worktree:

   ```bash
   herdr worktree open --cwd <primary-worktree> --path <review-worktree> --label reviewer-pr<N>-<sha7> --no-focus
   ```

5. starts the Reviewer in the returned root pane:

   ```bash
   herdr agent start reviewer-pr<N>-<sha7> --kind <reviewer.kind> --pane <pane-id> [-- <reviewer.start_args>...]
   ```

6. delivers the request as one fixed line through `herdr agent prompt reviewer-pr<N>-<sha7> "<line>"` without `--wait`. For Claude Code the line is, verbatim:

   ```text
   /squad-reviewer pr=<N> head=<full-sha> base=<full-sha> implementer=<implementer.agent_name>
   ```

   For Codex the line is, verbatim:

   ```text
   $squad-reviewer pr=<N> head=<full-sha> base=<full-sha> implementer=<implementer.agent_name>
   ```

7. reads the Reviewer's state once with `herdr agent get` for blocked detection (§8.4);
8. prints the name, workspace and pane IDs, worktree path, target, delivery mechanism, and observed state.

Increment 2 tests whether a pasted skill invocation runs as a command in each harness. The fallback is passing the same line as the harness's initial prompt argument after `--` in step 5 and skipping step 6. The mechanism chosen per harness kind is a constant of the Herdr adapter, recorded in the Increment 2 evidence; both mechanisms are covered by fake-Herdr tests. No configuration field selects it.

### 8.4 Blocked detection

After launch or adopt, the Implementer reads the Reviewer's state once. If it is `blocked`, or if `agent start` returned `agent_not_ready`, the command exits with the retained-resources status (§10.1), keeps the Reviewer running, and reports the pane and workspace IDs so the Developer can answer the trust or permission prompt in that pane. The tool never sends keys. After the person answers, `reviewer adopt --pr <N>` delivers the request line to the now-idle Reviewer (§8.6). A Reviewer that is `working` or `idle` after delivery is left alone; the Implementer goes idle (§8.8).

### 8.5 Result and stop: `handoff review-result` and `handoff stopped`

The Reviewer posts on the PR first and sends second.

`agent-squad handoff review-result --pr <N> --head <full-sha> --verdict <verdict>` verifies that a tagged review by the Reviewer identity with that head and verdict exists on the PR, then sends the Implementer agent named in the configuration one line plus one sentence:

```text
AGENT_SQUAD/0.5.0 REVIEW_RESULT pr=<N> head=<full-sha> verdict=<verdict>
```

The sentence is fixed per verdict:

- `approved`: `Run agent-squad status --pr <N>, report the approval to the Developer, and do not merge without the Developer's instruction.`
- `changes_requested`: `Run agent-squad status --pr <N>, evaluate every blocking thread on the PR, and record dispositions before requesting another review.`
- `needs_human`: `Run agent-squad status --pr <N> and relay the decision required to the Developer.`

`agent-squad handoff stopped --pr <N> --head <full-sha> --reason <reason>` verifies that a `STOPPED` comment with that head and reason exists on the PR, then sends:

```text
AGENT_SQUAD/0.5.0 STOPPED pr=<N> head=<full-sha> reason=<reason>
Automated review has stopped; run agent-squad status --pr <N> and relay the reason and the remaining problems to the Developer.
```

Both commands target the Implementer by the configured name through `herdr agent prompt` without `--wait`. If the Implementer agent is not found or the prompt fails, the command fails with the Herdr error; the review or stop already exists on the PR and is discovered through `status` (§8.7). The Reviewer does not retry blindly (§13.2).

### 8.6 Adopt: `reviewer adopt`

`agent-squad reviewer adopt --pr <N>` finds the live agent named for the current head, verifies its kind and that its `cwd` is the review worktree, refuses if it is `blocked`, and delivers the request line of §8.3 step 6 again. It creates nothing. It exists for the blocked-at-startup case and for a launch whose prompt delivery failed after the agent started.

### 8.7 Lost notification

If a `REVIEW_RESULT` or `STOPPED` prompt never reaches the Implementer, the PR still holds the review or stop. The recovery path is the Developer saying "check the PR", on which the Implementer runs `status --pr <N>` and acts on the derived next action. No marker, outbox, or retry queue exists.

### 8.8 Asynchronous handoff discipline

Both skills keep the current prompts' rule. The Implementer skill carries the current Implementer prompt's text **[verbatim]**, where `reviewer` denotes the Reviewer launched for the current head, whose name is `reviewer-pr<N>-<sha7>`:

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

The Reviewer skill carries the current Reviewer prompt's counterpart, quoted in §12.3 rule 10.

### 8.9 Close: `reviewer close`

After consuming a result or a stop, the Implementer runs `agent-squad reviewer close --pr <N> [--head <full-sha>]`, which:

1. locates the Reviewer's workspace through the live agent, or through the Herdr worktree registration when the agent has already exited;
2. verifies through `workspace get` that the workspace still holds exactly one tab and one pane and that the pane is the Reviewer's pane; otherwise it reports and leaves everything in place;
3. removes the checkout and closes the workspace with `herdr worktree remove --workspace <id> --force`, falling back to `workspace close` plus `git worktree remove --force` when Herdr no longer registers the worktree;
4. verifies that `git worktree list` no longer lists the path.

Review worktrees hold nothing authoritative, so forced removal of a squad-created worktree is acceptable. `reviewer close` never touches the scratch directory, and it MUST refuse a workspace or worktree that the tool did not create.

---

## 9. Configuration (Schema Version 2)

This section supersedes v0.4.4 §19. The file is `<control-root>/config.json`:

```json
{
  "schema_version": 2,
  "forge": {"kind": "github", "owner": "MagiLand", "repo": "agent-squad"},
  "implementer": {"agent_name": "implementer", "kind": "codex", "forge_account": "patrickhe"},
  "reviewer": {"kind": "claude", "start_args": [], "forge_account": "patrick-magiland"},
  "developer_accounts": [],
  "base_branch": "main",
  "max_review_passes": 3,
  "merge_method": "merge",
  "worktree_root": ".agent-squad/worktrees",
  "scratch_root": ".agent-squad/review-scratch"
}
```

| Field | Type | Default | Validation |
| --- | --- | --- | --- |
| `schema_version` | integer | `2` | MUST equal 2; a schema 1 file is refused with guidance to move it aside and rerun `init` |
| `forge.kind` | string | `"github"` | MUST be `"github"` in v0.5.0; the field exists so v0.6.0 can add `"forgejo"` without a schema change |
| `forge.owner` | string | derived from the origin remote at `init` | non-empty; no `/`, whitespace, or null bytes |
| `forge.repo` | string | derived from the origin remote at `init` | non-empty; a trailing `.git` is stripped at `init`; same character rules |
| `implementer.agent_name` | string | `"implementer"` | MUST match `[a-z][a-z0-9_-]{0,31}` |
| `implementer.kind` | string | `"codex"` | `"claude"` or `"codex"` |
| `implementer.forge_account` | string | required, no default | non-empty; MUST differ from `reviewer.forge_account` |
| `reviewer.kind` | string | `"claude"` | `"claude"` or `"codex"` |
| `reviewer.start_args` | array of strings | `[]` | each non-empty, no null bytes; passed to `agent start` after `--` |
| `reviewer.forge_account` | string | required, no default | non-empty |
| `developer_accounts` | array of strings | `[]` | forge logins, besides the Implementer identity, whose directly posted `DECISION` comments count (§7.1); each non-empty and different from `reviewer.forge_account` |
| `base_branch` | string | the remote's default branch at `init`, else `"main"` | non-empty; no whitespace, `..`, or null bytes; MUST exist on the remote |
| `max_review_passes` | integer | `3` | at least 1 |
| `merge_method` | string | `"merge"` | `"merge"` or `"squash"` |
| `worktree_root` | string | `".agent-squad/worktrees"` | a path; relative paths resolve against the primary worktree; §5.5 rules |
| `scratch_root` | string | `".agent-squad/review-scratch"` | same rules; MUST differ from `worktree_root` |

Rules:

- Unknown fields and wrong types are rejected by explicit standard-library validators, as in v0.4.4 §28.1.
- `init` derives `forge.owner` and `forge.repo` from the `origin` remote URL by taking the last two path components, which also covers SSH host aliases such as `git@github-magiland:MagiLand/agent-squad.git` because only the path is parsed. It sets `forge.kind` to `github` and verifies as the Implementer identity that `GET /repos/{owner}/{repo}` succeeds. `--owner`, `--repo`, and `--base-branch` override the derivation. The two `forge_account` values cannot be derived; `--implementer-account` and `--reviewer-account` are REQUIRED arguments of `init`.
- `init` MUST NOT overwrite an existing configuration; it validates it and reports differences from the defaults.
- Configuration MUST NOT become a workflow definition, a role registry, or a policy archive (v0.4.4 §19 retained in spirit).

---

## 10. CLI Surface

This section supersedes v0.4.4 §32. `agent-squad` remains a standard-library Python package with an installed `agent-squad` command.

### 10.1 Common rules

- **No protocol state.** No command writes a file that records a verdict, disposition, decision, stop, budget, or handoff status. The only things the CLI writes locally are `config.json`, worktrees, scratch directories, and installed skill copies.
- **Identity.** Every forge mutation takes `--as implementer|reviewer` (§4.5). Read-only forge commands accept `--as` and default to `reviewer` when run inside a squad review worktree and to `implementer` otherwise.
- **Context.** Every command discovers the control root through the Git common directory (§5.1) and validates the configuration before doing anything else.
- **Output.** Human-readable output by default; `--json` prints one JSON object. Errors are one line on standard error, prefixed `error:`.
- **Exit status.** `0` success; `1` failure (validation, forge, Herdr, or Git error); `2` usage error; `3` resources retained (a blocked Reviewer, a workspace or worktree that could not be verified as squad-owned, a cleanup step that was skipped); `4` a protocol gate refused the action (§7.9 gates, budget, approval validity). The skills branch on these codes.
- **Timeouts.** Herdr calls keep the existing adapter timeout; forge calls time out and fail rather than hang.

### 10.2 Command table

| Group | Command | Acts as | Derives | Fails when |
| --- | --- | --- | --- | --- |
| Setup | `init --implementer-account <login> --reviewer-account <login> [--owner] [--repo] [--base-branch]` | implementer (one verification read) | forge owner and repo from the remote; base branch from the remote default | not a Git worktree; remote not parseable; repository not readable; the two accounts are equal; an existing configuration is invalid |
| Setup | `doctor [--live-reviewer]` | both (reads) | the checks of §10.3 | any check fails; `--live-reviewer` leaves a Reviewer running (exit 3) |
| Setup | `skill install [--claude] [--codex] [--force]` | none | packaged skill contents | a differing skill file or symlink exists and `--force` is absent (§12.1) |
| Forge | `issue view --issue <N>` | read | issue title, body, labels, comments | issue not readable |
| Forge | `pr create --as implementer --issue <N> --task <file> --report <file> [--title <text>]` | implementer | head branch from the implementation worktree; base branch from configuration; body per §7.2 | sections invalid; branch not pushed; a PR for the branch already exists |
| Forge | `pr report --as implementer --pr <N> --report <file>` | implementer | the replaced `## Implementation report` section | sections invalid; PR not open |
| Forge | `pr head --pr <N>` | read | head SHA and branch, base branch, merge-base, open and merged state | PR not readable |
| Forge | `pr reviews --pr <N>` | read | tagged reviews with header fields, forge state, and current flag; malformed reviews as diagnostics | PR not readable |
| Forge | `review post --as reviewer --pr <N> --head <sha> --base <sha> --verdict <verdict> --body <file> --threads <file> [--resume <review-id>]` | reviewer | the header; finding IDs (§7.4); anchors (§11.2); verdict consistency (§7.3). Without `--resume` it always creates a new formal review, even when an earlier review carries the same header, because a same-head reconsideration or a re-review after a Task amendment legitimately repeats it (§7.5). With `--resume <review-id>` it creates no review and only posts the roots missing from that identified review (§11.1) | head is not the PR head; base invalid; anchors invalid; verdict inconsistent with threads; a root still fails after the §11.1 fallback; with `--resume`, the identified review is not a tagged review by the Reviewer identity with this header and this `## Findings` list |
| Forge | `thread reply --as <role> --pr <N> --finding REV-<n> --body <file>` | either | the thread's root comment from the finding ID | the first line is a tagged line that violates §7.1 or §7.5; finding unknown |
| Forge | `thread open --as <role> --pr <N> --finding REV-<n> --path <path> --line <line> [--start-line <line>]` | either | the finding's text from the tagged review that lists it as unanchored (§7.4); the anchor validated (§11.2) | finding unknown or already has a thread; anchor invalid |
| Forge | `thread resolve --as reviewer --pr <N> --finding REV-<n>` | reviewer | the thread node ID | the thread is blocking and not settled |
| Forge | `decision post --as implementer --pr <N> --finding <REV-n\|none> [--budget <n>] [--task <file>] --body <file>` | implementer | the header; with `--task`, a Task amendment whose body is the decision text followed by the complete amended `## Task` section, posted first and then mirrored into the PR body (§7.6); resumable: when the latest decision is already a Task amendment with the same section, it posts nothing and only re-applies the mirror | `budget` not greater than `used`; finding unknown; the task file is not a complete `## Task` section; the mirror fails after the decision was posted (exit 3; `status` reports `task_body_stale`) |
| Forge | `stop post --as <role> --pr <N> --head <sha> --reason <reason> --body <file>` | either | the header | reason outside the vocabulary; head not a full SHA |
| Forge | `pr merge --as implementer --pr <N> [--accept-moved-base]` | implementer | approval validity, moved base, merge verification (§7.10) | not agent-approved (exit 4); base moved without the flag (exit 4); the forge refuses; verification fails; a cleanup step fails (exit 3) |
| Derived state | `status --pr <N> [--json]` | read | everything in §7.9, including under `--json` the full PR body and every review, thread, reply, decision, and stop body with author and timestamp | PR not readable |
| Reviewer lifecycle | `review-worktree create --pr <N> --head <sha>` | none | the worktree path of §5.2; an existing clean worktree at that head is reused | head not present locally; the path exists and is not a clean worktree at that head |
| Reviewer lifecycle | `review-worktree remove --pr <N> --head <sha>` | none | the worktree path | the path is not a squad worktree |
| Reviewer lifecycle | `reviewer launch --pr <N>` | read | §8.3 | any gate (exit 4); Herdr failure; blocked (exit 3) |
| Reviewer lifecycle | `reviewer adopt --pr <N>` | read | §8.6 | no live Reviewer for the head; blocked (exit 3) |
| Reviewer lifecycle | `reviewer close --pr <N> [--head <sha>]` | none | §8.9 | workspace not squad-owned or not isolated (exit 3) |
| Handoff | `handoff review-result --pr <N> --head <sha> --verdict <verdict>` | read | §8.5 | no matching review on the PR; Implementer agent not found |
| Handoff | `handoff stopped --pr <N> --head <sha> --reason <reason>` | read | §8.5 | no matching stop on the PR; Implementer agent not found |

This is the whole surface. It is not an invitation to expose internal steps as commands.

### 10.3 `doctor`

Deterministic checks (v0.4.4 §20.1 amended):

- inside a non-bare Git worktree; control root and configuration valid; the local exclude contains both patterns;
- `gh` on `PATH`; `gh auth token --user` succeeds for both accounts; `GET /user` returns each configured login; the two logins differ; the repository is readable by both; the Reviewer identity has write permission;
- `base_branch` exists on the remote;
- Herdr on `PATH`; schema and live protocol consistent; the required methods and result types of §8.2 present; the Herdr socket reachable from the current process; integration current for both configured kinds; a live agent named `implementer.agent_name` of kind `implementer.kind` exists (a warning when absent, because the Developer's session may be started later);
- both roots creatable and writable; a disposable detached worktree can be created under `worktree_root` and removed;
- the two skill files installed and byte-identical to the packaged versions; the `~/.claude/skills` symlinks present; the installed `code-review` skill present (§12.4);
- orphaned resources: worktrees under `worktree_root` whose name matches the review convention but whose PR is merged or closed, live agents whose name matches the convention for a merged or closed PR, and scratch directories for merged PRs; reported with paths and never removed automatically (v0.4.4 §20.4 and §34.4 retained);
- `.gitmodules` present: warning (v0.4.4 §22.9).

`doctor --live-reviewer` creates a disposable detached worktree at the current `HEAD`, opens it in Herdr, starts the configured Reviewer kind with `reviewer.start_args`, waits for it to be interactive, records whether a trust or permission prompt appeared, closes the workspace, and removes the worktree. It proves start, readiness, trust inheritance, and close; it does not send a review request. Resources that cannot be closed safely are reported and retained (exit 3).

### 10.4 Reads used by the skills

The skills obtain every fact through `status --pr <N> --json`, `pr head`, `pr reviews`, and `issue view`; they do not call `gh` directly and never see a token. `status --json` is the complete evidence interface: it carries the PR body, so the Reviewer takes its spec from the effective Task (§7.6) there rather than from the issue, and it carries every review, thread, reply, decision, and stop body with author and timestamp (§7.9), so a fresh Reviewer can verify every disposition, read every decision, and inspect every finding's evidence without any other read.

---

## 11. Forge Adapter

### 11.1 GitHub through `gh api`

The adapter invokes the `gh` executable found on `PATH`. v0.4.4 §14 applies: it verifies `gh --version` and `gh auth token --user` at `doctor` time and does not hard-code observed versions. It uses:

- **PR record:** `GET /repos/{owner}/{repo}/pulls/{N}` for `head.sha`, `head.ref`, `base.ref`, `base.sha`, `state`, `merged`, `merge_commit_sha`, and `mergeable_state`; `POST /repos/{owner}/{repo}/pulls` to create; `PATCH .../pulls/{N}` to update the body.
- **Merge-base:** computed locally with Git after fetching the base branch; `GET .../compare/{base}...{head}` (`merge_base_commit.sha`) MAY cross-check it.
- **Reviews:** `POST .../pulls/{N}/reviews` with `commit_id`, `body`, `event` (`APPROVE`, `REQUEST_CHANGES`, or `COMMENT`), and `comments[]` of `{path, line, side: "RIGHT", start_line?, start_side?, body}`. On a batch rejection the adapter re-validates the anchors and posts the review with the same event and no comments, with the body extended by a `## Unanchored findings` section that holds the complete text of every finding, starting with each finding line, so that this first successful write persists all evidence on the PR (§7.4); its `## Findings` list keeps the association. It then posts each root with `POST .../pulls/{N}/comments` including `commit_id`; the forge attaches such comments to synthetic reviews without headers, which are not tagged reviews and never count. A root that still fails is reported, the command exits 1, and the Reviewer re-anchors it with `thread open` (§7.4). The review body is never edited afterwards, nothing already posted is deleted, and the review remains one logical review. Resumption is explicit: `review post --resume <review-id>` identifies the interrupted review by its forge ID, verifies that it is a tagged review by the Reviewer identity with the same header and the same `## Findings` list, posts no second review, and creates only the roots that are missing, so an interruption at any point is recovered without a second logical review. A plain `review post` never suppresses a review on the strength of a matching header: a fresh pass at the same head (§7.5) is a new formal review that MUST be published and counted (§7.8), and after a Task amendment it is the fresh approval's submission time that restores approval validity (§7.10).
- **Stranded drafts:** before posting, `GET .../pulls/{N}/reviews` is scanned for a `PENDING` review by the Reviewer identity; one is submitted with `POST .../pulls/{N}/reviews/{id}/events` and `event: COMMENT`, never deleted, because a pending draft blocks a new review and may hold finished replies.
- **Threads and replies:** `POST .../pulls/{N}/comments/{root_id}/replies` posts a reply. Enumeration goes per review, `GET .../pulls/{N}/reviews/{id}/comments` for every review, because the flat `GET .../pulls/{N}/comments` listing can omit replies; the flat listing supplies `line`, `start_line`, and `side` for anchors, which the per-review listing reports as `null`. Thread node IDs and resolution state come from GraphQL `repository.pullRequest.reviewThreads`, paginated, and resolution uses the `resolveReviewThread` mutation.
- **Conversation comments:** `GET` and `POST .../issues/{N}/comments` for decisions and stops.
- **Merge:** `PUT .../pulls/{N}/merge` with `merge_method` and `sha` set to the approved head; `DELETE .../git/refs/heads/{branch}` when the branch survives the merge.
- **Identity:** `GET /user` per §4.5.

Responses are parsed as JSON with explicit validators; forge error messages are surfaced verbatim in the one-line error. The adapter does not retry mutations automatically; a failed mutation is reported and the caller re-derives the state.

### 11.2 Anchor validation

Anchor validation is the productized form of the existing `anchors.py` helper: parse the unified diff of `git diff <base> <head>` into, per file path, the set of right-side line numbers that are added or context lines. Deleted lines and `\ No newline at end of file` markers are excluded; renamed and new files use their new path; binary files have no commentable lines. `review post` accepts an anchor only when `path` is in the set and `line` and `start_line`, when present, are both in that file's set with `start_line` not greater than `line`. Validation runs before any forge call. Unit tests cover multiple hunks, new files, deleted files, renames, binary files, and files without a trailing newline.

### 11.3 Fake forge

A committed fake forge serves the automated tests, like the committed fake Herdr: an executable named `gh`, placed ahead of `PATH` by the tests, that implements `gh --version`, `gh auth token --user <account>` (returning a distinct fake token per account), and the subset of `gh api` REST paths and GraphQL queries listed in §11.1. It keeps a scripted PR model in a JSON file named by an environment variable, applies mutations to it, records every call with the `GH_TOKEN` it received, and can be told through fixture settings to reject a batch review, drop an out-of-diff anchor silently, omit replies from the flat listing, report a pending draft, refuse a merge whose `sha` does not match, or refuse an approval from the PR author. Automated tests never call the real forge.

---

## 12. Skills

This section supersedes v0.4.4 §35. Two skills, each a directory with one `SKILL.md`, carry the role instructions. Their content is control-plane material under v0.4.4 §16.3.

### 12.1 Packaging and installation

- The skill files are packaged with the CLI as data files and installed by `agent-squad skill install`. The tool never touches the Herdr skill, any other skill, or any `AGENTS.md` or `CLAUDE.md`.
- Install location: `~/.agents/skills/<name>/SKILL.md`, with a relative symlink `~/.claude/skills/<name>` pointing to `../../.agents/skills/<name>`. Codex reads `~/.agents/skills` directly, so one copy serves both harnesses; this is how the Herdr and `code-review` skills are installed on the reference machine.
- `--codex` performs only the copy; `--claude` performs only the symlink; without either flag both are performed.
- `skill install` refuses to overwrite a `SKILL.md` whose content differs from the packaged one unless `--force` is given, and never replaces a symlink that points elsewhere without `--force`.
- `doctor` verifies that both installed copies are byte-identical to the packaged versions.
- Each `SKILL.md` starts with frontmatter naming the skill and describing when to use it: `squad-implementer` triggers when the Developer invokes it and says "Let's start on issue #N"; `squad-reviewer` triggers only through the request line of §8.3.

### 12.2 `squad-implementer`: mandatory rules

An outline; each rule is mandatory content of the skill text. Markers refer to the current Implementer prompt.

1. **Task statement [new].** Read the issue with `issue view`; draft `## Task` (objective, acceptance criteria, constraints, non-goals); present it to the Developer once and wait for approval; then work autonomously until the first handback.
2. **Implementation workflow [adapted].** Use the dedicated worktree `<worktree_root>/issue-<N>` on a branch following the consuming repository's naming policy (use `<type>/issue-<N>-<slug>` when no policy is specified); understand the issue and relevant existing code before making changes; implement the requested change without unnecessary scope expansion; run the relevant tests, checks, and validation; commit and push; open the PR with `pr create` including both sections; update the report with `pr report` on every later push; list changes to agent instruction or control-plane files under "Areas worth extra review".
3. **Request a review [adapted].** Run `reviewer launch --pr <N>`; on exit 4 report the gate to the Developer; on exit 3 tell the Developer which pane needs an answer and later run `reviewer adopt`; never send keys.
4. **Asynchronous handoff [verbatim]** as quoted in §8.8, after a successful `reviewer launch` or `reviewer adopt`.
5. **Handling review feedback [adapted].** On a `REVIEW_RESULT` prompt or a "check the PR" instruction, run `status --pr <N>` first and act on the derived next action; read the review directly from the GitHub PR, including inline threads and suggestions, and treat the PR as the authoritative source; independently evaluate each substantive actionable finding; fix findings that are valid; do not change the code merely to satisfy findings that are incorrect, inappropriate, already resolved, or no longer applicable; if `status` reports `open_threads`, open a thread for each blocking unanchored finding with `thread open` before anything else, and for optional ones when convenient; record a `DISPOSITION` reply on every blocking thread with `thread reply`; run the relevant tests and validation after making changes; commit and push; update the report; then `reviewer close` for the finished Reviewer and `reviewer launch` for the new head.
6. **Non-blocking and optional findings [verbatim]:**

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

7. **Needs-human and decisions [adapted].** On a `needs_human` verdict, or before posting a `needs-human` disposition, relay the decision required to the Developer; record the Developer's answer with `decision post`, quoting the Developer; only then request another review. When the Developer amends the Task, record it with `decision post --finding none --task <file>`, which posts the complete amended section as a general decision and mirrors it into the PR body (§7.6); if `status` reports `task_body_stale`, re-run the same command; then request a review of the current head even if no code changed.
8. **Approval and merge [adapted].** Verify approval on the PR through `status`, never from the Herdr message alone; report "approved at `<full-sha>`, ready to merge" and wait; when the Developer instructs the merge, run `pr merge`; if the base moved, report it and ask; after a successful merge, report the merge commit and the cleanup performed. If the approved SHA is no longer the current PR head, do not merge based on that approval; the newer revision must be reviewed.
9. **Manual-intervention guard [adapted].** On a `STOPPED` prompt, or when `status` reports `stopped`: make no further review-driven changes; do not request another review automatically; preserve the PR, branch, commits, and worktree; report the reason and remaining problems to the Developer; wait for the Developer's decision about whether to continue, change approach, or terminate the work; record a continuation as a `DECISION` before launching again.
10. **Handoff discipline [adapted].** Herdr messages are the fixed lines of §8; identify code states by PR number and full commit SHA, never by round number; GitHub remains the authoritative source for implementation history, review findings, inline discussion, suggestions, and finding disposition.
11. **Sandbox [new].** If the harness sandbox blocks the Herdr socket or a write outside the worktree, request escalated permission for that exact command once and report the failure rather than retrying blindly (§13.2).

### 12.3 `squad-reviewer`: mandatory rules

An outline; markers refer to the current Reviewer prompt.

1. **Verify the target [adapted].** Parse `pr=`, `head=`, `base=`, and `implementer=` from the request line; confirm that `git rev-parse HEAD` equals `head`, that `base` is an ancestor of `head`, and that `status --pr <N>` reports the same head; keep that revision pinned throughout the review and do not silently switch to a newer PR head; if the PR head has moved, post nothing, report the mismatch in the pane, and stop. Then read the derived next action (§7.9): continue only when it is `launch_review` or `reviewer_live`; when `status` reports an incomplete review by the Reviewer identity at `head`, complete it as in rule 7; otherwise the request is a duplicate delivery (§14): post nothing, report it in the pane, and stop, because a plain `review post` would publish and count a second review (§7.8).
2. **Budget guard [adapted].** Read the budget from `status`; if `remaining` is not positive, post `STOPPED` with `reason=budget` and run `handoff stopped`.
3. **Read the PR first [adapted].** Read `## Task`, the report (untrusted), every `DECISION`, every prior tagged review, and every thread through `status --json`; apply all general decisions together and the latest decision per finding (§7.6); treat settled threads as closed (§7.5); open a thread with `thread open` for every blocking unanchored finding, and for optional ones when it can; re-run the probes saved in `<scratch_root>/pr<N>` against the new head and save new ones there.
4. **Verify dispositions by execution [new].** For every blocking thread with a disposition newer than its last verification, run the stated verification, inspect the cited evidence, and reply `VERIFIED fixed`, `VERIFIED rejection accepted`, or `NOT FIXED` with what was run; never accept a reply on trust.
5. **Two-axis review [adapted].** Run the installed `code-review` skill as described in §12.4 from the review worktree, with `base` as the fixed point and the effective Task (§7.6) as the spec; inspect the implementation for correctness, regressions, relevant edge cases, maintainability, and compliance with the issue and repository requirements; verify material claims in the report.
6. **Severity and follow-up policy [verbatim]:**

   > A substantive actionable finding is one that reasonably requires resolution before the reviewed revision should be approved, such as a correctness defect, regression risk, security or reliability concern, meaningful maintainability problem, violated requirement, or other material engineering issue.
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

   Substantive actionable findings become `blocking` threads and everything else `optional` (§7.4). v0.4.4 §29 remains the review policy.
7. **Publish [adapted].** Write the review body (§7.3) and the threads (§7.4) to files in the scratch directory; trial-apply every suggestion; run `review post` with the verdict; if it exits 1 with unanchored findings, re-anchor every blocking one with `thread open` before handing off; after an interruption, run `status`: an incomplete review by the Reviewer identity at `head` is completed with `review post --resume <review-id>` and the same files, never with a plain `review post`, which would publish a second review; a complete current review with this header means publication finished and the pass continues with the next step; when neither exists, nothing reached the PR and the plain `review post` is repeated; then `thread resolve` for each thread verified in this pass; publish substantive actionable findings only on the GitHub PR.
8. **Hand off [adapted].** Run `handoff review-result` with the exact head and verdict; or, when the loop must stop, `stop post` and then `handoff stopped`. Do not duplicate detailed findings in Herdr messages.
9. **Early stop [verbatim conditions, adapted mechanics].** Stop before the budget is spent under the conditions of §7.7, using the vocabulary there; record the stop on the PR; after the review that exhausts the budget with substantive findings remaining, stop with `reason=budget`.
10. **Asynchronous handoff [verbatim]:**

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

    Here `implementer` denotes the agent named in the request line.
11. **Never write tracked files [adapted].** Probe scripts and harnesses go in the scratch directory; validation output only in Git-ignored locations; the Reviewer does not modify the implementation code.
12. **Sandbox [new].** As in §12.2 rule 11, for `handoff`, `review post`, and scratch-directory writes.

### 12.4 Invoking the installed `code-review` skill

Claude Code ships a built-in command also named `code-review`, so the wrapper MUST invoke the installed two-axis skill unambiguously: it reads `~/.agents/skills/code-review/SKILL.md` by that path and follows the process written there, and it MUST NOT type `/code-review` or `$code-review`. It supplies the fixed point as the `base` SHA, so the skill's three-dot diff equals the reviewed scope, and the spec as the PR's `## Task` section. It presents the two axes under the `## Standards` and `## Spec` sections of the review body and turns each finding into a thread with the severity of §12.3. `doctor` checks that the file exists and that its frontmatter `name` is `code-review`.

---

## 13. Harness Specifics

### 13.1 Claude Code

- Skill prefix `/`; the request line is `/squad-reviewer ...` (§8.3).
- Whether a pasted slash command sent through `herdr agent prompt` runs as a command is the Increment 2 experiment; the initial-prompt fallback applies otherwise.
- `--add-dir` in `reviewer.start_args` is the place for an extra writable directory if one is needed for the scratch directory.

### 13.2 Codex

- Skill prefix `$`; the request line is `$squad-reviewer ...`.
- The sandbox may block the Herdr socket, as seen in the 2026-09-05 trials, and may block writes outside the launch directory. The skills say to request escalated permission for `agent-squad reviewer launch`, `agent-squad handoff ...`, and scratch-directory writes, once, and to report the failure rather than retry blindly.
- Worktrees under the launch directory keep the Implementer's writes inside the sandbox. `reviewer.start_args` is the place for a Codex `-c` override that widens writable roots if Increment 2 finds one necessary.

### 13.3 Both

- Trust and permission prompts are answered by a person; the tool never injects keys (v0.4.4 §16.4).
- In the 2026-09-05 trials neither harness recorded a trust entry for the review worktrees, only for the repositories they belonged to, which suggests both inherit trust for linked worktrees. Increment 2 confirms this with `doctor --live-reviewer` before the fresh-Reviewer-per-pass model is relied on; if a trust prompt appears on every pass and cannot be avoided, the per-PR persistent Reviewer fallback of §4.3 is adopted and recorded in the evidence.

---

## 14. Failure and Recovery Semantics

This section supersedes v0.4.4 §33. In every case the PR is preserved and re-derived; no local state can be corrupted because none exists.

| Condition | Behaviour |
| --- | --- |
| Forge unreachable or `gh` fails | The command fails with the forge message; nothing is posted or merged partially except as §11.1 describes; the caller retries later and re-derives |
| Token or account mismatch | The mutation is refused before any call; `doctor` explains which account is wrong |
| Herdr unavailable | `reviewer launch` fails after creating the worktree; the worktree is left and reported; a later `reviewer launch` reuses it |
| Reviewer blocked by a trust or permission prompt | Exit 3 with the pane; the Developer answers; `reviewer adopt` delivers the request |
| Reviewer dies or stalls before posting | Nothing is on the PR; `status` shows no live Reviewer and `launch_review`; the Implementer runs `reviewer close` and then `reviewer launch` for the same head; the budget is not consumed |
| Review posted but `REVIEW_RESULT` lost | `status` reports the current review and next action; the Developer's "check the PR" is the trigger (§8.7) |
| Stop posted but `STOPPED` prompt lost | `status` reports `stopped`; same recovery |
| Duplicate request delivery | The Reviewer skill checks `status`; a second review at the same head is posted only when the derived next action is `launch_review` (§12.3 rule 1); otherwise it reports the duplicate in its pane and posts nothing |
| Malformed tagged line | Reported as a diagnostic; ignored for derivation; the author posts a corrected comment |
| Review with a header but a mismatching forge state | Reported; not approving; the Reviewer identity posts a corrected review |
| PR head moved after launch | The Reviewer detects the mismatch (§12.3 rule 1) and posts nothing; the Implementer closes it and launches for the new head |
| Batch review rejected by the forge | §11.1 fallback: the first successful write carries every finding's full text; roots follow one by one; a root that fails exits 1 |
| Interruption during the fallback, or a blocking finding still unanchored after it | The finding's full text is already on the PR; `status` reports the incomplete review's ID, and `review post --resume <review-id>` posts the missing roots without a second review; either route recovers it: `--resume` re-posts every missing root from the threads file, `thread open` re-anchors one finding from PR data alone; approval and the next launch stay blocked until the finding is settled |
| Optional finding unanchored | Listed by `status` as a diagnostic and recoverable through `thread open`; blocks nothing |
| Task amendment posted but the PR body mirror failed | The effective Task is the amendment (§7.6); `status` reports `task_body_stale`; re-running the same `decision post --task` re-applies the mirror without posting a second decision |
| Tagged `DECISION` by an unauthorized author | Reported as a diagnostic; no effect on the budget, the gates, or the Task; the Developer or the Implementer posts the decision |
| Task amended after an approval | The approval is invalid (§7.10 condition 6); `status` shows `launch_review` for the same head, or `needs_decision` when the budget is exhausted; the next review evaluates the amended Task |
| Stranded pending draft | Submitted as a comment review before posting |
| Anchor outside the diff | Refused locally before posting |
| Base branch moved before merge | `pr merge` refuses without `--accept-moved-base`; the Developer decides |
| Forge refuses the merge (required approval, checks, head moved) | Reported with the forge's message; nothing local changes |
| Merge verification fails | Reported; branch and worktree retained; exit 1 |
| Cleanup step fails after a verified merge | Reported with the path; exit 3; `doctor` lists the residue |
| Orphaned worktree, agent, or scratch directory | Reported by `doctor`; never adopted; removed only by an explicit `reviewer close` or `review-worktree remove` |
| Submodule-dependent validation cannot run in the review worktree | Reported; the Reviewer says so in the review; the Developer decides (v0.4.4 §33.17 retained) |

---

## 15. Cleanup Policy

This section supersedes v0.4.4 §34.

- After consuming a result or a stop, the Implementer runs `reviewer close`; a Reviewer never outlives its review pass by design.
- Forced removal of a squad-created review worktree is acceptable because it holds nothing authoritative; the Reviewer's saved probes live in the scratch directory, not in the worktree.
- The per-PR scratch directory survives across passes and is removed by `pr merge` after a verified merge.
- After a verified merge the implementation worktree and branch are removed (§7.10).
- The tool never runs `git clean`, never removes a worktree it did not create, and never closes a Herdr workspace that holds anything besides the Reviewer it started.
- Residue is visible through `doctor`; automatic garbage collection of old Herdr or Git resources remains outside scope (v0.4.4 §34.4).

---

## 16. Testing Strategy

This section supersedes v0.4.4 §36. Automated tests use `unittest`, never call a real model, and never call the real forge or the real Herdr.

### 16.1 Unit tests

Required coverage:

- parsing and rendering of the `REVIEW`, `DECISION`, and `STOPPED` headers, the finding line, the `DISPOSITION` and verification lines, and the Herdr request, result, and stop lines, including rejection of every malformed variant (wrong tag, leading zeros, extra tokens, abbreviated SHAs, wrong case, missing fields);
- the authorship rules of §7.1, including an authorized direct decision, an Implementer-posted quoted decision, and a syntactically valid decision by an unrelated author, of which only the first two alter the budget, the gates, or the Task;
- cumulative general decisions: two independent general decisions, then a budget-only extension, then an explicit revision of one decision, after which only the revised question changes;
- next-action ordering: approval at used/effective 1/1, 3/3, and 4/4 after an extension yields `approved`, and a budget-exhausting `changes_requested` yields `stopped` or `needs_decision`;
- Task amendment: approval at head H against Task A is invalid after an amendment to Task B without a code change, and a report-only edit leaves it valid;
- fallback association: a `changes_requested` review whose batch failed but whose roots succeeded individually, one whose blocking root also failed, and one interrupted immediately after the body-only write, all keep one logical review and the finding's full text and ID on the PR and offer recovery through `review post --resume` or `thread open`; a plain `review post` after a same-head reconsideration or after a Task amendment publishes and counts a new review even though its header repeats an earlier one, and its submission time restores approval validity; an optional unanchored finding blocks neither approval nor a later launch;
- Task amendment recording: `decision post --task` posts the decision before the mirror, a failed mirror leaves the effective Task intact with `status` reporting `task_body_stale`, and re-running the command posts no second decision;
- finding-ID allocation across reviews, authors, and resolved threads;
- budget derivation, including `budget=` decisions, malformed reviews, and reviews at replaced heads;
- `STOPPED` and `DECISION` ordering, including equal timestamps;
- thread state derivation: dispositions, verifications, settled threads, `NOT FIXED`, `needs-human`, same-head reconsideration;
- approval validity, including each failing condition and the forge-state mismatch;
- moved-base detection and the squash tree-identity rule;
- anchor validation (§11.2);
- the request, result, and stop templates for both harness prefixes;
- configuration schema 2 validation, defaults, remote-URL derivation for SSH, SSH-alias, and HTTPS remotes, and schema 1 refusal;
- identity resolution: the right account per `--as`, no token in any output, no `gh auth switch`.

### 16.2 Integration tests

With the fake forge and the fake Herdr, in temporary repositories:

- nested worktree creation and removal under `.agent-squad/worktrees`, including forced removal and the outer status staying clean;
- `reviewer launch`, `adopt`, and `close`, including blocked at startup, `agent_not_ready`, prompt failure, `agent_not_found`, a workspace with an extra pane, and an already-exited Reviewer;
- token selection by role for every mutating command, asserted from the fake forge's call log;
- `review post` with the batch rejection fallback, a stranded pending draft, and the unanchored-findings append;
- reply enumeration per review when the flat listing omits replies;
- `thread resolve` through the fake GraphQL endpoint;
- `thread open` for an unanchored finding by each identity, with `status` reporting `open_threads` only while a blocking one exists, and `review post --resume` after an injected interruption, including its refusal when the identified review has a different header or list;
- `status` across scripted PR states covering every next action of §7.9;
- `pr merge` with `merge` and `squash`, a moved base with and without `--accept-moved-base`, a forge refusal, and a cleanup failure;
- `doctor` catching each misconfiguration the live trials hit (§17.4).

### 16.3 Smoke scenario

`scripts/run-smoke-tests` (`make smoke`) runs the whole loop in a temporary repository with the fake forge and the fake Herdr, driving both roles through the CLI with scripted content and no model:

1. `init`; `doctor`.
2. Create the issue worktree, commit a candidate, push to the fake remote, `pr create` with both sections.
3. `reviewer launch`; a scripted Reviewer posts a `changes_requested` review with two blocking findings and one optional finding, then `handoff review-result`.
4. `status` shows `address_findings`; `thread reply` records `fixed` and `rejected` dispositions; a `needs-human` disposition is shown to block `reviewer launch` until a `DECISION` names it.
5. Commit, push, `pr report`; `reviewer close`; `reviewer launch`; the scripted Reviewer verifies both dispositions, resolves the threads, and posts `approved`; `status` shows `approved`.
6. A later push invalidates the approval: `status` shows `launch_review`, not `approved`.
7. The third review, at the new head, posts `changes_requested`; with `max_review_passes` 3 the scripted Reviewer posts `STOPPED` with `reason=budget` and `handoff stopped`; `reviewer launch` is refused (exit 4).
8. `decision post` with `budget=4`; `reviewer launch` succeeds; the fourth review approves.
9. `pr merge` is refused while the base branch has been advanced on the fake remote, then succeeds with `--accept-moved-base`; integration is verified by ancestry with `merge`; a second PR on the same fake repository is then merged with `squash` on a base that has not moved, verifying tree identity; after each merge the issue worktree, branch, review worktrees, and scratch directory are gone.
10. Lost notification: with the fake Herdr set to fail `agent prompt`, a scripted review is posted and `handoff review-result` fails; `status` still reports the current review and the correct next action.
11. Fallback: with the fake forge set to reject the batch, a review is posted with every finding's full text and then its roots individually; with one blocking root also failing, `status` shows `open_threads`, `thread open` recovers the finding from PR data alone, and the loop continues with one logical review counted; an interrupted `review post` is completed with `--resume` or, when nothing reached the PR, repeated, and is counted once.
12. Cleanup verification: no tracked runtime files, no registered review worktrees, the temporary root removed, retained resources reported on failure.

The runner MUST NOT commit an intentional defect into a real development checkout, and it MUST work from a source export without `.git`, as today.

### 16.4 Live trials

Before v0.5.0 is released, on a disposable repository under the MagiLand organization, using `patrickhe` as the Implementer identity and `patrick-magiland` as the Reviewer identity:

- Codex implements and Claude Code reviews, then the reverse, each reaching approval and a human-gated merge from a single interactive session;
- at least one pass with blocking findings, dispositions, and verification by a fresh Reviewer;
- one `needs_human` decision recorded as a `DECISION` and respected by the next Reviewer;
- one stop and continuation;
- one lost notification recovered through `status`;
- one consumer trial on a real repository other than Agent Squad, documentation-only changes being acceptable, as in v0.4.4 §38.

Pull requests to Agent Squad itself keep the current manual review pipeline until these trials have passed.

### 16.5 Evidence record

Each increment that involves a live exercise adds `docs/verification/<YYYY-MM-DD>-issue-<N>.md` with these sections, in this order: **Implementation baseline** (the full SHA under test); **Commands executed** (a table of command and observed outcome, including `make test` counts and durations); **Live trials** (a table of direction, repository, PR number, reviewed heads, review IDs, verdicts, decisions, stops, merge commit, and merge method); **Harness versions** (Herdr, Codex, Claude Code, `gh`); **Defects found and fixed**; **Scope and limitations**, naming anything scripted rather than real; **Cleanup** (workspaces closed, worktrees removed, repositories archived or deleted, retained resources). Private raw evidence stays outside the repository, with archive digests listed.

---

## 17. Implementation Increments

Each increment is one issue under milestone v0.5.0 and one PR. Increment 0 is this document, done when merged.

### 17.1 Increment 1: GitHub adapter and derived state

Build the GitHub adapter (§11.1), the convention grammars and derivation (§7), anchor validation (§11.2), the fake forge (§11.3), configuration schema 2 and `init` (§9), and the commands `pr head`, `pr reviews`, `pr create`, `pr report`, `review post`, `thread reply`, `thread resolve`, `decision post`, `stop post`, `issue view`, and `status`. Done when the forge steps of the smoke scenario pass against the fake forge and `review post` has been exercised once on a real disposable GitHub PR as `patrick-magiland`, with the evidence recorded (§16.5).

### 17.2 Increment 2: Reviewer lifecycle

Build `review-worktree create` and `remove`, `reviewer launch`, `adopt`, and `close`, the handoff templates and both `handoff` commands (§8), blocked detection, and the delivery experiment (prompt after start versus initial prompt). Done when the fake-Herdr tests pass, one real Claude Code Reviewer and one real Codex Reviewer have been launched and closed, the delivery mechanism per harness is recorded, and trust inheritance for linked worktrees is confirmed or the persistent-Reviewer fallback adopted (§13.3).

### 17.3 Increment 3: Skills

Write both `SKILL.md` files (§12.2, §12.3), `skill install` (§12.1), and `pr merge` (§7.10). Done when a real loop on the disposable repository reaches approval and a human-gated merge from a single interactive session.

### 17.4 Increment 4: Doctor and preflight

Adapt `doctor` and `doctor --live-reviewer` to §10.3: two forge identities distinct and authorized, Herdr schema, socket reachability from the configured Implementer kind, worktree and scratch roots, skill installation, orphaned Reviewers and worktrees. Done when `doctor` catches each misconfiguration the live trials hit.

### 17.5 Increment 5: Smoke, documentation, removal, release

Build the smoke runner (§16.3); rewrite `README.md` and `docs/workflow-verification.md` for the PR-based loop; remove the v0.4.4 machinery listed in §5.6; set the version to 0.5.0; run the live trials (§16.4) and record the evidence (§16.5).

### 17.6 Definition of done

v0.5.0 is done when this developer experience works reliably:

```text
Developer says "Let's start on issue #N" and approves the task statement once
        ↓
Implementer implements, pushes, opens the PR, launches a fresh Reviewer
        ↓
Reviewer reviews the exact head and posts a tagged review with threads
        ↓
Implementer records dispositions, fixes, pushes, launches the next Reviewer
        ↓
Decisions and stops go through the Developer and are recorded on the PR
        ↓
"approved at <sha>, ready to merge" — the Developer says merge
        ↓
Implementer merges with the configured method and cleans up
```

with both role directions proven in disposable repositories, one real consuming repository, the lost-notification and decision paths exercised, and the anti-overengineering boundary preserved.

### 17.7 Instructions to the implementing agent

v0.4.4 §45 applies with item 11 replaced: do not improvise around the tagged-line grammars, the authorship rules, finding-ID allocation, disposition and verification semantics, the settled-thread rule, decision ordering, the stop gate, budget derivation, approval validity, the moved-base and squash rules, identity selection, or the fixed Herdr lines; those semantics are fixed by this delta. When a requirement is ambiguous, choose the interpretation that preserves exact-revision review, preserves human authority, makes stalls visible through `status`, avoids Developer message relay in the normal loop, and introduces the least new machinery.

---

## 18. Guarantees, Non-guarantees, and Simplifications

### 18.1 Reliability guarantees

Within the operating assumptions of §3.3, v0.5.0 MUST guarantee:

1. A review, disposition, decision, or stop exists on the PR before any Herdr message about it is sent.
2. A valid approval cannot apply to a head other than the one it was posted against, and a newer head is unreviewed until reviewed.
3. The Reviewer reads a detached worktree at the exact head, never the Implementer's working tree.
4. The Reviewer needs no write access to the implementation worktree.
5. The loop's state is fully recoverable from the PR, Git, and Herdr after any local interruption.
6. A lost Herdr notification never leaves the loop silently stalled: `status` reports the current review, stop, or next action.
7. Duplicate prompt delivery cannot create a duplicate logical review, budget effect, or state transition (§12.3 rule 1).
8. Every blocking finding is dispositioned before the next review, and every disposition is verified by execution before a thread is settled.
9. A `needs_human` verdict or a `needs-human` disposition cannot produce another automatic review without a recorded decision.
10. The review budget is derived from the PR and cannot be exceeded without a recorded decision.
11. A stop blocks further automatic reviews until a decision is recorded.
12. Nothing merges without the Developer's instruction; a merge is verified by ancestry or tree identity; a moved base is reported before merging.
13. Every forge mutation runs as exactly one configured identity, and no token is ever printed.
14. The normal loop proceeds without Developer message relay.
15. A `DECISION` by an author other than the Implementer identity or a configured Developer login has no effect.
16. A finding listed by a tagged review cannot be lost by a partial posting failure or an interruption, because its full text reaches the PR before any root is attempted, and approval cannot overlook a blocking one.
17. A material Task amendment invalidates every earlier approval.

### 18.2 Non-guarantees

v0.4.4 §40 is amended: the items about Herdr delivery, malicious same-user agents, multi-machine consistency, automatic recovery from every failure, self-review of control-plane changes, CI success, submodules, and model review replacing human review are retained. Added: no guarantee against forge outages, rate limits, or forge-side data loss; thread resolution state is not authority; the forge may reject a review for reasons outside the tool's control, in which case nothing is posted and the Reviewer retries; the tool does not verify that CI ran on a head.

### 18.3 Deliberate simplifications

v0.4.4 §41.1 to §41.4 no longer apply because there is no local state, lock, outbox, or delivery retry. v0.4.4 §41.5 to §41.9 are retained. Added:

- **The PR instead of local state.** Deriving everything from the PR removes the state file, the lock, the bundles, the markers, the classification of stale and invalid results, and the recovery commands that existed to keep them consistent.
- **Counting instead of a budget ledger.** Reviews are counted; extensions are decisions.
- **A fresh Reviewer instead of a persistent one.** The PR carries continuity; a per-PR scratch directory carries probes.
- **One forge adapter instead of a forge abstraction.** `forge.kind` exists for v0.6.0, but v0.5.0 has one adapter and no plug-in interface.
- **Fixed strings instead of templates.** The tagged lines and Herdr lines are literal grammars, not a template language.

---

## 19. Non-goals and Deferred Items

v0.4.4 §8 is retained in full. Additionally, v0.5.0 does not include:

- **Forgejo support, deferred to v0.6.0.** Facts gathered on 2026-09-07 for that release: the installed `fj` cannot create formal reviews or inline comments, so those need the Gitea-compatible REST endpoints with a token; Forgejo's API refuses approve or request-changes from the PR author with HTTP 422 (`preparePullReviewType`), and its UI disables those buttons; `fj` stores one credential per host. Codeberg remains available for public trials.
- **A single-identity mode, deferred to v0.6.0.** The client's Forgejo instance allows one account per person and requires formal approval from a separate human colleague, so v0.6.0 needs a mode in which the agent's verdict lives only in the `REVIEW` header, the review is submitted as a comment, and the human approval is checked separately. **This is why the review header, not the forge review state, is the authoritative verdict in v0.5.0:** a forge's review state cannot carry the agent's verdict when the Reviewer and the PR author share one account, and keeping the header authoritative now means that mode needs no convention change later. In v0.5.0 the forge state is a mirror and a consistency check.
- the `rebase` merge method;
- automatic merging, merge queues, or merging on a Herdr message alone;
- CI orchestration, or interpretation of check results as part of approval validity;
- stacked or dependent pull requests;
- automatic garbage collection of Herdr or Git resources;
- automated review of Agent Squad's own control-plane changes before the live trials pass;
- any forge other than GitHub, any harness other than Claude Code and Codex, and Windows.

---

## 20. Deviations from the Plan

**Departures from the plan's decisions (plan Section 9): none.** Every decision is carried unchanged: the PR is the authority and the CLI stores no protocol state; GitHub only, with Forgejo and the single-identity mode in v0.6.0 and the header authoritative for that reason; two skills installed for both harnesses from one location; a fresh Reviewer per pass with a per-PR scratch directory and the settled-thread rule, and per-PR persistence as the fallback; `squad-reviewer` wraps the two-axis `code-review` skill; merge commits by default, squash verified by tree identity; the task statement confirmed once; `patrickhe` implements and `patrick-magiland` reviews.

**Refinements this delta states that the plan did not.** None of these contradicts a plan decision; they are listed so that no reader mistakes them for silent departures:

1. `pr create`, `pr report`, and `pr merge` are added to the plan's Section 5.2 command table. The plan's identity rule ("every forge mutation takes `--as`, so no skill prose handles tokens") and its merge rule ("the tool verifies integration by tree identity") require the CLI, not the skill, to create, update, and merge the PR.
2. The control root is `<primary-worktree>/.agent-squad/` (§5.1) rather than v0.4.4's per-implementation-worktree root, because the plan places the per-issue and review worktrees inside `.agent-squad/worktrees/` of the launch directory.
3. A `needs_human` verdict or a `needs-human` disposition gates the next launch until a `DECISION` is recorded (§7.5, §7.9), carrying v0.4.4 §26.12 and §26.16 into PR form.
4. A same-head launch is accepted only when every open blocking disposition is `rejected` (§7.5), carrying v0.4.4 §26.5 into PR form.
5. `DECISION budget=` must exceed the used count (§7.6); a Reviewer whose `changes_requested` review exhausts the budget posts `STOPPED` with `reason=budget` itself (§7.8).
6. The finding category has a grammar and a recommended vocabulary (§7.4); the plan left `<category>` open.
7. `pr merge` requires `--accept-moved-base` to proceed on a moved base (§7.10); that is how "leaves the choice to the Developer" is enforced.
8. Read-only commands default to the identity implied by the current worktree (§10.1).
9. The `## Unanchored findings` fallback, the `thread open` recovery command, and the exit-status vocabulary (§7.4, §10.1, §11.1) are specified so that a partial posting failure cannot lose a finding and the skills can branch on outcomes.
10. An optional `developer_accounts` configuration field (§9), absent from the plan's example, authorizes a Developer login that differs from the Implementer identity to post decisions directly, which plan Section 3.6 allows; every other author's decision is a diagnostic.
11. General decisions are cumulative and a Task amendment must carry the amended section (§7.6), so that plan Section 3.6's "latest decision on a question" cannot let a budget extension erase an unrelated decision, and so that approval validity can require review against the amended Task (§7.10 condition 6).
12. A review's `## Findings` list is the authoritative association between a review and its findings (§7.3, §7.4), because the forge cannot attach a standalone comment to an existing review; the fallback persists every finding's full text in its first write, and `review post --resume <review-id>` completes an interrupted publication without suppressing a fresh same-head review (§11.1).
13. `decision post --task` owns a Task amendment together with its mirror into the PR body, and the effective Task is derived from the latest amendment (§7.6, §10.2), so the skills never need `gh` for a Task edit.

---

# Appendix A: Example End-to-End Run

*Informative.* Exact output may differ.

```bash
# Once per repository, in the primary checkout.
agent-squad init --implementer-account patrickhe --reviewer-account patrick-magiland
agent-squad skill install
agent-squad doctor
agent-squad doctor --live-reviewer
```

The Developer starts an interactive agent in Herdr, invokes `/squad-implementer` (or `$squad-implementer`), and says "Let's start on issue #41". The Implementer reads the issue, drafts `## Task`, and the Developer approves it. The Implementer works in `.agent-squad/worktrees/issue-41`, pushes `feat/issue-41-example`, and runs the commands below; the forge assigns the new pull request the number 42:

```bash
agent-squad pr create --as implementer --issue 41 --task task.md --report report.md
agent-squad reviewer launch --pr 42
```

Herdr opens `.agent-squad/worktrees/reviewer-pr42-1a2b3c4` in a background workspace, starts `reviewer-pr42-1a2b3c4`, and delivers:

```text
/squad-reviewer pr=42 head=1a2b3c4d... base=9f8e7d6c... implementer=implementer
```

The Reviewer verifies the target, reads the PR, runs the two-axis review, and posts:

```bash
agent-squad review post --as reviewer --pr 42 --head 1a2b3c4d... --base 9f8e7d6c... \
  --verdict changes_requested --body review.md --threads threads.json
agent-squad handoff review-result --pr 42 --head 1a2b3c4d... --verdict changes_requested
```

The Implementer receives `AGENT_SQUAD/0.5.0 REVIEW_RESULT pr=42 head=1a2b3c4d... verdict=changes_requested`, runs `agent-squad status --pr 42`, fixes `REV-1`, rejects `REV-2` with evidence, and replies on both threads:

```bash
agent-squad thread reply --as implementer --pr 42 --finding REV-1 --body rev1.md   # DISPOSITION fixed 5e6f7a8b...
agent-squad thread reply --as implementer --pr 42 --finding REV-2 --body rev2.md   # DISPOSITION rejected
agent-squad pr report --as implementer --pr 42 --report report.md
agent-squad reviewer close --pr 42 --head 1a2b3c4d...
agent-squad reviewer launch --pr 42
```

The next Reviewer, `reviewer-pr42-5e6f7a8`, verifies both dispositions by execution, replies `VERIFIED fixed` and `VERIFIED rejection accepted`, resolves the threads, posts a review with `verdict=approved` as an approving forge review, and hands off. The Implementer reports:

```text
approved at 5e6f7a8b..., ready to merge
```

The Developer says "merge". The Implementer runs:

```bash
agent-squad pr merge --as implementer --pr 42
```

which merges with a merge commit, verifies that `5e6f7a8b...` is an ancestor of the merge commit, deletes the branch, removes the issue worktree, the review worktree, and `.agent-squad/review-scratch/pr42`, and prints the command to fast-forward the local `main`.
