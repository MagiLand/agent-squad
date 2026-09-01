# Agent Squad v0.4.4

## Implementation Review Squad Specification

**Version:** 0.4.4  
**Status:** Implementation baseline; ready to hand to the implementing agent  
**Primary runtime:** Herdr  
**Initial supported coding agents:** Codex CLI and Claude Code  
**Reference implementation target:** Python 3.11 or later on macOS and Linux

---

## 1. Executive Summary

Agent Squad is a lightweight local tool for coordinating a **small, organized group of AI coding agents** that work toward a common software-engineering objective.

The name *squad* is intentional. It uses the ordinary sense of a small group engaged in a common effort. Agent Squad is not intended to create an agent swarm, a large autonomous workforce, or a general distributed orchestration platform.

Version 0.4.4 supports one collaboration pattern only:

> **Implementation Review Squad:** one coding agent implements a change, a different coding agent independently reviews the exact committed revision, and the two agents iterate until the revision is approved or human judgment is required.

The primary product outcome is equally narrow:

> Remove the developer's repetitive message-relay work from the implementation-review cycle without removing the developer's authority over requirements, architecture, risk, or final integration.

A normal run is:

```text
Developer defines the task
        ↓
Implementer discusses and implements
        ↓
Implementer commits and submits through Agent Squad
        ↓
Reviewer is launched in an isolated Git worktree
        ↓
Reviewer records findings and notifies Implementer
        ↓
Implementer fixes, rebuts, or escalates
        ↓
Reviewer re-reviews
        ↓
Approved or Needs Human
        ↓
Developer performs final integration
```

Agent Squad automates coordination and preserves review correctness. It does not automate engineering judgment.

---

## 2. Intended Use of This Specification

This document is the candidate implementation baseline for Agent Squad v0.4.4.

It consolidates:

- the original implementation-review-loop concept;
- the exact-revision, isolation, artifact, and recovery requirements identified during the v1, v2, and v0.3 reviews;
- the decision to reject distributed-system and general-framework machinery that is disproportionate to the local MVP;
- the product decision to build Agent Squad as an independent repository rather than as a Double Dubs subsystem;
- the final protocol clarifications needed to prevent silent stalls, repeated human decisions, and implementation-time invention of state semantics.

The document is intentionally precise, but it must not be interpreted as permission to expand the product into a generic multi-agent workflow engine.

The final focused protocol review answered four questions before this implementation baseline was finalized:

1. Can a normal implementation-review run still become silently stalled without a visible recovery path?
2. Can any path approve or complete a revision other than the exact revision that was validly reviewed?
3. Can a recorded Developer resolution fail to reach a later round-scoped Reviewer?
4. Does any required protocol behavior remain ambiguous enough that the implementing agent would need to invent workflow semantics?

Implementers and future reviewers should continue to reject machinery whose primary purpose is hypothetical future squads, distributed operation, or hostile multi-user environments.

A finding that adds complexity without materially improving the supported local Implementation Review Squad should be treated as a scope-expansion proposal, not automatically as a blocking defect.

---

## 3. Normative Language

The terms **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.

- **MUST / REQUIRED** identifies a condition necessary for v0.4.4 acceptance.
- **SHOULD** identifies a strong recommendation that may be departed from only with a documented reason.
- **MAY** identifies an optional capability.

Sections labelled *Informative* explain design history or rationale and are not independently normative.

---

## 4. Problem Statement

### 4.1 The existing workflow

A developer who uses both Codex and Claude Code commonly performs a sequence such as:

```text
Developer asks Codex to implement
        ↓
Codex finishes
        ↓
Developer tells Claude Code to review
        ↓
Claude Code reports findings
        ↓
Developer tells Codex to evaluate and fix
        ↓
Codex commits another revision
        ↓
Developer tells Claude Code to review again
```

The developer remains necessary for requirements, decisions, and risk. The repeated handoff messages, however, carry little engineering value. They are coordination overhead.

### 4.2 Why a second agent matters

Asking the implementing agent to review its own work does not create the same degree of independence.

An implementer naturally forms assumptions about:

- what the task means;
- which constraints matter;
- which design is appropriate;
- which edge cases are plausible;
- which tests are sufficient.

Those assumptions help the agent implement efficiently, but they may also prevent it from noticing defects rooted in the same mental model.

### 4.3 Model and harness diversity

Codex and Claude Code differ in more than branding. They may use different:

- underlying models;
- system instructions;
- coding harnesses;
- repository exploration strategies;
- tool interfaces;
- implementation preferences;
- failure modes.

Agent Squad begins with the practical hypothesis that this diversity can sometimes produce a **1 + 1 > 2** effect when the agents are assigned genuinely different roles.

This is a hypothesis to test through real engineering work, not an assumption that every additional agent necessarily improves quality.

### 4.4 The problem Agent Squad solves

Agent Squad v0.4.4 solves the following problem only:

> How can one implementation agent and one independent review agent exchange work repeatedly through Herdr, review exact Git revisions, preserve structured findings, and escalate real decisions to the developer without requiring the developer to act as a messenger?

---

## 5. Product Definition

### 5.1 Agent Squad

Agent Squad is a standalone local developer tool.

It provides:

- role assignment for one Implementer and one Reviewer;
- a concrete implementation-review protocol;
- local run state;
- Git revision and worktree management;
- structured request, review, and response artifacts;
- Herdr-based handoff messages;
- bounded iteration and human escalation.

### 5.2 Implementation Review Squad

The Implementation Review Squad is the only squad implemented in v0.4.4.

It has exactly two active agent roles:

1. **Implementer**
2. **Reviewer**

Either supported agent may take either role:

```text
Codex implements → Claude Code reviews
```

or:

```text
Claude Code implements → Codex reviews
```

### 5.3 Squad does not mean swarm

Agent Squad is designed for a small, directed group.

Version 0.4.4 does not support:

- dozens of agents;
- autonomous agent spawning;
- nested squads;
- self-organizing agent networks;
- dynamically generated roles.

The project name expresses organized collaboration, not scale.

---

## 6. Scope and Operating Assumptions

Version 0.4.4 assumes:

- one developer;
- one local machine;
- one local OS account;
- one implementation worktree per active run;
- one active Agent Squad run per implementation worktree;
- one Implementer session;
- one round-scoped Reviewer session per review round;
- cooperative but fallible coding agents;
- Herdr is installed and running;
- Codex and Claude Code are already usable through Herdr;
- the Herdr skill may already be installed for both agents;
- the target project is a Git repository;
- the developer remains available when `needs_human` is reached.

Version 0.4.4 is not designed for:

- multiple developers coordinating through a shared state store;
- multiple machines;
- hostile processes sharing one OS account;
- cloud workers;
- remote message delivery guarantees;
- multiple concurrent writers to one worktree.

These assumptions are part of the product boundary and must guide implementation choices.

---

## 7. Goals

### 7.1 Eliminate human message relay

After a run starts, the normal implementation-review-fix-review cycle MUST proceed without the developer sending routine relay messages between the agents.

### 7.2 Preserve independent review

The Reviewer MUST review a dedicated Git worktree fixed at the requested revision rather than the Implementer's mutable working tree.

### 7.3 Bind approval to exact code

Every review MUST identify the exact base and head Git object IDs. An approval MUST apply only to the reviewed `head_oid`.

### 7.4 Preserve durable, readable evidence

Task definitions, implementation reports, review results, implementation responses, and state transitions MUST be stored as local artifacts. Terminal conversation history MUST NOT be the only authoritative record.

### 7.5 Preserve human authority

The developer MUST remain responsible for:

- changing task requirements;
- architectural and product decisions;
- compatibility policy;
- material security or operational risk;
- unresolved disagreement;
- final merge, push, release, or deployment.

### 7.6 Keep the tool small

The reference implementation SHOULD remain understandable as a small local CLI with direct, explicit logic for one squad type.

---

## 8. Explicit Non-goals

Agent Squad v0.4.4 MUST NOT be designed as:

- a general-purpose multi-agent framework;
- a workflow DAG engine;
- a generic role registry;
- an autonomous software-development platform;
- an agent swarm;
- an agent factory;
- a distributed scheduler;
- a durable distributed message broker;
- an exactly-once delivery system;
- a multi-user coordination system;
- an enterprise access-control system;
- a security boundary against malicious same-user processes;
- a CI/CD platform;
- a GitHub Actions replacement;
- an automatic merge system;
- a deployment controller;
- an issue-triage tool;
- a release-preparation system;
- a documentation pipeline;
- a general task scheduler.

Future collaboration patterns may be explored later. Their hypothetical needs MUST NOT shape the v0.4.4 core unless they are also necessary for the Implementation Review Squad.

---

## 9. Design Principles

### 9.1 Cognitive separation is the source of value

The product uses more than one agent only where role separation creates meaningful independence or complementary perspective.

The Implementer and Reviewer MUST have distinct responsibilities.

### 9.2 Human-guided, not human-relayed

The developer should make decisions, not carry routine notifications.

### 9.3 Correctness over conversational continuity

Reviewer session continuity is less important than reviewing the exact intended revision.

A fresh Reviewer in an isolated worktree is preferable to a persistent Reviewer that might inspect mutable or stale content.

### 9.4 Artifacts over terminal memory

The protocol MUST use files for authoritative content. Herdr prompts are control messages, not a database.

### 9.5 Persist before notifying

A logical request or result MUST be recorded before Agent Squad attempts to send the corresponding Herdr prompt.

This prevents a transport failure from erasing the underlying engineering state.

### 9.6 Local recoverability over distributed machinery

The implementation MUST recover safely from ordinary local interruption, but it MUST NOT introduce leases, fencing tokens, consensus, or a message broker merely to obtain distributed guarantees that the product does not need.

### 9.7 Extract abstractions from proven repetition

Do not create a generic `Squad`, `Workflow`, `RoleRegistry`, or `StepGraph` abstraction simply because future squad types are conceivable.

If future squads reveal actual shared requirements, abstractions may be extracted then.

### 9.8 One writer per worktree

Only the Implementer writes production code in the implementation worktree.

The Reviewer MAY write review artifacts and generated validation output in the isolated review worktree, but MUST NOT modify tracked files.

---

## 10. Relationship with Herdr

Herdr is the runtime and communication substrate.

Herdr is responsible for capabilities such as:

- panes and workspaces;
- coding-agent sessions;
- agent discovery;
- starting an agent in a pane;
- delivering prompts;
- reading terminal output for diagnostics;
- opening Git worktrees.

Agent Squad is responsible for:

- roles;
- run state;
- Git revision binding;
- review-worktree preparation;
- request and result artifacts;
- state transitions;
- handoff prompts;
- human escalation.

Conceptually:

```text
                   Developer
                       |
                       v
                 Agent Squad CLI
                    /       \
                   /         \
             Git adapter   Herdr adapter
                 |              |
                 v              v
       Implementation tree   Agent sessions
                 |          /              \
                 |    Implementer        Reviewer
                 |                         |
                 +---- exact revision -----+
```

Herdr agent status is a scheduling hint, not protocol truth.

For example, `idle` does not prove that a review was completed. A review is complete only when a valid review artifact has been submitted and applied.

`agent prompt --wait` MUST NOT be used as proof that a specific protocol message was processed.

`agent read` MAY be used for diagnostics, but terminal prose MUST NOT be parsed as the authoritative verdict.

---

## 11. Repository Independence

Agent Squad MUST live in its own repository.

A likely repository name is:

```text
agent-squad
```

It MUST be usable in arbitrary local Git repositories and MUST NOT require Double Dubs-specific conventions.

Double Dubs may be the first dogfood repository, but the following remain project-local concerns:

- branch naming;
- issue-tracker policy;
- `.scratch/` conventions;
- repository-specific agent instructions;
- test commands;
- build output;
- release process.

Agent Squad configuration MAY reference project-specific commands or context, but the core protocol MUST NOT encode them.

---

## 12. Reference Implementation Constraints

The v0.4.4 reference implementation SHOULD use:

- Python 3.11 or later;
- the Python standard library for runtime functionality;
- `pathlib` for paths;
- `subprocess` with argument arrays and `shell=False`;
- `json` for machine-readable artifacts;
- `dataclasses` or typed data models for internal representation;
- `fcntl.flock` for the local POSIX state lock;
- temporary files plus `os.replace()` for atomic state writes;
- `unittest` for the base test suite.

Supported operating systems:

- macOS;
- Linux.

Windows support is outside v0.4.4.

A small packaging dependency used only to install the project MAY be introduced through standard Python packaging, but the installed runtime SHOULD NOT require third-party libraries.

---

## 13. Suggested Agent Squad Repository Layout

The implementation repository SHOULD resemble:

```text
agent-squad/
├── README.md
├── LICENSE
├── pyproject.toml
├── Makefile
├── src/
│   └── agent_squad/
│       ├── __init__.py
│       ├── cli.py
│       ├── models.py
│       ├── state_store.py
│       ├── git_adapter.py
│       ├── herdr_adapter.py
│       ├── artifacts.py
│       ├── validation.py
│       ├── review_worktree.py
│       └── prompts.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── scripts/
│   └── run-smoke-tests
├── docs/
│   ├── PROTOCOL.md
│   ├── OPERATIONS.md
│   ├── REVIEW-POLICY.md
│   ├── TROUBLESHOOTING.md
│   └── SECURITY-AND-LIMITATIONS.md
└── examples/
    └── double-dubs/
```

This layout is a recommendation, not a reason to introduce unnecessary modules. The implementation MAY combine small modules when that improves clarity.

The project MUST provide an installed `agent-squad` command.

For repository-local development, it SHOULD also provide repeatable Make targets such as:

```text
make test
make smoke
make doctor
```

---

## 14. Target-Environment Discovery

The implementation MUST verify the locally installed Herdr command surface rather than hard-code examples from a historical Herdr version.

During implementation and integration testing, inspect at least:

```bash
herdr --version
herdr --skill
herdr api schema --json
herdr api snapshot
herdr agent --help
herdr agent start --help
herdr agent prompt --help
herdr agent get --help
herdr worktree --help
herdr integration status
```

Expected handling:

- machine-readable Herdr outputs SHOULD be parsed as structured data;
- `herdr integration status` MUST be treated as plain text unless the installed version explicitly provides a structured option;
- the implementation MUST NOT install, replace, or modify the user's globally installed Herdr skill automatically;
- command arguments and response fields MUST be based on the installed Herdr schema and tested fixtures.

Herdr version numbers observed during earlier design reviews are informative only and MUST NOT be hard-coded.

---

## 15. Roles and Authority

### 15.1 Developer

The Developer:

