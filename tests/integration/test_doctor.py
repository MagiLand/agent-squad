from __future__ import annotations

import json
import os
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

from agent_squad.doctor import diagnose
from agent_squad.initialization import initialize_repository
from agent_squad.runs import start_run, repository_identity
from agent_squad.initialization import discover_git_worktree
from agent_squad.submissions import submit_candidate


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.head = seed_git_repository(self.repo)
        self.reviews = self.root / "reviews"
        initialize_repository(self.repo, review_worktree_root=self.reviews)
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

    def report(self, **kwargs):
        return diagnose(self.repo, **kwargs)

    def details(self, report):
        return "\n".join(
            f"{d.severity}: {d.check}: {d.detail}" for d in report.diagnostics
        )

    def commands(self):
        return [
            json.loads(line)["arguments"]
            for line in (self.root / "fake-herdr-state/invocations.jsonl")
            .read_text()
            .splitlines()
        ]

    def test_normal_checks_leave_authority_and_existing_worktrees_unchanged(
        self,
    ):
        before = self.config_path.read_bytes()
        exclude = (self.repo / ".git/info/exclude").read_bytes()
        report = self.report()
        self.assertTrue(report.ok, self.details(report))
        self.assertEqual(before, self.config_path.read_bytes())
        self.assertEqual(
            exclude, (self.repo / ".git/info/exclude").read_bytes()
        )
        self.assertEqual(list(self.reviews.iterdir()), [])
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

    def test_cli_and_make_target_report_failures_without_initializing(self):
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

    def test_configuration_exclusions_base_and_submodule_diagnostics(self):
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

    def test_each_required_herdr_capability_is_reported(self):
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

    def test_implementer_identity_mismatch_is_non_mutating(self):
        with mock.patch.dict(
            os.environ, {"FAKE_HERDR_IMPLEMENTER_KIND": "claude"}
        ):
            report = self.report()
        self.assertFalse(report.ok)
        self.assertIn("expected 'codex'", self.details(report))

    def test_invalid_placement_and_unwritable_control_storage(self):
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
    ):
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

    def test_active_identity_and_missing_expected_path_are_errors(self):
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

    def prepare_review(self):
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

    def test_busy_run_lock_is_reported_without_waiting(self):
        from agent_squad.doctor import _check_status
        from agent_squad.initialization import AgentSquadError
        from agent_squad.storage import exclusive_file_lock

        task = self.root / "task.md"
        task.write_text("Implement fixture")
        start_run(self.repo, task_path=task)
        with exclusive_file_lock(self.repo / ".agent-squad/lock"):
            with self.assertRaisesRegex(AgentSquadError, "run lock is busy"):
                _check_status(self.repo)

    def test_healthy_active_review_is_not_reported_as_orphan(self):
        submitted = self.prepare_review()
        report = self.report()
        self.assertTrue(report.ok, self.details(report))
        self.assertFalse(
            any(d.check == "residual resources" for d in report.diagnostics)
        )
        self.assertTrue(submitted.review_worktree.exists())

    def test_sibling_implementation_history_prevents_false_orphans(self):
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

    def test_mismatched_review_head_and_terminal_resources_are_retained(self):
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
    ):
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

    def test_live_missing_close_capability_does_not_launch(self):
        with mock.patch.dict(
            os.environ, {"FAKE_HERDR_SCHEMA_MISSING": "workspace.close"}
        ):
            report = self.report(live_reviewer=True)
        self.assertIn("workspace.close", self.details(report))
        self.assertFalse(list(self.reviews.glob(".preflight-*")))

    def test_live_launch_and_cleanup_failures_retain_manifest(self):
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
            len(list(self.reviews.glob(".preflight-*/diagnostic.json"))), 3
        )

    def test_installed_kind_and_stale_integration_are_reported(self):
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

    def test_live_blocked_reviewer_retains_inspectable_history(self):
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
            len(list(self.reviews.glob(".preflight-*/history.txt"))), 1
        )

    def test_failed_worktree_removal_retains_probe_and_existing_resources(
        self,
    ):
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
        self.assertTrue(list(self.reviews.glob(".doctor-*/snapshot/.git")))
        self.assertEqual((unrelated / "data").read_text(), "preserve")

    def test_sha256_repository_passes_normal_and_live_checks(self):
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

    def test_live_preflight_proves_actual_submission_and_cleans_owned_resources(
        self,
    ):
        report = self.report(live_reviewer=True, timeout_seconds=1)
        self.assertTrue(report.ok, self.details(report))
        self.assertEqual(list(self.reviews.iterdir()), [])
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

    def test_live_failures_retain_evidence_and_never_close_resources(self):
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
        evidence = list(self.reviews.glob(".preflight-*/diagnostic.json"))
        self.assertEqual(len(evidence), 5)
        self.assertFalse(
            any(
                c[:2] == ["workspace", "close"] and "--help" not in c
                for c in self.commands()
            )
        )

    def test_live_cleanup_refuses_workspace_with_added_pane(self):
        with mock.patch.dict(os.environ, {"FAKE_HERDR_PANE_COUNT": "2"}):
            report = self.report(live_reviewer=True, timeout_seconds=1)
        self.assertFalse(report.ok)
        self.assertIn("no longer isolated", self.details(report))
        self.assertTrue(
            list(
                self.reviews.glob(
                    ".preflight-*/*/round-001/.agent-squad-review/local-state.json"
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
