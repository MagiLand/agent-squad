"""Prerequisite checks and configuration discovery across worktrees."""

import argparse
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from tests.forge_support import ForgeFixture
from agent_squad.initialization import load_initialized_repository


class InitDoctorTests(unittest.TestCase):
    def test_loading_repository_reads_git_startup_facts_once(self) -> None:
        with ForgeFixture() as f:
            f.initialize()
            f.candidate()
            nested = f.worktree / "nested directory"
            nested.mkdir()
            for start in (f.repo, f.worktree, nested):
                with (
                    self.subTest(start=start),
                    patch.dict(os.environ, f.env, clear=True),
                    patch("subprocess.Popen", wraps=subprocess.Popen) as popen,
                ):
                    repository = load_initialized_repository(start)
                git_calls = [
                    call.args[0] for call in popen.call_args_list
                    if call.args[0][0] == "git"
                ]
                self.assertEqual(
                    [args[1] for args in git_calls], ["rev-parse", "worktree"],
                )
                self.assertEqual(repository.primary, f.repo)
                self.assertEqual(repository.common, f.repo / ".git")
                self.assertEqual(
                    repository.root, f.repo if start == f.repo else f.worktree,
                )

    def test_outside_git_preserves_discovery_error(self) -> None:
        with ForgeFixture() as f:
            expected = f.run([
                "git", "rev-parse", "--is-bare-repository",
            ], cwd=f.root)
            result = f.cli("issue", "view", "--issue", "1",
                           cwd=f.root, expected=1)
            self.assertEqual(
                result["error"],
                "error: " + " ".join(expected.stderr.strip().splitlines()),
            )

    def test_discovery_preserves_newlines_in_repository_paths(self) -> None:
        with ForgeFixture() as f:
            renamed = f.repo.with_name("repository\ncontinued")
            f.repo.rename(renamed)
            f.repo = f.worktree = renamed
            f.initialize()
            f.candidate()
            with patch.dict(os.environ, f.env, clear=True):
                for start in (f.repo, f.worktree):
                    with self.subTest(start=start):
                        repository = load_initialized_repository(start)
                        self.assertEqual(repository.root, start)
                        self.assertEqual(repository.common, f.repo / ".git")

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


class IdentityConfigurationTests(unittest.TestCase):
    REMOVED = (
        "single-identity mode was removed in v0.7.0; move config.json aside"
        " and rerun agent-squad init with two accounts"
    )

    def test_init_rejects_removed_options_and_equal_accounts(self) -> None:
        with ForgeFixture() as f:
            path = f.repo / ".agent-squad/config.json"
            for extra in (("--identity-mode", "single"),
                          ("--identity-mode", "dual"),
                          ("--approver-account", "human")):
                with self.subTest(extra=extra):
                    result = f.cli(
                        "init", "--implementer-account", "developer",
                        "--reviewer-account", "reviewer", *extra, expected=2,
                    )
                    self.assertIn("unrecognized arguments", result["error"])
                    self.assertFalse(path.exists())
            result = f.cli("init", "--implementer-account", "developer",
                           "--reviewer-account", "DEVELOPER", expected=1)
            self.assertIn("Implementer and Reviewer accounts must differ",
                          result["error"])
            self.assertFalse(path.exists())
            self.assertEqual(f.read_model()["calls"], [])

    def test_init_writes_neither_legacy_key(self) -> None:
        with ForgeFixture() as f:
            f.initialize()
            data = json.loads(
                (f.repo / ".agent-squad/config.json").read_text())
            self.assertNotIn("identity_mode", data)
            self.assertNotIn("approver_accounts", data)

    def test_v061_dual_configuration_loads_unchanged(self) -> None:
        with ForgeFixture() as f:
            f.initialize()
            path = f.repo / ".agent-squad/config.json"
            data = json.loads(path.read_text())
            data.update(identity_mode="dual", approver_accounts=[])
            path.write_text(json.dumps(data, indent=2) + "\n")
            original = path.read_bytes()
            self.assertTrue(f.cli("doctor")["ok"])
            self.assertEqual(
                f.cli("issue", "view", "--issue", "1")["number"], 1)
            result = f.cli("init", "--implementer-account", "developer",
                           "--reviewer-account", "reviewer")
            self.assertFalse(result["created"])
            self.assertEqual(result["differences"], {})
            self.assertEqual(path.read_bytes(), original)

    def test_single_configuration_is_refused_by_every_command(self) -> None:
        from agent_squad.cli import parser

        def leaves(current, prefix=()):
            children = [a for a in current._actions
                        if isinstance(a, argparse._SubParsersAction)]
            if not children:
                return [(prefix, current)]
            return [leaf for name, child in children[0].choices.items()
                    for leaf in leaves(child, (*prefix, name))]

        with ForgeFixture() as f:
            f.initialize()
            path = f.repo / ".agent-squad/config.json"
            data = json.loads(path.read_text())
            data.update(identity_mode="single", approver_accounts=["human"])
            data["reviewer"]["forge_account"] = "developer"
            path.write_text(json.dumps(data, indent=2) + "\n")
            original = path.read_bytes()
            calls = len(f.read_model()["calls"])
            commands = 0
            for words, command in leaves(parser()):
                if words == ("skill", "install"):
                    continue
                args = list(words)
                for action in command._actions:
                    if action.required and action.option_strings:
                        value = (
                            action.choices[0] if action.choices
                            else "1" if action.type is not None
                            else action.dest
                        )
                        args += [action.option_strings[0], value]
                with self.subTest(command=words):
                    result = f.cli(*args, expected=1, cwd=f.repo)
                    text = (
                        result["diagnostics"][0]["detail"]
                        if words == ("doctor",) else result["error"]
                    )
                    self.assertIn(self.REMOVED, text)
                    commands += 1
            self.assertEqual(commands, 24)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(f.read_model()["calls"]), calls)
