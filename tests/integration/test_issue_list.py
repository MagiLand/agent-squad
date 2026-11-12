"""The read-only issue list and the milestone in issue view (#164)."""

import json
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from tests.forge_support import ForgeFixture, ForgejoFixture, is_write

REPO = "/repos/MagiLand/trial"


def pr(number: int, merged: bool, state: str = "closed") -> dict:
    return {
        "id": 900 + number, "number": number, "title": f"PR {number}",
        "state": state, "body": "", "user": {"login": "developer"},
        "created_at": "2026-01-01T00:00:09Z", "merged": merged,
    }


def closer(number: int, repository: str = "MagiLand/trial") -> dict:
    return {"number": number, "repository": repository}


class IssueListContract:
    """The same contract on each fake forge; subclasses name the fixture."""

    fixture: type = ForgeFixture
    # The state reason of a closed issue: GitHub's, lower-cased, or none.
    reason: str | None = None

    def setUp(self) -> None:
        self.f = self.fixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        model = self.f.read_model()
        model["labels"] += [{"id": 43, "name": "bug"},
                            {"id": 44, "name": "x,y"}]
        # Milestone 6's title holds a comma, which F16 splits a filter at,
        # and milestone 7's title is milestone 3's reference.
        model["milestones"] = [
            {"id": 3, "title": "v1", "state": "closed"},
            {"id": 4, "title": "v1.1", "state": "open"},
            {"id": 6, "title": "a,b", "state": "open"},
            {"id": 7, "title": "3", "state": "open"},
        ]
        template = model["issues"]["1"]

        def issue(number: int, state: str, milestone: int | None,
                  labels: list[str], **links: list[str]) -> None:
            model["issues"][str(number)] = dict(
                template, id=number, number=number, title=f"Issue {number}",
                state=state, labels=[{"name": name} for name in labels],
                milestone=milestone, conversation=[],
                state_reason="completed" if state == "closed" else None,
                **links,
            )

        # Issue 2 was closed by PR 7; PR 8 only refers to it.
        issue(2, "closed", 3, ["bug"], linked_prs=["7"],
              referring_prs=["8"])
        # Issue 3 was closed by a PR elsewhere; PR 9 closed unmerged.
        issue(3, "closed", 3, ["bug", "x,y"],
              linked_prs=["MagiLand/other#4", "9"])
        # Issue 4 was closed with no pull request, like a wontfix.
        issue(4, "closed", 3, [])
        issue(5, "open", 3, ["bug"], linked_prs=["10"])
        # Issue 6 was closed by PR 7, reopened, and closed by PR 8.
        issue(6, "closed", 4, ["bug"], linked_prs=["7", "8"])
        issue(11, "closed", 6, ["bug"])
        issue(12, "open", 7, [])
        model["prs"].update({
            "7": pr(7, True), "8": pr(8, True), "9": pr(9, False),
            "10": pr(10, False, "open"),
        })
        # A pull request in the milestone is never listed.
        model["prs"]["9"]["milestone"] = 3
        model["foreign_prs"] = {"MagiLand/other#4": pr(4, True)}
        self.f.save_model(model)

    def requests(self, start: int) -> list[str]:
        """The endpoint or GraphQL operation of every forge request."""
        raise NotImplementedError

    def listed(self, *options: str, expected: int = 0) -> dict:
        start = len(self.f.read_model()["calls"])
        result = self.f.cli("issue", "list", *options, expected=expected)
        calls = self.f.read_model()["calls"][start:]
        self.assertFalse([c for c in calls if is_write(c)])
        self.sent = self.requests(start)
        return result

    def numbers(self, result: dict) -> list[int]:
        return [row["number"] for row in result["issues"]]

    def test_milestone_rows_carry_their_record_and_closing_prs(self) -> None:
        result = self.listed("--milestone", "v1", "--state", "all")
        self.assertEqual(
            {key: value for key, value in result.items() if key != "issues"},
            {"milestone": "v1", "labels": [], "state": "all"},
        )
        self.assertEqual(self.numbers(result), [2, 3, 4, 5])
        rows = {row["number"]: row for row in result["issues"]}
        self.assertEqual(rows[2], {
            "number": 2, "title": "Issue 2", "state": "closed",
            "state_reason": self.reason, "labels": ["bug"],
            "milestone": "v1", "closed_by": [closer(7)],
        })
        self.assertEqual(rows[3]["closed_by"],
                         [closer(4, "MagiLand/other")])
        self.assertEqual(rows[3]["labels"], ["bug", "x,y"])
        self.assertEqual(rows[4]["closed_by"], [])
        self.assertEqual(rows[5]["closed_by"], [])
        self.assertIsNone(rows[5]["state_reason"])

    def test_a_reopened_issue_lists_both_closing_prs(self) -> None:
        result = self.listed("--milestone", "v1.1", "--state", "closed")
        self.assertEqual(self.numbers(result), [6])
        self.assertEqual(result["issues"][0]["closed_by"],
                         [closer(7), closer(8)])

    def test_labels_and_state_narrow_on_each_record(self) -> None:
        for options, numbers in (
            (("--milestone", "v1", "--state", "closed", "--label", "bug"),
             [2, 3]),
            (("--label", "x,y", "--label", "bug", "--state", "closed"),
             [3]),
            (("--label", "bug", "--label", "bug"), [5]),
            (("--milestone", "a,b", "--state", "all"), [11]),
            (("--milestone", "v1.1", "--state", "open"), []),
            (("--milestone", "v1", "--state", "open"), [5]),
            (("--milestone", "v1", "--state", "closed"), [2, 3, 4]),
            # The title, not the reference, selects the milestone.
            (("--milestone", "3", "--state", "all"), [12]),
            ((), [1, 5, 12]),
        ):
            with self.subTest(options=options):
                result = self.listed(*options)
                self.assertEqual(self.numbers(result), numbers)
        self.assertEqual(result["state"], "open")
        self.assertIsNone(result["issues"][0]["milestone"])

    def test_unknown_or_shared_names_fail_before_the_listing(self) -> None:
        model = self.f.read_model()
        model["milestones"].append(
            {"id": 8, "title": "v1.1", "state": "closed"})
        self.f.save_model(model)
        for options, error in (
            (("--milestone", "v9"), "no milestone has the title v9"),
            (("--milestone", "V1"), "no milestone has the title V1"),
            (("--milestone", "v1.1"), "2 milestones have the title v1.1"),
            (("--label", "BUG"), "repository has no label named BUG"),
            (("--label", "x"), "repository has no label named x"),
        ):
            with self.subTest(options=options):
                result = self.listed(*options, expected=1)
                # One line naming the cause, and no list.
                self.assertTrue(result["error"].startswith(f"error: {error}"))
                self.assertNotIn("\n", result["error"])
                self.assertFalse(
                    [r for r in self.sent if "issues" in r.lower()])

    def test_either_role_reads_the_same_list(self) -> None:
        for role in ("reviewer", "implementer"):
            with self.subTest(role=role):
                result = self.listed("--as", role, "--milestone", "v1")
                self.assertEqual(self.numbers(result), [5])

    def test_issue_view_reports_the_milestone(self) -> None:
        for number, milestone in (("2", "v1"), ("1", None)):
            with self.subTest(number=number):
                result = self.f.cli("issue", "view", "--issue", number)
                self.assertEqual(result["milestone"], milestone)


