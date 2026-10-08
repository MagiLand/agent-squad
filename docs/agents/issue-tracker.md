# Issue Tracker: GitHub

Issues and specifications for this repository live in GitHub Issues. Use the `gh` CLI from within the repository so it resolves `MagiLand/agent-squad` from the configured remote.

## Common operations

- Create: `gh issue create --title "..." --body "..."`
- Read with comments: `gh issue view <number> --comments`
- List: `gh issue list --state open`
- Comment: `gh issue comment <number> --body "..."`
- Add a label: `gh issue edit <number> --add-label "..."`
- Remove a label: `gh issue edit <number> --remove-label "..."`
- Close: `gh issue close <number> --comment "..."`

Use JSON output and `jq` when a skill needs structured issue data.

## Pull requests as a request surface

**PRs as a request surface: no.**

External pull requests do not enter the triage queue automatically. This setting can be changed here later.

## Skill conventions

When a skill says “publish to the issue tracker,” create a GitHub issue. When it says “fetch the relevant ticket,” read the issue body, comments, and labels.

Create implementation tickets in dependency order so blocking relationships can reference existing issue numbers.

## Triage output

Triage writes its result into the issue body, because the body is what the implementation agent builds from.

1. **The body is the Task.** `agent-squad pr create` copies the issue body into the pull request’s `## Task` section without rewording, and copies nothing else ([spec §7.2](../agent-squad-spec.md#72-pr-body)). A later Developer comment that changes what to build means the issue is no longer the Task, so the Implementer must draft a Task and wait for the Developer’s approval before coding. Triage comments are posted from the Developer’s account, so they count as Developer comments.
2. **Triage edits the body.** When triage settles open points, changes acceptance criteria, or otherwise changes what to build, rewrite the issue body with `gh issue edit <N> --body-file <file>` so that the body alone is the complete brief: objective, what to build, acceptance criteria, constraints, and out-of-scope items. Keep the original problem statement and evidence in the body; GitHub keeps the edit history.
3. **The triage comment holds evidence only.** The `## Triage` comment records what was verified: on `main` at which commit, with which probe, and with what result. Its first paragraph after the AI disclaimer says that the body holds the brief and that the comment changes nothing.
4. **Later changes follow the same rule.** A scope change after an issue is `ready-for-agent` edits the body, and a short comment says what changed and that the body now holds the change. Once a pull request is open, editing the body no longer changes its Task; the Task then changes only by a Developer instruction recorded with `agent-squad decision post --task` (spec §7.2 and §7.6).

## Blocking relationships

Use GitHub’s native issue dependencies when available:

1. Resolve the blocking issue’s numeric database ID:

   `gh api repos/MagiLand/agent-squad/issues/<number> --jq .id`

2. Add the dependency:

   `gh api --method POST repos/MagiLand/agent-squad/issues/<blocked-number>/dependencies/blocked_by -F issue_id=<blocker-database-id>`

The database ID is not the visible issue number or GraphQL node ID. If native dependencies are unavailable, add `Blocked by: #<number>` to the issue body.

A ticket is ready when all blockers are closed and it has no assignee.

## Wayfinder conventions

A Wayfinder map is one issue labelled `wayfinder:map`, with decision tickets represented as sub-issues when available. If sub-issues are unavailable, maintain a task list in the map and add `Part of #<map>` to each child.

Use `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task` for child types. Claim work by assigning the issue before making changes.
