"""CI result classification, summary, the checks wait (#145), and logs."""

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from tests._support import FakeClock, add_src_to_path
from tests.unit.test_conventions import H, config

add_src_to_path()

from agent_squad.commands import (  # noqa: E402
    CHECK_POLL_INTERVAL, FAILED_LOG_LINES, LOG_REASON_LENGTH, ci_record,
    log_excerpt, log_reason, pr_checks, summarize_checks,
)
from agent_squad.forge import (  # noqa: E402
    Check, ChecksUnavailable, CommitChecks, ForgeError, NoJobLog,
    UnfinishedRun, visible,
)
from agent_squad.forgejo import (  # noqa: E402
    Forgejo, names_job, parse_status,
)
from agent_squad.github import (  # noqa: E402
    GitHub, check_run_result, parse_check_run, parse_commit_status,
)
from agent_squad.initialization import GateError, Repository  # noqa: E402
from tests.unit.test_forgejo import forgejo_config  # noqa: E402

OTHER = "c" * 40
MERGE = "d" * 40


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

    def test_only_a_check_run_is_marked_as_one(self) -> None:
        # #176: a status may carry any creator login, the Actions app's
        # slug included; only the mark says the URL names a job. Both
        # checks reads, GraphQL and REST, build checks with these parsers.
        status = parse_commit_status({
            "context": "test", "state": "failure",
            "creator": {"login": "github-actions"},
            "target_url": "https://github.com/MagiLand/trial/actions/runs/5"
                          "/job/12",
        })
        self.assertEqual(status.source, "github-actions")
        self.assertFalse(status.check_run)
        run = parse_check_run({
            "name": "test", "status": "completed", "conclusion": "failure",
            "check_suite": None, "app": {"slug": "github-actions"},
            "html_url": None, "started_at": None, "completed_at": None,
        }, {})
        self.assertTrue(run.check_run)
        self.assertIsNone(run.check_run_id)
        # REST gives a check run's ID as ``id``; GraphQL's ``databaseId``
        # is rewritten into it (REV-7 of PR #181).
        self.assertEqual(parse_check_run({
            "id": 12, "name": "test", "status": "completed",
            "conclusion": "failure", "check_suite": None, "app": None,
            "html_url": None, "started_at": None, "completed_at": None,
        }, {}).check_run_id, 12)
        # The mark and the ID stay out of the public check record.
        self.assertNotIn("check_run", run.record())
        self.assertEqual(set(run.record()), {
            "name", "event", "state", "result", "source", "url",
            "started_at", "completed_at",
        })

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
            check_run=True,
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
                    summarize_checks(CommitChecks(tuple(map(row, results)))),
                    expected,
                )

    def test_an_unfinished_run_is_pending_unless_a_row_failed(self) -> None:
        # #169: a run that has not completed may still create check runs.
        run = (UnfinishedRun(7, "in_progress"),)
        for results, expected in (
            ((), "pending"),
            (("passed",), "pending"),
            (("skipped",), "pending"),
            (("passed", "pending"), "pending"),
            (("passed", "failed"), "failed"),
            (("failed",), "failed"),
        ):
            with self.subTest(results=results):
                self.assertEqual(summarize_checks(CommitChecks(
                    tuple(map(row, results)), run)), expected)


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
                        self.assertEqual(record["unfinished_runs"], [])
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
            {"id": f"CS-{n}", "status": "COMPLETED",
             "app": {"slug": "github-actions"},
             "workflowRun": None if event is None else {
                 "databaseId": 900 + n, "event": event},
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

    def read(self, commit: dict) -> CommitChecks:
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
        self.assertEqual(checks.unfinished_runs, ())
        self.assertEqual(
            [(c.name, c.event, c.state, c.result, c.source, c.url)
             for c in checks.checks],
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
                             return_value=CommitChecks(())) as rest:
            self.assertEqual(self.forge.checks(H), CommitChecks(()))
        rest.assert_called_once_with(H)

    def test_snapshot_checks_must_be_at_the_head(self) -> None:
        commit = graphql_commit([("push", [graphql_run(1, "test")])])
        self.assertEqual(len(self.forge.head_checks(commit, H).checks), 1)
        self.assertIsNone(self.forge.head_checks(commit, OTHER))
        self.assertIsNone(self.forge.head_checks(None, H))
        commit["checkSuites"] = None
        self.assertIsNone(self.forge.head_checks(commit, H))

    def test_an_unfinished_workflow_suite_is_an_unfinished_run(self) -> None:
        # #169: the suite's status, not its check runs, says whether the
        # workflow run has completed; its ID is the run's.
        for status, expected in (
            ("COMPLETED", ()),
            *((value, (UnfinishedRun(900, value.lower()),)) for value in (
                "QUEUED", "IN_PROGRESS", "PENDING", "REQUESTED", "WAITING",
                "UNNAMED_STATUS")),
        ):
            with self.subTest(status=status):
                commit = graphql_commit([("push", [graphql_run(1, "test")])])
                commit["checkSuites"]["nodes"][0]["status"] = status
                found = self.read(commit)
                self.assertEqual(found.unfinished_runs, expected)
                self.assertEqual([c.result for c in found.checks],
                                 ["passed"])

    def test_unfinished_runs_are_ordered_by_id(self) -> None:
        commit = graphql_commit([("push", []), ("pull_request", [])])
        for node, (ident, status) in zip(
            commit["checkSuites"]["nodes"],
            ((902, "QUEUED"), (901, "IN_PROGRESS")),
        ):
            node["status"] = status
            node["workflowRun"]["databaseId"] = ident
        self.assertEqual(self.read(commit).unfinished_runs, (
            UnfinishedRun(901, "in_progress"), UnfinishedRun(902, "queued"),
        ))

    def test_a_suite_without_a_workflow_run_is_ignored(self) -> None:
        # Other apps' suites stayed QUEUED for 25 to 40 minutes, with and
        # without check runs, in the triage of #169.
        commit = graphql_commit([(None, []), (None, [graphql_run(3, "az")]),
                                 (None, [])])
        nodes = commit["checkSuites"]["nodes"]
        nodes[0].update(status="QUEUED", app={"slug": "codecov"})
        nodes[1].update(status="QUEUED", app={"slug": "azure-pipelines"})
        del nodes[2]["status"]
        found = self.read(commit)
        self.assertEqual(found.unfinished_runs, ())
        self.assertEqual([c.result for c in found.checks], ["passed"])

    def test_an_unreadable_workflow_suite_is_read_by_rest(self) -> None:
        rest = CommitChecks((), (UnfinishedRun(4, "queued"),))
        for field, value in (
            ("status", None), ("status", ""), ("status", 7),
            ("databaseId", None), ("databaseId", 0), ("databaseId", "12"),
        ):
            with self.subTest(field=field, value=value):
                commit = graphql_commit([("push", [graphql_run(1, "test")])])
                node = commit["checkSuites"]["nodes"][0]
                node["status"] = "IN_PROGRESS"
                target = node if field == "status" else node["workflowRun"]
                target[field] = value
                response = {"repository": {"object": commit}}
                with patch.object(self.forge, "graphql",
                                  return_value=response), \
                        patch.object(self.forge, "rest_checks",
                                     return_value=rest) as read:
                    self.assertEqual(self.forge.checks(H), rest)
                read.assert_called_once_with(H)
                self.assertIsNone(self.forge.head_checks(commit, H))

    def test_a_null_suite_is_read_by_rest(self) -> None:
        # GitHub answered `checkSuites.nodes: [null]` with no error 6.5
        # seconds after a push in the triage of #169.
        commit = graphql_commit([])
        commit["checkSuites"]["nodes"] = [None]
        self.assertIsNone(self.forge.head_checks(commit, H))
        response = {"repository": {"object": commit}}
        with patch.object(self.forge, "graphql", return_value=response), \
                patch.object(self.forge, "rest_checks",
                             return_value=CommitChecks(())) as read:
            self.assertEqual(self.forge.checks(H), CommitChecks(()))
        read.assert_called_once_with(H)


def rest_run(ident: int, status: object, suite: int = 1) -> dict:
    return {"id": ident, "check_suite_id": suite, "event": "push",
            "status": status}


class RestCheckTests(unittest.TestCase):
    """The REST read lists the workflow runs at the commit (#169)."""

    def setUp(self) -> None:
        path = Path("/repo")
        with patch("agent_squad.github.shutil.which", return_value="/gh"):
            self.forge = GitHub(
                Repository(path, path, path / ".git", config()), "reviewer"
            )

    def read(self, runs: list) -> CommitChecks:
        pages = {
            "check-runs": {"check_runs": [{
                "id": 3, "name": "test", "status": "completed",
                "conclusion": "success", "check_suite": {"id": 1},
                "app": {"slug": "github-actions"}, "html_url": None,
                "started_at": None, "completed_at": None,
            }]},
            "/status": {"state": "pending", "statuses": []},
            "actions/runs": {"workflow_runs": runs},
        }
        with patch.object(self.forge, "graphql",
                          side_effect=ForgeError("GraphQL failed")), \
                patch.object(self.forge, "api", side_effect=lambda e: next(
                    v for k, v in pages.items() if k in e)):
            return self.forge.checks(H)

    def test_a_run_not_completed_is_an_unfinished_run(self) -> None:
        for status, expected in (
            ("completed", ()),
            *((value, (UnfinishedRun(5, value),)) for value in (
                "queued", "in_progress", "waiting", "requested", "pending",
                "unnamed_status")),
        ):
            with self.subTest(status=status):
                found = self.read([rest_run(5, status)])
                self.assertEqual(found.unfinished_runs, expected)
                self.assertEqual(
                    [(c.event, c.result) for c in found.checks],
                    [("push", "passed")],
                )

    def test_unfinished_runs_are_ordered_by_id(self) -> None:
        found = self.read([rest_run(9, "queued", 2),
                           rest_run(8, "completed", 3),
                           rest_run(7, "in_progress")])
        self.assertEqual(found.unfinished_runs, (
            UnfinishedRun(7, "in_progress"), UnfinishedRun(9, "queued"),
        ))

    def test_an_unreadable_run_fails_the_read(self) -> None:
        for run in (
            rest_run(5, None), rest_run(5, ""), rest_run(5, 7),
            rest_run(None, "queued"), rest_run("5", "queued"),
            rest_run(0, "queued"),
        ):
            with self.subTest(run=run):
                with self.assertRaises(ForgeError) as raised:
                    self.read([run])
                self.assertNotIsInstance(raised.exception, ChecksUnavailable)


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

    def checks(self, sha: str) -> CommitChecks:
        self.reads.append(sha)
        result = self.results.pop(0) if len(self.results) > 1 else (
            self.results[0])
        if result == "unavailable":
            raise ChecksUnavailable("refused (HTTP 403)", 403)
        return CommitChecks(() if result == "none" else (row(result),))


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


class MergedForge(StubForge):
    """A closed PR; counts its reads, which must stay at one (#157)."""

    def __init__(
        self,
        results: list,
        *,
        merged: bool = True,
        merge_commit: str | None = MERGE,
    ) -> None:
        super().__init__(results)
        self.merged = merged
        self.merge_commit = merge_commit
        self.pr_reads = 0

    def pr(self, number: int):
        self.pr_reads += 1
        return type("PR", (), {
            "number": number, "head": self.head, "merged": self.merged,
            "merge_commit": self.merge_commit,
        })()


class MergeCommitTests(unittest.TestCase):
    def run_wait(self, forge: MergedForge, wait: int | None) -> tuple:
        clock = FakeClock()
        with patch("agent_squad.commands.time", clock):
            return pr_checks(forge, 1, wait, merge_commit=True), clock.sleeps

    def test_without_wait_reads_the_merge_commit_once(self) -> None:
        forge = MergedForge(["failed"])
        result, sleeps = self.run_wait(forge, None)
        self.assertEqual((result["head"], result["summary"]),
                         (MERGE, "failed"))
        self.assertNotIn("head_changed", result)
        self.assertEqual((forge.reads, forge.pr_reads, sleeps),
                         ([MERGE], 1, []))

    def test_wait_moves_through_none_and_pending_to_final(self) -> None:
        forge = MergedForge(["none", "pending", "failed"])
        result, sleeps = self.run_wait(forge, 60)
        self.assertEqual(result, {
            "head": MERGE, "summary": "failed",
            "checks": result["checks"], "unfinished_runs": [],
            "waited_seconds": 20, "timed_out": False, "head_changed": False,
        })
        self.assertEqual(sleeps, [CHECK_POLL_INTERVAL] * 2)
        self.assertEqual(forge.reads, [MERGE] * 3)
        self.assertEqual(forge.pr_reads, 1)

    def test_wait_times_out_with_the_last_summary(self) -> None:
        forge = MergedForge(["pending"])
        result, sleeps = self.run_wait(forge, 15)
        self.assertEqual(result["summary"], "pending")
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["head_changed"])
        self.assertEqual(sleeps, [10, 5])
        self.assertEqual(forge.pr_reads, 1)

    def test_unavailable_ends_the_wait_at_once(self) -> None:
        forge = MergedForge(["unavailable"])
        result, sleeps = self.run_wait(forge, 60)
        self.assertEqual(result["summary"], "unavailable")
        self.assertFalse(result["timed_out"])
        self.assertEqual(sleeps, [])

    def test_refuses_before_any_checks_read(self) -> None:
        for merged, merge_commit, message in (
            (False, None, "not merged"),
            (False, MERGE, "not merged"),
            (True, None, "no merge commit"),
        ):
            with self.subTest(merged=merged, merge_commit=merge_commit):
                forge = MergedForge(
                    ["passed"], merged=merged, merge_commit=merge_commit,
                )
                with self.assertRaisesRegex(GateError, message):
                    pr_checks(forge, 1, merge_commit=True)
                self.assertEqual(forge.reads, [])

    def test_without_the_flag_a_merged_pr_reads_its_head(self) -> None:
        forge = MergedForge(["passed"])
        self.assertEqual(pr_checks(forge, 1)["head"], H)
        self.assertEqual(forge.reads, [H])


