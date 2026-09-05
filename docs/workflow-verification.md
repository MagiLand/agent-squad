# Workflow verification

This is the executable proof and live-trial procedure for issue #16 and
specification Sections 36.4–36.6. The production runtime remains standard-library
Python and permits one active run per implementation worktree.

## Deterministic proof

From an Agent Squad checkout with Python 3.11 or later and Git installed:

```bash
make test
make smoke
python3 scripts/run-smoke-tests --json
```

`make test` discovers the unit, Git integration, fake-Herdr, recovery,
validation, and artifact end-to-end tests. The smoke scenario is also an
integration test. Automated tests never need a real model.

The smoke entry point resolves files relative to itself, so it also works from
another current directory or a source export without `.git`. Its inputs are
`src/`, `tests/smoke_workflow.py`, the committed fake Herdr executable, and
`tests/fixtures/smoke/`. It neither reads ignored development archives nor
initializes the Agent Squad source repository. The child environment removes
inherited Git overrides, Python overrides, and fake-Herdr fault settings, then
sets an owned data directory and the exact fake executable ahead of `PATH`.
System and global Git configuration are disabled for the fixture commands.

The fixture starts with a passing named-greeting function and tests. Only after
`start` captures the task does it commit an incomplete empty-name fallback.
A deterministic Reviewer result identifies missing whitespace handling. The
Implementer applies the result, escalates a `needs_human` response, and resumes
with the committed **scripted** compatibility decision and a one-round budget
extension. A changed candidate replaces the response and reaches a new request
and Reviewer identity. The assertions verify exact response preservation and
resolution propagation before running tests in the fresh review worktree.

The final `review-submit` deliberately fails only the fake result notification.
The test verifies that the local marker remains valid, discovers it with
`status` and `retry-handoff`, applies the exact result, and completes twice to
check replay. It checks the approved Git object ID, released active-run slot,
clean Git status, absence of tracked runtime artifacts, and removal of both
review worktrees and their registrations. Finally it deletes the entire owned
temporary root. On cleanup failure it reports that root and exits unsuccessfully.

The scripted review and resolution prove artifact and command behavior. They
are not evidence of independent agent judgment or a human's project decision.

## Real Herdr directions

These commands only prepare disposable repositories; they do not launch agents
or spend model usage:

```bash
python3 scripts/prepare-live-smoke --implementer codex
python3 scripts/prepare-live-smoke --implementer claude
```

Each invocation prints an owned root and writes `trial.json` there. It seeds a
clean Git baseline, exposes the candidate CLI at `<root>/bin/agent-squad`, configures
the opposite Reviewer kind, and captures a task plus a protocol context containing
the review and response schemas. All review worktrees are under that root.
Keep the candidate checkout available until the trial and cleanup finish: the
wrapper imports that checkout's source. Preparation failures report the allocated
root for inspection. Repeating preparation allocates a distinct root.

Use the installed Herdr skill to start the named Implementer from `trial.json`
in an owned pane at `<root>/repository`. Resolve native trust/permission prompts
with the Developer. Reviewer `start_args` default to the harness defaults; to use
different supported permissions, cancel this unsubmitted run, edit the local
configuration, and start a new captured run. Do not edit stored run records.

1. Ask the real Implementer to read the task and protocol context, implement and
   test a committed candidate, write an honest report outside the Git worktree,
   and call the exposed CLI's `submit --report <report> --mode new_revision`.
   For the correction exercise only, explicitly authorize an incomplete first
   checkpoint in this disposable fixture and identify it as such in the report.
2. Let the fresh opposite-kind Reviewer inspect the exact revision and write its
   own review artifacts. The protocol context gives the absolute CLI command;
   the Reviewer calls `review-submit` from its detached worktree.
3. Use `status` and `apply-review --result-id <id>` from the implementation
   repository. Evaluate substantive findings. Use `escalate --response <json>`
   for a complete response needing Developer authority. The Developer records
   the decision with `resume --resolution <markdown> --applies-to-finding <id>
   --extend-rounds 1`. A scripted test decision must be identified as scripted.
4. Have the Implementer make and test a correction. Submit a complete replacement
   response with a new ID, `supersedes_response_id`, and linked `resolution_ids`.
   Confirm the new Reviewer receives and follows the resolution. The final
   `apply-review` and `complete` must refer to the exact approved revision.
5. Preserve concise evidence: source revision, harness versions, run/request/result
   IDs, reviewed Git object IDs, commands and exits, review disposition, relevant
   archive digests, and cleanup outcomes. Never commit runtime directories or
   generated review bundles into either repository.

In addition to both directions, Section 36.6 calls for same-SHA reconsideration,
request re-dispatch after Reviewer exit, supersession, lost notification,
`needs_human`, resolution propagation, and a small review-budget extension.
Record which actually ran; a deterministic fixture or an unsuccessful command
is not a successful live exercise.

To exercise lost notification without changing artifacts, pause before the
Reviewer submits and temporarily rename only the owned trial Implementer through
Herdr. The Reviewer runs normal `review-submit`; discovery cannot notify the
configured name, but its valid local marker remains. Restore the Implementer
name and use `status`, `retry-handoff`, and `apply-review` to discover and consume
the result. For Reviewer-exit recovery, stop only the owned Reviewer, then call
`retry-handoff`; verify the round and request IDs stayed the same. Use
`supersede --reason <text>` to exercise supersession. Do not manufacture a false
finding or a dishonest response merely to obtain same-SHA reconsideration.

After recording completion and preserving required evidence, inspect the owned
Herdr workspace identities and contents, then close only workspaces created for
the trial. Check `git worktree list --porcelain` in the temporary repository and
remove any retained, inspected review worktree with normal `git worktree remove`.
Remove the allocated root only when no live agent uses it. Report anything kept
for diagnosis. Do not close the development-session Reviewer or unrelated panes.

## Consuming-repository trial

Use a real project other than Agent Squad. A temporary clone at an exact source
commit is appropriate when the developer's checkout has unrelated work:

```bash
git clone --no-hardlinks /path/to/consumer /owned/trial/consumer
git -C /owned/trial/consumer switch -c chore/agent-squad-trial
```

Expose the candidate CLI, initialize local configuration, and start with an
accepted, small task applicable to the consumer. Keep task/report/response input
files outside its tracked worktree. Run the consumer's relevant validation,
independent review, application, and completion using the supported commands.
Record the source repository and exact base/approved commits, what changed, tests,
review outcome, and limitations. Verify `git ls-files .agent-squad
.agent-squad-review` is empty and preserve unrelated source-checkout files.
A documentation-only trial must not be described as app-build or release proof.

## Evidence status

The dated verification record is [2026-09-05](verification/2026-09-05-issue-16.md).
The live directions and consuming-repository acceptance criteria remain open
until that record contains completed trials. Automated success alone is not
v0.4.4 release acceptance and is not independent self-review of Agent Squad.
