"""The forge and Herdr steps of §16.3, with scripted reviews and decisions."""

from __future__ import annotations

import json
import time

from tests.forge_support import ForgeFixture, finding


def run_smoke() -> dict:
    started = time.monotonic()
    with ForgeFixture() as f:
        f.initialize()
        diagnosed = f.cli("doctor")
        assert diagnosed["ok"]
        checks = {d["check"] for d in diagnosed["diagnostics"]}
        assert {
            "implementer forge identity", "reviewer forge identity",
            "Reviewer write permission", "disposable worktree",
            "installed code-review", "orphan resources",
        } <= checks
        head = f.candidate()
        f.create_pr()
        f.cli("reviewer", "launch", "--pr", "1")
        assert f.cli("issue", "view", "--issue", "1")["labels"] == [
            "ready-for-agent"
        ]
        f.review(
            "changes_requested",
            [
                finding(title="First blocking"),
                finding(title="Second blocking"),
                finding("optional", "Advisory finding"),
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
        f.reply(
            "REV-1", "DISPOSITION needs-human\n\nScripted policy question."
        )
        gated = f.status()
        assert (
            gated["gates"]["needs_decision"]
            and gated["next_action"] == "needs_decision"
        )
        f.cli("reviewer", "launch", "--pr", "1", expected=4)
        f.cli(
            "thread",
            "resolve",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--finding",
            "REV-1",
            expected=4,
        )
        f.decision(fid="REV-1")
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
        before_push = f.status()
        # The earlier needs-human disposition still has its Developer
        # decision; the new fixed disposition takes effect after the push.
        assert before_push["next_action"] == "push"
        assert any(
            d["kind"] == "invalid_disposition"
            for d in before_push["diagnostics"]
        )
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
        for fid in ("REV-1", "REV-2"):
            f.cli(
                "thread",
                "resolve",
                "--as",
                "reviewer",
                "--pr",
                "1",
                "--finding",
                fid,
            )
        f.review("approved")
        assert f.status()["next_action"] == "approved"
        f.cli("reviewer", "close", "--pr", "1")
        head = f.push("value = 1\nsecond = 20\nthird = 30\n")
        assert f.status()["next_action"] == "launch_review"
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
        f.cli("reviewer", "launch", "--pr", "1", expected=4)
        f.cli("reviewer", "close", "--pr", "1")
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
        approved = f.status()
        assert approved["next_action"] == "approved"
        assert approved["budget"] == {
            "effective": 4,
            "used": 4,
            "remaining": 0,
        }
        f.herdr_settings(prompt_failure=True)
        f.cli(
            "handoff",
            "review-result",
            "--pr",
            "1",
            "--head",
            head,
            "--verdict",
            "approved",
            expected=1,
        )
        lost = f.status()
        assert lost["current_review_unacted"]["head"] == head
        assert lost["next_action"] == "approved"
        f.herdr_settings(prompt_failure=False)
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
        assert_merge_cleanup(f, 1, "issue-1")

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
        f.cli(
            "pr", "create", "--as", "implementer", "--issue", "2",
            "--task", f.task, "--report", f.report,
        )
        f.cli("reviewer", "launch", "--pr", "2")
        f.cli(
            "review", "post", "--as", "reviewer", "--pr", "2",
            "--head", second_head, "--base", review_base,
            "--verdict", "approved", "--body", f.review_body,
            "--threads", f.write("second-threads.json", "[]"),
        )
        squashed = f.cli(
            "pr", "merge", "--as", "implementer", "--pr", "2", cwd=f.repo,
        )
        assert squashed["integration"] == "verified by tree identity"
        assert_merge_cleanup(f, 2, "issue-2")
        commands = list(f.history)
    # Step 11 uses an independent disposable PR so it cannot invalidate the
    # completed approval/merge exercises above.
    with ForgeFixture() as f:
        f.initialize()
        f.candidate()
        f.create_pr()
        f.settings(reject_batch=True, fail_roots=["REV-1"])
        f.review(
            "changes_requested",
            [
                finding(title="Fallback blocking"),
                finding("optional", "Fallback advisory"),
            ],
            expected=1,
        )
        partial = f.status()
        assert partial["next_action"] == "open_threads"
        assert partial["budget"]["used"] == 1
        f.settings(fail_roots=[])
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
        assert f.status()["next_action"] == "address_findings"
        f.settings(interrupt_after_body=True)
        inputs = [finding(title="Interrupted blocking")]
        f.review("changes_requested", inputs, expected=1)
        interrupted = f.status()
        review_id = interrupted["reviews"][-1]["id"]
        f.review("changes_requested", inputs, resume=review_id)
        recovered = f.status()
        assert recovered["budget"]["used"] == 2
        assert not recovered["gates"]["unanchored_findings"]
        assert (
            f.git(
                "ls-files",
                ".agent-squad",
                ".agent-squad-review",
                cwd=f.worktree,
            )
            == ""
        )
        assert f.git("status", "--porcelain") == ""
        commands.extend(f.history)
    return {
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
        "commands": commands,
        "cleanup": "owned temporary repository and all worktrees removed",
    }


def assert_merge_cleanup(f: ForgeFixture, pr: int, branch: str) -> None:
    assert not f.worktree.exists()
    assert f.git("branch", "--list", branch) == ""
    assert f.git("ls-remote", "--heads", "origin", branch) == ""
    assert not (f.repo / f".agent-squad/review-scratch/pr{pr}").exists()
    assert f.git("worktree", "list", "--porcelain").count("worktree ") == 1
    assert f.git("rev-parse", "HEAD") == f.base
    assert f.git("status", "--porcelain") == ""
