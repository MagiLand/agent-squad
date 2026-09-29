# A single-identity review loop

This example uses the commands shipped in v0.6.0. Start in the primary
checkout with ordinary Git transport working. Use the GitHub or
[Forgejo setup](../README.md#forgejo-setup) instructions, selecting `single`
and a separate person-operated approver. The example's numbers are
placeholders: issue and PR numbers need not match.

The Developer invokes `$squad-implementer` (Codex) or `/squad-implementer`
(Claude Code) with “Let's start on issue #42”. The Implementer reads:

```sh
agent-squad issue view --issue 42 --json
```

It uses a ready issue unchanged as the Task, creates the configured issue
worktree from the fetched base, implements, tests, commits, and pushes.
It writes the report in the configured issue scratch directory, then runs
from the issue worktree:

```sh
agent-squad pr create --as implementer --issue 42 --report "$REPORT"
```

Suppose the returned PR is 43. Unless the Developer retained the merge or a
review-before-merge hold applies, the Implementer records the actual start
instruction in a general decision. `$DECISION` begins with exactly
`Standing merge instruction: merge when approved.` and quotes that instruction.

```sh
agent-squad decision post --as implementer --pr 43 --finding none --body "$DECISION"
agent-squad reviewer launch --pr 43
```

A successful launch hands off and the Implementer becomes idle. The fresh
Reviewer receives fixed full head/base SHAs and a detached checkout. It
executes its review, publishes findings and notifies the Implementer through
the CLI. In single mode an agent approval is a `COMMENT` review, with the
protocol verdict in its body. This is distinct from a human approval.

On notification, or when the Developer says “check the PR”, the Implementer
reads authoritative state:

```sh
agent-squad status --pr 43 --json
```

Valid findings receive fixes or evidence-backed dispositions. Fixes are
committed, pushed and reported; a fresh Reviewer verifies them. Optional
findings receive a disposition even when left unchanged. On Forgejo the
Reviewer skips `thread resolve`, which is unsupported; verification replies
establish settlement. For interrupted publication it uses `review post
--resume <review-id>` with the original full command inputs. A pending draft
requires explicit `--discard-draft <draft-id>` after inspection, never an
implicit deletion.

When status is `await_human_approval`, the Implementer reports:

> approved by the agent at `<full-head-sha>`, waiting for approval from `<logins>`

The person reviews in the browser. A human request-changes is reported with
its login and commit; the Developer then instructs the next step. After fixes,
a fresh agent review and a replacement human approval must cover the current
head. The human tells the Implementer to “check the PR” after acting.

When status permits merge, the Implementer checks all CI at that exact head.
A reported merge hold still requires the Developer's explicit merge instruction.
It runs from the primary checkout:

```sh
agent-squad pr merge --as implementer --pr 43
```

Only a Developer-released hold permits adding `--accept-merge-hold`. If the
base moved, the Implementer merges the fetched base into its branch, validates,
pushes, updates the report, and requests a fresh review. It does not reuse an
approval for the older head.

After integration is verified, the command removes owned resources and the
unchanged tracking ref after proving the remote branch absent. The Implementer
reports cleanup, any retained resources, primary-checkout fast-forward result,
optional dispositions, and exact-head and base-push CI. It never runs a printed
fallback fast-forward command itself. Tagging and publishing the v0.6.0
release remain Developer actions.
