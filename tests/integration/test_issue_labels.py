"""Issue dependencies in issue view and the guarded issue label (#144)."""

import sys
import unittest

from tests.forge_support import ForgeFixture, ForgejoFixture

REPO = "/repos/MagiLand/trial"
PROTECTED = ("needs-triage", "needs-info", "ready-for-human", "wontfix")


def related(number: int, state: str, title: str | None = None,
            repository: str = "MagiLand/trial") -> dict:
    return {"number": number, "title": title or f"Issue {number}",
            "state": state, "repository": repository}


class IssueLabelContract:
    """The same contract on each fake forge; subclasses name the fixture."""

    fixture: type = ForgeFixture
    # The add request's label list: names on GitHub, IDs on Forgejo.
    added_labels: list = []

    def setUp(self) -> None:
        self.f = self.fixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        model = self.f.read_model()
        model["labels"] += [
            {"id": 43, "name": "needs-info"},
            {"id": 44, "name": "ready-for-human"},
            {"id": 45, "name": "wontfix"},
            {"id": 46, "name": "area: cli/forge"},
        ]
        template = model["issues"]["1"]

        def issue(number: int, state: str, labels: list[str],
                  blocked_by: list[str]) -> None:
            model["issues"][str(number)] = dict(
                template, id=number, number=number, title=f"Issue {number}",
                state=state, labels=[{"name": name} for name in labels],
                conversation=[], blocked_by=blocked_by,
            )

        # Issue 2 is the closed Task issue; it blocked 3, 4, and an issue in
        # another repository, and was itself blocked by a closed one there.
        issue(2, "closed", ["ready-for-agent"], ["MagiLand/other#8"])
        issue(3, "open", [], ["2"])
        issue(4, "open", ["needs-info"], ["2", "5"])
        issue(5, "open", ["needs-triage"], [])
        issue(6, "open", [], [])
        model["foreign_issues"] = {
            "MagiLand/other#7": {"title": "Elsewhere", "state": "open",
                                 "blocked_by": ["2"]},
            "MagiLand/other#8": {"title": "Earlier", "state": "closed"},
        }
        # Number 9 is only a pull request.
        model["prs"]["9"] = {
            "id": 9, "number": 9, "title": "A pull request",
            "state": "open", "body": "", "user": {"login": "developer"},
            "created_at": "2026-01-01T00:00:09Z", "merged": False,
        }
        self.f.save_model(model)

    def requests(self, start: int) -> list[tuple]:
        """(method, endpoint, body, account) of every forge request."""
        raise NotImplementedError

    def writes(self, start: int) -> list[tuple]:
        return [r for r in self.requests(start) if r[0] != "GET"]

    def label(self, *options: str, expected: int = 0) -> dict:
        return self.f.cli("issue", "label", "--as", "implementer",
                          *options, expected=expected)

    def labels_of(self, number: str) -> list[str]:
        return [label["name"]
                for label in self.f.read_model()["issues"][number]["labels"]]

    def test_issue_view_reports_dependencies_in_both_directions(self) -> None:
        f = self.f
        view = f.cli("issue", "view", "--issue", "2")
        self.assertEqual(view["blocks"], [
            related(3, "open"), related(4, "open"),
            related(7, "open", "Elsewhere", "MagiLand/other"),
        ])
        self.assertEqual(view["blocked_by"], [
            related(8, "closed", "Earlier", "MagiLand/other"),
        ])
        self.assertEqual(
            (view["state"], view["labels"]), ("closed", ["ready-for-agent"]))
        view = f.cli("issue", "view", "--issue", "4")
        self.assertEqual(
            (view["blocks"], view["blocked_by"]),
            ([], [related(2, "closed"), related(5, "open")]),
        )
        view = f.cli("issue", "view", "--issue", "6")
        self.assertEqual((view["blocks"], view["blocked_by"]), ([], []))
        # A refused dependency read is null; the rest of the view stands.
        f.settings(dependencies_denied=["developer"])
        view = f.cli("issue", "view", "--issue", "2")
        self.assertEqual(
            (view["blocks"], view["blocked_by"], view["labels"]),
            (None, None, ["ready-for-agent"]),
        )

    def test_issue_label_changes_labels_on_closed_and_open_issues(
        self,
    ) -> None:
        f = self.f
        start = len(f.read_model()["calls"])
        result = self.label("--issue", "2", "--remove", "ready-for-agent")
        self.assertEqual(result, {
            "issue": 2, "state": "closed",
            "labels_before": ["ready-for-agent"], "labels_after": [],
            "added": [], "removed": ["ready-for-agent"],
        })
        self.assertEqual(self.labels_of("2"), [])
        self.assertEqual(self.writes(start), [
            ("DELETE", f"{REPO}/issues/2/labels/ready-for-agent", None,
             "developer"),
        ])
        start = len(f.read_model()["calls"])
        result = self.label("--issue", "3", "--add", "ready-for-agent",
                            "--add", "area: cli/forge")
        self.assertEqual(
            (result["state"], result["labels_before"],
             result["labels_after"], result["added"], result["removed"]),
            ("open", [], ["ready-for-agent", "area: cli/forge"],
             ["ready-for-agent", "area: cli/forge"], []),
        )
        self.assertEqual(self.writes(start), [
            ("POST", f"{REPO}/issues/3/labels",
             {"labels": self.added_labels}, "developer"),
        ])
        # Removal names the label in the path, encoded.
        start = len(f.read_model()["calls"])
        result = self.label("--issue", "3", "--remove", "area: cli/forge",
                            "--add", "wontfix")
        self.assertEqual(
            (result["added"], result["removed"], result["labels_after"]),
            (["wontfix"], ["area: cli/forge"],
             ["ready-for-agent", "wontfix"]),
        )
        self.assertEqual(
            [(method, endpoint) for method, endpoint, _, _
             in self.writes(start)],
            [("POST", f"{REPO}/issues/3/labels"),
             ("DELETE", f"{REPO}/issues/3/labels/area%3A%20cli%2Fforge")],
        )
        self.assertEqual(self.labels_of("3"), ["ready-for-agent", "wontfix"])
        human = f.run([sys.executable, "-m", "agent_squad", "issue", "label",
                       "--as", "implementer", "--issue", "6", "--add",
                       "ready-for-agent"], cwd=f.worktree)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(human.stdout.splitlines()[0],
                         "Issue #6: added ready-for-agent.")

    def test_present_and_absent_labels_need_no_write(self) -> None:
        f = self.f
        start = len(f.read_model()["calls"])
        result = self.label("--issue", "4", "--add", "needs-info",
                            "--remove", "ready-for-agent")
        self.assertEqual(result, {
            "issue": 4, "state": "open", "labels_before": ["needs-info"],
            "labels_after": ["needs-info"], "added": [], "removed": [],
        })
        self.assertTrue(self.requests(start))
        self.assertEqual(self.writes(start), [])

    def test_refusals_send_no_write(self) -> None:
        f = self.f
        cases = [
            (("--issue", "3", "--add", "ready"), 1,
             "repository has no label named ready; issue label never"
             " creates one", True),
            (("--issue", "3", "--remove", "Ready-For-Agent"), 1,
             "repository has no label named Ready-For-Agent", True),
            (("--issue", "9", "--add", "ready-for-agent"), 1,
             "issue label requires an issue, not pull request #9", True),
            (("--issue", "3",), 2,
             "issue label requires --add or --remove", False),
            (("--issue", "3", "--add", "wontfix", "--remove", "wontfix"), 1,
             "issue label cannot both add and remove wontfix", False),
        ]
        # Protected even when absent, and whatever its case.
        cases += [
            (("--issue", "5", "--remove", name), 1,
             f"issue label never removes {name}, which keeps an issue from"
             " being a Task", False)
            for name in (*PROTECTED, "Needs-Triage")
        ]
        for options, expected, message, reads in cases:
            with self.subTest(options=options):
                before = f.read_model()
                refused = self.label(*options, expected=expected)
                self.assertIn(message, refused["error"])
                after = f.read_model()
                self.assertEqual(after["issues"], before["issues"])
                requests = self.requests(len(before["calls"]))
                self.assertEqual(self.writes(len(before["calls"])), [])
                self.assertEqual(bool(requests), reads)
        start = len(f.read_model()["calls"])
        refused = f.cli("issue", "label", "--as", "reviewer", "--issue", "3",
                        "--add", "ready-for-agent", expected=2)
        self.assertIn("invalid choice: 'reviewer'", refused["error"])
        self.assertEqual(self.requests(start), [])

    def test_a_change_missing_from_the_read_back_fails(self) -> None:
        f = self.f
        f.settings(ignore_label_writes=True)
        for options, message in (
            (("--issue", "3", "--add", "ready-for-agent"),
             "issue #3 read back after the label change: ready-for-agent"
             " was not added"),
            (("--issue", "2", "--remove", "ready-for-agent", "--add",
              "wontfix"),
             "issue #2 read back after the label change: wontfix was not"
             " added; ready-for-agent was not removed"),
        ):
            with self.subTest(options=options):
                refused = self.label(*options, expected=1)
                self.assertEqual(refused["error"], "error: " + message)