class GitHubIssueListTests(IssueListContract, unittest.TestCase):
    fixture = ForgeFixture
    reason = "completed"

    def requests(self, start: int) -> list[str]:
        found = []
        for call in self.f.read_model()["calls"][start:]:
            arguments = call["arguments"]
            if arguments[:2] == ["api", "graphql"]:
                found.append(call["body"]["query"].split("(")[0])
            elif arguments[:1] == ["api"]:
                found.append(arguments[1])
        return found

    def queries(self, start: int) -> list[dict]:
        return [c["body"] for c in self.f.read_model()["calls"][start:]
                if c["arguments"][:2] == ["api", "graphql"]]

    def further_pages(self, bodies: list[dict]) -> list[tuple]:
        """(issue node, list) of each further page read for an issue."""
        return sorted(
            (body["variables"]["id"],
             body["query"].split("... on Issue { ")[1].split("(")[0])
            for body in bodies
            if body["query"].startswith("query SquadPage")
        )

    def test_values_travel_as_variables(self) -> None:
        start = len(self.f.read_model()["calls"])
        result = self.listed("--milestone", "a,b", "--label", "bug",
                             "--state", "all")
        self.assertEqual(self.numbers(result), [11])
        # One listing per state (PR #167 REV-3).
        bodies = self.queries(start)
        self.assertEqual(len(bodies), 2)
        for body, state in zip(bodies, ("OPEN", "CLOSED")):
            self.assertTrue(body["query"].startswith("query SquadIssues("))
            self.assertNotIn("a,b", body["query"])
            self.assertNotIn('"bug"', body["query"])
            self.assertEqual(body["variables"], {
                "owner": "MagiLand", "repo": "trial",
                "states": [state], "labels": ["bug"],
                "filter": {"milestoneNumber": "6"}, "cursor": None,
            })

    def test_open_issues_are_listed_without_asking_for_closing_prs(
        self,
    ) -> None:
        # PR #167 REV-3: the fake returns the closing pull requests only
        # when the query selects them, so asking for them here would show.
        for options, numbers in (
            (("--milestone", "v1",), [5]),
            (("--milestone", "v1", "--state", "all"), [2, 3, 4, 5]),
        ):
            with self.subTest(options=options):
                start = len(self.f.read_model()["calls"])
                result = self.listed(*options)
                self.assertEqual(self.numbers(result), numbers)
                rows = {row["number"]: row for row in result["issues"]}
                self.assertEqual(rows[5]["closed_by"], [])
                for body in self.queries(start):
                    self.assertEqual(
                        "closedByPullRequestsReferences" in body["query"],
                        body["variables"]["states"] == ["CLOSED"])

    def test_a_refused_closing_page_leaves_that_issue_unknown(self) -> None:
        # PR #167 REV-2: issue 6's second page of closing pull requests is
        # read after the listing; refusing it makes only that issue null.
        self.f.settings(page_sizes={
            "Issue.closedByPullRequestsReferences": 1})
        for status, expected in ((401, 0), (403, 0), (404, 0), (502, 1)):
            with self.subTest(status=status):
                self.f.settings(closing_page_refusals={"6": status})
                result = self.listed("--label", "bug", "--state", "closed",
                                     expected=expected)
                if expected:
                    self.assertIn("Resource not accessible", result["error"])
                    continue
                rows = {row["number"]: row for row in result["issues"]}
                self.assertEqual(sorted(rows), [2, 3, 6, 11])
                self.assertIsNone(rows[6]["closed_by"])
                self.assertEqual(rows[2]["closed_by"], [closer(7)])

    def test_every_page_is_read_and_only_matching_issues_complete(
        self,
    ) -> None:
        self.f.settings(page_sizes={
            "Repository.issues": 2, "Issue.labels": 1,
            "Issue.closedByPullRequestsReferences": 1,
        })
        start = len(self.f.read_model()["calls"])
        result = self.listed("--state", "closed")
        self.assertEqual(self.numbers(result), [2, 3, 4, 6, 11])
        bodies = self.queries(start)
        self.assertEqual(
            [body["variables"]["cursor"] for body in bodies
             if body["query"].startswith("query SquadIssues")],
            [None, "2", "4"],
        )
        self.assertEqual(self.further_pages(bodies), [
            ("I-3@MagiLand/trial", "labels"),
            ("I-6@MagiLand/trial", "closedByPullRequestsReferences"),
        ])
        rows = {row["number"]: row for row in result["issues"]}
        self.assertEqual(rows[6]["closed_by"], [closer(7), closer(8)])
        self.assertEqual(rows[3]["labels"], ["bug", "x,y"])
        # GitHub returns issues with any of the labels; issue 6 lacks one,
        # so its second page of closing pull requests is never read.
        start = len(self.f.read_model()["calls"])
        result = self.listed("--label", "bug", "--label", "x,y",
                             "--state", "closed")
        self.assertEqual(self.numbers(result), [3])
        self.assertEqual(self.further_pages(self.queries(start)), [
            ("I-3@MagiLand/trial", "labels"),
        ])

    def test_processes_grow_with_pages_not_issues(self) -> None:
        counts = []
        for added in (0, 40):
            model = self.f.read_model()
            for number in range(100, 100 + added):
                model["issues"][str(number)] = dict(
                    model["issues"]["2"], id=number, number=number)
            self.f.save_model(model)
            start = len(self.f.read_model()["calls"])
            result = self.listed("--milestone", "v1", "--state", "closed")
            self.assertEqual(len(result["issues"]), 3 + added)
            counts.append(len(self.f.read_model()["calls"]) - start)
        self.assertEqual(counts[0], counts[1])


