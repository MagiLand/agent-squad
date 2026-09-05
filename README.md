# Agent Squad

Agent Squad is a lightweight local tool for coordinating an implementation agent and an independent review agent through Herdr. It is intended to automate the routine implementation-review handoff while preserving developer control over requirements, architecture, risk, and final integration.

## Project status

Agent Squad is being implemented against the approved version 0.4.4 baseline. The current command-line application can initialize a repository, start one authoritative local run, submit exact committed revisions to fresh round-scoped Reviewers through Herdr, recover or supersede stuck reviews, escalate decisions to the Developer, resume from durable Developer resolutions, apply approved, changes-requested, or needs-human results, carry a finding-complete implementation response into a correction round, and complete the exact approved revision.

The canonical specification is [Agent Squad v0.4.4](docs/agent-squad-v0.4.4-spec.md).

The reference implementation targets Python 3.11 or later on macOS and Linux and has no third-party runtime dependencies.

## Install

Install the command from this checkout in a Python 3.11 or later environment:

```bash
python -m pip install .
agent-squad --help
```

## Initialize a repository

Run initialization anywhere inside a non-bare Git worktree:

```bash
cd /path/to/project
agent-squad init
```

Initialization creates or validates `.agent-squad/config.json` at the worktree root and adds these entries to the Git common directory's `info/exclude` file:

```gitignore
.agent-squad/
.agent-squad-review/
```

It does not modify the project's tracked `.gitignore`, overwrite valid existing configuration, or create an active run. Repeating the command is safe. Invalid configuration must be corrected before initialization can continue.

## Start and inspect a run

Start a run with an approved UTF-8 Markdown task. The Implementer agent name, Reviewer kind, and base reference default to `.agent-squad/config.json`; pass them explicitly to override those selections for this run. `--implementer` changes the agent name within the configured `implementer.kind`; change the configuration to select another Implementer kind. Configured Reviewer `start_args` are used only when the configured Reviewer kind is selected. Repeat `--context` to capture only the additional files the run needs.

```bash
agent-squad start \
  --task path/to/task.md \
  --context path/to/context.md \
  --implementer codex-main \
  --reviewer claude \
  --base origin/main
```

Starting captures immutable copies under `.agent-squad/runs/<run-id>/`, resolves the base to a full Git object ID, records the worktree and role identities, initializes the review budget, and creates the authoritative `.agent-squad/state.json`. Only one run may be active in a worktree.

Inspect idle or active state from anywhere in the initialized worktree:

```bash
agent-squad status
```

Status validates the captured task and context digests before reporting the active phase, local Git identities, selected roles, fixed base, review budget, and next action. If a disposable review worktree is absent, status still reports the authoritative run and marks that worktree unavailable. If the worktree is present but the Reviewer altered or removed a bundle input, status still reports the run and prints `Review bundle intact: no` with the reason; commands that consume review content, such as `apply-review`, keep validating the bundle strictly.

## Submit the first candidate

Commit a coherent candidate and prepare a UTF-8 Markdown implementation report, then submit the exact current revision:

```bash
agent-squad submit \
  --report path/to/implementation-report.md \
  --mode new_revision
```

The first submission requires a clean tracked worktree, no unexpected untracked files, a current branch or detached state matching the run, a head different from the fixed base, and the fixed base as an ancestor of that head. Known generated paths may be configured through `allowed_generated_paths`.

Before contacting Herdr, Agent Squad creates a durable round record, detached review worktree, self-contained `.agent-squad-review/` bundle, and pending handoff state. It discovers the installed Herdr schema and command capabilities, opens the exact worktree, and launches or adopts the deterministic Reviewer. If discovery, launch, or prompting fails, the same logical round and request remain recorded for recovery; another `submit` does not create a replacement round. Run the reported `agent-squad retry-handoff` command instead.

## Recover a review handoff

Inspect the durable round and retry its current handoff from the implementation worktree:

```bash
agent-squad status
agent-squad retry-handoff
```