class GitHubIssueLabelTests(IssueLabelContract, unittest.TestCase):
    fixture = ForgeFixture
    added_labels = ["ready-for-agent", "area: cli/forge"]

    def requests(self, start: int) -> list[tuple]:
        return [
            (c["arguments"][c["arguments"].index("--method") + 1],
             c["arguments"][1], c["body"], c["account"])
            for c in self.f.read_model()["calls"][start:]
            if c["arguments"][:1] == ["api"]
        ]


class ForgejoIssueLabelTests(IssueLabelContract, unittest.TestCase):
    fixture = ForgejoFixture
    added_labels = [42, 46]

    def requests(self, start: int) -> list[tuple]:
        return [
            (c["method"], c["path"].removeprefix("/api/v1"), c["body"],
             (c["Authorization"] or "").removeprefix("token fake-token-"))
            for c in self.f.read_model()["calls"][start:]
            if c["path"] not in ("/api/v1/version", "/api/v1/user")
        ]

    def test_forgejo_hides_or_disables_a_dependency_list(self) -> None:
        f = self.f
        model = f.read_model()
        # F16 drops a blocked issue the identity may not read from blocks.
        model["foreign_issues"]["MagiLand/other#7"]["unreadable"] = True
        f.save_model(model)
        view = f.cli("issue", "view", "--issue", "2")
        self.assertEqual(view["blocks"],
                         [related(3, "open"), related(4, "open")])
        # A hidden blocker leaves blocked_by unknown rather than shorter.
        model = f.read_model()
        model["issues"]["4"]["blocked_by"].append("hidden")
        f.save_model(model)
        view = f.cli("issue", "view", "--issue", "4")
        self.assertEqual((view["blocks"], view["blocked_by"]), ([], None))
        # F16 refuses only the blocked-by list when dependencies are off.
        f.settings(dependencies_disabled=True)
        view = f.cli("issue", "view", "--issue", "2")
        self.assertEqual(view["blocked_by"], None)
        self.assertEqual(len(view["blocks"]), 2)


if __name__ == "__main__":
    unittest.main()
