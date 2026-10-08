# Workflow verification

The [Agent Squad specification](agent-squad-spec.md#16-testing-strategy) defines the
release proof. Deterministic tests establish command behavior; live trials
establish delivery, independent review, human decisions, and integration in
actual repositories. Passing one does not establish the other.

## Deterministic proof

```bash
make test
make smoke
python3 scripts/run-smoke-tests --json
```

`make test` runs the standard-library unittest suite, including the smoke
scenario and a second invocation from a source export without `.git`.
It covers convention parsing, Git identity, gates, publication recovery,
Reviewer ownership and cleanup, skills, packaging, and doctor diagnostics.
The release checks compare the CLI command surface with the specification
and require package metadata `0.6.1`, independently of the declared protocol
version `0.5.0` and tag `AGENT_SQUAD/0.5.0`. Both wheel and sdist are inspected.
They also require `README.md`, this document, and the specification's header
and §1 to state the same package version.
Packaging needs the declared setuptools build backend; if absent, that test
is skipped and must be run in a prepared environment before claiming proof.

The smoke runner executes all twelve steps of §16.3 twice, on GitHub and on
Forgejo, each with two accounts. Its JSON contains `ok`, `scenarios`, and
`cleanup`. Each scenario identifies `forge`, `duration_seconds`, `scripted`,
`command_count`, `commands` (arguments and observed exits), `steps` (number
and successful result), and `cleanup`. CLI command counts are reported per
scenario; they are not HTTP request counts.

| Step | Evidence asserted by the runner |
| --- | --- |
| 1 | Initialization and full prerequisite diagnostics succeed. |
| 2 | A dedicated issue worktree is committed and pushed; PR sections and head match. |
| 3 | A fresh Reviewer publishes two blocking findings and one optional finding, then hands off. |
| 4 | A needs-human disposition blocks launch until a finding-specific decision; fixed/rejected dispositions are recorded. |
| 5 | The fix is pushed and reported; a fresh Reviewer verifies dispositions, resolves supported threads, and approves the exact head. |
| 6 | A later push invalidates approval and requires review. |
| 7 | The third review requests changes; a budget stop is posted and handed off; launch returns exit 4. |
| 8 | A decision extends the budget to four; the fourth review approves with no budget remaining. |
| 9 | Merge refuses a moved base, then accepts it explicitly and verifies ancestry; a second PR squash verifies tree identity on an unmoved base. Both remove their owned resources. |
| 10 | Notification fails after a published review; status still identifies that review and approval. |
| 11 | GitHub batch rejection or Forgejo empty-diff-hunk read-back preserves findings; thread open and explicit resume recover roots without duplicate reviews. Forgejo also refuses and explicitly discards a pending draft owned by the Reviewer's account. |
| 12 | No runtime files are tracked, no review worktrees remain, and all owned temporary roots are gone. Removal failure reports the retained root. |

The runner creates isolated working repositories and bare origins. It uses
committed `tests/fixtures/gh` and `fake_herdr.py` executables, and the loopback
`fake_forgejo.py` HTTP server. Forgejo initialization goes through the real
CLI. Unsupported thread resolution is asserted and tracking-ref absence is
checked with independent Git commands after every verified merge. Reviews,
dispositions, verifications, decisions, stops, and Herdr process states/messages
are scripted. Recovery scenarios use separate disposable PRs after the two
merge exercises. The fixture excludes inherited Git, Python, and authentication
overrides and supplies its own Git configuration and skill files.
It never contacts a real forge, calls a model, or commits an intentional defect into
the development checkout. Ordinary test failures still remove owned roots;
cleanup failure reports the path requiring attention.

## Real prerequisites and live trials

```bash
make doctor
PYTHONPATH=src python3 -m agent_squad doctor --live-reviewer --json
```

These commands use the real configured accounts, repository, and Herdr.
Doctor checks both forge identities, Reviewer write access, remote base branch,
Herdr schema/protocol and integrations, installed skills, writable roots,
disposable worktree creation/removal, and orphaned resources. The optional
live probe adds startup, readiness, trust behavior, and safe close. It sends
no review request and cannot prove a review loop by itself.

The v0.6.0 release trials, recorded in the
[#61 evidence](verification/2026-09-29-issue-61.md), ran the GitHub regression
and two Forgejo trials (a disposable local container and a network instance)
in the identity mode that v0.7.0 removed
([specification §20](agent-squad-spec.md#20-decision-history)). Their
transport, publication, recovery, and merge observations remain evidence within
their stated scope; their approval path is not current behavior. The
trial-specific permissions recorded there do not change the default setup
rules.

The API experiments in [#54](verification/2026-09-25-issue-54.md) and setup
checks in [#60](verification/2026-09-27-issue-60.md) use a real 16.0.3 container,
but do not establish a live review loop. For a Forgejo trial, record the container digest, actual server
version, merge method, whether branch deletion was explicit or already done,
and whether issue GET and PR-body PATCH were exercised. Keep unexercised
paths marked unverified. The [#98 record](verification/2026-10-07-issue-98.md)
establishes one supervised two-account loop on a local 16.0.3 container in one
agent direction; its limitations stay unverified. [#99](https://github.com/MagiLand/agent-squad/issues/99) tracks standalone
specification consolidation.

Any deliberately seeded input or failed notification must be identified as
scripted in the evidence. Human Task approval, decisions, continuation, and
merge instructions must be recorded as actual human actions. Every new
prerequisite misconfiguration found by a trial needs a doctor check and test.

## Evidence status

[Issue #61 evidence](verification/2026-09-29-issue-61.md) tracks v0.6.0
release readiness. In-progress or missing trials remain explicit blockers;
a package version or successful deterministic smoke is not release approval.
Both Forgejo trials completed and their disposable instances were removed.
The observed Herdr Reviewer startup defect is tracked in
[#100](https://github.com/MagiLand/agent-squad/issues/100). The Developer chose
separate tracking and kept release acceptance pending a verified fix.

[Issue #46 evidence](verification/2026-09-16-issue-46.md) records the prior
v0.5.0 release trials. It establishes both agent directions for that release,
not Forgejo behavior.

CI's pull-request `smoke` group runs both forge scenarios. The source-export
variant remains in `main-only`, executed on main pushes and weekly runs.
`make test` continues to run the full suite, including both variants. This
increment changes no CI actions, permissions, or triggers.

Earlier increment evidence remains useful within its stated scope:

- [Issue #42](verification/2026-09-12-issue-42.md): real forge publication with scripted review content.
- [Issue #43](verification/2026-09-13-issue-43.md): Reviewer lifecycle and harness delivery selection.
- [Issue #44](verification/2026-09-13-issue-44.md): packaged skills and merge mechanics.
- [Issue #45](verification/2026-09-16-issue-45.md): prerequisite diagnostics and real startup probes.

Each dated record follows the seven sections of §16.5 and lists private raw
evidence archive digests. Preserve unrelated checkouts and resources. Archiving
the trial repository, closing the milestone, publishing the release, and
scheduling the supervised client pilot are Developer actions after the release
PR merges.
