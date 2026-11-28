# Event Store

Every run of the `agent-squad` command appends one event to a file in the
user's state directory. The events let agents measure how the tool is used
across every repository it runs in: how often commands fail or are refused,
how long they take, and how often a loop stops or a merge leaves cleanup
incomplete. Humans read what those agents write, not the files.

An event holds no free text. Every value is a number, a boolean, `null`, or
a word from a fixed set the CLI defines. An event names the command, numbers
taken from the `--pr` and `--issue` options, the exit code, a hash that
identifies the repository without naming it, and, for five commands, an
outcome: what the command's write did, such as a review's verdict and its
number of findings. No title, body, path, branch, account name, finding ID,
or commit ID is stored. The only argument values stored are the Reviewer's
model and reasoning-effort level, and only in the form that
[Reviewer configuration](#reviewer-configuration) allows. No content of a
client repository leaves that repository through this file.

This is schema version 2. Version 1, introduced by
[#148](https://github.com/MagiLand/agent-squad/issues/148), recorded CLI runs
only; version 2 adds `outcome`
([#184](https://github.com/MagiLand/agent-squad/issues/184)). An outcome
describes a write but is not the record of it: reviews, decisions, and stops
stay on the forge, which remains their record.

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
- A run killed by another signal, such as a shell timeout's `SIGTERM`, ends
  without running the recorder and leaves no event.
- Setting `AGENT_SQUAD_EVENTS=off` records nothing and creates no directory.
  Any other value, including an empty one, leaves recording on.

## Fields

Each event has exactly these keys, in this order:

| Field | Value |
| --- | --- |
| `schema_version` | `2` |
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
| `outcome` | an object describing what the run's write did ([Outcomes](#outcomes)); `null` for every other run |

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

## Outcomes

Five commands record an outcome. Each records it when its write happened,
whatever the exit code, so a run that writes and then fails still describes
its write:

- `review post`: when the run published the review, or found it with
  `--resume`. This includes a run that published the review and then could
  not post a thread root (exit code `1`). A `--resume` run that the forge
  refuses over a pending draft (exit code `4`) records none.
- `thread reply`: when the reply was posted.
- `decision post`: when the decision was posted, or an identical Task
  amendment was reused. This includes a run that posted the decision and then
  could not mirror the Task (exit code `3`).
- `stop post`: when the `STOPPED` comment was posted.
- `reviewer launch`: when Herdr started the Reviewer. This includes a run that
  leaves a blocked Reviewer, or one Herdr reports not ready, for
  `reviewer adopt` (exit code `3`).

A refused run (exit code `4`), a usage error, and a run that fails before its
write have `outcome: null`, and so does every other command. A write counts
as happened once the forge answered it with the new object's ID, even when
reading it back then fails (exit code `1`), as Forgejo's review and reply
read-backs can. A run whose write cannot be confirmed, such as one
interrupted before the forge answered, has `outcome: null`.

Each outcome has exactly these keys, in this order:

| Command | Key | Value |
| --- | --- | --- |
| `review post` | `verdict` | `approved`, `changes_requested`, or `needs_human` |
| `review post` | `blocking` | the number of this review's findings of severity `blocking` |
| `review post` | `optional` | the number of this review's findings of severity `optional` |
| `review post` | `pass` | the review's position among the PR's tagged reviews, counting itself: `1` for the first |
| `review post` | `resumed` | `true` when the run found the review with `--resume`, otherwise `false` |
| `review post` | `diff_lines` | added plus deleted lines of `git diff <base> <head>` |
| `review post` | `diff_lines_since_previous` | added plus deleted lines of `git diff <previous> <head>`; `null` without a previous reviewed head or when it is not an ancestor of `head` |
| `review post` | `findings_on_changed_lines` | how many of this review's findings have an anchored line added or changed since the previous reviewed head; `null` when `diff_lines_since_previous` is |
| `thread reply` | `disposition` | `fixed`, `rejected`, or `needs-human`, as given; otherwise `null` |
| `thread reply` | `verification` | `fixed`, `rejection-accepted`, or `not-fixed`, as given; otherwise `null` |
| `thread reply` | `rejection` | `not-pursued` for `--not-pursued`, `deferred` for `--deferred-to` (the issue number is not recorded), otherwise `null` |
| `decision post` | `kind` | the kind on the Decision record line: `finding`, `task-approval`, `task-amendment`, `continuation`, `budget`, `hold-release`, or `answer`; `null` for a standing merge instruction or its withdrawal |
| `decision post` | `merge_instruction` | `record` or `withdraw`, as given; otherwise `null` |
| `decision post` | `recommendation` | `followed`, `changed`, or `none`, as given; `null` for a standing merge instruction or its withdrawal |
| `decision post` | `budget` | the number given to `--budget`, or `null` |
| `decision post` | `task_amended` | `true` when the decision carries a Task amendment, otherwise `false` |
| `decision post` | `reused` | `true` when the run reused an identical Task amendment instead of posting, otherwise `false` |
| `stop post` | `reason` | `budget`, `repeat`, `scope`, `design`, `ambiguity`, or `judgement` |
| `reviewer launch` | `reviewer_kind` | `claude` or `codex`, from `reviewer.kind` |
| `reviewer launch` | `model` | the model named in `reviewer.start_args`, or `null` ([Reviewer configuration](#reviewer-configuration)) |
| `reviewer launch` | `effort` | the reasoning-effort level named in `reviewer.start_args`, or `null` |
| `reviewer launch` | `reviewer_skill` | the first 16 lowercase hexadecimal characters of the SHA-256 digest of the installed `~/.agents/skills/squad-reviewer/SKILL.md`; `null` when that file cannot be read or is not a regular file |

### Review measures

- **Previous reviewed head.** The `head` of the latest tagged review before
  this one, from the reviews `review post` already reads. The read after
  publication decides, so a stranded draft that the run submitted before
  publishing counts as an earlier review; when that read fails, the reviews
  read before publication decide. For `--resume`, it is the latest one
  before the resumed review. For `--resume`, `blocking`, `optional`, and
  `pass` also describe the resumed review.
- **Line counts.** `diff_lines` and `diff_lines_since_previous` sum the
  added and deleted lines of `git diff --numstat` with the rename detection
  that anchor validation uses. A binary file counts `0`.
- **Findings on changed lines.** A finding counts when at least one of its
  anchored lines is added or changed on the head side of
  `git diff <previous> <head>`. For a finding with `start_line`, that is any
  line from `start_line` to `line`. A finding whose thread root is already
  published, as on `--resume`, is counted at that root's anchor. Context
  lines never count, including those `diff.interHunkContext` adds. This
  separates findings in fix code from findings in code an earlier pass could
  have reviewed.
- **Edge cases.** With no earlier tagged review, or a previous reviewed head
  that is not an ancestor of `head`, `diff_lines_since_previous` and
  `findings_on_changed_lines` are `null`. When the previous reviewed head
  equals `head`, both are `0`. A Git error, as opposed to a "not an
  ancestor" answer, makes the whole outcome `null`.

### Reviewer configuration

`model` and `effort` are read from `reviewer.start_args` only, never from a
harness configuration file, profile, or environment variable, and only from
the arguments before a `--` separator: what follows it is positional, such
as a prompt, even where an option before it still lacked its value. No other
element of `reviewer.start_args` is ever recorded, because it can hold a
credential.

| `reviewer.kind` | `model` forms | `effort` forms |
| --- | --- | --- |
| `claude` | `--model X`, `--model=X` | `--effort X`, `--effort=X` |
| `codex` | `--model X`, `--model=X`, `-m X`, `-c model=X`, `--config model=X` | `-c model_reasoning_effort=X`, `--config model_reasoning_effort=X` |

- **Quotes.** For a `-c` or `--config` value, one pair of matching single or
  double quotes around the whole value `X` is removed, so `-c model="o3"`
  records `o3`.
- **When the value is `null`:**
  - the arguments name the setting in none of these forms, including another
    spelling such as `--config=model=X`;
  - they name it more than once, even with the same value;
  - they select a Codex profile (`-p`, `--profile`) or Claude settings
    (`--settings`) and name the setting nowhere else;
  - after quote removal, the value does not match
    `^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,63}$` in full, which keeps a path, a
    space, or any longer text out of the store.

## Reading the events

- **Versions.** A version 1 event has no `outcome` key. One month's file can
  hold both versions, because the version changes with the installed CLI.
- **Counting each write once.** Count each review once by `project`, `pr`,
  and `outcome.pass`: a `--resume` run repeats the pass of the run that
  published it. Count each decision once by skipping events whose
  `outcome.reused` is `true`.
- **Passes.** A pass's `review post` or `stop post` event belongs to the
  latest `reviewer launch` event for the same `project` and `pr` before it.
  Pass duration and loop time follow from the `time` of the `reviewer
  launch`, `review post`, and `handoff` events of the same PR.
- **Reviewer rules.** To match a `reviewer_skill` value to a commit of this
  repository, hash `src/agent_squad/skills/squad-reviewer/SKILL.md` at that
  commit:

  ```bash
  git show <commit>:src/agent_squad/skills/squad-reviewer/SKILL.md \
    | shasum -a 256 | cut -c1-16
  ```

## Rules

- No command reads the store, and nothing in it is ever authority for any
  decision. Loop state is derived from the PR, Git, and Herdr alone (§3.2 of
  the specification), and the forge remains the record of reviews,
  decisions, and stops.
- A store that cannot be written, for any reason, changes neither the exit
  code nor the output of the command; the event is lost.
- An outcome never changes a command. Computing it makes no forge or Herdr
  call beyond the command's own. Any error while computing it, such as a
  failing `git diff`, records `outcome: null` and leaves the exit code,
  standard output, and standard error unchanged. With
  `AGENT_SQUAD_EVENTS=off`, no outcome is computed: no extra `git` process
  runs and the skill file is not read.
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
