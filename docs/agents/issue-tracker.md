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
