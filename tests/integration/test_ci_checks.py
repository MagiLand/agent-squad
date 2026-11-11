"""CI results read at a PR head through both fake forges (#145)."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._support import FakeClock, add_src_to_path
from tests.forge_support import ForgeFixture, ForgejoFixture

add_src_to_path()

from agent_squad.commands import pr_checks  # noqa: E402
from agent_squad.forge import make_forge  # noqa: E402
from agent_squad.initialization import (  # noqa: E402
    load_initialized_repository,
)

MERGE_COMMIT = "d" * 40


def check_run(
    name: str,
    conclusion: str | None = "success",
    *,
    status: str = "completed",
    suite: int = 1,
) -> dict:
    """One GitHub check run as the check-runs read serves it."""
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "check_suite": {"id": suite},
        "app": {"slug": "github-actions"},
        "html_url": f"https://github.example/runs/{name}",
        "started_at": "2026-11-09T10:00:00Z",
        "completed_at": (
            "2026-11-09T10:05:00Z" if status == "completed" else None
        ),
    }


def commit_status(context: str, state: str, creator: str = "ci-bot") -> dict:
    """One GitHub commit status."""
    return {
        "context": context,
        "state": state,
        "creator": {"login": creator},
        "target_url": f"https://ci.example/{context}",
    }


def forgejo_status(
    context: str, status: str, creator: str | None = None
) -> dict:
    """One Forgejo commit status; Forgejo Actions writes no creator."""
    return {
        "id": 1,
        "context": context,
        "status": status,
        "description": "",
        "target_url": f"/MagiLand/trial/actions/runs/1/jobs/{context}",
        "creator": None if creator is None else {"login": creator},
    }


def other_fields(state: dict) -> dict:
    return {key: value for key, value in state.items() if key != "ci"}


def graphql_check_read(body: object) -> bool:
    """Whether a GraphQL request reads checks (#149)."""
    if not isinstance(body, dict) or "query" not in body:
        return False
    query = body["query"]
    return (
        query.startswith("query SquadChecks")
        or (query.startswith("query SquadSnapshot")
            and body["variables"]["checks"])
        or (query.startswith("query SquadPage")
            and ("on Commit " in query or "on CheckSuite " in query))
    )


class CheckFixtureMixin:
    """Shared setup: one open PR whose current head carries checks."""

    fixture = ForgeFixture

    def setUp(self) -> None:
        self.f = self.fixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.head = self.f.candidate()
        self.f.create_pr()

    def seed(self, checks: object, sha: str | None = None) -> None:
        model = self.f.read_model()
        model.setdefault("checks", {})[sha or self.head] = checks
        self.f.save_model(model)

    def sequence(self, *states: object, sha: str | None = None) -> None:
        model = self.f.read_model()
        model.setdefault("check_sequence", {})[sha or self.head] = list(states)
        self.f.save_model(model)

    def close_pr(
        self, *, merged: bool = True, merge_commit: str | None = MERGE_COMMIT,
    ) -> None:
        """Record how the PR closed; no merge record, worktree kept."""
        model = self.f.read_model()
        model["prs"]["1"].update(
            state="closed", merged=merged, merge_commit_sha=merge_commit,
        )
        self.f.save_model(model)

    def ci(self) -> dict:
        return self.f.status()["ci"]

    def checks(
        self, *args: str, expected: int = 0, cwd: Path | None = None,
    ) -> dict:
        return self.f.cli("pr", "checks", "--pr", "1", *args,
                          expected=expected, cwd=cwd)

    def at_merge_commit(self, *args: str, expected: int = 0) -> dict:
        return self.checks("--merge-commit", *args, expected=expected,
                           cwd=self.f.repo)

    def wait_on_clock(self, limit: int) -> tuple[dict, list[float]]:
        """Run the merge-commit wait in process on a virtual clock."""
        clock = FakeClock()
        with patch.dict(os.environ, self.f.env, clear=True):
            repository = load_initialized_repository(self.f.repo)
            with patch("agent_squad.commands.time", clock):
                result = pr_checks(
                    make_forge(repository, "implementer"), 1, limit,
                    merge_commit=True,
                )
        return result, clock.sleeps

    def check_reads(self) -> list:
        raise NotImplementedError

    def pull_reads(self) -> list:
        raise NotImplementedError

    def test_head_without_checks_reports_none(self) -> None:
        ci = self.ci()
        self.assertEqual(ci, {
            "head": self.head, "summary": "none", "checks": [],
        })
        self.assertEqual(self.checks(), ci)

    def test_new_head_is_the_head_reported(self) -> None:
        self.seed(self.passing())
        self.assertEqual(self.ci()["summary"], "passed")
        head = self.f.push("value = 2\n")
        ci = self.ci()
        self.assertEqual((ci["head"], ci["summary"]), (head, "none"))
        self.assertEqual(self.checks()["head"], head)

    def test_refused_read_is_unavailable_and_changes_nothing_else(
        self,
    ) -> None:
        before = self.f.status()
        self.f.settings(checks_denied=["developer"])
        result = self.f.run([
            sys.executable, "-m", "agent_squad", "status", "--pr", "1",
            "--as", "implementer", "--json",
        ], cwd=self.f.worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads(result.stdout)
        self.assertEqual(after["ci"]["summary"], "unavailable")
        self.assertEqual(after["ci"]["checks"], [])
        self.assertIn("403", after["ci"]["reason"])
        self.assertEqual(other_fields(after), other_fields(before))
        self.assertEqual(
            self.checks("--as", "implementer")["summary"], "unavailable"
        )
        # The Reviewer's identity is still allowed to read.
        self.assertEqual(
            self.checks("--as", "reviewer")["summary"], "none"
        )

    def test_pr_checks_reads_with_the_selected_identity(self) -> None:
        for role, account in (("implementer", "developer"),
                              ("reviewer", "reviewer")):
            with self.subTest(role=role):
                start = len(self.check_reads())
                self.checks("--as", role)
                reads = self.check_reads()[start:]
                self.assertTrue(reads)
                self.assertEqual({self.account_of(c) for c in reads},
                                 {account})

    def test_wait_returns_once_pending_checks_finish(self) -> None:
        self.sequence(self.pending(), self.passing())
        result = self.checks("--wait", "5")
        self.assertEqual(result["summary"], "passed")
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["head_changed"])
        self.assertLessEqual(result["waited_seconds"], 5)

    def test_wait_times_out_with_the_last_summary_and_exits_zero(
        self,
    ) -> None:
        self.seed(self.pending())
        result = self.checks("--wait", "1")
        self.assertEqual(result["summary"], "pending")
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["waited_seconds"], 1)

    def test_wait_ends_at_once_on_an_unavailable_read(self) -> None:
        self.f.settings(checks_denied=["reviewer"])
        result = self.checks("--as", "reviewer", "--wait", "60")
        self.assertEqual(result["summary"], "unavailable")
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["waited_seconds"], 0)

    def test_status_reports_ci_without_changing_merge_authority(
        self,
    ) -> None:
        f = self.f
        f.review("approved")
        f.decision(merge_instruction="record", body="")
        baseline = f.status()
        self.assertEqual(baseline["next_action"], "merge")
        for name, checks in (
            ("failed", self.failing()), ("pending", self.pending()),
            ("none", self.empty()), ("unavailable", self.passing()),
        ):
            with self.subTest(summary=name):
                self.seed(checks)
                f.settings(checks_denied=(
                    ["developer"] if name == "unavailable" else []
                ))
                state = f.status()
                self.assertEqual(state["ci"]["summary"], name)
                for key in ("next_action", "reasons", "gates", "approval",
                            "merge_instruction", "merge_hold"):
                    self.assertEqual(state[key], baseline[key], key)
        f.settings(checks_denied=[])
        self.seed(self.failing())
        start = len(f.read_model()["calls"])
        result = f.run([
            sys.executable, "-m", "agent_squad", "pr", "merge",
            "--as", "implementer", "--pr", "1", "--json",
        ], cwd=f.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["merged"])
        # The merge reads no CI.
        self.assertEqual(self.check_reads()[start:], [])

    def test_merge_commit_checks_are_read_apart_from_the_head(self) -> None:
        f = self.f
        self.seed(self.passing())
        f.review("approved")
        f.decision(merge_instruction="record", body="")
        f.cli("pr", "merge", "--as", "implementer", "--pr", "1", cwd=f.repo)
        merge = f.cli("pr", "head", "--pr", "1", cwd=f.repo)["merge_commit"]
        self.assertNotEqual(merge, self.head)
        self.seed(self.failing(), merge)
        result = self.at_merge_commit()
        self.assertEqual((result["head"], result["summary"]),
                         (merge, "failed"))
        self.assertEqual([c["result"] for c in result["checks"]], ["failed"])
        plain = self.checks(cwd=f.repo)
        self.assertEqual((plain["head"], plain["summary"]),
                         (self.head, "passed"))
        self.assertEqual([c["result"] for c in plain["checks"]], ["passed"])
        self.assertEqual(f.cli("status", "--pr", "1", cwd=f.repo)["ci"], plain)

    def test_merge_commit_wait_moves_through_none_and_pending_to_final(
        self,
    ) -> None:
        self.close_pr()
        self.sequence(self.empty(), self.pending(), self.failing(),
                      sha=MERGE_COMMIT)
        before = len(self.pull_reads())
        result, sleeps = self.wait_on_clock(60)
        self.assertEqual((result["head"], result["summary"]),
                         (MERGE_COMMIT, "failed"))
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["head_changed"])
        self.assertEqual(result["waited_seconds"], 20)
        self.assertEqual(sleeps, [10, 10])
        self.assertEqual(len(self.pull_reads()) - before, 1)

    def test_merge_commit_wait_times_out_with_the_last_summary(self) -> None:
        self.close_pr()
        self.seed(self.pending(), MERGE_COMMIT)
        before = len(self.pull_reads())
        result, sleeps = self.wait_on_clock(15)
        self.assertEqual(result["summary"], "pending")
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["head_changed"])
        self.assertEqual(sleeps, [10, 5])
        self.assertEqual(len(self.pull_reads()) - before, 1)

    def test_merge_commit_wait_exits_zero_at_the_limit(self) -> None:
        self.close_pr()
        self.seed(self.pending(), MERGE_COMMIT)
        result = self.at_merge_commit("--wait", "1")
        self.assertEqual(result["summary"], "pending")
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["head_changed"])

    def test_merge_commit_refused_read_is_unavailable_and_exits_zero(
        self,
    ) -> None:
        self.close_pr()
        self.f.settings(checks_denied=["reviewer"])
        before = len(self.pull_reads())
        result = self.at_merge_commit("--as", "reviewer", "--wait", "60")
        self.assertEqual((result["head"], result["summary"]),
                         (MERGE_COMMIT, "unavailable"))
        self.assertIn("403", result["reason"])
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["head_changed"])
        self.assertEqual(result["waited_seconds"], 0)
        self.assertEqual(len(self.pull_reads()) - before, 1)

    def test_merge_commit_refuses_before_any_checks_read(self) -> None:
        for name, closed, message in (
            ("open", None, "not merged"),
            ("closed with a merge commit",
             {"merged": False, "merge_commit": MERGE_COMMIT}, "not merged"),
            ("merged without a merge commit",
             {"merged": True, "merge_commit": None}, "no merge commit"),
        ):
            with self.subTest(name):
                if closed is not None:
                    self.close_pr(**closed)
                start = len(self.check_reads())
                result = self.at_merge_commit(expected=4)
                self.assertIn(message, result["error"])
                self.assertEqual(self.check_reads()[start:], [])

    def test_merge_commit_reads_with_the_selected_identity(self) -> None:
        self.close_pr()
        self.seed(self.failing(), MERGE_COMMIT)
        for role, account in (("implementer", "developer"),
                              ("reviewer", "reviewer")):
            with self.subTest(role=role):
                start = len(self.check_reads())
                result = self.at_merge_commit("--as", role)
                self.assertEqual((result["head"], result["summary"]),
                                 (MERGE_COMMIT, "failed"))
                reads = self.check_reads()[start:]
                self.assertTrue(reads)
                self.assertEqual({self.account_of(c) for c in reads},
                                 {account})

    def test_plain_text_names_the_commit_it_read(self) -> None:
        self.close_pr()
        self.seed(self.failing(), MERGE_COMMIT)
        command = [sys.executable, "-m", "agent_squad", "pr", "checks",
                   "--pr", "1"]
        at_merge = self.f.run([*command, "--merge-commit"], cwd=self.f.repo)
        self.assertEqual(at_merge.returncode, 0, at_merge.stderr)
        self.assertEqual(at_merge.stdout.splitlines()[0],
                         f"CI failed: 1 checks at merge commit {MERGE_COMMIT}")
        at_head = self.f.run(command, cwd=self.f.repo)
        self.assertEqual(at_head.returncode, 0, at_head.stderr)
        self.assertEqual(at_head.stdout.splitlines()[0],
                         f"CI none: 0 checks at {self.head}")