class ForgejoIssueListTests(IssueListContract, unittest.TestCase):
    fixture = ForgejoFixture
    reason = None

    def requests(self, start: int) -> list[str]:
        return [
            c["path"].removeprefix("/api/v1")
            for c in self.f.read_model()["calls"][start:]
            if c["path"] not in ("/api/v1/version", "/api/v1/user")
        ]

    def test_no_filter_is_sent_and_short_pages_do_not_end_a_read(
        self,
    ) -> None:
        self.f.settings(max_response_items=2)
        result = self.listed("--milestone", "v1", "--state", "closed")
        self.assertEqual(self.numbers(result), [2, 3, 4])
        listing = [urlsplit(r) for r in self.sent
                   if urlsplit(r).path == f"{REPO}/issues"]
        self.assertTrue(listing)
        for request in listing:
            query = parse_qs(request.query)
            self.assertEqual(
                {key: value for key, value in query.items()
                 if key != "page"},
                {"state": ["closed"], "type": ["issues"], "limit": ["50"]},
            )
        # Five closed issues in pages of two, then the empty page.
        self.assertEqual(len(listing), 4)
        timelines = [r for r in self.sent if "/timeline" in r]
        self.assertEqual(
            sorted({urlsplit(r).path for r in timelines}),
            [f"{REPO}/issues/{n}/timeline" for n in (2, 3, 4)],
        )

    def test_a_label_lost_to_a_short_label_page_is_refused(self) -> None:
        # The label read predates #164 and stops at a short page, as the
        # triage observed on F16; the result is an error, never a list.
        self.f.settings(max_response_items=2)
        result = self.listed("--label", "bug", expected=1)
        self.assertTrue(result["error"].startswith(
            "error: repository has no label named bug"))
        self.assertFalse([r for r in self.sent if "/issues" in r])

    def test_open_issues_get_no_timeline_read(self) -> None:
        result = self.listed("--milestone", "v1", "--state", "open")
        self.assertEqual(self.numbers(result), [5])
        self.assertEqual(result["issues"][0]["closed_by"], [])
        self.assertFalse([r for r in self.sent if "/timeline" in r])

    def test_a_refused_timeline_is_null_and_a_failed_one_fails(self) -> None:
        for status, expected in ((403, 0), (404, 0), (401, 0), (500, 1)):
            with self.subTest(status=status):
                self.f.settings(timeline_refusals={"3": status})
                result = self.listed("--milestone", "v1", "--state",
                                     "closed", expected=expected)
                if expected:
                    self.assertIn("timeline unreadable", result["error"])
                    continue
                rows = {row["number"]: row for row in result["issues"]}
                self.assertIsNone(rows[3]["closed_by"])
                self.assertEqual(rows[2]["closed_by"], [closer(7)])

    def test_rows_dropped_after_paging_leave_a_short_page(self) -> None:
        model = self.f.read_model()
        model["foreign_prs"]["MagiLand/hidden#5"] = dict(
            pr(5, True), hidden=True)
        model["issues"]["2"]["linked_prs"] = [
            "MagiLand/hidden#5", "9", "7", "MagiLand/other#4"]
        # An earlier close and its reopen, and a commit's reference, name
        # no PR; the fake adds the last close after the linked PRs.
        model["issues"]["2"]["timeline_extra"] = [
            {"type": "comment", "body": "Closes #2"},
            {"type": "code", "hidden": True},
            {"type": "close", "created_at": "2026-01-01T00:00:01Z"},
            {"type": "reopen", "created_at": "2026-01-01T00:00:02Z"},
            {"type": "commit_ref", "ref_commit_sha": "a" * 40},
        ]
        self.f.save_model(model)
        self.f.settings(max_response_items=2)
        result = self.listed("--milestone", "v1", "--state", "closed")
        rows = {row["number"]: row for row in result["issues"]}
        self.assertEqual(rows[2]["closed_by"], [
            closer(4, "MagiLand/other"), closer(7),
        ])
        timeline = [r for r in self.sent
                    if urlsplit(r).path == f"{REPO}/issues/2/timeline"]
        # Eleven rows in pages of two, two of them holding one row once
        # the hidden one is dropped, then the null page beyond the end.
        self.assertEqual(len(timeline), 7)

    def test_a_hidden_page_before_the_last_close_leaves_it_unknown(
        self,
    ) -> None:
        # Decision 5 of #164: rows the reader may not see fill issue 2's
        # first page, which F16 serves as null, as it serves the end.
        model = self.f.read_model()
        model["issues"]["2"]["timeline_extra"] = [
            {"type": "issue_ref", "hidden": True},
            {"type": "issue_ref", "hidden": True},
        ]
        # Issue 3 hides one row of a page and is read to its close.
        model["issues"]["3"]["timeline_extra"] = [
            {"type": "issue_ref", "hidden": True},
        ]
        # Issue 4's close time was never recorded: unknown, with no read.
        model["issues"]["4"]["closed_at"] = None
        self.f.save_model(model)
        self.f.settings(max_response_items=2)
        result = self.listed("--milestone", "v1", "--state", "closed")
        rows = {row["number"]: row for row in result["issues"]}
        self.assertEqual(sorted(rows), [2, 3, 4])
        self.assertIsNone(rows[2]["closed_by"])
        self.assertEqual(rows[3]["closed_by"],
                         [closer(4, "MagiLand/other")])
        self.assertIsNone(rows[4]["closed_by"])
        # The listing comes newest first. Issue 3 is read to the null page
        # after its close; issue 2's read ends at its first page.
        self.assertEqual(
            [urlsplit(r).path for r in self.sent if "/timeline" in r],
            [*[f"{REPO}/issues/3/timeline"] * 3,
             f"{REPO}/issues/2/timeline"])

    def fake(self, account: str = "developer", **query: str) -> list[int]:
        """Ask the fake's issue listing directly; no CLI path sends this."""
        url = (f"{self.f.server.base_url}/api/v1{REPO}/issues?"
               + urlencode({"state": "all", "type": "issues", **query}))
        request = Request(url, headers={
            "Authorization": f"token fake-token-{account}"})
        with urlopen(request, timeout=10) as response:
            return sorted(row["number"] for row in json.load(response))

    def test_fake_models_the_f16_listing_filters(self) -> None:
        model = self.f.read_model()
        # Milestone 8 shares milestone 4's title; F16 picks one of them.
        model["milestones"].append(
            {"id": 8, "title": "v1.1", "state": "closed"})
        model["issues"]["13"] = dict(
            model["issues"]["4"], id=13, number=13, milestone=8)
        self.f.save_model(model)
        everything = self.fake()
        self.assertEqual(everything, [1, 2, 3, 4, 5, 6, 11, 12, 13])
        for query, numbers in (
            ({"milestones": "v1"}, [2, 3, 4, 5]),
            ({"milestones": "4"}, [6]),
            # Milestone 7's title "3" wins over milestone 3's ID.
            ({"milestones": "3"}, [12]),
            ({"milestones": "v1.1"}, [6]),
            # A comma names several milestones; parts naming none drop.
            ({"milestones": "v1,4"}, [2, 3, 4, 5, 6]),
            ({"milestones": "v1,v9"}, [2, 3, 4, 5]),
            # A filter naming no milestone is ignored entirely.
            ({"milestones": "a,b"}, everything),
            ({"milestones": "V1"}, everything),
            ({"milestones": "v9"}, everything),
            ({"labels": "bug"}, [2, 3, 5, 6, 11]),
            ({"labels": "bug,x"}, [2, 3, 5, 6, 11]),
            ({"labels": "BUG"}, everything),
            ({"labels": "x,y"}, everything),
        ):
            with self.subTest(query=query):
                self.assertEqual(self.fake(**query), numbers)


if __name__ == "__main__":
    unittest.main()