- initializes Agent Squad for a target repository;
- defines or approves the task specification;
- chooses the Implementer and Reviewer kinds;
- participates in implementation discussions when needed;
- resolves `needs_human` decisions;
- authorizes material requirement changes;
- performs final integration.

The Developer is not expected to relay routine review notifications.

### 15.2 Implementer

The Implementer owns the mutable implementation worktree.

The Implementer:

- reads the task and project instructions;
- discusses unresolved implementation decisions with the Developer;
- modifies production code;
- adds or updates tests;
- runs validation;
- commits a coherent candidate;
- writes an implementation report;
- submits the candidate through Agent Squad;
- receives and evaluates review results;
- fixes valid findings;
- rebuts invalid findings with evidence;
- escalates decisions that require human authority;
- completes the run only after exact-revision approval.

### 15.3 Reviewer

The Reviewer is an independent, normally round-scoped agent launched in the isolated review worktree.

The Reviewer:

- reads the captured task and review bundle;
- examines the exact requested revision;
- examines relevant surrounding code and tests;
- treats the implementation report as an untrusted aid;
- records blocking findings and non-blocking observations;
- runs relevant validation when practical;
- writes the review artifacts inside the review worktree;
- submits the result through Agent Squad;
- does not modify tracked files.

### 15.4 No autonomous project manager

Agent Squad does not introduce a third planning or management agent in v0.4.4.

The CLI coordinates deterministic state and handoffs. The Developer retains project authority.

---

## 16. Trust and Security Model

### 16.1 Cooperative but fallible agents

Codex and Claude Code are treated as cooperative participants that may misunderstand instructions, make mistakes, duplicate an operation, or stop unexpectedly.

They are not treated as hostile processes attempting to subvert the local user account.

### 16.2 Same-user limitation

Agent Squad does not create a strong security boundary between processes running under the same OS user.

Pane identity, interactive confirmation, and agent-role checks are accidental-action controls, not protection against a malicious process with equivalent filesystem and shell access.

### 16.3 Sensitive control-plane changes

Automated Agent Squad review is not guaranteed for changes that modify the mechanism controlling the review itself.

The following are sensitive examples:

- Agent Squad's own CLI or protocol implementation;
- Agent Squad's reviewer prompt templates;
- repository-level `AGENTS.md`, `CLAUDE.md`, or equivalent files when the candidate change affects reviewer behavior;
- Herdr integration scripts used by Agent Squad.

For v0.4.4, these changes SHOULD be reviewed manually or through a previously installed stable Agent Squad version with a neutral review profile.

The first implementation of Agent Squad MUST NOT claim to have independently validated itself through the unreviewed candidate implementation.

### 16.4 No automatic permission-key injection

Agent Squad MUST NOT automatically answer approval prompts by sending arbitrary keys to an agent UI.

If a sandbox or permission model prevents required operations, the run SHOULD stop with a clear diagnostic.

---

## 17. Consuming-Repository Runtime Layout

### 17.1 Canonical control root

For each implementation worktree, the canonical Agent Squad runtime root is:

```text
<implementation-worktree>/.agent-squad/
```

Normal commands MUST NOT accept an arbitrary alternate control root.

The canonical root exists to ensure that two separately initialized state stores cannot control the same implementation worktree.

### 17.2 Suggested layout

A consuming repository SHOULD use a layout equivalent to:

```text
.agent-squad/
├── config.json
├── state.json
├── lock
├── task.md
└── runs/
    └── <run-id>/
        ├── run.json
        ├── task.md
        ├── events.jsonl
        ├── context/
        │   └── ... optional explicitly captured files ...
        ├── escalations/
        │   ├── 001-escalation.json
        │   └── 001-escalation.md
        ├── resolutions/
        │   ├── 001-resolution.json
        │   └── 001-resolution.md
        └── rounds/
            ├── 001/
            │   ├── round.json
            │   ├── request.json
            │   ├── implementation-report.md
            │   ├── response.json
            │   ├── retired-results.json
            │   ├── review.json
            │   ├── review.md
            │   ├── diagnostics/
            │   │   ├── late-results/
            │   │   │   └── <result-id>/
            │   │   ├── invalid-results/
            │   │   │   └── <diagnostic-id>/
            │   │   └── replaced-responses/
            │   │       └── <response-id>.json
            │   └── bundle/
            │       ├── input/
            │       ├── output/
            │       └── local-state.json
            └── 002/
                └── ...
```

A file MAY be absent when it is not applicable. For example:

- round 1 normally has no `response.json`;
- an approved first round may have no escalation or resolution;
- an unapplied result has not yet been archived into `bundle/`.

### 17.3 Review worktree root

Review worktrees MUST live outside the implementation worktree.

A configurable local root MAY be used, for example:

```text
<configured-review-root>/
└── <repository-id>/
    └── <run-id>/
        └── round-001/
```

The review-worktree root MUST be proven usable through preflight. It MUST NOT be assumed writable merely because both agents run under the same OS user.

### 17.4 Git exclusion

`agent-squad init` MUST add both of the following patterns to the repository's local Git exclude when they are not already present:

```gitignore
.agent-squad/
.agent-squad-review/
```

The local Git exclude is normally the Git common directory's `info/exclude`, which is shared by linked worktrees.

Agent Squad SHOULD NOT modify the consuming repository's committed `.gitignore` automatically.

### 17.5 Historical retention

Terminal runs MUST retain their authoritative artifacts under `runs/<run-id>/`.

Review worktrees and round-scoped Reviewer sessions are disposable runtime resources. Their removal MUST NOT delete the archived task, request, result, response, escalation, resolution, or event history.

---

## 18. Repository and Worktree Identity

### 18.1 Canonical implementation root

Every mutating implementation-side command MUST resolve the current Git worktree root through Git and verify that it matches the root recorded for the active run.

### 18.2 Stored identity

The run MUST record at least:

- canonical implementation-worktree path;
- canonical Git common-directory path;
- canonical per-worktree Git-directory path;
- repository identifier derived from stable local Git identity;
- branch or detached state at start;
- run ID.

A reasonable repository identifier is a digest of the canonical Git common-directory path. It is a local collision-avoidance identifier, not a portable repository identity.

### 18.3 Run ID

Run IDs MUST be collision-resistant. A UUID is sufficient.

### 18.4 One active run per worktree

Only one active run is supported per implementation worktree.

The canonical `.agent-squad/` root and a local exclusive lock MUST enforce this invariant.

Separate Git worktrees MAY each have their own active run.

### 18.5 Revalidation

Every mutating command MUST revalidate:

- the current worktree root;
- the Git common directory;
- the per-worktree Git directory;
- the active run ID;
- the expected branch or detached state when relevant.

A command MUST stop rather than operate on a different, moved, or re-associated worktree when identity cannot be established safely.

### 18.6 Terminal release

When a run becomes `completed` or `cancelled`, it MUST no longer occupy the active-run slot.

Historical artifacts MUST remain available.

A crash after the terminal state is atomically persisted but before incidental cleanup completes MUST NOT prevent a later run from starting.

---

## 19. Configuration

Repository-local configuration SHOULD remain small.

A representative shape is:

```json
{
  "schema_version": 1,
  "implementer": {
    "agent_name": "codex-main",
    "kind": "codex"
  },
  "reviewer": {
    "kind": "claude",
    "start_args": []
  },
  "base_ref": "origin/main",
  "review_worktree_root": "/Users/example/.local/share/agent-squad/worktrees",
  "max_completed_change_reviews": 4,
  "allowed_generated_paths": [
    "build/",
    "dist/",
    "coverage/",
    ".cache/"
  ]
}
```

The exact fields MAY be refined, but configuration MUST NOT become:

- a generic workflow-definition language;
- a role registry;
- a DAG;
- a project-policy archive.

Repository-specific validation commands SHOULD remain in the task specification or ordinary project instructions.

The `allowed_generated_paths` list is optional and MUST be interpreted narrowly. It does not authorize broad cleanup of arbitrary untracked content.

---

## 20. Preflight and Capability Verification

Cross-worktree permissions and agent sandbox behavior MUST be verified rather than assumed.

Agent Squad MUST provide:

```text
agent-squad doctor
```

It SHOULD provide an explicit live Reviewer preflight mode such as:

```text
agent-squad doctor --live-reviewer
```

### 20.1 Deterministic checks

`doctor` SHOULD verify:

- the current directory is inside a Git worktree;
- the canonical `.agent-squad/` root can be created or read;
- the local Git exclude contains the required Agent Squad patterns;
- the Herdr binary is available;
- the configured Implementer agent exists when required;
- the configured Reviewer kind is supported;
- the review-worktree root can be created;
- Git can create and remove a disposable detached worktree there;
- required Herdr commands are present;
- stored repository identity is internally consistent;
- the active run, if any, references existing expected paths;
- the Git object format can be determined;
- `.gitmodules`, if present, is reported as a submodule limitation warning.

### 20.2 Live Reviewer preflight

The live preflight MUST create a disposable review worktree and launch a short-lived Reviewer using the configured Reviewer profile.

The Reviewer must prove that it can:

- read a file in the review worktree;
- read the local review request;
- write a sentinel file inside `.agent-squad-review/output/`;
- write `.agent-squad-review/local-state.json`;
- access the Herdr environment required to send a result handoff.

The preflight MUST clean up disposable resources when safe.

A successful result MAY be cached against relevant local configuration and Herdr version, but the Developer MUST be able to rerun it.

This is a fixed capability check for one workflow, not a general capability-negotiation framework.

### 20.3 Control-pane checks

When a command requires direct Developer confirmation, Agent Squad MAY verify:

- a usable `/dev/tty` exists;
- the current Herdr pane is not recognized as either configured coding-agent session;
- the user can answer an interactive confirmation.

Agent Squad MUST NOT depend on a Herdr foreground-process-type field that the installed protocol does not expose.

### 20.4 Orphaned review-resource inspection

`doctor` SHOULD inspect the configured review-worktree root for:

- review worktrees with no corresponding run or round record;
- worktrees belonging to terminal rounds that were not cleaned up;
- worktrees whose `HEAD` does not match the recorded round;
- deterministic Reviewer names that appear to outlive their round.

`doctor` MUST report such resources and their paths.

It MUST NOT delete them automatically unless the Developer invokes a separate explicit cleanup path and safety checks succeed.

---

## 21. Task and Context Capture

### 21.1 Task specification

Every run MUST have a task specification approved by the Developer.

A recommended structure is:

```markdown
# Task

## Objective

## Requirements

## Constraints

## Acceptance Criteria

## Non-goals
```

### 21.2 Immutable run copy

At `start`, Agent Squad MUST copy the task specification into:

```text
.agent-squad/runs/<run-id>/task.md
```

The captured file is authoritative for the run and MUST NOT be silently replaced.

If the objective, requirements, constraints, or acceptance criteria materially change, the normal action is to cancel the run and start a new one.

### 21.3 Optional explicit context

The Developer MAY explicitly provide additional context files at `start`.

Agent Squad MAY copy them into the run directory and record their SHA-256 hashes.

Agent Squad MUST NOT automatically snapshot:

- every repository policy document;
- full GitHub issue history;
- all agent instruction files;
- every architecture document.

If information is important to review correctness, it should be stated in the task or explicitly captured.

### 21.4 Project instructions

The Reviewer MAY read project instructions present in the requested Git revision.

Changes to agent instructions or Agent Squad control files are subject to the sensitive-control-plane limitation in Section 16.3.

### 21.5 Developer resolutions are not silent task rewrites

A Developer resolution MAY settle:

- an interpretation;
- an architecture choice;
- a compatibility choice;
- a policy question;
- a bounded risk-acceptance question;
- an unresolved review disagreement.

A resolution MUST NOT silently redefine the task's objective or material acceptance criteria.

When a proposed resolution materially changes the captured task, the run SHOULD be cancelled and restarted with a new task snapshot.

---

## 22. Git Revision Model

### 22.1 Exact object IDs

Every request, result, response, round record, and approval MUST store the full Git object ID returned by Git.

The validator MUST support the repository's configured object format rather than assuming SHA-1 only.

Typical lengths are:

- 40 lowercase hexadecimal characters for SHA-1 repositories;
- 64 lowercase hexadecimal characters for SHA-256 repositories.

Abbreviated hashes MAY be displayed but MUST NOT be authoritative.

### 22.2 Fixed base

At run start, Agent Squad resolves the configured base reference to an exact `base_oid`.

The resolved `base_oid` is fixed for the run.

Agent Squad MUST NOT silently move the base when a branch reference advances.

### 22.3 Base ancestry requirement

Before every review submission, Agent Squad MUST run an equivalent of:

```bash
git merge-base --is-ancestor <base_oid> <head_oid>
```

The check MUST succeed.

If `base_oid` is not an ancestor of `head_oid`, submission MUST stop with guidance equivalent to:

```text
The fixed review base is no longer an ancestor of the candidate head.
Cancel this run and start a new run with a newly resolved base.
```

Agent Squad MUST NOT repair the history automatically through reset, merge, or rebase.

### 22.4 Reviewed scope

The reviewed change scope is the tree difference from the fixed `base_oid` to the round's `head_oid`.

Conceptually:

```bash
git diff <base_oid>..<head_oid>
```

Merging upstream into the implementation branch may widen this scope by introducing additional commits and tree changes.

If the widened scope is no longer appropriate for the task, the Developer SHOULD cancel and restart with a new base rather than silently reinterpret the run.

### 22.5 Candidate head

Each review round records an exact `head_oid`.

Submission-mode consistency rules are defined in Sections 26.4 and 28.1.

### 22.6 Approval scope

Approval applies only to the exact tuple:

```text
run_id
round
base_oid
head_oid
result_id
```

A newer commit invalidates the earlier approval for completion purposes.

### 22.7 Implementation-tree cleanliness

Before ordinary `submit`, the implementation worktree MUST have no uncommitted tracked changes.

Unexpected untracked files SHOULD block submission unless they are:

- Agent Squad runtime files excluded by Git;
- project-configured generated output;
- explicitly accepted by the Developer for the run.

Agent Squad MUST NOT delete unrelated untracked files to satisfy this check.

### 22.8 Sensitive instruction-file warning

When the candidate diff includes known agent-control or instruction files, `submit` SHOULD issue a non-blocking warning.

Examples include:

```text
AGENTS.md
CLAUDE.md
agent prompt templates
Agent Squad integration fragments
Herdr integration scripts
```

The warning does not create a general control-plane classifier and does not automatically block submission.

### 22.9 Submodules

Repositories containing `.gitmodules` are supported only with a documented limitation in v0.4.4.

