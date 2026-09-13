"""The forge steps of §16.3, with scripted reviews and decisions."""

from __future__ import annotations

import time

from tests.forge_support import ForgeFixture, finding


def run_smoke() -> dict:
    started = time.monotonic()
    with ForgeFixture() as f:
        f.initialize()
        f.cli("doctor")
        head = f.candidate()
        f.create_pr()
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
        assert f.status()["next_action"] == "address_findings"
        f.reply(
            "REV-1", "DISPOSITION needs-human\n\nScripted policy question."
        )
        gated = f.status()
        assert (
            gated["gates"]["needs_decision"]
            and gated["next_action"] == "needs_decision"
        )
        # Increment 2 adds reviewer launch. The derived gate is the check here.
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
        head = f.push("value = 1\nsecond = 20\nthird = 30\n")
        assert f.status()["next_action"] == "launch_review"
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
        f.decision(budget=4)
        f.reply(
            "REV-4",
            "DISPOSITION rejected\n\nScripted reconsideration evidence.",
        )
        assert f.status()["next_action"] == "launch_review"
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
        # Isolated recovery exercise follows an explicit scripted budget
        # extension.
        f.decision(budget=6)
        f.settings(reject_batch=True, fail_roots=["REV-5"])
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
        assert partial["budget"]["used"] == 5
        f.settings(fail_roots=[])
        f.cli(
            "thread",
            "open",
            "--as",
            "implementer",
            "--pr",
            "1",
            "--finding",
            "REV-5",
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
        assert recovered["budget"]["used"] == 6
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
        commands = list(f.history)
    return {
        "ok": True,
        "duration_seconds": round(time.monotonic() - started, 3),
        "scripted": [
            "reviews",
            "dispositions",
            "verifications",
            "decisions",
            "stop",
        ],
        "commands": commands,
        "cleanup": "owned temporary repository and all worktrees removed",
    }