Recovery first checks the expected review output and Reviewer-local marker. Marker-confirmed output is returned with the exact `apply-review` command without contacting Herdr. Output without a valid marker is reported as incomplete and is never treated as an applicable result.

If a failed `apply-review` left a provisional archive for a result that recovery later retired, applying corrected marker-confirmed output preserves the retired attempt under the round's `diagnostics/retired-apply-attempts/` directory before archiving and applying the corrected result.

When no valid result is ready, recovery checks the deterministic Reviewer session and, when Herdr supports it, reads recent terminal history before sending anything. It can adopt a request already visible in history, re-prompt the existing Reviewer, or relaunch the deterministic Reviewer in the same review worktree. If optional history diagnostics are unavailable or fail, recovery safely falls back to re-prompting. Every path reuses the run ID, round, request ID, Reviewer name, worktree, and bundle; repeated recovery never allocates a replacement round.

If recovery must remove a malformed or mismatched marker, it first binds that result ID to its original digest in implementation-owned round storage. A copy in the review bundle gives `review-submit` early feedback, but implementation-side status, recovery, and application use only the authoritative binding; a missing, malformed, mismatched, or non-regular advisory copy cannot veto them. Recovery excludes that entry from invalid-result evidence traversal and safely replaces an invalid advisory directory from authority before removing the marker. Corrected content must use a new result ID.

## Supersede a stuck review

When the active review is no longer relevant, invalidate that exact round from the implementation worktree:

```bash
agent-squad supersede --reason "The request is obsolete."
```

Supersession records the Implementer actor, cause, and timestamp in authoritative history, marks only the active reviewing round `superseded`, returns the run to `implementing`, and does not consume review budget. The Reviewer notice is best effort; a missing or unreachable Reviewer does not undo the committed transition.

Cleanup holds the Reviewer submission lock while it probes for marker-confirmed output. Any late result is copied to the round's `diagnostics/late-results/<result-id>/` directory without being applied, and the complete review bundle is archived and verified before removal. Agent Squad deletes only the review bundle and configured generated paths, then uses normal non-forced Git worktree removal. If a bundle input was altered or removed, cleanup retains the worktree without archiving the damaged bundle and reports why. If unrelated tracked, untracked, or ignored files remain after the bundle has been archived and removed, it preserves the worktree and reports why. A missing review-worktree directory is unregistered through an exact-path Git operation.

A later `new_revision` submission creates a distinct round and Reviewer. It may reuse the immediately preceding superseded round's head because that round produced no applied result, even when an older applied `changes_requested` result reviewed the same head. The older applied result's complete response is still required. A different submitted head remains subject to the normal rule that it must differ from the most recent applied reviewed head. Reconsideration remains available when recovery returns to the most recent applied reviewed head and its authoritative response can be reused unchanged. A persisted round keeps that response frozen even if the round later becomes superseded, stale, or invalid. Consequently, reconsideration that changes an already-captured `fixed` disposition to `rejected` is unavailable; recover at the correction head with the unchanged response instead. This prioritizes Section 26.6’s no-rewrite rule over the broader reconsideration path in Section 26.13. Any response submitted at that unchanged applied head uses reconsideration semantics, so a `fixed` disposition still requires a different committed revision.

## Escalate and resume a Developer decision

Request Developer authority directly while a run is `implementing`, `reviewing`, or `approved`:

```bash
agent-squad escalate \
  --note path/to/decision-needed.md
```

When answering an applied `changes_requested` review, `--response path/to/response.json` may also stage a complete response whose unresolved dispositions are `needs_human`. The response, escalation record and note, event, run state, and any affected round metadata become authoritative at one commit point. A failed pre-commit attempt leaves the response and escalation correctable. Retrying the exact committed escalation and response is idempotent; a different response cannot overwrite it.

Escalating during review supersedes that round with the escalation ID in its cause and sends the Reviewer a best-effort pause notice. Escalating after approval clears current approval authority while preserving the approved head in the escalation record. Applying a valid Reviewer `needs_human` result performs the same transition automatically and links the escalation to the exact request and result.