Agent Squad MUST NOT automatically run a recursive submodule update that may access the network or change external checkouts without explicit project authorization.

`doctor` MUST warn that a detached review worktree may not contain initialized submodule content.

The consuming project is responsible for preparing required submodules or defining a safe project-specific preflight.

### 22.10 No destructive Git operations

Agent Squad MUST NOT automatically:

- reset;
- rebase;
- merge;
- push;
- switch the implementation branch;
- delete branches;
- run broad destructive cleanup;
- initialize or update submodules through network access without explicit authorization.

---

## 23. Review Worktree and Self-contained Bundle

### 23.1 One detached worktree per round

For each submitted review round, Agent Squad MUST create a dedicated detached Git worktree at the exact `head_oid`.

### 23.2 Bundle layout

Before the Reviewer starts, Agent Squad MUST place a self-contained bundle inside the review worktree.

The bundle root is:

```text
<review-worktree>/.agent-squad-review/
```

Suggested layout:

```text
.agent-squad-review/
├── input/
│   ├── request.json
│   ├── task.md
│   ├── implementation-report.md
│   ├── previous-review.json
│   ├── previous-response.json
│   ├── resolutions/
│   │   ├── 001-resolution.json
│   │   └── 001-resolution.md
│   └── context/
│       └── ... optional captured files ...
├── output/
│   ├── review.json
│   └── review.md
├── retired-results.json
└── local-state.json
```

Files that are not applicable MAY be absent. When the run has recorded Developer resolutions, every resolution JSON and companion Markdown MUST be present in the bundle in ascending `created_at` order.

`retired-results.json` is present only after recovery retires a marker-confirmed result identity. The implementation-owned round copy is authoritative. The review-bundle copy is an advisory mirror that allows `review-submit` to reject contradictory reuse early without requiring access to `.agent-squad/`. Implementation-side decisions MUST NOT depend on that advisory copy being present or valid.

The bundle MUST contain every protocol artifact the round-scoped Reviewer needs without requiring write access to the implementation worktree's `.agent-squad/` directory.

### 23.3 Path convention

Every artifact path stored in `request.json` MUST be relative to the review bundle root unless the field is explicitly documented as an absolute runtime path.

For example:

```text
input/task.md
input/implementation-report.md
input/resolutions/001-resolution.json
output/review.json
```

This convention MUST remain valid after the complete bundle is archived under:

```text
.agent-squad/runs/<run-id>/rounds/<round>/bundle/
```

### 23.4 Permitted Reviewer writes

The Reviewer MAY write:

- `.agent-squad-review/output/**`;
- `.agent-squad-review/local-state.json`;
- known ignored validation output created by tests or builds.

The Reviewer MUST NOT modify any tracked file.

Unexpected non-ignored files outside `.agent-squad-review/` SHOULD invalidate the result unless the project explicitly permits them.

### 23.5 Pre-review integrity

Before launch, Agent Squad MUST verify:

- the review worktree exists;
- its `HEAD` equals `head_oid`;
- tracked content is clean;
- the bundle input matches the authoritative round artifacts;
- required Developer resolution files and hashes match;
- `.agent-squad-review/` is Git-excluded.

### 23.6 Reviewer-local submission marker

`local-state.json` is a Reviewer-side submission marker. It is not authoritative implementation-side workflow state.

After a valid `review-submit`, it MUST record data equivalent to:

```json
{
  "schema_version": 1,
  "request_id": "550e8400-e29b-41d4-a716-446655440001",
  "result_id": "550e8400-e29b-41d4-a716-446655440002",
  "status": "result_submitted",
  "review_json_path": "output/review.json",
  "review_sha256": "example-sha256",
  "submitted_at": "2026-08-23T03:00:00Z"
}
```

The marker MUST be written atomically before the result notification is attempted.

A marker-confirmed output can therefore be discovered even when the Herdr result notification is lost.

### 23.7 Post-review integrity

Before applying a result, Agent Squad MUST verify:

- the review worktree `HEAD` still equals `head_oid`;
- no tracked file has been modified;
- the request, result, and local marker identify the same run, round, request, base, and head;
- the marker's result ID matches the result;
- the marker's recorded digest matches `output/review.json`;
- the output paths are the expected paths for the active round;
- every referenced Developer resolution has the expected digest.

### 23.8 Bundle archive

After a result is validly applied, Agent Squad MUST archive a complete copy of the bundle under the authoritative round directory.

The archived bundle preserves:

- bundle-relative path semantics;
- the exact task and resolution inputs reviewed;
- the output reviewed and applied;
- the Reviewer-local submission marker.

The transient `output/.review-submit.lock` file coordinates one local process;
it is not review evidence and MUST NOT be included in the archive.

If application is retried after the complete bundle archive was written but
before the authoritative state transition committed, the existing archive is
the provisional snapshot. The retry MUST revalidate every authoritative
bundle entry against current evidence, retain any optional advisory
`retired-results.json` snapshot already present in that archive, and derive
the eventual authoritative manifest from the verified archive. A mutable live
advisory mirror MUST NOT invalidate or redefine the provisional archive.

If that provisional archive instead belongs to a result identity that recovery
later retired, `apply-review` MUST verify the archived `review.json` result ID
and digest against the authoritative retirement ledger before replacing it.
It MUST first quarantine the complete provisional archive and any round-root
apply artifacts under
`diagnostics/retired-apply-attempts/<retired-result-id>/`. Quarantine MUST stay
inside normal implementation-owned round directories and MUST be resumable if
moving the evidence is interrupted. A mismatch without an exact authoritative
retirement binding remains an integrity error.

Convenience copies of `review.json`, `review.md`, and other key artifacts MAY also exist at the round root.

### 23.9 Reviewer session lifecycle

The default Reviewer is round-scoped.

A deterministic name SHOULD be derived from run and round, for example:

```text
agent-squad-<run-short>-r001-reviewer
```

On retry, Agent Squad SHOULD adopt an existing worktree workspace or named Reviewer for that exact round rather than create a second logical reviewer.

Reviewer conversational continuity across rounds is not required. The task, previous review, previous response, and Developer resolutions provide continuity.

---

## 24. Authoritative Local State

### 24.1 State file

The authoritative current state is:

```text
.agent-squad/state.json
```

A representative shape is:

```json
{
  "schema_version": 1,
  "updated_at": "2026-08-23T03:00:00Z",
  "active_run_id": "550e8400-e29b-41d4-a716-446655440000",
  "phase": "reviewing",
  "implementation_root": "/absolute/path/to/worktree",
  "git_common_dir": "/absolute/path/to/common/git/dir",
  "worktree_git_dir": "/absolute/path/to/worktree/git/dir",
  "repository_id": "local-repository-id",
  "base_oid": "0123456789abcdef0123456789abcdef01234567",
  "current_round": 2,
  "current_head_oid": "89abcdef0123456789abcdef0123456789abcdef",
  "approved_head_oid": null,
  "active_escalation_id": null,
  "active_round": {
    "round": 2,
    "status": "reviewing",
    "mode": "new_revision",
    "request_id": "550e8400-e29b-41d4-a716-446655440010",
    "result_id": null,
    "review_worktree": "/absolute/path/to/review/worktree"
  },
  "review_budget": {
    "original_limit": 4,
    "additional_rounds_granted": 1,
    "effective_limit": 5,
    "completed_change_reviews": 2
  },
  "handoff": {
    "kind": "review_request",
    "round": 2,
    "status": "sent",
    "target": "agent-squad-550e8400-r002-reviewer",
    "last_error": null,
    "updated_at": "2026-08-23T03:00:00Z"
  }
}
```

Equivalent information MAY use different field names, but the semantic content MUST be preserved.

### 24.2 Run record

Each run MUST maintain:

```text
.agent-squad/runs/<run-id>/run.json
```

It records stable and terminal run metadata, including:

- run identity;
- task path and digest;
- repository identity;
- fixed base;
- assigned roles;
- start time;
- final phase and finish time when terminal.

### 24.3 Round record

Each round MUST maintain:

```text
.agent-squad/runs/<run-id>/rounds/<round>/round.json
```

It is the authoritative historical status record for that round.

It SHOULD record:

- round number;
- mode;
- request ID;
- base and head OIDs;
- review worktree path;
- authoritative round status;
- result ID and recorded classification when a marker-confirmed result is classified as `applied`, `stale`, or `invalid`;
- diagnostic ID when an invalid artifact has no trustworthy result ID;
- timestamps;
- stale, invalid, or supersede reason when applicable.

When a marker-confirmed result is classified as `stale` or `invalid`, `round.json` MUST retain any syntactically valid `result_id` that was presented together with the recorded classification. If no trustworthy `result_id` can be extracted, the round MUST retain a `diagnostic_id` instead; such an artifact remains recoverable through its diagnostic path but cannot be deduplicated by result ID.

### 24.4 Atomic writes

`state.json`, `run.json`, `round.json`, resolution records, escalation records, and Reviewer-local markers MUST use temporary-file writes, flush, and atomic replacement.

### 24.5 Local lock

All mutating implementation-side commands MUST acquire an exclusive local file lock.

The lock MAY be held across a short Herdr prompt operation to prevent two normal local CLI processes from dispatching the same handoff concurrently.

The OS releases the lock if the process exits unexpectedly.

This local lock is sufficient for the v0.4.4 single-machine model. Do not add distributed leases.

### 24.6 Per-run event log

Every run MUST maintain exactly one append-only event log at:

```text
.agent-squad/runs/<run-id>/events.jsonl
```

There MUST NOT be a competing top-level authoritative event log.

Every event MUST contain an RFC 3339 UTC timestamp, for example:

```json
{
  "timestamp": "2026-08-23T03:00:00Z",
  "event": "review_applied",
  "round": 2,
  "result_id": "550e8400-e29b-41d4-a716-446655440002",
  "verdict": "approved"
}
```

`state.json` remains authoritative for current state. The event log is not an event-sourcing system and MUST NOT need to be replayed to determine the effective review budget or active phase.

### 24.7 Reviewer-local state boundary

`.agent-squad-review/local-state.json` is authoritative only for the fact that a Reviewer-side result submission was locally validated and marked.

It does not directly change the implementation-side phase or round status.

Until `apply-review` succeeds:

- implementation-side round status remains `reviewing`;
- `status` may report that a marker-confirmed result is ready;
- the result has not yet changed the authoritative run phase.

---

## 25. Run and Round State Model

### 25.1 Run phases

Authoritative run phases are:

```text
implementing
reviewing
approved
needs_human
completed
cancelled
```

### 25.2 Terminal phases

Terminal phases are:

```text
completed
cancelled
```

### 25.3 Normal transitions

```text
start
  |
  v
implementing
  |
  | submit
  v
reviewing
  |
  +-----------------------------+------------------------------+
  |                             |                              |
  | valid approved result       | valid changes_requested      | valid needs_human result
  |                             | with budget remaining         | or exhausted review budget
  v                             v                              v
approved                    implementing                  needs_human
  |
  | complete
  v
completed
```

### 25.4 New candidate after approval

A routine committed change after approval MUST NOT require an artificial human escalation.

When phase is `approved`, `submit` MAY create a new `new_revision` round if:

- the current implementation `HEAD` differs from `approved_head_oid`;
- the normal cleanliness and base-ancestry checks pass.

Before the new round is persisted:

```text
approved_head_oid = null
phase = reviewing
```

The earlier approval remains in historical round artifacts but is no longer the current completion authority.

### 25.5 Escalation

The following transitions are supported:

```text
implementing ──→ needs_human
reviewing    ──→ needs_human
approved     ──→ needs_human
```

An escalation MUST record an escalation artifact and set `active_escalation_id` to that artifact's ID.

A valid Reviewer `needs_human` verdict and review-budget exhaustion are escalation sources. In those cases, `apply-review` creates the escalation artifact automatically.

The Developer MAY resume into `implementing` only after recording a Developer resolution linked to the active escalation.

### 25.6 Cancellation

The following transitions are supported:

```text
implementing ──→ cancelled
reviewing    ──→ cancelled
needs_human  ──→ cancelled
approved     ──→ cancelled
```

When cancellation occurs from `reviewing`, the active round MUST first become `superseded` with `run_cancelled` and the cancellation reason recorded as its cause.

When cancellation occurs from `approved`, `approved_head_oid` MUST be cleared.

Cancellation of an active review SHOULD send a best-effort cancellation notice to the Reviewer.

Notification failure MUST NOT prevent the run from becoming `cancelled`.

### 25.7 Authoritative round statuses

Each `round.json` MUST use one of:

```text
prepared
reviewing
applied
superseded
stale
invalid
```

`result_submitted` is not an implementation-side authoritative round status. It is a Reviewer-local marker state recorded in `.agent-squad-review/local-state.json`.

A round remains authoritatively `reviewing` until `apply-review` classifies and records the result.

### 25.8 Review budget

The configured review limit applies to **valid applied `changes_requested` results**.

It does not count:

- transport retries;
- invalid artifacts;
- stale results;
- superseded rounds;
- duplicate notifications;
- approved results.

Recommended original limit:

```text
4
```

Authoritative budget state MUST be stored in `state.json`, including:

```text
original_limit
additional_rounds_granted
effective_limit
completed_change_reviews
```

When an applied `changes_requested` result reaches the effective limit, the run enters `needs_human`.

A Developer MAY grant a small extension through `resume`.

The extension MUST be:

- explicitly stated;
- recorded in the Developer resolution;
- added to authoritative state;
- recorded in the event log.

If the run needs another review after budget exhaustion, `resume` MUST reject a resolution that neither grants additional rounds nor cancels/restarts the run.

---

## 26. Protocol Lifecycle

### 26.1 Initialize repository

The Developer runs:

```text
agent-squad init
```

It MUST:

- verify the current Git worktree;
- create or validate the canonical `.agent-squad/` root;
- add `.agent-squad/` and `.agent-squad-review/` to local Git exclude;
- create or validate configuration;
- avoid overwriting existing valid configuration without explicit confirmation;
- preserve unrelated untracked files;
- record no active run merely because initialization succeeded.

### 26.2 Start run

The Developer runs `agent-squad start` with:

- task specification;
- Implementer agent identity;
- Reviewer kind;
- base reference;
- optional explicit context files.

It MUST:

1. ensure no active run exists in the implementation worktree;
2. resolve and record canonical repository identity;
3. resolve the configured base to an exact `base_oid`;
4. copy the task and optional context;
5. create `run.json` and per-run `events.jsonl`;
6. initialize authoritative review-budget state;
7. set phase to `implementing`;
8. write a timestamped `run_started` event.

### 26.3 Interactive implementation

The Implementer works interactively with the Developer.

