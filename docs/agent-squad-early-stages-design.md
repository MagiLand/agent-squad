# Agent Squad Early Stages Design

Status: proposal for issue #139 until merged. Merging approves the design; it changes no rule. The specification and the skills change only through the implementation issues of section 9.

Evidence baseline: `main` at `689e7a49fc8a5ff84148e6cff376f7dacb09c370`, with the forge read on 2026-11-10. Counts of issues cover numbers #1 to #159. A reference `§N` names a section of `docs/agent-squad-spec.md`.

## 1. Purpose and scope

This document designs how agents write and check two early stages of software work: the requirements brief and the design document. The ordering rule of §3.5 governs it: "a step is automated only once an independent check exists for what that step produces, and the Developer keeps authority over requirements, risk, and release." Each stage therefore has a writer, an independent check, and an artefact the Developer approves, the separation the implementation loop already has (§§3.7, 4.2, 4.3).

Stages 1 and 2 are designed in detail. Stage 3, chaining and second perspectives, is limited to preconditions (section 7). These bounds are the Developer's decisions of 2026-11-09, recorded in #139. Every design choice below is a recommendation for the Developer to accept or change when reviewing this pull request.

Two terms keep one meaning here. A **review** is a formal review of an exact commit under §7.3. A **brief check** is the stage 1 check of an issue body; it is not a review and spends no review budget (§7.8).

## 2. Evidence from practice

### Documents through the review loop

First-parent `main` holds 78 pull request merge commits. Eleven of those pull requests changed only Markdown files: #1, #41, #67, #70, #75, #79, #81, #125, #135, #140, and #141. Six of the eleven carry tagged reviews (`pr reviews`), nine reviews in all:

| Pull request | Document | Review passes | Findings | Blocking | Optional |
| --- | --- | --- | --- | --- | --- |
| #79 | Investigation record | 2: changes requested, approved | 4 | 2 | 2 |
| #81 | `v0.6.0` specification delta | 2: changes requested, approved | 6 | 2 | 4 |
| #125 | Verification record with specification changes | 2: changes requested, approved | 1 | 1 | 0 |
| #135, #140, #141 | Rule and specification text | 1 each: approved | 0 | 0 | 0 |

