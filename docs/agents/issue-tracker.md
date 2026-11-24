# Issue Tracker: GitHub

Issues and specifications for this repository live in GitHub Issues. Use the `gh` CLI from within the repository so it resolves `MagiLand/agent-squad` from the configured remote.

File issues only into `MagiLand/agent-squad`, never into `mattpocock/skills`, the repository the engineering skills come from. Before the first `gh issue create` of a session, check the target with `gh repo view --json nameWithOwner`.

## Common operations

- Create: `gh issue create --title "..." --body-file <file>`. Use a file or a heredoc for multi-line bodies.
- Read: `gh issue view <number> --json number,title,body,labels,comments`. Outside a terminal, `gh issue view --comments` prints only the comments, not the body.
- List: `gh issue list --state open --json number,title,labels`, with `--label` filters as needed.
- Comment: `gh issue comment <number> --body "..."`
- Add a label: `gh issue edit <number> --add-label "..."`
- Remove a label: `gh issue edit <number> --remove-label "..."`
- Close: post the explanation with `gh issue comment <number> --body "..."`, then run `gh issue close <number>`.
- Make a sub-issue: `gh issue create --parent <parent> ...`, or `gh issue edit <parent> --add-sub-issue <child>` afterwards. Without sub-issues, put `Part of #<parent>` at the top of the child body.

Use JSON output and `jq` when a skill needs structured issue data.

## Pull requests as a request surface

**PRs as a request surface: no.**

External pull requests do not enter the triage queue automatically. `/triage` reads this flag, and it can be changed here later.

## Skill conventions

When a skill says “publish to the issue tracker,” create a GitHub issue. When it says “fetch the relevant ticket,” read the issue body, comments, and labels as in **Read** above.

Create implementation tickets in dependency order so blocking relationships can reference existing issue numbers.

## Triage output

Triage writes its result into the issue body, because the body is what the implementation agent builds from.

1. **The body is the Task.** `agent-squad pr create` copies the issue body into the pull request’s `## Task` section without rewording, and copies nothing else ([spec §7.2](../agent-squad-spec.md#72-pr-body)). A later Developer comment that changes what to build means the issue is no longer the Task, so the Implementer must draft a Task and wait for the Developer’s approval before coding. Triage comments are posted from the Developer’s account, so they count as Developer comments.
2. **Triage edits the body.** When triage settles open points, changes acceptance criteria, or otherwise changes what to build, rewrite the issue body with `gh issue edit <N> --body-file <file>` so that the body alone is the complete brief: objective, what to build, acceptance criteria, constraints, and out-of-scope items. Keep the original problem statement and evidence in the body; GitHub keeps the edit history.
3. **The triage comment holds evidence only.** The `## Triage` comment records what was verified: on `main` at which commit, with which probe, and with what result. Its first paragraph after the AI disclaimer says that the body holds the brief and that the comment changes nothing.
4. **Later changes follow the same rule.** A scope change after an issue is `ready-for-agent` edits the body, and a short comment says what changed and that the body now holds the change. Once a pull request is open, editing the body no longer changes its Task; the Task then changes only by a Developer instruction recorded with `agent-squad decision post --task` (spec §7.2 and §7.6).

## Blocking relationships

Use GitHub’s native issue dependencies:

- Add a blocker when creating an issue: `gh issue create --blocked-by <number>,<number> ...`
- Add a blocker afterwards: `gh issue edit <blocked-number> --add-blocked-by <blocker-number>`
- Count open blockers: `gh api repos/MagiLand/agent-squad/issues/<number> --jq .issue_dependencies_summary.blocked_by`. The count covers open blockers only, so 0 means the issue can start. `gh issue view <number> --json blockedBy` also lists closed blockers, so filter it with `--jq '[.blockedBy.nodes[] | select(.state == "OPEN")] | length'`.

With a `gh` older than 2.94, add the dependency through the REST API:

1. Resolve the blocking issue’s numeric database ID:

   `gh api repos/MagiLand/agent-squad/issues/<number> --jq .id`

2. Add the dependency:

   `gh api --method POST repos/MagiLand/agent-squad/issues/<blocked-number>/dependencies/blocked_by -F issue_id=<blocker-database-id>`

The database ID is not the visible issue number or GraphQL node ID. If native dependencies are unavailable, add `Blocked by: #<number>` to the issue body.

A ticket is ready when all blockers are closed and it has no assignee.

## Wayfinder conventions

A Wayfinder map is one issue labelled `wayfinder:map`, holding the Notes, Decisions-so-far, and Fog sections. Its tickets are sub-issues of the map. If sub-issues are unavailable, maintain a task list in the map and add `Part of #<map>` to the top of each child.

- **Child types:** label each child `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`.
- **Frontier:** the map’s open children with no open blocker and no assignee, in map order. The first one is next.
- **Claim:** `gh issue edit <number> --add-assignee @me`, as the session’s first write.
- **Resolve:** comment with the answer, close the issue, then add a one-line summary and link to the map’s Decisions-so-far.
