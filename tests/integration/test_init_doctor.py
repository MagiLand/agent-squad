"""Prerequisite checks and configuration discovery across worktrees."""

import json
import unittest

from tests.forge_support import ForgeFixture


class InitDoctorTests(unittest.TestCase):
    def test_bare_repository_is_refused_before_configuration_writes(
        self,
    ) -> None:
        with ForgeFixture() as f:
            result = f.cli(
                "init",
                "--implementer-account",
                "developer",
                "--reviewer-account",
                "reviewer",
                cwd=f.origin,
                expected=1,
            )
            self.assertIn("non-bare Git worktree is required", result["error"])
            self.assertFalse((f.origin / ".agent-squad").exists())

    def test_missing_base_branch_prevents_initialization(self) -> None:
        with ForgeFixture() as f:
            result = f.cli(
                "init",
                "--implementer-account",
                "developer",
                "--reviewer-account",
                "reviewer",
                "--base-branch",
                "missing",
                expected=1,
            )
            self.assertIn(
                "base_branch does not exist on origin", result["error"]
            )
            self.assertFalse((f.repo / ".agent-squad/config.json").exists())

    def test_symlinked_control_root_and_configuration_are_refused(
        self,
    ) -> None:
        for target in ("control", "config"):
            with self.subTest(target=target), ForgeFixture() as f:
                f.initialize()
                control = f.repo / ".agent-squad"
                path = (
                    control if target == "control" else control / "config.json"
                )
                saved = f.root / "saved"
                path.rename(saved)
                path.symlink_to(saved, target_is_directory=target == "control")
                result = f.cli("issue", "view", "--issue", "1", expected=1)
                self.assertIn("must not be symlinks", result["error"])
                self.assertTrue(path.is_symlink())

    def test_init_defaults_remote_default_and_no_overwrite(self) -> None:
        with ForgeFixture() as f:
            f.git("branch", "-m", "trunk")
            f.git("push", "origin", "trunk")
            f.git("symbolic-ref", "HEAD", "refs/heads/trunk", cwd=f.origin)
            f.initialize()
            path = f.repo / ".agent-squad/config.json"
            config = json.loads(path.read_text())
            self.assertEqual(config["base_branch"], "trunk")
            self.assertEqual(
                config["forge"],
                {"kind": "github", "owner": "MagiLand", "repo": "trial"},
            )
            config["max_review_passes"] = 5
            path.write_text(json.dumps(config))
            original = path.read_bytes()
            result = f.cli(
                "init",
                "--implementer-account",
                "developer",
                "--reviewer-account",
                "reviewer",
            )
            self.assertFalse(result["created"])
            self.assertIn("max_review_passes", result["differences"])
            self.assertEqual(path.read_bytes(), original)
            exclude = (f.repo / ".git/info/exclude").read_text()
            self.assertEqual(exclude.splitlines().count(".agent-squad/"), 1)
            self.assertEqual(
                exclude.splitlines().count(".agent-squad-review/"), 1
            )

    def test_config_refusal_applies_to_all_read_commands(self) -> None:
        with ForgeFixture() as f:
            for args in [
                ("status", "--pr", "1"),
                ("issue", "view", "--issue", "1"),
                ("pr", "head", "--pr", "1"),
                ("pr", "reviews", "--pr", "1"),
            ]:
                result = f.cli(*args, expected=1)
                self.assertIn("agent-squad init", result["error"])
            f.initialize()
            path = f.repo / ".agent-squad/config.json"
            path.write_text('{"schema_version": 1}')
            f.cli(
                "init",
                "--implementer-account",
                "developer",
                "--reviewer-account",
                "reviewer",
                expected=1,
            )
            self.assertEqual(path.read_text(), '{"schema_version": 1}')

    def test_doctor_retained_checks_and_missing_implementer_warning(
        self,
    ) -> None:
        with ForgeFixture() as f:
            f.initialize()
            self.assertTrue(f.cli("doctor")["ok"])
            f.env["FAKE_HERDR_MISSING_AGENT"] = "1"
            result = f.cli("doctor")
            self.assertTrue(
                any(d["severity"] == "warn" for d in result["diagnostics"])
            )
            f.env.pop("FAKE_HERDR_MISSING_AGENT")
            for key, value in [
                ("FAKE_HERDR_PROTOCOL", "99"),
                ("FAKE_HERDR_INTEGRATION", "outdated"),
                ("FAKE_HERDR_SCHEMA_MISSING", "agent.get"),
            ]:
                with self.subTest(key=key):
                    f.env[key] = value
                    f.cli("doctor", expected=1)
                    f.env.pop(key)
            path = f.repo / ".git/info/exclude"
            path.write_text("")
            f.cli("doctor", expected=1)
            f.initialize()
            (f.repo / ".gitmodules").write_text("")
            result = f.cli("doctor")
            self.assertTrue(
                any(
                    d["check"] == "submodules" and d["severity"] == "warn"
                    for d in result["diagnostics"]
                )
            )

    def test_cli_usage_surface_and_required_roles(self) -> None:
        with ForgeFixture() as f:
            f.initialize()
            for command in (
                "start",
                "submit",
                "review-submit",
                "apply-review",
                "supersede",
                "retry-handoff",
                "escalate",
                "resume",
                "cancel",
                "complete",
                "reviewer",
                "review-worktree",
                "handoff",
                "skill",
            ):
                f.cli(command, expected=2)
            f.cli("review", "post", "--pr", "1", expected=2)
            f.cli(
                "thread",
                "resolve",
                "--as",
                "implementer",
                "--pr",
                "1",
                "--finding",
                "REV-1",
                expected=2,
            )
            self.assertTrue(f.cli("doctor", "--live-reviewer")["ok"])


class SingleInitTests(unittest.TestCase):
    def test_missing_approver_is_usage_error_and_writes_nothing(self) -> None:
        with ForgeFixture() as f:
            result = f.cli('init', '--implementer-account', 'developer',
                           '--reviewer-account', 'developer',
                           '--identity-mode', 'single', expected=2)
            self.assertIn('--approver-account', result['error'])
            self.assertFalse((f.repo / '.agent-squad/config.json').exists())

    def test_single_repeatable_approvers_and_dual_defaults(self) -> None:
        with ForgeFixture() as f:
            f.cli('init', '--implementer-account', 'developer',
                  '--reviewer-account', 'DEVELOPER', '--identity-mode', 'single',
                  '--approver-account', 'human', '--approver-account', 'other')
            path = f.repo / '.agent-squad/config.json'
            data = json.loads(path.read_text())
            self.assertEqual(data['identity_mode'], 'single')
            self.assertEqual(data['approver_accounts'], ['human', 'other'])
            original = path.read_bytes()
            f.initialize()
            self.assertEqual(path.read_bytes(), original)
        with ForgeFixture() as f:
            f.initialize()
            data = json.loads(
                (f.repo / '.agent-squad/config.json').read_text())
            self.assertEqual(data['identity_mode'], 'dual')
            self.assertEqual(data['approver_accounts'], [])
