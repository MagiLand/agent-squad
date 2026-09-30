"""Scripted baseline smoke plus the single-identity approval scenario."""

from __future__ import annotations

import json
from pathlib import Path
import time

from tests.forge_support import ForgeFixture, ForgejoFixture, finding
from agent_squad.conventions import MERGE_INSTRUCTION


def initialize(f: ForgeFixture) -> None:
    if not isinstance(f, ForgejoFixture):
        f.initialize()
        return
    token = f.root / "developer.token"
    token.write_text("fake-token-developer\n")
    token.chmod(0o600)
    f.cli(
        "init", "--forge", "forgejo", "--base-url", f.server.base_url,
        "--implementer-account", "developer",
        "--reviewer-account", "developer",
        "--implementer-token-file", str(token),
        "--reviewer-token-file", str(token),
        "--identity-mode", "single", "--approver-account", "human",
    )


def human_review(f: ForgeFixture, event: str, *, pr: int = 1,
                 login: str = "human") -> dict:
    """Seed scripted browser evidence only in the disposable fake model."""
    from tests.fixtures.fake_forgejo import Handler

    model = f.read_model()
    row = Handler.record(model, login, "Scripted human review.")
    row.update(state=event, dismissed=False, submitted_at=row["created_at"],
               commit_id=f.git("rev-parse", "HEAD", cwd=f.worktree))
    model["prs"][str(pr)]["reviews"].append(row)
    f.save_model(model)
    return row


def require_human(f: ForgeFixture, *, pr: int = 1) -> None:
    if not isinstance(f, ForgejoFixture):
        return
    state = f.cli("status", "--pr", str(pr))
    assert state["next_action"] == "await_human_approval"
    assert f.read_model()["prs"][str(pr)]["reviews"][-1]["state"] == "COMMENT"
    f.cli("pr", "merge", "--as", "implementer", "--pr", str(pr), expected=4)
    human_review(f, "APPROVED", pr=pr)


