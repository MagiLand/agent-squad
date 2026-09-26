# Agent Squad v0.6.0

## Specification Delta: A Second Forge and a Single Identity

- **Version:** 0.6.0
- **Status:** Specification delta; Increment 0b, issue #55 of the [decided plan](agent-squad-v0.6.0-plan.md)
- **Baseline:** [Agent Squad v0.5.0](agent-squad-v0.5.0-spec.md), including its amendments through issue #74, at `fe65f39dadaf5033e435e2456759cc7cd565f78c`
- **Primary runtime:** Herdr
- **Supported coding agents:** Codex CLI and Claude Code
- **Supported forges:** GitHub through `gh`; Forgejo through the standard-library HTTP client (§11.4)
- **Reference implementation target:** Python 3.11 or later on macOS and Linux; no runtime dependencies

## 1. How to Read This Delta

This is a delta over v0.5.0 only, not a consolidated specification. The v0.5.0 dispositions of v0.4.4 remain in force. Section 2 gives every numbered v0.5.0 section and Appendix A a disposition:

- **Retained:** its text applies unchanged and is not repeated here.
- **Amended:** its text applies except where this delta says otherwise; each amended subsection is written in full below.
- **Superseded:** this delta replaces the identified section entirely.

The normative language of v0.4.4 §3 applies: **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, and **MAY** retain their defined meanings. Sections marked *Informative* are not independently normative. `v0.5.0 §N` names the baseline; `§N` alone names this delta. A retained subsection keeps its baseline number, including when referenced from this delta. Within copied text, references to retained sections resolve to that retained baseline text.

The skill-rule markers retain their baseline meanings: **[verbatim]** marks a rule quoted unchanged from the baseline prompts, **[adapted]** an adapted rule, and **[new]** a rule with no counterpart in those prompts.

The input is the whole v0.6.0 plan, with later decisions taking precedence where they explicitly amend earlier ones. Section 20 maps every decision and lists departures and evidence-driven refinements. The v0.5.0 text is not edited. Historical v0.5.0 release requirements remain historical, not new release gates. Increment numbers inside retained historical text refer to that baseline release; the new work order is exclusively §17.

The package version becomes `0.6.0` only in Increment 6. The protocol tag remains `AGENT_SQUAD/0.5.0`: no tagged-line or Herdr-line grammar changes. The release audit checks a declared protocol constant independently of the package version.