class GitHubCheckTests(CheckFixtureMixin, unittest.TestCase):
    fixture = ForgeFixture

    @staticmethod
    def empty() -> dict:
        return {}

    @staticmethod
    def passing() -> dict:
        return {"check_runs": [check_run("test")]}

    @staticmethod
    def pending() -> dict:
        return {"check_runs": [check_run("test", None,
                                         status="in_progress")]}

    @staticmethod
    def failing() -> dict:
        return {"check_runs": [check_run("test", "failure")]}

    def check_reads(self) -> list:
        return [c for c in self.f.read_model()["calls"]
                if c["arguments"][:1] == ["api"]
                and ("/commits/" in c["arguments"][1]
                     or "/actions/runs" in c["arguments"][1]
                     or graphql_check_read(c["body"]))]

    def pull_reads(self) -> list:
        return [c for c in self.f.read_model()["calls"]
                if c["arguments"][:1] == ["api"]
                and c["arguments"][1].endswith("/pulls/1")]

    @staticmethod
    def account_of(call: dict) -> str:
        return call["account"]

    def test_merge_commit_of_an_unmerged_record_is_refused(self) -> None:
        # GitHub reports a merge commit for a PR closed without merging.
        self.close_pr(merged=False)
        with patch.dict(os.environ, self.f.env, clear=True):
            repository = load_initialized_repository(self.f.repo)
            pr = make_forge(repository, "implementer").pr(1)
        self.assertEqual((pr.merged, pr.merge_commit), (False, MERGE_COMMIT))
        start = len(self.check_reads())
        self.assertIn("not merged", self.at_merge_commit(expected=4)["error"])
        self.assertEqual(self.check_reads()[start:], [])

    def test_each_wait_poll_reads_the_checks_then_the_head(self) -> None:
        # One checks query, then the PR record; never a snapshot (#149).
        self.sequence(self.pending(), self.passing())
        start = len(self.f.read_model()["calls"])
        self.assertEqual(self.checks("--wait", "5")["summary"], "passed")
        reads = [
            c["body"]["query"].split("(", 1)[0]
            if c["arguments"][1] == "graphql" else c["arguments"][1]
            for c in self.f.read_model()["calls"][start:]
            if c["arguments"][:1] == ["api"]
        ]
        pull = reads[0]
        self.assertTrue(pull.endswith("/pulls/1"), pull)
        poll = ["query SquadChecks", pull]
        self.assertEqual(reads, [pull, *poll, *poll])

    def test_check_runs_and_statuses_are_both_reported(self) -> None:
        self.seed({
            "check_runs": [
                check_run("lint", suite=7),
                check_run("test", "skipped", suite=7),
            ],
            "statuses": [commit_status("ci/legacy", "success")],
            "workflow_runs": [{"check_suite_id": 7, "event": "pull_request"}],
        })
        ci = self.ci()
        self.assertEqual(ci["head"], self.head)
        self.assertEqual(ci["summary"], "passed")
        self.assertEqual(ci["checks"], [
            {
                "name": "ci/legacy", "event": None, "state": "success",
                "result": "passed", "source": "ci-bot",
                "url": "https://ci.example/ci/legacy",
                "started_at": None, "completed_at": None,
            },
            {
                "name": "lint", "event": "pull_request",
                "state": "completed/success", "result": "passed",
                "source": "github-actions",
                "url": "https://github.example/runs/lint",
                "started_at": "2026-11-09T10:00:00Z",
                "completed_at": "2026-11-09T10:05:00Z",
            },
            {
                "name": "test", "event": "pull_request",
                "state": "completed/skipped", "result": "skipped",
                "source": "github-actions",
                "url": "https://github.example/runs/test",
                "started_at": "2026-11-09T10:00:00Z",
                "completed_at": "2026-11-09T10:05:00Z",
            },
        ])

    def test_zero_statuses_with_pending_combined_state_are_not_pending(
        self,
    ) -> None:
        self.seed({"check_runs": [check_run("test")]})
        self.assertEqual(self.ci()["summary"], "passed")
        # The combined read the adapter ignored did say pending.
        combined = subprocess.run(
            ["gh", "api",
             f"/repos/MagiLand/trial/commits/{self.head}/status"],
            env={**self.f.env, "GH_TOKEN": "fake-token-reviewer"},
            text=True, capture_output=True, timeout=30, shell=False,
        )
        self.assertEqual(combined.returncode, 0, combined.stderr)
        body = json.loads(combined.stdout)
        self.assertEqual((body["state"], body["statuses"]), ("pending", []))

    def test_failed_then_passed_rerun_is_reported_once_as_passed(
        self,
    ) -> None:
        self.seed({
            "check_runs": [check_run("test", "failure"), check_run("test")],
            "statuses": [commit_status("ci/legacy", "failure"),
                         commit_status("ci/legacy", "success")],
        })
        ci = self.ci()
        self.assertEqual(ci["summary"], "passed")
        self.assertEqual(
            [(c["name"], c["result"]) for c in ci["checks"]],
            [("ci/legacy", "passed"), ("test", "passed")],
        )

    def test_push_and_pull_request_results_are_both_reported(self) -> None:
        self.seed({
            "check_runs": [check_run("test", suite=1),
                           check_run("test", "failure", suite=2),
                           check_run("nightly", suite=3)],
            "workflow_runs": [
                {"check_suite_id": 1, "event": "pull_request"},
                {"check_suite_id": 2, "event": "push"},
                {"check_suite_id": 3, "event": "schedule"},
            ],
        })
        ci = self.ci()
        self.assertEqual(ci["summary"], "failed")
        self.assertEqual(
            [(c["name"], c["event"], c["result"]) for c in ci["checks"]],
            [("nightly", "schedule", "passed"),
             ("test", "pull_request", "passed"),
             ("test", "push", "failed")],
        )

    def test_every_page_is_read(self) -> None:
        self.seed({
            "check_runs": [check_run(f"job-{n:03}") for n in range(100)]
            + [check_run("job-100", "failure")],
            "statuses": [commit_status(f"s-{n:03}", "success")
                         for n in range(100)]
            + [commit_status("s-100", "pending")],
        })
        ci = self.ci()
        self.assertEqual(len(ci["checks"]), 202)
        self.assertEqual(ci["summary"], "failed")


