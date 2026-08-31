# Agent Squad

Agent Squad is a lightweight local tool for coordinating an implementation agent and an independent review agent through Herdr. It is intended to automate the routine implementation-review handoff while preserving developer control over requirements, architecture, risk, and final integration.

## Project status

Agent Squad is being implemented against the approved version 0.4.4 baseline. The current command-line application can initialize a repository, start one authoritative local run, submit exact committed revisions to fresh round-scoped Reviewers through Herdr, recover lost request or result handoffs, apply approved or changes-requested results, carry a finding-complete implementation response into a correction round, and complete the exact approved revision.

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

Status validates the captured task and context digests before reporting the active phase, local Git identities, selected roles, fixed base, review budget, and next action. If a disposable review worktree is absent, status still reports the authoritative run and marks that worktree unavailable; present bundle paths and contents remain subject to strict validation.

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

When no valid result is ready, recovery checks the deterministic Reviewer session and, when Herdr supports it, reads recent terminal history before sending anything. It can adopt a request already visible in history, re-prompt the existing Reviewer, or relaunch the deterministic Reviewer in the same review worktree. If optional history diagnostics are unavailable or fail, recovery safely falls back to re-prompting. Every path reuses the run ID, round, request ID, Reviewer name, worktree, and bundle; repeated recovery never allocates a replacement round.

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

## Development

Run the deterministic unit and Git integration tests with either command:

```bash
make test
python -m unittest discover -s tests
```

## License

Agent Squad is licensed under the [Apache License 2.0](LICENSE).
