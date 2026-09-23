# Agent Squad v0.6.0 Development Plan

Status: draft, decided with the Developer on 2026-09-21. This plan is the input to the v0.6.0 specification delta (Increment 0b below) in the same way that `docs/agent-squad-v0.5.0-plan.md` was the input to `docs/agent-squad-v0.5.0-spec.md`. Where this plan and the delta disagree once the delta is approved, the delta governs.

Inputs: `docs/agent-squad-v0.5.0-spec.md` §19 (the two deferred items), the v0.5.0 release record `docs/verification/2026-09-16-issue-46.md`, and a survey of the v0.5.0 code at `795be79`.

Background research: a read-only survey of the Forgejo source and public Codeberg data, kept locally at `.local/research/v0.6.0/forgejo-api-facts.md`, supplied the hypotheses below. The ignored file is not a project input. Increment 0a (#54) records the write experiments; the specification delta (#55) cites recorded evidence or versioned Forgejo source references for each behaviour.

## 1. Purpose and scope

v0.6.0 delivers the two items §19 of the v0.5.0 delta deferred, plus six small operational items the v0.5.0 release trials and the first loop PRs exposed:

| Item | Why |
| --- | --- |
| Forgejo as a second forge | The Developer's client projects are moving to a self-hosted Forgejo instance |
| A single-identity mode | The client's instance allows one account per person and requires formal approval from a separate human colleague |
| Faster per-change CI | Every CI run took 8 to 13 minutes; the integration suite is 467 s locally and a second adapter adds tests |
| The `agent_not_ready` Reviewer launches | Two of three fresh Claude Reviewer launches in the release trials returned `agent_not_ready` with no visible prompt and needed the Developer's "Adopt both PRs"; that breaks guarantee 14 of §18.1 ("the normal loop proceeds without Developer message relay") |
| Stale remote-tracking ref after merge | Both trial cleanups removed `refs/remotes/origin/<branch>` by hand after the forge deleted the branch |
| Accountability for optional findings | PR #62, the first PR through the v0.5.0 loop, was approved on the first pass with four optional findings; none received a reply, the report did not mention them, and the PR merged eighteen minutes later. Under the v0.4.4 loop all 34 optional threads on PRs #41 to #51 were answered and 15 applied. The v0.5.0 delta's "MAY receive a one-line disposition or none" (§7.5) and the blocking-only `unaddressed_findings` gate caused the change |
| A home and a lifetime for the Implementer's files | After PR #62 merged, 1460 files (20 MB) remained under `.agent-squad/review-scratch/issue-52-*`: CLI input files, validation logs, saved `--json` responses, an unpublishable post-merge PR description, and a 19 MB virtual environment. The spec gives the Reviewer `pr<N>` with creation, removal, and orphan reporting; the Implementer has no named directory, so `pr merge` and `doctor` ignore its files |
| Fewer Developer turns after approval | After every approval the Developer gives the merge instruction and then fast-forwards the primary checkout, or asks the Implementer to, because `pr merge` only prints the command (§7.10 step 5). For a routine Task the merge decision can often be made when the issue starts |

Non-goals, unchanged from §19 unless listed here: CI result enforcement or interpretation, the `rebase` merge method, automatic garbage collection of Herdr or Git resources, stacked PRs, Windows, any harness other than Claude Code and Codex, GitHub Enterprise hosts, and Forgejo instances older than 16.0.0.

The protocol tag stays `AGENT_SQUAD/0.5.0`: no tagged-line grammar changes in this release. The package version becomes `0.6.0`. The release audit checks the protocol tag against a declared constant instead of requiring it to equal the package version.

## 2. The client scenario

Facts supplied by the Developer on 2026-09-21:

- Forgejo `16.0.3`, self-hosted; `fj` `0.6.0` is installed locally but is not used by the tool (§4.3).
- One account per person. Both agents therefore act as the Developer's account, which is also the PR author.
- A separate human colleague gives the formal approval, and the forge enforces it through branch protection.
- After that approval the Implementer merges on the Developer's instruction, as `pr merge` does in v0.5.0.

Forgejo refuses `APPROVED` and `REQUEST_CHANGES` reviews from the PR author with HTTP 422 (`preparePullReviewType`, unchanged from 7.0 through the development branch) but accepts a `COMMENT` review with a body and inline comments from the author. The agent Reviewer's verdict therefore lives in the `REVIEW` header of a `COMMENT` review, exactly as §7.3 and §19 of the v0.5.0 delta anticipated, and the colleague's approval is read from the forge review state.

## 3. Identity modes

The mode is a property of the configuration, not of the forge. Both modes work on both forges; GitHub also accepts an author's `COMMENT` review.

### 3.1 `dual` (the v0.5.0 behaviour, unchanged)

`implementer.forge_account` and `reviewer.forge_account` differ; the Reviewer's forge review state mirrors the header verdict; approval validity is §7.10 conditions 1 to 6 unchanged.

### 3.2 `single`

- `implementer.forge_account` and `reviewer.forge_account` are equal; `identity_mode` is `"single"` explicitly (§5).
- The Reviewer posts every review as `COMMENT`. The `forge_state_mismatch` diagnostic of §7.3 fires when the state is anything else.
- `approver_accounts` (§5) names the human logins whose formal approval counts. It is required and non-empty in this mode.
- Approval validity replaces §7.10 condition 5 ("forge review state is `APPROVED`") with two conditions: (5a) some login in `approver_accounts` has an approval review at exactly `pr.head` that is not dismissed and is that login's latest approve-or-request-changes review; (5b) no login in `approver_accounts` has a request-changes review as its latest approve-or-request-changes review. Conditions 1 to 4 and 6 apply unchanged to the agent's tagged review. The two approvals may arrive in either order.
- A human "request changes" stays outside the protocol: it is not parsed as findings, it does not block `reviewer launch`, and it makes the approval invalid through (5b) until the approver's next approval. `status` reports it with the login and commit. The Implementer reports it to the Developer as it reports `needs_human`, acts on the Developer's instruction, pushes, and the loop continues with a fresh review of the new head.
- After the agent Reviewer approves and the human approval is missing, the derived next action is `await_human_approval`. The Implementer reports "approved by the agent at `<sha>`, waiting for approval from `<approver logins>`" and goes idle; it never polls the forge. "Check PR #N" resumes it through `status`, the same path as the lost notification of §8.7.
- `pr merge` runs only on the Developer's instruction, given at the time or in advance (decision 31), and requires `approved`, which now includes (5a) and (5b). A forge refusal is reported as in v0.5.0.
- Role separation is by convention only. The forge cannot distinguish the two agents, so any line the Reviewer may post the Implementer could post with the same token, and the authorship sets of §7.1 collapse to the shared login. This is stated as a non-guarantee, alongside v0.4.4 §40's "malicious same-user agents". `DECISION` authorship stays as in §7.1: `developer_accounts` plus the Implementer identity, which in this mode is the shared account. The independent check in this mode is the human colleague's approval; that is why the mode exists.

## 4. Forge adapters

### 4.1 Boundary

`forge.py` gains a `Forge` protocol (a typed interface) that both adapters implement, the forge-neutral data types (`Evidence`, `Review`, `Comment`, `PullRequest`, `Snapshot`, and a neutral `Anchor`), and a factory `make_forge(repository, role)` keyed on `forge.kind`. The three construction sites (`cli.py`, `doctor.py`, `initialization.py`) call the factory; every annotation names the protocol. `conventions.py`, `commands.py`, `merging.py`, `reviewer.py`, and `doctor.py` contain no forge name.

Differences stay inside the adapters:

- review events and states are translated to one shared vocabulary (`approved`, `changes_requested`, `commented`, `pending`, plus a separate `dismissed` flag);
- each adapter builds its own inline-comment payload from the neutral anchor;
- each adapter reports capability flags (`can_resolve_threads`, `can_read_thread_resolution`, `can_read_branch_rules`), and the commands branch on the flags, never on the kind;
- each adapter exposes `approvals()` returning the human approve-or-request-changes reviews in the neutral vocabulary, so §3.2's conditions are written once.

### 4.2 GitHub

Behaviour unchanged. The adapter keeps calling `gh` as a subprocess with `gh auth token --user` for tokens; the fake `gh` on `PATH` keeps serving the tests. The refactoring is verified by the existing suite and by the first PR that runs through the loop after the installed runtime is upgraded to it (§8).

### 4.3 Forgejo

The adapter speaks the Gitea-compatible REST API (`<base_url>/api/v1`) directly through the standard library's `urllib.request`; the package keeps zero runtime dependencies. `fj` 0.6.0 cannot create reviews, inline comments, or replies, stores one credential per host, and has no authenticated passthrough, so it cannot play the part `gh api` plays.

Forgejo hypotheses from the read-only survey of 2026-09-21 and the proposed adapter handling. Increment 0a (#54) tests the items marked *experiment* through real writes; the specification delta (#55) cites recorded evidence or versioned Forgejo source references for each behaviour.

| Hypothesis | Proposed handling |
| --- | --- |
| The approve event is spelled `APPROVED`; an unknown event silently creates a hidden `PENDING` review with HTTP 200 | Send `APPROVED`, `REQUEST_CHANGES`, or `COMMENT`; read the review back and fail unless the returned state is the one requested |
| An inline comment whose line is outside the diff, or whose path does not exist, is accepted and stored with an empty `diff_hunk` (*experiment*: how it renders) | Local anchor validation of §11.2 before any write, unchanged; after posting, read the review's comments back and treat a root with an empty `diff_hunk` as unanchored, which routes it through the §7.4 `## Unanchored findings` fallback and `thread open`; the v0.5.0 batch-rejection path stays for GitHub |
| `commit_id` is stored as sent, never checked against the head | Send the target head; derivation already compares `commit_id` with `pr.head` |
| A review is built inside the account's hidden pending draft and submitted at the end; an interrupted post leaves a draft that a retry duplicates into, and a human draft on the same account is swept into the agent's review | Before every `review post`, list the account's reviews for the PR and refuse with the draft's ID when a `PENDING` review exists (a gate, never an automatic deletion); `review post --discard-draft <review-id>` deletes exactly that pending review first. *Experiment*: post the review with its body and no inline comments, then add each root to the submitted review through `POST …/reviews/{id}/comments`; if that works, no draft accumulates and an interruption leaves the `--resume` case of §11.1 |
| Two comments on the same review, path, and line form one conversation; there is no thread ID and no reply-to field | Refuse duplicate anchors within a review; identify a thread by the `[REV-n]` tag of its root, never by line; a reply is `POST …/reviews/{id}/comments` with the root's `path` and `position` (*experiment*: it lands in the thread) |
| The `position` and `commit_id` returned on a comment describe the blamed commit, not the reviewed head | Never derive a finding's line from `position`; the head line, when needed, is the last line of `diff_hunk` |
| Resolving a conversation is a web-UI action only; a read-only `resolver` field marks a resolved root | `thread resolve` exits with "not supported on this forge" (`can_resolve_threads` false); `status` shows the resolution state from `resolver`; the settled-thread rule of §7.5 is unaffected because resolution is not authority |
| `GET …/reviews/{id}/comments` is unpaginated and returns a different order on every request | Sort by `id` |
| `base.sha` is the live tip of the base branch, not frozen as on GitHub | The moved-base logic already computes the merge base locally; the parser accepts `merge_base` |
| Multi-line comments exist from 16.0 through `extra_lines_count` (*experiment*: mapping of a `start_line`) | Range anchors map to `extra_lines_count = line - start_line`; if the experiment fails, anchor at the last line with the range stated in the finding text |
| Merge: `POST …/pulls/{index}/merge` with `Do` (`merge`, `squash`, …), `head_commit_id` (409 "head out of date" on mismatch), `delete_branch_after_merge`; branch delete `DELETE …/branches/{name}` | Same semantics as the GitHub merge: exact-head guard, confirmation by ancestry or tree identity, moved-base rule of §7.10; 409 joins the refused-status set |
| Bodies are stored verbatim, never `null` | The CRLF normalisation of v0.5.0 stays as a harmless no-op |
| Reviews list includes `REQUEST_REVIEW` rows with an empty `commit_id` and the caller's own `PENDING` rows; `stale`, `official`, and `dismissed` flags; an approver's newer review can auto-dismiss their earlier one | The parser skips `REQUEST_REVIEW` rows, keeps `dismissed`, ignores `stale` and `official` (§3.2 uses the exact head instead) |
| Timestamps carry the server's offset | Parsed as ISO 8601 with offset and ordered by instant; ties broken by ascending ID as in §7.1 |
| Auth is `Authorization: token <t>`; scopes `write:repository`, `write:issue`, `read:user`; `GET /user` identifies the token; `GET /version` is public; pagination `page`/`limit` with a maximum of 50 | Identity verification before the first mutation as in §4.5; `limit=50` paging with a short-page stop; `doctor` enforces version ≥ 16.0.0 |
| Repository permissions are in the repository object (`permissions.push`); branch protections are readable only by administrators | `doctor` reads `permissions`; `pr merge` reports protection requirements as "not visible" when the read is refused |

The minimum supported Forgejo version is 16.0.0; the client's 16.0.3 is the reference. No behaviour varies with the server version.

## 5. Configuration and `init`

Schema version 2 is kept; every existing configuration stays valid and no `init` rerun is required. New fields:

| Field | Type | Default | Validation |
| --- | --- | --- | --- |
| `identity_mode` | string | `"dual"` | `"dual"` or `"single"`; `dual` requires the two accounts to differ (the v0.5.0 rule); `single` requires them to be equal |
| `approver_accounts` | array of strings | `[]` | forge logins whose formal approval counts in `single` mode; required non-empty when `single`, MUST be empty when `dual`; each different from both role accounts |
| `forge.kind` | string | `"github"` | `"github"` or `"forgejo"` |
| `forge.base_url` | string | absent | required when `kind` is `forgejo`, absent otherwise; an absolute URL, `https://` except that `http://` is accepted for loopback hosts (`127.0.0.1`, `::1`, `localhost`) so the fake server can serve the tests; no query or fragment; a path prefix is kept for instances installed under a sub-path |
| `implementer.token_file`, `reviewer.token_file` | string | absent | required when `kind` is `forgejo`, absent otherwise; an absolute path to a regular file outside every worktree of the repository, mode with no group or other bits, containing one non-empty line; checked by `doctor` and on every read; in `single` mode both may name the same file |

`init` is told the forge rather than detecting it, because SSH host aliases hide the host: `--forge github|forgejo` (default `github`), and for Forgejo `--base-url <URL>`, `--implementer-token-file <path>`, `--reviewer-token-file <path>`, plus `--identity-mode single --approver-account <login>` (repeatable) for the single mode. `init` verifies the repository as the Implementer identity as today and probes no unknown host. `doctor` verifies the base URL and version, each token's login and file permissions, that every `approver_accounts` login exists, and, in `single` mode, that the shared account has push permission; the "two logins differ" and "Reviewer write permission" checks apply to `dual` only.

## 6. Command changes

- `thread resolve`: unsupported on Forgejo (exit status from the existing vocabulary, message "not supported on this forge"); the Reviewer skill skips it when `status --json` reports the capability as absent.
- `status`: adds the human approvals (login, state, commit, dismissed), the `await_human_approval` and human-request-changes states, a pending draft on the acting account, and the forge resolution state where readable.
- `review post`: the pending-draft gate and `--discard-draft <review-id>` (Forgejo only); the read-back check after posting.
- `pr merge`: in `single` mode requires the human approval through `approved`; after the forge deletes the branch, removes the local remote-tracking ref `refs/remotes/origin/<branch>` with an exact-SHA guard (`git update-ref -d <ref> <expected>`) only after confirming the branch is absent on the forge; reports protection requirements as "not visible" when unreadable.
- `reviewer launch`: only if Increment 0's investigation finds a slow-start cause, a bounded wait after `agent_not_ready` that re-reads the agent state once more, delivers the request line to an `idle` agent, and leaves a `blocked` agent exactly as §8.4 describes. The tool still never sends keys and never answers a trust prompt; `reviewer adopt` keeps refusing a blocked agent.
- `init` and `doctor`: §5.
- `thread reply`, `status`, `reviewer launch`, `pr merge` (#64): every unsettled thread, blocking or optional, needs a `DISPOSITION` before the next launch and before the merge; `unaddressed_findings` covers optional threads and is evaluated before `approved` in the next-action order; `pr merge` refuses on it (exit 4) naming the threads; on an optional thread `rejected` is followed by `Not pursued:` with the reason or `Deferred to #<open issue>:`, and `thread reply` checks the issue exists and is open; the "ready to merge" report lists every optional finding with its disposition. No first-line keyword changes.
- `issue view`, `pr merge`, `doctor` (#66): `issue view --json` reports `paths.issue_scratch` (`<scratch_root>/issue-<N>`), the Implementer's directory for its Task draft, report, reply and decision bodies, probes, and validation output; `pr merge` removes it with the per-PR directory, with the same refusals; `doctor` reports one whose issue is closed. The Implementer skill names it in sections 1, 2, 5, 7, and 8 and gains three rules: no tool installations or virtual environments under the repository; the PR description is frozen at merge (`pr report` refuses a PR that is not open) and the merge report goes to the Developer; filter `--json` output rather than saving whole responses.
- `pr merge` (#69): after a verified merge and the cleanup of §7.10 step 4, fast-forwards the primary checkout to the verified base tip when that checkout has the base branch checked out and no staged or unstaged changes to tracked files. Otherwise, or when Git refuses, it reports the reason and, when the checkout is on the base branch, prints the command. The result never changes the exit status; the Implementer skill reports it and never runs the command itself.
- `status` and skills (decision 31): a standing merge instruction, recorded as a general `DECISION` whose body opens with a fixed line, turns the next action `approved` into `merge`, and `status` names the decision. The Implementer skill records the instruction only when the Developer gives it, runs `pr merge` on `merge`, reports afterwards everything the ready-to-merge report lists, and still stops for a moved base, a forge refusal, or incomplete cleanup.
- Skills: wording that names GitHub as the authority becomes "the forge"; the Reviewer skill learns the capability check before `thread resolve`; the Implementer skill learns the `await_human_approval` report and the human-request-changes report. Under #64 the Implementer skill's sections 5, 6, and 8 and the Reviewer skill's sections 4 and 6 carry the disposition rule, and the Reviewer's verbatim policy gains the bright line that a finding showing a Task acceptance criterion unmet or unenforced is blocking, never optional.

## 7. Testing and CI

### 7.1 Fake Forgejo

A loopback HTTP server (`http.server.ThreadingHTTPServer` on `127.0.0.1:0`) under `tests/fixtures/`, started by the test fixture and named to the CLI through `forge.base_url`, so the integration tests exercise the real request construction end to end as the fake `gh` does. It models the facts of §4.3: the `APPROVED` spelling and hidden `PENDING` on an unknown event, acceptance of out-of-diff anchors with an empty `diff_hunk`, the pending-draft absorption, conversation grouping by (review, path, line), random comment order, `REQUEST_REVIEW` rows, the live `base.sha`, `head_commit_id` 409, the 422 for an author's approval, and the verbatim bodies. Its responses are seeded from recordings taken on the 16.0.3 container in Increment 0a. It records every request with its `Authorization` header so the tests prove per-role tokens and that no token is printed.

### 7.2 CI layout

Integration tests stay on every change. `.github/workflows/test.yml` splits the suite into five parallel jobs on pull requests, each naming its test modules: `test_doctor`; `test_reviewer`; `test_forge_commands`; the smoke module; and everything else (the unit tests, `test_merge`, `test_init_doctor`). The source-export smoke and the package-build test move into a module that only pushes to `main` and a nightly schedule run; the macOS job runs only there too, on the full suite. The job-to-module assignment lives in one committed grouping file that a small group runner and the workflow both use (the standard library has no YAML parser), and a unit test fails when a test module under `tests/` belongs to no group or to more than one, is missing from the main-only list, or when a group named in the file is absent from the workflow. `make test` remains the full suite. A failed nightly run relies on GitHub's own failure notification.

Measured on 2026-09-21 (local, Python 3.12): 74 unit tests in 0.2 s; 111 integration tests in 467 s, of which `test_doctor` ≈ 135 s, `test_smoke_workflow` 101 s (two tests of ≈ 50 s each), `test_reviewer` ≈ 90 s, `test_forge_commands` ≈ 83 s, `test_merge` ≈ 53 s, `test_init_doctor` ≈ 17 s. Expected pull-request wait after the split: under four minutes, against 8 to 13 today.

## 8. Experiments, trials, and evidence

- **Write experiments (Increment 0a)** on a disposable local Forgejo `16.0.3` container (`podman`, image `codeberg.org/forgejo/forgejo:16.0.3`): the author's `COMMENT` review with inline comments through the API; the body-first posting order; a reply landing in the right conversation; the rendering of an empty-`diff_hunk` comment; a bogus `commit_id`; the `extra_lines_count` mapping; the pending-draft absorption; the merge guard. Recorded responses seed the fake server. The container never holds a real token of the Developer.
- **Trials for the release**, each in one agent direction (v0.5.0 proved both directions and the direction does not depend on the forge):
  1. GitHub regression: the first Agent Squad PR that runs through the loop after the installed runtime is upgraded to the refactored adapter (Increment 1); every later PR repeats it.
  2. GitHub single-identity, on `MagiLand/agent-squad-trial`: both agents as `patrickhe`, `patrick-magiland` as the human approver operated by hand, including one human "request changes".
  3. Forgejo single-identity on the local 16.0.3 container: one human "request changes" and one interrupted review post recovered with `--resume`.
  4. Forgejo single-identity over the real network on the Developer's VPS instance pinned to 16.0.3, with HTTPS, SSH pushes, and branch protection requiring one approval: the rehearsal for the client pilot.
- Two-account mode on Forgejo is implemented (the mode is forge-independent) and covered by the fake server, but not trialled; it is documented as unverified and gets a follow-up issue.
- The Codeberg trial of the original plan is dropped in favour of trial 4.
- Evidence records follow §16.5. The client pilot is a supervised exercise after the release, not a release condition.

## 9. Increments and schedule

Release target Friday 2026-10-02; hard date Monday 2026-10-05. Six chores run first and independently, C3 before the next loop PR merges; the rest form a linear chain as in v0.5.0. Each is one issue in milestone `v0.6.0`.

| # | Increment | Dates |
| --- | --- | --- |
| C1 | Chore: CI split (§7.2) | 09-22 to 09-23 |
| C2 | Investigation: `agent_not_ready` launches, bounded to one working day; a reproduced cause with evidence or a documented negative result | 09-22 to 09-23 |
| C3 | Chore: a disposition on every thread before launch and merge; Task-criterion findings blocking; spec amended in place, skills, gates, tests (#64) | 09-22 to 09-23 |
| C4 | Chore: the CI guard locks the Python versions and the group runner sets `PYTHONPATH` (#65, from PR #62 REV-2 and REV-4) | 09-23 |
| C5 | Chore: a per-issue scratch directory for the Implementer, removed by `pr merge` and reported by `doctor`; three skill rules on tool installations, the frozen PR description, and `--json` filtering (#66) | 09-23 |
| C6 | Chore: `pr merge` fast-forwards a clean primary checkout on the base branch after a verified merge (#69) | 09-23 |
| 0a | Forgejo write experiments on the 16.0.3 container, evidence record, recordings for the fake | 09-22 to 09-24 |
| 0b | The v0.6.0 specification delta over v0.5.0 (§4.5, §7.3, §7.5, §7.6, §7.9, §7.10, §8.3 if C2 warrants, §9, §10, §11, §12, §16, §17, §18, §19), written after 0a so it contains no unverified claim | 09-24 to 09-26 |
| 1 | The `Forge` protocol, neutral types, factory; GitHub behind it unchanged; runtime upgrade and trial 1 | 09-26 to 09-27 |
| 2 | Single-identity mode on GitHub: configuration, approval validity, `status`, `pr merge`, skills; the standing merge instruction (decision 31); trial 2 | 09-28 to 09-29 |
| 3 | Forgejo reads: transport, token files, parsers, `status` on Forgejo; the fake server | 09-30 |
| 4 | Forgejo writes: `review post` with the draft gate and read-back, replies, `thread open`, `decision post`, `stop post`, `pr create`, `pr merge` with the tracking-ref cleanup; the launch fix if C2 warrants it | 10-01 to 10-02 |
| 5 | `init` and `doctor` for Forgejo and the single mode | 10-03 |
| 6 | Release: smoke against both fakes, documentation, trials 3 and 4, version 0.6.0, tag and release | 10-04 to 10-05 |

If the work is behind on 10-01: two-account mode on Forgejo loses its fake-server coverage too, trial 4 moves after the release, and the launch fix becomes a follow-up issue.

Development runs through the v0.5.0 loop itself: `squad-implementer` and a fresh `squad-reviewer` per pass, with the installed runtime upgraded only between PRs from merged `main`, so no PR is reviewed by its own unmerged code. The Developer plans, triages, and gives a second opinion on `needs_human`.

## 10. Decisions record (2026-09-21 and 2026-09-22)

1. Theme: Forgejo plus single-identity mode; also the CI split, the launch investigation, and the tracking-ref cleanup. Excluded: CI enforcement, `rebase`, garbage collection.
2. The client's timeline: release within two weeks; pilot after the release.
3. Client facts: Forgejo 16.0.3, one account, colleague approval enforced by the forge, Implementer merges on instruction.
4. v0.5.0 housekeeping done the same day: milestone v0.5.0 closed, milestone v0.6.0 created, tag `v0.5.0` on `795be79`, GitHub release published; the trial repository stays unarchived.
5. v0.6.0 is developed through the v0.5.0 loop; the runtime is upgraded only between PRs.
6. The specification is a delta over v0.5.0 only; consolidation is a documentation issue after the release.
7. The identity mode is forge-independent; explicit `identity_mode` defaulting to `dual`; schema 2 kept with optional fields.
8. Role separation in single mode is by convention, stated as a non-guarantee.
9. A required `approver_accounts` list; human approval at the exact head, not dismissed, either order.
10. A human request-changes stays outside the protocol.
11. `thread resolve` is unsupported on Forgejo; no marker emulation.
12. A `Forge` protocol and factory; GitHub keeps `gh`; Forgejo uses `urllib`; zero dependencies.
13. Forgejo tokens come from a `token_file` per role with owner-only permissions.
14. The Forgejo fake is a loopback HTTP server; `http://` only for loopback.
15. A pending draft on the account refuses the post; an explicit `--discard-draft <id>` recovers an agent's own draft.
16. Write experiments on a local `podman` container pinned to 16.0.3.
17. CI: integration tests stay per change, split into five jobs with a guard test; macOS, the source-export smoke, and the package build only on `main` and nightly.
18. Two-account mode on Forgejo: in code and in the fake, no trial; first to drop.
19. Protocol tag unchanged.
20. Minimum Forgejo 16.0.0; no version-dependent behaviour; range anchors through `extra_lines_count`.
21. Launch failure: bounded investigation first; the bounded-wait fix only for a slow-start cause.
22. The Implementer waits for human approval idle, without polling.
23. `init` is told the forge; `doctor` verifies.
24. Trials 1 to 4 as in §8; Codeberg dropped; trial 4 on the Developer's VPS.
25. Order of work as in §9.
26. (2026-09-22) Every unsettled thread, blocking or optional, carries a `DISPOSITION` before `reviewer launch` and before `pr merge`; the gate is `unaddressed_findings` extended to optional threads and evaluated before `approved` in the next-action order. Optional findings still never withhold approval.
27. (2026-09-22) On an optional thread `rejected` names its reason, `Not pursued:` or `Deferred to #<open issue>:`, and the tool checks the issue; no new keywords, protocol tag unchanged. The "approved, ready to merge" report lists every optional finding with its disposition so the Developer decides with the findings in view; a fix after approval moves the head and needs another pass.
28. (2026-09-22) A finding that a Task acceptance criterion is unmet or not enforced is blocking, never optional; the rest of the severity policy is unchanged.
29. (2026-09-22) Filed as independent chores #64 and #65, to run before the next loop PR merges; the v0.5.0 specification is amended in place with an Amendments list, and the v0.6.0 delta (#55) takes the amended text as its baseline. PR #62's four optional threads were dispositioned the same day as deferred to #64 (REV-1, REV-3) and #65 (REV-2, REV-4).
30. (2026-09-22) The Implementer gets `<scratch_root>/issue-<N>` with the same lifetime as the Reviewer's `pr<N>`: reported by `issue view`, removed by `pr merge`, reported by `doctor` when its issue is closed. File-based CLI inputs and redirected validation logs stay, because editing a file and reading a log's summary line cost fewer output tokens than re-emitting or reading everything; tool installations and virtual environments never go under `.agent-squad/`; the PR description is frozen at merge and the merge report goes to the Developer only; `--json` output is filtered, not saved. Filed as chore #66 after PR #62 left 1460 files (20 MB) under `review-scratch/issue-52-*`, 19 MB of it a virtual environment.
31. (2026-09-22) The merge stays gated on the Developer's instruction, and no third skill takes over the steps after approval: the Implementer already holds the PR, the worktree ownership, and the primary checkout. The Developer may give the instruction in advance, for example "start on #N and merge when approved". The Implementer then records it as a general `DECISION` whose body opens with a fixed line that the delta defines, so the instruction lives on the PR, is attributable, and survives an interrupted session; the Developer may also post it from a `developer_accounts` login. While it is in force, `status` reports the next action `merge` instead of `approved`; the Implementer runs `pr merge` without waiting and then reports the merge, the cleanup, and every optional finding with its disposition. It never extends to a moved base, a forge refusal, or incomplete cleanup; the Implementer reports those and waits, as today. A newer `STOPPED` or Task amendment cancels it, and a later general decision that says so and links it withdraws it (§7.6); it neither lifts a stop nor settles `needs_decision`. In `single` mode the Implementer still learns of the human approval only through "Check PR #N" (decision 22). Waiting stays the default. There is no configuration switch, because a merge instruction belongs on the PR it authorises, and the forge's own auto-merge is not used, because it checks branch protection rather than §7.10 and does no cleanup. No tagged-line grammar change. The delta (#55) specifies it; Increment 2 (#57) implements it beside `await_human_approval`.
32. (2026-09-22) After a verified merge, `pr merge` fast-forwards the primary checkout to the verified base tip when that checkout has the base branch checked out and no changes to tracked files; otherwise it reports why and, when the checkout is on the base branch, prints the command, and the Implementer never runs the command itself. A fast-forward cannot lose work, and Git refuses when a local change or an untracked file would be overwritten, so the reason behind "never modifies the base-branch checkout" (§7.10 step 5) does not apply to it. Together with #66, the Developer's "merge" becomes the only step after approval. Filed as chore #69.

## 11. Facts still to verify by experiment (Increment 0a)

1. An author's `COMMENT` review with inline comments is accepted through the API (verified in source, not yet by a write).
2. Adding a root to an already submitted review through `POST …/reviews/{id}/comments` works, and a reply with the root's `path` and `position` joins the root's conversation.
3. How the web UI renders a comment stored with an empty `diff_hunk`.
4. The server's response to a `commit_id` that is not a commit of the PR.
5. `extra_lines_count` for a `start_line`/`line` range.
6. Whether a body-only review submission sweeps an existing pending draft of the same account, and whether `DELETE …/reviews/{id}` on the draft is enough to recover.
7. The exact refusal and status codes of the merge guard and of a merge refused by branch protection.