class RecordTests(unittest.TestCase):
    def test_rows_keep_every_record_field(self) -> None:
        check = replace(row("failed"), event="push", url="https://ci/1")
        forge = StubForge([])
        forge.checks = lambda sha: CommitChecks(
            (check,), (UnfinishedRun(7, "queued"),)
        )
        self.assertEqual(ci_record(forge, H), {
            "head": H, "summary": "failed", "checks": [{
                "name": "test", "event": "push", "state": "failed",
                "result": "failed", "source": None, "url": "https://ci/1",
                "started_at": None, "completed_at": None,
            }],
            "unfinished_runs": [{"id": 7, "status": "queued"}],
        })


class LogExcerptTests(unittest.TestCase):
    """The excerpt ``pr checks --failed-logs`` gives a failed job (#176)."""

    def test_control_characters_become_visible(self) -> None:
        self.assertEqual(
            visible("a\x1b[1mb\x07\x00\x7f\x9b\tc\nd ﻿"),
            "a\\x1b[1mb\\x07\\x00\\x7f\\x9b\tc\nd ﻿",
        )

    def test_excerpt_ends_at_the_last_marked_line(self) -> None:
        lines = [f"line {n}" for n in range(1, 151)]
        lines[119] = "x ##[error]first"
        lines[129] = "x ##[error]last"
        excerpt = log_excerpt("\n".join(lines) + "\n", "##[error]")
        self.assertEqual(len(excerpt["lines"]), FAILED_LOG_LINES)
        self.assertEqual(excerpt["lines"][0], "line 31")
        self.assertEqual(excerpt["lines"][-1], "x ##[error]last")
        self.assertEqual((excerpt["total_lines"], excerpt["truncated"]),
                         (150, True))

    def test_without_a_marker_the_excerpt_ends_at_the_last_line(self) -> None:
        excerpt = log_excerpt("a ##[error]x\r\nb\r\n", None)
        self.assertEqual(excerpt, {
            "lines": ["a ##[error]x", "b"], "total_lines": 2,
            "truncated": False,
        })
        self.assertEqual(log_excerpt("a\nb", "##[error]")["lines"],
                         ["a", "b"])

    def test_a_bare_carriage_return_stays_in_its_line(self) -> None:
        # Only LF and CRLF end a line; a progress update's bare CR, inside
        # a line or at the end of the log, is shown (REV-3 of PR #181).
        self.assertEqual(log_excerpt("10%\r20%\nend\r", None), {
            "lines": ["10%\\x0d20%", "end\\x0d"], "total_lines": 2,
            "truncated": False,
        })
        self.assertEqual(log_excerpt("a\r\r\nb\r\n", None)["lines"],
                         ["a\\x0d", "b"])

    def test_empty_log_has_no_lines(self) -> None:
        self.assertEqual(log_excerpt("", "##[error]"), {
            "lines": [], "total_lines": 0, "truncated": False,
        })

    def test_status_context_names_its_job(self) -> None:
        for context, name, expected in (
            ("Tests / unit (pull_request)", "unit", True),
            ("/ alpha (push)", "alpha", True),
            ("ci / test (3.11) (push)", "test (3.11)", True),
            ("a / b / unit (push)", "unit", True),
            ("Tests / unit (pull_request)", "it", False),
            ("Tests / unit (pull_request)", "Tests / unit", False),
            ("Tests / unit", "unit", False),
            ("unit (push)", "unit", False),
        ):
            with self.subTest(context=context, name=name):
                self.assertIs(names_job(context, name), expected)


