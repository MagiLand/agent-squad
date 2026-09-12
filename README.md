# Agent Squad

Agent Squad coordinates implementation and independent review through Herdr.
The GitHub pull request carries the Task, reviews, findings, dispositions,
Developer decisions, and approval evidence.

The implementation baseline is the [v0.5.0 specification delta](docs/agent-squad-v0.5.0-spec.md).
Increment 1 provides configuration, the GitHub adapter, forge commands, and
state derived from the PR. Reviewer lifecycle commands, installed skills,
merging, and the complete prerequisite checks follow in later increments.

Python 3.11 or later, Git, `gh`, and two authenticated GitHub accounts are
required. The Python runtime uses only the standard library.

```bash
python -m pip install .
agent-squad init --implementer-account <login> --reviewer-account <different-login>
agent-squad doctor
agent-squad issue view --issue 42
agent-squad status --pr 43 --json
```

Initialization discovers the primary checkout through Git's common directory,
creates schema 2 configuration there, and adds local Git exclusions. Existing
configuration is validated and kept. Move a schema 1 configuration aside and
rerun `init`; it is not migrated. Both configured storage roots must be writable.

The available forge commands are `issue view`, `pr head`, `pr reviews`,
`pr create`, `pr report`, `review post`, `thread reply`, `thread open`,
`thread resolve`, `decision post`, `stop post`, and `status`. Mutations require
`--as implementer` or `--as reviewer`, subject to each command's role.
Reads default to the Reviewer inside a squad review worktree, and to the
Implementer elsewhere. Tokens are selected per child process without changing
`gh`'s active account.

`pr create` accepts files containing the complete `## Task` and
`## Implementation report` sections. `review post` accepts a body beginning at
`## Summary`, with `## Verified dispositions` and `## Findings`; it generates
the Findings list from a JSON array supplied through `--threads`:

```json
[
  {
    "severity": "blocking",
    "category": "correctness",
    "title": "Describe the concrete problem",
    "path": "src/example.py",
    "line": 12,
    "body": "**Problem**: ...\n\n**Evidence**: ...\n\n**Impact**: ...\n\n**Required change**: ...\n\n**Verification**: ..."
  }
]
```

Use `[]` when there are no new findings. `start_line` is optional; anchors
must be right-side added or context lines in the reviewed diff. A plain
`review post` publishes a new review. `review post --resume <review-id>` repairs
missing roots for that identified publication. `thread open` recovers an
unanchored finding from the text already recorded on the PR.

Run deterministic validation with:

```bash
make test
make smoke
make doctor
```

`make test` includes the isolated forge smoke scenario. Tests use committed
fake forge and Herdr executables and never call real models or GitHub.
See [workflow verification](docs/workflow-verification.md) for scope and evidence.

Licensed under [Apache 2.0](LICENSE).
