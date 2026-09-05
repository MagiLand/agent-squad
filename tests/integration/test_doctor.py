from __future__ import annotations

from dataclasses import replace
import json
import os
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._support import (
    add_src_to_path,
    install_fake_herdr,
    run,
    run_cli,
    seed_git_repository,
)

add_src_to_path()

from agent_squad.doctor import (  # noqa: E402
    DoctorReport,
    _check_status,
    _check_worktree,
    diagnose,
    git_output,
)
from agent_squad.herdr import (
    HerdrClient,
    HerdrError,
    ReviewerSession,
)  # noqa: E402
from agent_squad.initialization import (  # noqa: E402
    AgentKind,
    AgentSquadError,
    GitWorktree,
    discover_git_worktree,
    initialize_repository,
    load_initialized_repository,
)
from agent_squad.runs import start_run, repository_identity  # noqa: E402
from agent_squad.submissions import (  # noqa: E402
    SubmitResult,
    submit_candidate,
)


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.head = seed_git_repository(self.repo)
        self.reviews = self.root / "reviews"
        initialize_repository(self.repo, review_worktree_root=self.reviews)
        self.namespace = (
            self.reviews
            / repository_identity(
                discover_git_worktree(self.repo)
            ).repository_id
        )
        self.config_path = self.repo / ".agent-squad/config.json"
        self.config = json.loads(self.config_path.read_text())
        self.config["base_ref"] = "main"
        self.config_path.write_text(json.dumps(self.config))
        _, environment = install_fake_herdr(self.root)
        environment["FAKE_HERDR_IMPLEMENTER_CWD"] = str(self.repo)
        self.environment = environment
        self.patch = mock.patch.dict(os.environ, environment)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def report(
        self,
        *,
        live_reviewer: bool = False,
        timeout_seconds: float = 120.0,
    ) -> DoctorReport:
        return diagnose(
            self.repo,
            live_reviewer=live_reviewer,
            timeout_seconds=timeout_seconds,
        )

    def details(self, report: DoctorReport) -> str:
        return "\n".join(
            f"{d.severity}: {d.check}: {d.detail}" for d in report.diagnostics
        )

    def commands(self) -> list[list[str]]:
        return [
            json.loads(line)["arguments"]
            for line in (self.root / "fake-herdr-state/invocations.jsonl")
            .read_text()
            .splitlines()
        ]

    def test_normal_checks_leave_authority_and_existing_worktrees_unchanged(
        self,
    ) -> None:
        before = self.config_path.read_bytes()
        exclude = (self.repo / ".git/info/exclude").read_bytes()
        report = self.report()
        self.assertTrue(report.ok, self.details(report))
        self.assertEqual(before, self.config_path.read_bytes())
        self.assertEqual(
            exclude, (self.repo / ".git/info/exclude").read_bytes()
        )
        self.assertEqual(list(self.namespace.iterdir()), [])
        self.assertFalse((self.repo / ".agent-squad/state.json").exists())
        self.assertFalse(
            any(
                c[:2]
                in (
                    ["agent", "start"],
                    ["agent", "prompt"],
                    ["workspace", "close"],
                )
                and "--help" not in c
                for c in self.commands()
            )
        )

    def test_cli_reports_failures_without_initializing(self) -> None:
        self.config_path.unlink()
        result = run_cli(
            self.repo,
            "doctor",
            data_home=self.root,
            env_overrides=self.environment,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("configuration", result.stdout)
        self.assertFalse(self.config_path.exists())

    def test_configuration_exclusions_base_and_submodule_diagnostics(
        self,
    ) -> None:
        (self.repo / ".gitmodules").write_text("# fixture")
        self.config["base_ref"] = "missing"
        self.config_path.write_text(json.dumps(self.config))
        (self.repo / ".git/info/exclude").write_text("")
        report = self.report()
        details = self.details(report)
        self.assertFalse(report.ok)
        self.assertIn("local exclusions", details)
        self.assertIn("configured base", details)
        self.assertIn("warning: submodules", details)

    def test_each_required_herdr_capability_is_reported(self) -> None:
        for missing in (
            "agent.get",
            "agent.start",
            "agent.prompt",
            "agent.read",
            "worktree.open",
            "agent_view",
        ):
            with (
                self.subTest(missing=missing),
                mock.patch.dict(
                    os.environ, {"FAKE_HERDR_SCHEMA_MISSING": missing}
                ),
            ):
                report = self.report()
                self.assertFalse(report.ok)
                self.assertIn(missing, self.details(report))

    def test_implementer_identity_mismatch_is_non_mutating(self) -> None:
        with mock.patch.dict(
            os.environ, {"FAKE_HERDR_IMPLEMENTER_KIND": "claude"}
        ):
            report = self.report()
        self.assertFalse(report.ok)
        self.assertIn("expected 'codex'", self.details(report))

    def test_invalid_placement_and_unwritable_control_storage(self) -> None:
        with mock.patch(
            "agent_squad.doctor.atomic_write",
            side_effect=PermissionError("denied control write"),
        ):
            self.assertIn("denied control write", self.details(self.report()))
        self.config["review_worktree_root"] = str(self.repo / "reviews")
        self.config_path.write_text(json.dumps(self.config))
        self.assertIn(
            "outside the implementation", self.details(self.report())
        )
        self.assertFalse((self.repo / "reviews").exists())

    def test_orphaned_worktree_bundle_and_reviewer_are_reported_not_removed(
        self,
    ) -> None:
        identity = repository_identity(discover_git_worktree(self.repo))
        orphan = (
            self.reviews / identity.repository_id / "unknown" / "round-001"
        )
        orphan.parent.mkdir(parents=True)
        run(
            ["git", "worktree", "add", "--detach", str(orphan), self.head],
            cwd=self.repo,
        )
        bundle = orphan / ".agent-squad-review"
        bundle.mkdir()
        (bundle / "evidence").write_text("preserve")
        extra = [
            {
                "name": "asq-12345678-r001-reviewer",
                "cwd": str(orphan),
                "pane_id": "w-old:p1",
            }
        ]
        with mock.patch.dict(
            os.environ, {"FAKE_HERDR_EXTRA_AGENTS": json.dumps(extra)}
        ):
            report = self.report()
        self.assertTrue(report.ok, self.details(report))
        self.assertIn("orphaned Reviewer", self.details(report))
        self.assertIn(str(bundle), self.details(report))
        self.assertEqual((bundle / "evidence").read_text(), "preserve")

    def test_active_identity_and_missing_expected_path_are_errors(
        self,
    ) -> None:
        task = self.root / "task.md"
        task.write_text("Implement fixture")
        start_run(self.repo, task_path=task)
        (self.repo / "README.md").write_text("candidate")
        run(["git", "add", "README.md"], cwd=self.repo)
        run(["git", "commit", "-m", "candidate"], cwd=self.repo)
        report = self.root / "report.md"
        report.write_text("Implemented fixture")
        submitted = submit_candidate(
            self.repo, report_path=report, mode="new_revision"
        )
        run(
            [
                "git",
                "worktree",
                "remove",
                "--force",
                str(submitted.review_worktree),
            ],
            cwd=self.repo,
        )
        diagnosed = self.report()
        self.assertIn(
            "active review worktree is missing", self.details(diagnosed)
        )
        self.assertFalse(diagnosed.ok)

    def prepare_review(self) -> SubmitResult:
        task = self.root / "task.md"
        task.write_text("Implement fixture")
        start_run(self.repo, task_path=task)
        (self.repo / "README.md").write_text("candidate")
        run(["git", "add", "README.md"], cwd=self.repo)
        run(["git", "commit", "-m", "candidate"], cwd=self.repo)
        report = self.root / "report.md"
        report.write_text("Implemented fixture")
        return submit_candidate(
            self.repo, report_path=report, mode="new_revision"
        )

    def test_busy_run_lock_is_reported_without_waiting(self) -> None:
        from agent_squad.doctor import _check_status
        from agent_squad.initialization import AgentSquadError
        from agent_squad.storage import exclusive_file_lock

        task = self.root / "task.md"
        task.write_text("Implement fixture")
        start_run(self.repo, task_path=task)
        with exclusive_file_lock(self.repo / ".agent-squad/lock"):
            with self.assertRaisesRegex(AgentSquadError, "run lock is busy"):
                _check_status(self.repo)

    def test_healthy_active_review_is_not_reported_as_orphan(self) -> None:
        submitted = self.prepare_review()
        report = self.report()
        self.assertTrue(report.ok, self.details(report))
        self.assertFalse(
            any(d.check == "residual resources" for d in report.diagnostics)
        )
        self.assertTrue(submitted.review_worktree.exists())

    def test_sibling_implementation_history_prevents_false_orphans(
        self,
    ) -> None:
        self.prepare_review()
        sibling = self.root / "sibling"
        run(
            ["git", "worktree", "add", "-b", "sibling", str(sibling)],
            cwd=self.repo,
        )
        initialize_repository(sibling, review_worktree_root=self.reviews)
        (sibling / ".agent-squad/config.json").write_bytes(
            self.config_path.read_bytes()
        )
        with mock.patch.dict(
            os.environ, {"FAKE_HERDR_IMPLEMENTER_CWD": str(sibling)}
        ):
            report = diagnose(sibling)
        self.assertTrue(report.ok, self.details(report))
        self.assertFalse(
            any(d.check == "residual resources" for d in report.diagnostics)
        )

    def test_mismatched_review_head_and_terminal_resources_are_retained(
        self,
    ) -> None:
        submitted = self.prepare_review()
        run(
            ["git", "checkout", "--detach", self.head],
            cwd=submitted.review_worktree,
        )
        report = self.report()
        self.assertIn("review HEAD differs", self.details(report))
        self.assertTrue(submitted.review_worktree.exists())
        run_path = next((self.repo / ".agent-squad/runs").glob("*/run.json"))
        record = json.loads(run_path.read_text())
        record["phase"] = "cancelled"
        run_path.write_text(json.dumps(record))
        report = self.report()
        self.assertIn("outlives or differs", self.details(report))
        self.assertIn(
            "retained reviewing round worktree", self.details(report)
        )

    def test_unregistered_bundle_and_symlink_are_not_traversed_or_removed(
        self,
    ) -> None:
        identity = repository_identity(discover_git_worktree(self.repo))
        namespace = self.reviews / identity.repository_id
        bundle = namespace / "orphan" / "round-001" / ".agent-squad-review"
        bundle.mkdir(parents=True)
        (bundle / "evidence").write_text("preserve")
        outside = self.root / "outside"
        outside.mkdir()
        (namespace / "linked").symlink_to(outside, target_is_directory=True)
        report = self.report()
        self.assertIn(
            "unregistered review worktree or bundle", self.details(report)
        )
        self.assertTrue((namespace / "linked").is_symlink())
        self.assertEqual((bundle / "evidence").read_text(), "preserve")

    def test_live_missing_close_capability_does_not_launch(self) -> None:
        with mock.patch.dict(
            os.environ, {"FAKE_HERDR_SCHEMA_MISSING": "workspace.close"}
        ):
            report = self.report(live_reviewer=True)
        self.assertIn("workspace.close", self.details(report))
        self.assertFalse(list(self.namespace.glob(".preflight-*")))

    def test_live_launch_and_cleanup_failures_retain_manifest(self) -> None:
        for variable, expected in (
            ("FAKE_HERDR_FAIL_START", "start failure"),
            ("FAKE_HERDR_FAIL_PROMPT", "prompt failure"),
            ("FAKE_HERDR_FAIL_CLOSE", "close failure"),
        ):
            with (
                self.subTest(variable=variable),
                mock.patch.dict(os.environ, {variable: "1"}),
            ):
                report = self.report(live_reviewer=True, timeout_seconds=1)
                self.assertFalse(report.ok)
                self.assertIn(expected, self.details(report))
        self.assertEqual(
            len(list(self.namespace.glob(".preflight-*/diagnostic.json"))), 3
        )

    def test_installed_kind_and_stale_integration_are_reported(self) -> None:
        for environment, expected in (
            (
                {"FAKE_HERDR_KINDS": "codex"},
                "does not advertise Reviewer kind",
            ),
            ({"FAKE_HERDR_STALE_KIND": "claude"}, "is not current"),
            ({"FAKE_HERDR_IMPLEMENTER_NAME": "absent"}, "is not available"),
        ):
            with (
                self.subTest(environment=environment),
                mock.patch.dict(os.environ, environment),
            ):
                report = self.report()
                self.assertFalse(report.ok)
                self.assertIn(expected, self.details(report))

    def test_live_blocked_reviewer_retains_inspectable_history(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_HERDR_AGENT_STATUS": "blocked",
                "FAKE_HERDR_PREFLIGHT": "timeout",
            },
        ):
            report = self.report(live_reviewer=True, timeout_seconds=1)
        self.assertIn("blocked on a permission", self.details(report))
        self.assertEqual(
            len(list(self.namespace.glob(".preflight-*/history.txt"))), 1
        )

    def test_failed_worktree_removal_retains_probe_and_existing_resources(
        self,
    ) -> None:
        from agent_squad.doctor import git_output

        unrelated = self.reviews / "unrelated"
        unrelated.mkdir(parents=True)
        (unrelated / "data").write_text("preserve")

        def fail_remove(root, *arguments):
            if arguments[:2] == ("worktree", "remove"):
                raise OSError("injected worktree removal failure")
            return git_output(root, *arguments)

        with mock.patch(
            "agent_squad.doctor.git_output", side_effect=fail_remove
        ):
            report = self.report()
        self.assertIn("owned probe retained", self.details(report))
        self.assertTrue(list(self.namespace.glob(".doctor-*/snapshot/.git")))
        self.assertEqual((unrelated / "data").read_text(), "preserve")

    def test_sha256_repository_passes_normal_and_live_checks(self) -> None:
        other = self.root / "sha256"
        seed_git_repository(other, object_format="sha256")
        initialize_repository(other, review_worktree_root=self.reviews)
        (other / ".agent-squad/config.json").write_bytes(
            self.config_path.read_bytes()
        )
        with mock.patch.dict(
            os.environ, {"FAKE_HERDR_IMPLEMENTER_CWD": str(other)}
        ):
            report = diagnose(other, live_reviewer=True, timeout_seconds=1)
        self.assertTrue(report.ok, self.details(report))
        self.assertIn("sha256", self.details(report))

    def test_live_preflight_submits_and_cleans_owned_resources(
        self,
    ) -> None:
        report = self.report(live_reviewer=True, timeout_seconds=1)
        self.assertTrue(report.ok, self.details(report))
        self.assertEqual(list(self.namespace.iterdir()), [])
        commands = self.commands()
        self.assertTrue(
            any(
                c[:2] == ["workspace", "close"] and "--help" not in c
                for c in commands
            )
        )
        prompts = [
            c
            for c in commands
            if c[:2] == ["agent", "prompt"] and "--help" not in c
        ]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all(c[2].startswith("asq-") for c in prompts))
        self.assertFalse((self.repo / ".agent-squad/state.json").exists())

    def test_live_failures_retain_evidence_and_never_close_resources(
        self,
    ) -> None:
        for mode, expected in (
            ("permission", "permission denied"),
            ("timeout", "timed out"),
            ("sentinel", "sentinel differs"),
            ("marker", "local-state.json"),
            ("history", "does not expose"),
        ):
            with (
                self.subTest(mode=mode),
                mock.patch.dict(os.environ, {"FAKE_HERDR_PREFLIGHT": mode}),
            ):
                report = self.report(live_reviewer=True, timeout_seconds=0.01)
                self.assertFalse(report.ok)
                self.assertIn(expected, self.details(report))
        evidence = list(self.namespace.glob(".preflight-*/diagnostic.json"))
        self.assertEqual(len(evidence), 5)
        self.assertFalse(
            any(
                c[:2] == ["workspace", "close"] and "--help" not in c
                for c in self.commands()
            )
        )

    def test_invalid_run_lock_types_are_rejected(self) -> None:
        lock = self.repo / ".agent-squad/lock"
        os.mkfifo(lock)
        with self.assertRaisesRegex(AgentSquadError, "not a regular file"):
            _check_status(self.repo)
        lock.unlink()
        target = self.root / "foreign-lock"
        target.write_text("preserve", encoding="utf-8")
        lock.symlink_to(target)
        with self.assertRaises(OSError):
            _check_status(self.repo)
        self.assertEqual(target.read_text(encoding="utf-8"), "preserve")

    def test_worktree_identity_and_head_mismatch_retain_probe(self) -> None:
        repository = load_initialized_repository(self.repo)
        for field in ("root", "common_directory", "head"):

            def wrong_identity(path: Path) -> GitWorktree:
                discovered = discover_git_worktree(path)
                if field == "head":
                    return discovered
                return replace(discovered, **{field: self.root})

            def wrong_head(root: Path, *arguments: str) -> str:
                if field == "head" and arguments == ("rev-parse", "HEAD"):
                    return "0" * 40
                return git_output(root, *arguments)

            with (
                self.subTest(field=field),
                mock.patch(
                    "agent_squad.doctor.discover_git_worktree",
                    side_effect=wrong_identity,
                ),
                mock.patch(
                    "agent_squad.doctor.git_output", side_effect=wrong_head
                ),
            ):
                with self.assertRaisesRegex(AgentSquadError, "mismatch"):
                    _check_worktree(repository)
        self.assertEqual(
            len(list(self.namespace.glob(".doctor-*/snapshot"))), 3
        )

    def test_failed_worktree_add_removes_only_empty_owned_probe(self) -> None:
        existing = self.reviews / "existing"
        existing.mkdir(parents=True)
        (existing / "evidence").write_text("preserve", encoding="utf-8")

        def fail_add(root: Path, *arguments: str) -> str:
            if "add" in arguments:
                raise AgentSquadError("worktree creation denied")
            return git_output(root, *arguments)

        with mock.patch("agent_squad.doctor.git_output", side_effect=fail_add):
            with self.assertRaisesRegex(AgentSquadError, "creation denied"):
                _check_worktree(load_initialized_repository(self.repo))
        self.assertFalse(list(self.namespace.glob(".doctor-*")))
        self.assertEqual(
            (existing / "evidence").read_text(encoding="utf-8"), "preserve"
        )

    def test_residual_history_identity_is_checked(self) -> None:
        self.prepare_review()
        run_path = next((self.repo / ".agent-squad/runs").glob("*/run.json"))
        original = run_path.read_bytes()
        record = json.loads(original)
        record["repository"]["repository_id"] = "0" * 64
        run_path.write_text(json.dumps(record), encoding="utf-8")
        self.assertIn(
            "run repository identity differs", self.details(self.report())
        )
        run_path.write_bytes(original)
        round_path = run_path.parent / "rounds/001"
        renamed = round_path.with_name("002")
        round_path.rename(renamed)
        self.assertIn(
            "record identity differs from history path",
            self.details(self.report()),
        )
        renamed.rename(round_path)
        run_directory = run_path.parent
        renamed_run = run_directory.with_name(
            "00000000-0000-4000-8000-000000000000"
        )
        run_directory.rename(renamed_run)
        self.assertIn(
            "record identity differs from history path",
            self.details(self.report()),
        )

    def test_damaged_bundle_is_reported_without_cleanup(self) -> None:
        submitted = self.prepare_review()
        task = submitted.review_worktree / ".agent-squad-review/input/task.md"
        task.chmod(0o600)
        task.write_text("changed", encoding="utf-8")
        report = self.report()
        self.assertFalse(report.ok)
        self.assertTrue(
            any(
                item.check == "stored identity and active paths"
                and item.severity == "error"
                for item in report.diagnostics
            )
        )
        self.assertEqual(task.read_text(encoding="utf-8"), "changed")

    def launch_test_reviewer(self) -> tuple[HerdrClient, ReviewerSession]:
        client = HerdrClient(self.repo)
        session = client.dispatch_review_request(
            reviewer_name="asq-doctor-test-reviewer",
            reviewer_kind=AgentKind.CLAUDE,
            start_args=(),
            review_worktree=self.repo,
            prompt="owned test request",
            allow_adoption=False,
        )
        return client, session

    def test_cleanup_refuses_adopted_moved_or_uninspectable_session(
        self,
    ) -> None:
        client, session = self.launch_test_reviewer()
        path = self.root / "fake-herdr-state/agent.json"
        original = path.read_bytes()
        for mode, expected in (
            ("adopted", "adopted preflight session"),
            ("pane_id", "Reviewer moved"),
            ("workspace_id", "Reviewer moved"),
            ("inventory", "cannot verify preflight workspace ownership"),
        ):
            path.write_bytes(original)
            selected = session
            if mode == "adopted":
                selected = replace(session, adopted=True)
            elif mode != "inventory":
                agent = json.loads(original)
                agent[mode] = "foreign-resource"
                path.write_text(json.dumps(agent), encoding="utf-8")
            with (
                self.subTest(mode=mode),
                mock.patch.dict(
                    os.environ,
                    {
                        "FAKE_HERDR_NO_WORKSPACES": (
                            "1" if mode == "inventory" else "0"
                        ),
                    },
                ),
            ):
                with self.assertRaisesRegex(HerdrError, expected):
                    client.close_preflight(
                        selected,
                        name="asq-doctor-test-reviewer",
                        kind=AgentKind.CLAUDE,
                        worktree=self.repo,
                    )
            self.assertTrue(path.exists())
        self.assertNotIn(
            ["workspace", "close", session.workspace_id], self.commands()
        )

    def test_preflight_does_not_adopt_existing_agent_or_worktree(self) -> None:
        client, _ = self.launch_test_reviewer()
        before = len(self.commands())
        for mode, expected in (
            ("agent", "name already exists"),
            ("worktree", "worktree was already open"),
        ):
            if mode == "worktree":
                (self.root / "fake-herdr-state/agent.json").unlink()
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(HerdrError, expected):
                    client.dispatch_review_request(
                        reviewer_name="asq-doctor-test-reviewer",
                        reviewer_kind=AgentKind.CLAUDE,
                        start_args=(),
                        review_worktree=self.repo,
                        prompt="must not be sent",
                        allow_adoption=False,
                    )
        self.assertFalse(
            any(
                command[:2]
                in (
                    ["agent", "start"],
                    ["agent", "prompt"],
                    ["workspace", "close"],
                )
                for command in self.commands()[before:]
            )
        )

    def test_live_receipt_and_request_must_match_owned_submission(
        self,
    ) -> None:
        for mode, expected in (
            ("receipt-not-sent", "did not confirm the owned result handoff"),
            ("receipt-wrong-id", "did not confirm the owned result handoff"),
            ("request", "observed request differs"),
        ):
            with (
                self.subTest(mode=mode),
                mock.patch.dict(
                    os.environ,
                    {
                        "FAKE_HERDR_PREFLIGHT": mode,
                    },
                ),
            ):
                report = self.report(live_reviewer=True, timeout_seconds=1)
                self.assertFalse(report.ok)
                self.assertIn(expected, self.details(report))
        self.assertFalse(
            any(
                command[:2] == ["workspace", "close"]
                and "--help" not in command
                for command in self.commands()
            )
        )
        self.assertEqual(
            len(
                list(
                    self.namespace.glob(
                        ".preflight-*/*/round-001/"
                        ".agent-squad-review/local-state.json",
                    )
                )
            ),
            3,
        )

    def test_failed_preflight_reports_session_and_stale_registration(
        self,
    ) -> None:
        with mock.patch.dict(os.environ, {"FAKE_HERDR_FAIL_CLOSE": "1"}):
            failed = self.report(live_reviewer=True, timeout_seconds=1)
        self.assertFalse(failed.ok)
        manifest_path = next(self.reviews.rglob("diagnostic.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        worktree = Path(manifest["worktree"])
        report = self.report()
        self.assertIn(manifest["reviewer_name"], self.details(report))
        self.assertIn(manifest["pane_id"], self.details(report))
        self.assertIn(
            str(worktree / ".agent-squad-review"), self.details(report)
        )
        self.assertTrue(worktree.exists())
        shutil.rmtree(manifest_path.parent)
        report = self.report()
        self.assertIn(str(worktree), self.details(report))
        registered = run(
            ["git", "worktree", "list", "--porcelain"], cwd=self.repo
        ).stdout
        self.assertIn(str(worktree), registered)
        self.assertIn("prunable", registered)

    def test_diagnostics_do_not_report_another_repositorys_probes(
        self,
    ) -> None:
        with mock.patch.dict(os.environ, {"FAKE_HERDR_FAIL_CLOSE": "1"}):
            self.report(live_reviewer=True, timeout_seconds=1)
        manifest_path = next(self.reviews.rglob("diagnostic.json"))
        other = self.root / "other"
        seed_git_repository(other)
        initialize_repository(other, review_worktree_root=self.reviews)
        (other / ".agent-squad/config.json").write_bytes(
            self.config_path.read_bytes()
        )
        report = diagnose(other)
        self.assertTrue(report.ok, self.details(report))
        self.assertNotIn(str(manifest_path.parent), self.details(report))
        self.assertTrue(manifest_path.exists())

    def test_probe_namespace_symlink_is_not_followed(self) -> None:
        outside = self.root / "outside-probe-root"
        outside.mkdir()
        self.reviews.mkdir()
        self.namespace.symlink_to(outside, target_is_directory=True)
        report = self.report(live_reviewer=True, timeout_seconds=1)
        self.assertFalse(report.ok)
        self.assertIn(
            "diagnostic namespace is a symlink", self.details(report)
        )
        self.assertEqual(list(outside.iterdir()), [])

    def test_unregistered_preflight_bundle_is_reported(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_HERDR_FAIL_CLOSE": "1"}):
            self.report(live_reviewer=True, timeout_seconds=1)
        manifest_path = next(self.namespace.rglob("diagnostic.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        worktree = Path(manifest["worktree"])
        bundle = worktree / ".agent-squad-review"
        saved = self.root / "saved-bundle"
        shutil.copytree(bundle, saved)
        run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=self.repo,
        )
        shutil.copytree(saved, bundle)
        report = self.report()
        self.assertIn(
            f"unregistered review worktree or bundle: {worktree}",
            self.details(report),
        )
        self.assertTrue(bundle.exists())

    def test_unborn_head_does_not_accumulate_empty_probes(self) -> None:
        from tests._support import initialize_git_repository

        unborn = self.root / "unborn"
        initialize_git_repository(unborn)
        initialize_repository(unborn, review_worktree_root=self.reviews)
        for _ in range(3):
            report = diagnose(unborn)
            self.assertFalse(report.ok)
            self.assertFalse(list(self.reviews.rglob(".doctor-*")))
            self.assertNotIn("owned probe retained", self.details(report))

    def test_live_cleanup_refuses_workspace_with_added_pane(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_HERDR_PANE_COUNT": "2"}):
            report = self.report(live_reviewer=True, timeout_seconds=1)
        self.assertFalse(report.ok)
        self.assertIn("no longer isolated", self.details(report))
        self.assertTrue(
            list(
                self.namespace.glob(
                    ".preflight-*/*/round-001/"
                    ".agent-squad-review/local-state.json"
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