class LogReasonTests(unittest.TestCase):
    """The reason a failed check has no log (REV-9 of PR #181)."""

    def test_reason_is_one_short_nonempty_line(self) -> None:
        self.assertEqual(log_reason(NoJobLog("a\n  b\r\nc")), "a b c")
        self.assertEqual(log_reason(NoJobLog("")), "NoJobLog")
        long = log_reason(ForgeError("x" * 5000, 404))
        self.assertEqual(
            long, "x" * (LOG_REASON_LENGTH - 3) + "... (HTTP 404)")
        self.assertEqual(log_reason(ForgeError("refused (HTTP 403)", 403)),
                         "refused (HTTP 403)")


class ForgejoJobPositionTests(unittest.TestCase):
    """Which status URLs name a job of the configured repository (#176)."""

    def position(self, url: str, *, status: bool = True):
        from types import SimpleNamespace

        forge = SimpleNamespace(
            base_url="https://git.example.com/forge",
            repository=SimpleNamespace(configuration=SimpleNamespace(
                forge=SimpleNamespace(owner="MagiLand", repo="trial"),
            )),
        )
        return Forgejo.job_position(forge, url, status=status)

    def test_origin_folds_ascii_case_only(self) -> None:
        path = "/forge/MagiLand/trial/actions/runs/3/jobs/1"
        for url in (path, "https://git.example.com" + path,
                    "HTTPS://GIT.Example.COM" + path,
                    "https://git.example.com/forge/magiland/TRIAL/actions"
                    "/runs/3/jobs/1"):
            with self.subTest(url=url):
                self.assertEqual(self.position(url), (3, 1))
        # Unicode folds "ı" and "İ" to "i" and "ſ" to "s"; host names, the
        # scheme, and account names do not (REV-11 of PR #181).
        for url in ("https://g\u0131t.example.com" + path,
                    "https://G\u0130T.example.com" + path,
                    "http\u017f://git.example.com" + path,
                    "https://git.example.com/forge/Mag\u0131Land/trial"
                    "/actions/runs/3/jobs/1"):
            with self.subTest(url=url):
                self.assertIsNone(self.position(url))
        # A job's html_url is compared by path, whatever its origin.
        self.assertEqual(
            self.position("http://other.example" + path, status=False),
            (3, 1))


if __name__ == "__main__":
    unittest.main()