class ForgejoCheckTests(CheckFixtureMixin, unittest.TestCase):
    fixture = ForgejoFixture

    @staticmethod
    def empty() -> list:
        return []

    @staticmethod
    def passing() -> list:
        return [forgejo_status("Tests / unit (pull_request)", "success")]

    @staticmethod
    def pending() -> list:
        return [forgejo_status("Tests / unit (pull_request)", "pending")]

    @staticmethod
    def failing() -> list:
        return [forgejo_status("Tests / unit (pull_request)", "failure")]

    def check_reads(self) -> list:
        return [c for c in self.f.read_model()["calls"]
                if "/commits/" in c["path"]]

    def pull_reads(self) -> list:
        return [c for c in self.f.read_model()["calls"]
                if c["path"].split("?")[0].endswith("/pulls/1")]

    @staticmethod
    def account_of(call: dict) -> str:
        return call["Authorization"].removeprefix("token fake-token-")

    def test_merge_commit_rows_are_reported_as_the_forge_gives_them(
        self,
    ) -> None:
        # Forgejo names the event inside the check, so nothing filters by it.
        self.close_pr()
        self.seed([
            forgejo_status("Tests / unit (push)", "success"),
            forgejo_status("Deploy / deploy (push)", "failure"),
            forgejo_status("Tests / unit (schedule)", "pending"),
        ], MERGE_COMMIT)
        result = self.at_merge_commit()
        self.assertEqual(result["summary"], "failed")
        self.assertEqual(
            sorted(c["name"] for c in result["checks"]),
            ["Deploy / deploy (push)", "Tests / unit (push)",
             "Tests / unit (schedule)"],
        )
        self.assertEqual({c["event"] for c in result["checks"]}, {None})

    def test_statuses_are_reported_with_their_creator(self) -> None:
        self.seed([
            forgejo_status("Tests / unit (push)", "success"),
            forgejo_status("Tests / unit (pull_request)", "skipped"),
            forgejo_status("deploy/preview", "success", creator="ci-bot"),
        ])
        ci = self.ci()
        self.assertEqual(ci["summary"], "passed")
        self.assertEqual(ci["checks"], [
            {
                "name": name, "event": None, "state": state,
                "result": result, "source": source,
                "url": f"/MagiLand/trial/actions/runs/1/jobs/{name}",
                "started_at": None, "completed_at": None,
            }
            for name, state, result, source in (
                ("Tests / unit (pull_request)", "skipped", "skipped", None),
                ("Tests / unit (push)", "success", "passed", None),
                ("deploy/preview", "success", "passed", "ci-bot"),
            )
        ])

    def test_failed_then_passed_rerun_is_reported_once_as_passed(
        self,
    ) -> None:
        self.seed([
            forgejo_status("Tests / unit (push)", "failure"),
            forgejo_status("Tests / unit (push)", "success"),
        ])
        ci = self.ci()
        self.assertEqual(ci["summary"], "passed")
        self.assertEqual(len(ci["checks"]), 1)

    def test_failure_on_the_last_page_fails_despite_page_state(self) -> None:
        self.seed(
            [forgejo_status(f"Tests / job-{n:02} (push)", "success")
             for n in range(60)]
            + [forgejo_status("Tests / last (push)", "failure")]
        )
        start = len(self.check_reads())
        ci = self.ci()
        self.assertEqual(len(ci["checks"]), 61)
        self.assertEqual(ci["summary"], "failed")
        # Page 1 reports state "success" and total_count 50; neither ends
        # the read.
        self.assertEqual(
            [c["path"].rpartition("?")[2]
             for c in self.check_reads()[start:]],
            ["limit=50&page=1", "limit=50&page=2"],
        )

    def test_push_and_pull_request_results_are_both_reported(self) -> None:
        self.seed([
            forgejo_status("Tests / unit (pull_request)", "success"),
            forgejo_status("Tests / unit (push)", "failure"),
        ])
        ci = self.ci()
        self.assertEqual(ci["summary"], "failed")
        self.assertEqual(
            [(c["name"], c["event"], c["result"]) for c in ci["checks"]],
            [("Tests / unit (pull_request)", None, "passed"),
             ("Tests / unit (push)", None, "failed")],
        )

    def test_warning_error_and_unknown_states_fail(self) -> None:
        for state in ("warning", "error", "cancelled"):
            with self.subTest(state=state):
                self.seed([forgejo_status("Tests / unit (push)", state)])
                ci = self.ci()
                self.assertEqual(ci["summary"], "failed")
                self.assertEqual(ci["checks"][0]["state"], state)


if __name__ == "__main__":
    unittest.main()
