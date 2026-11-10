"""CI result classification, summary, and the checks wait (#145)."""

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from tests._support import FakeClock, add_src_to_path
from tests.unit.test_conventions import H, config

add_src_to_path()

from agent_squad.commands import (  # noqa: E402
    CHECK_POLL_INTERVAL, ci_record, pr_checks, summarize_checks,
)
from agent_squad.forge import (  # noqa: E402
    Check, ChecksUnavailable, ForgeError,
)
from agent_squad.forgejo import Forgejo, parse_status  # noqa: E402
from agent_squad.github import (  # noqa: E402
    GitHub, check_run_result, parse_check_run, parse_commit_status,
)
from agent_squad.initialization import Repository  # noqa: E402
from tests.unit.test_forgejo import forgejo_config  # noqa: E402

OTHER = "c" * 40


def row(result: str, name: str = "test") -> Check:
    return Check(name, None, result, result, None, None, None, None)


class GitHubClassificationTests(unittest.TestCase):
    def test_check_runs_classify_every_status_and_conclusion(self) -> None:
        cells = [
            *((status, None, "pending") for status in (
                "queued", "in_progress", "waiting", "requested", "pending",
                "unnamed_status")),
            ("in_progress", "success", "pending"),
            ("completed", "success", "passed"),
            ("completed", "neutral", "passed"),
            ("completed", "skipped", "skipped"),
            *(("completed", conclusion, "failed") for conclusion in (
                "failure", "cancelled", "timed_out", "action_required",
                "stale", "startup_failure", "unnamed_conclusion", None)),
        ]
        for status, conclusion, expected in cells:
            with self.subTest(status=status, conclusion=conclusion):
                self.assertEqual(
                    check_run_result(status, conclusion), expected
                )

    def test_commit_statuses_classify_every_state(self) -> None:
        for state, expected in (
            ("success", "passed"), ("pending", "pending"),
            ("failure", "failed"), ("error", "failed"),
            ("unnamed", "failed"),
        ):
            with self.subTest(state=state):
                check = parse_commit_status(
                    {"context": "ci", "state": state, "creator": None}
                )
                self.assertEqual(check.result, expected)
                self.assertEqual(check.state, state)

    def test_check_run_fields_come_from_the_run_and_its_workflow_run(
        self,
    ) -> None:
        run = {
            "name": "Tests / unit (push)", "status": "completed",
            "conclusion": "failure", "check_suite": {"id": 9},
            "app": {"slug": "github-actions"}, "html_url": "",
            "started_at": "2026-11-09T10:00:00Z", "completed_at": None,
        }
        check = parse_check_run(run, {9: "pull_request"})
        # The event comes from the owning workflow run, never the name.
        self.assertEqual(check, Check(
            "Tests / unit (push)", "pull_request", "completed/failure",
            "failed", "github-actions", None, "2026-11-09T10:00:00Z", None,
        ))
        external = parse_check_run(
            {**run, "app": None, "check_suite": None}, {9: "push"}
        )
        self.assertEqual((external.event, external.source), (None, None))
        self.assertEqual(
            parse_check_run({**run, "status": "queued", "conclusion": None},
                            {}).state,
            "queued",
        )
        with self.assertRaises(ForgeError):
            parse_check_run({**run, "started_at": "yesterday"}, {})


class ForgejoClassificationTests(unittest.TestCase):
    def test_statuses_classify_every_state(self) -> None:
        for state, expected in (
            ("success", "passed"), ("skipped", "skipped"),
            ("pending", "pending"), ("failure", "failed"),
            ("error", "failed"), ("warning", "failed"),
            ("cancelled", "failed"), ("anything", "failed"),
        ):
            with self.subTest(state=state):
                check = parse_status({
                    "context": "Tests / unit (push)", "status": state,
                    "creator": None, "target_url": "",
                })
                self.assertEqual(check.result, expected)
                self.assertEqual(
                    (check.event, check.source, check.url),
                    (None, None, None),
                )