def run_scenario(fixture: type[ForgeFixture]) -> dict:
    started = time.monotonic()
    steps = []
    roots = []
    with fixture() as f:
        roots.append(f.root)
        initialize(f)
        diagnosed = f.cli("doctor")
        assert diagnosed["ok"]
        checks = {d["check"] for d in diagnosed["diagnostics"]}
        assert {
            "implementer forge identity", "reviewer forge identity",
            "Reviewer write permission", "disposable worktree",
            "installed code-review", "orphan resources",
        } <= checks
        steps.append({"step": 1, "result": "init and full doctor passed"})
        head = f.candidate()
        issue_scratch = Path(
            f.cli("issue", "view", "--issue", "1")["paths"]["issue_scratch"])
        issue_scratch.mkdir()
        (issue_scratch / "report.md").write_text("saved report")
        f.create_pr()
        created = f.status()
        assert created["target"]["head"] == head
        assert "## Task\n" in created["pr"]["evidence"]["body"]
        assert "## Implementation report\n" in (
            created["pr"]["evidence"]["body"]
        )
        steps.append({"step": 2, "result": "pushed issue worktree and PR"})
        f.cli("reviewer", "launch", "--pr", "1")
        assert f.cli("issue", "view", "--issue", "1")["labels"] == [
            "ready-for-agent"
        ]
        f.review(
            "changes_requested",
            [
                finding(title="First blocking"),
                finding(title="Second blocking", line=3),
                finding("optional", "Advisory finding", line=1),
            ],
        )
        f.cli(
            "handoff",
            "review-result",
            "--pr",
            "1",
            "--head",
            head,
            "--verdict",
            "changes_requested",
        )
        assert f.status()["next_action"] == "address_findings"
        steps.append({"step": 3, "result": "three findings and handoff"})
        f.reply(
            "REV-1", "DISPOSITION needs-human\n\nScripted policy question."
        )
        gated = f.status()
        assert (
            gated["gates"]["needs_decision"]
            and gated["next_action"] == "needs_decision"
        )
        refused = f.cli("reviewer", "launch", "--pr", "1", expected=4)
        assert "needs_decision" in refused["error"]
        resolution = f.cli(
            "thread",
            "resolve",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--finding",
            "REV-1",
            expected=1 if isinstance(f, ForgejoFixture) else 4,
        )
        if isinstance(f, ForgejoFixture):
            assert "not supported on this forge" in resolution["error"]
            assert not gated["capabilities"]["can_resolve_threads"]
        f.decision(fid="REV-1")
        assert not f.status()["gates"]["needs_decision"]
        previous = head
        head = f.commit("value = 1\nsecond = 20\nthird = 3\n")
        f.reply(
            "REV-1",
            f"DISPOSITION fixed {head}\n\nScripted fix; python -m unittest.",
        )
        f.reply(
            "REV-2",
            "DISPOSITION rejected\n\nThe scripted example already meets the"
            " requirement.",
        )
        f.reply(
            "REV-3",
            "DISPOSITION rejected\n\nNot pursued: the advisory improvement"
            " is unnecessary for this fixture.",
        )
        before_push = f.status()
        # The earlier needs-human disposition still has its Developer
        # decision; the new fixed disposition takes effect after the push.
        assert before_push["next_action"] == "push"
        assert any(
            d["kind"] == "invalid_disposition"
            for d in before_push["diagnostics"]
        )
        steps.append({"step": 4, "result": "dispositions and decision gate"})
        f.git("push", "origin", "HEAD", cwd=f.worktree)
        assert f.status()["next_action"] == "launch_review"
        f.cli(
            "pr",
            "report",
            "--as",
            "implementer",
            "--pr",
            "1",
            "--report",
            f.report,
        )
        f.cli("reviewer", "close", "--pr", "1", "--head", previous)
        f.cli("reviewer", "launch", "--pr", "1")
        f.reply(
            "REV-1",
            "VERIFIED fixed\n\nScripted execution evidence.",
            "reviewer",
        )
        f.reply(
            "REV-2",
            "VERIFIED rejection accepted\n\nScripted execution evidence.",
            "reviewer",
        )
        f.reply(
            "REV-3",
            "VERIFIED rejection accepted\n\nScripted advisory checked.",
            "reviewer",
        )
        for fid in ("REV-1", "REV-2", "REV-3"):
            f.cli(
                "thread",
                "resolve",
                "--as",
                "reviewer",
                "--pr",
                "1",
                "--finding",
                fid,
                expected=1 if isinstance(f, ForgejoFixture) else 0,
            )
        f.review("approved")
        require_human(f)
        approved = f.status()
        assert approved["next_action"] == "approved"
        assert approved["approval"]["approved"]
        assert approved["target"]["head"] == head
        assert all(t["settled"] for t in approved["findings"]
                   if t["severity"] == "blocking")
        if isinstance(f, ForgejoFixture):
            requested = human_review(f, "REQUEST_CHANGES")
            vetoed = f.status()
            assert not vetoed["approval"]["approved"]
            veto = vetoed["human_request_changes"][0]
            assert veto["login"] == "human" and veto["commit_id"] == head
            assert veto["id"] == requested["id"]
            f.cli("pr", "merge", "--as", "implementer", "--pr", "1",
                  expected=4)
            human_review(f, "APPROVED")
            assert f.status()["next_action"] == "approved"
        steps.append({"step": 5, "result": "verified and approved"})
        f.cli("reviewer", "close", "--pr", "1")
        head = f.push("value = 1\nsecond = 20\nthird = 30\n")
        invalidated = f.status()
        assert invalidated["next_action"] == "launch_review"
        assert not invalidated["approval"]["approved"]
        steps.append({"step": 6, "result": "later push invalidated approval"})
        f.cli("reviewer", "launch", "--pr", "1")
        f.review("changes_requested", [finding(title="Budget finding")])
        f.cli(
            "stop",
            "post",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--head",
            head,
            "--reason",
            "budget",
            "--body",
            f.write("stop.md", "Scripted budget exhausted."),
        )
        assert f.status()["next_action"] == "stopped"
        f.cli(
            "handoff",
            "stopped",
            "--pr",
            "1",
            "--head",
            head,
            "--reason",
            "budget",
        )
        refused = f.cli("reviewer", "launch", "--pr", "1", expected=4)
        assert "stopped" in refused["error"]
        assert "budget_exhausted" in refused["error"]
        f.cli("reviewer", "close", "--pr", "1")
        assert f.status()["budget"]["used"] == 3
        steps.append({"step": 7, "result": "budget stop and refused launch"})
        f.decision(budget=4)
        f.reply(
            "REV-4",
            "DISPOSITION rejected\n\nScripted reconsideration evidence.",
        )
        assert f.status()["next_action"] == "launch_review"
        f.cli("reviewer", "launch", "--pr", "1")
        f.reply(
            "REV-4",
            "VERIFIED rejection accepted\n\nScripted probe passed.",
            "reviewer",
        )
        f.review("approved")
        require_human(f)
        approved = f.status()
        assert approved["next_action"] == "approved"
        assert approved["budget"] == {
            "effective": 4,
            "used": 4,
            "remaining": 0,
        }
        assert approved["optional_findings"][0]["disposition"]["value"] == (
            "rejected"
        )
        assert not approved["gates"]["unaddressed_findings"]
        steps.append({"step": 8, "result": "budget extended; fourth approved"})
        f.cli("reviewer", "close", "--pr", "1")
        # Step 9: the Developer scripts acceptance of a moved base for merge.
        advance = f.root / "advance"
        f.git("clone", str(f.origin), str(advance))
        (advance / "base-only.txt").write_text("base advanced\n")
        f.git("add", "base-only.txt", cwd=advance)
        f.git("commit", "-m", "test: move smoke base", cwd=advance)
        f.git("push", "origin", "main", cwd=advance)
        f.cli("pr", "merge", "--as", "implementer", "--pr", "1", expected=4)
        # Leave a closed review checkout as residue to exercise merge cleanup.
        f.cli("review-worktree", "create", "--pr", "1", "--head", head)
        merged = f.cli(
            "pr", "merge", "--as", "implementer", "--pr", "1",
            "--accept-moved-base", cwd=f.repo,
        )
        assert merged["integration"] == "verified by ancestry"
        assert_merge_cleanup(f, 1, "issue-1", issue=1)

        # A second PR in this same repository exercises unmoved-base squash.
        model = f.read_model()
        model["issues"]["2"] = dict(model["issues"]["1"], id=2, number=2)
        f.save_model(model)
        config_path = f.repo / ".agent-squad/config.json"
        config = json.loads(config_path.read_text())
        config["merge_method"] = "squash"
        config_path.write_text(json.dumps(config))
        review_base = f.git("rev-parse", "origin/main")
        f.worktree = f.repo / ".agent-squad/worktrees/issue-2"
        f.git(
            "worktree", "add", "-b", "issue-2", str(f.worktree), "origin/main"
        )
        second_head = f.push("value = 100\nsecond = 20\nthird = 30\n")
        issue_scratch = Path(
            f.cli("issue", "view", "--issue", "2")["paths"]["issue_scratch"])
        issue_scratch.mkdir()
        (issue_scratch / "report.md").write_text("second saved report")
        f.cli(
            "pr", "create", "--as", "implementer", "--issue", "2",
            "--task", f.task, "--report", f.report,
        )
        f.cli("reviewer", "launch", "--pr", "2")
        body = Path(f.review_body)
        body.write_text(body.read_text() +
                        "\n## Merge hold\n\nItem 3: merge rules.\n")
        f.cli(
            "review", "post", "--as", "reviewer", "--pr", "2",
            "--head", second_head, "--base", review_base,
            "--verdict", "approved", "--body", f.review_body,
            "--threads", f.write("second-threads.json", "[]"),
        )
        require_human(f, pr=2)
        held = f.cli("status", "--pr", "2")
        assert held["next_action"] == "approved"
        refused = f.cli("pr", "merge", "--as", "implementer", "--pr", "2",
                        cwd=f.repo, expected=4)
        review_id = held["merge_hold"]["review_id"]
        assert f"merge hold in review {review_id}" in refused["error"]
        squashed = f.cli(
            "pr", "merge", "--as", "implementer", "--pr", "2",
            "--accept-merge-hold", cwd=f.repo,
        )
        assert squashed["integration"] == "verified by tree identity"
        assert_merge_cleanup(f, 2, "issue-2", issue=2)
        steps.append({"step": 9,
                      "result": "merge and squash verified; cleaned"})
        commands = list(f.history)
    assert not roots[-1].exists()
    # Recovery uses independent disposable PRs after the merge exercises.
    with fixture() as f:
        roots.append(f.root)
        initialize(f)
        head = f.candidate()
        f.create_pr(issue_task=True)
        f.decision(body=MERGE_INSTRUCTION + '\n\n> Start issue #1.')
        f.cli("reviewer", "launch", "--pr", "1")
        f.review("approved")
        require_human(f)
        f.herdr_settings(prompt_failure=True)
        f.cli(
            "handoff", "review-result", "--pr", "1", "--head", head,
            "--verdict", "approved", expected=1,
        )
        lost = f.status()
        assert lost["current_review_unacted"]["head"] == head
        assert lost["next_action"] == "merge"
        assert lost["merge_instruction"] is not None
        assert lost["budget"]["used"] == 1
        f.herdr_settings(prompt_failure=False)
        f.cli("reviewer", "close", "--pr", "1")
        assert f.git("worktree", "list", "--porcelain").count("worktree ") == 2
        merged = f.cli("pr", "merge", "--as", "implementer", "--pr", "1",
                       cwd=f.repo)
        assert merged["integration"] == "verified by ancestry"
        assert_merge_cleanup(f, 1, "issue-1", issue=1)
        commands.extend(f.history)
        steps.append({"step": 10,
                      "result": "lost handoff recovered by status"})
    assert not roots[-1].exists()
    with fixture() as f:
        roots.append(f.root)
        initialize(f)
        f.candidate()
        f.create_pr()
        if isinstance(f, ForgejoFixture):
            f.settings(out_of_diff_accepted=True)
        else:
            f.settings(reject_batch=True, fail_roots=["REV-1"])
        f.review(
            "changes_requested",
            [
                finding(title="Fallback blocking"),
                finding("optional", "Fallback advisory", line=3),
            ],
            expected=1,
        )
        partial = f.status()
        if isinstance(f, ForgejoFixture):
            wire_roots = f.read_model()["prs"]["1"]["comments"]
            assert len(wire_roots) == 2
            assert all(root["diff_hunk"] == "" for root in wire_roots)
        assert partial["next_action"] == "open_threads"
        assert partial["budget"]["used"] == 1
        first_review = partial["reviews"][0]
        for title in ("Fallback blocking", "Fallback advisory"):
            assert title in first_review["body"]
        for field in ("Problem", "Evidence", "Impact", "Required change",
                      "Verification"):
            assert first_review["body"].count(f"**{field}**:") == 2
        f.settings(fail_roots=[], out_of_diff_accepted=False)
        f.cli(
            "thread",
            "open",
            "--as",
            "implementer",
            "--pr",
            "1",
            "--finding",
            "REV-1",
            "--path",
            "example.py",
            "--line",
            "2",
        )
        opened = f.status()
        assert opened["next_action"] == "address_findings"
        assert opened["budget"]["used"] == 1
        assert opened["reviews"][0]["id"] == first_review["id"]
        if isinstance(f, ForgejoFixture):
            # The unusable optional root also needs a valid recovery anchor.
            f.cli("thread", "open", "--as", "implementer", "--pr", "1",
                  "--finding", "REV-2", "--path", "example.py", "--line", "3")
            draft = human_review(f, "PENDING", login="developer")
            refused = f.review("changes_requested",
                               [finding(title="Interrupted blocking")],
                               expected=4)
            assert "pending_draft" in refused["error"], refused
            # Discard and publish the interrupted review in one command.
        f.settings(interrupt_after_body=True)
        inputs = [finding(title="Interrupted blocking")]
        f.review("changes_requested", inputs, expected=1,
                 **({"discard_draft": draft["id"]}
                    if isinstance(f, ForgejoFixture) else {}))
        interrupted = f.status()
        review_id = interrupted["reviews"][-1]["id"]
        if isinstance(f, ForgejoFixture):
            f.settings(interrupt_after_body=False)
            assert all(r["id"] != draft["id"]
                       for r in f.read_model()["prs"]["1"]["reviews"])
        f.review("changes_requested", inputs, resume=review_id)
        recovered = f.status()
        assert recovered["budget"]["used"] == 2
        assert [r["id"] for r in recovered["reviews"]] == [
            first_review["id"], review_id,
        ]
        assert not recovered["gates"]["unanchored_findings"]
        assert_no_tracked_runtime(f)
        assert f.git("worktree", "list", "--porcelain").count("worktree ") == 2
        assert f.git("status", "--porcelain") == ""
        commands.extend(f.history)
        steps.append({"step": 11,
                      "result": "fallback and resume preserved reviews"})
    assert all(not root.exists() for root in roots)
    steps.append({"step": 12, "result": "all temporary roots removed"})
    return {
        "forge": "forgejo" if fixture is ForgejoFixture else "github",
        "identity_mode": "single" if fixture is ForgejoFixture else "dual",
        "ok": True,
        "duration_seconds": round(time.monotonic() - started, 3),
        "scripted": [
            "reviews",
            "dispositions",
            "verifications",
            "decisions",
            "stop",
            "Herdr process states and messages",
        ],
        "command_count": len(commands),
        "commands": commands,
        "steps": steps,
        "cleanup": "owned temporary repository and all worktrees removed",
    }