Agent Squad does not script planning, design discussion, clarification, or coding.

### 26.4 Submit candidate

A conceptual command is:

```text
agent-squad submit
  --report <implementation-report.md>
  [--response <response.json>]
  --mode <new_revision|reconsideration>
```

The Implementer runs it only after the candidate and required artifacts are ready.

Under the local lock, `submit` MUST:

1. verify the current role and permitted phase;
2. verify repository and worktree identity;
3. verify implementation-tree cleanliness;
4. resolve the exact current `head_oid`;
5. verify `base_oid` is an ancestor of `head_oid`;
6. validate submission-mode and head-OID consistency;
7. ingest and validate the implementation report;
8. ingest and validate a required previous-round response as staged submission data, when applicable;
9. refuse dispatch when a response contains `needs_human`, with guidance to use `agent-squad escalate`;
10. issue a non-blocking warning for sensitive instruction or control-plane changes;
11. create the next round directory and staged `round.json`;
12. create the detached review worktree at `head_oid`;
13. create the self-contained review bundle;
14. copy every Developer resolution recorded for the run into the bundle, in ascending `created_at` order;
15. prepare the authoritative `request.json`;
16. set the staged round status to `reviewing`;
17. set the staged run phase to `reviewing`;
18. clear any previously current `approved_head_oid` in staged state;
19. record the request handoff as `pending` in staged state;
20. commit any required prior-round response archival or replacement and persist the request, state, and round metadata as the successful submission commit point before notification;
21. launch or adopt the deterministic round-scoped Reviewer;
22. send the short review-request prompt;
23. record handoff success or failure without discarding the request or round.

Steps 8–19 prepare submission data under the local lock. Canonical prior-round response paths MUST NOT be modified before step 20. If any earlier step fails, staged response data MUST be discarded, the previous authoritative response MUST remain unchanged, and no new authoritative round may exist.

### 26.5 Submission-mode and OID rules

For this section, `previous_reviewed_head_oid` means the `head_oid` of the most recent round whose result reached authoritative status `applied`. Intervening `superseded`, `stale`, or `invalid` rounds are ignored for this definition.

#### First review round

Round 1 MUST use:

```text
mode = new_revision
head_oid != base_oid
```

A no-change first round MUST be rejected.

#### After a valid applied `changes_requested` result

`new_revision` MUST satisfy:

```text
head_oid != previous_reviewed_head_oid
```

This detects cases where an agent claims to have fixed findings but:

- did not commit;
- committed on the wrong branch;
- returned to the same revision unintentionally.

`reconsideration` MUST satisfy:

```text
head_oid == previous_reviewed_head_oid
```

It also requires a valid evidence-based response. Every response disposition in reconsideration mode MUST be `rejected` with concrete evidence. A `fixed` disposition requires a changed committed revision and therefore `mode = new_revision`. A finding already satisfied by the existing reviewed revision is represented as `rejected` with evidence pointing to that existing code or test.

#### After `superseded`, `stale`, or `invalid`

A same-head `new_revision` submission MAY be accepted because the previous round did not produce a valid applied change-review result.

This is a specific recovery exception. It is not a third general submission mode.

#### From `approved`

A new submission MUST use:

```text
mode = new_revision
head_oid != approved_head_oid
```

### 26.6 Response intake

A `response.json` is REQUIRED for any submission following an applied `changes_requested` result.

`submit` MUST validate that:

- every blocking finding in the previous review has exactly one response;
- no unknown finding ID is present;
- `fixed` includes rationale and verification;
- `rejected` includes concrete evidence;
- `needs_human` identifies the decision that requires Developer authority;
- `review_result_id`, reviewed head, run, and round match the previous review;
- in `reconsideration` mode, every disposition is `rejected` with concrete evidence;
- in `new_revision` mode, `fixed` and `rejected` are permitted, while any `needs_human` disposition requires escalation rather than dispatch.

The validated response belongs to the previous reviewed round and MUST be archived at:

```text
runs/<run-id>/rounds/<previous-round>/response.json
```

During `submit`, response intake before the successful submission commit point is staging only. Agent Squad MUST validate and stage the candidate response without replacing canonical `response.json` or writing `diagnostics/replaced-responses/` until Section 26.4 step 20.

If no response is already present, the validated response becomes the authoritative response for that reviewed round only when the new review round is successfully persisted.

The same commit-point rule applies to `escalate --response`. Escalation-time response intake is staging only until Section 26.16 step 10 successfully persists the escalation, response archival, affected round metadata, and run state. A failed pre-commit escalation MUST leave the previous authoritative response unchanged. If a crash leaves a response file that no persisted escalation references, recovery MUST treat it as provisional and permit a corrected `escalate --response` retry.

An escalation-time response successfully committed by `escalate --response` is provisional for the later continuation path because its `needs_human` disposition records why the run stopped. After the Developer records a resolution, the next `submit` MAY replace that canonical response with a new complete submit-time response for the same review result.

That replacement MUST:

- use a new `response_id`;
- set `supersedes_response_id` to the escalation-time response ID;
- list in `resolution_ids` the Developer resolution or resolutions relied on, including the resolution linked to the escalation created from that response;
- replace every former `needs_human` disposition with `fixed` or `rejected`, subject to the submission-mode rules in Section 26.5;
- explain in each affected rationale how the disposition follows the recorded Developer resolution.

Before replacing the canonical path, Agent Squad MUST preserve the escalation-time response at:

```text
runs/<run-id>/rounds/<previous-round>/diagnostics/replaced-responses/<response-id>.json
```

The submit-time response then becomes authoritative at `response.json`. Repeating the same replacement with the same response ID and digest is idempotent. A different rewrite that does not satisfy this explicit replacement rule MUST be rejected after a successfully persisted new round references the authoritative response.

If a submission fails before that commit point, the staged response is not frozen by the no-rewrite guard. The Implementer MAY correct it on retry. If a crash leaves a partially written response but no persisted new round references it, recovery MUST treat the file as provisional, preserve it as a diagnostic copy when useful, and permit a corrected retry.

The authoritative response MUST also be copied into the next review bundle as:

```text
input/previous-response.json
```

### 26.7 Review

The Reviewer:

1. reads the self-contained bundle;
2. verifies the requested revision and bundle identity;
3. reads every Developer resolution recorded for the run;
4. reviews independently;
5. runs relevant validation when practical;
6. writes `output/review.json` and `output/review.md`;
7. runs `agent-squad review-submit`.

### 26.8 Reviewer-side submission

`review-submit` MUST:

- run from the expected review worktree;
- validate local worktree and request identity;
- fully validate review artifact structure and semantic invariants;
- verify no tracked file has been modified;
- verify resolution inputs and digests;
- write or reuse a stable `result_id`;
- write `local-state.json` atomically before notification;
- send a short, verdict-neutral result prompt to the Implementer;
- tolerate safe retry without creating a second logical result.

The configured Reviewer SHOULD run `review-submit`.

If the Reviewer session is still alive, it MAY rerun `review-submit` to resend a lost notification, provided the same request, result ID, and review digest are retained.

The Developer MAY also rerun `review-submit` from the review worktree solely to resend a marker-confirmed existing result. The Developer MUST NOT rewrite the review content or impersonate a new Reviewer result.

`review-submit` MUST NOT require write access to the implementation worktree's control root.

### 26.9 Discover unapplied result

While phase is `reviewing`, implementation-side `status` MUST inspect the active review worktree's expected:

```text
.agent-squad-review/output/review.json
.agent-squad-review/local-state.json
```

If a valid marker-confirmed result exists but has not been applied, `status` MUST report:

- run and round;
- result ID;
- verdict;
- result path;
- that the result is not yet authoritative;
- the exact `agent-squad apply-review --result-id <result-id>` command.

This probe prevents a lost Herdr result prompt from leaving the run silently stalled.

### 26.10 Apply review result

A conceptual command is:

```text
agent-squad apply-review [--result-id <result-id>]
```

It SHOULD locate the expected output for the active round without requiring the user to retype an arbitrary artifact path. A result notification or recovery command SHOULD provide `--result-id` when available.

Under the implementation-side lock, it MUST:

1. verify repository identity and resolve the presented result ID from `--result-id` or the expected current-round marker;
2. search authoritative round history for that result ID and, if it was already recorded with classification `applied`, `stale`, or `invalid`, return the previously recorded outcome or classification before enforcing active-phase or implementation-HEAD requirements;
3. verify the run is in the permitted active phase and identify the active request and expected review worktree;
4. require a valid Reviewer-local submission marker;
5. verify that the presented result ID equals the active marker's result ID; if it does not, refuse without modifying any round status, phase, budget, event effects, or notifications;
6. read the expected `review.json`;
7. rerun full structural validation;
8. rerun all semantic verdict and finding validation;
9. verify run, round, request ID, result ID, base OID, and head OID;
10. verify the marker's review digest;
11. resolve the current implementation `HEAD` and verify that it equals the active round's `head_oid`; otherwise classify the result as stale under Section 27.4;
12. verify review-worktree `HEAD`;
13. verify no tracked file has been modified;
14. verify every Developer resolution input and digest;
15. archive the complete review bundle;
16. copy human-readable convenience artifacts into the round directory;
17. classify the result as applied, stale, or invalid;
18. apply the deterministic run-state transition;
19. persist state and round metadata atomically;
20. write a timestamped event;
21. notify the Implementer only when useful and not already operating in that session.

The historical lookup in step 2 MUST work from authoritative round records even when the original review worktree no longer exists. For a previously `stale` or `invalid` result, Agent Squad returns the recorded classification and diagnostic location without repeating any transition. The refusal in step 5 is not an invalidation of the active round; Section 27.5 applies only after the presented result ID matches the active round's marker.

Implementation-side validation MUST NOT trust that Reviewer-side `review-submit` validated correctly.

### 26.11 Changes requested

When a valid result is `changes_requested` and budget remains:

- round status becomes `applied`;
- `completed_change_reviews` increments;
- phase becomes `implementing`;
- the Implementer evaluates every blocking finding;
- another submission requires the response artifact rules in Section 26.6.

When the valid result reaches the effective review budget:

- round status becomes `applied`;
- `completed_change_reviews` increments;
- `apply-review` creates escalation JSON and Markdown artifacts with reason `review_budget_exhausted`, the source request and applied result IDs, the review summary, and all blocking finding IDs;
- `active_escalation_id` is set to the generated escalation ID;
- phase becomes `needs_human`.

### 26.12 Needs-human result

When a fully validated review result has verdict `needs_human`, `apply-review` MUST:

1. set the round status to `applied`;
2. leave `completed_change_reviews` unchanged;
3. create escalation JSON and Markdown artifacts from the review summary and related finding IDs;
4. record machine-readable reason `reviewer_needs_human` and the source request and result IDs;
5. set `active_escalation_id` to the generated escalation ID;
6. set phase to `needs_human`;
7. persist the escalation, round, run state, and timestamped event atomically with the applied transition.

The later Developer resolution MUST reference this auto-created escalation ID.

### 26.13 Same-SHA reconsideration

The Implementer MAY reject one or more findings with concrete evidence without changing code.

A reconsideration:

- uses the same `head_oid`;
- uses `mode = reconsideration`;
- creates a new numbered round;
- includes the previous review and response;
- includes every Developer resolution recorded for the run;
- does not require a meaningless commit.

If the Reviewer maintains the same blocking finding after considering the rebuttal and repository evidence cannot resolve the disagreement, the Reviewer SHOULD return `needs_human`.

### 26.14 Approval

A valid `approved` result sets:

```text
phase = approved
approved_head_oid = reviewed head_oid
```

The round status becomes `applied`.

### 26.15 Complete

The Implementer or Developer runs:

```text
agent-squad complete
```

It MUST verify:

- phase is `approved`;
- current implementation `HEAD` equals `approved_head_oid`;
- the implementation worktree has no uncommitted tracked changes;
- unexpected untracked files satisfy the same policy as Section 22.7;
- the approved result and request are archived.

Equality between current implementation `HEAD` and `approved_head_oid` is the complete concrete revision-authority check. No separate search for a vaguely defined "newer unreviewed commit" is required.

It then sets phase to `completed` and releases the active-run slot.

It MUST NOT merge, push, deploy, or delete branches.

### 26.16 Escalate

Agent Squad MUST provide:

```text
agent-squad escalate
```

It MAY accept:

```text
--note <decision-needed.md>
--response <response.json>
```

It is available from `implementing`, `reviewing`, or `approved`.

It MUST:

1. validate the active run;
2. ingest the escalation note and optional response as staged escalation data;
3. when `--response` is provided, validate it under Section 26.6 and stage it for archival at `runs/<run-id>/rounds/<review-round>/response.json`; if a successfully persisted escalation already references a response, retrying with the same response ID and digest is idempotent, while a different response MUST NOT overwrite it through `escalate`; any later replacement occurs only through `submit` after a Developer resolution under Section 26.6;
4. generate the escalation ID and prepare the artifact data, including the current head, round, related finding IDs, timestamp, and prior phase;
5. when invoked from `reviewing`, stage the active round as `superseded` with the generated escalation ID as its reason;
6. when invoked from `approved`, stage the previous approved head in the escalation artifact and stage clearing `approved_head_oid`;
7. prepare escalation JSON and Markdown artifacts;
8. stage `active_escalation_id` with the generated escalation ID;
9. stage phase `needs_human`;
10. commit any staged escalation-time response archival and persist the escalation artifacts, state, and affected round metadata as the successful escalation commit point;
11. write a timestamped event;
12. send a best-effort pause notice to an active Reviewer when relevant.

Steps 2–9 prepare escalation data under the local lock. Canonical `response.json`, escalation artifacts, round metadata, and run state MUST NOT become authoritative before step 10. If an earlier step fails, staged data MUST be discarded and the prior authoritative state MUST remain unchanged. A response file left by a crash but not referenced by a successfully persisted escalation is provisional under Section 26.6 and MAY be corrected on retry. The no-overwrite rule in step 3 applies only after a persisted escalation references that response.

If `submit` sees a `needs_human` response disposition, it MUST NOT create another review round. It MUST direct the caller to `agent-squad escalate`.

### 26.17 Developer resolution and resume

The Developer resumes through a command conceptually equivalent to:

```text
agent-squad resume
  --resolution <resolution.md>
  [--applies-to-finding <id> ...]
  [--extend-rounds <positive-integer>]
```

`resume` MUST:

