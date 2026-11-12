# Release Procedure

A release publishes a new package version of Agent Squad: a pull request
changes the version and adds the release notes, and after it merges the merge
commit is tagged and the notes are published on the release page. The release
pull request goes through the review loop like every other pull request
([#150](https://github.com/MagiLand/agent-squad/issues/150)).

Release notes live in this repository, one file per release, at
`docs/releases/v<version>.md`. The release pull request adds the file, the
Reviewer reviews it there, and the same file becomes the text of the release
page. Notes for `v0.7.0` and earlier exist only on their release pages.

A release has four parts, described below in order:

1. [The release issue](#1-the-release-issue), whose body is the Task of the
   release pull request.
2. [The pull request](#2-the-pull-request), an ordinary pass through the
   loop.
3. [After the merge](#3-after-the-merge): tag, release page, milestone, and
   upgrade, outside the loop.
4. [What a release does not do](#4-what-a-release-does-not-do).

## Before you run a command

In the text, `<version>` is the version being released, without the leading
`v`, for example `0.8.0`. The commands read it from the shell variable
`version` and keep their files in the directory named by `scratch`. Set both
in every new shell, from the primary checkout of this repository:

```bash
version=0.8.0
scratch=$(mktemp -d)
```

The commands that read or change issues, runs, tags, releases, and milestones
use Git and the GitHub CLI (`gh`) with the account `gh` is logged in as. The
Developer runs them, or an agent session on the Developer's instruction; the
Implementer and the Reviewer do not run them as part of the loop. The
commands in the body to copy are `agent-squad` reads, which the Implementer
and the Reviewer run themselves.

## 1. The release issue

Each release has one issue in its milestone `v<version>`, titled
`Release v<version>`. Its body is the Task of the release pull request:
`agent-squad pr create` copies the body without rewording, so the body below
is complete, and the version is its only blank.

### Filing the issue

Write the body below to a file, with every `<version>` replaced, and file the
issue in the milestone. The first command takes the body from this document,
so the issue states exactly what is reviewed here:

```bash
awk '/^````markdown$/ { inside = 1; next } /^````$/ { inside = 0 } inside' \
  docs/release.md | sed "s/<version>/$version/g" > "$scratch/release-issue.md"
gh issue create --repo MagiLand/agent-squad --milestone "v$version" \
  --title "Release v$version" --body-file "$scratch/release-issue.md"
```

File the issue once every other issue in the milestone is closed, and triage
it straight away.

### Triage

The release notes are written and checked against two lists, the closed
issues of the milestone with the pull requests that closed them and the open
issues labelled `bug`. Nobody posts them: the Implementer and the Reviewer
each read them with `agent-squad issue list`
([#164](https://github.com/MagiLand/agent-squad/issues/164)) at the time of
their own work, as the body to copy says.

Triage the release issue only when it is the milestone's last open issue.
This command lists the milestone's open issues; it must print the release
issue alone:

```bash
gh issue list --repo MagiLand/agent-squad --milestone "v$version" \
  --state open --limit 1000 --json number,title \
  --jq '.[] | "#\(.number) \(.title)"'
```

The release Task needs an installed CLI and installed skills that include
`issue list`. This command exits 0 when the installed CLI has it, and 2 with
`invalid choice: 'list'` when it does not:

```bash
agent-squad issue list --help > /dev/null
```

When it exits 2, upgrade the installation first, on the Developer's
instruction, with the commands of [Step 5](#step-5-upgrade-the-installed-cli-and-skills).
The release's own tag does not exist yet, so install the newest commit of
`main` in place of the tag. Take the commit first:

```bash
git fetch origin main
commit=$(git rev-parse origin/main)
```

Then install it into the environment that holds the current installation:

```bash
python -m pip install --force-reinstall \
  "agent-squad @ git+https://github.com/MagiLand/agent-squad@$commit"
```

For an installation made with `uv tool`, use this command instead:

```bash
uv tool install --force \
  "agent-squad @ git+https://github.com/MagiLand/agent-squad@$commit"
```

Then finish as Step 5 describes, from `agent-squad --version` on, comparing
each refused skill with the copy at the commit, for example:

```bash
git show "$commit:src/agent_squad/skills/squad-implementer/SKILL.md" |
  diff ~/.agents/skills/squad-implementer/SKILL.md -
```

Then mark the issue ready:

```bash
issue=$(gh issue list --repo MagiLand/agent-squad --milestone "v$version" \
  --state open --limit 1000 --json number,title \
  --jq ".[] | select(.title == \"Release v$version\") | .number")
gh issue edit "${issue:?no open issue titled Release v$version}" \
  --repo MagiLand/agent-squad --add-label ready-for-agent
```

### The body to copy

````markdown
## Release v<version>

This issue releases Agent Squad `<version>`. It is the Task of the release
pull request, which goes through the review loop like any other pull request
(`docs/release.md`).

## What the pull request changes

- `src/agent_squad/__init__.py`: `__version__` becomes `"<version>"`.
- `tests/unit/test_cli_surface.py`: the deliberate version pin in
  `test_release_and_protocol_versions` becomes `"<version>"`.
- Every sentence that `test_documents_state_package_version` in
  `tests/unit/test_cli_surface.py` checks now states `<version>`:
  - `README.md`: "The package version is `<version>`" and
    "The version is `<version>`";
  - `docs/workflow-verification.md`: "require package metadata `<version>`";
  - `docs/agent-squad-spec.md`: the header line
    `- **Package version:** <version>` and the section 1 sentence
    "baseline for package version `<version>`".

  If that test checks another sentence at the pull request's head, that
  sentence changes too.
- A new notes file, `docs/releases/v<version>.md`, described below.

No other file changes.

## Where the lists come from

The notes are written from two lists, which the Implementer and the Reviewer
each read with these commands at the time of their own work:

- the closed issues of the milestone `v<version>`, each with the merged pull
  requests that the forge links to it as closing it in `closed_by`:
  `agent-squad issue list --milestone v<version> --state closed --json`;
- the open issues labelled `bug`:
  `agent-squad issue list --label bug --state open --json`.

`closed_by` is the forge's link, not a record of the event that closed the
issue; the skills say what follows from that. A `closed_by` of `null` means
the issue's closing pull requests are unknown, not that there are none. The
lists are evidence, and this issue stays the Task.

## What the notes file contains

The notes file is the text of the release page, which shows the title, so
the file has no top-level heading. It has these sections, in this order:

1. `## Issues closed`: one line for each issue in the first list,
   `- #<issue> <title> (#<pull request>)`, naming the pull requests in its
   `closed_by`, separated by commas when there are several. An issue whose
   `closed_by` is empty gives the reason it was closed instead, from its
   `state_reason`, for example "not planned". An issue whose `closed_by` is
   null names no pull request and reads
   `- #<issue> <title> (closing pull request unknown)`. This issue is not
   listed; it closes when the pull request merges.
2. `## Changes to act on`: every change an existing user must act on,
   including each changed command or option and each changed skill, and
   whether the protocol tag changed, stating the tag either way. To find
   them, compare the head with the previous release:
   `git diff --stat "$(git describe --tags --abbrev=0)" HEAD -- src/agent_squad/cli.py src/agent_squad/conventions.py src/agent_squad/skills`.
3. `## Upgrade`: how to upgrade the CLI and the skills together, with the
   commands of step 5 of "After the merge" in `docs/release.md` written out
   for `<version>`.
4. `## Known limitations`: one line for each issue in the second list,
   `- #<issue> <title>`, or "None." when the list is empty.

## What the Reviewer checks

- The stated versions agree: every file listed above states `<version>`,
  and `python -m unittest tests/unit/test_cli_surface.py` passes.
- The Reviewer runs both commands of "Where the lists come from" itself
  and compares the notes with its own output: no closed issue of the
  milestone is missing from the notes, each issue the notes name is in the
  first list, each pull request they name is one that `closed_by` gives for
  its issue, an issue whose `closed_by` is null has its closing pull request
  marked unknown, and the known limitations are exactly the open issues
  labelled `bug`.
- The checks at the head passed (`ci` in `agent-squad status --pr <N>
  --json`).

## Review before merging

The pull request releases, so item 5 of the review-before-merge rule
applies: it waits for the Developer's review before it merges.

## Acceptance criteria

- [ ] `__version__`, the version pin, and every sentence the
  version-consistency test checks state `<version>`.
- [ ] `docs/releases/v<version>.md` exists with the four sections above, and
  its lists match the output of
  `agent-squad issue list --milestone v<version> --state closed --json` and
  `agent-squad issue list --label bug --state open --json`.
- [ ] No other file changes.
- [ ] `make test` and `make lint` pass.

## Out of scope

- The steps after the merge in `docs/release.md`: the tag, the release page,
  closing the milestone, and the upgrade.
- Release acceptance, which the Developer decides separately on the recorded
  evidence.
````

## 2. The pull request

The release issue is an ordinary Task. The Implementer starts it like any
other issue, opens the pull request with `agent-squad pr create`, which
copies the body, and has the head reviewed. The Reviewer checks what the body
lists under "What the Reviewer checks", along with its usual review.

Item 5 of the
[review-before-merge rule](agent-squad-spec.md#122-squad-implementer-mandatory-rules)
applies because the pull request releases: its notes file becomes the release
page, and the merge commit becomes the tagged version. The pull request
therefore waits for the Developer's review before it merges, and the
Implementer records no standing merge instruction. After the Developer
instructs the merge, the
Implementer merges with `agent-squad pr merge` and reports as for any pull
request.

## 3. After the merge

The Developer runs these steps, or an agent session that the Developer
instructs, each step on the Developer's instruction. They are not part of the
Implementer skill, whose work ends with its report after the merge. Run them
in this order, in one shell, from the primary checkout, with `version` set.

### Step 1. Wait for the run at the merge commit

Find the release pull request's merge commit on `main`: the first commit on
`main` whose change against its first parent sets `__version__` to the
version. For a squash merge it is the squash commit.

```bash
git fetch origin main
merge=$(git log --first-parent --format=%H \
  -S "__version__ = \"$version\"" origin/main -- src/agent_squad/__init__.py |
  tail -n 1)
git show --no-patch --format='%H %s' "${merge:?no commit on main sets $version}"
```

Then wait for the push run of the test workflow at that commit:

```bash
run=$(gh run list --repo MagiLand/agent-squad --workflow test.yml \
  --event push --commit "$merge" --limit 1 --json databaseId \
  --jq '.[0].databaseId')
gh run watch "${run:?no push run at $merge yet}" --repo MagiLand/agent-squad \
  --exit-status
```

The push run includes the macOS jobs and the `main-only` job, which do not
run on pull requests. If the first command finds no run, the run has not
started yet; wait a minute and repeat it. Go on only when `gh run watch`
exits 0. A job that was cancelled at its timeout without ever getting a
runner points to a runner shortage, not a defect; rerun the failed jobs with
`gh run rerun "$run" --repo MagiLand/agent-squad --failed` and watch again.
Any other failure stops the release until the Developer decides how to fix
it.

### Step 2. Tag the merge commit

Create an annotated tag on the merge commit, with the message the earlier
tags use, and push it:

```bash
git tag -a "v$version" -m "Agent Squad v$version" "$merge"
git push origin "v$version"
```

### Step 3. Publish the release

Publish the release from the tag, with the notes file at the tag as its
text, so that the published notes are exactly the reviewed file:

```bash
git show "v$version:docs/releases/v$version.md" |
  gh release create "v$version" --repo MagiLand/agent-squad --verify-tag \
    --latest --title "Agent Squad v$version" --notes-file -
```

`--verify-tag` refuses to publish if the tag is not on the forge.

### Step 4. Close the milestone

The release issue closed when the pull request merged, so the milestone has
no open issue left. This command must print nothing; settle any issue it
prints first:

```bash
gh issue list --repo MagiLand/agent-squad --milestone "v$version" \
  --state open --limit 1000 --json number,title \
  --jq '.[] | "#\(.number) \(.title)"'
```

Then close the milestone:

```bash
milestone=$(gh api --paginate \
  "repos/MagiLand/agent-squad/milestones?state=open" \
  --jq ".[] | select(.title == \"v$version\") | .number")
gh api --method PATCH \
  "repos/MagiLand/agent-squad/milestones/${milestone:?no open milestone v$version}" \
  -f state=closed --jq '.title + " " + .state'
```

### Step 5. Upgrade the installed CLI and skills

Only when the Developer asks. Upgrade the CLI and the skills together,
because a newer skill names commands that an older CLI lacks. Install the tag
into the environment that holds the current installation:

```bash
python -m pip install --force-reinstall \
  "agent-squad @ git+https://github.com/MagiLand/agent-squad@v$version"
```

For an installation made with `uv tool`, use this command instead:

```bash
uv tool install --force \
  "agent-squad @ git+https://github.com/MagiLand/agent-squad@v$version"
```

Then install the skills and check the result:

```bash
agent-squad --version
agent-squad skill install
```

`agent-squad --version` prints the version. `skill install` refuses to
replace an installed skill that differs from the packaged one, which is
expected when the release changed a skill. Compare each refused skill with
the release's copy, for example:

```bash
git show "v$version:src/agent_squad/skills/squad-implementer/SKILL.md" |
  diff ~/.agents/skills/squad-implementer/SKILL.md -
```

When the only differences are the release's own changes, run
`agent-squad skill install --force`. Agents read their skills when a session
starts, so start a new session for each running Implementer agent. Finally run
`agent-squad doctor` in the primary checkout of each repository that uses
Agent Squad; it also checks that the installed skills match the installed
package.

## 4. What a release does not do

A release is not a release acceptance. A merged release pull request, a
passing push run, a tag, and a release page establish the published version
and its deterministic checks, not live behavior. Whether a release is
accepted is the Developer's separate decision, made on the recorded evidence,
as the [README](../README.md) and the
[workflow verification](workflow-verification.md) document already say.
