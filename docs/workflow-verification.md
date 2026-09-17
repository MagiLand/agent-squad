# Workflow verification

The [v0.5.0 delta](agent-squad-v0.5.0-spec.md#16-testing-strategy) defines the
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
and require package metadata `0.5.0` with protocol tag `AGENT_SQUAD/0.5.0`.
Packaging needs the declared setuptools build backend; if absent, that test
is skipped and must be run in a prepared environment before claiming proof.

The smoke runner executes all twelve steps of §16.3. Its JSON contains
`ok`, `duration_seconds`, `scripted`, `commands` (arguments and observed exits),
`steps` (number and successful result), and `cleanup`.

| Step | Evidence asserted by the runner |
| --- | --- |
| 1 | Initialization and full prerequisite diagnostics succeed. |
| 2 | A dedicated issue worktree is committed and pushed; PR sections and head match. |
| 3 | A fresh Reviewer publishes two blocking findings and one optional finding, then hands off. |
| 4 | A needs-human disposition blocks launch until a finding-specific decision; fixed/rejected dispositions are recorded. |
| 5 | The fix is pushed and reported; a fresh Reviewer verifies dispositions, resolves blocking threads, and approves the exact head. |
| 6 | A later push invalidates approval and requires review. |
| 7 | The third review requests changes; a budget stop is posted and handed off; launch returns exit 4. |
| 8 | A decision extends the budget to four; the fourth review approves with no budget remaining. |
| 9 | Merge refuses a moved base, then accepts it explicitly and verifies ancestry; a second PR squash verifies tree identity on an unmoved base. Both remove their owned resources. |
| 10 | Notification fails after a published review; status still identifies that review and approval. |
| 11 | Batch rejection preserves every finding's text; thread open and explicit resume recover roots without counting duplicate reviews. |
| 12 | No runtime files are tracked, no review worktrees remain, and all owned temporary roots are gone. Removal failure reports the retained root. |

The runner creates isolated working repositories and bare origins. It uses
committed `tests/fixtures/gh` and `fake_herdr.py` executables. Reviews,
dispositions, verifications, decisions, stops, and Herdr process states/messages
are scripted. Recovery scenarios use separate disposable PRs after the two
merge exercises. The fixture excludes inherited Git, Python, and authentication
overrides and supplies its own Git configuration and skill files.
It never contacts GitHub, calls a model, or commits an intentional defect into
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

Before release, §16.4 requires the following live evidence using `patrickhe`
as Implementer identity and `patrick-magiland` as Reviewer identity:

- Codex implements and Claude Code reviews, then the reverse, on
  `MagiLand/agent-squad-trial`; each reaches approval and a human-gated merge.
- A blocking finding is dispositioned and verified by a fresh Reviewer.
- A needs_human decision is recorded as a DECISION and honored subsequently.
- A stop and human-authorized continuation, plus a lost notification recovered
  through status after the Developer says “check the PR”.
- A real consuming repository completes the loop; documentation-only work
  is sufficient and Agent Squad runtime files must remain untracked.

Any deliberately seeded input or failed notification must be identified as
scripted in the evidence. Human Task approval, decisions, continuation, and
merge instructions must be recorded as actual human actions. Every new
prerequisite misconfiguration found by a trial needs a doctor check and test.

## Evidence status

[Issue #46 evidence](verification/2026-09-16-issue-46.md) records the current
release validation, exact runtime revision, live-trial progress, limitations,
and retained resources. The live release proof is incomplete until that record
contains all required outcomes; a `0.5.0` package version alone is not release
acceptance.

Earlier increment evidence remains useful within its stated scope:

- [Issue #42](verification/2026-09-12-issue-42.md): real forge publication with scripted review content.
- [Issue #43](verification/2026-09-13-issue-43.md): Reviewer lifecycle and harness delivery selection.
- [Issue #44](verification/2026-09-13-issue-44.md): packaged skills and merge mechanics.
- [Issue #45](verification/2026-09-16-issue-45.md): prerequisite diagnostics and real startup probes.

Each dated record follows the seven sections of §16.5 and lists private raw
evidence archive digests. Preserve unrelated checkouts and resources. Archiving
the trial repository, closing the milestone, and switching Agent Squad's own
review pipeline are Developer actions after the release PR merges.
