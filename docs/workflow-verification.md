# Workflow verification

The [v0.5.0 delta](agent-squad-v0.5.0-spec.md#16-testing-strategy) defines the
verification strategy. Increment 1 implements the forge steps of §16.3; later
increments extend the scenario to the Reviewer lifecycle and merge workflow.

```bash
make test
make smoke
python3 scripts/run-smoke-tests --json
```

The runner creates a temporary repository, bare origin, and issue worktree.
It uses the committed `tests/fixtures/gh` and `fake_herdr.py` executables.
Every review, disposition, verification, decision, and stop is scripted.
It exercises creation, findings, human-decision gates, approval, a later push,
budget exhaustion and extension, batch rejection, missing-root recovery, and
explicit resumption of an interrupted publication. Its JSON output records
commands and exits. It removes its owned temporary directory on success or
failure and runs from source exports without `.git`.

The fixture environment excludes inherited Git, Python, and authentication
overrides; its Git configuration is isolated. It never writes an intentional
defect into the development checkout, calls a real model, or contacts GitHub.
The scripted evidence establishes command behavior, not independent judgment.

`make doctor` runs the retained prerequisite checks with schema 2: non-bare
repository and configuration, local exclusions, writable roots, Git object
format and HEAD, installed Herdr schema and live protocol, integration status,
and Implementer identity. An absent Implementer is a warning. The remaining
checks are scheduled for Increment 4; live Reviewer preflight is Increment 2.

The live forge exercise for issue #42 is recorded under `docs/verification/`
with the exact implementation SHA, trial PR and reviewed head, review ID,
commands, tool versions, and cleanup. The exercise posts a scripted review
through the new CLI as `patrick-magiland` and reads it back as derived state;
it does not claim the later live-agent trials have run.