**Evidence notation.** `E1`–`E9` below refer to the named experiments in the committed [issue #54 evidence record](verification/2026-09-25-issue-54.md), on exactly Forgejo 16.0.3. `Setup` and recording numbers refer to the same record and its linked JSON. A link labelled `F16` identifies a source file at tag `v16.0.3`; it is source evidence, not a live trial. Product requirements are distinguished from claims about observed server behaviour. No ignored research or private archive is a project input.

## 2. Disposition of v0.5.0 Sections

The baseline's title, metadata, and Amendments list are historical context. Its §2 mapping of v0.4.4 continues to apply through the baseline. New subsections §11.0, §11.4, §16.6, and §17.8 are defined here.

A parent row governs only the parent's own introductory text; each subsection's row governs that subsection. Informative notes under retained headings explain the disposition without amending the retained rules.

| v0.5.0 section | Disposition | Where in this delta |
| --- | --- | --- |
| 1 How to Read This Delta | Superseded | §1: reading rules and protocol version |
| 2 Disposition of v0.4.4 Sections | Superseded | §2: this disposition table; baseline inheritance preserved |
| 3 What Changes | Amended | §3.1, §3.3, §3.4 |
| 3.1 The loop | Amended | §3.1: human approval wait |
| 3.2 Principles restated for the PR | Retained | — |
| 3.3 Operating assumptions | Amended | §3.3: forges and identity modes |
| 3.4 Terms | Amended | §3.4: neutral and approval terminology |
| 4 Roles, Identities, and Authority | Amended | §4.5; other roles and authority retained |
| 4.1 Developer | Retained | — |
| 4.2 Implementer | Retained | — |
| 4.3 Reviewer | Retained | — |
| 4.4 Trust and security | Retained | — |
| 4.5 Forge identities | Superseded | §4.5: identity modes and token sources |
| 5 Runtime Layout | Amended | §5.6; layout and ownership retained |
| 5.1 Control root | Retained | — |
| 5.2 Worktree root and path conventions | Retained | — |
| 5.3 Scratch root | Retained | — |
| 5.4 Git exclusion | Retained | — |
| 5.5 Root validation | Retained | — |
| 5.6 Implementation constraints and layout | Amended | §5.6: adapter boundary |
| 6 Git Revision Model | Amended | §6.5; exact object IDs and target unchanged |
| 6.1 Review target | Retained | — |
| 6.2 Exact object IDs | Retained | — |
| 6.3 Reviewed scope and ancestry | Retained | — |
| 6.4 Push and cleanliness | Retained | — |
| 6.5 Permitted Git operations | Amended | §6.5: guarded tracking-ref removal |
| 7 Pull Request Conventions | Amended | §7.1, §7.3–§7.5, §7.9–§7.10 |
| 7.1 Tagged lines | Amended | §7.1: neutral timestamps and shared-login authorship |
| 7.2 PR body | Retained | — |
| 7.3 Formal review | Amended | §7.3: mode-specific mirror rule |
| 7.4 Findings | Amended | §7.4: usable roots and body-first publication |
| 7.5 Dispositions and verification | Amended | §7.5: capability-based resolution and human reviews |
| 7.6 Decisions | Retained | — |
| 7.7 Stop | Retained | — |
| 7.8 Budget | Retained | — |
| 7.9 Derived state | Superseded | §7.9: capabilities, human decisions, draft gate, ordering |
| 7.10 Approval validity and merge | Superseded | §7.10: approval conditions and guarded cleanup |
| 8 Herdr Handoff | Retained | §8: #53 found no basis for a new wait |
| 8.1 Reviewer name, worktree, and scratch directory | Retained | — |
| 8.2 Herdr surface | Retained | — |
| 8.3 Request: `reviewer launch` | Retained | — |
| 8.4 Blocked detection | Retained | — |
| 8.5 Result and stop: `handoff review-result` and `handoff stopped` | Retained | — |
| 8.6 Adopt: `reviewer adopt` | Retained | — |
| 8.7 Lost notification | Retained | — |
| 8.8 Asynchronous handoff discipline | Retained | — |
| 8.9 Close: `reviewer close` | Retained | — |
| 9 Configuration (Schema Version 2) | Superseded | §9: compatible schema 2 extension |
| 10 CLI Surface | Amended | §10.2–§10.4 |
| 10.1 Common rules | Retained | — |
| 10.2 Command table | Superseded | §10.2: complete machine-readable command table |
| 10.3 `doctor` | Superseded | §10.3: per-forge and per-mode checks |
| 10.4 Reads used by the skills | Amended | §10.4: forge authority and existing CI-read allowance |
| 11 Forge Adapter | Superseded | §11: neutral boundary and two adapters |
| 11.1 GitHub through `gh api` | Amended | §11.1: unchanged transport and writes, neutral translations |
| 11.2 Anchor validation | Amended | §11.2: neutral anchors and range mapping |
| 11.3 Fake forge | Superseded | §11.3: both fake forges |
| 12 Skills | Amended | §12.2–§12.3; packaging and two-axis review retained |
| 12.1 Packaging and installation | Retained | — |
| 12.2 `squad-implementer`: mandatory rules | Amended | §12.2: forge authority and human approval wait |
| 12.3 `squad-reviewer`: mandatory rules | Amended | §12.3: capability check before resolution |
| 12.4 Invoking the installed `code-review` skill | Retained | — |
| 13 Harness Specifics | Retained | — |
| 13.1 Claude Code | Retained | — |
| 13.2 Codex | Retained | — |
| 13.3 Both | Retained | — |
| 14 Failure and Recovery Semantics | Superseded | §14: transport, draft, read-back and recovery failures |
| 15 Cleanup Policy | Amended | §15: tracking-ref cleanup; ownership guards retained |
| 16 Testing Strategy | Superseded | §16: retained coverage plus new rules and release trials |
| 16.1 Unit tests | Amended | §16.1: complete required unit coverage |
| 16.2 Integration tests | Amended | §16.2: complete integration coverage |
| 16.3 Smoke scenario | Amended | §16.3: twelve-step workflow on both fakes |
| 16.4 Live trials | Superseded | §16.4: four v0.6.0 trials |
| 16.5 Evidence record | Amended | §16.5: source and trial provenance |
| 17 Implementation Increments | Superseded | §17: #56–#61 and staged delivery |
| 17.1 Increment 1: GitHub adapter and derived state | Superseded | §17.1: #56, protocol and GitHub regression |
| 17.2 Increment 2: Reviewer lifecycle | Superseded | §17.2: #57, single identity |
| 17.3 Increment 3: Skills | Superseded | §17.3: #58, Forgejo reads |
| 17.4 Increment 4: Doctor | Superseded | §17.4: #59, Forgejo writes and tracking refs |
| 17.5 Increment 5: Smoke, documentation, release | Superseded | §17.5: #60, init and doctor |
| 17.6 Definition of done | Superseded | §17.6: #61, release |
| 17.7 Instructions to the implementing agent | Superseded | §17.7: definition of done; instructions move to §17.8 |
| 18 Guarantees, Non-guarantees, and Simplifications | Amended | §18.1–§18.3 |
| 18.1 Reliability guarantees | Amended | §18.1: exact-head approval in both modes |
| 18.2 Non-guarantees | Amended | §18.2: conventional role separation in single mode |
| 18.3 Deliberate simplifications | Amended | §18.3: bounded forge interface |
| 19 Non-goals and Deferred Items | Superseded | §19: non-goals and unverified behaviour |
| 20 Deviations from the Plan | Superseded | §20: v0.6.0 decisions and deviations |
| Appendix A Example End-to-End Run | Superseded | Appendix A: single identity on Forgejo |

## 3. What Changes

### 3.1 The loop

```text
Developer starts an issue; a ready issue is the Task
    ↓
Implementer implements, validates, pushes and creates the PR
    ↓
Fresh Reviewer checks the exact head, posts a tagged review, hands off, goes idle
    ↓
Implementer dispositions findings, fixes and obtains review of each new head
    ↓
Agent approval + (single mode: valid independent human approval)
    ↓
merge under standing instruction | approved: held or instruction absent
    ↓
Implementer verifies CI, merges, verifies integration, cleans up and reports
```

Decisions, stops, Task amendments, budgets, exact object IDs, fresh detached Reviewers, post-before-notify, and the one-writer rule are unchanged. The PR remains authority; Herdr remains a scheduling and delivery mechanism. In `single` mode an agent approval without the required human approval yields `await_human_approval`: the Implementer reports and goes idle. Human request-changes reviews are reported to the Developer, never translated into protocol findings. The approvals may arrive in either order (§7.10).

### 3.3 Operating assumptions

The v0.4.4 §6 assumptions apply, with these replacements for v0.5.0 §3.3:

- the repository uses a supported forge and an ordinary working Git transport;
- on GitHub, `gh` is installed and authenticated for the configured role account or accounts;
- on Forgejo, the configured server meets §11.4's minimum version and the role token files satisfy §9; supported transport is HTTPS, with HTTP allowed only for loopback tests;
- in `dual` mode the role accounts differ and the Reviewer has repository write permission; in `single` mode they are equal, the shared account has push permission, and at least one distinct human approver is configured;
- the forge is reachable while the loop runs;
- the Developer's interactive Implementer session starts inside Herdr in the primary checkout; one PR is driven per session, with separate issue worktrees and Reviewers for concurrent sessions;
- people answer trust and permission prompts and operate human approval accounts.

Outages, simultaneous Implementers editing one PR, and base branches forbidding the configured merge method remain unsupported. Minimum-version support is a product policy, not a claim of trials on every supported version (§19).

### 3.4 Terms

| Term | Meaning |
| --- | --- |
| Review target | `pr=<N> head=<full-sha> base=<full-sha>` (§6.1) |
| Tagged review | A formal review with a valid `REVIEW` header from the Reviewer identity |
| Current review | A tagged review at exactly the PR head |
| Finding, thread | An inline root with a valid finding line and usable anchor, associated by finding ID (§7.4) |
| Disposition, verification, settled thread | The Implementer's reply, the Reviewer's executed check, and the closure rule of §7.5 |
| Decision, general decision, Task amendment, effective Task | The unchanged records and derivation of §7.6 |
| Unanchored finding, incomplete review | A listed finding without a usable root; a review listing at least one such finding |
| Stop, budget | The unchanged records and derivation of §7.7 and §7.8 |
| Agent approval | A current tagged `approved` verdict satisfying the agent-side conditions of §7.10 |
| Merge-valid approval (`agent-approved` in retained command wording) | All §7.10 conditions, including the independent human approval in `single` mode |
| Human approval record | An adapter-normalized approve-or-request-changes review, including author, state, commit, dismissal, time and ID (§11.0) |
| Identity mode | `dual` (distinct role accounts) or `single` (one shared role account); independent of forge |
| Capability | An adapter-reported ability, never a forge-name test in protocol derivation |
| Control, worktree and scratch roots | The unchanged directories of §5.1–§5.3 |

## 4. Roles, Identities, and Authority

Sections 4.1–4.4 are retained. Their references to forge identities use §4.5 below; their existing manual review and trust rules are not relaxed.

### 4.5 Forge identities

- The configuration names `implementer.forge_account` and `reviewer.forge_account`. In `dual` mode they MUST differ; in `single` mode they MUST be equal (§9). Every mutation still selects a role with `--as implementer` or `--as reviewer`.
- In `single` mode role separation is by convention only. The forge sees one login and cannot establish which agent wrote a line. The §7.1 authorship sets collapse to that shared account. `DECISION` authorship remains the Implementer identity plus `developer_accounts`. A distinct configured human approver supplies the independent approval required by §7.10.
- The GitHub adapter resolves the selected account with `gh auth token --user <account>` and passes the value only to the child `gh` environment as `GH_TOKEN`. It MUST NOT print or persist the token or run `gh auth switch`.
- The Forgejo adapter reads the selected role's `token_file`, revalidates the file on each read, and sends `Authorization: token <t>` only to the configured API origin. The prescribed scopes are `write:repository`, `write:issue`, and `read:user`; the recorded identity reads and review writes used those scopes (Setup 002–004, E1–E9). It MUST NOT print the token, copy it to another file, or invoke `fj` for credentials.
- Before the first mutation in each process, each adapter MUST verify the token's login with `GET /user`; mismatch is a failure. No skill handles token values. Token errors identify the role and problem without exposing credentials.
- Git pushes use the repository's ordinary Git transport and credentials. The CLI does not configure SSH, replace remotes, or change forge identities in global state.

## 5. Runtime Layout

Sections 5.1–5.5 are retained, including resource ownership metadata and scratch lifetime.

### 5.6 Implementation constraints and layout

The implementation remains standard-library Python. Configuration uses a temporary file plus `os.replace()`; no workflow state, lock, outbox, or artifact store is introduced.

`forge.py` contains the typed protocol, neutral records, shared review vocabulary, errors, and factory of §11.0. Separate adapter modules implement GitHub subprocess transport and Forgejo standard-library `urllib.request` transport. Existing `cli.py`, `initialization.py`, `doctor.py`, `preflight.py`, `herdr.py`, `conventions.py`, `anchors.py`, `worktrees.py`, and `templates.py` retain their responsibilities. The three construction sites call `make_forge(repository, role)`; other annotations name `Forge`, not a concrete adapter. Forge wire payloads and state/event translation remain inside adapters. No framework, plugin registry, HTTP dependency, or additional role is introduced.

`gh` uses argument arrays and `shell=False`. Keep `dependencies = []`, the packaged skills, and `make test`, `make smoke`, and `make doctor`. The removed v0.4.4 run machinery stays removed.

## 6. Git Revision Model

Sections 6.1–6.4 are retained. In particular the review base is computed with local Git after fetch, never inferred from a forge's PR `base.sha`.

### 6.5 Permitted Git operations

v0.4.4 §22.10 is amended. The tool and the Implementer skill MAY:

- push the PR branch;
- merge the fetched base branch into the PR branch in the issue worktree after a moved-base refusal, resolve conflicts within the Task, validate, push, and obtain review of the new head (§7.10);
- merge the PR through the forge, on the Developer's instruction (§7.10);
- delete the merged branch locally and remotely after a verified merge;
- remove worktrees the tool created, with force when needed (§15).

Inside `pr merge`, the tool MAY also fast-forward the primary checkout's checked-out base branch to the verified base tip after a verified merge (§7.10). This does not authorize the Implementer to run a separate command that changes the base checkout.

After verified integration the tool MAY remove the exact remote-tracking ref for the merged PR branch, only under the absence and expected-SHA guards of §7.10.

Everything else in v0.4.4 §22.10 remains forbidden: no reset, rebase, or branch switch of a checkout the tool did not create, no broad destructive cleanup, and no networked submodule initialization. v0.4.4 §22.9 (submodules) is retained: `doctor` warns.

## 7. Pull Request Conventions

Sections 7.2, 7.6, 7.7, and 7.8 are retained in full, including issue-as-Task, standing instructions, holds, optional dispositions, and bounded review. No tag grammar changes.

### 7.1 Tagged lines

All protocol lines share these rules:

- The protocol tag is `AGENT_SQUAD/0.5.0`. Only this exact tag is recognized in v0.6.0 as well.
- A tagged line MUST be the first line of the review body, comment body, or reply body that carries it. Tokens are separated by exactly one ASCII space; the line has no leading whitespace and no text after its last token.
- Grammar notation: `<N>` and `<n>` are positive decimal integers without leading zeros; `<full-sha>` is a full object ID (§6.2); alternatives are written `a|b`; square brackets in a grammar line mark an optional token, except in the finding line of §7.4, where they are literal characters.
- Parsers MUST match the whole line and MUST be exact. A first line that starts with `AGENT_SQUAD/`, `[REV-`, `DISPOSITION`, `VERIFIED`, or `NOT FIXED` but does not parse is *malformed*. Malformed lines are reported by `status` and `pr reviews` as diagnostics and are ignored for every derivation.
- Authorship is part of validity. A `REVIEW` header counts only when posted by the Reviewer identity; a finding thread root counts when posted by the Reviewer identity, or by either identity through `thread open` for a finding that a tagged review lists (§7.4); `DISPOSITION` lines count only from the Implementer identity; `VERIFIED` and `NOT FIXED` lines count only from the Reviewer identity; a `DECISION` counts only from the Implementer identity or from a login listed in `developer_accounts` (§9), which is how the Developer's own login is authorized when it differs from the Implementer identity; a `STOPPED` counts from either identity. Tagged lines by any other author, including collaborators and bots, are diagnostics and have no effect on the budget, the gates, or the Task.
- "Newer" and "latest" compare the forge's creation timestamps (`submitted_at` for reviews, `created_at` for comments); timestamps MUST be parsed as ISO 8601 with an offset and compared by instant, not lexicographically; equal instants are ordered by ascending forge ID. The server time fields are offset-bearing timestamps (F16 [review structs](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/modules/structs/pull_review.go)); the recordings sample UTC, not every offset.
- In `single` mode the shared account belongs to both role authorship sets; no claim of forge-enforced role separation is made. Untagged human approvals remain outside these grammars and the review budget.
- The CLI composes every header it posts from command arguments (§10.2), so a header written by a skill never reaches the forge unvalidated.

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

**The header is the authoritative verdict.** In `dual` mode the normalized forge state MUST mirror the verdict: `approved` for `approved`, `changes_requested` for `changes_requested`, and `commented` for `needs_human`; the review MUST NOT be dismissed to approve. In `single` mode every verdict is submitted in the neutral `commented` state and human approval is checked separately (§7.10). `status` reports `forge_state_mismatch` when the normalized state differs from the mode's required state and treats that review as nonapproving; it never repairs it. The adapters alone translate these neutral states into wire events (§11).

Before publication, `review post` uses the adapter's pending-draft handling (§11). A protected pending draft causes `pending_draft`, exit 4, with its ID. An explicit `--discard-draft <review-id>` deletes only the acting account's named pending review after ownership/state validation; it never authorizes deleting a submitted review. A post whose read-back fails has exit 1, identifies any review and finding roots already written, and uses §14 recovery.

`review post` composes the header from its arguments and prepends it to the body file, which starts at `## Summary`. The body contains these level-2 sections in this order; the first three are REQUIRED:

```markdown
## Summary
## Verified dispositions
## Findings
## Merge hold
## Standards
## Spec
## Evidence
```

- `## Summary` states the verdict in prose and, for `needs_human`, the decision required.
- `## Verified dispositions` lists every earlier thread verified in this pass, blocking or optional, with its verification line (`REV-<n>: VERIFIED fixed`, `VERIFIED rejection accepted`, or `NOT FIXED`), mirroring the thread replies of §7.5; it says `none` when there is nothing to verify.
- `## Findings` lists each finding opened by this review as `REV-<n> [blocking|optional] <title>`, or says `none`; `review post` generates it from the threads it posts. This list is the authoritative association between a review and its findings (§7.4): a finding belongs to the tagged review that lists its ID, whether its thread was created together with the review or afterwards.
- `## Merge hold` is optional and, when present, MUST be non-empty and immediately follow `## Findings`. Its first content line is `Item <n>: <reason>` or `Task: <reason>`, supplied by the Reviewer under §12.3. It never changes the verdict.
- `## Standards` and `## Spec` carry the two-axis summaries of the installed `code-review` skill (§12.4); `## Evidence` lists the commands the Reviewer ran. These three are RECOMMENDED.
- `## Unanchored findings` is present only when the review was published through the durable body-first path of §11.1 or §11.4. It holds the complete text of every finding of that review, starting with each finding line, and is written in the same forge call as the rest of the body.

A review body MUST NOT be edited after submission. Findings live in threads, not in the body; the `## Unanchored findings` section that the durable body-first path of §11.1 or §11.4 writes together with the review is the exception that keeps a finding's full text on the PR until its thread exists (§7.4).

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

The rest of the root comment carries the v0.4.4 §28.4 fields as bold labels, one paragraph each: **Problem**, **Evidence**, **Impact**, **Required change**, **Verification**. For an optional finding, **Required change** describes the suggested change. Forge-rendered `suggestion` blocks MAY be included; each SHOULD have been trial-applied and validated before posting.

**Anchors.** A thread anchors to a right-side line of `git diff <base> <head>`, that is an added or context line, identified by `path`, `line`, and optionally `start_line` for a range. `review post` MUST validate every anchor locally against that diff and MUST refuse to post an anchor outside the commentable set, because forge acceptance alone does not establish a usable inline anchor (§11.2; E3). A finding about content that is not in the diff is anchored to the most relevant commentable line and says so under **Evidence**.

**ID allocation.** Finding IDs are sequential across the PR. `review post` allocates the next ID as one plus the largest `<n>` among all finding IDs on the PR, whether they appear in a root comment's finding line or in a tagged review's `## Findings` or `## Unanchored findings` list, regardless of author, review, or resolution state; the first finding on a PR is `REV-1`. The Reviewer supplies severity, category, title, anchor, and body for each thread in the `--threads` file; `review post` prepends the finding line with the allocated ID. Under `--resume <review-id>` no ID is allocated: each `--threads` entry is matched in order to the identified review's `## Findings` list and keeps the ID listed there, so the "same list" check of §11.1 compares count, severity, and title. The same defect MUST NOT receive a second ID: a later Reviewer that revisits an existing finding replies on its thread.

**Association.** A finding belongs to the tagged review whose `## Findings` list names its ID (§7.3), not to the forge review record that happens to hold its root comment. Its thread is the root comment on the PR whose finding line carries that ID; there is at most one usable root. An adapter marks a comment with an empty or unusable diff anchor as unusable; it remains visible in evidence but cannot satisfy the association. Recovery creates a usable root with the same finding ID and leaves the unusable comment intact. Usability is established for the opening review, not revalidated against each later PR diff. A later push or a null current-line field must not erase an already-established thread or its replies; existing root association is retained. This holds whether the root was created together with the review, by the body-first path of §11.1 or §11.4, or by `thread open`.

**Unanchored findings.** A finding is *unanchored* when a tagged review lists it but no usable root comment on the PR carries its ID; the state is derived from the missing root, never from the body text. It arises through the body-first path of §11.1 or §11.4, which writes the complete text of every finding of that review, starting with each finding line, under `## Unanchored findings` in the same forge call that publishes the review, before any root is attempted, so the full problem, evidence, impact, required change, and verification survive an interruption at any later point. An unanchored finding keeps its ID, severity, and place in the review's list and is never dropped. When blocking, it counts as an unsettled blocking finding: it blocks approval and, having no thread for a disposition, blocks the next launch. When optional, it blocks nothing and stays visible and recoverable, as §7.5 requires. The recovery route is `thread open --pr <N> --finding REV-<n> --path <path> --line <line> [--start-line <line>]`, run by the Reviewer identity in the same pass or by either identity later, which validates the anchor locally and posts a root comment consisting of the finding line and the body copied from the review's `## Unanchored findings` entry; from then on the finding has an ordinary thread. `status` lists every unanchored finding and reports `open_threads` as the next action only while a blocking one exists (§7.9).

**Severity.** Blocking findings are the substantive actionable findings of §12.3; optional findings are advisory and never block approval.

### 7.5 Dispositions and verification

Before requesting the next review, the Implementer MUST reply on every unsettled thread, blocking or optional. The reply's first line is one of:

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

**Settled threads.** A thread is settled when its latest verification is `VERIFIED fixed` or `VERIFIED rejection accepted`, or when a `DECISION` with `finding=REV-<n>` for that thread is newer than its latest disposition and a later Reviewer has replied `VERIFIED fixed` or `VERIFIED rejection accepted` after checking compliance with the decision. A settled thread is closed for later Reviewers unless new evidence appears; a later Reviewer MUST NOT reopen it merely because it would have judged differently. New evidence is raised as a new finding that cites it. `thread resolve` marks settled threads resolved only when `can_resolve_threads` is true; the Reviewer checks that capability before calling it. `can_read_thread_resolution` controls whether `status` reports a boolean `resolved` or `null`; resolution state is a convenience for readers, never authority.

**Same-head reconsideration.** When every open blocking thread's latest disposition is `rejected` and no code changed, the next review targets the same head. `reviewer launch` accepts a head equal to the latest tagged review's head only in that case, or when a Task amendment (§7.6) is newer than that review, because the amended Task must be reviewed even if the code did not change; a `fixed` disposition requires a changed revision (v0.4.4 §26.5 and §26.13, retained in PR form).

**Optional threads.** An optional thread never blocks approval, but it blocks `reviewer launch` and `pr merge` until it carries a disposition newer than its latest verification, unless it is settled. On an optional thread, `DISPOSITION rejected` MUST be followed by a second non-empty line starting with `Not pursued:` and the reason, or `Deferred to #<issue>:` naming an open issue of the same repository. On an optional thread, `thread reply` refuses an Implementer reply whose first line is not a `DISPOSITION`. It refuses any other rejection body, reads the referenced issue through `issue view`, and refuses a missing or closed issue or a pull request. The existing rules for `fixed <full-sha>` and `needs-human` apply to every thread. `thread resolve` MAY resolve an optional thread when the capability permits it; forge resolution does not replace its disposition.

**Human reviews.** Approvals and request-changes reviews by `approver_accounts` are read through `approvals()`, never parsed as findings, dispositions, verifications, or budget consumption. In `single` mode a human request-changes review affects only §7.10 condition 5b; it does not block `reviewer launch`. The Implementer reports the login and commit to the Developer and acts on the Developer's instruction. A human review alone is not a protocol `DECISION`.

### 7.9 Derived state

`status --pr <N>` derives the loop's state from these inputs and nothing else: the PR record (head SHA, head branch, base branch, open, merged, merge commit), the local worktrees (`git worktree list --porcelain`), the configuration, the PR's reviews and their comments, the PR's review threads, the PR's conversation comments, and the live Herdr agents when Herdr is reachable.

Derived facts:

- **Target.** `pr`, the current head, the head branch, and `base` recomputed as in §6.1.
- **Tagged reviews.** Every valid `REVIEW` header with its forge ID, `commit_id`, state, verdict, and whether it is *current* (`commit_id` equals the PR head).
- **Threads.** Optional findings are also listed with their IDs, titles, and complete latest dispositions for the ready-to-merge report. For each finding: ID, severity, category, title, anchor, the opening review, the latest disposition, the latest verification, whether it is settled, and the forge resolution state (`resolved: null` when it cannot be read).
- **Decisions and stops** in timestamp order. `merge_instruction` is the in-force standing instruction's `id`, `author`, and `created_at`, or `null`; `merge_hold` is `{review_id, text}` from the latest tagged review, or `null`. An earlier review's hold does not apply when the latest review has none.
- **Budget** as in §7.8.
- **Evidence.** The complete PR body with its `## Task` and `## Implementation report` sections; the full body of every tagged review; every finding with its root comment body and every reply body; every decision and stop body; each with author login, forge ID, and timestamp, so that a Reviewer can perform every check in §12.3 through this output alone.
- **Gates.** `stopped` (the latest `STOPPED` is newer than the latest ordinary `DECISION`, excluding standing instructions and withdrawals); `needs_decision` (the latest tagged review has verdict `needs_human` with no newer ordinary `DECISION`, or an unsettled thread's latest disposition is `needs-human` with no newer `DECISION` naming it); `unanchored_findings` (a blocking finding is unanchored, §7.4); `unaddressed_findings` (an unsettled thread, blocking or optional, has no disposition newer than its latest verification); `same_head_requires_rejections` (§7.5); `task_amended` (a Task amendment is newer than the latest tagged review, §7.6); `budget_exhausted`; `not_pushed` (§6.4); `reviewer_live` (a live Herdr agent is named for the current head).
- **Capabilities.** `can_resolve_threads`, `can_read_thread_resolution`, and `can_read_branch_rules`, all booleans reported by the adapter; commands branch on these flags, not the forge name.
- **Human approvals.** `approvals` contains every configured approver's approve-or-request-changes record with login, neutral state, commit, `dismissed`, timestamp, and ID. `human_request_changes` names each configured login whose latest such review requests changes, with its commit and ID. The full records remain in evidence, including superseded or dismissed records. Latest is selected per login by §7.1 ordering, before checking the current head or dismissal; an older approval MUST NOT reappear after a newer decision. Only `single` mode uses these facts for approval.
- **Pending drafts.** `pending_drafts` lists pending review IDs owned by the acting account. The adapter reports which require a `pending_draft` gate. This gate applies to `review post` (including `--resume`), not `reviewer launch`; it does not consume budget. Other accounts' drafts are never discarded or adopted.
- **Approval** as in §7.10, with the list of reasons when the head is not agent-approved.
- **Diagnostics.** Malformed tagged lines, forge-state mismatches, optional unanchored findings, incomplete reviews (a tagged review with an unanchored finding, §7.4, reported with its forge ID; the repair is `review post --resume` while the review is current and `thread open` otherwise), and `task_body_stale` (the PR body's `## Task` differs from the effective Task, §7.6), each with the command that repairs it where one exists; diagnostics never gate an action.
- **Next action**, the first matching condition in this order:

  1. `merged` when the PR is merged; otherwise `closed` when closed without merge.
  2. `address_findings` when the agent-side approval conditions hold but `unaddressed_findings` holds.
  3. `await_human_approval` in `single` mode when the agent-side approval conditions hold, neither `stopped` nor `needs_decision` holds, and condition 5a or 5b does not, with separate reasons for missing approval and human request-changes.
  4. `merge` when all approval conditions hold, a standing instruction is in force, the latest review has no merge hold, and neither `stopped` nor `needs_decision` holds.
  5. `approved` when all approval conditions hold.
  6. `stopped` when that gate holds.
  7. `needs_decision` when that gate holds or the budget is exhausted and another agent review is needed.
  8. `open_threads` for blocking unanchored findings.
  9. `address_findings` for `unaddressed_findings`.
  10. `push` for `not_pushed`, or `same_head_requires_rejections` without `task_amended`.
  11. `reviewer_live` for a live Reviewer at the current head with no current review.
  12. `launch_review` otherwise.

The agent-side conditions are §7.10 conditions 1–4 and 6 plus the mode's mirror rule. Missing dispositions are evaluated before either approval or the human wait. Missing human approval alone MUST NOT become `needs_decision` merely because the agent review used the last budget slot. Human request-changes does not enter `needs_decision`, `unaddressed_findings`, or a launch gate. A pending draft is a publication gate, not a reason to request another agent review. There is no new `human_request_changes` next-action value: it is a reported reason within `await_human_approval`, or alongside the other current action after a head change.

For `await_human_approval`, `status` succeeds (exit 0), while `pr merge` refuses (exit 4). The Implementer reports the full head and approver logins and goes idle. It reports any human request-changes with login and commit to the Developer; it never polls for a later human review. A subsequent “check the PR” instruction re-derives all evidence. In `dual` mode the v0.5.0 next-action behaviour is unchanged, including approval at an exhausted budget.

`status` prints the next action first and the reasons that led to it. It MUST make a current review that the Implementer has not acted on prominent; that is how a lost Herdr notification is discovered (§8.7). `--json` prints the same facts as one object.

Every command re-derives the state when it runs. A review's forge ID identifies its publication; the review target and header do not uniquely identify it, because a fresh review at the same head is legitimate under §7.5. Re-running `review post --resume <review-id>` completes only that identified publication and never creates a second logical review (§11.1). A plain `review post` publishes a new formal review and increases the used budget, even when its header matches an earlier review (§7.8). A tagged review whose `commit_id` is not the PR head is not current; it is neither stale, invalid, nor superseded in the v0.4.4 sense, and no classification machinery exists for it.

### 7.10 Approval validity and merge

A revision has merge-valid approval (called **agent-approved** by retained command text) only when all applicable conditions hold:

1. the latest tagged review has `verdict=approved`;
2. that review's forge `commit_id` equals the current PR head;
3. the PR head equals the `HEAD` of the implementation worktree, which is the registered worktree whose checked-out branch is the PR's head branch;
4. no `STOPPED` is newer than that review;
5. in `dual` mode the review's neutral state is `approved` and it is not dismissed. In `single` mode its state MUST be `commented` and not dismissed under §7.3, and replace the distinct-identity approval check with both conditions below:
   - **5a:** some login in `approver_accounts` has an approval review at exactly `pr.head` that is not dismissed and is that login's latest approve-or-request-changes review;
   - **5b:** no login in `approver_accounts` has a request-changes review as its latest approve-or-request-changes review;
6. no Task amendment (§7.6) is newer than that review, because the review evaluated the earlier Task; an edit that touches only the `## Implementation report` section has no effect on approval. After a Task amendment the same head MAY be reviewed again without a code change (§7.5), and `status` reports `launch_review` when the budget permits (§7.8) and `needs_decision` otherwise.

Conditions 1–4 and 6 are unchanged and apply to the agent's tagged review. The agent approval and human approval may arrive in **either order**. Human approvals do not need a protocol header. For each configured human login, select its latest approve-or-request-changes review across all heads before testing 5a/5b; ignore comment and pending reviews. Dismissal invalidates 5a; 5b has no current-head exemption. A request-changes at an earlier head therefore remains a veto until that login's next approval. `stale` and `official` are not approval inputs: E4 and E9 show why they cannot replace exact-head comparison. Adapter normalization and dismissed-state limitations are defined in §11.0 and §19.

If the agent-side conditions hold but either human condition fails, report `await_human_approval` as §7.9 specifies. The Implementer does not manufacture a protocol decision or launch a review merely to wait for a human.

Before merging or reporting approval, the Implementer MUST reply on every unsettled thread, blocking or optional. `pr merge` MUST refuse with exit 4 while `unaddressed_findings` holds, naming each affected thread; all applicable approval conditions remain required. A standing instruction yields `merge` and authorizes proceeding without another confirmation. When `next_action` is `approved` (no instruction or a hold), send “approved at `<full-sha>`, ready to merge”, with every optional finding's ID, title, and disposition and any hold's item and reason, and wait for the Developer. Nothing merges without the Developer's instruction, given at the start or later.

**Merge hold.** The latest tagged review's non-empty `## Merge hold` adds a refusal with exit 4 naming that review, unless `--accept-merge-hold` is supplied. Only the Developer releases a hold by instructing the merge after seeing it; only then may the Implementer pass that flag. A standing instruction never releases a hold and the flag does not relax any other check.

**CI before merging.** The Implementer confirms every check run for the approved head concluded `success`, `neutral`, or `skipped`, waiting for running checks through the existing authenticated CI-read allowance of §10.4 (`gh run watch <run-id> --exit-status` on GitHub). When CI evidence is inaccessible on another forge, report the limitation and wait; no new credential-handling authority is granted. A failed check is a defect to fix within the Task and have reviewed, or to report if outside scope. If no check exists although the repository runs PR checks, report that and wait. The tool itself does not read or interpret CI.

**Human approval.** Where the repository also requires a human approval or passing checks to merge, the report says so. `pr merge` derives this from the forge: it reads the PR's `mergeable_state` where available and calls `branch_rules` only when `can_read_branch_rules` is true. Otherwise the branch-rule report is "not visible", not "no requirements". Adapter-declared access refusal is also reported as not visible; unrelated transport or malformed-response errors still fail. A merge the forge refuses for that reason is reported with the forge's message; the Developer approves on the forge and the merge is retried.

**Moved base.** If the base tip differs from the approving review's merge-base, `pr merge` MUST refuse unless explicitly given `--accept-moved-base`. Under a standing or explicit merge instruction the Implementer merges the fetched base branch into the PR branch in the issue worktree (not a rebase), resolves conflicts within the Task, validates, pushes, updates the report, reapplies the hold rule, closes the finished Reviewer, and has the new head reviewed. It reports and waits when the review budget is used up or a conflict needs a choice outside the Task. Reviewing the same head retains the old merge-base and does not remove this refusal. Accepting a moved base remains solely the Developer's explicit choice.

**Merge.** On the Developer's instruction the Implementer runs `pr merge --as implementer --pr <N>`, which:

1. verifies the agent-approved condition, refuses any `unaddressed_findings` with exit 4 and their thread IDs, and checks the moved-base rule at that moment;
2. merges through the forge with the configured `merge_method`, passing the approved head SHA as the SHA the PR head must still match, so the forge refuses a head that moved in between;
3. fetches and verifies integration using the saved approved SHA and returned merge commit, independently of a later PR head field: with `merge` (the default) the approved head MUST be an ancestor of the merge commit, so the approved SHA stays an ancestor of the base branch; with `squash` the tree of the merge commit MUST equal the tree of the approved head, which holds exactly when the base had not moved, so a squash merge accepted under `--accept-moved-base` is reported as "integration not verifiable by tree identity" rather than verified;
4. confirms whether the original PR head branch still exists on the forge, deletes only that merged branch if needed, and confirms absence. It uses the pre-merge branch identity, never a post-deletion synthetic pull ref (E9). Only after confirmed absence may it remove `refs/remotes/origin/<head_branch>` with `git update-ref -d <ref> <expected>`, where `<expected>` is the full approved head SHA; a different current value is retained and reported, never deleted unconditionally. An already-absent tracking ref is a successful no-op. If branch absence cannot be confirmed, retain the tracking ref and report incomplete cleanup. An absent remote branch MUST NOT receive a speculative DELETE: E7 observed a 500 for that operation. These guards apply on both forges. It then removes the implementation worktree, deletes the local branch, removes any remaining review worktrees for the PR, and removes the per-PR and per-issue scratch directories (§15);
5. after step 4, whatever its outcome, fast-forwards the primary checkout to the exact base tip SHA that step 3 verified, when the PR targets the configured base branch, that checkout has that branch checked out, and there are no staged or unstaged changes to tracked files. Otherwise, or when Git refuses, it reports the reason and, when the checkout is on the base branch, prints the command for the Developer. It uses only `git merge --ff-only --no-overwrite-ignore <verified-tip-sha>`: it never creates a merge commit, rebases, switches branches, or changes a checkout on another branch or detached `HEAD`. Untracked files alone do not prevent an attempt; Git refuses if they would be overwritten, including ignored files protected by `--no-overwrite-ignore`. A skipped or refused fast-forward does not change the exit status, including exit 3 for incomplete cleanup. The result replaces `fast_forward_command` with a `fast_forward` object containing `result` (`fast-forwarded`, `up to date`, `skipped`, or `refused`), `from` (starting SHA), `to` (verified target SHA), `reason` (text or null), and `command` (text or null). The command is only supplied on a skip or refusal when both the PR and the checkout use the configured base branch and uses the verified SHA, never a ref resolved again. A PR targeting another branch skips the checkout update without a command. Once a merge has been attempted, an exception is never reported as `skipped`: the tool re-reads `HEAD`, reports `fast-forwarded` (or `up to date` if it started at the target) when it confirms the verified tip, and otherwise reports `refused`. It preserves the exception message and omits a fallback command for a confirmed completed update. Before integration is verified, the result is `skipped` with a reason and null SHAs and command; no fast-forward is attempted.

After merging, if the repository runs CI on base-branch pushes, the Implementer waits for that run at the merge commit. It then sends one report needing no answer: merge commit, method, integration check, CI at the approved head and push result, each cleanup step, fast-forward result, and every optional finding with ID, title, and disposition. If push CI failed, it asks the Developer to choose a fix or revert and changes nothing else; missing or inaccessible CI evidence is reported explicitly. The PR description stays frozen.

The `rebase` merge method remains unsupported. A failed integration check retains all cleanup resources and does not fast-forward the primary checkout. E9 did not establish which head was integrated; API fields alone MUST NOT be treated as Git inclusion evidence.

## 8. Herdr Handoff

All of v0.5.0 §8, including §8.3 and §8.4, is **retained**.

*Informative.* The [#53 investigation recommendation](verification/2026-09-25-issue-53.md#recommendation-for-increment-4) found a persistent first-launch trust prompt, not a measured automatic transition from blocked to idle. There is no new post-error wait, retry loop, or readiness timeout. `agent_not_ready` keeps exit 3 and reports retained pane/workspace identities; a person answers, then `reviewer adopt` delivers to the now-idle Reviewer. Neither the tool nor a skill sends keys. Human approval waiting does not alter the fixed handoff lines or the asynchronous discipline.

## 9. Configuration (Schema Version 2)

The file remains `<control-root>/config.json`. No schema bump or `init` rerun is required for a valid existing schema 2 configuration. Missing new optional fields take the defaults below.

```json
{
  "schema_version": 2,
  "forge": {"kind": "github", "owner": "MagiLand", "repo": "agent-squad"},
  "identity_mode": "dual",
  "approver_accounts": [],
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
| `schema_version` | integer | `2` | MUST equal 2; refuse schema 1 with guidance to move it aside and rerun `init` |
| `identity_mode` | string | `"dual"` | `dual` or `single`; dual requires unequal role logins, single equal logins |
| `approver_accounts` | array of strings | `[]` | non-empty in single, MUST be empty in dual; each login non-empty and different from both role accounts |
| `forge.kind` | string | `"github"` | `github` or `forgejo` |
| `forge.owner` | string | origin path at `init` | non-empty; no slash, whitespace, or null bytes |
| `forge.repo` | string | origin path at `init` | same character rules; strip trailing `.git` at init |
| `forge.base_url` | string | absent | required for forgejo and absent otherwise; absolute HTTPS URL, or HTTP only for the literal loopback hosts `127.0.0.1`, `::1`, `localhost`; no query, fragment, or embedded credentials; preserve an instance path prefix |
| `implementer.agent_name` | string | `"implementer"` | `[a-z][a-z0-9_-]{0,31}` |
| `implementer.kind` | string | `"codex"` | `claude` or `codex` |
| `implementer.forge_account` | string | required | non-empty; mode-specific equality rule |
| `implementer.token_file` | string | absent | required for forgejo, absent otherwise; token-file rule below |
| `reviewer.kind` | string | `"claude"` | `claude` or `codex` |
| `reviewer.start_args` | array of strings | `[]` | each non-empty and without null bytes; passed after `--` to `agent start` |
| `reviewer.forge_account` | string | required | non-empty; mode-specific equality rule |
| `reviewer.token_file` | string | absent | required for forgejo, absent otherwise; token-file rule below; may equal implementer's file in single mode |
| `developer_accounts` | array of strings | `[]` | each non-empty and different from the Reviewer login; authorizes directly posted decisions in addition to the Implementer login |
| `base_branch` | string | remote default, else `"main"` | non-empty; no whitespace, `..`, or null bytes; must exist on origin |
| `max_review_passes` | integer | `3` | at least 1 |
| `merge_method` | string | `"merge"` | `merge` or `squash` |
| `worktree_root` | string | `".agent-squad/worktrees"` | relative to primary or absolute; §5.5 containment rules |
| `scratch_root` | string | `".agent-squad/review-scratch"` | same rules; differs from worktree_root and neither contains the other |

Login equality and membership use the same case-insensitive comparison as identity verification. Unknown fields and wrong types are rejected with explicit standard-library validators. A token file MUST be an absolute path to a regular file outside every registered worktree of the repository, with no group or other permission bits, containing one non-empty line. Validate filesystem identity, not just a lexical path prefix, so aliases cannot put a token inside a worktree. Check these conditions in `doctor` and every time the token is read; report role/path and reason, never its contents. The CLI does not create, chmod, relocate, or repair a token file. Configuration stores its path, never its value.

`init` is told the forge: `--forge github|forgejo` defaults to `github`. The new options are `--base-url <URL>`, `--implementer-token-file <path>`, `--reviewer-token-file <path>`, `--identity-mode dual|single` (default dual), and repeatable `--approver-account <login>`. The two existing account arguments remain required, even when equal in single mode. Derive owner/repo from the last two origin path components, including SSH aliases; `--owner`, `--repo`, and `--base-branch` override. An SSH alias does not select an API host. Verify the configured repository as the Implementer through `make_forge`; do not probe an unknown host. Existing configurations are validated, never overwritten; report differences from requested/default values. Configuration remains data, not a workflow language or policy archive.

## 10. CLI Surface

Section 10.1 is retained, including identity selection, timeouts, one-object JSON and exit codes 0–4.

### 10.2 Command table

| Group | Command | Acts as | Derives | Fails when |
| --- | --- | --- | --- | --- |
| Setup | `init --implementer-account <login> --reviewer-account <login> [--owner] [--repo] [--base-branch] [--forge github\|forgejo] [--base-url <URL>] [--implementer-token-file <path>] [--reviewer-token-file <path>] [--identity-mode dual\|single] [--approver-account <login>]` | implementer (one verification read) | forge owner and repo from the remote; base branch from the remote default | not a Git worktree; remote not parseable; repository not readable; identity-mode/account or token-file rules fail; an existing configuration is invalid |
| Setup | `doctor [--live-reviewer]` | both (reads) | the checks of §10.3 | any check fails; `--live-reviewer` leaves a Reviewer running (exit 3) |
| Setup | `skill install [--claude] [--codex] [--force]` | none | packaged skill contents | a differing skill file or symlink exists and `--force` is absent (§12.1) |
| Forge | `issue view --issue <N>` | read | issue title, body, labels, comments; with `--json`, `paths.issue_scratch` is `<scratch_root>/issue-<N>` | issue not readable |
| Forge | `pr create --as implementer --issue <N> [--task <file>] --report <file> [--title <text>]` | implementer | head branch from the implementation worktree; base branch from configuration; body per §7.2 | sections invalid; automatic Task issue closed, a pull request, or empty; branch not pushed; a PR for the branch already exists |
| Forge | `pr report --as implementer --pr <N> --report <file>` | implementer | the replaced `## Implementation report` section | sections invalid; PR not open |
| Forge | `pr head --pr <N>` | read | head SHA and branch, base branch, merge-base, open and merged state | PR not readable |
| Forge | `pr reviews --pr <N>` | read | tagged reviews with header fields, forge state, and current flag; malformed reviews as diagnostics | PR not readable |
| Forge | `review post --as reviewer --pr <N> --head <sha> --base <sha> --verdict <verdict> --body <file> --threads <file> [--resume <review-id>] [--discard-draft <review-id>]` | reviewer | the header; finding IDs (§7.4); anchors (§11.2); verdict consistency and optional non-empty `## Merge hold` immediately after `## Findings` (§7.3). Without `--resume` it always creates a new formal review, even when an earlier review carries the same header, because a same-head reconsideration or a re-review after a Task amendment legitimately repeats it (§7.5). With `--resume <review-id>` it creates no review and only posts the roots missing from that identified review (§11.1) | head is not the PR head; base invalid; anchors invalid; verdict inconsistent with threads; pending draft without explicit valid discard (exit 4); unsupported discard, invalid discard ownership/state, state read-back mismatch or unusable root (exit 1, naming written roots); a root still fails after §11 fallback; with `--resume`, the identified review is not a tagged review by the Reviewer identity with this header and this `## Findings` list |
| Forge | `thread reply --as <role> --pr <N> --finding REV-<n> --body <file>` | either | the thread's root comment from the finding ID | the first line is a tagged line that violates §7.1 or §7.5; finding unknown |
| Forge | `thread open --as <role> --pr <N> --finding REV-<n> --path <path> --line <line> [--start-line <line>]` | either | the finding's text from the tagged review that lists it as unanchored (§7.4); the anchor validated (§11.2) | finding unknown or already has a thread; anchor invalid |
| Forge | `thread resolve --as reviewer --pr <N> --finding REV-<n>` | reviewer | capability and opaque thread identity | capability false: exit 1, `not supported on this forge`; otherwise blocking thread not settled |
| Forge | `decision post --as implementer --pr <N> --finding <REV-n\|none> [--budget <n>] [--task <file>] --body <file>` | implementer | the header; with `--task`, a Task amendment whose body is the decision text followed by the complete amended `## Task` section, posted first and then mirrored into the PR body (§7.6); resumable: when the latest decision is already a Task amendment with the same section, it posts nothing and only re-applies the mirror | `budget` not greater than `used`; finding unknown; the task file is not a complete `## Task` section; the mirror fails after the decision was posted (exit 3; `status` reports `task_body_stale`) |
| Forge | `stop post --as <role> --pr <N> --head <sha> --reason <reason> --body <file>` | either | the header | reason outside the vocabulary; head not a full SHA |
| Forge | `pr merge --as implementer --pr <N> [--accept-moved-base] [--accept-merge-hold]` | implementer | approval validity including 5a/5b in single mode, capability-based branch-rule report, moved base, merge verification, exact-SHA tracking-ref cleanup, primary checkout fast-forward (§7.10) | not agent-approved (exit 4); latest review holds merge without `--accept-merge-hold` (exit 4); base moved without the flag (exit 4); the forge refuses; verification fails; a cleanup step fails (exit 3) |
| Derived state | `status --pr <N> [--json]` | read | `merge_instruction`, `merge_hold`, capabilities, approvals, human request-changes, pending drafts, resolution where readable, and everything else in §7.9, including under `--json` the full PR body and every review, thread, reply, decision, and stop body with author and timestamp | PR not readable |
| Reviewer lifecycle | `review-worktree create --pr <N> --head <sha>` | none | the worktree path of §5.2; an existing clean worktree at that head is reused | head not present locally; the path exists and is not a clean worktree at that head |
| Reviewer lifecycle | `review-worktree remove --pr <N> --head <sha>` | none | the worktree path | the path is not a squad worktree |
| Reviewer lifecycle | `reviewer launch --pr <N>` | read | §8.3 | any gate (exit 4); Herdr failure; blocked (exit 3) |
| Reviewer lifecycle | `reviewer adopt --pr <N>` | read | §8.6 | no live Reviewer for the head; blocked (exit 3) |
| Reviewer lifecycle | `reviewer close --pr <N> [--head <sha>]` | none | §8.9 | workspace not squad-owned or not isolated (exit 3) |
| Handoff | `handoff review-result --pr <N> --head <sha> --verdict <verdict>` | read | §8.5 | no matching review on the PR; Implementer agent not found |
| Handoff | `handoff stopped --pr <N> --head <sha> --reason <reason>` | read | §8.5 | no matching stop on the PR; Implementer agent not found |

This is the whole surface. It is not an invitation to expose internal steps as commands.

### 10.3 `doctor`

Checks use the factory and neutral adapter methods. `doctor.py` contains no forge name or branch on `forge.kind`: applicability of transport prerequisites is implemented inside the selected adapter. It calls `version()` using the adapter's `version_label`, then `verify_identity()` for each role; each method raises the applicable named failure below as `ForgeError`. Configuration validation supplies configuration failures; common mode checks consume neutral permissions and `user_exists()` results. A failed check exits 1; a safely retained live Reviewer exits 3; warnings alone do not fail. Error details may add a role or path to these named failure messages, but MUST NOT include token contents.

| Applicability | Check | Failure text |
| --- | --- | --- |
| All | Non-bare repository, valid configuration, both local exclude entries | `not a Git worktree`; `invalid configuration: <reason>`; `local exclusions are missing; run agent-squad init` |
| All | Control/worktree/scratch roots creatable and writable; disposable detached worktree create/remove | `root is not writable: <path>` or the Git failure; unsafe cleanup: `probe directory retained: <path>` |
| All | Configured base exists on origin | `base_branch does not exist on origin: <branch>` |
| All | Selected token identifies each role | `cannot resolve token for configured <role> account`; `GET /user does not match configured <role> account` |
| All | Repository readable by each role | `<role> repository is not readable` with safe underlying forge error |
| Dual only | Two role logins differ | `dual identity_mode requires different forge accounts` |
| Dual only | Reviewer has write or admin repository permission | `Reviewer account requires repository write permission` |
| Single only | Equal role logins and non-empty independent approver list | `single identity_mode requires equal forge accounts`; `single identity_mode requires approver_accounts`; `approver account must differ from role accounts` |
| Single only | Shared account has push/write or admin permission | `shared account requires repository push permission` |
| Single only | Every configured approver login exists | `approver account does not exist: <login>` |
| GitHub | Adapter `version()` checks executable/version; `verify_identity()` resolves the per-role token through gh | `GitHub CLI not found on PATH`; safe CLI/version/token error |
| Forgejo | Configuration validation checks API URL; adapter `version()` checks reachable version endpoint and version at least 16.0.0 | `invalid forge.base_url: <reason>`; `cannot read forge version`; `Forgejo version must be at least 16.0.0` |
| Forgejo, each role | Adapter `verify_identity()` validates absolute regular token file outside worktrees, owner-only mode and one non-empty line before use | `<role> token_file must be an absolute regular file outside repository worktrees`; `<role> token_file must have no group or other permissions`; `<role> token_file must contain one non-empty line` |
| All | Herdr executable, schema, socket and integration for both harness kinds | underlying Herdr error with affected check named |
| All | Installed skills match package, expected Claude symlinks and installed code-review skill | `installed skill differs from package: <path>`; `skill symlink must point to <target>`; `skill requires frontmatter name: code-review: <path>` |

Labels and transport-specific failure text belong to the adapter, including Increment 1's existing `GitHub CLI` label; moving that label out of `doctor.py` preserves its displayed output without exempting the module from plan §4.1. Forgejo repository push permission is read from `permissions.push` (Setup 009–010); a role's base permission is normalized before the common mode check. Approver existence is not proof that the forge will accept or count its review; forge branch protections remain authoritative at merge time.

Retain the live Implementer-name/kind check (absence is a warning), orphan reporting for closed/merged PR resources and closed-issue scratch, and the `.gitmodules` warning. An unreadable issue/PR is a failure, never an assumed orphan; doctor never removes residue. Keep all Git object-format, committed-HEAD, ownership, and root probes.

`doctor --live-reviewer` creates a disposable detached worktree at current HEAD, opens Herdr, starts the configured Reviewer with start_args, observes readiness and trust, closes owned resources and removes the worktree. It sends no review request. Unsafe cleanup retains and reports resources, exit 3. Section 8's unchanged trust/adopt rules apply.

### 10.4 Reads used by the skills

`issue view --issue <N> --json` reports `paths.issue_scratch` as `<scratch_root>/issue-<N>`. Workflow paths include `issue_scratch: null` when no issue number is known. This path names a location for drafts and validation output, not protocol authority.

The skills obtain Task, PR, review, decision, stop, and budget state through `status --pr <N> --json`, `pr head`, `pr reviews`, and `issue view`; they never retrieve or read a token or switch forge identities. The Implementer alone MAY use read-only `gh run list`, `gh run view` (including logs), `gh run watch <run-id> --exit-status`, `gh pr checks`, and `gh api --method GET` against Actions, check, branch-protection, or branch-rule endpoints for CI evidence and required-check metadata these commands do not expose, scoped to the configured repository. Match CI evidence to the full current PR head and event, and post-merge push evidence to the integration commit; record run IDs, results, and per-job durations when relevant. Distinguish checks that ran from configured required checks, and report missing access or configuration. This allowance, granted by decision 5775326494 on PR #62, does not permit `gh` mutations, other `gh` reads, or using `gh` for Task, PR, review, decision, stop, or budget authority. Use existing authenticated access and report its limitations. `status --json` is the complete evidence interface: it carries the PR body, so the Reviewer takes its spec from the effective Task (§7.6) there rather than from the issue, and it carries every review, thread, reply, decision, and stop body with author and timestamp (§7.9), so a fresh Reviewer can verify every disposition, read every decision, and inspect every finding's evidence without any other read.

These reads refer to the configured forge, not to GitHub as a universal authority. The existing `gh` CI-read allowance is usable only for a GitHub repository. This delta does not grant a skill permission to read Forgejo token files or improvise authenticated HTTP calls; inaccessible CI evidence is reported under §7.10. Both modes retain the same read-only role defaults and complete evidence interface.

## 11. Forge Adapter

### 11.0 Neutral protocol, records, and capabilities

`forge.py` defines a typed `Forge` protocol and `make_forge(repository: Repository, role: Role) -> Forge`, selected only by `forge.kind`. Each role has a process-local adapter instance with its configured account. Factory selection, adapter code and configuration validation may name a forge; `conventions.py`, `commands.py`, `merging.py`, `reviewer.py` and `doctor.py` contain no forge name. They use the protocol, neutral states, capabilities and adapter-supplied diagnostics, never a branch on kind. `EVENTS`/`STATES` wire maps leave `conventions.py`; `Anchor` emits no payload. Raw HTTP endpoints, `gh` commands, transport errors and review payload fields stay in adapters.

**Neutral records.** Use typed, immutable records (typed mappings are acceptable for existing dictionary-shaped outputs). IDs are positive integers unless explicitly an opaque thread identity; SHAs are full object IDs; timestamps are offset-bearing ISO 8601 values with §7.1 instant ordering; bodies normalize CRLF to LF without otherwise rewriting text.

| Type | Required data and semantics |
| --- | --- |
| `Evidence` | `id`, author login, creation/submission timestamp, body |
| `Review` | evidence, reviewed `commit_id`, neutral state, separate `dismissed` boolean |
| `Comment` | evidence, holding review ID, reply-to/root association when known, path, end line, optional start line, side, usable-anchor indication; adapters retain enough private location data to reply without exposing wire semantics to conventions |
| `PullRequest` | evidence, number, title, head SHA and branch, base SHA and branch, open/closed state, merged boolean, optional merge commit and mergeability information; base SHA is informational, never the local review merge-base |
| `Snapshot` | PR, reviews, comments, conversation evidence, thread resolution records, capabilities and acting-account pending-draft facts; no local authority cache |
| `Anchor` | `path`, positive `line`, optional positive `start_line`; new/right side only; no payload method |
| Approval record | login, neutral state, commit, dismissed, timestamp, ID, including all authors before configured-approver filtering |
| Issue record | number, title, body, open/closed state, `is_pull_request`, label strings, conversation evidence |
| Thread state | root comment ID, optional opaque resolution identity, `resolved` boolean or unknown |
| Repository record / permission | validated repository identity and default branch; permission normalized to `admin`, `write`, `read`, or `none` |
| Branch-rule report | visible requirements or explicit nonvisibility; raw detail is informational, never an approval gate |
| Merge result | confirmed merge commit SHA and safe message; confirmation still requires Git integration checks |
| Review publication / root | exact head, neutral requested state, body, complete durable fallback body, ordered finding bodies and neutral anchors; finding IDs allocated by the common protocol before publication |

The shared review states are exactly `approved`, `changes_requested`, `commented`, and `pending`. Dismissal is a separate boolean, not a fifth state. The mode/verdict rule selects a neutral requested state (§7.3); adapters translate events and responses. For ordinary review evidence, GitHub's `DISMISSED` may normalize to nonapproving `commented` with `dismissed=true`; approval history additionally recovers its original state as described below. Forgejo keeps its explicit dismissal flag independently of the neutral state (E9).

`approvals(number)` returns all approve-or-request-changes reviews by every login, including dismissed ones, in deterministic submission-time/ID order. Ordinary comment and pending reviews are excluded. When GitHub exposes only `DISMISSED`, the adapter joins the review ID to its documented `review_dismissed` event's original state, keeping the review's author, commit and submission time, and sets `dismissed=true`. The original state is available in the [GitHub issue-event contract](https://docs.github.com/en/rest/using-the-rest-api/issue-event-types#review_dismissed); never fabricate it or treat the dismissal event's actor as the reviewer. Missing or ambiguous history needed to classify such a record is a response-validation failure, not permission to revive an older approval. These additional reads belong to the new approvals method; Increment 1's existing loop does not call it. The common derivation selects configured approvers and their latest approve-or-request-changes review before examining head/dismissal. No adapter treats server `stale` or `official` as proof of approval.

**Complete protocol surface.** The following are method contracts; equivalent keyword-only signatures or typed request records preserve these contracts. A method that adds transport-private data is not thereby a new common method. `number`, review IDs and root IDs are positive integers; `branch`, login, body and opaque thread ID are strings; `head` is a full SHA; `method` is `merge` or `squash`. `Role` is `implementer|reviewer`. The listed operations are not CLI commands. Besides the capability flags below, the protocol exposes a read-only `version_label: str` supplied by the adapter for the doctor version check; it carries no transport decision into the caller.

Every I/O method may raise `ForgeError(message, status=None)` for bounded transport failure, inaccessible/missing resources, bad JSON, malformed data or identity mismatch; messages redact credentials. Invalid configuration/factory inputs raise `AgentSquadError`; protected publication drafts raise the existing `GateError` (exit 4). The extra gate/failure contracts in the last column are exhaustive exceptions to ordinary success/failure handling. Mutations are never automatically retried.

| Method and inputs | Neutral result | Additional contract / errors |
| --- | --- | --- |
| `version()` | version text | adapter checks its transport prerequisite and enforces any minimum version; raises the applicable §10.3 failures |
| `verify_identity()` | none | adapter resolves and validates its selected role's credential source and identity before first mutation; raises applicable §10.3 failures; no token result |
| `repository_record()` | repository record | validates configured repository identity |
| `repository_permission()` | normalized permission | selected role's base permission, not a custom-role name |
| `user_exists(login)` | boolean | absent login is false; access or transport failure is an error |
| `issue(number)` | issue record | includes all issue comments and labels |
| `pr(number)` | `PullRequest` | exact object IDs required |
| `reviews(number)` | tuple of `Review` | all pages; skip non-review requests inside adapter |
| `approvals(number)` | tuple of approval records | every author; no protocol/header filtering |
| `snapshot(number)` | `Snapshot` | complete evidence; reads resolution only when supported |
| `thread_states(number)` | tuple of thread states | unknown resolution when unreadable; no invented resolved=false |
| `branch_rules(branch)` | branch-rule report | access-limited reads return nonvisibility; unrelated errors fail |
| `branch_prs(branch)` | sequence of PR records/identities | existing PR detection across open/closed states |
| `branch_head(branch)` | full SHA or absent | distinguish confirmed absence from an unreadable branch |
| `create_pr(title, head_branch, base_branch, body)` | `PullRequest` | verifies role and response identity |
| `update_body(number, body)` | `PullRequest` | exact replacement, no hidden Task edit |
| `prepare_review(number, discard_draft=None)` | none | protected draft raises `GateError` (CLI exit 4); invalid/unsupported explicit discard is `ForgeError` (exit 1); validates named pending owner before the sole authorized deletion |
| `post_review(number, publication)` | `Review` | exact-state read-back; adapter performs §11.1 or §11.4 publication sequence; partial failure identifies published review and missing/unusable root IDs without replaying successful writes |
| `post_root(number, head, review_id, anchor, body)` | `Comment` | usable-anchor read-back required; no deletion on failure |
| `reply(number, root_id, body)` | `Comment` | locates the stored root/conversation through adapter data |
| `comment(number, body)` | `Evidence` | PR conversation comment for decision/stop |
| `resolve(thread_id)` | confirmed thread state | unsupported capability raises `ForgeError("not supported on this forge")`, exit 1 |
| `merge(number, head, method)` | merge result | exact-head server guard; refusal preserves HTTP status; uncertain outcome is not reported as definitely unmerged |
| `delete_branch(branch, expected_head)` | confirmed absence | refuses changed identity/head; never deletes an already-confirmed absent branch |

Token resolution, generic HTTP/GraphQL calls, subprocess invocation, pending-review submission/deletion and JSON wire parsers are private adapter mechanics. `prepare_review` retains GitHub's existing submit-as-comment treatment of a stranded draft; the explicit discard operation exists only for the adapter that protects drafts. `--resume` creates no second review: the common command identifies the prior publication and asks for missing roots only. No method consumes a human token on an agent's behalf.

The full release protocol is implemented in increments: Increment 1 needs the currently consumed operations, neutral translation, capabilities and `approvals()`; transport, explicit draft discard, user lookup, and remote branch operations needed only for later features arrive with their owning increments (§17). Do not add unused scaffolding merely to fill later rows early.

| Capability | GitHub | Forgejo | Command behaviour |
| --- | --- | --- | --- |
| `can_resolve_threads` | true | false | `thread resolve` refuses unsupported use with exit 1 and exact text `not supported on this forge`; Reviewer skips it |
| `can_read_thread_resolution` | true | true for the resolver field supported by F16 below | `status` uses boolean when readable, `null` otherwise; resolution never settles a finding |
| `can_read_branch_rules` | true | depends on the acting account's readable administrative access (Setup 012 refused the author) | `pr merge` reads rules only if true; otherwise reports `not visible` |

Capability values describe the adapter/acting account, not a protocol branch on kind. An authenticated refusal while reading rules can lower visibility without implying no protection. In all other cases malformed responses and failed requests remain errors.

### 11.1 GitHub through `gh api`

GitHub behaviour and subprocess construction remain unchanged except the requested identity-mode event selection. The adapter invokes the `gh` executable found on `PATH`. v0.4.4 §14 applies: it verifies `gh --version` and `gh auth token --user` at `doctor` time and does not hard-code observed versions. It uses:

- **Issues:** `GET /repos/{owner}/{repo}/issues/{N}` for title, body, and labels, and `GET .../issues/{N}/comments` for comments used by `issue view` and `pr create`.
- **PR record:** `GET /repos/{owner}/{repo}/pulls/{N}` for `head.sha`, `head.ref`, `base.ref`, `base.sha`, `state`, `merged`, `merge_commit_sha`, and `mergeable_state`; `POST /repos/{owner}/{repo}/pulls` to create; `PATCH .../pulls/{N}` to update the body.
- **Merge-base:** computed locally with Git after fetching the base branch; `GET .../compare/{base}...{head}` (`merge_base_commit.sha`) MAY cross-check it.
- **Reviews:** `POST .../pulls/{N}/reviews` with `commit_id`, `body`, `event` (`APPROVE`, `REQUEST_CHANGES`, or `COMMENT`), and `comments[]` of `{path, line, side: "RIGHT", start_line?, start_side?, body}`. On a batch rejection the adapter re-validates the anchors and posts the review with the same event and no comments, with the body extended by a `## Unanchored findings` section that holds the complete text of every finding, starting with each finding line, so that this first successful write persists all evidence on the PR (§7.4); its `## Findings` list keeps the association. It then posts each root with `POST .../pulls/{N}/comments` including `commit_id`; the forge attaches such comments to synthetic reviews without headers, which are not tagged reviews and never count. A root that still fails is reported, the command exits 1, and the Reviewer re-anchors it with `thread open` (§7.4). The review body is never edited afterwards, nothing already posted is deleted, and the review remains one logical review. Resumption is explicit: `review post --resume <review-id>` identifies the interrupted review by its forge ID, verifies that it is a tagged review by the Reviewer identity with the same header and the same `## Findings` list, posts no second review, and creates only the roots that are missing, so an interruption at any point is recovered without a second logical review. A plain `review post` never suppresses a review on the strength of a matching header: a fresh pass at the same head (§7.5) is a new formal review that MUST be published and counted (§7.8), and after a Task amendment it is the fresh approval's submission time that restores approval validity (§7.10).
- **Stranded drafts:** before posting, `GET .../pulls/{N}/reviews` is scanned for a `PENDING` review by the Reviewer identity; one is submitted with `POST .../pulls/{N}/reviews/{id}/events` and `event: COMMENT`, never deleted, because a pending draft blocks a new review and may hold finished replies. This behaviour stays behind `prepare_review`; explicit `--discard-draft` is unsupported on this adapter and exits 1 without deletion.
- **Threads and replies:** `POST .../pulls/{N}/comments/{root_id}/replies` posts a reply. Enumeration goes per review, `GET .../pulls/{N}/reviews/{id}/comments` for every review, because the flat `GET .../pulls/{N}/comments` listing can omit replies; the flat listing supplies `line`, `start_line`, and `side` for anchors, which the per-review listing reports as `null`. Thread node IDs and resolution state come from GraphQL `repository.pullRequest.reviewThreads`, paginated, and resolution uses the `resolveReviewThread` mutation.
- **Conversation comments:** `GET` and `POST .../issues/{N}/comments` for decisions and stops.
- **Merge:** `PUT .../pulls/{N}/merge` with `merge_method` and `sha` set to the approved head; `DELETE .../git/refs/heads/{branch}` when the branch survives the merge.
- **Identity:** `GET /user` per §4.5.

Responses are parsed as JSON with explicit validators; forge error messages are surfaced verbatim in the one-line error. The adapter does not retry mutations automatically; a failed mutation is reported and the caller re-derives the state.

The GitHub adapter maps response states `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, and `PENDING` to `approved`, `changes_requested`, `commented`, and `pending`; `DISMISSED` sets `dismissed=true` with the original state recovered for `approvals()` as §11.0 specifies. It maps requested `approved`, `changes_requested`, and `commented` states to `APPROVE`, `REQUEST_CHANGES`, and `COMMENT`. In single mode the requested state is always `commented`. It alone builds single-line `{path, line, side: "RIGHT"}` or range `{path, line, side: "RIGHT", start_line, start_side: "RIGHT"}` payloads. Common code compares only neutral states. Increment 1 preserves existing external GitHub output except the three capability flags; neutral internal records do not justify unrelated serialization changes.

### 11.2 Anchor validation

Anchor validation is the productized form of the existing `anchors.py` helper: parse the unified diff of `git diff <base> <head>` into, per file path, the set of right-side line numbers that are added or context lines. Deleted lines and `\ No newline at end of file` markers are excluded; renamed and new files use their new path; binary files have no commentable lines. `review post` accepts an anchor only when `path` is in the set and `line` and `start_line`, when present, are both in that file's set with `start_line` not greater than `line`. Validation runs before any forge call. Unit tests cover multiple hunks, new files, deleted files, renames, binary files, and files without a trailing newline.

`Anchor` keeps only path, end line and optional start line. Payload construction belongs to each adapter. For Forgejo, a single new-side line maps to `new_position=line, extra_lines_count=0`; a range maps to `new_position=start_line, extra_lines_count=line-start_line`. E5's tested start 2/count 2 rendered lines 2–4, and F16 [CreatePullReviewComment](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/modules/structs/pull_review.go) describes the count as additional following lines. Do not send the end line as the start. The tested mapping succeeded, so the plan's last-line fallback is not selected.

Forgejo publication MUST refuse duplicate conversation anchors within a review before writing: two intended roots with the same review/path/start position would otherwise join one conversation (E2 and F16 [comment conversion](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/services/convert/pull_review.go)). The finding ID remains the protocol identity. Do not derive a head anchor from a returned `position` alone; decode the new-side lines of `diff_hunk`, using the last displayed new-side line as the range end. Retain the returned location privately for replies. If no usable head-side line can be established, the root is unanchored (§7.4). An empty diff hunk never proves successful anchoring (E3).

### 11.3 Fake forges

Retain the executable fake `gh` of v0.5.0 unchanged for Increment 1: distinct fake role tokens, the existing scripted model and call log, batch rejection, omitted flat-list replies, pending drafts, exact-head refusal and author-approval refusal. Never call the real forge in automated tests.

The Forgejo fake is a committed `http.server.ThreadingHTTPServer` on `127.0.0.1:0`, started and stopped by the fixture. Tests select it through `forge.base_url` and exercise the real standard-library request construction. Seed its response shapes from the committed #54 recordings. Required cases model E1–E9: event spelling, hidden pending state for an unknown event, author approval/rejection 422, arbitrary accepted commit IDs, invalid anchors retained with empty hunks, body-first roots and replies, range-start mapping, real-draft absorption and deletion, requested-review rows, superseding dismissal, unreliable stale flags, live base tip, protection 405, exact-head 409, absent branch DELETE 500, and the post-deletion PR-head discrepancy. Some of these are intentionally adverse injected cases, not desired server behaviour.

The fake also shuffles comments, varies timestamp offsets, and serves multiple paginated review/issue lists to test defensive handling even where the live sample did not reproduce variation (§19). Per-review comments are returned unpaginated as the F16 handler does. It can expose or hide resolver/rule data. Its private test log records each request and fake Authorization value to prove token-per-role selection; CLI output must never reveal even fake tokens. No real token is put in a fixture or evidence record. Automated tests call neither a real model nor real Herdr.

For operations not exercised by #54, seed the fake from the pinned source contracts in §11.4 and mark the cases **synthetic, source-backed**: squash merge, successful explicit deletion of an existing branch, PR-body PATCH and issue GET. Their inclusion in automated tests is not live evidence; retain their §19 limitations until a recorded trial verifies them.

### 11.4 Forgejo through the standard-library HTTP client

The adapter uses `urllib.request` against `<forge.base_url>/api/v1`, preserving a configured instance sub-path. Minimum supported version is 16.0.0; the reference experiment version is 16.0.3. This is a support policy with no version-dependent behaviour; other versions have not been live-trialled. No `fj`, HTTP library or forge SDK is added. URL components are encoded individually. Requests have bounded timeouts, validate JSON and response types, and redact credentials from errors. Do not forward an Authorization header across origins or follow redirects to an unvalidated origin.

The table covers every row of plan §4.3. Observations refer to [#54](verification/2026-09-25-issue-54.md); source links are pinned to 16.0.3. The handler/struct contracts cited below are not claims of an additional live experiment.

| Topic | Recorded or source fact | Required adapter behaviour |
| --- | --- | --- |
| Review events | E8: author `APPROVED`/`REQUEST_CHANGES` returned 422; unknown event returned hidden `PENDING`; E1: author `COMMENT` worked | Map neutral states to `APPROVED`, `REQUEST_CHANGES`, `COMMENT`; read back and require expected state, account and exact commit. Single mode always sends COMMENT. Never silently count a pending review as submitted. |
| Invalid anchors | E3: out-of-hunk and missing-path roots were accepted with empty `diff_hunk`, shown without inline code | Validate locally, then read back every root; unusable roots remain unanchored with durable full text and exit 1 naming findings. |
| Commit ID | E4: a nonexistent full ID and an old head were accepted verbatim | Send the exact target; check it on read-back and in common derivation. Never trust server acceptance as proof that a commit belongs to the PR. |
| Pending draft and publication order | E6: body-only submission absorbed a real UI draft; deleting a named draft removed it. E2: roots added after submission and replies joined correctly | Before every post/resume, list acting-account reviews and refuse protected pending drafts (exit 4). With explicit discard validate owner, PR and pending state, delete only that ID and re-read. Publish body first, then roots; retain the body and IDs on partial failure. |
| Conversations | E2 plus F16 [CreatePullReviewComment](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/routers/api/v1/repo/pull_review.go): roots/replies carry review/path/position and no API reply-to field | Refuse duplicate root anchors. Associate protocol roots by `[REV-n]`, group untagged replies with that root's review/path/location, sort by ID, and use the root's stored location when replying. |
| Head lines | F16 [ToPullReviewComment](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/services/convert/pull_review.go) emits `Comment.CommitSHA`, stored line and converted patch; E5's excerpt ends at range end | Interpret head lines from `diff_hunk`, not a presumed relation between `position`, comment commit and reviewed head. Preserve wire location only inside adapter. Unusable hunk means unanchored. |
| Resolution | F16 [PullReviewComment](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/modules/structs/pull_review.go) exposes `resolver`; conversion maps ResolveDoer; #54 did not exercise a resolved root | `can_resolve_threads=false`, no marker emulation; expose readable resolution from resolver, with null for unknown/unreadable. Live resolved-root behaviour is unverified (§19). |
| Comment enumeration | F16 [GetPullReviewComments and ToPullReviewCommentList](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/routers/api/v1/repo/pull_review.go) returns the whole converted list; conversion iterates grouped maps. E1's three reads happened to have the same order | Read once per review and sort by ID. Do not assert live random order or paginate this endpoint. |
| Base tip | Setup 016–017: `base.sha` advanced while `merge_base` stayed fixed | Accept both fields; compute review base locally after fetch. Never replace moved-base checks with the API base field. |
| Ranges | E5 and the F16 struct: start position plus additional lines | Use §11.2's start-line mapping; preserve full range text and local diff validation. |
| Merge and deletion | E7: 405 before approval, isolated wrong-head 409 after approval, correct-head 200, absent-branch GET 404, repeated DELETE 500; E9: post-deletion PR head differed. F16 [MergePullRequestForm](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/services/forms/repo_form.go) admits `merge` and `squash`; [MergePullRequest](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/routers/api/v1/repo/pull.go) applies the selected method/head guard and may delete the branch; [DeleteBranch](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/routers/api/v1/repo/branch.go) returns 204 after successful explicit deletion. Squash and successful explicit DELETE were not exercised by #54 (§19). | POST merge with `Do=merge\|squash`, `head_commit_id` and `delete_branch_after_merge`; verify returned/re-read merge identity by Git, including §7.10's squash tree check. Check branch existence before DELETE and confirm absence with a follow-up GET; pin cleanup to pre-merge branch and approved head. |
| Bodies | E1–E9 recording bodies retain the submitted text, including invalid roots | Preserve text with existing CRLF normalization; validate rather than assume all future responses are non-null. |
| Review-list rows and flags | Setup 014–015: `REQUEST_REVIEW` has empty commit; E8 pending visibility; E9 newer reviews dismissed prior decisions | Skip REQUEST_REVIEW before full-SHA validation. Normalize APPROVED/REQUEST_CHANGES/COMMENT/PENDING, preserve dismissed, ignore stale/official for approval. Unknown states fail validation. |
| Timestamps | F16 structs use time.Time for submitted/created values; #54 samples are UTC | Parse offsets and order by instant, tie by ascending ID; do not compare raw strings. Non-UTC variation is tested synthetically. |
| Authentication and paging | Setup 001–005: public version, authenticated users, settings max-response-items 50; E1–E9 used the three recorded token scopes. F16 ListPullReviews takes list options | Send `Authorization: token <t>`, verify GET /user before mutation. For paginated endpoints request `limit=50&page=N` and stop on a short page; never apply that loop to the unpaginated comments endpoint. Multi-page boundaries remain a trial limitation. |
| Permissions and branch rules | Setup 009–012: permission object distinguishes push/read; author's protection read was 403 | Normalize permissions; expose rules only with readable administrative access, otherwise `not visible`. Do not infer an unprotected branch from 403. |

**Durable publication and recovery.** `review post` validates the whole target, verdict, body, findings, duplicate anchors and local diff before any mutation, including an explicit discard. It then checks the draft gate, optionally discards only the named validated pending draft, and rechecks that no protected pending draft remains. A concurrent new draft can still race this check; do not claim atomicity (§19).

Publish the formal review with the full `## Findings` list and complete `## Unanchored findings` text in the initial body-only write. E2 establishes that submitted reviews accept roots afterwards; E6 establishes why an unchecked prior draft is unsafe. Read back the review before root writes. Require its author, exact `commit_id`, normalized requested state and body; report mismatch as exit 1 with the review ID and preserve the server record. Post roots in finding order through `POST .../pulls/{N}/reviews/{id}/comments`; read each back and require a usable diff hunk and correct finding association. Report all missing or unusable finding IDs, exit 1; never erase evidence or edit the submitted body. The fallback section remains historical text even once every root exists; current completeness is derived from usable roots.

On interruption, re-read the PR: `--resume <review-id>` validates the exact tagged review/header/list and creates only missing usable roots; `thread open` recovers one finding from the durable body alone. Both leave existing usable roots unchanged, never publish another logical review, and never discard a pending draft implicitly. A plain post is a new review and consumes budget. A draft left by an invalid event or an interruption requires explicit discard, not a blind retry. The adapter may delete any named acting-account pending draft only because `--discard-draft` is explicit authorization for that exact ID; it never decides on its own that a human draft is disposable.

Issue, PR, review, conversation-comment and branch operations use repository-scoped API paths; create/update/read-back identities must match the requested repository and number. Identity and scope failures are ordinary ForgeError failures. Body-first and reply paths are E2; PR creation and conversation comments are Setup 013 and final recordings 064–065; merge is E7. Transport success never substitutes for protocol validation or Git integration proof.

PR-body PATCH and issue GET are source-backed, not #54 observations: F16 [EditPullRequest](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/routers/api/v1/repo/pull.go) applies the optional body from [EditPullRequestOption](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/modules/structs/pull.go), and [GetIssue](https://codeberg.org/forgejo/forgejo/src/tag/v16.0.3/routers/api/v1/repo/issue.go) reads the requested repository/index with an access check. The adapter validates issue identity and required fields and reads back the PR after a body update; a mismatch fails without claiming a successful report update (§19).

## 12. Skills

The two packaged skills remain control-plane material. Sections 12.1 and 12.4 are retained, including installation checks and explicit use of the installed two-axis review skill. Implement these text changes in the increments named in §17; this documentation PR does not install or change the current skills.

### 12.2 `squad-implementer`: mandatory rules

An outline; each rule is mandatory content of the skill text. Markers refer to the current Implementer prompt. The preamble directs the Implementer to filter `--json` output to read what is needed rather than save whole responses to disk; a saved response is never authority.

1. **Task statement [adapted].** Read title, body, labels, and comments through `issue view`. When §7.2's readiness conditions hold and the start instruction does not change scope or request a Task, the issue is the Task: do not draft or present one. Otherwise draft the complete Task in `paths.issue_scratch`, present it once, and wait for approval before coding. Ask only about unanswered questions changing the result; record other interpretations under “Design decisions”. Do not silently change the Task.
2. **Implementation workflow [adapted].** Use the dedicated worktree `<worktree_root>/issue-<N>` on a branch following the consuming repository's naming policy (use `<type>/issue-<N>-<slug>` when no policy is specified); understand the issue and relevant existing code before making changes; implement the requested change without unnecessary scope expansion; run the relevant tests, checks, and validation; commit and push; open the PR with `pr create --report`, adding `--task` only for an approved drafted Task; apply rule 8 to the whole PR at creation and after every push; right after creation record the §7.6 standing instruction with the start instruction quoted unless the Developer kept the merge or a hold applies; withdraw it when the Developer asks or a hold arises; update the report with `pr report` on every later push; list changes to agent instruction or control-plane files under "Areas worth extra review". Put the report file, probe scripts, and validation output in `<scratch_root>/issue-<N>`. Tool installations and virtual environments go outside the repository (for example in the harness scratchpad), never under `.agent-squad/` or `scratch_root`.
3. **Request a review [adapted].** Run `reviewer launch --pr <N>`; on exit 4 report the gate to the Developer; on exit 3 tell the Developer which pane needs an answer and later run `reviewer adopt`; never send keys.
4. **Asynchronous handoff [verbatim]** as quoted in §8.8, after a successful `reviewer launch` or `reviewer adopt`.
5. **Handling review feedback [adapted].** On a `REVIEW_RESULT` prompt or a "check the PR" instruction, run `status --pr <N>` first and act on the derived next action (`merge` proceeds through rule 8, `approved` waits, `await_human_approval` reports and goes idle); read the review directly from the forge PR, including inline threads and suggestions, and treat the PR as the authoritative source; independently evaluate each substantive actionable finding; fix findings that are valid; do not change the code merely to satisfy findings that are incorrect, inappropriate, already resolved, or no longer applicable; if `status` reports `open_threads`, open a thread for each blocking unanchored finding with `thread open` before anything else, and for optional ones when convenient; write reply bodies in `<scratch_root>/issue-<N>` and record a `DISPOSITION` reply on every unsettled thread, blocking or optional, with `thread reply`; run the relevant tests and validation after making changes; commit and push; update the report and reapply the whole-PR hold rule, withdrawing the instruction if necessary; then `reviewer close` for the finished Reviewer and `reviewer launch` for the new head.
6. **Non-blocking and optional findings [verbatim]:**

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

7. **Needs-human and decisions [adapted].** Report human request-changes with the login and commit to the Developer and act only on the Developer's instruction; it is not a finding, a disposition, or a decision by itself. Do not poll for human approval. After agent approval with missing human approval, report “approved by the agent at `<full-sha>`, waiting for approval from `<approver logins>`” and go idle until a new user instruction.  Write decision bodies and amended Task files in `<scratch_root>/issue-<N>`. On a `needs_human` verdict, or before posting a `needs-human` disposition, relay the decision required to the Developer; record the Developer's answer with `decision post`, quoting the Developer; only then request another review. When the Developer amends the Task, record it with `decision post --finding none --task <file>`, which posts the complete amended section as a general decision and mirrors it into the PR body (§7.6); if `status` reports `task_body_stale`, re-run the same command; then record the standing instruction again unless the Developer said otherwise or a hold applies, and request a review of the current head even if no code changed.
8. **Approval and merge [adapted].** Verify current full-head approval through `status`; reply on every unsettled thread. Apply the rule below at creation and after every push; name any item and reason under “Areas worth extra review” and omit or withdraw the standing instruction. For `merge`, check all approved-head CI as in §7.10 and run `pr merge` from the primary checkout without another confirmation. For `approved`, report the full SHA, every optional finding and disposition, and any hold's item and reason, then wait. Only after the Developer sees and releases a hold may `--accept-merge-hold` be used. Under a standing or explicit merge instruction, integrate a moved base into the PR branch and have the new head reviewed (§7.10); stop for exhausted budget or an out-of-Task choice. `--accept-moved-base` remains the Developer's explicit choice. Wait for base-push CI if configured, then give the single final report of §7.10. If it failed, ask for a fix or revert and change nothing else. Report each cleanup and fast-forward result, including reasons and any printed command; never run that command or otherwise change the base checkout yourself. The PR description is frozen after merge and the removed issue scratch directory MUST NOT be recreated.

   **Review before merge [verbatim]:**

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

9. **Manual-intervention guard [adapted].** On a `STOPPED` prompt, or when `status` reports `stopped`: make no further review-driven changes; do not request another review automatically; preserve the PR, branch, commits, and worktree; report the reason and remaining problems to the Developer; wait for the Developer's decision about whether to continue, change approach, or terminate the work; record the actual continuation as a `DECISION` before launching again, then record the standing instruction again unless the Developer said otherwise or a hold applies. Neither standing line itself lifts a stop.
10. **Handoff discipline [adapted].** Herdr messages are the fixed lines of §8; identify code states by PR number and full commit SHA, never by round number; the forge remains the authoritative source for implementation history, review findings, inline discussion, suggestions, and finding disposition.
11. **Sandbox [new].** If the harness sandbox blocks the Herdr socket or a write outside the worktree, request escalated permission for that exact command once and report the failure rather than retrying blindly (§13.2).

### 12.3 `squad-reviewer`: mandatory rules

An outline; markers refer to the current Reviewer prompt.

1. **Verify the target [adapted].** Parse `pr=`, `head=`, `base=`, and `implementer=` from the request line; confirm that `git rev-parse HEAD` equals `head`, that `base` is an ancestor of `head`, and that `status --pr <N>` reports the same head; keep that revision pinned throughout the review and do not silently switch to a newer PR head; if the PR head has moved, post nothing, report the mismatch in the pane, and stop. Then read the derived next action (§7.9): continue only when it is `launch_review` or `reviewer_live`; when `status` reports an incomplete review by the Reviewer identity at `head`, complete it as in rule 7; otherwise the request is a duplicate delivery (§14): post nothing, report it in the pane, and stop, because a plain `review post` would publish and count a second review (§7.8).
2. **Budget guard [adapted].** Read the budget from `status`; if `remaining` is not positive, post `STOPPED` with `reason=budget` and run `handoff stopped`.
3. **Read the PR first [adapted].** Read `## Task`, the report (untrusted), every `DECISION`, every prior tagged review, and every thread through `status --json`; apply all general decisions together and the latest decision per finding (§7.6); treat settled threads as closed (§7.5); open a thread with `thread open` for every blocking unanchored finding, and for optional ones when it can; re-run the probes saved in `<scratch_root>/pr<N>` against the new head and save new ones there.
4. **Verify dispositions by execution [new].** For every thread, blocking or optional, whose disposition is newer than its last verification, run the stated verification, inspect the cited evidence, and reply `VERIFIED fixed`, `VERIFIED rejection accepted`, or `NOT FIXED` with what was run; never accept a reply on trust.
5. **Two-axis review [adapted].** Run the installed `code-review` skill as described in §12.4 from the review worktree, with `base` as the fixed point and the effective Task (§7.6) as the spec; inspect the implementation for correctness, regressions, relevant edge cases, maintainability, and compliance with the issue and repository requirements; verify material claims in the report.
6. **Severity and follow-up policy [verbatim]:**

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

   Substantive actionable findings become `blocking` threads and everything else `optional` (§7.4). v0.4.4 §29 remains the review policy. Apply the following rule to the whole PR on every pass, not only its latest changes. Neither standing decision line is a design decision or permission to lift a stop or settle `needs_decision`.

   **Review before merge [verbatim]:**

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

   When an item applies, add `## Merge hold` after `## Findings`, starting its content with `Item <n>: <reason>` or `Task: <reason>`. The hold never changes the verdict: defects are findings; unresolved Developer questions remain `needs_human` or `STOPPED` with `reason=judgement`. Only the Developer releases a hold after seeing it.
7. **Publish [adapted].** Include the non-empty `## Merge hold` immediately after `## Findings` when rule 6 applies; omit it otherwise. Write the review body (§7.3) and the threads (§7.4) to files in the scratch directory; trial-apply every suggestion; run `review post` with the verdict; if it exits 1 with unanchored findings, re-anchor every blocking one with `thread open` before handing off; after an interruption, run `status`: an incomplete review by the Reviewer identity at `head` is completed with `review post --resume <review-id>` and the same files, never with a plain `review post`, which would publish a second review; a complete current review with this header means publication finished and the pass continues with the next step; when neither exists, nothing reached the PR and the plain `review post` is repeated; then check `can_resolve_threads` in `status --json` and run `thread resolve` for each thread verified in this pass only when true; when false skip it, preserving the verification replies; publish substantive actionable findings only on the forge PR.
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

## 13. Harness Specifics

All of v0.5.0 §13 is retained.

*Informative.* There is no forge-specific harness mechanism. The #53 [recommendation](verification/2026-09-25-issue-53.md#recommendation-for-increment-4) leaves first-launch trust resolution to a person and supplies no safe post-error wait bound. Runtime and skill installation remain between PRs from merged main (§17.8); no PR reviews itself using its unmerged runtime or changed skill rules.

## 14. Failure and Recovery Semantics

This section supersedes v0.4.4 §33. In every case the PR is preserved and re-derived; no local state can be corrupted because none exists.

| Condition | Behaviour |
| --- | --- |
| Forge transport unavailable (subprocess or HTTP) | The command fails with the forge message; nothing is posted or merged partially except as §11 describes; the caller retries later and re-derives |
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
| Stranded pending draft | Adapter-specific handling: GitHub retains submit-as-comment; Forgejo's protected draft gates review post/resume, exit 4 naming the ID (E6, E8). No automatic deletion. |
| Explicit discard names wrong PR, owner, non-pending or missing review | Exit 1 before deleting anything; correct the ID or inspect authoritative state. A race or failed deletion is not permission to delete another review. |
| Explicit discard succeeds but a later post fails | The named draft deletion is not undone; re-read the PR. No other draft or submitted review is deleted. |
| Body-first post interrupted after review creation | Preserve review ID and full findings; `status` exposes completeness; resume only that publication's missing usable roots. No extra budget count. |
| Post read-back state/head/account/body mismatch | Exit 1 naming published review and written/missing roots; no implicit resubmission or edit. A pending result remains gated until explicit discard. |
| Root read-back has empty/unusable hunk | Exit 1 naming the affected finding IDs; retain full text, mark unanchored, recover with resume or thread open (E3). |
| Human approval missing or latest human request-changes in single mode | Status succeeds with `await_human_approval` when agent conditions hold; merge refuses exit 4. Report and go idle; no polling and no synthetic findings. |
| Thread resolution unsupported | Exit 1, `not supported on this forge`; skip from the Reviewer skill when capability false. |
| Branch rules not readable | Report `not visible`; do not infer no requirements. Forge refusal remains authoritative. |
| Remote branch already absent | Do not DELETE again (E7); apply only the exact-SHA tracking-ref guard. |
| Tracking ref moved or remote absence cannot be proved | Retain it and report cleanup incomplete, exit 3; do not delete another SHA. |
| Anchor outside the diff | Refused locally before posting |
| Base branch moved before merge | Exit 4; integrate base into the issue branch, validate and obtain review of the new head under the standing/explicit instruction (§7.10). Explicit moved-base acceptance or an out-of-Task choice remains the Developer's decision. |
| Forge refuses the merge (required approval, checks, head moved) | Reported with the forge's message; nothing local changes |
| Merge verification fails | Reported; branch and worktree retained; exit 1 |
| Cleanup step fails after a verified merge | Reported with the path; exit 3; `doctor` lists the residue |
| Orphaned worktree, agent, or scratch directory | Reported by `doctor`; never adopted; removed only by an explicit `reviewer close` or `review-worktree remove` |
| Submodule-dependent validation cannot run in the review worktree | Reported; the Reviewer says so in the review; the Developer decides (v0.4.4 §33.17 retained) |

## 15. Cleanup Policy

This section supersedes v0.4.4 §34.

- After consuming a result or a stop, the Implementer runs `reviewer close`; a Reviewer never outlives its review pass by design.
- Forced removal of a squad-created review worktree is acceptable because it holds nothing authoritative; the Reviewer's saved probes live in the scratch directory, not in the worktree.
- The per-PR scratch directory survives across passes and is removed by `pr merge` after a verified merge.
- `pr merge` also removes `<scratch_root>/issue-<N>` for the issue in the validated implementation-worktree ownership record (§7.10 step 4). Both scratch cleanup steps retain a symlink or a directory containing a registered worktree, report the retained path, and return exit 3. Unrelated issue scratch directories remain untouched.
- After a verified merge the implementation worktree and branch are removed (§7.10).
- The tool never runs `git clean`, never removes a worktree it did not create, and never closes a Herdr workspace that holds anything besides the Reviewer it started.
- Residue is visible through `doctor`; automatic garbage collection of old Herdr or Git resources remains outside scope (v0.4.4 §34.4).

The remote-tracking ref cleanup of §7.10 runs only after verified integration and confirmed remote branch absence, with the full approved SHA as the delete guard. A changed ref is retained and reported with exit 3. The original PR branch identity is captured before merge; a synthetic post-deletion pull ref is never a cleanup target (E9). Cleanup never depends on an unverified API claim about which head was integrated.

## 16. Testing Strategy

Use `unittest` and deterministic temporary repositories and fake processes. Automated tests never call a real model, real forge, real Herdr or container. The baseline coverage below is retained; new coverage is additional, not a reason to delete prior tests. Existing approval-output assertions describe dual mode; single-mode variants add the human conditions explicitly.

### 16.1 Unit tests

Required coverage:

- issue Task copying: ATX levels 1–6, backtick and tilde fences, LF/CRLF, exact wording, `validate_section`, closed issue/pull request/empty body refusals;
- standing instruction and withdrawal; cancellation by newer stop or Task amendment and restoration by re-recording; neither directive lifts a stop or resolves `needs_decision`; unrelated authors ignored; combined Task/budget decisions malformed and ignored; `address_findings` precedes `merge`;
- latest-review holds yield `approved` with `merge_hold`, older holds do not survive a later review without one; review validation refuses empty or misplaced holds; the rule is packaged byte for byte in both skills and both specification sections;

- parsing and rendering of the `REVIEW`, `DECISION`, and `STOPPED` headers, the finding line, the `DISPOSITION` and verification lines, and the Herdr request, result, and stop lines, including rejection of every malformed variant (wrong tag, leading zeros, extra tokens, abbreviated SHAs, wrong case, missing fields);
- the authorship rules of §7.1, including an authorized direct decision, an Implementer-posted quoted decision, and a syntactically valid decision by an unrelated author, of which only the first two alter the budget, the gates, or the Task;
- cumulative general decisions: two independent general decisions, then a budget-only extension, then an explicit revision of one decision, after which only the revised question changes;
- next-action ordering: approval at used/effective 1/1, 3/3, and 4/4 after an extension yields `approved`, and a budget-exhausting `changes_requested` yields `stopped` or `needs_decision`;
- an approved current review with an undispositioned optional thread yields `address_findings`, then `approved` after its disposition; a newer successful verification settles the thread as before, and a newer `NOT FIXED` requires a new disposition;
- `check_merge_gate` refuses undispositioned optional threads with exit 4 and names their IDs, including after forge resolution;
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
- workflow paths: `issue_scratch` resolves to `<scratch_root>/issue-<N>` when the issue is known, and is `null` otherwise;
- identity resolution: the right account per `--as`, no token in any output, no `gh auth switch`.

Additional required coverage:

- Factory selection by kind, all three construction sites, protocol stubs, no concrete adapter annotations or wire states in common derivation; Increment 1 refuses Forgejo with `forge.kind forgejo is not implemented until Increment 3` (exit 1), accepts it at configuration validation, and keeps init's default GitHub output.
- Every state/event translation in both directions, including dismissed state/flag, pending rows and unknown-state rejection; single mode always requests commented; neutral Anchor has no payload method, and both adapter single/range payloads match §11.2.
- `approvals()` includes every login's approve/request-changes history, preserves dismissal, excludes ordinary comments/pending, selects latest by instant then ID before head filtering, and cannot resurrect a dismissed/superseded approval. Configured and unrelated authors, multiple approvers, either arrival order, each failed 5a/5b condition, a request-changes at an old head, and a later approval must be covered.
- Both identity modes, mismatch diagnostics, exact-head equality, Task-amendment invalidation, current worktree identity, stops and review budget alongside the human wait; an approved last-budget-slot review still waits for a human without asking for another agent review. A `needs-human` disposition on an unsettled optional thread yields `needs_decision`, not the human-approval wait. Human reviews change no protocol count or finding gate.
- Schema 2 compatibility and every new field's default/type/value/conditional presence; URL schemes, loopback spellings, preserved path prefixes, query/fragment/credentials rejection; invalid token paths, modes, multi-line/empty files, alias containment and revalidation.
- `can_resolve_threads=false` exits 1 with exact unsupported message using a protocol stub; unknown resolution serializes null, not false; branch-rule nonvisibility does not erase requirements or hide unrelated errors.
- Protected pending gate is exit 4 with IDs; explicit discard refuses wrong owner, wrong PR, submitted or missing review; validates before deletion, deletes exactly one ID, rechecks, and never silently discards another draft. GitHub's existing stranded-draft handling remains tested unchanged.
- Full durable finding text before first root, unknown-event pending response, malformed review read-back, empty hunk, duplicate conversation anchors, comment order shuffling, multi-page lists, REQUEST_REVIEW skipping before SHA parsing, stale/official independence, offset/tie ordering, and interrupted resume without duplicate roots or review count.
- Merge refusal statuses including 409; capture pre-merge branch/head; verify integration by Git despite E9-like API fields; confirmed remote absence and expected-SHA ref deletion; absent ref, changed ref, unreadable branch, and no cleanup on failed integration.
- Every new doctor diagnostic and mode/forge applicability, including adapter-supplied version labels/failures without forge names in the five common modules listed in §11.0; packaged skill capability checks, no-poll human wait, unchanged six-item hold rule and fixed handoff strings.
- CLI table matches the 23 command paths; release version and protocol constant are tested independently. Packaging keeps no runtime dependencies.

### 16.2 Integration tests

With the fake forge and the fake Herdr, in temporary repositories:

- `pr create` without `--task` posts the copied issue and refuses a closed issue; `status --json` exposes `merge_instruction` and `merge_hold`;
- `pr merge` refuses a held PR with exit 4 naming its review, succeeds with `--accept-merge-hold`, and retains every other gate;

- per-issue scratch cleanup after a verified merge, using the owned issue number even when it differs from the PR number; symlinks and contained worktrees retained with exit 3; unrelated issue directories preserved;
- `doctor` passes on an open issue's scratch directory, warns with the path for a closed issue, fails on an unreadable issue, and never removes the directory;
- nested worktree creation and removal under `.agent-squad/worktrees`, including forced removal and the outer status staying clean;
- `reviewer launch`, `adopt`, and `close`, including blocked at startup, `agent_not_ready`, prompt failure, `agent_not_found`, a workspace with an extra pane, and an already-exited Reviewer;
- token selection by role for every mutating command, asserted from the fake forge's call log;
- `review post` with the batch rejection fallback, a stranded pending draft, and the unanchored-findings append;
- optional-thread rejection bodies: accept `Not pursued:` with a reason and `Deferred to #<open issue>:`; refuse a bare `DISPOSITION rejected`, invalid bodies, and closed, missing, or pull-request references, with the fake forge call log proving the issue read;
- `reviewer launch` refuses an undispositioned optional thread and succeeds after its disposition; the scripted workflow dispositions its optional finding before the next launch and before reaching a verified merge;
- reply enumeration per review when the flat listing omits replies;
- `thread resolve` through the fake GraphQL endpoint;
- `thread open` for an unanchored finding by each identity, with `status` reporting `open_threads` only while a blocking one exists, and `review post --resume` after an injected interruption, including its refusal when the identified review has a different header or list;
- `status` across scripted PR states covering every next action of §7.9;
- `pr merge` with `merge` and `squash`, a moved base with and without `--accept-moved-base`, a forge refusal, and a cleanup failure;
- primary checkout fast-forward to the exact verified tip with both SHAs reported and a clean status; a base tip that advances after the forge merge is distinguished from the merge commit, and the checkout remains unchanged until cleanup has run; PRs retargeted away from the configured base branch skip without a command whether the configured or PR base is checked out; interrupted merge and post-merge HEAD-read failures recover a confirmed update or report refused without changing the merge/cleanup exit status; already-current checkout; staged and unstaged tracked changes skipped with a reason and command; another branch or detached `HEAD` skipped without a command; divergent local base history and an obstructing untracked file (including an ignored file) refused by Git without losing local data; unrelated untracked files preserved while fast-forwarding; cleanup failure still followed by an attempt, retaining exit 3; a changed remote-tracking ref after verification cannot change the target; integration verification failure, including squash after a moved base, causes no attempt; skipped and refused results do not alter the merge/cleanup exit status;
- `doctor` catching each misconfiguration the live trials hit (§17.4).

Run applicable cases on each fake, preserving all GitHub regressions. Add:

- token-per-role identity test through `make_forge`; assert no token in output and no global identity switch;
- shared-account GitHub loop, independent human approval on either side of agent approval, old-head approval, human request-changes, replacement approval, merge gate and standing instruction/hold ordering;
- Forgejo transport end to end against the loopback fake: URL prefix, Authorization, safe failures, list paging, comments/replies and read-back, typed errors, permissions, version and file checks;
- body-only then roots; draft absorption if the guard were omitted; guarded refusal; exact explicit discard; interruption after every write and resume; invalid roots retaining full text and IDs; wrong returned state/head/author; unsupported resolve without any request;
- status resolution where readable and null when unreadable; all three capability flags; branch-rule read succeeds or reports not visible by capability;
- both identity modes on Forgejo's fake, including the author-review restriction; real two-account Forgejo trials are deferred (§19);
- real temporary Git refs for remote-deleted branch and exact-SHA tracking-ref removal on both adapters; changed refs and unproved absence retained; API merged-head discrepancy does not alter approved-SHA ancestry/tree checks;
- init's new flags and no-overwrite behaviour, every doctor mode/forge failure, and existing first-launch trust/adopt behaviour unchanged.

### 16.3 Smoke scenario

`scripts/run-smoke-tests` (`make smoke`) runs the whole loop in a temporary repository against both fake forges and the fake Herdr, driving both roles through the CLI with scripted content and no model:

1. `init`; `doctor`.
2. Create the issue worktree, commit a candidate, push to the fake remote, `pr create` with both sections.
3. `reviewer launch`; a scripted Reviewer posts a `changes_requested` review with two blocking findings and one optional finding, then `handoff review-result`.
4. `status` shows `address_findings`; `thread reply` records `fixed` and `rejected` dispositions; a `needs-human` disposition is shown to block `reviewer launch` until a `DECISION` names it.
5. Commit, push, `pr report`; `reviewer close`; `reviewer launch`; the scripted Reviewer verifies both dispositions, resolves the threads when the capability permits, and posts `approved`; `status` shows `approved`.
6. A later push invalidates the approval: `status` shows `launch_review`, not `approved`.
7. The third review, at the new head, posts `changes_requested`; with `max_review_passes` 3 the scripted Reviewer posts `STOPPED` with `reason=budget` and `handoff stopped`; `reviewer launch` is refused (exit 4).
8. `decision post` with `budget=4`; `reviewer launch` succeeds; the fourth review approves.
9. `pr merge` is refused while the base branch has been advanced on the fake remote, then succeeds with `--accept-moved-base`; integration is verified by ancestry with `merge`; a second PR on the same fake repository has a `## Merge hold`, refuses merge naming its review, then merges only with `--accept-merge-hold` using `squash` on a base that has not moved, verifying tree identity; after each merge the issue worktree, branch, review worktrees, and both per-PR and per-issue scratch directories are gone, and the primary checkout is at the newly verified base tip with a clean status.
10. A ready issue is copied into the Task and a standing instruction is recorded at creation. Lost notification: with the fake Herdr set to fail `agent prompt`, a scripted approval is posted and `handoff review-result` fails; `status` still reports the current review and `merge`. The PR merges under the standing instruction without a separate merge instruction, with verified integration and cleanup.
11. Fallback: with the fake forge set to reject the batch, the GitHub fallback or Forgejo body-first path posts every finding's full text and then its roots individually; with one blocking root also failing, `status` shows `open_threads`, `thread open` recovers the finding from PR data alone, and the loop continues with one logical review counted; an interrupted `review post` is completed with `--resume` or, when nothing reached the PR, repeated, and is counted once.
12. Cleanup verification: no tracked runtime files, no registered review worktrees, the temporary root removed, retained resources reported on failure.

The runner MUST NOT commit an intentional defect into a real development checkout, and it MUST work from a source export without `.git`, as today.

The twelve logical steps remain the same. In the single-mode variant, supply scripted human approvals before approval-dependent assertions, exercise one human request-changes and its replacement approval, and assert the idle human-wait state. Fake Forgejo roots use body-first posting; unsupported resolution is skipped. Exercise its protected draft gate, explicit discard and interrupted resume. These are scripted fixtures, not release trials. Record command counts per scenario; do not pretend the new Forgejo transport has the same request count as GitHub. Increment 1 specifically records the unchanged GitHub twelve steps and command count at `795be79`, and a byte comparison of the smoke PR-state status JSON before/after with only capability additions removed.

### 16.4 Live trials

Before the v0.6.0 release, record these four trials, each in one agent direction. v0.5.0 already proved both directions; the direction does not depend on the forge.

1. **GitHub regression:** after Increment 1 merges, install that exact main runtime with `uv tool install --force`, verify the installed source commit and `agent-squad --version`, and run the next Agent Squad PR (#57) through the loop on it. That PR's implementation report names the runtime commit and carries trial 1 evidence. Later PRs repeat the regression.
2. **GitHub single identity:** on `MagiLand/agent-squad-trial`, both agents act as `patrickhe`; the human operates `patrick-magiland` by hand as approver. Include a human request-changes, a resulting Developer instruction, a new reviewed head, and a valid replacement human approval and verified merge.
3. **Local Forgejo single identity:** on a disposable container pinned to 16.0.3, include a human request-changes and one interrupted review post recovered by `--resume`, then valid approval and verified merge. #54's API experiments are prerequisites, not this loop trial.
4. **Network Forgejo single identity:** on the Developer's authorized VPS instance pinned to 16.0.3, use HTTPS API transport, SSH pushes and branch protection requiring one human approval; reach a Git-verified merge and ownership-safe cleanup. This is the rehearsal for the client pilot, not use of the client's repository.

Two-account Forgejo mode is implemented and fake-tested but not live-trialled. Record it as unverified and create the independently required follow-up issue when preparing the release. No Codeberg trial is required. The client pilot is supervised after release and is not a release condition. Record actual completion, not a planned trial, as evidence.

For trials 3 and 4, record the actual merge method, whether cleanup explicitly deleted an existing branch or found it already absent, and whether PR-body PATCH and issue GET were exercised. A path that neither trial exercises remains explicitly unverified in release evidence (§19); source-backed fake coverage does not close that gap.

### 16.5 Evidence record

Each increment involving a live exercise adds `docs/verification/<YYYY-MM-DD>-issue-<N>.md` with these sections in order: **Implementation baseline** (full runtime and code SHA under test); **Commands executed** (exact command and observed outcome, test counts and durations); **Live trials** (direction, repository, PR number, reviewed full heads, review IDs, verdicts, human approvals/request-changes, decisions, stops, merge commit and method); **Harness versions** (Herdr, Codex, Claude Code, and forge client/server as applicable); **Defects found and fixed**; **Scope and limitations** (scripted versus real, unverified behaviour); **Cleanup** (owned workspaces/worktrees removed, repositories archived/deleted, retained resources). Private raw evidence stays outside the repository; record archive digests without credentials, client content or private infrastructure addresses.

Cite experiment IDs and versioned source paths for server claims. Preserve observed errors and surprising results; expected behaviour is not a substitute for a failed request. For #56 record the boundary grep and output, smoke step/command counts, status comparison, and the runtime installed after merge. Because the source commit being merged cannot contain its own future installation evidence, put that installation read-back with trial 1 in #57's evidence and link it from #56's pre-merge record. Do not edit a merged PR body or recreate removed scratch to append evidence. For all trials distinguish the approved head, the integration commit, the fetched base tip, and any local fast-forward target.

### 16.6 CI layout

Keep the implemented split in `.github/workflows/test.yml` and `tests/ci_groups.json`: five parallel pull-request jobs (`doctor`, `reviewer`, `forge-commands`, `smoke`, `rest`) cover every ordinary unit and integration module on every change. `main-only` runs the source-export smoke and package-build tests on main pushes and the nightly schedule; `macos` runs the full suite there. The committed grouping file, group runner and guard test remain the single assignment mechanism: fail missing/duplicate/unregistered modules or a group absent from the workflow. The runner sets checkout src ahead of inherited non-empty PYTHONPATH entries. Preserve the current Python 3.11 Ubuntu and Python 3.12 macOS guards and existing action/runner choices. `make test` still runs the whole suite. Rely on the forge's nightly failure notification, not a new scheduler or CI orchestration service.

## 17. Implementation Increments

Each increment is one independently reviewed issue/PR in milestone v0.6.0. The prerequisites are the completed chores, #54 experiments and this #55 delta. Follow the plan's linear order; unchanged earlier features remain covered at each step. The full release specification does not authorize an early increment to implement later scope.

### 17.1 Increment 1 — #56: Protocol and unchanged GitHub adapter

Introduce `Forge`, neutral records/Anchor/vocabulary, capabilities, `approvals()`, and the factory; switch the three construction sites and concrete annotations. Move wire maps and payloads into the GitHub adapter. Accept `forge.kind=forgejo` in validation but refuse construction with exactly `forge.kind forgejo is not implemented until Increment 3` (exit 1); init still writes github. No HTTP transport, token files, single identity or other schema fields arrive here.

Done when the existing fake gh and tests remain unchanged except internal maps/Anchor payload assertions moved to adapter tests, all repository commands pass, and the issue's boundary grep, twelve smoke steps/command count and status comparison have recorded evidence. The executable command-surface test reads this delta's §10.2 from this increment (same 23 command paths). Keep single-identity, draft flags and later options staged for their increments, not implied by a command-name test. Install the merged main runtime only after merge; trial 1 completes on #57 (§16.4).

### 17.2 Increment 2 — #57: Single identity on GitHub

Add identity_mode/approver_accounts, per-mode validation, comment-state agent reviews, exact-head human approval conditions, status evidence/wait, merge gates and skill reporting. Preserve distinct-account mode, all standing instruction/hold rules already implemented by #74, no-poll discipline and ordinary review budgets. Do not implement Forgejo transport.

Done when the full single-mode approval/ordering tests and GitHub fake loop pass, trial 1 names the installed #56 runtime, and trial 2 reaches a verified merge after a human request-changes. Skills/runtime are upgraded between PRs only; the trial uses an explicitly recorded merged implementation, not this PR to review itself.

### 17.3 Increment 3 — #58: Forgejo reads and fake server

Implement the standard-library transport, per-role token files, base_url configuration validation, minimum version check, read parsers, neutral approval history, capabilities and status against Forgejo. Add the loopback fake seeded by #54 and defensive cases of §11.3. Replace Increment 1's factory refusal. Unsupported writes fail clearly until Increment 4; no silent use of the GitHub adapter.

Done when offline unit/integration reads exercise response validation, identity, token safety, paging, comments, timestamps, current-head approval and branch-rule visibility through the real transport against the fake. No required real write trial is invented for this increment.

### 17.4 Increment 4 — #59: Forgejo writes and tracking-ref cleanup

Implement PR creation/reporting, decisions/stops, body-first review posting, protected draft gate and exact explicit discard, read-back, roots/replies, thread opening and capability refusal, resume, guarded merge and branch deletion. Add exact-SHA remote-tracking-ref cleanup on both forges. Preserve Git-based integration proof even when post-merge PR fields disagree.

Done when every write/recovery/failure path is fake-tested, interruptions preserve full findings and count once, and merge cleanup keeps changed refs/unproved resources. #53 recommends no launch wait; §§8.3/8.4 remain unchanged. Do not invent a launch fix to fill the increment's name. Live loop trials 3/4 are release work after this runtime merges.

### 17.5 Increment 5 — #60: Init and doctor

Add the final init flags and every mode/forge diagnostic in §10.3, including token-file revalidation, user/approver existence, permissions, version, URL and repository selection. Preserve schema 2 compatibility, no overwrite, SSH alias path derivation, Herdr/trust/skill/ownership checks and orphan reporting.

Done when tests exercise every new validation and wrong-role/permission configuration, make doctor works on the supported configured setups, and observed limitations are recorded without granting skills token access or altering SSH configuration.

### 17.6 Increment 6 — #61: Release

Complete smoke on both fakes, README and workflow documentation, end-to-end example, protocol-constant release audit, package version `0.6.0`, release trials 3 and 4, and evidence consolidation. Two-account Forgejo mode stays marked unverified by live trial with its follow-up issue. Run `make test`, `make smoke`, `make doctor`, source-export smoke and package build; verify the installed runtime identity. Tag/release only the verified merged commit under the required Developer release instruction and review-before-merge rule.

Done when the four required trials and deterministic checks pass, each evidence record distinguishes runtime/head/merge SHAs and states its limitations, and a human can follow the documented single-identity workflow without credentials entering prompts or repository data.

### 17.7 Definition of done

A Developer can start a ready issue and obtain exact-revision agent review on either forge, with dual or single identity as configured. Every unsettled thread is dispositioned, every settled disposition verified, and stops and Task changes remain under Developer authority. Single mode waits visibly and without polling for an independent human decision; valid agent and human approvals may arrive in either order. A standing instruction proceeds only when all conditions and holds allow; CI evidence is checked by the Implementer, the forge may still refuse, and actual integration is verified before guarded cleanup and the permitted primary fast-forward.

The planned schedule targets 2026-10-02 with 2026-10-05 as the hard date. Preserve the plan's contingency: if behind on 10-01, two-account Forgejo fake coverage is the first scope to drop, trial 4 moves after release, and any launch fix is follow-up work. Such a cut MUST be recorded explicitly in the release scope/evidence and Developer decision, not silently reported as completed coverage. On current #53 evidence there is no launch fix to defer.

### 17.8 Instructions to the implementing agent

Follow v0.4.4 §45 with its item 11 replaced by the fixed semantics of this delta: do not improvise around tagged grammars, authorship, IDs, dispositions, decisions, stops, budgets, exact-head approval, human request-changes, moved-base and squash checks, role-selected token handling, fixed Herdr lines, pending-draft deletion guards or ownership-safe cleanup. Where detail is unspecified choose the smallest design preserving exact review, human authority, visible recovery and the adapter boundary; material choices require the Developer.

Development uses the v0.5.0 loop with a fresh Reviewer per pass. Use a ready issue as Task under the amended baseline and plan decision 33; the issue's historical “Task approved once” wording does not reinstate routine approval of a reworded ready issue. Upgrade the installed runtime and skills only between PRs from merged main, never from the branch being reviewed. Review held authority/credential/cleanup changes before merging. This specification PR changes documentation only and does not write configuration, install code, or change the schema version.

## 18. Guarantees, Non-guarantees, and Simplifications

### 18.1 Reliability guarantees

Within the operating assumptions of §3.3, v0.6.0 MUST guarantee:

1. A review, disposition, decision, or stop exists on the PR before any Herdr message about it is sent.
2. Neither an agent approval nor a required human approval can authorize a head other than exactly the reviewed head. In single mode both approvals and the absence of a latest human request-changes are required; a later head invalidates earlier approval.
3. The Reviewer reads a detached worktree at the exact head, never the Implementer's working tree.
4. The Reviewer needs no write access to the implementation worktree.
5. The loop's state is fully recoverable from the PR, Git, and Herdr after any local interruption.
6. A lost Herdr notification never leaves the loop silently stalled: `status` reports the current review, stop, or next action.
7. Duplicate prompt delivery cannot create a duplicate logical review, budget effect, or state transition (§12.3 rule 1).
8. Every blocking finding is dispositioned before the next review, and every disposition is verified by execution before a thread is settled.
9. A `needs_human` verdict or a `needs-human` disposition cannot produce another automatic review without a recorded decision.
10. The review budget is derived from the PR and cannot be exceeded without a recorded decision.
11. A stop blocks further automatic reviews until a decision is recorded.
12. Nothing merges without applicable agent and human approval under §7.10 and the Developer's instruction, given when the issue starts or later; a PR under the review-before-merge rule waits for the Developer's review. A merge is verified by ancestry or tree identity; a moved base requires integration and fresh review or explicit Developer acceptance.
13. Every forge mutation selects exactly one configured role account; single mode maps both roles to the same account. Tokens are handled only by the adapter and never printed; role separation in single mode is not forge-enforced.
14. The normal loop proceeds without Developer message relay.
15. A `DECISION` by an author other than the Implementer identity or a configured Developer login has no effect. In single mode the shared account is in that set; the tool cannot distinguish which agent used it. A human approver does not gain decision authority merely by being an approver.
16. A finding listed by a tagged review cannot be lost by a partial posting failure or an interruption, because its full text reaches the PR before any root is attempted, and approval cannot overlook a blocking one.
17. A material Task amendment invalidates every earlier approval.

### 18.2 Non-guarantees

v0.4.4 §40 is amended: the items about Herdr delivery, malicious same-user agents, multi-machine consistency, automatic recovery from every failure, self-review of control-plane changes, CI success, submodules, and model review replacing human review are retained. Added: no guarantee against forge outages, rate limits, or forge-side data loss; thread resolution state is not authority; the forge may reject or partially publish a review for reasons outside the tool's control, so the Reviewer re-derives the record and resumes missing roots explicitly rather than assuming nothing was written; the tool does not verify that CI ran on a head. Whether an item of the review-before-merge rule applies is an agent's judgement; a missed item can merge without the Developer's review.

In single mode the two agents are not isolated by forge identity. Either could produce text permitted to the other using the same account; role separation is a convention, not a security guarantee. The independent human approval and the forge's own branch protections are separate checks. Do not claim this prevents malicious same-user agents or a compromised shared credential. A preflight draft check and a later write are not atomic against concurrent human/agent activity on that account. The version support range does not imply a live trial on every release or deployment configuration.

### 18.3 Deliberate simplifications

v0.4.4 §41.1 to §41.4 no longer apply because there is no local state, lock, outbox, or delivery retry. v0.4.4 §41.5 to §41.9 are retained. Added:

- **The PR instead of local state.** Deriving everything from the PR removes the state file, the lock, the bundles, the markers, the classification of stale and invalid results, and the recovery commands that existed to keep them consistent.
- **Counting instead of a budget ledger.** Reviews are counted; extensions are decisions.
- **A fresh Reviewer instead of a persistent one.** The PR carries continuity; a per-PR scratch directory carries probes.
- **One bounded protocol, two adapters.** The typed Forge interface isolates transport, payloads, capability limits and state translation. It is not a plugin system, multi-forge repository router, or generic forge SDK.
- **Fixed strings instead of templates.** The tagged lines and Herdr lines are literal grammars, not a template language.

## 19. Non-goals, Deferred Items, and Unverified Behaviour

The relevant non-goals of v0.4.4 §8 remain: no orchestration platform, sandbox guarantee, automatic requirement/design decisions, persistent workflow state or replacement for human review. Forgejo and single identity are now in scope; the old v0.5.0 deferrals for them are superseded.

Still out of scope: rebase merge method, merge queues or forge auto-merge, merging on a Herdr message alone, tool-level CI orchestration/enforcement/interpretation, stacked/dependent PRs, automatic garbage collection, Windows, harnesses other than Codex and Claude Code, GitHub Enterprise, Forgejo below 16.0.0, Codeberg trials, and client rollout as a release condition. Routine merges under recorded Developer standing instructions are permitted; that is distinct from unconditionally automatic merging. Specification consolidation is separate documentation work after release.

The following limits MUST remain visible in release evidence. An unverified fact is never silently promoted to a server guarantee:

| Unverified or bounded observation | Source and safe implementation treatment |
| --- | --- |
| Comment order variation was not reproduced; three lists had the same order | E1. Sort by ID and shuffle the fake; do not claim ordering is guaranteed or experimentally random. |
| Non-UTC timestamp ordering and full pagination boundaries were not live-tested | #54 scope; F16 timestamp/list-handler contracts support defensive parsing. Test offsets, ties and multiple pages synthetically; fail malformed responses. |
| Resolved-root resolver behaviour was not exercised; no product resolution API is supported | F16 structs/conversion expose resolver, but #54 did not resolve a root. Read known resolver data only, use null for unavailable state, keep resolution nonauthoritative and refuse thread resolve. |
| Returned comment locations on blamed/unchanged lines were not exhaustively tested | F16 conversion uses stored comment line/commit; E5 verifies one new-side range only. Derive the head anchor from a usable hunk; otherwise leave the finding unanchored and recover explicitly. |
| Staleness computation and the push effect on a still-valid approval were not isolated | E4/E9: the earlier approval was already dismissed before the push. Ignore stale/official for protocol approval; require exact head and latest decision, with dismissal preserved. |
| PR 1's actual integration head/tree and cause of post-deletion head discrepancy are unknown | E9 did not fetch the integration commit. Save the approved SHA/branch before merge and verify with Git; failure retains resources and refuses cleanup/fast-forward. |
| Branch DELETE for an already absent branch returned 500 | E7. Confirm absence first and skip DELETE. Do not reinterpret that recorded error as an expected successful no-op. |
| Squash merge and successful explicit DELETE of an existing branch were not exercised by #54 | F16 merge form and merge/branch handlers (§11.4) supply synthetic, source-backed fake cases (§11.3). Keep the squash tree-identity check of §7.10 step 3; confirm DELETE success only with the follow-up branch GET. Trials 3/4 record whether each path ran, retaining unverified status when neither did. |
| PR-body PATCH and issue GET were not exercised by #54 | F16 EditPullRequest/EditPullRequestOption and GetIssue (§11.4) supply synthetic, source-backed fake cases. Validate issue identity/shape and read back the updated PR body; fail a mismatch rather than assume success. Trials 3/4 record actual coverage. |
| No authorized Forgejo CI-evidence read path in the current skill allowance | §10.4 permits GitHub CI reads only; decision 33 keeps the tool itself out of CI. For a Forgejo repository with PR checks, the Implementer reports the gap and waits for the Developer to supply evidence or authorize a read path before merging, even under a standing instruction. A tool-level commit-status read requires a Developer decision; the available status endpoint (#54 recording 066) does not grant that authority. |
| Concurrent human draft creation between check and post was not tested or made atomic | E6 proves absorption, not concurrency prevention. Gate known pending drafts, require explicit named discard, never automatically delete an unexpected draft; advise avoiding simultaneous review publication on the shared account. |
| Other versions, TLS/SSH deployment and client configuration were not established by #54 | Scope of #54. Trials 3/4 supply loop and network evidence; minimum 16.0.0 is policy, reference 16.0.3, with no version-specific behaviour. |
| Two-account Forgejo loop has no release live trial | Plan decision 18. Implement and fake-test it, label unverified, track a follow-up at release unless the explicit schedule contingency removes fake coverage too. |
| GitHub dismissed-review history is source-documented, not a #54 experiment or a new live GitHub trial | The adapter joins the documented dismissal event by review ID for approvals (§11.0). Missing/ambiguous original state fails validation; never infer a kind or revive an older approval. Test the mapping and keep live-trial scope explicit. |
| Historical Reviewer blocked-to-idle transition is unexplained | #53 recommendation. Keep retained-resources exit 3 and person-assisted adoption; no new wait or polling policy. |

## 20. Deviations from the Plan

**Departures from the plan's decisions: none.** Later decisions explicitly amend earlier decisions as recorded in the plan; this is not a new choice by this delta. The evidence-driven refinements below replace hypotheses with #54 observations without changing the decisions.

| Plan §10 decision | Disposition in this delta |
| --- | --- |
| 1 Scope | §§3, 11, 17, 19 |
| 2 Timeline | §17.7; client pilot remains after release |
| 3 Client facts / colleague approval | §§4.5, 7.10, 16.4 |
| 4 v0.5.0 housekeeping | Historical; no repeat tag/release or trial-repository archival |
| 5 Development via installed v0.5.0 loop | §17.8; upgrades only between PRs |
| 6 Delta only | §§1–2; consolidation deferred |
| 7 Forge-independent modes / schema 2 | §§4.5, 9 |
| 8 Conventional single-mode role separation | §§4.5, 18.2 |
| 9 Independent exact-head human approval | §7.10 conditions 5a/5b; either order |
| 10 Human request-changes outside protocol | §§7.5, 7.9, 12.2 |
| 11 No Forgejo resolve or marker emulation | §§10.2, 11.0, 12.3 |
| 12 Protocol, gh, urllib, zero dependencies | §§5.6, 11 |
| 13 Role token files | §§4.5, 9–10 |
| 14 Loopback fake / HTTP restriction | §§9, 11.3 |
| 15 Protected draft / explicit discard | §§7.3, 7.9, 11.4, 14 |
| 16 Pinned local write experiments | #54 recorded evidence, §1; no rerun claimed |
| 17 Five-job CI split and guard | §16.6; existing implementation retained |
| 18 Forgejo dual mode untrialled / first cut | §§16.4, 17.7, 19 |
| 19 Protocol tag unchanged | §§1, 7.1, 16.1 |
| 20 Minimum version / range support | §§9, 11.2, 11.4 |
| 21 Investigate before launch fix | §§8, 13, 17.4: #53 recommends no wait |
| 22 Idle human wait, no polling | §§7.9, 12.2 |
| 23 Explicit init forge / doctor verifies | §§9–10 |
| 24 Four release trials / no Codeberg | §16.4 |
| 25 Work order | §17 |
| 26 Every unsettled thread dispositioned | §§7.5, 7.9, 7.10; #64 retained |
| 27 Optional rejection reasons / reporting | §7.5 and §§12.2–12.3; decision 33's report timing retained |
| 28 Task acceptance failures are blocking | §12.3 verbatim policy |
| 29 Chores amend v0.5.0 baseline | §1 baseline includes amendments; §16.6 retains guards |
| 30 Per-issue scratch / frozen merged PR / filtered JSON | §§5, 12.2, 15; #66 retained |
| 31 Recorded standing instruction | Retained §7.6 as amended by decision 33; §§7.9–7.10 |
| 32 Verified-tip primary fast-forward | §7.10 step 5, unchanged #69 guards |
| 33 Ready issue, ordinary merge authority, holds, CI and moved-base review | Retained §§7.2, 7.6–7.8; §§7.9–7.10, 12.2–12.3, 17.8; no new approval bypass |

**Refinements from evidence and precise interface definition:**

1. E2 selects body-first review publication with roots added to the submitted review; E6 requires the draft gate even for that body-only first write. Durability uses the existing unanchored-findings body and explicit resume, not a new protocol tag.
2. E5 fixes the range request at the start line with end-minus-start additional lines. The plan gave the count but left the tested start mapping to the experiment. The fallback is unnecessary because the mapping worked.
3. E7 distinguishes approval refusal (405) from an isolated head guard (409), and records absent-branch deletion as 500. Cleanup reads before deleting; no expected status is substituted for the recording.
4. E4/E9 establish that stale flags cannot replace exact-head logic. E9 requires integration proof independent of post-deletion PR fields; that strengthens the already-required Git verification rather than inventing a server guarantee.
5. E1 did not reproduce varying order. Sorting remains defensive; the fake's shuffled output is explicitly synthetic. Source-backed but untrialled resolver/paging/offset cases remain listed in §19.
6. The method table and neutral records make the plan's interface implementable; request signatures may use equivalent typed records, but transports never leak into protocol rules. A pending draft is a review-publication gate (exit 4), unsupported resolution is exit 1, and the human wait is a successful status read with a refused merge (exit 4).
7. Token-file checks use actual filesystem identity and redirects must not leak credentials; these implement the plan's outside-worktree, owner-only and explicit-host constraints without granting skills new credential authority.
8. #56's future installation record belongs with #57's trial 1 evidence because installation occurs only after merge and merged PR descriptions are frozen. This is a provenance placement rule, not permission to omit the runtime SHA.
9. Doctor labels and transport-specific failures are adapter-supplied (§§10.3, 11.0), preserving plan §4.1's boundary for all five common modules while retaining the existing displayed GitHub label.
10. Decision 33 and the existing skill read allowance leave no authorized Forgejo CI-evidence read path. The resulting Developer wait (§19 and Appendix A) is a visible limit on the plan's fewer-turns objective, not a new CI capability or a departure from that decision.

No amendment to the decided product scope is implied by a refined failure message, a source-backed defensive parser, or the retained #53 trust behaviour.

# Appendix A: Example End-to-End Run

*Informative.* This example uses a disposable Forgejo repository with single identity. `agent-user` is the shared account; `human-reviewer` is a distinct person-operated account. The token files already exist outside every worktree with private permissions; no token values are passed in commands. The API host below is illustrative. This example requires the merged runtime implementing all relevant increments, not the current v0.5.0 runtime.

```bash
# Once, inside the primary checkout whose ordinary Git transport already works.
agent-squad init --forge forgejo --base-url https://forge.example.test   --implementer-account agent-user --reviewer-account agent-user   --implementer-token-file /private/agent-squad/agent.token   --reviewer-token-file /private/agent-squad/agent.token   --identity-mode single --approver-account human-reviewer
agent-squad skill install
agent-squad doctor
agent-squad doctor --live-reviewer
```

The Developer invokes squad-implementer and starts ready issue 101. The Implementer uses the issue unchanged as Task, creates the dedicated issue worktree from fetched main, implements, validates and pushes `feat/issue-101-example`. Report and reply files live in `paths.issue_scratch`. The forge assigns PR 102. There is no merge hold in this example.

```bash
agent-squad pr create --as implementer --issue 101 --report "$ISSUE_SCRATCH/report.md"
# The file opens with: Standing merge instruction: merge when approved.
# It then quotes the Developer's start instruction.
agent-squad decision post --as implementer --pr 102 --finding none   --body "$ISSUE_SCRATCH/merge-instruction.md"
agent-squad reviewer launch --pr 102
```

The CLI sends the unchanged full-SHA request line to the new detached Reviewer. The Implementer goes idle. The Reviewer independently evaluates the Task, saves inputs/probes in `paths.scratch`, and posts `changes_requested`. The adapter sends a COMMENT review with that verdict in the protocol header (single mode, supported by E1/E8), then adds roots (E2). A protected pending draft would stop publication with exit 4 and its ID; the person decides whether the exact named draft may be discarded. It is never discarded automatically.

```bash
# REVIEW_HEAD and REVIEW_BASE are the full SHAs from the fixed request line.
agent-squad review post --as reviewer --pr 102 --head "$REVIEW_HEAD"   --base "$REVIEW_BASE" --verdict changes_requested   --body "$PR_SCRATCH/review.md" --threads "$PR_SCRATCH/threads.json"
agent-squad handoff review-result --pr 102 --head "$REVIEW_HEAD"   --verdict changes_requested
```

If interrupted after publishing the body, the Reviewer re-reads status and resumes the reported review ID with the same head/base/body/threads and `--resume <review-id>`. This creates only missing usable roots and does not increase the review count. The full finding text already exists on the PR.

The Implementer reads status, fixes the blocking finding, records any optional rejection with `Not pursued:` and its reason, validates, commits, pushes and updates the report. It replies `DISPOSITION fixed <full-new-commit>` on the fixed thread, closes the finished Reviewer, and launches the new head. The fresh Reviewer verifies dispositions by execution and posts approval. It skips thread resolve because the capability is false; verification replies still settle the findings.

```bash
agent-squad status --pr 102 --json
agent-squad thread reply --as implementer --pr 102 --finding REV-1   --body "$ISSUE_SCRATCH/rev1.md"
agent-squad pr report --as implementer --pr 102 --report "$ISSUE_SCRATCH/report.md"
agent-squad reviewer close --pr 102 --head "$REVIEW_HEAD"
agent-squad reviewer launch --pr 102
```

After the approved handoff, status shows `await_human_approval`. The Implementer reports the exact head and `human-reviewer`, then goes idle. The human requests changes on the forge. On the Developer's “check PR #102”, the Implementer reports that login and commit; it makes no synthetic protocol finding. The Developer supplies the actual change instruction. If scope changes, record the complete Task amendment before implementing; otherwise record the instruction as appropriate. Implement, validate, push, disposition any agent findings, and obtain a fresh agent approval at the new head.

The person gives a formal approval at that exact head. That new approval supersedes their request-changes (E9 demonstrates the server-side review sequence; §7.10 is the independent protocol rule). The human could instead have approved before the agent. A subsequent “check PR #102” produces `merge` only when both approvals, dispositions and the standing instruction are valid and there is no hold. If this Forgejo repository has PR checks, the current skill allowance provides no authorized CI-evidence read path: the Implementer reports the gap and waits for the Developer to supply evidence or authorize a read path (§19). The standing instruction does not bypass that wait. Once the applicable CI evidence has been checked under §7.10, the Implementer runs from the primary checkout:

```bash
agent-squad pr merge --as implementer --pr 102
```

The tool sends the approved head as the merge guard, fetches and verifies its ancestry (or exact tree for an unmoved-base squash), confirms original branch absence, deletes only the matching remote-tracking ref, removes owned issue/review worktrees and scratch, and attempts the permitted primary fast-forward. API post-deletion head fields are not inclusion proof (E9). The Implementer checks configured base-push CI and sends the final merge/CI/cleanup/fast-forward report with every optional finding and disposition. Any hold would instead require the Developer's review and explicit merge instruction before using `--accept-merge-hold`.
