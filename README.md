# Agent Squad

Agent Squad is a lightweight local tool for coordinating an implementation agent and an independent review agent through Herdr. It is intended to automate the routine implementation-review handoff while preserving developer control over requirements, architecture, risk, and final integration.

## Project status

Agent Squad is being implemented against the approved version 0.4.4 baseline. The current command-line application can initialize a repository, start one authoritative local run, and inspect its status. Review submission and Herdr handoff commands are not implemented yet.

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

Start a run with an approved UTF-8 Markdown task. The Implementer agent name,
Reviewer kind, and base reference default to `.agent-squad/config.json`; pass
them explicitly to override those selections for this run. `--implementer`
changes the agent name within the configured `implementer.kind`; change the
configuration to select another Implementer kind. Configured Reviewer
`start_args` are used only when the configured Reviewer kind is selected.
Repeat `--context` to capture only the additional files the run needs.

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

Status validates the captured task and context digests before reporting the active phase, local Git identities, selected roles, fixed base, review budget, and next action.

## Development

Run the deterministic unit and Git integration tests with either command:

```bash
make test
python -m unittest discover -s tests
```

## License

Agent Squad is licensed under the [Apache License 2.0](LICENSE).