Those 11 findings carry the forge categories (§7.4) `spec` 6, `docs` 4, and `correctness` 1 (`status --pr` on #79, #81, #125). Nine were fixed. Two were rejected, #79 REV-4 as not pursued and #81 REV-6 as deferred to #60; they are also the only findings an approving pass raised. By kind, which is this document's own classification and not a forge field:

| Kind | Findings | Severity |
| --- | --- | --- |
| Claim contradicted by available evidence | #79 REV-1, REV-2 | Blocking |
| Claim with no cited evidence | #81 REV-3 | Blocking |
| Departure or consequence left unrecorded | #81 REV-1; #81 REV-5 | Blocking; optional |
| Required content missing | #125 REV-1 | Blocking |
| Two statements that disagree | #79 REV-4; #81 REV-6 | Optional |
| Rule or term left undefined | #81 REV-2, REV-4 | Optional |
| Evidence that will not stay checkable | #79 REV-3 | Optional |

All five blocking findings asked whether a statement was true, supported, recorded, or present. None was a preference about the design.

#67, #70, and #75 added and revised the `v0.6.0` plan. #139 calls them earlier than the tagged protocol, but #62, opened before them, carries a tagged review; what can be stated is that these three carry none. `pr reviews` and `status` return no inline comment for them. The `## Triage` comment on #139 records 2, 1, and 10 inline comments on them; this document neither recounted nor classified those comments. #41's review replies do not parse under the current grammar (`pr reviews --pr 41` reports five malformed lines), and #1 has no review.

The evidence has a limit: #135, #140, and #141 were approved with no findings, and nothing records whether those documents had no defects or whether defects went unseen.

### Requirements briefs

Of issues #1 to #159, 22 have a comment with a `## Triage` heading: #26, #28, #31, #111, #112, #114, #120, #121, #124, #126, #132, #139, #142, #144 to #151, and #157. Since #130 the body holds the brief and the comment holds verified evidence (`docs/agents/issue-tracker.md`, "Triage output"). Of those 22, twelve hold the brief in the body: #132 under its own headings, and 11 under an `## Agent Brief` heading (#139, #142, #144 to #151, and #157). All 11 have Category, Summary, Current behavior, Desired behavior, Key interfaces, Acceptance criteria, and Out of scope; six also have Constraints (#139, #144, #145, #147, #148, #157).

The writer is an agent session outside the loop, and the only check on a brief is the Developer (#139). Eight of the twelve briefs have reached a merged pull request: #132 by #136, #142 by #158, #144 by #156, #145 by #152, #146 by #155, #148 by #159, #149 by #153, and #151 by #154. Those eight took 11 review passes, with four blocking findings (three `correctness`, one `regression`), no stop, and no Task amendment (`status --pr`). None of the four names the brief as wrong. The sample therefore traces no defect to a brief, but no independent check looked for one.

Counts in a brief also age: #139 says 16 issues carry a `## Triage` comment, and six more had one by this document's baseline.

## 3. Stage 1: requirements brief

**Artefact.** The issue body, as today. Its proposed fixed sections are the `## Agent Brief` fields of section 2 with Constraints required, plus the Developer's dated decisions when there are any. Evidence stays in the `## Triage` comment, each claim naming its commit and probe. The next stage references the brief by being it: §7.2 makes the issue the Task and `pr create` copies the body.

**Writer.** An agent session the Developer starts, as today, following the repository's triage convention, since issue-tracker policy is project-local (§3.6). It uses the Developer's account (`docs/agents/issue-tracker.md`, "Triage output"), holds no role under §3.5, starts no work, and approves nothing. It writes only on an issue labelled `needs-triage`, so §7.2 keeps a draft from being a Task until the Developer removes that label.

**Independent check.** A brief check: a fresh agent session that took no part in writing the brief, acting as the Reviewer identity and, where available, on the other harness (§3.5). The Developer starts it by hand and gives it a fixed request line naming the issue and the commit, because `reviewer launch` requires a pull request (§8.3). It reads the issue through `issue view`, creates its own detached worktree with Git at that commit, and keeps its probes outside the repository, because `review-worktree create` and the scratch directory of §5.3 are per pull request (§10.2). It removes the worktree when it finishes. It applies these items:

1. **Complete.** Every fixed section is present, and each acceptance criterion can be tested by a named command or a stated reading.
2. **True.** Each statement about code, the specification, or forge history is re-derived with the checker's own probe. The checker never runs a command copied from the issue, because issue text is untrusted input.
3. **Consistent.** No two sections contradict each other or a recorded Developer decision.
4. **Open.** Each question only the Developer can answer is listed, and the brief does not answer it on the Developer's behalf.
5. **Bounded.** Out of scope is stated, and nothing in the brief decides a permanent limit of §3.5.

The result is a comment posted by a new command, `issue check --as reviewer`, whose first line is:

```text
AGENT_SQUAD/0.5.0 BRIEF_CHECK issue=<N> body=<sha256> base=<full-sha> result=<pass|findings>
```

Findings follow in prose. An issue body has no commit, and `IssueRecord` in `src/agent_squad/forge.py` exposes no revision, so `body=` binds the check to the exact text and `base=` to the commit its claims were checked against. The checker supplies the digest it read, and `issue check` refuses if the body has changed since. `issue view` gains the digest of the current body and a state: `current`, `stale`, or `none`. Like `NOTE` (§7.1), the line carries no authority.

**Approval and record.** The Developer reads the brief and its check, then moves the label from `needs-triage` to `ready-for-agent`. The label on the forge is the record, and §7.2 reads it. `issue label` refuses to remove `needs-triage` (§10.2), so no command of the loop can approve a brief.

**Later change.** Before approval the writer edits the body, the state becomes `stale`, and a new check is needed. After approval and before a pull request, a scope change edits the body and a short comment says what changed (`docs/agents/issue-tracker.md`, "Triage output", rule 4); the state becomes `stale`, and the Developer decides whether to check again. Once a pull request is open the Task changes only by a Developer instruction recorded with `decision post --task` (§§7.2, 7.6).

## 4. Stage 2: design document

**Artefact.** A Markdown file at the path its Task names, as `docs/agent-squad-early-stages-design.md` is here; a preferred location for such documents stays project-local (§3.6). Its fixed sections are: purpose and scope; evidence from practice; the design; answers to the design questions, each with alternatives, a recommendation, and a reason; rule changes, quoted exactly; open questions for the Developer; and implementation issues. An implementation Task references it with one line, `Design: <path> at <full-sha>`, where the object ID (§6.2) is the merge commit.

**Writer.** The Implementer, through the existing worktree, pull request, and report (§§4.2, 7.2), as on #79, #81, and #125. In a document posture, `Validation performed` gives the command or reading behind each count and citation.

**Independent check.** A fresh Reviewer at the exact head (§§4.3, 7.10), using the existing threads and the `spec` and `docs` categories (§7.4), and applying the checklist below in place of §12.5's lists. The posture applies when the Task says `Review posture: document`. A Markdown-only diff is not a sufficient signal, because #135, #140, and #141 changed rules rather than proposing a design.

**Approval and record.** The merge is the approval. A design document leaves design questions to the Developer, so item 6 of the review-before-merge rule (§12.2 rule 8) holds it and no standing instruction is recorded; the Developer releases the hold after seeing it (§7.10). The merge commit and the review's merge hold are the record. #147, open at this document's baseline, proposes a `hold-release` decision that would also record the release.

**Later change.** An approved document changes only through a later pull request under the same posture and hold. An implementation that must depart from it needs a `DECISION` on its own pull request naming the section it departs from (§7.6). The Reviewer treats the cited revision as part of the effective Task unless such a decision says otherwise.

**Document-review checklist.**

| Item | The Reviewer checks | Blocking |
| --- | --- | --- |
| Complete | Every required section is present; each question has alternatives, a recommendation, and a reason | Yes |
| True | Each cited section, command, pull request, and issue says what the document claims, read or run at the reviewed head | Yes, for a false or uncheckable claim |
| Traced | Each decision names the issue or Developer statement that made it, and each departure from a source is recorded | Yes |
| Consistent | No two statements contradict each other; each term keeps one meaning; a conflict with a permanent limit is stated | Yes, for a contradiction in a rule or an unstated conflict; otherwise optional |
| Checked | Each stage proposed for automation names its independent check | Yes |
| Feasible | Each implementation issue is one reviewable increment and says what it changes; no capability the tool lacks is relied on without an issue that adds it | Yes |
| Bounded | The Task's length and scope limits hold | Yes |
| Durable | Cited evidence stays checkable after the merge | Optional |

A design the Reviewer believes is wrong is reported as a finding that states the failure it would cause. It is blocking only when it shows the design cannot work as written. A preference for another design or other wording is never blocking. A question only the Developer can settle stays `needs_human`, or a `STOPPED` with `design` or `judgement` (§7.7).

The two axes stay (§12.4). The `code-review` skill's Standards axis carries a code-smell baseline (#139, `## Triage` comment), so in this posture it reports the repository's documented conventions only and states that the baseline does not apply.

## 5. Answers to the six design questions

"Developer only" marks a choice this document can only recommend.

| Question | Answered in | Alternatives | Recommended | Reason | Developer only |
| --- | --- | --- | --- | --- | --- |
| 1. Artefacts | Sections 3, 4 | Brief: (a) issue body; (b) a file reviewed by pull request. Design: (c) file at a Task-named path; (d) a fixed directory | (a) and (c) | `pr create` copies only the body (§7.2), and (b) adds a pull request and its CI run for each brief; (d) would encode a location that §3.6 leaves project-local. Cost of (a): no commit, flagged in section 6 | Yes |
| 2. Review criteria | Sections 3, 4 | (a) extend §12.5's lists; (b) add a document counterpart to §12.5; (c) restate a checklist in each Task | (b) | The five blocking findings of section 2 concerned statements, which §12.5's code-oriented lists reach only as requirement violations; (c) leaves the criteria to be retyped and to drift | No |
| 3. Approval | Sections 3, 4 | Brief: (a) label move; (b) a digest-bound Developer comment. Design: (c) merge under a hold; (d) a separate sign-off before the merge | (a) and (c) | §7.2 already reads labels, and under it a later Developer comment can stop an issue being the Task. The hold of (c) already exists; (d) adds a record that says what the merge says | Yes |
| 4. Roles and skills | Sections 3, 4 | (a) two roles with postures, the brief writer staying a Developer-started session; (b) a third role with its own identity; (c) the Implementer writes briefs | (a): writers use the Developer's account (brief) and the Implementer identity (design); checks use the Reviewer identity | §3.5 fixes two roles, and (b) needs new configuration and a §4.6 decision. Under (c) the agent that builds would have written its own requirements | Yes |
| 5. Specification limits | Section 6 | (a) narrow §§4.6 and 19 only where a recommendation needs it; (b) remove those limits; (c) keep them and leave both stages outside the tool | (a) | §3.5 makes each change a recorded Developer decision, so it should be as small as the design allows; (c) leaves the briefs unchecked | Yes |
| 6. Evidence | Section 5, below | (a) this pull request's trial record alone; (b) counts from past pull requests; (c) a supervised trial with seeded defects | (c) | Section 2 shows that an approval with no findings cannot tell a sound document from a missed defect. Only a known defect measures the check | Thresholds only |

**The trial of question 6.** At least three real briefs and two real design documents go through their checks. Disposable copies of a brief and of a design document each receive one seeded defect per blocking item. The record follows §16.5 under `docs/verification/` and states each defect caught and missed, each finding the Developer rejected, and where each approval is visible on the forge. A stage becomes a release claim when its check catches every seeded false citation and contradiction, and when each miss of another kind has been reported to the Developer.

## 6. Specification changes

Each sentence below would change in the direction given.

1. §3.5: "a concrete implementation-review protocol;" Extend it to name design documents under the same protocol.
2. §4.3: "reviews independently along the two axes of the installed `code-review` skill (§12.4);" Add the document-review checklist when the Task selects that posture.
3. §4.6: "The current release does not introduce a third planning or management agent." Qualify it: a Developer-started session drafting a brief is no such agent, because it holds no role, starts nothing, and approves nothing. Developer only.
4. §10.2, `issue view`: "issue title, body, labels, comments, and for each comment `agent_note`, true exactly when its first line is the `NOTE` line of §7.1, which the human-readable output also names;" Add the body digest and the check state. A new row adds `issue check`.
5. §12.1: "Each `SKILL.md` starts with frontmatter naming the skill and describing when to use it: `squad-implementer` triggers when the Developer invokes it and says "Let's start on issue #N"; `squad-reviewer` triggers only through the request line of §8.3." Add a second fixed request line for a brief check.
6. §12.5: "The Reviewer SHOULD examine the following where relevant:" Limit it to implementations, and point to the document counterpart.
7. §12.5: "Blocking findings SHOULD be limited to:" Limit it in the same way.
8. §19: "The current release is not an autonomous software-development platform, an issue-triage tool, or a general task scheduler: it does not plan work, triage issues, or schedule tasks." Narrow it: the tool may check a brief at the Developer's request, and it still does not decide triage. Developer only.

§7.1 gains a paragraph defining `BRIEF_CHECK` as a new kind of line under the unchanged tag, valid only from the Reviewer identity. §4.3 gains a paragraph stating that a brief check uses the Reviewer identity without being a review pass, so its account of how a Reviewer is launched stays true. §§12.2 (rule 2), 12.3 (rules 1 and 5), and 12.4 only gain text for the document posture, the brief-check request, and the Standards-axis rule; none of their sentences changes. §20 gains an entry recording the Developer's decisions, as §3.5 and §19 require, and noting one exception to its earlier statement that "the Reviewer gains no issue-writing ability".

These stay as written: "The Implementation Review Squad is the only implemented squad." (§3.5), since a document is a type of work in the one loop; the two roles (§3.5); the Developer's responsibilities and the warning against a generic `Workflow` or `StepGraph` abstraction (§3.7). §19's first paragraph also stands: this design adds no "documentation pipeline", because the Developer starts each document and each one is reviewed singly, and no "automatic requirement/design decisions", because agents draft and the Developer decides.

The three permanent limits of §3.5 stay as they are:

- **Review of an exact commit by an independent Reviewer.** Unchanged. **Flag:** a brief check has no commit to bind to, so it is bound to a body digest and a named base commit. It adds a check and replaces none; every change that reaches the repository is still reviewed at its exact head.
- **The forge as the record of authority.** Unchanged. Checks, labels, reviews, and merges are all on the forge.
- **The Developer's authority.** Unchanged. The Developer moves the label and releases the hold.

## 7. Stage 3

Chaining and second perspectives are not designed here. Designing them would be justified only when all of these hold:

1. The trial of section 5 has met its thresholds for both stages, and both have shipped.
2. At least three merged design documents have had their implementation issues filed by hand, and those filings show the same mechanical steps. §3.7 allows shared abstractions only once real requirements appear.
3. Forge counts, or the event store of #148 (`docs/events.md`), show that the filing costs enough Developer turns to be worth removing.
4. For second perspectives, the trial or later records show blocking defects that one Reviewer missed and a second perspective would have caught.

## 8. Open questions for the Developer

1. Is a Developer-started session that drafts briefs a "third planning or management agent" under §4.6? Section 6 assumes not.
2. Should a `stale` or absent brief check stop the Implementer, or only be reported? This document recommends reporting only until the trial shows the check's value, because stopping would change §7.2's readiness conditions.
3. Does the label move need a verified author? `issue view` reports labels without actor or time (§10.2), so the CLI cannot show who moved one. The writer uses the Developer's account, so only its instructions keep it from moving the label by other means.
4. Should every agent-written brief get a check, or only those above some size? Each check costs one agent session, and section 2 shows no defect traced to a brief so far.
5. Should the 13 inline comments on #67, #70, and #75 be classified? That needs a forge read the loop's roles do not have.

## 9. Implementation issues for the next release

In dependency order. Issues 1 to 4 change the rules agents work under, so item 3 of the review-before-merge rule holds them.

**1. Record the early-stage decisions in the specification** (stages 1 and 2).
Apply section 6's changes to §§3.5, 4.6, and 19, and add the §20 entry recording what the Developer decided on this document, including its answers to section 8.
*Changes:* specification. *Depends on:* this document's merge.

**2. Add the document posture to the skills** (stage 2).
`squad-reviewer` gains the checklist, the `Review posture: document` trigger, and the Standards-axis rule. `squad-implementer` gains the report guidance and the `Design: <path> at <full-sha>` reference.
*Changes:* both skills; specification §§4.3, 12.2, 12.3, 12.4, 12.5; `tests/unit/test_skills.py`. *Depends on:* 1.

**3. Report the brief digest and record a brief check** (stage 1).
`issue view` reports the body's SHA-256 and the check state. A new `issue check --as reviewer --issue <N> --body-sha256 <digest> --base <full-sha> --result <pass|findings> --body <file>` posts the `BRIEF_CHECK` line and refuses a body whose digest differs.
*Changes:* CLI, including both forge adapters and fakes; specification §§7.1, 10.2; unit and integration tests. *Depends on:* 1.

**4. Add the brief check to the Reviewer skill** (stage 1).
`squad-reviewer` gains a second fixed request line naming the issue and base commit, the five items of section 3, the rule that the checker writes its own probes, and how it creates and removes its detached worktree.
*Changes:* `squad-reviewer`; specification §§12.1, 12.3; `tests/unit/test_skills.py`. *Depends on:* 3.

**5. Run the supervised trial of both stages** (stages 1 and 2).
Carry out the trial of section 5 and commit its record. The Developer decides from the record whether each stage becomes a release claim.
*Changes:* one new file under `docs/verification/`. *Depends on:* 2 and 4.
