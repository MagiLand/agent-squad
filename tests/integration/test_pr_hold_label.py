"""The configured hold label on every PR that pr create opens (#174)."""

import json
from pathlib import Path
import unittest

from tests.forge_support import ForgeFixture, ForgejoFixture

REPO = "/repos/MagiLand/trial"
HOLD = "no-auto-merge"


class HoldLabelContract:
    """The same contract on each fake forge; subclasses name the fixture."""

    fixture: type = ForgeFixture

    def setUp(self) -> None:
        self.f = self.fixture()
        self.addCleanup(self.f.close)
        self.f.initialize()
        self.f.candidate()
        self.ownership = Path(self.f.git(
            "rev-parse", "--absolute-git-dir", cwd=self.f.worktree,
        )) / "agent-squad-implementation.json"

    def requests(self, start: int) -> list[tuple]:
        """(method, endpoint, body) of every forge request after start."""
        raise NotImplementedError

    def writes(self, start: int) -> list[tuple]:
        return [r for r in self.requests(start) if r[0] != "GET"]

    def configure(self, name: str | None = HOLD) -> None:
        path = self.f.repo / ".agent-squad/config.json"
        config = json.loads(path.read_text())
        config.pop("pr_hold_label", None)
        if name is not None:
            config["pr_hold_label"] = name
        path.write_text(json.dumps(config))

    def add_label(self, key: str = "labels", label_id: int = 50) -> None:
        model = self.f.read_model()
        model.setdefault(key, []).append({"id": label_id, "name": HOLD})
        self.f.save_model(model)

    def create(self, expected: int = 0) -> dict:
        return self.f.cli(
            "pr", "create", "--as", "implementer", "--issue", "1",
            "--report", self.f.report, expected=expected,
        )

    def pr_labels(self, number: int = 1) -> list[str]:
        pr = self.f.read_model()["prs"][str(number)]
        return [label["name"] for label in pr.get("labels", [])]

    def check_created(self, start: int, label_id: int) -> None:
        raise NotImplementedError

    def test_without_the_field_create_reads_and_sends_no_label(self) -> None:
        start = len(self.f.read_model()["calls"])
        created = self.create()
        self.assertIsNone(created["hold_label"])
        self.assertEqual(created["number"], 1)
        self.assertEqual(
            [r for r in self.requests(start) if "labels" in r[1]], [])
        (create,) = self.writes(start)
        self.assertEqual(create[:2], ("POST", f"{REPO}/pulls"))
        self.assertEqual(set(create[2]), {"title", "head", "base", "body"})
        self.assertEqual(self.pr_labels(), [])

    def test_create_puts_the_repository_label_on_the_new_pr(self) -> None:
        self.add_label()
        self.configure()
        start = len(self.f.read_model()["calls"])
        created = self.create()
        self.assertEqual(created["number"], 1)
        self.assertEqual(created["hold_label"],
                         {"name": HOLD, "applied": True})
        self.assertEqual(self.pr_labels(), [HOLD])
        self.check_created(start, 50)
        self.assertEqual(
            json.loads(self.ownership.read_text())["pr"], 1)

    def test_a_missing_label_refuses_before_any_write(self) -> None:
        self.configure()
        start = len(self.f.read_model()["calls"])
        refused = self.create(expected=1)
        self.assertIn(f"pr_hold_label {HOLD} is not a label",
                      refused["error"])
        self.assertIn("nothing was written", refused["error"])
        self.assertEqual(self.writes(start), [])
        self.assertEqual(self.f.read_model()["prs"], {})
        self.assertFalse(self.ownership.exists())

    def test_a_dropped_label_is_repaired_by_a_second_create(self) -> None:
        f = self.f
        self.add_label()
        self.configure()
        f.settings(drop_pr_labels=True)
        refused = self.create(expected=1)
        self.assertIn(
            f"PR #1 created without the hold label {HOLD}", refused["error"])
        self.assertIn(
            "run agent-squad pr create --as implementer --issue 1"
            f" --report {f.report} to add the label", refused["error"])
        self.assertEqual(self.pr_labels(), [])
        # The PR stays the loop's PR.
        self.assertEqual(json.loads(self.ownership.read_text())["pr"], 1)
        f.settings(drop_pr_labels=False)
        model = f.read_model()
        before = dict(model["prs"]["1"])
        model["prs"]["1"]["labels"] = [{"id": 41, "name": "needs-triage"}]
        f.save_model(model)
        start = len(model["calls"])
        repaired = self.create()
        self.assertEqual(repaired["number"], 1)
        self.assertEqual(repaired["hold_label"],
                         {"name": HOLD, "applied": True})
        (write,) = self.writes(start)
        self.assertEqual(write[:2], ("POST", f"{REPO}/issues/1/labels"))
        after = f.read_model()["prs"]["1"]
        self.assertEqual(self.pr_labels(), ["needs-triage", HOLD])
        for key in ("body", "title", "head", "base", "state"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(json.loads(self.ownership.read_text())["pr"], 1)
        # A PR that carries the label keeps today's refusal.
        start = len(f.read_model()["calls"])
        refused = self.create(expected=1)
        self.assertEqual(refused["error"],
                         "error: a PR already exists for this branch")
        self.assertEqual(self.writes(start), [])

    def test_repair_refuses_another_account_or_no_record(self) -> None:
        f = self.f
        self.add_label()
        self.configure()
        f.settings(drop_pr_labels=True)
        self.create(expected=1)
        f.settings(drop_pr_labels=False)
        model = f.read_model()
        author = model["prs"]["1"]["user"]
        record = self.ownership.read_text()
        for case in ("another account", "no record", "closed"):
            with self.subTest(case=case):
                model = f.read_model()
                model["prs"]["1"]["user"] = (
                    {"login": "someone"} if case == "another account"
                    else author)
                model["prs"]["1"]["state"] = (
                    "closed" if case == "closed" else "open")
                f.save_model(model)
                if case == "no record":
                    self.ownership.unlink()
                start = len(model["calls"])
                refused = self.create(expected=1)
                self.assertEqual(
                    refused["error"],
                    "error: a PR already exists for this branch")
                self.assertEqual(self.writes(start), [])
                self.assertEqual(self.pr_labels(), [])
                self.ownership.write_text(record)

    def test_pr_merge_merges_a_pr_that_carries_the_label(self) -> None:
        f = self.f
        self.add_label()
        self.configure()
        self.create()
        f.review("approved")
        merged = f.cli("pr", "merge", "--as", "implementer", "--pr", "1",
                       cwd=f.repo)
        self.assertTrue(merged["merged"])
        self.assertEqual(self.pr_labels(), [HOLD])

    def test_doctor_reports_the_hold_label(self) -> None:
        f = self.f

        def row(expected: int) -> dict:
            # Every other check passes in the fixture.
            result = f.cli("doctor", expected=expected)
            rows = [d for d in result["diagnostics"]
                    if d["check"] == "PR hold label"]
            self.assertEqual(len(rows), 1)
            return rows[0]

        self.configure()
        missing = row(1)
        self.assertEqual(missing["severity"], "fail")
        self.assertIn(f"pr_hold_label {HOLD} is not a label",
                      missing["detail"])
        self.add_label()
        self.assertEqual(row(0), {
            "check": "PR hold label", "severity": "pass", "detail": HOLD,
        })
        self.configure(None)
        result = f.cli("doctor")
        self.assertNotIn("PR hold label",
                         [d["check"] for d in result["diagnostics"]])


class GitHubHoldLabelTests(HoldLabelContract, unittest.TestCase):
    fixture = ForgeFixture

    def requests(self, start: int) -> list[tuple]:
        result = []
        for call in self.f.read_model()["calls"][start:]:
            arguments = call["arguments"]
            if arguments[:1] != ["api"] or arguments[1] == "graphql":
                continue
            method = (arguments[arguments.index("--method") + 1]
                      if "--method" in arguments else "GET")
            result.append((method, arguments[1].split("?")[0],
                           call["body"]))
        return result

    def check_created(self, start: int, label_id: int) -> None:
        # GitHub's create endpoint takes no labels: the label follows at
        # once, before any other request.
        requests = self.requests(start)
        writes = [r for r in requests if r[0] != "GET"]
        self.assertEqual(
            [r[:2] for r in writes],
            [("POST", f"{REPO}/pulls"), ("POST", f"{REPO}/issues/1/labels")])
        self.assertNotIn("labels", writes[0][2])
        self.assertEqual(writes[1][2], {"labels": [HOLD]})
        create = requests.index(writes[0])
        self.assertEqual(requests[create + 1], writes[1])

    def test_init_round_trips_the_hold_label_and_validates_it(self) -> None:
        f = ForgeFixture()
        self.addCleanup(f.close)
        accounts = ("--implementer-account", "developer",
                    "--reviewer-account", "reviewer")
        for name in ("", " no-auto-merge", "no-auto-merge\t"):
            with self.subTest(name=name):
                refused = f.cli("init", *accounts, "--pr-hold-label", name,
                                expected=1)
                self.assertIn("pr_hold_label must be non-empty, without"
                              " leading or trailing whitespace",
                              refused["error"])
                self.assertFalse((f.repo / ".agent-squad/config.json")
                                 .exists())
        created = f.cli("init", *accounts, "--pr-hold-label", HOLD)
        self.assertTrue(created["created"])
        path = f.repo / ".agent-squad/config.json"
        self.assertEqual(json.loads(path.read_text())["pr_hold_label"], HOLD)
        # A rerun validates the stored value and reports a difference.
        again = f.cli("init", *accounts)
        self.assertEqual(again["differences"], {
            "pr_hold_label": {"configured": HOLD, "default": None},
        })
        config = json.loads(path.read_text())
        config["pr_hold_label"] = " " + HOLD
        path.write_text(json.dumps(config))
        refused = f.cli("issue", "view", "--issue", "1", expected=1)
        self.assertIn("pr_hold_label must be non-empty", refused["error"])


class ForgejoHoldLabelTests(HoldLabelContract, unittest.TestCase):
    fixture = ForgejoFixture

    def requests(self, start: int) -> list[tuple]:
        return [
            (c["method"], c["path"].removeprefix("/api/v1").split("?")[0],
             c["body"])
            for c in self.f.read_model()["calls"][start:]
            if c["path"] not in ("/api/v1/version", "/api/v1/user")
        ]

    def check_created(self, start: int, label_id: int) -> None:
        # The label's ID travels in the create request itself.
        (create,) = self.writes(start)
        self.assertEqual(create[:2], ("POST", f"{REPO}/pulls"))
        self.assertEqual(create[2]["labels"], [label_id])

    def test_create_puts_the_organization_label_on_the_new_pr(self) -> None:
        self.add_label("org_labels", 70)
        self.configure()
        start = len(self.f.read_model()["calls"])
        created = self.create()
        self.assertEqual(created["hold_label"],
                         {"name": HOLD, "applied": True})
        self.assertEqual(self.pr_labels(), [HOLD])
        self.check_created(start, 70)
        self.assertIn(("GET", "/orgs/MagiLand/labels", None),
                      self.requests(start))

    def test_a_repository_label_wins_over_an_organization_label(
        self,
    ) -> None:
        self.add_label("org_labels", 70)
        self.add_label("labels", 50)
        self.configure()
        start = len(self.f.read_model()["calls"])
        self.create()
        self.check_created(start, 50)
        self.assertNotIn("/orgs/MagiLand/labels",
                         [r[1] for r in self.requests(start)])

    def test_a_user_owner_has_no_organization_label(self) -> None:
        # The label exists only where a user-owned repository cannot see it.
        self.add_label("org_labels", 70)
        self.configure()
        self.f.settings(owner_is_user=True)
        start = len(self.f.read_model()["calls"])
        refused = self.create(expected=1)
        self.assertIn(f"pr_hold_label {HOLD} is not a label",
                      refused["error"])
        self.assertEqual(self.writes(start), [])
        self.assertFalse(self.ownership.exists())
        result = self.f.cli("doctor", expected=1)
        (row,) = [d for d in result["diagnostics"]
                  if d["check"] == "PR hold label"]
        self.assertEqual(row["severity"], "fail")

    def test_the_repair_adds_an_organization_label_by_id(self) -> None:
        f = self.f
        self.add_label("org_labels", 70)
        self.configure()
        f.settings(drop_pr_labels=True)
        self.create(expected=1)
        f.settings(drop_pr_labels=False)
        start = len(f.read_model()["calls"])
        self.create()
        (write,) = self.writes(start)
        self.assertEqual(write, ("POST", f"{REPO}/issues/1/labels",
                                 {"labels": [70]}))
        self.assertEqual(self.pr_labels(), [HOLD])


if __name__ == "__main__":
    unittest.main()
