"""Every deterministic prerequisite, including recorded trial failures."""

from collections import Counter
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

from tests.forge_support import ForgeFixture
from agent_squad.doctor import diagnose
from agent_squad.herdr import HerdrClient, HerdrError
from agent_squad.initialization import AgentKind, git_output


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = ForgeFixture()
        self.addCleanup(self.f.close)
        self.f.initialize()

    def diagnostic(self, result: dict, name: str,
                   severity: str = "fail") -> dict:
        matches = [d for d in result["diagnostics"] if d["check"] == name]
        self.assertEqual(len(matches), 1, result)
        self.assertEqual(matches[0]["severity"], severity, matches[0])
        return matches[0]

    def configuration(self, **values: object) -> None:
        path = self.f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config.update(values)
        path.write_text(json.dumps(config))

    def test_discovery_probes_run_once_and_integration_runs_per_role(
        self,
    ) -> None:
        self.assertTrue(self.f.cli("doctor")["ok"])
        calls = Counter(tuple(call) for call in self.f.herdr_model()["calls"])
        for command in (
            ("--version",), ("api", "schema", "--json"),
            *(args for args, _ in HerdrClient._HELP_CHECKS),
        ):
            with self.subTest(command=command):
                self.assertEqual(calls[command], 1)
        self.assertEqual(calls[("integration", "status")], 2)
        # One discovery snapshot and the separate, fresh orphan inventory.
        self.assertEqual(calls[("api", "snapshot")], 2)

    def test_shared_contract_failure_preserves_both_role_rows(self) -> None:
        self.f.env["FAKE_HERDR_SCHEMA_MISSING"] = "agent.get"
        result = self.f.cli("doctor", expected=1)
        for role in ("Reviewer", "Implementer"):
            self.assertEqual(
                self.diagnostic(
                    result, f"Herdr {role} discovery and integration",
                )["detail"],
                "installed Herdr schema is missing required method "
                "contract agent.get -> AgentTarget",
            )
        calls = Counter(tuple(call) for call in self.f.herdr_model()["calls"])
        self.assertEqual(calls[("--version",)], 1)
        self.assertEqual(calls[("api", "schema", "--json")], 1)
        self.assertEqual(calls[("integration", "status")], 0)

    def test_shared_discovery_preserves_per_role_failure_precedence(
        self,
    ) -> None:
        roles = {"Reviewer": AgentKind.CLAUDE, "Implementer": AgentKind.CODEX}
        with patch.dict(os.environ, self.f.env, clear=True):
            client = HerdrClient(self.f.repo)
            original = client._run

            def run(arguments, **kwargs):
                result = original(arguments, **kwargs)
                if arguments == ("agent", "start", "--help"):
                    result.stdout = result.stdout.replace(
                        "codex, claude", "codex",
                    )
                if arguments == ("worktree", "--help"):
                    result.stdout = ""
                return result

            with patch.object(client, "_run", side_effect=run):
                shared = client.discover_roles(roles)
                for role, kind in roles.items():
                    with self.subTest(role=role):
                        with self.assertRaises(HerdrError) as single:
                            client.discover(kind, role=role)
                        self.assertEqual(
                            str(shared[role]), str(single.exception),
                        )
            self.assertIn(
                "does not advertise Reviewer kind", str(shared["Reviewer"]),
            )
            self.assertIn("required syntax", str(shared["Implementer"]))

    def test_all_checks_pass_without_mutating_user_resources(self) -> None:
        f = self.f
        before = f.git("worktree", "list", "--porcelain")
        config = (f.repo / ".agent-squad/config.json").read_bytes()
        exclude = (f.repo / ".git/info/exclude").read_bytes()
        result = f.cli("doctor")
        self.assertTrue(result["ok"])
        self.assertTrue(
            all(d["severity"] == "pass" for d in result["diagnostics"]))
        self.assertEqual(f.git("worktree", "list", "--porcelain"), before)
        self.assertEqual(
            (f.repo / ".agent-squad/config.json").read_bytes(), config)
        self.assertEqual((f.repo / ".git/info/exclude").read_bytes(), exclude)
        self.assertEqual(f.git("status", "--porcelain"), "")
        self.assertEqual(f.herdr_model()["workspaces"], [])
        for call in f.read_model()["calls"]:
            if call["arguments"][0] == "api":
                args = call["arguments"]
                self.assertEqual(args[args.index("--method") + 1], "GET")
        self.assertNotIn("fake-token-", json.dumps(result))

    def test_text_is_one_line_per_check_and_json_is_one_object_on_failure(
            self) -> None:
        f = self.f
        f.settings(version="fixture version\nsecond line")
        (f.repo / ".gitmodules").write_text("")
        result = f.run([sys.executable, "-m", "agent_squad", "doctor"])
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(all(line.startswith(("PASS ", "WARN ", "FAIL "))
                        for line in lines))
        self.assertTrue(any(line.startswith("WARN submodules:")
                        for line in lines))
        f.settings(user_failure=["reviewer"])
        failed = f.cli("doctor", expected=1)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["exit_code"], 1)
        self.diagnostic(failed, "reviewer forge identity")
        self.assertNotIn("fake-token-", json.dumps(failed))

    def test_repository_configuration_missing_bare_and_invalid(self) -> None:
        f = self.f
        for cwd in (f.origin, f.root):
            self.diagnostic(f.cli("doctor", cwd=cwd, expected=1),
                            "repository and configuration")
        path = f.repo / ".agent-squad/config.json"
        path.unlink()
        self.diagnostic(f.cli("doctor", expected=1),
                        "repository and configuration")
        path.write_text('{"schema_version": 1}')
        result = f.cli("doctor", expected=1)
        self.assertIn("schema 1", self.diagnostic(
            result, "repository and configuration")["detail"])

    def test_accounts_must_differ_ignoring_case(self) -> None:
        self.configuration(
            reviewer={
                "kind": "claude",
                "start_args": [],
                "forge_account": "DEVELOPER"})
        result = self.f.cli("doctor", expected=1)
        self.assertIn("must differ", self.diagnostic(
            result, "repository and configuration")["detail"])

    def test_trial_issue44_credential_store_failure_for_both_roles(
            self) -> None:
        f = self.f
        f.settings(token_failure=["developer", "reviewer"])
        result = f.cli("doctor", expected=1)
        for role in ("implementer", "reviewer"):
            item = self.diagnostic(result, f"{role} forge identity")
            self.assertIn("cannot resolve token", item["detail"])
        self.assertNotIn("fake-token-", json.dumps(result))
        self.assertTrue(
            any(d["check"] == "installed code-review"
                for d in result["diagnostics"]))

    def test_token_empty_or_multiline_responses_are_refused(self) -> None:
        for response in ("", "fake-token-reviewer\nextra"):
            with self.subTest(response=response):
                self.f.settings(token_response={"reviewer": response})
                result = self.f.cli("doctor", expected=1)
                self.diagnostic(result, "reviewer forge identity")
                self.assertNotIn("fake-token-", json.dumps(result))

    def test_get_user_mismatch_malformed_and_unavailable(self) -> None:
        for role, account in (
            ("implementer", "developer"),
                ("reviewer", "reviewer")):
            for value in (
                {"login": "wrong"},
                {},
                [],
                {"login": 42},
                    {"login": ""}):
                with self.subTest(role=role, value=value):
                    self.f.settings(user_response={account: value})
                    result = self.f.cli("doctor", expected=1)
                    self.diagnostic(result, f"{role} forge identity")
                    self.assertNotIn("fake-token-", json.dumps(result))
        self.f.settings(
            user_response={
                "developer": {
                    "login": "DEVELOPER"}, "reviewer": {
                    "login": "REVIEWER"}})
        self.assertTrue(self.f.cli("doctor")["ok"])

    def test_both_accounts_must_read_the_repository(self) -> None:
        self.f.settings(repository_denied=["developer", "reviewer"])
        result = self.f.cli("doctor", expected=1)
        for role in ("implementer", "reviewer"):
            self.diagnostic(result, f"{role} repository access")
        self.assertNotIn("fake-token-", json.dumps(result))

    def test_reviewer_permission_uses_base_permission_not_role_name(
            self) -> None:
        for permission, role, expected in (
            ("write", "maintain", 0), ("admin", "admin", 0),
            ("write", "custom-reviewer", 0), ("read", "triage", 1),
            ("none", "write", 1), ("unexpected", "admin", 1),
        ):
            with self.subTest(permission=permission, role=role):
                self.f.settings(permission=permission, role_name=role)
                result = self.f.cli("doctor", expected=expected)
                self.diagnostic(result, "Reviewer write permission",
                                "fail" if expected else "pass")

    def test_permission_lookup_failures_are_not_success(self) -> None:
        f = self.f
        for response in (
            {}, [], {
                "permission": "write", "user": {
                "login": "someone-else"}}):
            with self.subTest(response=response):
                f.settings(permission_response=response)
                self.diagnostic(f.cli("doctor", expected=1),
                                "Reviewer write permission")
        f.settings(permission_failure=True)
        result = f.cli("doctor", expected=1)
        self.diagnostic(result, "Reviewer write permission")
        self.assertNotIn("fake-token-", json.dumps(result))

    def test_gh_and_herdr_missing_on_path(self) -> None:
        f = self.f
        original = shutil.which
        for missing in ("gh", "herdr"):
            def which(name):
                return None if name == missing else original(name)

            with (
                self.subTest(missing=missing),
                patch.dict(os.environ, f.env, clear=True),
                patch("shutil.which", side_effect=which),
            ):
                result = diagnose(f.repo)
                self.assertFalse(result["ok"])
                self.assertTrue(
                    any(
                        "not" in d["detail"]
                        and missing in d["detail"].lower()
                        for d in result["diagnostics"]
                        if d["severity"] == "fail"))

    def test_empty_gh_version_is_a_failure(self) -> None:
        self.f.settings(version="")
        self.diagnostic(self.f.cli("doctor", expected=1), "forge client")

    def test_remote_base_must_exist_now_not_only_in_local_refs(self) -> None:
        f = self.f
        f.git("update-ref", "refs/remotes/origin/removed", f.base)
        self.configuration(base_branch="removed")
        self.diagnostic(f.cli("doctor", expected=1), "remote base branch")
        self.configuration(base_branch="ma*")
        self.diagnostic(f.cli("doctor", expected=1), "remote base branch")

    def test_trial_issue42_remote_transport_failure_is_reported(self) -> None:
        f = self.f
        remote = str(f.root / "unreachable.git")
        f.git("remote", "set-url", "origin", remote)
        result = f.cli("doctor", expected=1)
        item = self.diagnostic(result, "remote base branch")
        self.assertIn("Could not read from remote repository", item["detail"])
        self.diagnostic(result, "reviewer forge identity", "pass")
        self.assertEqual(f.git("remote", "get-url", "origin"), remote)

    def test_exclusions_are_checked_in_common_directory_from_linked_worktree(
            self) -> None:
        f = self.f
        f.candidate()
        self.diagnostic(f.cli("doctor"), "local exclusions", "pass")
        path = f.repo / ".git/info/exclude"
        for content in (".agent-squad/\n", ".agent-squad-review/\n"):
            path.write_text(content)
            self.diagnostic(f.cli("doctor", expected=1), "local exclusions")
            self.assertEqual(path.read_text(), content)

    def test_roots_overlap_or_escape_control_directory(self) -> None:
        for values in (
            {"worktree_root": "worktrees"},
            {"scratch_root": ".agent-squad/worktrees/scratch"},
            {"scratch_root": ".agent-squad/worktrees"},
        ):
            with self.subTest(values=values):
                self.configuration(worktree_root=".agent-squad/worktrees",
                                   scratch_root=".agent-squad/review-scratch")
                self.configuration(**values)
                self.diagnostic(self.f.cli("doctor", expected=1),
                                "repository and configuration")

    def test_missing_roots_are_probed_and_not_left_as_repairs(self) -> None:
        f = self.f
        parent = f.root / "missing/nested"
        self.configuration(worktree_root=str(
            parent / "worktrees"), scratch_root=str(parent / "scratch"))
        self.assertTrue(f.cli("doctor")["ok"])
        self.assertFalse((f.root / "missing").exists())

    def test_roots_that_are_files_fail_without_removal(self) -> None:
        f = self.f
        for field, check in (
            ("worktree_root", "worktree root"),
                ("scratch_root", "scratch root")):
            target = f.root / field
            target.write_text("preserve")
            self.configuration(**{field: str(target)})
            result = f.cli("doctor", expected=1)
            self.diagnostic(result, check)
            self.assertEqual(target.read_text(), "preserve")

    def test_control_root_write_failure_does_not_hide_other_checks(
            self) -> None:
        f = self.f
        import agent_squad.doctor as doctor
        original = doctor.tempfile.TemporaryFile

        def probe(*args, **kwargs):
            if kwargs.get("dir") == f.repo / ".agent-squad":
                raise PermissionError("control root is read-only")
            return original(*args, **kwargs)

        with (
            patch.dict(os.environ, f.env, clear=True),
            patch.object(doctor.tempfile, "TemporaryFile", side_effect=probe),
        ):
            result = diagnose(f.repo)
        self.diagnostic(result, "control root")
        self.diagnostic(result, "installed code-review", "pass")

    def test_disposable_worktree_add_and_remove_failures_report_residue(
            self) -> None:
        f = self.f
        for operation in ("add", "remove"):
            def git(root, *args):
                if args[:2] == ("worktree", operation):
                    from agent_squad.initialization import AgentSquadError
                    raise AgentSquadError("scripted worktree failure")
                return git_output(root, *args)

            with (
                self.subTest(operation=operation),
                patch.dict(os.environ, f.env, clear=True),
                patch("agent_squad.doctor.git_output", side_effect=git),
            ):
                result = diagnose(f.repo)
            item = self.diagnostic(result, "disposable worktree")
            roots = list((f.repo / ".agent-squad/worktrees").glob("doctor-*"))
            self.assertEqual(len(roots), int(operation == "remove"))
            if roots:
                self.assertIn(str(roots[0]), item["detail"])
                self.assertIn(str(roots[0]), f.git("worktree", "list"))

    def test_trial_issue42_outdated_claude_integration(self) -> None:
        f = self.f
        f.env["FAKE_HERDR_CLAUDE_INTEGRATION"] = "outdated"
        result = f.cli("doctor", expected=1)
        self.diagnostic(result, "Herdr Reviewer discovery and integration")
        self.diagnostic(
            result, "Herdr Implementer discovery and integration", "pass")
        self.assertFalse(any(call[:2] == ["integration", "install"]
                         for call in f.herdr_model()["calls"]))

    def test_trial_issue42_protocol_mismatch_and_missing_schema_contracts(
            self) -> None:
        f = self.f
        f.env["FAKE_HERDR_PROTOCOL"] = "99"
        self.diagnostic(f.cli("doctor", expected=1),
                        "Herdr Reviewer discovery and integration")
        f.env.pop("FAKE_HERDR_PROTOCOL")
        for missing in (
            *HerdrClient._REQUIRED_METHODS,
                *HerdrClient._REQUIRED_RESULTS):
            with self.subTest(missing=missing):
                f.env["FAKE_HERDR_SCHEMA_MISSING"] = missing
                result = f.cli("doctor", expected=1)
                self.diagnostic(
                    result, "Herdr Reviewer discovery and integration")
                self.diagnostic(
                    result, "Herdr Implementer discovery and integration")

    def test_names_the_resolved_session_and_checks_it(self) -> None:
        f = self.f
        elsewhere = f.root / "elsewhere"
        elsewhere.mkdir()
        f.herdr_settings(implementer={"cwd": str(elsewhere)})
        f.herdr_session("ours", implementer={})
        for inherited in ("fixture", "ours", None):
            with self.subTest(inherited=inherited):
                f.inherit_herdr(inherited)
                result = f.cli("doctor", "--live-reviewer")
                self.assertEqual(
                    self.diagnostic(
                        result, "Herdr Implementer session", "pass",
                    )["detail"],
                    f"ours ({f.herdr_socket('ours')})",
                )
                self.diagnostic(result, "Implementer identity", "pass")
                model = f.herdr_model()
                self.assertEqual(
                    {tuple(c[:2]) for c in model["calls"]},
                    {("session", "list"), ("agent", "get")},
                )
                ours = model["sessions"]["ours"]
                self.assertEqual(ours["workspaces"], [])
                for command in (
                    ["api", "snapshot"], ["integration", "status"],
                    ["worktree", "open"], ["agent", "start"],
                ):
                    self.assertTrue(
                        any(c[:2] == command and "--help" not in c
                            for c in ours["calls"]),
                        command,
                    )
                model["calls"] = []
                ours["calls"] = []
                f.save_herdr(model)

    def test_no_session_warns_and_several_sessions_fail(self) -> None:
        f = self.f
        f.herdr_session("twin", implementer={})
        result = f.cli("doctor", "--live-reviewer", expected=1)
        for name in ("Herdr Implementer session", "live Reviewer"):
            self.assertIn(
                "several running Herdr sessions hold Implementer"
                " 'implementer' (kind 'codex') working in "
                f"{f.repo}: fixture, twin; sessions examined: fixture, twin",
                self.diagnostic(result, name)["detail"],
            )
        self.assertIsNone(result["live_reviewer"])
        self.assertEqual(result["exit_code"], 1)
        self.diagnostic(f.cli("doctor", expected=1),
                        "Herdr Implementer session")

        model = f.herdr_model()
        model["settings"]["implementer"] = False
        model["sessions"]["twin"]["settings"]["implementer"] = False
        model["calls"] = []
        f.save_herdr(model)
        f.inherit_herdr("fixture")
        result = f.cli("doctor")
        self.assertTrue(result["ok"])
        self.assertIn(
            "no running Herdr session holds Implementer 'implementer'",
            self.diagnostic(
                result, "Herdr Implementer session", "warn")["detail"],
        )
        self.diagnostic(result, "Implementer identity", "warn")
        # The remaining Herdr rows fall back to the inherited session.
        self.diagnostic(result, "Herdr socket and inventory", "pass")
        self.assertIn(["api", "snapshot"], f.herdr_model()["calls"])
        before = f.git("worktree", "list", "--porcelain")
        result = f.cli("doctor", "--live-reviewer", expected=1)
        self.assertIn(
            "no running Herdr session holds",
            self.diagnostic(result, "live Reviewer")["detail"],
        )
        self.assertIsNone(result["live_reviewer"])
        self.assertEqual(f.git("worktree", "list", "--porcelain"), before)
        model = f.herdr_model()
        for state in (model, model["sessions"]["twin"]):
            self.assertEqual(state["workspaces"], [])

    def test_unusable_session_listing_fails(self) -> None:
        self.f.herdr_settings(session_list_failure=True)
        result = self.f.cli("doctor", expected=1)
        self.assertIn(
            "herdr session list failed",
            self.diagnostic(result, "Herdr Implementer session")["detail"],
        )

    def test_socket_unreachable_from_current_process(self) -> None:
        self.f.herdr_settings(unreachable=True)
        result = self.f.cli("doctor", expected=1)
        self.diagnostic(result, "Herdr socket and inventory")
        self.assertIsNone(result["live_reviewer"])

    def test_trial_issue43_missing_implementer_warns_wrong_kind_fails(
            self) -> None:
        f = self.f
        f.env["FAKE_HERDR_MISSING_AGENT"] = "1"
        self.diagnostic(f.cli("doctor"), "Implementer identity", "warn")
        f.env.pop("FAKE_HERDR_MISSING_AGENT")
        f.herdr_settings(implementer_kind="claude")
        self.diagnostic(f.cli("doctor", expected=1), "Implementer identity")

    def test_both_installed_skills_must_match_and_are_never_repaired(
            self) -> None:
        f = self.f
        for name in ("squad-implementer", "squad-reviewer"):
            path = f.home / ".agents/skills" / name / "SKILL.md"
            original = path.read_bytes()
            for content in (None, b"user modification"):
                with self.subTest(name=name, content=content):
                    if content is None:
                        path.unlink()
                    else:
                        path.write_bytes(content)
                    result = f.cli("doctor", expected=1)
                    self.diagnostic(result, f"installed {name}")
                    self.assertEqual(path.read_bytes()
                                     if path.exists() else None, content)
            path.write_bytes(original)

    def test_claude_links_must_exist_and_target_the_installed_skills(
            self) -> None:
        f = self.f
        for name in ("squad-implementer", "squad-reviewer"):
            path = f.home / ".claude/skills" / name
            path.unlink()
            self.diagnostic(f.cli("doctor", expected=1),
                            f"Claude symlink {name}")
            path.symlink_to(f.root / "missing")
            self.diagnostic(f.cli("doctor", expected=1),
                            f"Claude symlink {name}")
            self.assertEqual(path.readlink(), f.root / "missing")
            path.unlink()
            path.symlink_to(f"../../.agents/skills/{name}")

    def test_installed_code_review_requires_its_frontmatter_name(self) -> None:
        path = self.f.home / ".agents/skills/code-review/SKILL.md"
        path.unlink()
        self.diagnostic(self.f.cli("doctor", expected=1),
                        "installed code-review")
        path.write_text("---\nname: different\n---\nname: code-review\n")
        self.diagnostic(self.f.cli("doctor", expected=1),
                        "installed code-review")

    def test_live_probe_is_not_started_after_a_deterministic_failure(
            self) -> None:
        self.f.settings(permission="read")
        self.f.cli("doctor", "--live-reviewer", expected=1)
        self.assertFalse(any(
            call[:2] == ["agent", "start"] and "--help" not in call
            for call in self.f.herdr_model()["calls"]
        ))

    def test_orphans_use_forge_state_and_report_without_removing(self) -> None:
        f = self.f
        head = f.candidate()
        f.create_pr()
        root = f.repo / ".agent-squad/worktrees"
        review = root / f"reviewer-pr1-{head[:7]}"
        f.git("worktree", "add", "--detach", str(review), head)
        scratch = f.repo / ".agent-squad/review-scratch/pr1"
        scratch.mkdir()
        (scratch / "evidence.txt").write_text("preserve")
        model = f.herdr_model()
        model["agents"] = [{"name": review.name, "cwd": str(review)}]
        f.save_herdr(model)
        self.diagnostic(f.cli("doctor"), "orphan resources", "pass")
        for merged in (False, True):
            model = f.read_model()
            model["prs"]["1"].update(state="closed", merged=merged)
            f.save_model(model)
            result = f.cli("doctor")
            warnings = [d for d in result["diagnostics"]
                        if d["check"].startswith("orphan")]
            self.assertEqual(len(warnings), 3 if merged else 2)
            self.assertTrue(all(d["severity"] == "warn" for d in warnings))
            details = " ".join(d["detail"] for d in warnings)
            self.assertIn(str(review), details)
            self.assertIn(review.name, details)
            self.assertEqual(review.exists(), True)
            self.assertEqual(
                (scratch / "evidence.txt").read_text(), "preserve")
        self.assertEqual(f.herdr_model()["agents"], [
                         {"name": review.name, "cwd": str(review)}])
        self.assertFalse(any(
            call[:2] in (["workspace", "close"], ["worktree", "remove"])
            and "--help" not in call
            for call in f.herdr_model()["calls"]
        ))

    def test_orphan_lookup_failure_names_resource_and_never_assumes_closed(
            self) -> None:
        f = self.f
        f.candidate()
        f.create_pr()
        scratch = f.repo / ".agent-squad/review-scratch/pr1"
        scratch.mkdir()
        from agent_squad.forge import ForgeError
        with (
            patch.dict(os.environ, f.env, clear=True),
            patch("agent_squad.github.GitHub.pr",
                  side_effect=ForgeError("API unavailable")),
        ):
            result = diagnose(f.repo)
        item = self.diagnostic(result, "orphan scratch directory")
        self.assertIn(str(scratch), item["detail"])
        self.assertTrue(scratch.exists())

    def test_issue_scratch_open_passes_closed_warns_and_preserves_files(
        self,
    ) -> None:
        f = self.f
        # PR #1 stays open while issue #1 changes state.
        f.candidate()
        f.create_pr()
        root = f.repo / ".agent-squad/review-scratch"
        (root / "pr1").mkdir()
        scratch = root / "issue-1"
        scratch.mkdir()
        evidence = scratch / "report.md"
        evidence.write_text("preserve")
        self.diagnostic(f.cli("doctor"), "orphan resources", "pass")
        model = f.read_model()
        model["issues"]["1"]["state"] = "closed"
        f.save_model(model)
        result = f.cli("doctor")
        item = self.diagnostic(
            result, "orphan issue scratch directory", "warn")
        self.assertIn("issue #1 is closed", item["detail"])
        self.assertIn(str(scratch), item["detail"])
        self.assertEqual(evidence.read_text(), "preserve")

    def test_unreadable_issue_scratch_fails_and_preserves_files(self) -> None:
        f = self.f
        scratch = f.repo / ".agent-squad/review-scratch/issue-999"
        scratch.mkdir()
        (scratch / "keep").write_text("preserve")
        result = f.cli("doctor", expected=1)
        item = self.diagnostic(result, "orphan issue scratch directory")
        self.assertIn("cannot determine issue #999 state", item["detail"])
        self.assertIn("issue not found", item["detail"])
        self.assertIn(str(scratch), item["detail"])
        self.assertEqual((scratch / "keep").read_text(), "preserve")

    def test_issue_scratch_rejects_mismatched_or_invalid_issue(self) -> None:
        f = self.f
        scratch = f.repo / ".agent-squad/review-scratch/issue-1"
        scratch.mkdir()
        for change in (
            {"number": 2}, {"pull_request": {}}, {"state": "unknown"},
        ):
            with self.subTest(change=change):
                model = f.read_model()
                original = dict(model["issues"]["1"])
                model["issues"]["1"].update(change)
                f.save_model(model)
                result = f.cli("doctor", expected=1)
                item = self.diagnostic(
                    result, "orphan issue scratch directory")
                self.assertIn(str(scratch), item["detail"])
                self.assertTrue(scratch.exists())
                model["issues"]["1"] = original
                f.save_model(model)

    def test_orphans_without_any_forge_identity_fail_without_a_traceback(
            self) -> None:
        f = self.f
        f.settings(token_failure=["developer", "reviewer"])
        scratch = f.repo / ".agent-squad/review-scratch/pr1"
        scratch.mkdir()
        evidence = scratch / "evidence.txt"
        evidence.write_text("preserve")
        issue_scratch = scratch.parent / "issue-1"
        issue_scratch.mkdir()
        result = f.cli("doctor", expected=1)
        for role in ("implementer", "reviewer"):
            self.diagnostic(result, f"{role} forge identity")
        item = self.diagnostic(result, "orphan scratch directory")
        self.assertIn("forge identity unavailable", item["detail"])
        self.assertIn(str(scratch), item["detail"])
        self.assertEqual(evidence.read_text(), "preserve")
        item = self.diagnostic(result, "orphan issue scratch directory")
        self.assertIn("forge identity unavailable", item["detail"])
        self.assertIn(str(issue_scratch), item["detail"])
        self.assertTrue(issue_scratch.exists())
        self.assertNotIn("fake-token-", json.dumps(result))

    def test_non_repository_agent_is_ignored_without_a_forge_lookup(
            self) -> None:
        f = self.f
        plain = f.root / "plain"
        plain.mkdir()
        model = f.herdr_model()
        agents = [{"name": "reviewer-pr999-aaaaaaa", "cwd": str(plain)}]
        model["agents"] = agents
        f.save_herdr(model)
        self.diagnostic(f.cli("doctor"), "orphan resources", "pass")
        self.assertEqual(f.herdr_model()["agents"], agents)
        self.assertTrue(plain.is_dir())
        self.assertFalse(any(
            "/pulls/999" in c["arguments"]
            for c in f.read_model()["calls"]
        ))

    def test_other_repository_agents_and_nonconforming_resources_are_ignored(
            self) -> None:
        f = self.f
        foreign = f.root / "foreign"
        f.git("clone", str(f.origin), str(foreign))
        model = f.herdr_model()
        model["agents"] = [{"name": "reviewer-pr999-aaaaaaa",
                            "cwd": str(foreign)}, {"name": "unrelated"}]
        f.save_herdr(model)
        (f.repo / ".agent-squad/review-scratch/notes").mkdir()
        for name in ("issue-0", "issue-01", "issue-52-old"):
            (f.repo / ".agent-squad/review-scratch" / name).mkdir()
        (f.repo / ".agent-squad/review-scratch/issue-999").write_text("file")
        self.diagnostic(f.cli("doctor"), "orphan resources", "pass")
        self.assertFalse(
            any("/pulls/999" in c["arguments"]
                for c in f.read_model()["calls"]))

    def test_malformed_orphan_agent_inventory_fails(self) -> None:
        f = self.f
        for snapshot in (
            {}, {"agents": None}, {"agents": [None]},
            {"agents": [{"name": "reviewer-pr1-aaaaaaa", "cwd": None}]},
        ):
            with (
                self.subTest(snapshot=snapshot),
                patch.dict(os.environ, f.env, clear=True),
                patch("agent_squad.doctor.HerdrClient.snapshot",
                      return_value=snapshot),
            ):
                result = diagnose(f.repo)
            self.assertFalse(result["ok"])
            self.assertTrue(any(
                d["check"].startswith("orphan") and d["severity"] == "fail"
                for d in result["diagnostics"]
            ))
            self.assertEqual(f.herdr_model()["workspaces"], [])

    def test_removed_worktree_does_not_hide_its_orphan_agent(self) -> None:
        f = self.f
        f.candidate()
        f.create_pr()
        name = "reviewer-pr1-aaaaaaa"
        path = f.repo / ".agent-squad/worktrees" / name
        model = f.read_model()
        model["prs"]["1"].update(state="closed", merged=False)
        f.save_model(model)
        herdr = f.herdr_model()
        herdr["agents"] = [{"name": name, "cwd": str(path)}]
        f.save_herdr(herdr)
        result = f.cli("doctor")
        item = self.diagnostic(result, "orphan agent", "warn")
        self.assertIn(name, item["detail"])
        self.assertFalse(path.exists())

    def test_circular_skill_links_are_failed_json_diagnostics(self) -> None:
        f = self.f
        for name in ("squad-implementer", "squad-reviewer"):
            link = f.home / ".claude/skills" / name
            link.unlink()
            link.symlink_to(name)
            result = f.cli("doctor", expected=1)
            self.diagnostic(result, f"Claude symlink {name}")
            self.assertEqual(link.readlink(), Path(name))

    def test_circular_root_is_a_failed_json_diagnostic(self) -> None:
        cycle = self.f.root / "cycle"
        cycle.symlink_to("cycle")
        self.configuration(scratch_root=str(cycle))
        result = self.f.cli("doctor", expected=1)
        self.diagnostic(result, "repository and configuration")
        self.assertEqual(cycle.readlink(), Path("cycle"))

    def test_circular_orphan_agent_cwd_is_a_failed_diagnostic(self) -> None:
        f = self.f
        cycle = f.root / "cycle"
        cycle.symlink_to("cycle")
        model = f.herdr_model()
        model["agents"] = [{
            "name": "reviewer-pr1-aaaaaaa", "cwd": str(cycle),
        }]
        f.save_herdr(model)
        self.diagnostic(f.cli("doctor", expected=1), "orphan resources")
        self.assertEqual(cycle.readlink(), Path("cycle"))