def run_github_single() -> dict:
    roots = []
    commands = []
    with ForgeFixture() as f:
        roots.append(f.root)
        f.single_identity()
        assert f.cli("doctor")["ok"]
        f.candidate()
        f.create_pr()
        f.decision(body=MERGE_INSTRUCTION)
        f.review("approved")
        assert f.status()["next_action"] == "await_human_approval"
        f.cli("pr", "merge", "--as", "implementer", "--pr", "1", expected=4)
        requested = f.human_review("REQUEST_CHANGES")
        state = f.status()
        assert state["human_request_changes"][0]["id"] == requested["id"]
        assert state["next_action"] == "await_human_approval"
        f.push("value = 2\nsecond = 2\nthird = 3\n")
        assert f.status()["next_action"] == "launch_review"
        f.cli("reviewer", "launch", "--pr", "1")
        f.review("approved")
        assert f.status()["next_action"] == "await_human_approval"
        f.human_review("APPROVE")
        state = f.status()
        assert state["next_action"] == "merge"
        assert state["human_request_changes"] == []
        assert state["human_approvals"][0]["dismissed"]
        assert state["budget"]["used"] == 2
        result = f.cli("pr", "merge", "--as", "implementer", "--pr", "1",
                       cwd=f.repo)
        assert result["integration"] == "verified by ancestry"
        assert_merge_cleanup(f, 1, "issue-1", issue=1)
        commands.extend(f.history)
    assert all(not root.exists() for root in roots)

    return {"ok": True, "command_count": len(commands), "commands": commands}