1. require phase `needs_human`;
2. require a valid `active_escalation_id` belonging to the run;
3. ingest the Developer-authored resolution Markdown;
4. create a machine-readable resolution record linked through `resolves_escalation_id`;
5. identify the finding IDs or decided question the resolution addresses;
6. record a SHA-256 digest of the Markdown;
7. record an RFC 3339 UTC `created_at` that is later than every earlier resolution timestamp in the run;
8. record any explicit review-budget extension in authoritative state;
9. write a timestamped event;
10. clear `active_escalation_id`;
11. set phase to `implementing`.

Every Developer resolution recorded for the run MUST be copied unconditionally into every later review bundle, in ascending `created_at` order. The Reviewer determines relevance. If multiple resolutions address the same question, the later `created_at` is authoritative.

A later Reviewer MUST treat the recorded Developer decision as authoritative for the specific decided question.

The Reviewer MUST still verify that the implementation complies with the resolution.

The Reviewer MUST NOT repeatedly block on the already-decided question merely because it would have chosen a different option.

If the resolution materially changes the captured task, `resume` SHOULD refuse and instruct the Developer to cancel and restart.

### 26.18 Cancel

The Developer runs:

```text
agent-squad cancel
```

It MUST:

- preserve all authoritative artifacts;
- when invoked from `reviewing`, mark the active round `superseded` with `run_cancelled` and the cancellation reason recorded before the terminal transition;
- when invoked from `approved`, clear `approved_head_oid`;
- clear `active_escalation_id` while retaining the historical escalation artifact, if any;
- set phase to `cancelled`;
- release the active-run slot;
- write a timestamped event;
- send a best-effort cancellation notice to an active Reviewer.

Notification or cleanup failure MUST NOT undo cancellation.

---

## 27. Superseding, Stale, and Invalid Results

### 27.1 Supersede active review

The Implementer MUST NOT remain trapped in `reviewing` when the active review is no longer relevant.

Agent Squad MUST provide:

```text
agent-squad supersede --reason <text>
```

It:

- marks the active round `superseded`;
- records the reason and timestamp;
- returns phase to `implementing`;
- sends a best-effort supersede notice to the Reviewer.

### 27.2 Late result for superseded round

A late result for a superseded round MUST be archived for diagnostics under:

```text
runs/<run-id>/rounds/<round>/diagnostics/late-results/<result-id>/
```

The diagnostic directory SHOULD retain the review JSON, Markdown companion when present, and Reviewer-local marker.

The implementation-side cleanup path for a superseded round is the designated archival actor. Before removing the review bundle or detached worktree, cleanup MUST probe for marker-confirmed output and archive any late result under the path above.

It MUST NOT alter active state or approve a revision.

### 27.3 Mismatched result

This classification applies only to marker-confirmed output presented as the active round's own result after the presented `result_id` matches the active Reviewer-local marker.

Such a result is invalid or stale when its:

- run;
- round;
- request ID;
- base OID;
- head OID;
- expected output location

does not match the active request.

A result ID already recorded on a historical `applied`, `stale`, or `invalid` round is handled by the early lookup in Section 26.10. A presented result ID that matches neither authoritative round history nor the active round's marker MUST be refused without changing any round status, run phase, review budget, event effects, or notifications.

A mismatched result MUST NOT approve any revision.

### 27.4 Implementation `HEAD` advances during review

If the implementation `HEAD` changes before `apply-review` and the round was not first superseded:

- retain and archive the result as stale;
- set round status to `stale`;
- set phase deterministically to `implementing`;
- clear current approval authority;
- record both requested and observed head OIDs;
- require a new submission.

The result MUST NOT approve the newer implementation revision.

### 27.5 Invalid result recovery

This recovery applies only to the active round's own marker-confirmed output after the presented result ID has matched the active marker. An unknown, foreign, or historical result ID MUST NOT invalidate the current active round.

When that active result is structurally invalid, semantically invalid, identity-mismatched after marker matching, or produced after tracked-file modification:

- retain diagnostic copies under `runs/<run-id>/rounds/<round>/diagnostics/invalid-results/<diagnostic-id>/`;
- preserve the raw review JSON, Markdown companion when present, Reviewer-local marker when present, and a machine-readable validation-error summary when practical;
- set round status to `invalid`;
- return phase to `implementing` unless the failure indicates a safety concern requiring `needs_human`;
- do not consume review budget;
- allow a same-head `new_revision` recovery submission.

The common default is `implementing`. `needs_human` is reserved for cases where automatic continuation would be unsafe or ambiguous.

---

## 28. Artifact Contracts

### 28.1 General rules

Every machine-readable artifact MUST contain:

- `schema_version`;
- appropriate stable IDs;
- an RFC 3339 UTC timestamp appropriate to the artifact;
- explicit run and round identity where applicable.

Unknown critical fields or invalid types MUST be rejected through explicit standard-library validators.

The reference implementation does not require a general JSON Schema engine.

All paths in `request.json` are relative to the review bundle root unless explicitly documented otherwise.

### 28.2 Review request

Minimum shape:

```json
{
  "schema_version": 1,
  "created_at": "2026-08-23T03:00:00Z",
  "request_id": "550e8400-e29b-41d4-a716-446655440001",
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "round": 2,
  "mode": "new_revision",
  "base_oid": "0123456789abcdef0123456789abcdef01234567",
  "head_oid": "89abcdef0123456789abcdef0123456789abcdef",
  "task_path": "input/task.md",
  "implementation_report_path": "input/implementation-report.md",
  "previous_review_path": "input/previous-review.json",
  "previous_response_path": "input/previous-response.json",
  "resolution_paths": [
    "input/resolutions/001-resolution.json"
  ],
  "review_output_path": "output/review.json",
  "review_markdown_path": "output/review.md",
  "implementer_agent": "codex-main",
  "reviewer_kind": "claude"
}
```

Allowed modes:

```text
new_revision
reconsideration
```

Mode and head-OID invariants are normative as defined in Section 26.5.

`resolution_paths` MUST list every Developer resolution recorded for the run, ordered by ascending `created_at`. Each referenced resolution JSON identifies its companion Markdown and digest. If multiple resolutions address the same question, the later `created_at` governs.

### 28.3 Implementation report

Suggested Markdown structure:

```markdown
# Implementation Report

## Summary

## Scope

## Files Changed

## Important Design Decisions

## Validation Performed

## Known Limitations

## Areas Worth Extra Review
```

The report is an untrusted aid. The Reviewer MUST verify material claims.

### 28.4 Review result

Minimum shape:

```json
{
  "schema_version": 1,
  "created_at": "2026-08-23T03:20:00Z",
  "result_id": "550e8400-e29b-41d4-a716-446655440002",
  "request_id": "550e8400-e29b-41d4-a716-446655440001",
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "round": 2,
  "base_oid": "0123456789abcdef0123456789abcdef01234567",
  "head_oid": "89abcdef0123456789abcdef0123456789abcdef",
  "verdict": "changes_requested",
  "summary": "One correctness issue blocks approval.",
  "findings": [
    {
      "id": "REV-001",
      "severity": "high",
      "blocking": true,
      "category": "correctness",
      "file": "src/example.py",
      "line_start": 41,
      "line_end": 48,
      "problem": "Empty input bypasses the required fallback.",
      "evidence": "The early return executes before fallback handling.",
      "impact": "Valid empty-input requests return the wrong result.",
      "required_change": "Handle empty input before the early return.",
      "verification": "Add a regression test for empty input."
    }
  ],
  "non_blocking_observations": []
}
```

Allowed verdicts:

```text
approved
changes_requested
needs_human
```

Semantic rules:

```text
approved
  → no blocking findings

changes_requested
  → at least one blocking finding

needs_human
  → summary identifies the unresolved Developer decision
```

### 28.5 Implementation response

Minimum shape:

```json
{
  "schema_version": 1,
  "created_at": "2026-08-23T03:40:00Z",
  "response_id": "550e8400-e29b-41d4-a716-446655440003",
  "supersedes_response_id": null,
  "resolution_ids": [],
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "review_round": 2,
  "review_result_id": "550e8400-e29b-41d4-a716-446655440002",
  "reviewed_head_oid": "89abcdef0123456789abcdef0123456789abcdef",
  "responses": [
    {
      "finding_id": "REV-001",
      "disposition": "fixed",
      "rationale": "The empty-input path is now processed before return.",
      "changed_files": [
        "src/example.py",
        "tests/test_example.py"
      ],
      "evidence": [],
      "verification": "python -m unittest tests.test_example"
    }
  ]
}
```

Allowed dispositions:

```text
fixed
rejected
needs_human
```

A `rejected` response MUST include concrete evidence.

A `needs_human` response MUST identify the decision needed and MUST NOT be used to create another automatic review round.

For an ordinary response, `supersedes_response_id` MAY be null and `resolution_ids` MAY be empty. A submit-time response that replaces an earlier escalation-time response MUST set `supersedes_response_id` to that earlier response ID and MUST list the Developer resolution IDs on which the replacement relies. The resolution resolving the escalation created from the superseded response MUST be included. The replacement MUST satisfy the transition and archival rules in Section 26.6.

When the next submission uses `mode = reconsideration`, every response disposition MUST be `rejected` with concrete evidence. A `fixed` disposition is valid only for a changed committed revision submitted with `mode = new_revision`.

### 28.6 Escalation artifact

A machine-readable escalation SHOULD contain:

```json
{
  "schema_version": 1,
  "created_at": "2026-08-23T03:45:00Z",
  "escalation_id": "550e8400-e29b-41d4-a716-446655440004",
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "round": 2,
  "head_oid": "89abcdef0123456789abcdef0123456789abcdef",
  "related_finding_ids": [
    "REV-001"
  ],
  "source_request_id": "550e8400-e29b-41d4-a716-446655440001",
  "source_result_id": "550e8400-e29b-41d4-a716-446655440002",
  "previous_approved_head_oid": null,
  "note_path": "002-escalation.md",
  "note_sha256": "example-sha256",
  "reason": "reviewer_needs_human"
}
```

### 28.7 Developer resolution artifact

A machine-readable resolution MUST contain:

```json
{
  "schema_version": 1,
  "created_at": "2026-08-23T04:00:00Z",
  "resolution_id": "550e8400-e29b-41d4-a716-446655440005",
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "resolves_escalation_id": "550e8400-e29b-41d4-a716-446655440004",
  "applies_to_finding_ids": [
    "REV-001"
  ],
  "resolution_path": "001-resolution.md",
  "resolution_sha256": "example-sha256",
  "additional_rounds_granted": 1
}
```

The companion Markdown records the Developer's actual decision and constraints.

Resolution `created_at` values MUST be strictly increasing within a run. Every later review request includes every run resolution; the latest timestamp governs when resolutions address the same question.

### 28.8 Reviewer-local marker

Minimum shape:

```json
{
  "schema_version": 1,
  "submitted_at": "2026-08-23T04:15:00Z",
  "request_id": "550e8400-e29b-41d4-a716-446655440001",
  "result_id": "550e8400-e29b-41d4-a716-446655440002",
  "status": "result_submitted",
  "review_json_path": "output/review.json",
  "review_sha256": "example-sha256"
}
```

### 28.9 Round record

A round record SHOULD contain the fields shown below. For a marker-confirmed result classified as `stale` or `invalid`, the record MUST retain the trustworthy `result_id` and classification so a later replay can return the historical outcome without touching the active round. If the artifact has no trustworthy result ID, the record uses a diagnostic ID instead.

```json
{
  "schema_version": 1,
  "created_at": "2026-08-23T03:00:00Z",
  "updated_at": "2026-08-23T04:20:00Z",
  "run_id": "550e8400-e29b-41d4-a716-446655440000",
  "round": 2,
  "mode": "new_revision",
  "request_id": "550e8400-e29b-41d4-a716-446655440001",
  "result_id": "550e8400-e29b-41d4-a716-446655440002",
  "base_oid": "0123456789abcdef0123456789abcdef01234567",
  "head_oid": "89abcdef0123456789abcdef0123456789abcdef",
  "status": "applied",
  "verdict": "approved",
  "review_worktree": "/absolute/path/to/review/worktree"
}
```

---

## 29. Review Policy

The Reviewer SHOULD examine the following where relevant:

- compliance with the captured task;
- compliance with all recorded Developer resolutions relevant to the reviewed question;
- functional correctness;
- regressions;
- failure handling;
- security and privacy boundaries;
- concurrency and transaction behavior;
- data migration and compatibility;
- test validity and coverage;
- unnecessary scope expansion;
- consistency with surrounding architecture.

Blocking findings SHOULD be limited to:

- demonstrable correctness defects;
- security or privacy defects;
- data-loss risks;
- explicit requirement violations;
- regressions;
- broken compatibility commitments;
- missing tests necessary to establish required behavior;
- unsafe behavior under documented operating conditions.

Normally non-blocking:

- naming preferences;
- formatting preferences covered by tools;
- optional refactors;
- speculative future improvements;
- personal architectural preference without demonstrated impact;
- requests to expand the captured task.

A recorded Developer resolution is authoritative for the specific question it decides.

The Reviewer MUST:

- verify that the implementation complies with the resolution;
- continue to report defects within the decided approach;
- avoid reopening the decided choice solely because it would prefer another option.

The Reviewer MUST NOT approve merely because tests pass.

The Reviewer MUST NOT request changes merely because a different implementation style is possible.

---

## 30. Herdr Handoff Protocol

Herdr prompts SHOULD be short control messages.

### 30.1 Review-request prompt

```text
AGENT_SQUAD/0.4.4 REVIEW_REQUEST

run_id: <run-id>
round: <round>
request_id: <request-id>
base_oid: <full-object-id>
head_oid: <full-object-id>
review_worktree: <absolute-path>
request: <absolute-path-to-bundle/input/request.json>

Review the exact requested revision in this worktree.
Read the complete local review bundle, including any Developer resolutions.
Do not modify tracked files.
Write the required review artifacts and run agent-squad review-submit.
```

### 30.2 Review-result prompt

The result prompt MUST be verdict-neutral:

```text
AGENT_SQUAD/0.4.4 REVIEW_RESULT

run_id: <run-id>
round: <round>
request_id: <request-id>
result_id: <result-id>
head_oid: <full-object-id>
review: <absolute-path-to-bundle/output/review.json>

A marker-confirmed review result is ready.
Run agent-squad apply-review --result-id <result-id> and follow the state and verdict recorded by Agent Squad.
```

The prompt MUST NOT hard-code `changes_requested` instructions when the actual verdict may be `approved` or `needs_human`.

### 30.3 Supersede prompt

```text
AGENT_SQUAD/0.4.4 REVIEW_SUPERSEDED

run_id: <run-id>
round: <round>
reason: <brief-reason>

This round is no longer authoritative.
Stop work when safe and do not submit it as the current result.
```

