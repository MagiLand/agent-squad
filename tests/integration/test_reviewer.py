"""Reviewer recovery against real Git and the fake Herdr and forge."""

import json
from itertools import product
from pathlib import Path
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

    def two_sessions(self) -> None:
        """Default session: another repository's implementer. Ours: `ours`."""
        elsewhere = self.f.root / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        self.f.herdr_settings(implementer={"cwd": str(elsewhere)})
        self.f.herdr_session("ours", implementer={})

    def test_launch_creates_reviewer_in_the_implementers_session(
        self,
    ) -> None:
        f = self.f
        self.two_sessions()
        f.inherit_herdr("fixture")
        result = self.lifecycle("launch")
        model = f.herdr_model()
        ours = model["sessions"]["ours"]
        self.assertEqual([a["name"] for a in ours["agents"]], [result["name"]])
        self.assertEqual(
            [w["label"] for w in ours["workspaces"]], [result["name"]]
        )
        self.assertEqual(
            [c[2] for c in ours["calls"] if c[:2] == ["agent", "prompt"]],
            ["--help", result["name"]],
        )
        # The inherited session is read while searching and never changed.
        for key in ("workspaces", "tabs", "panes", "agents"):
            self.assertEqual(model[key], [], key)
        self.assertEqual(
            {tuple(c[:2]) for c in model["calls"]},
            {("session", "list"), ("agent", "get")},
        )

    def mutations(self, calls: list) -> list:
        return [
            c for c in calls
            if "--help" not in c and c[:2] in (
                ["worktree", "open"], ["worktree", "remove"],
                ["agent", "start"], ["agent", "prompt"],
                ["workspace", "close"],
            )
        ]

    def test_inherited_variables_never_decide_the_session(self) -> None:
        f = self.f
        self.two_sessions()
        # The right session, another one, a session that is gone, and none.
        for inherited in ("ours", "fixture", "absent", None):
            with self.subTest(inherited=inherited):
                f.inherit_herdr(inherited)
                result = self.lifecycle("launch")
                ours = f.herdr_model()["sessions"]["ours"]
                self.assertEqual(
                    [a["name"] for a in ours["agents"]], [result["name"]]
                )
                self.assertTrue(f.status()["gates"]["reviewer_live"])
                self.assertEqual(
                    self.lifecycle("adopt")["observed_state"], "working"
                )
                self.lifecycle("close")
                model = f.herdr_model()
                self.assertEqual(model["sessions"]["ours"]["workspaces"], [])
                self.assertFalse(self.path.exists())
                self.assertFalse(f.status()["gates"]["reviewer_live"])
                self.assertEqual(self.mutations(model["calls"]), [])
                self.assertEqual(
                    len(self.mutations(model["sessions"]["ours"]["calls"])), 5
                )
                model["sessions"]["ours"]["calls"] = []
                f.save_herdr(model)

    def test_launch_without_one_session_refuses_and_creates_nothing(
        self,
    ) -> None:
        f = self.f
        wanted = (
            "Implementer 'implementer' \\(kind 'codex'\\) working in "
            + str(f.repo)
        )
        scratch = f.repo / ".agent-squad/review-scratch/pr1"
        before = f.git("worktree", "list", "--porcelain")
        f.herdr_session("stopped", running=False, implementer={})
        f.herdr_session("twin", implementer={})
        for settings, message in (
            (
                {"twin": False},
                f"no running Herdr session holds {wanted};"
                " sessions examined: fixture, twin$",
            ),
            (
                {"twin": {}},
                f"several running Herdr sessions hold {wanted}:"
                " fixture, twin; sessions examined: fixture, twin$",
            ),
        ):
            with self.subTest(settings=settings):
                model = f.herdr_model()
                model["settings"]["implementer"] = settings["twin"]
                model["sessions"]["twin"]["settings"]["implementer"] = (
                    settings["twin"]
                )
                f.save_herdr(model)
                result = self.lifecycle("launch", expected=1)
                self.assertRegex(result["error"], message)
                self.assertFalse(self.path.exists())
                self.assertFalse(scratch.exists())
                self.assertEqual(
                    f.git("worktree", "list", "--porcelain"), before
                )
                model = f.herdr_model()
                for state in (model, *model["sessions"].values()):
                    self.assertEqual(state["workspaces"], [])
                    self.assertEqual(self.mutations(state["calls"]), [])
                self.assertEqual(model["sessions"]["stopped"]["calls"], [])

    def test_handoffs_prompt_only_the_resolved_implementer(self) -> None:
        f = self.f
        self.two_sessions()
        f.review("approved", [])
        f.cli(
            "stop", "post", "--as", "reviewer", "--pr", "1", "--head",
            self.head, "--reason", "budget", "--body",
            f.write("stop.md", "Stop."),
        )
        commands = (
            ("review-result", "--verdict", "approved"),
            ("stopped", "--reason", "budget"),
        )
        for inherited in ("fixture", "ours", None):
            f.inherit_herdr(inherited)
            for command in commands:
                with self.subTest(inherited=inherited, command=command[0]):
                    result = f.cli(
                        "handoff", command[0], "--pr", "1", "--head",
                        self.head, *command[1:],
                    )
                    model = f.herdr_model()
                    self.assertEqual(
                        self.mutations(model["sessions"]["ours"]["calls"]),
                        [["agent", "prompt", "implementer",
                          result["message"]]],
                    )
                    self.assertEqual(self.mutations(model["calls"]), [])
                    model["sessions"]["ours"]["calls"] = []
                    f.save_herdr(model)
        model = f.herdr_model()
        model["sessions"]["ours"]["settings"]["implementer"] = False
        f.save_herdr(model)
        for command in commands:
            with self.subTest(refused=command[0]):
                result = f.cli(
                    "handoff", command[0], "--pr", "1", "--head", self.head,
                    *command[1:], expected=1,
                )
                self.assertIn(
                    "no running Herdr session holds", result["error"]
                )
                model = f.herdr_model()
                for state in (model, model["sessions"]["ours"]):
                    self.assertEqual(self.mutations(state["calls"]), [])

    def test_no_session_hides_reviewer_from_status_and_keeps_resources(
        self,
    ) -> None:
        f = self.f
        self.lifecycle("launch")
        workspaces = f.herdr_model()["workspaces"]
        self.assertEqual(len(workspaces), 1)
        f.herdr_settings(implementer=False)
        before = len(self.mutations(f.herdr_model()["calls"]))
        state = f.status()
        self.assertFalse(state["gates"]["reviewer_live"])
        self.assertEqual(state["next_action"], "launch_review")
        adopted = self.lifecycle("adopt", expected=1)
        self.assertIn("no running Herdr session holds", adopted["error"])
        closed = self.lifecycle("close", expected=3)
        self.assertIn("no running Herdr session holds", closed["error"])
        self.assertIn("resources retained", closed["error"])
        self.assertTrue(self.path.exists())
        model = f.herdr_model()
        self.assertEqual(model["workspaces"], workspaces)
        self.assertEqual(len(model["agents"]), 1)
        self.assertEqual(len(self.mutations(model["calls"])), before)
        f.herdr_settings(implementer={})
        self.assertTrue(f.status()["gates"]["reviewer_live"])
        self.lifecycle("close")
        self.assertFalse(self.path.exists())

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

    def test_close_removes_created_worktree_without_herdr_workspace(
        self,
    ) -> None:
        self.worktree("create")
        self.lifecycle("close")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.f.herdr_model()["workspaces"], [])
        self.assertNotIn(str(self.path), self.f.git("worktree", "list"))

    def test_codex_long_review_keeps_name_and_closes_after_one_request(
        self,
    ) -> None:
        f = self.f
        path = f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config["reviewer"].update(
            kind="codex", start_args=["--add-dir", str(f.root)]
        )
        path.write_text(json.dumps(config))
        # If delivered during startup, this request remains working through
        # Herdr's deadline. After interactive startup it stays working with
        # no pending startup deadline. The fixture models timeouts, not sleep.
        f.herdr_settings(
            initial_request_state="working", prompt_state="working"
        )
        result = self.lifecycle("launch")
        self.assertEqual(result["delivery"], "agent_prompt")
        self.assertEqual(result["observed_state"], "working")
        self.assertEqual(result["name"], self.path.name)
        self.assertEqual(result["worktree"], str(self.path))
        agent = f.herdr_model()["agents"][0]
        self.assertEqual(agent["name"], result["name"])
        self.assertEqual(agent["pane_id"], result["pane_id"])
        self.assertEqual(agent["workspace_id"], result["workspace_id"])
        self.assertFalse(agent["launch_pending"])
        self.assertTrue(f.status()["gates"]["reviewer_live"])
        calls = f.herdr_model()["calls"]
        starts = [
            c
            for c in calls
            if c[:2] == ["agent", "start"] and "--help" not in c
        ]
        self.assertEqual(starts, [[
            "agent", "start", result["name"], "--kind", "codex",
            "--pane", result["pane_id"], "--", "--add-dir", str(f.root),
        ]])
        prompts = [
            c for c in calls
            if c[:2] == ["agent", "prompt"] and "--help" not in c
        ]
        self.assertEqual(prompts, [[
            "agent", "prompt", result["name"],
            f"$squad-reviewer pr=1 head={self.head} base={f.base}"
            " implementer=implementer",
        ]])
        self.assertLess(calls.index(starts[0]), calls.index(prompts[0]))
        self.assertFalse(any("--wait" in c or "send-keys" in c for c in calls))
        self.lifecycle("close")
        self.assertFalse(self.path.exists())
        self.assertEqual(f.herdr_model()["workspaces"], [])

    def test_startup_failure_retains_resources_without_prompt_or_retry(
        self,
    ) -> None:
        for kind in ("codex", "claude"):
            for settings, message in (
                ({"start_failure": True}, "agent exited during startup"),
                ({"start_state": "working"}, "timed out waiting"),
                ({"start_state": "unknown"}, "timed out waiting"),
            ):
                with self.subTest(kind=kind, settings=settings):
                    config_path = self.f.repo / ".agent-squad/config.json"
                    config = json.loads(config_path.read_text())
                    config["reviewer"]["kind"] = kind
                    config_path.write_text(json.dumps(config))
                    self.f.herdr_settings(
                        start_failure=False, start_state="idle"
                    )
                    self.f.herdr_settings(**settings)
                    result = self.lifecycle("launch", expected=1)
                    self.assertIn(message, result["error"])
                    self.assertIn("resources retained", result["error"])
                    self.assertTrue(self.path.exists())
                    model = self.f.herdr_model()
                    calls = [
                        c for c in model["calls"] if "--help" not in c
                    ]
                    self.assertEqual(sum(
                        c[:2] == ["agent", "start"] for c in calls
                    ), 1)
                    self.assertFalse(any(
                        c[:2] in (["agent", "prompt"], ["agent", "rename"])
                        for c in calls
                    ))
                    if model["agents"]:
                        # The timeout erased the name; unchanged cleanup
                        # guards must refuse this occupant, retaining it.
                        self.assertIsNone(model["agents"][0]["name"])
                        self.lifecycle("close", expected=3)
                        self.assertTrue(self.path.exists())
                    # Simulate agent exit solely to reset this fake case.
                    model["agents"] = []
                    model["panes"][0]["agent"] = None
                    self.f.save_herdr(model)
                    self.lifecycle("close")
                    model = self.f.herdr_model()
                    model["calls"] = []
                    self.f.save_herdr(model)

    def test_fake_startup_contract(self) -> None:
        self.worktree("create")
        opened = self.f.run([
            str(self.f.bin / "herdr"), "worktree", "open",
            "--cwd", str(self.f.repo), "--path", str(self.path),
            "--label", self.path.name, "--no-focus",
        ])
        self.assertEqual(opened.returncode, 0, opened.stderr)
        pane = json.loads(opened.stdout)["result"]["root_pane"]
        baseline = self.f.herdr_model()
        for state, code in (
            ("idle", None), ("blocked", "agent_not_ready"),
            ("working", "timeout"), ("unknown", "timeout"),
        ):
            with self.subTest(state=state):
                self.f.save_herdr(baseline)
                self.f.herdr_settings(start_state=state)
                started = self.f.run([
                    str(self.f.bin / "herdr"), "agent", "start",
                    self.path.name, "--kind", "codex", "--pane",
                    pane["pane_id"],
                ])
                self.assertEqual(started.returncode, int(code is not None))
                if code is not None:
                    self.assertEqual(
                        json.loads(started.stderr)["error"]["code"], code
                    )
                model = self.f.herdr_model()
                self.assertEqual(len(model["agents"]), 1)
                agent = model["agents"][0]
                self.assertEqual(agent["agent_status"], state)
                self.assertEqual(agent["name"], (
                    None if code == "timeout" else self.path.name
                ))
                self.assertEqual(agent["pane_id"], pane["pane_id"])
                self.assertEqual(agent["terminal_id"], pane["terminal_id"])
                self.assertEqual(model["panes"][0]["agent"], "codex")

    def test_blocked_and_not_ready_leave_resources_and_adopt_creates_nothing(
        self,
    ) -> None:
        for kind, settings in product(("codex", "claude"), (
            {"start_blocked_success": True},
            {"start_not_ready": True},
            {"prompt_state": "blocked"},
        )):
            with self.subTest(kind=kind, settings=settings):
                path = self.f.repo / ".agent-squad/config.json"
                config = json.loads(path.read_text())
                config["reviewer"]["kind"] = kind
                path.write_text(json.dumps(config))
                self.f.herdr_settings(
                    start_state="idle",
                    start_blocked_success=False,
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
                    start_blocked_success=False,
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
        for kind, setting in product(
            ("codex", "claude"), ("prompt_failure", "prompt_agent_not_found")
        ):
            with self.subTest(kind=kind, setting=setting):
                path = self.f.repo / ".agent-squad/config.json"
                config = json.loads(path.read_text())
                config["reviewer"]["kind"] = kind
                path.write_text(json.dumps(config))
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
        # An unreadable session cannot be resolved: nothing is created.
        self.assertIn(
            "sessions that could not be read: fixture",
            self.lifecycle("launch", expected=1)["error"],
        )
        self.assertFalse(self.path.exists())
        # A failure after resolution still retains the created worktree.
        self.f.herdr_settings(unreachable=False, snapshot_failure=True)
        self.assertIn(
            str(self.path), self.lifecycle("launch", expected=1)["error"]
        )
        self.assertTrue(self.path.exists())
        self.f.herdr_settings(snapshot_failure=False)
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

    def test_approved_handoff_defers_to_current_status_in_every_scenario(
        self,
    ) -> None:
        f = self.f
        original_model = f.read_model()
        expected_message = (
            f"AGENT_SQUAD/0.5.0 REVIEW_RESULT pr=1 head={self.head}"
            " verdict=approved\n"
            "Run agent-squad status --pr 1 --json and follow its derived"
            " next_action under the squad-implementer skill."
        )
        scenarios = (
            ("optional findings", "address_findings"),
            ("standing instruction", "merge"),
            ("no instruction", "approved"),
            ("merge hold", "approved"),
        )
        for scenario, next_action in scenarios:
            with self.subTest(scenario=scenario):
                f.save_model(original_model)
                f.review(
                    "approved",
                    [finding("optional")]
                    if scenario == "optional findings" else [],
                    sections=(
                        {"merge-hold": "Item 3: merge rules."}
                        if scenario == "merge hold" else None
                    ),
                )
                if scenario != "no instruction":
                    f.decision(merge_instruction="record", body="")
                before = len(f.herdr_model()["calls"])
                result = f.cli(
                    "handoff", "review-result", "--pr", "1",
                    "--head", self.head, "--verdict", "approved",
                )
                self.assertEqual(result["message"], expected_message)
                prompts = [
                    call for call in f.herdr_model()["calls"][before:]
                    if call[:2] == ["agent", "prompt"]
                ]
                self.assertEqual(prompts, [
                    ["agent", "prompt", "implementer", expected_message],
                ])
                self.assertEqual(f.status()["next_action"], next_action)

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
        for setting in ("prompt_failure", "prompt_agent_not_found"):
            with self.subTest(setting=setting):
                f.herdr_settings(**{setting: True})
                before = len(f.herdr_model()["calls"])
                published = f.read_model()["prs"]
                self.assertIn(
                    "agent prompt failed", f.cli(*args, expected=1)["error"]
                )
                self.assertEqual(
                    [
                        c[:2]
                        for c in f.herdr_model()["calls"][before:]
                        if c[:2] == ["agent", "prompt"]
                    ],
                    [["agent", "prompt"]],
                )
                self.assertEqual(f.read_model()["prs"], published)
                self.assertEqual(f.status()["next_action"], "approved")
                self.assertEqual(
                    f.status()["current_review_unacted"]["head"], self.head
                )
                f.herdr_settings(**{setting: False})
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

    def test_launch_refuses_unexpected_opened_path(self) -> None:
        self.f.herdr_settings(opened_path=str(self.f.root / "elsewhere"))
        result = self.lifecycle("launch", expected=3)
        self.assertIn("unexpected worktree or pane", result["error"])
        self.assertTrue(self.path.exists())
        self.assertFalse(
            any(
                c[:2] == ["agent", "start"] and "--help" not in c
                for c in self.f.herdr_model()["calls"]
            )
        )

    def test_corrupted_herdr_ownership_record_retains_everything(
        self,
    ) -> None:
        self.lifecycle("launch")
        admin = Path(
            self.f.git("rev-parse", "--absolute-git-dir", cwd=self.path)
        )
        record = admin / "agent-squad-owner.json"
        original = record.read_text()
        resource = json.loads(original)["herdr"]
        for herdr in (
            {"workspace_id": "w1"},
            "w1",
            1,
            {**resource, "kind": "shell"},
            {**resource, "terminal_id": ""},
            {**resource, "pane_id": 1},
        ):
            with self.subTest(herdr=herdr):
                owner = json.loads(original)
                owner["herdr"] = herdr
                record.write_text(json.dumps(owner))
                for command in ("close", "adopt"):
                    self.assertIn(
                        "ownership",
                        self.lifecycle(command, expected=3)["error"],
                    )
                self.assertTrue(self.path.exists())
                self.assertFalse(
                    any(
                        c[:2]
                        in (["worktree", "remove"], ["workspace", "close"])
                        and "--help" not in c
                        for c in self.f.herdr_model()["calls"]
                    )
                )
        record.write_text(original)
        self.lifecycle("close")

    def test_close_refuses_every_isolation_and_identity_mismatch(
        self,
    ) -> None:
        self.lifecycle("launch")
        original = self.f.herdr_model()
        primary = str(self.f.repo)
        changes = {
            "registration_path": lambda m: m["workspaces"][0][
                "worktree"
            ].update(checkout_path=primary),
            "registration_root": lambda m: m["workspaces"][0][
                "worktree"
            ].update(repo_root=str(self.f.worktree)),
            "registration_linked": lambda m: m["workspaces"][0][
                "worktree"
            ].update(is_linked_worktree=False),
            "snapshot_pane": lambda m: m["panes"].append(
                {**m["panes"][0], "pane_id": "w1:p9"}
            ),
            "snapshot_tab": lambda m: m["tabs"].append(
                {**m["tabs"][0], "tab_id": "w1:t9"}
            ),
            "pane_id": lambda m: m["panes"][0].update(pane_id="w1:p9"),
            "pane_tab": lambda m: m["panes"][0].update(tab_id="w1:t9"),
            "active_tab": lambda m: m["workspaces"][0].update(
                active_tab_id="w1:t9"
            ),
            "pane_cwd": lambda m: m["panes"][0].update(cwd=primary),
            "second_occupant": lambda m: m["agents"].append(
                {**m["agents"][0], "name": "second"}
            ),
            "occupant_kind": lambda m: m["agents"][0].update(agent="codex"),
            "occupant_terminal": lambda m: m["agents"][0].update(
                terminal_id="other"
            ),
            "occupant_cwd": lambda m: m["agents"][0].update(cwd=primary),
            "unlisted_agent": lambda m: m.update(agents=[]),
        }
        for change, mutate in changes.items():
            with self.subTest(change=change):
                model = json.loads(json.dumps(original))
                mutate(model)
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

    def test_swapped_in_clone_checkout_with_planted_record_is_refused(
        self,
    ) -> None:
        self.worktree("create")
        admin = Path(
            self.f.git("rev-parse", "--absolute-git-dir", cwd=self.path)
        )
        clone = self.f.root / "clone"
        self.f.git("clone", "--quiet", str(self.f.origin), str(clone))
        foreign = self.f.root / "foreign"
        self.f.git(
            "worktree", "add", "--detach", str(foreign), self.head, cwd=clone
        )
        foreign_admin = Path(
            self.f.git("rev-parse", "--absolute-git-dir", cwd=foreign)
        )
        (foreign_admin / "agent-squad-owner.json").write_text(
            (admin / "agent-squad-owner.json").read_text()
        )
        (self.path / ".git").write_text(f"gitdir: {foreign_admin}\n")
        self.assertEqual(
            self.f.git("rev-parse", "HEAD", cwd=self.path), self.head
        )
        for command in ("create", "remove"):
            self.assertIn(
                "ownership", self.worktree(command, expected=3)["error"]
            )
        self.lifecycle("close", expected=3)
        self.assertTrue(self.path.exists())

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
