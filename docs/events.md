# Event Store

Every run of the `agent-squad` command appends one event to a file in the
user's state directory. The events let agents measure how the tool is used
across every repository it runs in: how often commands fail or are refused,
how long they take, and how often a loop stops or a merge leaves cleanup
incomplete. Humans read what those agents write, not the files.

An event holds no free text. It names the command, numbers taken from the
`--pr` and `--issue` options, the exit code, and a hash that identifies the
repository without naming it. No title, body, path, branch, account name,
commit id, or other argument value is stored, so no content of a client
repository leaves that repository through this file.

This is schema version 1, introduced by
[#148](https://github.com/MagiLand/agent-squad/issues/148). It records CLI
runs only. Reviews, decisions, and stops stay on the forge and can be joined
to these events later by `project` and PR or issue number.

## Location

- The directory is `$XDG_STATE_HOME/agent-squad/events/`. When
  `XDG_STATE_HOME` is unset, empty, or a relative path (which the XDG Base
  Directory specification treats as invalid), it is
  `~/.local/state/agent-squad/events/`.
- Each UTC month has one file, `events-<YYYY>-<MM>.jsonl`, named after the
  event's `time`.
- Each line is one event: a JSON object followed by a newline, written with a
  single append to a file opened in append mode.
- The CLI creates the `agent-squad` and `events` directories, and the state
  directory when it is missing, with mode `0700`, and the file with mode
  `0600`. The state directory itself may be reached through a symbolic link;
  a symbolic link at `agent-squad`, at `events`, or at the file is not
  followed, and nothing is recorded.
- Nothing rotates or deletes old files.

## When an event is written

- One event for every run that reaches a command, whether it succeeds,
  fails, or is refused by a gate.
- A usage error (exit code `2`) is recorded. Its `group` and `command` are
  set only where the first arguments are names the CLI defines. Its `role`,
  `pr`, and `issue` are set only when every option was parsed before the
  error was found, such as `pr create --partial` without `--task`.
- A run that ends in an uncaught exception is recorded with exit code `1`,
  and one interrupted with Ctrl-C with `130`.
- `--help` and `--version` are not recorded.
- Setting `AGENT_SQUAD_EVENTS=off` records nothing and creates no directory.
  Any other value, including an empty one, leaves recording on.

## Fields

Each event has exactly these keys, in this order:

| Field | Value |
| --- | --- |
| `schema_version` | `1` |
| `id` | a random version 4 UUID in its lowercase hyphenated form |
| `time` | UTC time at the end of the run, `YYYY-MM-DDTHH:MM:SS.mmmZ` |
| `tool_version` | the package version, such as `0.7.0` |
| `project` | 16 lowercase hexadecimal characters identifying the configured repository (below); `null` when the run read no configuration |
| `forge` | `github` or `forgejo` from the configuration the run read; `null` when it read none |
| `group` | the command group, one of `init`, `doctor`, `issue`, `skill`, `pr`, `review`, `thread`, `decision`, `stop`, `status`, `review-worktree`, `reviewer`, `handoff`; `null` for a usage error whose first argument is not one of them |
| `command` | the subcommand within `group` as the CLI defines it, such as `merge` for `pr merge`; `null` for `init`, `doctor`, and `status`, which have none, and for a usage error whose second argument is not one |
| `role` | the value given to `--as`, `implementer` or `reviewer`; `null` when `--as` was not given. The role a read command derives by default is not recorded |
| `pr` | the number given to `--pr`, or `null` |
| `issue` | the number given to `--issue`, or `null`. Other numeric options, such as `--from-issue`, are not recorded |
| `exit_code` | the process exit code (§10.1 of the specification) |
| `duration_ms` | wall time in whole milliseconds from the start of the CLI's entry point to the end of the run; Python start-up and module imports are not included |

The command names are those of the specification's
[command table](agent-squad-spec.md#102-command-table).

### Project identifier

`project` is the first 16 hexadecimal characters of the SHA-256 digest of
four configured values joined by newlines and encoded as UTF-8: `forge.kind`,
`forge.base_url` (the empty string when the configuration has none, as for
GitHub), `forge.owner`, and `forge.repo`, exactly as written in
`.agent-squad/config.json`. Letter case is kept, so one repository configured
with different letter case yields two identifiers. For example:

```bash
printf 'github\n\nMagiLand\nagent-squad' | shasum -a 256 | cut -c1-16
# bd476f319d518727
```

The configuration is the one the run read. `init` records the configuration
it wrote or found; `skill install` and a usage error read none.

## Rules

- No command reads the store, and nothing in it is ever authority for any
  decision. Loop state is derived from the PR, Git, and Herdr alone (§3.2 of
  the specification).
- A store that cannot be written, for any reason, changes neither the exit
  code nor the output of the command; the event is lost.
- `doctor` reports the store's path in an `event store` row: `pass` when it
  is writable, a warning when it is not (never a failure), or `pass` naming
  `AGENT_SQUAD_EVENTS=off` when recording is disabled. The check creates the
  directories and the month's file, as the run's own event would.
- Nothing is sent anywhere. Copying events between machines, a report
  command, and a readable project alias are later work.

## Tests

The automated suite never writes to a user's store. `tests/__init__.py` sets
`AGENT_SQUAD_EVENTS=off` for in-process calls, and the forge fixtures run
the CLI with `XDG_STATE_HOME` inside their temporary directory. Only the
tests of this feature point the store at a temporary directory of their own.