### 30.4 Cancel prompt

```text
AGENT_SQUAD/0.4.4 RUN_CANCELLED

run_id: <run-id>
round: <round-or-null>
reason: <brief-reason>

The Agent Squad run has been cancelled.
Do not continue or submit a current review result.
```

### 30.5 Prompt content boundaries

Prompts MUST NOT carry the full diff, full review body, or full Developer resolution when an artifact path is available.

Prompts MUST identify the exact run, round, request, and result where applicable.

---

## 31. Local Delivery, Discoverability, and Idempotency

### 31.1 Persist before send

A logical request, result marker, escalation, or resolution MUST be written before the corresponding Herdr prompt is attempted.

### 31.2 Local serialization

Implementation-side state mutation and request dispatch MUST be serialized by the local state lock.

This prevents two normal local CLI processes using the canonical control root from launching duplicate round resources concurrently.

### 31.3 Crash after prompt delivery

If a process crashes after Herdr accepts a prompt but before local handoff status records `sent`, a retry may send a duplicate notification.

Version 0.4.4 accepts this local at-least-once possibility.

Duplicate notifications MUST NOT:

- create a new logical round;
- create a new request ID;
- create a second result transition;
- apply the same result twice.

Idempotency is based on:

- run ID;
- round;
- request ID;
- result ID;
- deterministic Reviewer name;
- deterministic review-worktree path.

### 31.4 Retry review request

`retry-handoff` MUST support a review request whose recorded handoff is:

```text
pending
failed
sent but unanswered
```

For a sent-but-unanswered request, it MUST first probe:

- expected Reviewer-local marker;
- expected review output;
- deterministic Reviewer session;
- deterministic review workspace.

Review output without a valid Reviewer-local marker is deliberately treated as **no submitted result**. Implementation-side commands MUST NOT adopt or apply it. `status` MAY warn that unmarked output exists, but MUST NOT offer `apply-review`. A still-live Reviewer may finish `review-submit`; otherwise a relaunched Reviewer may overwrite the unmarked output.

If no valid result is ready, it MAY:

- reprompt the existing Reviewer;
- relaunch the Reviewer in the same worktree;
- adopt an existing deterministic Reviewer or workspace.

It MUST reuse the existing run, round, request ID, worktree, and bundle.

It MUST NOT create another logical round.

### 31.5 Discover and recover result handoff

When a valid Reviewer-local marker and review output exist but the result is unapplied:

- `status` MUST report the result;
- `retry-handoff` MUST detect it;
- `retry-handoff` MUST NOT relaunch the review as if no result existed;
- it SHOULD resend the result notification when useful;
- it MUST always report the `apply-review` command.

If the configured Implementer is the current caller, direct application guidance is sufficient; sending a prompt back into the same active interaction is not required.

### 31.6 Review-submit retry

The Reviewer MAY rerun `review-submit` with the same result ID and digest to resend a lost result notification.

A corrected review artifact after a rejected local submission SHOULD use a new result ID unless the original result was never marker-confirmed.

The exact correction rule MUST be deterministic and tested.

Before recovery removes a marker-confirmed result identity, it MUST durably bind that result ID to its original review digest in implementation-owned round storage. A Reviewer-local mirror MAY provide earlier rejection, but implementation-side discovery and `apply-review` MUST use the authoritative binding even when that mirror is absent, malformed, or identity-mismatched.

### 31.7 Recorded result replay and unknown-result refusal

Before active-phase and implementation-HEAD enforcement, `apply-review` MUST search authoritative round history for the presented result ID.

When the result ID was previously recorded as:

- `applied`, return the previously applied outcome;
- `stale`, return the recorded stale classification and diagnostic location;
- `invalid`, return the recorded invalid classification and diagnostic location.

Returning a historical outcome or classification MUST NOT require the original disposable review worktree to still exist when authoritative round history is sufficient.

A historical replay MUST NOT repeat:

- phase transitions;
- budget increments;
- event effects;
- notifications.

When the presented result ID matches neither authoritative round history nor the active round's valid Reviewer-local marker, `apply-review` MUST refuse the invocation without changing any round status, run phase, budget value, event effect, or notification state.

### 31.8 No dispatcher leases or recipient fencing

Dispatcher leases, expiry, fencing tokens, and distributed recipient claims are intentionally excluded.

The supported environment uses:

- one canonical local control root;
- one local mutation lock;
- deterministic resources;
- explicit IDs;
- state and output probing.

---

## 32. CLI Surface

The final spelling MAY be refined, but the reference CLI SHOULD remain close to:

```text
agent-squad doctor
agent-squad init
agent-squad start
agent-squad status
agent-squad submit
agent-squad review-submit
agent-squad apply-review
agent-squad supersede
agent-squad retry-handoff
agent-squad escalate
agent-squad resume
agent-squad complete
agent-squad cancel
```

This is not an invitation to expose every internal transition as a command.

### 32.1 `doctor`

Performs deterministic checks, optional live Reviewer preflight, submodule warnings, and orphaned review-resource reporting.

### 32.2 `init`

Initializes canonical repository-local runtime state and local Git exclusions.

### 32.3 `start`

Starts one Implementation Review Squad run.

### 32.4 `status`

Reports at least:

- run ID;
- phase;
- task path;
- Implementer;
- Reviewer kind;
- base object ID;
- current round;
- round status;
- submission mode;
- current requested head;
- approved head;
- active escalation ID when phase is `needs_human`;
- request handoff status;
- effective review budget;
- review budget usage;
- review-worktree path;
- all Developer resolutions recorded for the run;
- whether a marker-confirmed unapplied result exists;
- the next safe command.

When a result is ready but unapplied, `status` MUST make that condition prominent.

### 32.5 `submit`

Creates a `new_revision` or `reconsideration` round and requests review.

It accepts the implementation report and, when required, the previous-round response. After a Developer resolution, it also performs the defined replacement of an earlier escalation-time response without losing the earlier artifact.

### 32.6 `review-submit`

Runs in the review worktree, validates local output, writes the Reviewer-local marker, and notifies the Implementer.

### 32.7 `apply-review`

Runs in the implementation worktree, independently validates and applies the expected active-round result idempotently. It accepts an optional `--result-id` and checks authoritative history first. Replays of results already classified `applied`, `stale`, or `invalid` return their recorded outcome or classification without requiring the original live review worktree; unknown or foreign result IDs are refused without changing active state.

### 32.8 `supersede`

Invalidates a no-longer-relevant active review round and returns to implementation.

### 32.9 `retry-handoff`

Re-drives the current request or result handoff without creating a new logical round.

It probes for an existing result before relaunching a Reviewer.

### 32.10 `escalate`

Moves the run to `needs_human` with a persisted escalation artifact. From `reviewing` it supersedes the active round; from `approved` it clears current approval authority. An optional valid response is archived in the round it answers.

### 32.11 `resume`

Resumes from `needs_human` only after a Developer resolution.

It may accept an explicitly recorded review-budget extension.

### 32.12 `complete`

Completes an exactly approved revision without merging, pushing, or deploying.

### 32.13 `cancel`

Cancels the active run while preserving artifacts and sending a best-effort notice to an active Reviewer. An open reviewing round is first marked `superseded`, and current approval authority is cleared when cancelling from `approved`.

---

## 33. Failure and Recovery Semantics

### 33.1 Herdr unavailable

Preserve logical state and artifacts.

Report the current handoff and the exact retry command.

Do not convert a transport problem into an engineering verdict.

### 33.2 Reviewer unavailable before delivery

Preserve the review request and round.

Allow `retry-handoff`, `supersede`, or Developer cancellation.

### 33.3 Reviewer dies or stalls after request delivery

If the request handoff is recorded as sent but no valid marker-confirmed result exists:

- `status` MUST show that the run is awaiting a result;
- `retry-handoff` MUST be able to reprompt, relaunch, or adopt the same round;
- `supersede` MUST remain available;
- no new round is created merely to recover delivery.

Unmarked review output is incomplete Reviewer work, not a submitted result. It MUST NOT be adopted or applied by the implementation side and MAY be overwritten by the recovered Reviewer.

### 33.4 Result exists but notification is lost

If valid `review.json` and `local-state.json` exist:

- `status` MUST report the result as ready;
- `retry-handoff` MUST report or resend the result handoff;
- `apply-review` MUST be directly usable;
- the run MUST NOT remain silently opaque.

### 33.5 Reviewer blocked by permissions

Report the condition.

Do not automatically inject approval keys or use arbitrary `send-keys`.

The Developer may correct configuration and retry or cancel.

### 33.6 Malformed or semantically invalid review

Reject the result without applying a verdict.

Preserve diagnostic artifacts under the active round's `diagnostics/invalid-results/<diagnostic-id>/` directory.

Set the round `invalid` and return to `implementing` unless the failure creates a genuine safety ambiguity.

Do not consume review budget.

### 33.7 Duplicate request notification

The deterministic round identity prevents creation of another logical round.

The recipient should adopt the existing request.

### 33.8 Replayed or unknown result notification

When a notification replays a result already classified `applied`, `stale`, or `invalid`, `apply-review` returns the recorded outcome or classification without repeating transitions, budget effects, events, or notifications.

When the result ID matches neither authoritative history nor the active marker, `apply-review` refuses it without altering the healthy active round.

### 33.9 Reviewer modified a tracked file

The result is invalid.

Preserve diagnostics.

Return to `implementing` by default and require another review round.

Use `needs_human` only when the modification or surrounding circumstances create an unresolved safety concern.

### 33.10 Review worktree cleanup failure

Leave the worktree in place and report its path.

Do not use forceful destructive cleanup merely to hide the failure.

### 33.11 Terminal-state interruption

If phase was atomically written as `completed` or `cancelled` but cleanup did not finish:

- the terminal run remains terminal;
- the active-run slot is released;
- `status` or `doctor` reports residual resources;
- a later run may start.

### 33.12 Unknown repository identity

A mutating command MUST stop rather than operate on a different or moved worktree without explicit Developer recovery.

### 33.13 Base ancestry failure

Submission stops.

Agent Squad instructs the Developer to cancel and restart with a newly resolved base.

It MUST NOT repair history automatically.

### 33.14 Missing, invalid, or uncommitted implementation response

A submission following `changes_requested` MUST stop before creating a new round when:

- response is absent;
- a blocking finding is unaddressed;
- an unknown finding is referenced;
- evidence required for rejection is absent;
- result identity does not match;
- a response replacement omits the required `supersedes_response_id` or Developer `resolution_ids`;
- a superseding response leaves a former `needs_human` disposition unresolved.

Response intake before the successful submission commit point is staging only. If a later pre-commit submission step fails, the staged response MUST NOT become an immutable authoritative rewrite. The Implementer may correct it and retry until a successfully persisted new round references the response. A partial response file left by a crash without such a round reference is provisional and recoverable under Section 26.6.

### 33.15 Missing or invalid Developer resolution

`resume` MUST stop when:

- no resolution is supplied;
- the resolution digest cannot be recorded;
- the referenced escalation or finding does not belong to the run;
- a required budget extension is absent after budget exhaustion.

A review result whose bundle resolution digest does not match the authoritative run copy is invalid.

### 33.16 Orphaned worktree

`doctor` reports orphaned review resources.

The Developer may invoke an explicit safe cleanup path after inspecting them.

The existence of an orphan MUST NOT cause Agent Squad to adopt it into an unrelated round.

### 33.17 Submodule-dependent review cannot run

Report the limitation and retain the request.

Do not initialize networked submodules automatically.

The Developer may prepare the worktree explicitly, revise the task's validation expectations, supersede, or cancel.

---

## 34. Cleanup Policy

Cleanup SHOULD remove:

- round-scoped Reviewer sessions when safe;
- `.agent-squad-review/` only after the complete bundle is archived or the round is intentionally abandoned;
- detached review worktrees after the round becomes terminal;
- known generated outputs in disposable smoke-test repositories.

Cleanup MUST preserve authoritative run artifacts.

### 34.1 Review bundle cleanup order

For an applied or deliberately abandoned round:

1. when the round is `superseded`, probe for marker-confirmed late output and archive it under Section 27.2;
2. archive all other required bundle artifacts;
3. verify the archive;
4. remove `.agent-squad-review/` from the detached worktree;
5. remove known configured generated output when safe;
6. verify no tracked modifications exist;
7. invoke normal `git worktree remove` without force.

### 34.2 Generated output

Known ignored output may include:

```text
build/
dist/
coverage/
.cache/
```

The project MAY support a narrow configured allowlist.

It MUST NOT:

- infer that every ignored path is safe to delete;
- run broad `git clean -fdx`;
- delete unrelated untracked content;
- use forced worktree removal as the default.

If safe cleanup cannot be established, leave the resource and report it.

### 34.3 Terminal cleanup and notices

`supersede` and `cancel` SHOULD notify an active Reviewer on a best-effort basis.

Notification failure does not prevent the authoritative state transition.

### 34.4 Orphan reporting

Residual worktrees and Reviewer sessions SHOULD be visible through `doctor`.

Automatic garbage collection of all old Herdr or Git resources is outside v0.4.4.

---

## 35. Shared Agent Instructions

Agent Squad SHOULD provide concise instruction fragments that consuming repositories may include or reference.

It MUST NOT overwrite unrelated `AGENTS.md`, `CLAUDE.md`, or equivalent content.

### 35.1 Implementer fragment

When an Agent Squad run is active:

- do not ask the Developer to relay a review request;
- submit only coherent committed candidates;
- provide the required implementation report;
- provide a complete response after `changes_requested`;
- evaluate review findings independently;
- fix valid findings;
- reject findings only with concrete evidence;
- use `agent-squad escalate` for real human decisions;
- do not create a meaningless commit for same-SHA reconsideration;
- do not claim completion until exact-revision approval is applied.

### 35.2 Reviewer fragment

When launched for an Agent Squad review:

- read the complete local bundle;
- review the exact requested revision;
- treat the implementation report as untrusted;
- read every Developer resolution recorded for the run;
- determine which resolutions are relevant to the reviewed question;
- treat the latest relevant resolution by `created_at` as authoritative for that decided question;
- verify that the implementation complies with those resolutions;
- do not re-litigate an already-decided choice solely because another option is preferable;
- do not modify tracked files;
- separate blocking findings from optional observations;
- write valid structured artifacts;
- run `agent-squad review-submit`;
- do not ask the Developer to relay the result.

### 35.3 Result handling fragment

When Agent Squad reports that a marker-confirmed result is ready:

- run the reported apply command, normally `agent-squad apply-review --result-id <result-id>`;
- follow the validated verdict and next-state guidance;
- do not infer the verdict from terminal prose alone.

