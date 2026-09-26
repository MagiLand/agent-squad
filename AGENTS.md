# Repository Guidelines

## Project Structure & Module Organization

The implementation baseline is `docs/agent-squad-v0.6.0-spec.md`, the approved delta over `docs/agent-squad-v0.5.0-spec.md`; do not treat files under ignored `.local/` as project inputs.

Implementation should follow the specification's focused increments. Production code belongs under `src/agent_squad/`, with the installed command defined in `cli.py`. Put unit tests in `tests/unit/`, Git and process integration tests in `tests/integration/`, reusable data in `tests/fixtures/`, smoke tooling in `scripts/`, and consumer examples in `examples/`. Prefer combining small modules over creating shallow wrappers.

## Build, Test, and Development Commands

Preserve these repository-level commands:

- `make test` — run the deterministic automated suite.
- `make smoke` — exercise the self-contained disposable-repository workflow.
- `make doctor` — check local Git, filesystem, and Herdr prerequisites.
- `python -m unittest discover -s tests` — run the standard-library test suite directly.

Never report a command as passing until its target exists and the command has run successfully.

## Coding Style & Naming Conventions

Target Python 3.11 or later and use four-space indentation. Follow `snake_case` for modules, functions, and variables; `PascalCase` for classes; and `UPPER_SNAKE_CASE` for constants. Add type annotations to public interfaces and protocol-bearing data. Prefer `pathlib`, typed models, and the standard library. Invoke subprocesses with argument arrays and `shell=False`. No formatter or linter is configured yet; keep changes PEP 8 compliant and avoid introducing runtime dependencies without justification.

## Testing Guidelines

Use `unittest`; name files `test_*.py` and test methods `test_<behavior>`. Every protocol derivation, validator, configuration write, recovery path, and Git identity check needs deterministic coverage. Use temporary repositories and fake Herdr processes for integration tests; automated tests must not call real models. The delta defines required coverage in Section 16; no numeric coverage threshold is currently set.

## Commit & Pull Request Guidelines

Current history uses short, imperative, typed subjects such as `docs: record issue 42 forge verification`. Continue with `feat:`, `fix:`, `docs:`, `test:`, or `refactor:` as appropriate. Keep pull requests focused on one implementation increment or independently reviewable slice. Link the governing issue or specification section, describe protocol or artifact changes, list exact verification commands and results, and identify any deliberate deviation from the approved specification.

## Security & Local State

Do not commit `.local/`, `.agent-squad/`, `.agent-squad-review/`, credentials, or generated review output. Preserve exact Git object identities and never treat a detached worktree as a security sandbox.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues for `MagiLand/agent-squad`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical triage labels defined in `docs/agents/triage-labels.md`.

### Domain docs

This repository uses a single-context domain-document layout. See `docs/agents/domain.md`.