class SummaryTests(unittest.TestCase):
    def test_summary_precedence(self) -> None:
        for results, expected in (
            ((), "none"),
            (("passed", "pending", "failed"), "failed"),
            (("failed", "pending"), "failed"),
            (("passed", "pending"), "pending"),
            (("passed", "skipped"), "passed"),
            (("skipped",), "passed"),
        ):
            with self.subTest(results=results):
                self.assertEqual(
                    summarize_checks(tuple(map(row, results))), expected
                )


class RefusalTests(unittest.TestCase):
    """Only a refusal for the acting identity becomes ``unavailable``."""

    def adapters(self) -> list:
        path = Path("/repo")
        github = Repository(path, path, path / ".git", config())
        forgejo = Repository(path, path, path / ".git", forgejo_config())
        with patch("agent_squad.github.shutil.which", return_value="/gh"):
            return [GitHub(github, "reviewer"),
                    Forgejo(forgejo, "reviewer")]

    @staticmethod
    def graphql_refused(forge: object):
        """GitHub's GraphQL read reports a refusal without a status (#149)."""
        if not isinstance(forge, GitHub):
            return nullcontext()
        return patch.object(forge, "graphql", side_effect=ForgeError(
            "gh: Resource not accessible by integration"
        ))

    def test_refusals_are_unavailable_and_other_errors_raise(self) -> None:
        for forge in self.adapters():
            for status in (401, 403, 404, 422, 500, None):
                error = ForgeError("refused", status)
                with self.subTest(forge=type(forge).__name__, status=status), \
                        self.graphql_refused(forge), \
                        patch.object(forge, "api", side_effect=error):
                    if status in (401, 403, 404):
                        record = ci_record(forge, H)
                        self.assertEqual(record["summary"], "unavailable")
                        self.assertEqual(record["checks"], [])
                        self.assertIn("refused", record["reason"])
                    else:
                        with self.assertRaises(ForgeError) as raised:
                            ci_record(forge, H)
                        self.assertNotIsInstance(
                            raised.exception, ChecksUnavailable
                        )

    def test_github_reads_every_part_before_reporting(self) -> None:
        forge = self.adapters()[0]
        pages = {
            "check-runs": {"check_runs": []},
            "/status": {"state": "pending", "statuses": []},
            "actions/runs": {"workflow_runs": []},
        }
        seen = []

        def api(endpoint: str) -> dict:
            seen.append(endpoint)
            return next(v for k, v in pages.items() if k in endpoint)

        with self.graphql_refused(forge), \
                patch.object(forge, "api", side_effect=api):
            self.assertEqual(ci_record(forge, H)["summary"], "none")
        self.assertEqual(seen, [
            f"/repos/org/repo/commits/{H}/check-runs"
            "?filter=latest&per_page=100&page=1",
            f"/repos/org/repo/commits/{H}/status?per_page=100&page=1",
            f"/repos/org/repo/actions/runs?head_sha={H}&per_page=100&page=1",
        ])


def graphql_run(ident: int, name: str) -> dict:
    return {
        "databaseId": ident, "name": name, "status": "COMPLETED",
        "conclusion": "SUCCESS", "startedAt": "2026-11-09T10:00:00Z",
        "permalink": f"https://github.com/org/repo/actions/runs/{ident}",
        "completedAt": "2026-11-09T10:05:00Z",
    }