---

## 36. Testing Strategy

### 36.1 Unit tests

Required coverage includes:

- run-state transitions;
- approved-to-new-review transition;
- terminal-state release;
- atomic writes;
- local locking;
- repository-identity validation;
- Git object-ID validation for SHA-1 and SHA-256;
- per-run timestamped event logging;
- authoritative review-budget extension;
- request validation;
- path-base validation;
- review-result semantic invariants;
- implementation-response completeness;
- rejected-response evidence;
- response-triggered escalation guidance;
- automatic escalation for a valid `needs_human` review result;
- automatic escalation at review-budget exhaustion;
- escalation artifacts;
- escalation from `reviewing` superseding the active round;
- escalation from `approved` clearing approval authority;
- cancellation from `reviewing` superseding the active round;
- optional escalation-response validation and archival;
- escalation-time response staging until the successful escalation commit point;
- corrected `escalate --response` retry after a failed pre-commit escalation;
- escalation-time response replacement after Developer resolution;
- preservation of replaced responses under the round diagnostic directory;
- replacement linkage through `supersedes_response_id` and `resolution_ids`;
- validation that the superseding response resolves former `needs_human` dispositions consistently with the recorded Developer resolution;
- Developer resolution artifacts;
- resolution hash validation;
- unconditional propagation of all run resolutions into later bundles;
- latest-resolution conflict ordering;
- duplicate applied-result lookup before active-phase and implementation-HEAD enforcement;
- replayed stale-result classification without altering a newer active round;
- replayed invalid-result classification without altering a newer active round;
- refusal of an unknown or foreign result ID without state mutation;
- response archival and replacement only at the successful submit commit point;
- corrected response retry after a failed pre-commit submission;
- same-SHA reconsideration;
- rejection of `fixed` dispositions in reconsideration mode;
- review-budget exhaustion and extension;
- superseded, stale, and invalid results;
- Reviewer-local marker validation;
- marker-confirmed result discovery;
- unmarked output treated as no submitted result;
- `previous_reviewed_head_oid` after intervening superseded or invalid rounds;
- cleanup-time discovery and archival of marker-confirmed late output for a superseded round;
- invalid-result diagnostic archival at the specified path;
- no duplicate logical round on retry.

### 36.2 Git integration tests

Use temporary repositories and worktrees to cover:

- fixed base resolution;
- base-ancestor success and failure;
- explicit `base_oid..head_oid` scope;
- round 1 no-change rejection;
- new-revision head-change requirement;
- reconsideration same-head requirement;
- same-head recovery after superseded or invalid round;
- detached review worktree creation;
- `.agent-squad-review/` local exclusion;
- pre- and post-review integrity;
- rejection after any tracked-file modification;
- allowed review-bundle output;
- complete bundle archive;
- cleanup with known ignored build output;
- implementation `HEAD` advancing during review;
- `apply-review` implementation-HEAD comparison;
- deterministic stale-result recovery to `implementing`;
- new committed candidate after approval;
- completion only at the approved head with a clean tracked worktree;
- orphaned worktree reporting;
- `.gitmodules` warning behavior.

### 36.3 Fake-Herdr tests

A fake Herdr executable or adapter MUST support fixtures for:

- agent discovery;
- agent start;
- agent get;
- agent prompt;
- worktree open;
- prompt failure;
- duplicate delivery;
- existing-agent adoption;
- blocked and unavailable conditions;
- Reviewer death after request delivery;
- sent-but-unanswered request retry;
- lost result notification;
- marker-confirmed result rediscovery;
- result-notification retry;
- cancellation and supersede notices.

Tests MUST inspect generated prompts and verify that request and result prompts are versioned and verdict-neutral where required.

No real model calls are required for automated tests.

### 36.4 Artifact end-to-end tests

Automated tests MUST exercise:

1. a task snapshot;
2. first candidate submission;
3. `changes_requested`;
4. an escalation-time response containing `needs_human`;
5. a Developer escalation;
6. a Developer resolution;
7. a submit-time response that supersedes the escalation-time response, links to the resolution, and preserves the earlier response under diagnostics;
8. a new revision or evidence-based reconsideration consistent with the resolution;
9. the Developer resolution copied into the later bundle;
10. a Reviewer that obeys the resolution;
11. a marker-confirmed result whose notification is lost;
12. `status` discovery;
13. `apply-review`;
14. exact-revision approval;
15. completion.

### 36.5 Self-contained smoke runner

The repository MUST include a committed smoke-test runner, for example:

```text
scripts/run-smoke-tests
```

It MUST:

1. create a disposable Git repository;
2. seed a small source fixture and tests;
3. install or expose the candidate Agent Squad CLI;
4. create an accepted task specification;
5. initialize Agent Squad configuration;
6. start from a clean baseline `HEAD`;
7. create a candidate change only after the run starts;
8. exercise request, changes requested, response, revision, re-review, and approval;
9. exercise one lost-notification discovery path;
10. exercise one Developer resolution propagation path;
11. clean up temporary worktrees and agents when safe;
12. report any retained resources.

The runner MUST NOT commit an intentional defect into an active real development checkout.

### 36.6 Real Herdr smoke tests

Before v0.4.4 release, perform both directions in disposable repositories:

```text
Codex implements → Claude Code reviews

Claude Code implements → Codex reviews
```

Also test:

- one same-SHA reconsideration;
- one request re-dispatch after a Reviewer exits;
- one marker-confirmed result with a lost notification;
- one superseded round;
- one `needs_human` transition;
- one Developer resolution read by a fresh round-scoped Reviewer;
- one small review-budget extension.

---

## 37. Implementation Increments

Agent Squad SHOULD be implemented through focused issues or stacked pull requests rather than one oversized change.

### Increment 1 — Core state, identity, and artifacts

Implement:

- package and CLI skeleton;
- repository initialization;
- canonical control root;
- local Git exclusion;
- run and round records;
- atomic state and local lock;
- per-run event log;
- task capture;
- artifact validators;
- unit tests.

### Increment 2 — Git and review isolation

Implement:

- repository identity;
- base and head resolution;
- base-ancestor validation;
- submission-mode rules;
- detached review worktrees;
- self-contained bundles;
- integrity checks;
- bundle archive;
- safe cleanup;
- Git integration tests.

### Increment 3 — Herdr request and result handoffs

Implement:

- installed-command discovery;
- agent lookup;
- deterministic Reviewer launch and adoption;
- request delivery;
- Reviewer-local submission marker;
- result delivery;
- status probing;
- retry behavior for request and result directions;
- fake-Herdr tests;
- live preflight.

### Increment 4 — Review iteration and human decisions

Implement:

- response intake;
- repeated rounds;
- same-SHA reconsideration;
- supersede;
- escalation;
- Developer resolution;
- resolution propagation;
- review-budget extension;
- approval, post-approval resubmission, completion, and cancellation.

### Increment 5 — Operations and dogfood

Implement:

- documentation;
- orphan-resource diagnostics;
- self-contained smoke runner;
- real two-direction smoke tests;
- Double Dubs dogfood example;
- first external review of the implementation.

Each increment MUST remain focused on the Implementation Review Squad.

---

## 38. Double Dubs as Dogfood

Double Dubs is an early consuming repository, not the Agent Squad host project.

The Agent Squad source lives in its own repository.

A Double Dubs example MAY describe:

- local agent names;
- preferred task-spec location;
- its `chore/` branch naming convention for developer tooling;
- validation commands;
- generated build paths;
- issue-tracker practice;
- submodule behavior if relevant.

These details MUST NOT become normative Agent Squad requirements.

Existing unrelated untracked files in a dogfood repository MUST NOT be deleted, moved, committed, or silently ignored by Agent Squad setup.

Dogfood development SHOULD use a clean dedicated worktree.

---

## 39. Reliability Guarantees

Within the stated local operating assumptions, Agent Squad v0.4.4 MUST guarantee:

1. A review request is persisted before notification is attempted.
2. A Reviewer-local result marker is persisted before result notification is attempted.
3. A valid approval cannot apply to a different Git revision.
4. The fixed base must remain an ancestor of every submitted head.
5. The Reviewer does not inspect the Implementer's uncommitted working-tree content as the review source.
6. The Reviewer does not require write access to the implementation control root.
7. Authoritative review results do not depend on terminal transcript retention.
8. A lost request notification can be retried without creating a new logical round.
9. A sent-but-unanswered request can be re-driven or superseded.
10. A marker-confirmed result can be discovered even when the result notification is lost.
11. A failed Herdr prompt does not erase the logical request or result.
12. Duplicate notifications do not create duplicate rounds, budget effects, or state transitions.
13. A completed or cancelled run does not permanently block a new run.
14. Stale, invalid, or superseded results cannot approve the active candidate.
15. A valid evidence-only rebuttal does not require a meaningless code commit.
16. Every applied `changes_requested` result requires a complete implementation response before another automatic review.
17. A Developer resolution is persisted and copied to every applicable later Reviewer bundle.
18. A fresh round-scoped Reviewer can learn and respect an authoritative Developer decision.
19. Review-budget extensions are stored in authoritative state.
20. The normal loop can proceed without Developer message relay.
21. Replaying a result previously classified `stale` or `invalid` cannot alter a newer active round.
22. An unknown or foreign result ID cannot invalidate the active round.
23. A response staged by a submission that fails before the successful commit point can be corrected on retry rather than being frozen as authoritative.
24. A response staged by `escalate --response` that fails before the successful escalation commit point can be corrected on retry rather than being frozen as authoritative.
25. Marker-confirmed output discovered during cleanup of a superseded round is archived as a late result before the review worktree is removed.

---

## 40. Explicit Non-guarantees

Agent Squad v0.4.4 does not guarantee:

- exactly-once Herdr prompt delivery;
- protection against malicious same-user agents;
- multi-machine consistency;
- automatic recovery from every Herdr or terminal failure;
- safe automated review of Agent Squad's own unreviewed control-plane changes;
- automatic merge correctness;
- CI success;
- automatic submodule initialization;
- that two agents always outperform one;
- that a model review replaces human review for high-risk software.

These limitations MUST be documented clearly.

---

## 41. Deliberate Simplifications and Rejected Overdesign

This section is normative design guidance.

### 41.1 JSON state instead of SQLite

A single active run per local worktree does not require a workflow database.

Use atomic JSON state, explicit run and round records, and a local lock.

### 41.2 Local lock instead of dispatcher lease

One canonical control root and one local CLI mutation lock are sufficient for the supported environment.

Do not add lease expiry, fencing, or distributed ownership.

### 41.3 Persisted handoff state instead of a durable outbox subsystem

State MUST preserve the current request handoff, while the Reviewer-local marker preserves result-submission evidence.

This does not require a generic transactional outbox table or message broker.

### 41.4 Probing and idempotent retry instead of exactly-once delivery

Duplicate local prompt delivery is acceptable when:

- logical identities are stable;
- state is probed before retry;
- operations are idempotent.

### 41.5 Captured task and explicit resolutions instead of full policy archive

Freeze the approved task, explicit context, and Developer resolutions.

Do not automatically archive every project document.

### 41.6 Interactive confirmation instead of a human identity system

Use practical accidental-action guards where helpful.

Do not build authentication, RBAC, or hostile-process defenses.

### 41.7 Direct protocol instead of generic workflow abstractions

Implement the concrete Implementation Review Squad directly.

### 41.8 Explicit self-review limitation instead of a control-plane version framework

Document self-hosting limits.

Do not build release pinning and bootstrap infrastructure solely to review Agent Squad itself in v0.4.4.

### 41.9 Narrow submodule warning instead of repository-environment provisioning

Detect and document submodule limitations.

Do not turn Agent Squad into a general repository provisioning tool.

---

## 42. Future Experiments

After real usage demonstrates that the Implementation Review Squad is valuable, Agent Squad may explore other small, role-based collaborations such as:

- Parallel Investigation Squad;
- Architecture Exploration Squad;
- Specialist Review Squad;
- Security Review Squad;
- Competitive Design Squad.

These are experiments, not roadmap commitments.

No v0.4.4 implementation decision should be justified solely by making these possibilities easier.

A shared framework should be extracted only after multiple implemented squads demonstrate concrete repeated requirements.

---

## 43. Acceptance Criteria

Agent Squad v0.4.4 is acceptable only when all of the following are true:

