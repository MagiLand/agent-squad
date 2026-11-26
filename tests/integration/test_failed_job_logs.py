"""Failed Actions jobs' log excerpts through both fake forges (#176)."""

import os
import sys
import unittest
from unittest.mock import patch

from tests._support import FakeClock, add_src_to_path
from tests.forge_support import ForgeFixture, ForgejoFixture

add_src_to_path()

from agent_squad.commands import FAILED_LOG_LINES, pr_checks  # noqa: E402
from agent_squad.forge import make_forge  # noqa: E402
from agent_squad.initialization import (  # noqa: E402
    load_initialized_repository,
)

MERGE_COMMIT = "d" * 40
OTHER_COMMIT = "e" * 40
# What a failing GitHub job logs: the failing step ends with ##[error],
# then actions/checkout's post step follows.
GITHUB_LOG = (
    "﻿2026-11-26T01:26:00Z Current runner version\n"
    "2026-11-26T01:26:03Z ##[group]Run make test\n"
    "2026-11-26T01:26:04Z \x1b[31mFAIL\x1b[0m: test_value\tbell \x07\r\n"
    "2026-11-26T01:26:05Z ##[error]Process completed with exit code 1.\n"
    "2026-11-26T01:26:05Z Post job cleanup.\n"
    "2026-11-26T01:26:06Z Cleaning up orphan processes\n"
)
FORGEJO_LOG = (
    "2026-11-26T02:19:50Z Runner received task 4 of job unit\n"
    "2026-11-26T02:19:50Z \x1b[31mred-text\x1b[0m and a bell \x07 end\n"
    "2026-11-26T02:19:50Z Job 'unit' failed\n"
)


def github_run(
    name: str, conclusion: str = "failure", *, job: int = 12,
    app: str = "github-actions", repository: str = "MagiLand/trial",
) -> dict:
    """One GitHub check run whose URL names Actions job ``job``.

    Each app has its own check suite, as on GitHub.
    """
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "check_suite": {"id": 1 if app == "github-actions" else 2},
        "app": {"slug": app},
        "html_url": (
            f"https://github.com/{repository}/actions/runs/5/job/{job}"
        ),
        "started_at": "2026-11-09T10:00:00Z",
        "completed_at": "2026-11-09T10:05:00Z",
    }


def forgejo_status(
    job: str, status: str, position: int, *, run: int = 3,
    path: str = "/MagiLand/trial", creator: str | None = None,
) -> dict:
    """One status Forgejo Actions writes for job ``job`` of run ``run``."""
    return {
        "id": 1,
        "context": f"Tests / {job} (pull_request)",
        "status": status,
        "description": "",
        "target_url": f"{path}/actions/runs/{run}/jobs/{position}",
        "creator": None if creator is None else {"login": creator},
    }


class LogFixtureMixin:
    fixture = ForgeFixture

    def setUp(self) -> None:
        self.f = self.fixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()

    def store(self, *, checks: object, actions: dict,
              sha: str | None = None) -> None:
        model = self.f.read_model()
        model.setdefault("checks", {})[sha or self.head] = checks
        model["actions"] = actions
        self.f.save_model(model)

    def failed_logs(self, *args: str) -> dict:
        return self.f.cli("pr", "checks", "--pr", "1", "--failed-logs", *args)

    def by_name(self, result: dict) -> dict:
        return {check["name"]: check for check in result["checks"]}