Only the Developer can resume the run by recording non-empty UTF-8 Markdown:

```bash
agent-squad resume \
  --resolution path/to/resolution.md \
  --applies-to-finding REV-001 \
  --extend-rounds 1
```

`--applies-to-finding` may be repeated and must identify findings belonging to the active escalation. `--extend-rounds` increases the authoritative review budget and is required when the budget is exhausted. Only a newly applied valid `changes_requested` result consumes budget; reaching the effective limit creates a linked `review_budget_exhausted` escalation and enters `needs_human`. Resume stores a hashed resolution linked to the active escalation, clears that escalation, and returns the run to `implementing`. Every later review bundle includes every resolution in creation order, so a fresh Reviewer receives the complete decision history and treats the latest relevant resolution as authoritative.

After resolving an escalation-time response, you may reuse it unchanged if its dispositions already permit submission. Otherwise, submit a complete replacement with a new `response_id`, `supersedes_response_id` identifying the earlier response, and `resolution_ids` including the resolution linked to that escalation. Change each former `needs_human` disposition to `fixed` or evidence-backed `rejected`, and explain how it follows the Developer decision. Successful submission preserves the earlier response under `diagnostics/replaced-responses/<response-id>.json` in the reviewed round and includes the replacement and resolutions in the fresh review bundle. A failed pre-commit attempt remains correctable; once a new round references the response, only identical content may be reused.

A Developer resolution must settle the captured task rather than silently rewrite it. If the decision materially changes the objective or acceptance criteria, the protocol requires cancelling and restarting with a new task snapshot. Cancel the run with `agent-squad cancel --reason <text>` (see below). An escalation raised after approval clears current approval authority; after the Developer resolves it, the resumed run continues by submitting a changed committed candidate with `--mode new_revision`.

## Apply a review, correct findings, and complete

From the detached review worktree, the Reviewer writes the structured result and Markdown companion, then submits them:

```bash
agent-squad review-submit
```

The command validates the exact request, revision, tracked content, result semantics, and bundle hashes before atomically writing the Reviewer-local marker. A failed result notification does not invalidate that marker. From the implementation worktree, `status` discovers the marker and prints the exact application command:

```bash
agent-squad status
agent-squad apply-review --result-id <result-id>
```

Application independently repeats every identity, schema, hash, head, tracked-integrity, and Reviewer check. It archives the complete bundle before changing authoritative state, and repeating the same application does not duplicate the outcome, budget effect, or event.

For `changes_requested`, application increments the consumed review budget once, returns the run to `implementing`, and safely removes the archived round's detached worktree. Address every blocking finding in a versioned `response.json`. A `fixed` disposition requires a new committed revision, rationale, and a verification command. A `rejected` disposition requires concrete evidence.

Submit the correction with both artifacts:

```bash
agent-squad submit \
  --report path/to/corrected-implementation-report.md \
  --response path/to/response.json \
  --mode new_revision
```

Agent Squad validates the response against the prior round and result before the submit commit point. A failed validation creates no new round and leaves the response correctable. A successful submission archives the response under the prior round and gives a fresh Reviewer a self-contained bundle containing the prior review and response. Use `--mode reconsideration` only at the unchanged reviewed head and only when every disposition is an evidence-backed `rejected` response.

For an approved result, application records immutable approval authority and changes the run to `approved`.

Complete only while the implementation worktree is still at the exact approved head and satisfies the configured tracked and untracked cleanliness policy:

```bash
agent-squad complete
```

Completion preserves the run and round history, releases the active-run slot, and removes only validated disposable review resources. It never merges, pushes, deploys, or deletes a development branch.

After approval, a changed committed candidate can enter a fresh review with
`submit --mode new_revision --report <report.md>`. The new round clears current
approval authority; the earlier approval remains in run history. An unchanged
approved head cannot be submitted again.

Cancel any active run with:

```bash
agent-squad cancel --reason "Developer stopped this task"
```

Cancellation records the actor and reason, clears approval and active escalation
references, and releases the active-run slot. A reviewing round becomes
superseded before the terminal state is committed. Reviewer notification and
owned-resource cleanup are best effort; failures leave cancellation in effect.
Run evidence is preserved, repeated cancellation is safe, and a new run may start.

## Development

Run the deterministic unit and Git integration tests with either command:

```bash
make test
python -m unittest discover -s tests
```

Run the complete disposable artifact workflow with:

```bash
make smoke
python3 scripts/run-smoke-tests --json
```

The smoke runner seeds a small Python project in an owned temporary Git
repository and exposes this checkout's CLI. It exercises task capture, changes
requested, an escalation response, a scripted Developer resolution, response
replacement, a fresh review, lost-notification discovery, approval, and
completion. It uses the committed fake Herdr executable and makes no real model
calls. The JSON option includes the commands and exit codes. Both success and
failure clean the owned directory, including immutable archives; cleanup errors
report the retained path and fail the command. No Agent Squad run is created in
the source checkout.

For manually supervised live agents, use the separate
[live workflow guide](docs/workflow-verification.md). Live trials are outside the
automated test and smoke commands.

## Diagnose local prerequisites

Run `agent-squad doctor` from an initialized implementation worktree, or use
`make doctor` when developing Agent Squad. Each check reports `OK`, `WARNING`,
or `ERROR`; errors produce exit status 1, while warnings remain non-blocking.
The normal command checks configuration, canonical Git and stored run identities,
local exclusions, writable control storage, exclusive locks, atomic replacement,
review-worktree placement, and creation/removal of an owned detached worktree.
It discovers the installed Herdr schema and checks both configured roles and
history access without launching an agent. Run `agent-squad init` first when
configuration or the required local exclusions are missing.

Doctor reports residual worktrees, unregistered review bundles, mismatched
review heads, and deterministic Reviewers that outlive their rounds. It reads
run histories across registered implementation worktrees in the same repository
so a sibling worktree's active review is not classified as an orphan. Existing
resources and run history are never removed or adopted. Submodules receive a
warning: detached review worktrees may need project-specific preparation.

To test the configured Reviewer's actual permissions, explicitly run:

```bash
agent-squad doctor --live-reviewer --timeout 120
```

This starts a temporary Reviewer with the configured kind and native start
arguments. A disposable request and helper prove snapshot/request reading,
permitted output writing, `review-submit`, local marker visibility, independent
result validation, and result-handoff visibility in Herdr history. The result
handoff targets that temporary Reviewer itself. The preflight creates an
unreferenced synthetic commit with the current committed tree and parent; no
branch or active run is changed. The synthetic commit can appear as dangling
in `git fsck` until normal Git garbage collection expires it.
Its output wait defaults to 120 seconds; individual Herdr calls also have
their own bounded timeouts.

Successful cleanup closes only the newly created Reviewer workspace after
checking that its identity is unchanged and it still contains just the original
pane and tab. Git then removes the owned detached worktree. On failure, doctor
retains the temporary worktree and available session, reports the failed stage,
and writes `diagnostic.json` and available Herdr history under the printed
`.preflight-*` directory inside `<review-root>/<repository-id>/`.
Output permissions may prevent the Reviewer from writing a receipt; in that
case the retained session/history provides the
permission error. Doctor reports retained preflight evidence on later runs,
without deleting it, including a surviving Reviewer and a worktree registration
whose directory has been removed. Other repositories' probes are not reported.
For manual cleanup, inspect the evidence and the workspace's current contents
before stopping the retained Reviewer. Preserve any needed evidence, then use
`git worktree remove` for the reported worktree; inspect
`git worktree prune --dry-run` before pruning
registrations whose directories are already gone. Doctor never runs that cleanup
for retained probes. Tests use fake Herdr processes and never launch real models.

## License

Agent Squad is licensed under the [Apache License 2.0](LICENSE).