1. Agent Squad exists in its own repository.
2. The installed CLI can initialize an arbitrary local Git worktree.
3. The consuming repository uses the canonical `.agent-squad/` control root.
4. Local Git exclusion covers `.agent-squad/` and `.agent-squad-review/`.
5. Only one active run is permitted per implementation worktree.
6. Codex can act as Implementer and Claude Code as Reviewer.
7. Claude Code can act as Implementer and Codex as Reviewer.
8. The Developer can interact normally with the Implementer before submission.
9. The Implementer can submit a committed candidate without Developer relay.
10. Round 1 rejects an unchanged base/head.
11. Every submitted base remains an ancestor of its head.
12. New-revision and reconsideration head rules are enforced.
13. A complete previous-round response is required after `changes_requested`.
14. A `needs_human` response cannot create another automatic review.
15. The logical review request is persisted before Herdr notification.
16. A dedicated detached review worktree is created at the exact requested head.
17. The review bundle is self-contained and uses bundle-root-relative paths.
18. The Reviewer does not require write access to the implementation control root.
19. A live preflight proves the Reviewer can read the snapshot and write permitted outputs.
20. The Reviewer receives the request through Herdr.
21. The Reviewer receives every Developer resolution recorded for the run and applies the latest relevant one.
22. The Reviewer writes structured JSON and human-readable Markdown results.
23. The Reviewer writes a marker before attempting result notification.
24. A result cannot be valid after any tracked-file modification.
25. `status` detects a marker-confirmed unapplied result.
26. A lost result notification does not leave the run silently stalled.
27. `apply-review` independently reruns full validation.
28. Result application is idempotent.
29. `changes_requested` returns the run to implementation when budget remains.
30. Review budget counts only valid applied change-request results.
31. Budget extensions are authoritative in `state.json`.
32. An unchanged revision can be resubmitted for evidence-based reconsideration without a meaningless commit, and reconsideration responses contain only evidence-based `rejected` dispositions.
33. A sent-but-unanswered request can be retried without a new round.
34. A no-longer-relevant review can be superseded.
35. Late results for superseded rounds cannot change active state.
36. An implementation HEAD advance makes the result stale and returns deterministically to implementation.
37. The Implementer can explicitly escalate from implementation.
38. A valid Reviewer `needs_human` verdict and review-budget exhaustion automatically create an escalation artifact.
39. Escalating or cancelling from `reviewing` closes the active round as `superseded`.
40. Escalating from `approved` clears current approval authority.
41. The Developer can record a resolution linked to the active escalation and resume.
42. Every recorded resolution is included in every later bundle, and the latest relevant resolution governs conflicts.
43. A fresh Reviewer treats the recorded Developer decision as authoritative for the decided question.
44. Approval applies only to the exact reviewed Git object ID and result.
45. A new committed candidate may be submitted after approval.
46. A newer commit prevents completion under an older approval.
47. `apply-review` compares the current implementation `HEAD` with the active round head before applying a result.
48. `complete` relies on exact approved-head equality and explicit worktree-cleanliness checks rather than an undefined newer-commit test.
49. Herdr prompt failure preserves state and supports retry.
50. Duplicate prompt delivery does not create duplicate logical effects.
51. Unmarked Reviewer output is never adopted as a submitted result.
52. Late results are archived under their superseded round and cannot affect active state.
53. Per-run events contain timestamps.
54. Terminal runs release the active-run slot.
55. Cancellation sends a best-effort Reviewer notice.
56. Runtime artifacts survive terminal closure.
57. Cleanup avoids broad destructive commands and default force removal.
58. `doctor` reports orphaned review resources.
59. Repositories with submodules receive a documented warning.
60. Unit tests pass.
61. Git integration tests pass.
62. Fake-Herdr tests pass.
63. The self-contained disposable smoke runner passes.
64. Both real role directions pass in disposable repositories.
65. A real run exercises a Developer resolution with a fresh Reviewer.
66. Double Dubs or another real repository successfully dogfoods the workflow.
67. Agent Squad does not automatically merge, push, deploy, or alter requirements.
68. The implementation does not introduce a generic multi-agent workflow engine.
69. A submit-time response after Developer resolution can supersede an escalation-time response without destroying it, and the replacement identifies both the superseded response and the resolution resolving the escalation created from that response.
70. Duplicate `apply-review` for an already applied result returns the historical outcome before active-phase or implementation-HEAD checks.
71. Invalid review results are retained under the round's named `diagnostics/invalid-results/` location.
72. Replayed results previously classified `stale` or `invalid` return their recorded classification without changing a newer active round.
73. A presented result ID that matches neither authoritative history nor the active marker is refused without state mutation.
74. Response archival or replacement becomes authoritative only at the successful submit commit point; a failed pre-commit submission does not freeze a corrected retry.
75. An escalation-time response becomes authoritative only at the successful escalation commit point; a failed pre-commit escalation does not freeze a corrected retry.
76. Cleanup of a superseded round archives marker-confirmed late output under the round's `diagnostics/late-results/` path before removing the review worktree.

---

## 44. Definition of Done

Agent Squad v0.4.4 is complete when the following developer experience works reliably:

```text
Developer defines a task and starts a run
        ↓
Implementer works interactively and commits a candidate
        ↓
Implementer submits through Agent Squad
        ↓
Agent Squad validates base/head, persists the request,
and creates an isolated review worktree
        ↓
Herdr launches the independent Reviewer
        ↓
Reviewer reviews the exact revision and submits structured findings
        ↓
If the result notification is lost, Agent Squad status still discovers it
        ↓
Implementer applies the fully revalidated result
        ↓
Implementer fixes, rebuts, or escalates
        ↓
Developer resolutions are recorded and reach later fresh Reviewers
        ↓
Reviewer approves the exact revision or returns another valid outcome
        ↓
Developer performs final integration
```

During the normal review-and-fix cycle, the Developer does not act as a messenger.

The project is not done merely because a design document, CLI skeleton, or happy path exists.

It is done when:

- both Codex-to-Claude and Claude-to-Codex loops work in disposable repositories;
- one real repository can use the tool;
- lost-notification and human-resolution paths work;
- the implementation preserves the anti-overengineering boundary.

---

## 45. Instructions to the Implementing Agent

Before coding:

1. Read this specification completely.
2. Inspect the Agent Squad repository, if it already exists.
3. Inspect the locally installed Herdr command and schema.
4. Identify any direct conflict between this specification and the installed environment.
5. Propose focused implementation increments rather than one large change.
6. Do not introduce abstractions for future squad types.
7. Prefer the smallest local mechanism that satisfies the stated correctness contract.
8. Treat scope-expanding ideas as separate proposals.
9. Do not claim that the unreviewed Agent Squad implementation independently reviewed itself.
10. Preserve evidence of tests, smoke runs, limitations, and deviations.
11. Do not improvise around base ancestry, submission modes, response intake and submit/escalate commit-point semantics, historical result replay, unknown-result refusal, late-result archival ownership, result discovery, needs-human application, escalation side effects, Developer resolutions, or budget state; those semantics are fixed by this specification.

When a requirement is still ambiguous, choose the interpretation that:

- preserves exact-revision review;
- preserves human authority;
- makes stalls visible and recoverable;
- avoids Developer message relay in the normal loop;
- introduces the least new machinery.

---

# Appendix A — Disposition of Review Findings

*Informative.* This appendix records why earlier concerns were accepted, simplified, deferred, or rejected.

| Review concern | v0.4.4 disposition | Rationale |
|---|---|---|
| Dispatch state changed after prompt delivery | Accepted | Request and reviewing state are persisted before notification. |
| Shared mutable worktree can invalidate exact review | Accepted | Every round uses a detached review worktree at the exact head. |
| Reviewer may lack cross-worktree write permission | Accepted with simpler design | Bundle and output live inside the review worktree; live preflight proves access. |
| Evidence-only rebuttal required a new commit | Accepted | Same-SHA reconsideration is supported. |
| Human deferral required a meaningless submission | Accepted | `escalate` is a direct transition. |
| Implementer could be trapped while Reviewer stalls | Accepted | `supersede` and sent-request retry provide recovery. |
| Final-round resume had no remaining budget | Accepted | Developer may grant an explicit extension stored in state. |
| Active-loop uniqueness existed only per arbitrary control root | Accepted | The canonical control root is mandatory. |
| Disposable smoke test was incomplete | Accepted | A self-contained committed runner is required. |
| Candidate control plane could review itself | Deferred by scope | Automated self-review remains outside the guarantee. |
| Full immutable snapshot of all policy context | Simplified | Task, explicit context, and Developer resolutions are captured. |
| Human-only commands required strong identity controls | Simplified | Practical interactive checks are allowed; hostile-process security is excluded. |
| SQLite transactional workflow database | Rejected | Atomic JSON, explicit run/round records, and a local lock satisfy the local model. |
| Durable outbox, dispatcher leases, and fencing | Rejected | Persist-before-send, local serialization, probing, deterministic identities, and idempotent retry are sufficient. |
| Generic recipient claims for all messages | Simplified | Stable IDs, marker-confirmed output, and idempotent application prevent duplicate logical processing. |
| General workflow engine for future squads | Rejected | Future abstractions must come from proven squad implementations. |
| Developer resolution was not delivered to later Reviewer | Accepted | Resume creates hashed resolution artifacts copied into every later bundle. |
| Submitted review result could go dark | Accepted | Reviewer-local marker, status probing, retry-handoff, and direct apply make the result visible. |
| Base usability was undefined | Accepted | Every submit requires `base_oid` to be an ancestor of `head_oid`; reviewed scope is explicit. |
| Submission mode and head consistency were undefined | Accepted | First-round, new-revision, reconsideration, recovery, and post-approval rules are fixed. |
| Sent-but-dead review request lacked recovery | Accepted | Retry may reprompt, relaunch, or adopt the same round; supersede remains available. |
| Event-log location was contradictory | Accepted | There is exactly one timestamped per-run event log. |
| Budget extension existed only in the log | Accepted | Original, additional, effective, and consumed budget values live in authoritative state. |
| Implementer had no escalation command | Accepted | `agent-squad escalate` is part of the minimal CLI. |
| `.agent-squad-review/` polluted worktrees and cleanup | Accepted | It is added to local Git exclude and removed before normal worktree removal. |
| Response intake was unspecified | Accepted | `submit` ingests, validates, archives, and bundles the previous-round response. |
| `apply-review` trusted Reviewer-side validation | Accepted | Implementation-side application reruns complete structural, semantic, identity, hash, and integrity checks. |
| Result prompt assumed changes were requested | Accepted | Result prompt is verdict-neutral. |
| “Tracked production files” was ambiguous | Accepted | The enforceable rule is no modification to any tracked file. |
| Reviewer-local and authoritative round statuses were conflated | Accepted | `result_submitted` is Reviewer-local; authoritative round remains reviewing until apply. |
| New commit after approval required artificial escalation | Accepted | `approved → submit new_revision → reviewing` is supported. |
| HEAD advance recovery was nondeterministic | Accepted | Result becomes stale and phase returns to implementing. |
| Orphaned worktrees were invisible | Accepted | `doctor` reports them without automatic destructive cleanup. |
| Cancel did not notify Reviewer | Accepted | Cancellation sends a best-effort notice. |
| Sensitive instruction changes were only process guidance | Accepted as warning | Submit surfaces a non-blocking warning. |
| Submodules were unaddressed | Accepted as limitation | Doctor warns; no automatic networked initialization is performed. |
| Valid `needs_human` verdict lacked an application path | Accepted | `apply-review` now applies the round, creates a linked escalation artifact, and enters `needs_human`; budget exhaustion uses the same explicit path. |
| Escalate and cancel left in-flight round or approval authority ambiguous | Accepted | Reviewing rounds become `superseded`; escalation/cancellation records the cause; approval authority is cleared when leaving `approved`. |
| Resolution applicability was undefined | Accepted with the simplest rule | Every run resolution is copied into every later bundle; the latest relevant `created_at` governs. |
| Reconsideration could claim a finding was fixed without changing the revision | Accepted | Reconsideration permits only evidence-based `rejected` dispositions; `fixed` requires a new committed revision. |
| `apply-review` omitted the current implementation-HEAD comparison | Accepted | Application now checks implementation `HEAD` against the round head before any valid transition. |
| `complete` used an undefined newer-unreviewed-commit clause | Accepted by deletion | Exact approved-head equality plus explicit worktree-cleanliness checks are the complete authority. |
| Unmarked Reviewer output had no stated recovery intent | Accepted | It is deliberately treated as incomplete and may be overwritten by a recovered Reviewer. |
| `previous_reviewed_head_oid` was undefined | Accepted | It is the head of the most recent round with an authoritative `applied` result. |
| Late-result retention location was unspecified | Accepted | Late results are archived under the superseded round's diagnostic directory. |
| Response replacement after `escalate --response` was undefined | Accepted | The later submit-time response supersedes the escalation-time response, preserves it under diagnostics, and links the replacement to the Developer resolution. |
| Duplicate-result detection occurred after phase and HEAD checks | Accepted | `apply-review` now returns an already applied result's historical outcome before enforcing active-phase or implementation-HEAD requirements. |
| Invalid-result diagnostics had no named location | Accepted | Invalid result artifacts and validation errors are archived under the round's `diagnostics/invalid-results/<diagnostic-id>/` directory. |
| Replayed stale or invalid result could invalidate a newer active round | Accepted | Historical `applied`, `stale`, and `invalid` classifications are returned before active-round checks; unknown or foreign result IDs are refused without state mutation. |
| Response archival was not ordered against failed submission | Accepted | Response data remains staged until the successful submit commit point; a failed pre-commit submission does not freeze a corrected retry. |
| Replacement response referred to a no-longer-active escalation | Accepted editorially | The contract now references the resolution resolving the escalation created from the superseded response. |
| `escalate --response` archived before the escalation commit point | Accepted | Escalation-time response intake is staged until the escalation, response archival, round metadata, and run state reach the successful commit point; failed pre-commit retries may correct provisional output. |
| Late-result archival had no designated actor | Accepted | The implementation-side cleanup path for a superseded round probes for marker-confirmed output and archives it before removing the review bundle or worktree. |

---

# Appendix B — Example End-to-End Run

*Informative.* Exact command syntax may differ slightly in the implementation.

```bash
# In a plain Herdr shell pane, initialize a consuming repository.
agent-squad init

# Verify local Git and Herdr integration.
agent-squad doctor
agent-squad doctor --live-reviewer

# Start a run with Codex as Implementer and Claude Code as Reviewer.
agent-squad start \
  --task .scratch/example/spec.md \
  --implementer codex-main \
  --reviewer claude \
  --base origin/main
```

The Developer and Codex discuss and implement normally.

Codex then runs:

```bash
agent-squad submit \
  --mode new_revision \
  --report .agent-squad/drafts/implementation-report.md
```

Agent Squad:

1. verifies the fixed base is an ancestor of the current head;
2. records the request;
3. creates the detached review worktree;
4. creates the self-contained bundle;
5. launches Claude Code through Herdr;
6. sends the request prompt.

Claude Code writes:

```text
.agent-squad-review/output/review.json
.agent-squad-review/output/review.md
```

and runs:

```bash
agent-squad review-submit
```

If the result prompt is lost, Codex or the Developer can run:

```bash
agent-squad status
```

and see:

```text
Marker-confirmed review result ready but not applied.
Run:
  agent-squad apply-review --result-id <result-id>
```

After applying a `changes_requested` result, Codex writes a complete response and commits the fix:

```bash
agent-squad submit \
  --mode new_revision \
  --report .agent-squad/drafts/implementation-report.md \
  --response .agent-squad/drafts/response.json
```

If Codex rejects a finding without changing code:

```bash
agent-squad submit \
  --mode reconsideration \
  --report .agent-squad/drafts/implementation-report.md \
  --response .agent-squad/drafts/response.json
```

If a decision requires the Developer:

```bash
agent-squad escalate \
  --response .agent-squad/drafts/response.json \
  --note .agent-squad/drafts/decision-needed.md
```

The Developer records a resolution:

```bash
agent-squad resume \
  --resolution .agent-squad/drafts/resolution.md \
  --applies-to-finding REV-003 \
  --extend-rounds 1
```

The Implementer then updates the complete response so the former `needs_human` disposition becomes `fixed` or evidence-based `rejected`, identifies the earlier response and the Developer resolution, and submits again:

```bash
agent-squad submit \
  --mode new_revision \
  --report .agent-squad/drafts/implementation-report.md \
  --response .agent-squad/drafts/response.json
```

Agent Squad preserves the escalation-time response under the reviewed round's `diagnostics/replaced-responses/` directory and places the superseding response and the Developer resolution in the next fresh Reviewer bundle.

When the exact revision is approved:

```bash
agent-squad complete
```

The Developer then decides whether and how to merge or push.