def run_smoke() -> dict:
    scenarios = [run_scenario(ForgeFixture), run_scenario(ForgejoFixture)]
    single = run_github_single()
    return {
        "ok": True, "scenarios": scenarios, "github_single": single,
        "cleanup": "all owned temporary roots removed",
    }


def assert_merge_cleanup(
    f: ForgeFixture, pr: int, branch: str, *, issue: int
) -> None:
    assert not f.worktree.exists()
    assert f.git("branch", "--list", branch) == ""
    assert f.git("ls-remote", "--heads", "origin", branch) == ""
    assert f.git("branch", "-r", "--list", f"origin/{branch}") == ""
    assert not (f.repo / f".agent-squad/review-scratch/pr{pr}").exists()
    assert not (f.repo / f".agent-squad/review-scratch/issue-{issue}").exists()
    assert f.git("worktree", "list", "--porcelain").count("worktree ") == 1
    assert f.git("rev-parse", "HEAD") == f.git("rev-parse", "origin/main")
    assert f.git("status", "--porcelain") == ""
    assert_no_tracked_runtime(f)
    assert not f.herdr_model()["workspaces"]
    assert not any(a["name"].startswith("reviewer-pr")
                   for a in f.herdr_model()["agents"])


def assert_no_tracked_runtime(f: ForgeFixture) -> None:
    assert f.git("ls-files", ".agent-squad", ".agent-squad-review") == ""
