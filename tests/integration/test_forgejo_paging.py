"""Forgejo list reads under a server page limit below 50 (#168).

Forgejo caps ``limit`` at ``MAX_RESPONSE_ITEMS``. With a cap of 2 every
page is short, so a read that ended at a short page saw only the first
two rows. Each case also runs without the cap.
"""

import json
import sys
import unittest

from tests.forge_support import ForgejoFixture, finding

CAPS = (2, None)


class ForgejoPagingTests(unittest.TestCase):
    def fixture(self, cap: int | None) -> ForgejoFixture:
        f = ForgejoFixture()
        self.addCleanup(f.close)
        f.initialize()
        if cap is not None:
            f.settings(max_response_items=cap)
        return f

    def test_issue_label_accepts_a_label_beyond_the_first_page(self):
        for cap in CAPS:
            with self.subTest(cap=cap):
                f = self.fixture(cap)
                model = f.read_model()
                model["labels"].append({"id": 43, "name": "bug"})
                f.save_model(model)
                f.cli("issue", "label", "--as", "implementer", "--issue",
                      "1", "--add", "bug")
                self.assertEqual(
                    [label["name"]
                     for label in f.read_model()["issues"]["1"]["labels"]],
                    ["ready-for-agent", "bug"],
                )

    def test_issue_create_sees_an_open_issue_beyond_the_first_page(self):
        for cap in CAPS:
            with self.subTest(cap=cap):
                f = self.fixture(cap)
                model = f.read_model()
                # The fake lists issues newest first: 3 and 2, then 1.
                for number in (2, 3):
                    model["issues"][str(number)] = dict(
                        model["issues"]["1"], id=number, number=number,
                        title=f"Issue {number}", conversation=[],
                    )
                f.save_model(model)
                note = f.write("note.md", "A duplicate of issue 1.\n")
                result = f.cli(
                    "issue", "create", "--as", "implementer", "--title",
                    "Exercise forge protocol", "--body", note,
                    "--from-issue", "2", expected=1,
                )
                self.assertIn("open issue #1 has the same title",
                              result["error"])
                self.assertEqual(sorted(f.read_model()["issues"]),
                                 ["1", "2", "3"])

    def review_pair(self, cap: int | None, verdict: str, threads: list):
        """An approval, an untagged review, then ``verdict`` at one head.

        With a cap of 2 the newest review is alone on the second page.
        """
        f = self.fixture(cap)
        head = f.candidate()
        f.create_pr()
        f.review("approved")
        model = f.read_model()
        model["prs"]["1"]["reviews"].append({
            "id": 900, "user": {"login": "developer"},
            "body": "An untagged comment.", "state": "COMMENT",
            "commit_id": head, "dismissed": False, "official": False,
            "submitted_at": "2026-01-01T00:00:01Z",
            "created_at": "2026-01-01T00:00:01Z",
        })
        f.save_model(model)
        f.review(verdict, threads)
        return f

    def merge(self, f: ForgejoFixture) -> tuple[int, str]:
        result = f.run([
            sys.executable, "-m", "agent_squad", "pr", "merge", "--as",
            "implementer", "--pr", "1", "--json",
        ], cwd=f.repo)
        return result.returncode, result.stdout + result.stderr

    def test_status_reads_the_newest_review_beyond_the_first_page(self):
        for cap in CAPS:
            for verdict, threads in (
                ("changes_requested", [finding()]),
                ("needs_human", []),
            ):
                with self.subTest(cap=cap, verdict=verdict):
                    f = self.review_pair(cap, verdict, threads)
                    state = f.status()
                    self.assertEqual(
                        [r["verdict"] for r in state["reviews"]],
                        ["approved", verdict],
                    )
                    self.assertIs(state["approval"]["approved"], False)
                    self.assertIn(
                        "latest tagged review is not approved",
                        state["approval"]["reasons"],
                    )
                    self.assertNotIn(state["next_action"],
                                     ("approved", "merge"))
                    code, output = self.merge(f)
                    self.assertNotEqual(code, 0, output)
                    self.assertIn("not approved", output)
                    self.assertEqual(f.read_model()["prs"]["1"]["state"],
                                     "open")

    def test_pr_create_sees_a_closed_pr_beyond_the_first_page(self):
        for cap in CAPS:
            with self.subTest(cap=cap):
                f = self.fixture(cap)
                f.candidate()
                f.create_pr()
                model = f.read_model()
                closed = dict(model["prs"]["1"], state="closed")
                # The fake lists pull requests in model order, so the
                # branch's closed PR is alone on the second page.
                model["prs"] = {
                    str(number): dict(
                        closed, id=number, number=number, state="open",
                        head=dict(closed["head"], ref=f"other-{number}"),
                    )
                    for number in (2, 3)
                }
                model["prs"]["1"] = closed
                f.save_model(model)
                result = f.cli(
                    "pr", "create", "--as", "implementer", "--issue", "1",
                    "--task", f.task, "--report", f.report, expected=1,
                )
                self.assertIn("a PR already exists for this branch",
                              result["error"])
                self.assertEqual(sorted(f.read_model()["prs"]),
                                 ["1", "2", "3"])

    def test_a_failed_status_beyond_the_first_page_fails_ci(self):
        for cap in CAPS:
            with self.subTest(cap=cap):
                f = self.fixture(cap)
                head = f.candidate()
                f.create_pr()
                model = f.read_model()
                model.setdefault("checks", {})[head] = [
                    {"id": n, "context": context, "status": status,
                     "description": "", "target_url": "", "creator": None}
                    for n, (context, status) in enumerate((
                        ("Tests / a (pull_request)", "success"),
                        ("Tests / b (pull_request)", "success"),
                        ("Tests / c (pull_request)", "failure"),
                    ))
                ]
                f.save_model(model)
                checks = f.cli("pr", "checks", "--pr", "1")
                self.assertEqual(checks["summary"], "failed")
                self.assertEqual(len(checks["checks"]), 3)
                ci = f.status()["ci"]
                self.assertEqual(ci["summary"], "failed")
                self.assertEqual(
                    json.dumps(ci["checks"], sort_keys=True),
                    json.dumps(checks["checks"], sort_keys=True),
                )


if __name__ == "__main__":
    unittest.main()