class GitHubFailedLogTests(LogFixtureMixin, unittest.TestCase):
    def seed(self, *runs: dict, job: dict | None = None,
             log: str = GITHUB_LOG, sha: str | None = None) -> None:
        self.store(
            checks={"check_runs": list(runs)},
            actions={
                "jobs": {"12": job or {
                    "id": 12, "run_id": 5, "head_sha": sha or self.head,
                    "name": "test",
                }},
                "logs": {"12": log, "11": "passing job's log\n"},
            },
            sha=sha,
        )

    def job_reads(self) -> list:
        return [c for c in self.f.read_model()["calls"]
                if c["arguments"][:1] == ["api"]
                and "/actions/jobs/" in c["arguments"][1]]

    def test_failed_job_excerpt_ends_at_the_error_line(self) -> None:
        self.seed(github_run("lint", "success", job=11), github_run("test"))
        checks = self.by_name(self.failed_logs())
        self.assertNotIn("log", checks["lint"])
        self.assertEqual(checks["test"]["log_reason"], None)
        self.assertEqual(checks["test"]["log"], {
            "job_id": 12,
            "lines": [
                "﻿2026-11-26T01:26:00Z Current runner version",
                "2026-11-26T01:26:03Z ##[group]Run make test",
                "2026-11-26T01:26:04Z \\x1b[31mFAIL\\x1b[0m: test_value"
                "\tbell \\x07",
                "2026-11-26T01:26:05Z ##[error]Process completed with"
                " exit code 1.",
            ],
            "total_lines": 6,
            "truncated": True,
        })
        # Only the failed job is read, with the gh flag that lets gh print
        # escape sequences.
        reads = self.job_reads()
        self.assertEqual([c["arguments"][1] for c in reads], [
            "/repos/MagiLand/trial/actions/jobs/12",
            "/repos/MagiLand/trial/actions/jobs/12/logs",
        ])
        log_read = reads[1]["arguments"]
        self.assertEqual(log_read[log_read.index("--method") + 1], "GET")
        self.assertIn("--allow-escape-sequences", log_read)
        self.assertNotIn("--allow-escape-sequences", reads[0]["arguments"])

    def test_reads_run_as_the_selected_role(self) -> None:
        self.seed(github_run("test"))
        for role, account in (("implementer", "developer"),
                              ("reviewer", "reviewer")):
            with self.subTest(role=role):
                start = len(self.job_reads())
                result = self.failed_logs("--as", role)
                self.assertEqual(result["checks"][0]["log"]["job_id"], 12)
                reads = self.job_reads()[start:]
                self.assertEqual(len(reads), 2)
                self.assertEqual(
                    {c["GH_TOKEN"] for c in reads}, {"fake-token-" + account}
                )

    def test_long_log_without_error_line_keeps_its_last_lines(self) -> None:
        log = "".join(f"line {n}\n" for n in range(1, 251))
        self.seed(github_run("test"), log=log)
        entry = self.failed_logs()["checks"][0]["log"]
        self.assertEqual(len(entry["lines"]), FAILED_LOG_LINES)
        self.assertEqual(entry["lines"][0], "line 151")
        self.assertEqual(entry["lines"][-1], "line 250")
        self.assertEqual(entry["total_lines"], 250)
        self.assertTrue(entry["truncated"])

    def test_short_log_without_error_line_is_whole(self) -> None:
        self.seed(github_run("test"), log="one\ntwo")
        entry = self.failed_logs()["checks"][0]["log"]
        self.assertEqual(entry["lines"], ["one", "two"])
        self.assertEqual((entry["total_lines"], entry["truncated"]),
                         (2, False))

    def test_checks_with_no_actions_job_have_reasons(self) -> None:
        cases = {
            "other app": {"check_runs": [github_run("test", app="codecov")]},
            "commit status": {"statuses": [{
                "context": "test", "state": "failure",
                "creator": {"login": "ci-bot"},
                "target_url": "https://github.com/MagiLand/trial/actions"
                              "/runs/5/job/12",
            }]},
            "other repository": {"check_runs": [
                github_run("test", repository="MagiLand/other")
            ]},
        }
        for case, checks in cases.items():
            with self.subTest(case=case):
                self.store(checks=checks, actions={
                    "jobs": {"12": {"id": 12, "run_id": 5,
                                    "head_sha": self.head}},
                    "logs": {"12": GITHUB_LOG},
                })
                start = len(self.job_reads())
                check = self.failed_logs()["checks"][0]
                self.assertIsNone(check["log"])
                self.assertTrue(check["log_reason"])
                self.assertEqual(self.job_reads()[start:], [])

    def test_job_of_another_run_or_commit_is_not_read(self) -> None:
        cases = {
            "another run": {"id": 12, "run_id": 6, "head_sha": self.head},
            "another commit": {"id": 12, "run_id": 5,
                               "head_sha": OTHER_COMMIT},
        }
        for case, job in cases.items():
            with self.subTest(case=case):
                self.seed(github_run("test"), job=job)
                start = len(self.job_reads())
                check = self.failed_logs()["checks"][0]
                self.assertIsNone(check["log"])
                self.assertIn("job 12", check["log_reason"])
                self.assertNotIn(
                    "/actions/jobs/12/logs",
                    [c["arguments"][1] for c in self.job_reads()[start:]],
                )

    def test_failed_log_read_gives_a_reason_and_exits_zero(self) -> None:
        self.seed(github_run("test"))
        self.f.settings(log_status={"12": 410})
        check = self.failed_logs()["checks"][0]
        self.assertIsNone(check["log"])
        self.assertIn("HTTP 410", check["log_reason"])

    def test_refused_checks_read_reads_no_log(self) -> None:
        self.seed(github_run("test"))
        self.f.settings(checks_denied=["developer"])
        result = self.failed_logs("--as", "implementer")
        self.assertEqual((result["summary"], result["checks"]),
                         ("unavailable", []))
        self.assertEqual(self.job_reads(), [])

    def test_without_the_flag_nothing_changes(self) -> None:
        self.seed(github_run("test"))
        plain = self.f.cli("pr", "checks", "--pr", "1")
        self.assertNotIn("log", plain["checks"][0])
        self.assertNotIn("log", self.f.status()["ci"]["checks"][0])
        self.assertEqual(self.job_reads(), [])

    def test_merge_commit_logs_are_read_at_the_merge_commit(self) -> None:
        self.seed(github_run("test"), sha=MERGE_COMMIT)
        model = self.f.read_model()
        model["prs"]["1"].update(
            state="closed", merged=True, merge_commit_sha=MERGE_COMMIT,
        )
        self.f.save_model(model)
        result = self.f.cli("pr", "checks", "--pr", "1", "--merge-commit",
                            "--failed-logs", cwd=self.f.repo)
        self.assertEqual(result["head"], MERGE_COMMIT)
        self.assertEqual(result["checks"][0]["log"]["job_id"], 12)

    def test_plain_output_prints_each_excerpt_or_reason(self) -> None:
        self.store(checks={"check_runs": [
            github_run("test"), github_run("cov", app="codecov", job=13),
        ]}, actions={
            "jobs": {"12": {"id": 12, "run_id": 5, "head_sha": self.head}},
            "logs": {"12": GITHUB_LOG},
        })
        result = self.f.run(
            [sys.executable, "-m", "agent_squad", "pr", "checks", "--pr", "1",
             "--failed-logs"], cwd=self.f.worktree,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("Log of test (job 12), 4 of 6 lines:", lines)
        self.assertIn(
            "    2026-11-26T01:26:05Z ##[error]Process completed with"
            " exit code 1.", lines,
        )
        self.assertIn(
            "No log for cov: not a GitHub Actions job (source codecov)",
            lines,
        )
        self.assertNotIn("\x1b", result.stdout)

    def test_wait_reads_logs_once_after_the_wait(self) -> None:
        pending = github_run("test")
        pending.update(status="in_progress", conclusion=None)
        self.seed(github_run("test"))
        model = self.f.read_model()
        model["check_sequence"] = {self.head: [
            {"check_runs": [pending]}, {"check_runs": [pending]},
            {"check_runs": [github_run("test")]},
        ]}
        self.f.save_model(model)
        clock = FakeClock()
        with patch.dict(os.environ, self.f.env, clear=True):
            repository = load_initialized_repository(self.f.repo)
            with patch("agent_squad.commands.time", clock):
                result = pr_checks(
                    make_forge(repository, "implementer"), 1, 60,
                    failed_logs=True,
                )
        self.assertEqual(result["summary"], "failed")
        self.assertEqual(result["checks"][0]["log"]["job_id"], 12)
        self.assertEqual(len(clock.sleeps), 2)
        self.assertEqual(
            [c["arguments"][1] for c in self.job_reads()],
            ["/repos/MagiLand/trial/actions/jobs/12",
             "/repos/MagiLand/trial/actions/jobs/12/logs"],
        )


class ForgejoFailedLogTests(LogFixtureMixin, unittest.TestCase):
    fixture = ForgejoFixture

    def actions(self, *, html_url: bool = False,
                sha: str | None = None) -> dict:
        """Run 3 has API ID 7, as run numbers and IDs differ (#176)."""
        jobs = [
            {"id": 20, "run_id": 7, "name": "lint", "attempt": 1},
            {"id": 21, "run_id": 7, "name": "unit", "attempt": 2},
        ]
        if html_url:
            for position, job in enumerate(jobs):
                job["html_url"] = (
                    f"{self.f.server.base_url}/MagiLand/trial/actions/runs"
                    f"/3/jobs/{position}/attempt/{job['attempt']}"
                )
        return {
            "runs": [
                {"id": 3, "index_in_repo": 1, "commit_sha": OTHER_COMMIT},
                {"id": 7, "index_in_repo": 3, "commit_sha": sha or self.head},
            ],
            "jobs": {"7": jobs},
            "logs": {"20": "lint log\n", "21": FORGEJO_LOG},
        }

    def seed(self, *statuses: dict, actions: dict | None = None) -> None:
        self.store(checks=list(statuses), actions=actions or self.actions())

    def statuses(self) -> list:
        return [forgejo_status("lint", "success", 0),
                forgejo_status("unit", "failure", 1)]

    def actions_reads(self) -> list:
        return [c for c in self.f.read_model()["calls"]
                if "/actions/" in c["path"]]

    def log_reads(self) -> list:
        return [c["path"] for c in self.actions_reads()
                if c["path"].endswith("/logs")]

    def test_job_shapes_of_both_versions_give_the_failed_excerpt(
        self,
    ) -> None:
        for version, html_url in (("16.x", False), ("17.x", True)):
            with self.subTest(version=version):
                self.seed(*self.statuses(),
                          actions=self.actions(html_url=html_url))
                start = len(self.actions_reads())
                checks = self.by_name(self.failed_logs())
                self.assertNotIn("log", checks["Tests / lint (pull_request)"])
                unit = checks["Tests / unit (pull_request)"]
                self.assertIsNone(unit["log_reason"])
                self.assertEqual(unit["log"], {
                    "job_id": 21,
                    "lines": [
                        "2026-11-26T02:19:50Z Runner received task 4 of"
                        " job unit",
                        "2026-11-26T02:19:50Z \\x1b[31mred-text\\x1b[0m"
                        " and a bell \\x07 end",
                        "2026-11-26T02:19:50Z Job 'unit' failed",
                    ],
                    "total_lines": 3,
                    "truncated": False,
                })
                paths = [c["path"].split("?")[0]
                         for c in self.actions_reads()[start:]]
                self.assertEqual(paths, [
                    "/api/v1/repos/MagiLand/trial/actions/runs",
                    "/api/v1/repos/MagiLand/trial/actions/runs",
                    "/api/v1/repos/MagiLand/trial/actions/runs/7/jobs",
                    "/api/v1/repos/MagiLand/trial/actions/jobs/21/logs",
                ])
                self.assertIn("run_number=3",
                              self.actions_reads()[start]["path"])

    def test_mismatches_never_return_another_jobs_log(self) -> None:
        def changed(edit) -> dict:
            actions = self.actions(html_url=True)
            edit(actions)
            return actions

        def rename(actions: dict) -> None:
            actions["jobs"]["7"][1]["name"] = "integration"

        def other_position(actions: dict) -> None:
            actions["jobs"]["7"][1]["html_url"] = (
                actions["jobs"]["7"][0]["html_url"]
            )

        def other_commit(actions: dict) -> None:
            actions["runs"][1]["commit_sha"] = OTHER_COMMIT

        def no_run(actions: dict) -> None:
            actions["runs"].pop()

        def two_runs(actions: dict) -> None:
            actions["runs"].append(dict(actions["runs"][1], id=8))

        def other_run_id(actions: dict) -> None:
            actions["jobs"]["7"][1]["run_id"] = 3

        cases = {
            "position past the last job": (
                forgejo_status("unit", "failure", 2), self.actions()),
            "html_url names another position": (
                forgejo_status("unit", "failure", 1),
                changed(other_position)),
            "job name differs": (
                forgejo_status("unit", "failure", 1), changed(rename)),
            "run at another commit": (
                forgejo_status("unit", "failure", 1), changed(other_commit)),
            "no run for the number": (
                forgejo_status("unit", "failure", 1), changed(no_run)),
            "two runs for the number": (
                forgejo_status("unit", "failure", 1), changed(two_runs)),
            "job of another run": (
                forgejo_status("unit", "failure", 1), changed(other_run_id)),
            "another repository": (
                forgejo_status("unit", "failure", 1, path="/MagiLand/other"),
                self.actions()),
            "not written by Actions": (
                forgejo_status("unit", "failure", 1, creator="ci-bot"),
                self.actions()),
        }
        for case, (status, actions) in cases.items():
            with self.subTest(case=case):
                self.seed(status, actions=actions)
                start = len(self.log_reads())
                check = self.failed_logs()["checks"][0]
                self.assertIsNone(check["log"])
                self.assertTrue(check["log_reason"])
                self.assertEqual(self.log_reads()[start:], [])

    def test_reads_run_as_the_selected_role(self) -> None:
        self.seed(*self.statuses())
        for role, account in (("implementer", "developer"),
                              ("reviewer", "reviewer")):
            with self.subTest(role=role):
                start = len(self.actions_reads())
                result = self.failed_logs("--as", role)
                self.assertEqual(
                    self.by_name(result)["Tests / unit (pull_request)"]
                    ["log"]["job_id"], 21,
                )
                self.assertEqual(
                    {c["Authorization"] for c in
                     self.actions_reads()[start:]},
                    {"token fake-token-" + account},
                )

    def test_refused_or_failing_reads_give_reasons(self) -> None:
        for setting, value, text in (
            ("log_status", {"21": 403}, "HTTP 403"),
            ("log_status", {"21": 500}, "HTTP 500"),
            ("actions_denied", ["developer"], "HTTP 404"),
            ("log_content_type", "application/zip", "application/zip"),
        ):
            with self.subTest(setting=setting, value=value):
                self.seed(*self.statuses())
                self.f.settings(**{setting: value})
                check = self.by_name(self.failed_logs("--as", "implementer"))[
                    "Tests / unit (pull_request)"]
                self.assertIsNone(check["log"])
                self.assertIn(text, check["log_reason"])
                model = self.f.read_model()
                del model["settings"][setting]
                self.f.save_model(model)

    def test_bytes_that_are_not_utf8_are_replaced(self) -> None:
        actions = self.actions()
        actions["log_bytes"] = {"21": b"ok \xff\xfe end\n".hex()}
        self.seed(*self.statuses(), actions=actions)
        check = self.by_name(self.failed_logs())[
            "Tests / unit (pull_request)"]
        self.assertEqual(check["log"]["lines"], ["ok �� end"])

    def test_refused_checks_read_reads_no_log(self) -> None:
        self.seed(*self.statuses())
        self.f.settings(checks_denied=["developer"])
        result = self.failed_logs("--as", "implementer")
        self.assertEqual(result["summary"], "unavailable")
        self.assertEqual(self.actions_reads(), [])

    def test_without_the_flag_nothing_is_read(self) -> None:
        self.seed(*self.statuses())
        self.f.cli("pr", "checks", "--pr", "1")
        self.f.status()
        self.assertEqual(self.actions_reads(), [])


class ForgejoSubPathFailedLogTests(LogFixtureMixin, unittest.TestCase):
    """An instance under a sub-path writes it into ``target_url``."""

    def fixture(self) -> ForgejoFixture:
        return ForgejoFixture(path_prefix="/forge")

    def test_sub_path_target_url_finds_the_job(self) -> None:
        self.store(
            checks=[forgejo_status("unit", "failure", 0,
                                   path="/forge/MagiLand/trial")],
            actions={
                "runs": [{"id": 7, "index_in_repo": 3,
                          "commit_sha": self.head}],
                "jobs": {"7": [{"id": 21, "run_id": 7, "name": "unit"}]},
                "logs": {"21": FORGEJO_LOG},
            },
        )
        check = self.failed_logs()["checks"][0]
        self.assertEqual(check["log"]["job_id"], 21)

    def test_target_url_without_the_sub_path_is_refused(self) -> None:
        self.store(
            checks=[forgejo_status("unit", "failure", 0)],
            actions={"runs": [], "jobs": {}, "logs": {}},
        )
        check = self.failed_logs()["checks"][0]
        self.assertIsNone(check["log"])
        self.assertIn("not an Actions job", check["log_reason"])


if __name__ == "__main__":
    unittest.main()
