"""Initialisation and read-only diagnostics using the real HTTP transport."""

import json
from pathlib import Path
import shutil
import sys
import unittest

from tests.forge_support import ForgeFixture, ForgejoFixture


def init_args(f: ForgejoFixture) -> list[str]:
    for account in ("developer", "reviewer"):
        token = f.root / f"{account}.token"
        token.write_text(f"fake-token-{account}\n")
        token.chmod(0o600)
    return [
        "init", "--forge", "forgejo", "--base-url", f.server.base_url + "/",
        "--implementer-account", "developer",
        "--reviewer-account", "reviewer",
        "--implementer-token-file", str(f.root / "developer.token"),
        "--reviewer-token-file", str(f.root / "reviewer.token"),
    ]


def without_gh(f: ForgeFixture) -> None:
    """A PATH containing just the fixture's prerequisites, without gh."""
    (f.bin / "gh").unlink()
    (f.bin / "git").symlink_to(shutil.which("git"))
    (f.bin / "python3").symlink_to(sys.executable)
    f.env["PATH"] = str(f.bin)
    assert shutil.which("gh", path=f.env["PATH"]) is None


class ForgejoInitDoctorTests(unittest.TestCase):
    def test_init_prefix_no_gh_and_linked_worktree(self) -> None:
        with ForgejoFixture(path_prefix="/forge/instance") as f:
            args = init_args(f)
            without_gh(f)
            self.assertTrue(f.cli(*args)["created"])
            config_path = f.repo / ".agent-squad/config.json"
            config = json.loads(config_path.read_text())
            self.assertEqual(config["schema_version"], 2)
            self.assertEqual(config["forge"], {
                "kind": "forgejo", "owner": "MagiLand", "repo": "trial",
                "base_url": f.server.base_url,
            })
            self.assertNotIn("fake-token-", config_path.read_text())
            self.assertEqual(f.read_model()["calls"], [{
                "method": "GET",
                "path": "/forge/instance/api/v1/repos/MagiLand/trial",
                "Authorization": "token fake-token-developer",
                "body": None,
            }])
            linked = f.root / "linked"
            f.git("worktree", "add", "--detach", str(linked))
            for cwd in (f.repo, linked):
                result = f.cli("doctor", cwd=cwd)
                self.assertTrue(result["ok"])
                checks = {d["check"]: d for d in result["diagnostics"]}
                self.assertEqual(
                    checks["forge client"]["detail"], "16.0.3",
                )
                for name in (
                    "distinct forge identities",
                    "Reviewer write permission",
                ):
                    self.assertEqual(checks[name]["severity"], "pass")
                self.assertNotIn("fake-token-", json.dumps(result))
            calls = f.read_model()["calls"]
            self.assertTrue(all(c["method"] == "GET" for c in calls))
            prefix = "/forge/instance/api/v1"
            developer = "token fake-token-developer"
            reviewer = "token fake-token-reviewer"
            expected_calls = [(prefix + "/repos/MagiLand/trial", developer)]
            for _ in (f.repo, linked):
                expected_calls.extend([
                    (prefix + "/version", developer),
                    (prefix + "/user", developer),
                    (prefix + "/repos/MagiLand/trial", developer),
                    (prefix + "/user", reviewer),
                    (prefix + "/repos/MagiLand/trial", reviewer),
                    (prefix + "/repos/MagiLand/trial", reviewer),
                ])
            self.assertEqual(
                [(c["path"], c["Authorization"]) for c in calls],
                expected_calls,
            )

    def test_existing_config_validated_kept_and_new_differences_reported(
        self,
    ) -> None:
        with ForgejoFixture() as f:
            args = init_args(f)
            f.cli(*args)
            path = f.repo / ".agent-squad/config.json"
            original = path.read_bytes()
            result = f.cli("init", "--implementer-account", "developer",
                           "--reviewer-account", "other")
            self.assertFalse(result["created"])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual({"forge", "implementer", "reviewer"},
                             result["differences"].keys())
            model = f.read_model()
            model["calls"] = []
            f.save_model(model)
            (f.root / "developer.token").chmod(0o644)
            f.cli(*args, expected=1)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(f.read_model()["calls"], [])

    def test_missing_flags_are_usage_errors_before_requests_or_writes(
        self,
    ) -> None:
        with ForgejoFixture() as f:
            args = init_args(f)
            for name in ("--base-url", "--implementer-token-file",
                         "--reviewer-token-file"):
                with self.subTest(name=name):
                    bad = args.copy()
                    index = bad.index(name)
                    del bad[index:index + 2]
                    self.assertIn(name, f.cli(*bad, expected=2)["error"])
            self.assertEqual(f.read_model()["calls"], [])
            self.assertFalse((f.repo / ".agent-squad").exists())

    def test_url_and_identity_rules_at_init_and_configuration_load(
        self,
    ) -> None:
        cases = [
            ("forge", "base_url", "http://untrusted.invalid", "--base-url"),
            ("forge", "base_url", "https://forge.invalid?q=1", "--base-url"),
            ("forge", "base_url", "https://forge.invalid#f", "--base-url"),
            ("forge", "base_url", "/relative", "--base-url"),
            ("forge", "kind", "github", "--forge"),
            ("reviewer", "forge_account", "DEVELOPER", "--reviewer-account"),
        ]
        with ForgejoFixture() as f:
            f.configure()
            path = f.repo / ".agent-squad/config.json"
            valid = json.loads(path.read_text())
            for section, key, value, flag in cases:
                with self.subTest(key=key, value=value):
                    args = init_args(f)
                    args[args.index(flag) + 1] = value
                    path.unlink(missing_ok=True)
                    f.cli(*args, expected=1)
                    self.assertFalse(path.exists())
                    data = json.loads(json.dumps(valid))
                    data[section][key] = value
                    path.write_text(json.dumps(data))
                    f.cli("issue", "view", "--issue", "1", expected=1)
                    f.cli("doctor", expected=1)
                    self.assertEqual(f.read_model()["calls"], [])

    def test_every_token_file_rule_at_init_and_load_before_any_request(
        self,
    ) -> None:
        with ForgejoFixture() as f:
            f.configure()
            path = f.repo / ".agent-squad/config.json"
            valid = json.loads(path.read_text())
            linked = f.root / "linked"
            f.git("worktree", "add", "--detach", str(linked))
            bad = f.root / "bad.token"
            symlink = f.root / "alias.token"
            symlink.symlink_to(bad)
            cases = [
                (bad, 0o604, "synthetic-private\n"),
                (bad, 0o640, "synthetic-private\n"),
                (f.repo / "inside.token", 0o600, "synthetic-private\n"),
                (linked / "inside.token", 0o600, "synthetic-private\n"),
                (path.parent / "inside.token", 0o600, "synthetic-private\n"),
                (bad, 0o600, ""), (bad, 0o600, "first\nsecond\n"),
                (symlink, 0o600, "synthetic-private\n"),
                (Path("relative.token"), 0o600, "synthetic-private\n"),
            ]
            for token, mode, content in cases:
                with self.subTest(token=token, mode=mode, content=content):
                    if token.is_absolute():
                        token.write_text(content)
                        token.chmod(mode)
                    args = init_args(f)
                    args[args.index("--reviewer-token-file") + 1] = str(token)
                    path.unlink(missing_ok=True)
                    result = f.cli(*args, expected=1)
                    self.assertNotIn("synthetic-private", json.dumps(result))
                    self.assertFalse(path.exists())
                    data = json.loads(json.dumps(valid))
                    data["reviewer"]["token_file"] = str(token)
                    path.write_text(json.dumps(data))
                    for cwd in (f.repo, linked):
                        result = f.cli("doctor", cwd=cwd, expected=1)
                        self.assertIn("reviewer", json.dumps(result))
                        self.assertNotIn(
                            "synthetic-private", json.dumps(result),
                        )
                    f.cli("issue", "view", "--issue", "1", expected=1)
                    self.assertEqual(f.read_model()["calls"], [])

    def test_version_identity_repository_and_permission_failures(self) -> None:
        with ForgejoFixture() as f:
            f.cli(*init_args(f))
            for settings, name in (
                ({"version": "15.0.9"}, "forge client"),
                ({"version": "malformed"}, "forge client"),
                ({"login_mismatch": "wrong"}, "reviewer forge identity"),
                ({"repository_missing": True},
                 "implementer repository access"),
                ({"permission": "read"}, "Reviewer write permission"),
            ):
                with self.subTest(settings=settings):
                    model = f.read_model()
                    model["settings"] = settings
                    f.save_model(model)
                    result = f.cli("doctor", expected=1)
                    checks = {d["check"]: d for d in result["diagnostics"]}
                    self.assertEqual(checks[name]["severity"], "fail")
                    self.assertNotIn("fake-token-", json.dumps(result))
            model = f.read_model()
            model["settings"] = {"version": "16.0.0"}
            f.save_model(model)
            self.assertTrue(f.cli("doctor")["ok"])

    def test_github_still_requires_gh(self) -> None:
        with ForgeFixture() as f:
            f.initialize()
            without_gh(f)
            result = f.cli("doctor", expected=1)
            self.assertTrue(any("gh" in d["detail"]
                                for d in result["diagnostics"]
                                if d["severity"] == "fail"))

    def test_closed_and_merged_orphans_use_neutral_pr_read(self) -> None:
        with ForgejoFixture() as f:
            f.cli(*init_args(f))
            head = f.candidate()
            f.seed_read_scenario()
            resource = (f.repo / ".agent-squad/worktrees"
                        / f"reviewer-pr1-{head[:7]}")
            f.git("worktree", "add", "--detach", str(resource))
            for merged in (False, True):
                with self.subTest(merged=merged):
                    model = f.read_model()
                    model["prs"]["1"].update(state="closed", merged=merged)
                    f.save_model(model)
                    result = f.cli("doctor")
                    orphans = [d for d in result["diagnostics"]
                               if d["check"] == "orphan worktree"]
                    self.assertEqual(len(orphans), 1)
                    self.assertEqual(orphans[0]["severity"], "warn")
                    self.assertIn("merged" if merged else "closed",
                                  orphans[0]["detail"])
                    self.assertTrue(resource.exists())
            f.settings(login_mismatch="wrong-account")
            result = f.cli("doctor", expected=1)
            self.assertTrue(any(d["check"] == "orphan worktree"
                                and d["severity"] == "fail"
                                and "forge identity unavailable" in d["detail"]
                                for d in result["diagnostics"]))
            self.assertTrue(resource.exists())