def graphql_commit(suites: list, contexts: list | None = None) -> dict:
    def page(nodes: list) -> dict:
        return {"nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None}}

    return {
        "id": "C-1", "oid": H,
        "checkSuites": page([
            {"id": f"CS-{n}", "app": {"slug": "github-actions"},
             "workflowRun": None if event is None else {"event": event},
             "checkRuns": page(runs)}
            for n, (event, runs) in enumerate(suites)
        ]),
        "status": None if contexts is None else {"contexts": contexts},
    }


class GraphQLCheckTests(unittest.TestCase):
    """GitHub reads the checks at one commit in one GraphQL query (#149)."""

    def setUp(self) -> None:
        path = Path("/repo")
        with patch("agent_squad.github.shutil.which", return_value="/gh"):
            self.forge = GitHub(
                Repository(path, path, path / ".git", config()), "reviewer"
            )

    def read(self, commit: dict) -> tuple[Check, ...]:
        response = {"repository": {"object": commit}}
        with patch.object(self.forge, "graphql", return_value=response), \
                patch.object(self.forge, "api",
                             side_effect=AssertionError("REST read")):
            return self.forge.checks(H)

    def test_runs_and_statuses_read_as_rest_reads_them(self) -> None:
        checks = self.read(graphql_commit(
            [("pull_request",
              [graphql_run(5, "test"), graphql_run(9, "test")]),
             (None, [graphql_run(7, "lint")])],
            [{"context": "deploy", "state": "FAILURE",
              "targetUrl": "https://ci.example/deploy",
              "creator": {"__typename": "Bot", "login": "ci"}},
             {"context": "docs", "state": "SUCCESS", "targetUrl": None,
              "creator": None}],
        ))
        self.assertEqual(
            [(c.name, c.event, c.state, c.result, c.source, c.url)
             for c in checks],
            [("deploy", None, "failure", "failed", "ci[bot]",
              "https://ci.example/deploy"),
             ("docs", None, "success", "passed", None, None),
             ("lint", None, "completed/success", "passed", "github-actions",
              "https://github.com/org/repo/actions/runs/7"),
             # REST lists the newer run with the same name and event first.
             ("test", "pull_request", "completed/success", "passed",
              "github-actions", "https://github.com/org/repo/actions/runs/9"),
             ("test", "pull_request", "completed/success", "passed",
              "github-actions", "https://github.com/org/repo/actions/runs/5")],
        )

    def test_a_full_status_list_is_read_by_rest(self) -> None:
        contexts = [{"context": f"s-{n}", "state": "SUCCESS",
                     "targetUrl": None, "creator": None}
                    for n in range(100)]
        response = {"repository": {"object": graphql_commit([], contexts)}}
        with patch.object(self.forge, "graphql", return_value=response), \
                patch.object(self.forge, "rest_checks",
                             return_value=()) as rest:
            self.assertEqual(self.forge.checks(H), ())
        rest.assert_called_once_with(H)

    def test_snapshot_checks_must_be_at_the_head(self) -> None:
        commit = graphql_commit([("push", [graphql_run(1, "test")])])
        self.assertEqual(len(self.forge.head_checks(commit, H)), 1)
        self.assertIsNone(self.forge.head_checks(commit, OTHER))
        self.assertIsNone(self.forge.head_checks(None, H))
        commit["checkSuites"] = None
        self.assertIsNone(self.forge.head_checks(commit, H))


class StubForge:
    """A PR whose head and check results follow scripted reads."""

    def __init__(self, results: list, heads: list | None = None) -> None:
        self.results = list(results)
        self.heads = list(heads or [])
        self.head = H
        self.reads: list[str] = []

    def pr(self, number: int):
        if self.heads:
            self.head = self.heads.pop(0)
        return type("PR", (), {"head": self.head})()

    def checks(self, sha: str) -> tuple[Check, ...]:
        self.reads.append(sha)
        result = self.results.pop(0) if len(self.results) > 1 else (
            self.results[0])
        if result == "unavailable":
            raise ChecksUnavailable("refused (HTTP 403)", 403)
        return () if result == "none" else (row(result),)


class WaitTests(unittest.TestCase):
    def run_wait(self, forge: StubForge, wait: int | None) -> tuple:
        clock = FakeClock()
        with patch("agent_squad.commands.time", clock):
            return pr_checks(forge, 1, wait), clock.sleeps

    def test_without_wait_reads_once(self) -> None:
        forge = StubForge(["pending"])
        result, sleeps = self.run_wait(forge, None)
        self.assertEqual(result["summary"], "pending")
        self.assertNotIn("timed_out", result)
        self.assertEqual((sleeps, forge.reads), ([], [H]))

    def test_returns_early_when_pending_turns_final(self) -> None:
        for final in ("passed", "failed"):
            with self.subTest(final=final):
                forge = StubForge(["pending", final])
                result, sleeps = self.run_wait(forge, 600)
                self.assertEqual(result["summary"], final)
                self.assertFalse(result["timed_out"])
                self.assertEqual(sleeps, [CHECK_POLL_INTERVAL])
                self.assertEqual(result["waited_seconds"], 10)

    def test_times_out_with_the_last_summary(self) -> None:
        forge = StubForge(["pending"])
        result, sleeps = self.run_wait(forge, 25)
        self.assertEqual(sleeps, [10, 10, 5])
        self.assertEqual(
            (result["summary"], result["timed_out"],
             result["waited_seconds"], result["head_changed"]),
            ("pending", True, 25, False),
        )
        self.assertEqual(len(forge.reads), 4)

    def test_waits_through_none(self) -> None:
        forge = StubForge(["none", "none", "passed"])
        result, sleeps = self.run_wait(forge, 600)
        self.assertEqual(result["summary"], "passed")
        self.assertEqual(sleeps, [10, 10])

    def test_unavailable_ends_the_wait_at_once(self) -> None:
        forge = StubForge(["unavailable"])
        result, sleeps = self.run_wait(forge, 600)
        self.assertEqual(result["summary"], "unavailable")
        self.assertEqual((sleeps, result["timed_out"]), ([], False))

    def test_head_change_ends_the_wait_early(self) -> None:
        # The head moves during the sleep; the next read is followed by a
        # head read that reports the change.
        forge = StubForge(["pending", "pending"], heads=[H, H, OTHER])
        result, sleeps = self.run_wait(forge, 600)
        self.assertTrue(result["head_changed"])
        self.assertFalse(result["timed_out"])
        self.assertEqual((result["head"], result["summary"]),
                         (H, "pending"))
        self.assertEqual((sleeps, forge.reads), ([10], [H, H]))

    def test_head_change_during_a_final_read_is_reported(self) -> None:
        """REV-1: a final result for the old head never ends the wait
        as if the head had not moved."""
        for name, results, heads, sleeps_expected in (
            ("initial read", ["passed"], [H, OTHER], []),
            ("read after a poll", ["pending", "passed"], [H, H, OTHER],
             [10]),
        ):
            with self.subTest(name):
                forge = StubForge(results, heads=heads)
                result, sleeps = self.run_wait(forge, 600)
                self.assertTrue(result["head_changed"])
                self.assertFalse(result["timed_out"])
                # The result stays attributed to the head it was read at.
                self.assertEqual((result["head"], result["summary"]),
                                 (H, "passed"))
                self.assertEqual(sleeps, sleeps_expected)

    def test_unmoved_head_reports_no_change(self) -> None:
        for results in (["passed"], ["pending", "passed"]):
            with self.subTest(results=results):
                forge = StubForge(results, heads=[H, H, H])
                result, _ = self.run_wait(forge, 600)
                self.assertFalse(result["head_changed"])
                self.assertEqual(result["summary"], "passed")


class RecordTests(unittest.TestCase):
    def test_rows_keep_every_record_field(self) -> None:
        check = replace(row("failed"), event="push", url="https://ci/1")
        forge = StubForge([])
        forge.checks = lambda sha: (check,)
        self.assertEqual(ci_record(forge, H), {
            "head": H, "summary": "failed", "checks": [{
                "name": "test", "event": "push", "state": "failed",
                "result": "failed", "source": None, "url": "https://ci/1",
                "started_at": None, "completed_at": None,
            }],
        })


if __name__ == "__main__":
    unittest.main()
