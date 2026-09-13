"""Reviewer recovery against real Git and the fake Herdr and forge."""

import json
from pathlib import Path
import sys
import unittest

from tests.forge_support import ForgeFixture, finding


class ReviewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ForgeFixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()
        self.path = (
            self.f.repo
            / f".agent-squad/worktrees/reviewer-pr1-{self.head[:7]}"
        )

    def lifecycle(self, command: str, expected: int = 0) -> dict:
        return self.f.cli("reviewer", command, "--pr", "1", expected=expected)

    def worktree(
        self, command: str, expected: int = 0, head: str | None = None
    ) -> dict:
        return self.f.cli(
            "review-worktree",
            command,
            "--pr",
            "1",
            "--head",
            head or self.head,
            expected=expected,
        )

    def test_launch_is_ordered_and_close_preserves_scratch(self) -> None:
        f = self.f
        result = self.lifecycle("launch")
        self.assertEqual(result["observed_state"], "working")
        self.assertEqual(result["worktree"], str(self.path))
        self.assertEqual(f.git("rev-parse", "HEAD", cwd=self.path), self.head)
        self.assertEqual(f.git("status", "--porcelain"), "")
        self.assertEqual(f.status()["next_action"], "reviewer_live")
        self.assertTrue(f.status()["gates"]["reviewer_live"])
        self.lifecycle("launch", expected=4)
        scratch = Path(result["scratch"]) / "probe.py"
        scratch.write_text("probe survives close")
        calls = f.herdr_model()["calls"]
        opened = next(
            i
            for i, c in enumerate(calls)
            if c[:2] == ["worktree", "open"] and "--help" not in c
        )
        started = next(
            i
            for i, c in enumerate(calls)
            if c[:2] == ["agent", "start"] and "--help" not in c
        )
        prompted = next(
            i
            for i, c in enumerate(calls)
            if c[:2] == ["agent", "prompt"] and "--help" not in c
        )
        self.assertLess(opened, started)
        self.assertLess(started, prompted)
        self.assertEqual(
            calls[opened],
            [
                "worktree",
                "open",
                "--cwd",
                str(f.repo),
                "--path",
                str(self.path),
                "--label",
                result["name"],
                "--no-focus",
            ],
        )
        self.assertEqual(
            calls[prompted],
            [
                "agent",
                "prompt",
                result["name"],
                (
                    f"/squad-reviewer pr=1 head={self.head} base={f.base}"
                    " implementer=implementer"
                ),
            ],
        )
        self.assertFalse(any("--wait" in c or "send-keys" in c for c in calls))
        self.lifecycle("close")
        self.assertFalse(self.path.exists())
        self.assertTrue(scratch.exists())
        self.assertEqual(f.status()["next_action"], "launch_review")
        self.assertEqual(f.herdr_model()["workspaces"], [])

    def test_create_reuses_clean_owned_worktree_and_refuses_dirty(
        self,
    ) -> None:
        self.assertFalse(self.worktree("create")["reused"])
        self.assertTrue(self.worktree("create")["reused"])
        (self.path / "untracked.txt").write_text("keep me")
        self.worktree("create", expected=3)
        (self.path / "example.py").write_text("dirty")
        self.worktree("remove")
        self.assertFalse(self.path.exists())

    def test_refuses_unowned_detached_worktree_at_matching_path_and_head(
        self,
    ) -> None:
        self.f.git("worktree", "add", "--detach", str(self.path), self.head)
        for command in ("create", "remove"):
            self.assertIn(
                "ownership", self.worktree(command, expected=3)["error"]
            )
        self.lifecycle("close", expected=3)
        self.assertTrue(self.path.exists())

    def test_refuses_directory_symlink_branch_and_changed_head(self) -> None:
        self.path.mkdir()
        self.worktree("create", expected=3)
        self.path.rmdir()
        self.path.symlink_to(self.f.worktree)
        self.worktree("remove", expected=3)
        self.path.unlink()
        self.f.git(
            "worktree", "add", "-b", "foreign", str(self.path), self.head
        )
        self.worktree("remove", expected=3)
        self.f.git("worktree", "remove", str(self.path))
        self.worktree("create")
        self.f.git("checkout", "--detach", self.f.base, cwd=self.path)
        self.worktree("remove", expected=3)
        self.assertTrue(self.path.exists())

    def test_full_head_identity_defeats_abbreviation_collision(self) -> None:
        self.worktree("create")
        wrong = self.head[:7] + "0" * (len(self.head) - 7)
        self.worktree("remove", expected=3, head=wrong)
        self.assertTrue(self.path.exists())

    def test_external_worktree_root(self) -> None:
        path = self.f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config["worktree_root"] = str(self.f.root / "outside")
        path.write_text(json.dumps(config))
        created = self.worktree("create")
        self.assertTrue(
            Path(created["path"]).is_relative_to(self.f.root / "outside")
        )
        self.worktree("remove")

    def test_start_arguments_and_initial_prompt_fallback(self) -> None:
        f = self.f
        path = f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config["reviewer"].update(
            kind="codex", start_args=["--add-dir", str(f.root)]
        )
        path.write_text(json.dumps(config))
        source = (
            "from agent_squad.herdr import REVIEW_DELIVERY; "
            "from agent_squad.initialization import AgentKind; "
            "from agent_squad.cli import main; "
            "REVIEW_DELIVERY[AgentKind.CODEX]='initial_prompt'; "
            "raise SystemExit(main(['reviewer','launch','--pr','1','--json']))"
        )
        result = f.run([sys.executable, "-c", source], cwd=f.worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["delivery"], "initial_prompt"
        )
        calls = f.herdr_model()["calls"]
        starts = [
            c
            for c in calls
            if c[:2] == ["agent", "start"] and "--help" not in c
        ]
        self.assertEqual(
            starts[0][-4:],
            [
                "--",
                "--add-dir",
                str(f.root),
                (
                    f"$squad-reviewer pr=1 head={self.head} base={f.base}"
                    " implementer=implementer"
                ),
            ],
        )
        self.assertFalse(
            any(
                c[:2] == ["agent", "prompt"] and "--help" not in c
                for c in calls
            )
        )
        self.lifecycle("close")

    def test_blocked_and_not_ready_leave_resources_and_adopt_creates_nothing(
        self,
    ) -> None:
        for settings in (
            {"start_state": "blocked"},
            {"start_not_ready": True},
            {"prompt_state": "blocked"},
        ):
            with self.subTest(settings=settings):
                self.f.herdr_settings(
                    start_state="idle",
                    start_not_ready=False,
                    prompt_state="working",
                    **{},
                )
                self.f.herdr_settings(**settings)
                result = self.lifecycle("launch", expected=3)
                self.assertIn("workspace=w", result["error"])
                self.assertIn("pane=w", result["error"])
                self.assertTrue(self.path.exists())
                self.lifecycle("adopt", expected=3)
                model = self.f.herdr_model()
                model["agents"][0]["agent_status"] = "idle"
                model["settings"].update(
                    start_not_ready=False,
                    start_state="idle",
                    prompt_state="working",
                )
                before = len(
                    [
                        c
                        for c in model["calls"]
                        if c[:2] == ["worktree", "open"]
                    ]
                )
                self.f.save_herdr(model)
                self.lifecycle("adopt")
                after = len(
                    [
                        c
                        for c in self.f.herdr_model()["calls"]
                        if c[:2] == ["worktree", "open"]
                    ]
                )
                self.assertEqual(before, after)
                self.lifecycle("close")

    def test_prompt_failure_and_agent_not_found_are_not_retried(self) -> None:
        for setting in ("prompt_failure", "prompt_agent_not_found"):
            with self.subTest(setting=setting):
                self.f.herdr_settings(**{setting: True})
                result = self.lifecycle("launch", expected=1)
                self.assertIn("resources retained", result["error"])
                model = self.f.herdr_model()
                calls = [
                    c
                    for c in model["calls"]
                    if c[:2] == ["agent", "prompt"] and "--help" not in c
                ]
                self.assertEqual(len(calls), 1)
                self.f.herdr_settings(**{setting: False})
                self.lifecycle("adopt")
                self.lifecycle("close")
                model = self.f.herdr_model()
                model["calls"] = []
                self.f.save_herdr(model)

    def test_unreachable_herdr_is_not_live_and_retains_created_worktree(
        self,
    ) -> None:
        self.f.herdr_settings(unreachable=True)
        self.assertEqual(self.f.status()["next_action"], "launch_review")
        self.assertIn(
            str(self.path), self.lifecycle("launch", expected=1)["error"]
        )
        self.assertTrue(self.path.exists())
        self.f.herdr_settings(unreachable=False)
        self.assertTrue(self.lifecycle("launch")["reused"])
        self.lifecycle("close")

    def test_adopt_refuses_wrong_kind_cwd_and_missing_agent(self) -> None:
        self.lifecycle("launch")
        model = self.f.herdr_model()
        for key, wrong in (("agent", "codex"), ("cwd", str(self.f.repo))):
            with self.subTest(key=key):
                changed = json.loads(json.dumps(model))
                changed["agents"][0][key] = wrong
                self.f.save_herdr(changed)
                self.lifecycle("adopt", expected=1)
        model["agents"] = []
        model["panes"][0]["agent"] = None
        self.f.save_herdr(model)
        self.lifecycle("adopt", expected=1)
        self.lifecycle("close")

    def test_close_refuses_extra_pane_tab_or_replaced_terminal_and_agent(
        self,
    ) -> None:
        self.lifecycle("launch")
        original = self.f.herdr_model()
        for change in ("pane", "tab", "terminal", "occupant", "label"):
            with self.subTest(change=change):
                model = json.loads(json.dumps(original))
                if change in ("pane", "tab"):
                    model["workspaces"][0][change + "_count"] = 2
                elif change == "terminal":
                    model["panes"][0]["terminal_id"] = "replacement"
                elif change == "occupant":
                    model["agents"][0]["name"] = "another-agent"
                else:
                    model["workspaces"][0]["label"] = "user-workspace"
                self.f.save_herdr(model)
                self.lifecycle("close", expected=3)
                self.assertTrue(self.path.exists())
                self.assertFalse(
                    any(
                        c[:2]
                        in (["worktree", "remove"], ["workspace", "close"])
                        and "--help" not in c
                        for c in self.f.herdr_model()["calls"]
                    )
                )
        self.f.save_herdr(original)
        self.lifecycle("close")

    def test_close_after_exit_and_lost_herdr_worktree_registration(
        self,
    ) -> None:
        for registration in (True, False):
            with self.subTest(registration=registration):
                self.lifecycle("launch")
                model = self.f.herdr_model()
                model["agents"] = []
                model["panes"][0]["agent"] = None
                if not registration:
                    model["workspaces"][0]["worktree"] = None
                self.f.save_herdr(model)
                self.lifecycle("close")
                self.assertFalse(self.path.exists())
                self.assertEqual(self.f.herdr_model()["workspaces"], [])

    def test_cleanup_failure_retains_resources_and_no_blind_fallback(
        self,
    ) -> None:
        self.lifecycle("launch")
        self.f.herdr_settings(remove_failure=True)
        self.lifecycle("close", expected=3)
        self.assertTrue(self.path.exists())
        self.assertFalse(
            any(
                c == ["workspace", "close", "w1"]
                for c in self.f.herdr_model()["calls"]
            )
        )
        self.f.herdr_settings(remove_failure=False)
        self.lifecycle("close")

    def test_handoffs_require_matching_authoritative_record_and_send_once(
        self,
    ) -> None:
        f = self.f
        args = (
            "handoff",
            "review-result",
            "--pr",
            "1",
            "--head",
            self.head,
            "--verdict",
            "approved",
        )
        f.cli(*args, expected=1)
        self.assertFalse(
            any(c[:2] == ["agent", "prompt"] for c in f.herdr_model()["calls"])
        )
        f.review("approved")
        f.herdr_settings(prompt_failure=True)
        f.cli(*args, expected=1)
        self.assertEqual(f.status()["next_action"], "approved")
        self.assertEqual(
            f.status()["current_review_unacted"]["head"], self.head
        )
        f.herdr_settings(prompt_failure=False)
        result = f.cli(*args)
        self.assertIn(
            "REVIEW_RESULT pr=1 head=" + self.head, result["message"]
        )
        f.cli(
            "handoff",
            "stopped",
            "--pr",
            "1",
            "--head",
            self.head,
            "--reason",
            "budget",
            expected=1,
        )
        f.cli(
            "stop",
            "post",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--head",
            self.head,
            "--reason",
            "budget",
            "--body",
            f.write("stop.md", "Stop."),
        )
        stopped = f.cli(
            "handoff",
            "stopped",
            "--pr",
            "1",
            "--head",
            self.head,
            "--reason",
            "budget",
        )
        self.assertIn("Automated review has stopped;", stopped["message"])

    def test_live_probe_cleans_and_retains_unsafe_resources(self) -> None:
        f = self.f
        for blocked in (False, True):
            f.herdr_settings(start_not_ready=blocked)
            result = f.cli("doctor", "--live-reviewer")
            self.assertEqual(
                result["live_reviewer"]["trust_or_permission_prompt"], blocked
            )
            self.assertEqual(f.herdr_model()["workspaces"], [])
            self.assertFalse(
                any(
                    "reviewer-doctor" in w
                    for w in f.git("worktree", "list").splitlines()
                )
            )
        f.herdr_settings(start_not_ready=False, start_extra_pane=True)
        result = f.cli("doctor", "--live-reviewer", expected=3)
        # doctor reports retained resource details in its JSON diagnostics.
        model = f.herdr_model()
        self.assertEqual(len(model["workspaces"]), 1)
        self.assertTrue(
            Path(model["workspaces"][0]["worktree"]["checkout_path"]).exists()
        )
        self.assertFalse(
            any(
                c[:2] == ["agent", "prompt"] and "--help" not in c
                for c in model["calls"]
            )
        )

    def test_missing_or_forged_ownership_record_retains_worktree(self) -> None:
        self.worktree("create")
        admin = Path(
            self.f.git("rev-parse", "--absolute-git-dir", cwd=self.path)
        )
        record = admin / "agent-squad-owner.json"
        original = record.read_text()
        for field in ("head", "common", "path", "name"):
            owner = json.loads(original)
            owner[field] = "foreign"
            record.write_text(json.dumps(owner))
            self.worktree("remove", expected=3)
        record.unlink()
        record.symlink_to(self.f.write("spoofed-owner.json", original))
        self.worktree("remove", expected=3)
        record.unlink()
        record.write_text(original)
        self.worktree("remove")

    def test_old_head_close_does_not_touch_new_head_reviewer(self) -> None:
        self.lifecycle("launch")
        original = self.head
        self.f.push("value = 1\nsecond = 20\nthird = 30\n")
        newer = self.lifecycle("launch")
        self.f.cli("reviewer", "close", "--pr", "1", "--head", original)
        self.assertTrue(Path(newer["worktree"]).exists())
        self.lifecycle("close")

    def test_existing_unowned_workspace_is_not_started_or_closed(self) -> None:
        self.worktree("create")
        self.f.run(
            [
                str(self.f.bin / "herdr"),
                "worktree",
                "open",
                "--cwd",
                str(self.f.repo),
                "--path",
                str(self.path),
                "--label",
                self.path.name,
                "--no-focus",
            ]
        )
        self.lifecycle("launch", expected=3)
        calls = self.f.herdr_model()["calls"]
        self.assertFalse(
            any(
                c[:2] == ["agent", "start"] and "--help" not in c
                for c in calls
            )
        )
        self.assertEqual(len(self.f.herdr_model()["workspaces"]), 1)
        self.lifecycle("close", expected=3)
        self.assertTrue(self.path.exists())

    def test_current_review_takes_precedence_over_live_hint(self) -> None:
        self.lifecycle("launch")
        self.f.review("changes_requested", [finding()])
        state = self.f.status()
        self.assertTrue(state["gates"]["reviewer_live"])
        self.assertEqual(state["next_action"], "address_findings")
        self.assertIsNotNone(state["current_review_unacted"])
        self.lifecycle("close")

    def test_shell_startup_busy_is_retried_before_single_agent_start(
        self,
    ) -> None:
        self.f.herdr_settings(start_busy=2)
        self.lifecycle("launch")
        model = self.f.herdr_model()
        self.assertEqual(len(model["agents"]), 1)
        starts = [
            c
            for c in model["calls"]
            if c[:2] == ["agent", "start"] and "--help" not in c
        ]
        prompts = [
            c
            for c in model["calls"]
            if c[:2] == ["agent", "prompt"] and "--help" not in c
        ]
        self.assertEqual(len(starts), 3)
        self.assertEqual(len(prompts), 1)
        self.lifecycle("close")