class IdentityDoctorTests(unittest.TestCase):
    def test_identity_checks_are_unconditional(self) -> None:
        with ForgeFixture() as f:
            f.initialize()
            result = f.cli('doctor')
            checks = {d['check']: d for d in result['diagnostics']}
            for name in ('Reviewer write permission',
                         'distinct forge identities'):
                self.assertEqual(checks[name]['severity'], 'pass', name)
            self.assertFalse([c for c in checks if 'approver' in c
                              or c.startswith('shared account')])
            f.settings(permission='read')
            result = f.cli('doctor', expected=1)
            checks = {d['check']: d for d in result['diagnostics']}
            self.assertEqual(
                checks['Reviewer write permission']['severity'], 'fail')
            f.settings(permission='write', token_failure=['reviewer'])
            result = f.cli('doctor', expected=1)
            checks = {d['check']: d for d in result['diagnostics']}
            self.assertEqual(
                (checks['distinct forge identities']['severity'],
                 checks['distinct forge identities']['detail']),
                ('fail', 'cannot verify both configured logins'))


class ForgejoDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.forge_support import ForgejoFixture

        self.f = ForgejoFixture()
        self.addCleanup(self.f.close)
        self.f.initialize()

    def test_dual_mode_checks_version_identity_tokens_access_and_write(
        self,
    ) -> None:
        result = self.f.cli("doctor")
        self.assertTrue(result["ok"])
        checks = {d["check"]: d for d in result["diagnostics"]}
        for name in (
            "forge client",
            "implementer forge identity",
            "reviewer forge identity",
            "implementer repository access",
            "reviewer repository access",
            "implementer token file",
            "reviewer token file",
            "Reviewer write permission",
        ):
            self.assertEqual(checks[name]["severity"], "pass", name)
        self.assertNotIn("GitHub CLI", checks)
        self.assertNotIn("fake-token-", json.dumps(result))
        self.assertTrue(
            all(c["method"] == "GET" for c in self.f.read_model()["calls"])
        )
        self.f.settings(permission="read")
        result = self.f.cli("doctor", expected=1)
        self.assertTrue(
            any(
                d["check"] == "Reviewer write permission"
                and d["severity"] == "fail"
                for d in result["diagnostics"]
            )
        )

    def test_invalid_token_and_old_server_have_specific_diagnostics(
        self,
    ) -> None:
        (self.f.root / "reviewer.token").chmod(0o640)
        self.f.settings(version="15.0.9")
        result = self.f.cli("doctor", expected=1)
        checks = {d["check"]: d for d in result["diagnostics"]}
        self.assertEqual(
            checks["repository and configuration"]["severity"], "fail"
        )
        self.assertIn("reviewer token_file", json.dumps(result))
        self.assertEqual(self.f.read_model()["calls"], [])
        (self.f.root / "reviewer.token").chmod(0o600)
        result = self.f.cli("doctor", expected=1)
        checks = {d["check"]: d for d in result["diagnostics"]}
        self.assertEqual(checks["forge client"]["severity"], "fail")
        self.assertIn("16.0.0", checks["forge client"]["detail"])
        self.assertNotIn("fake-token-", json.dumps(result))
