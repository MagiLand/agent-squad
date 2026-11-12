"""Decision kinds, recommendations, and the recorded hold release (#147).

Every check runs against both fake forges with real Git and the CLI.
"""

import unittest

from tests.forge_support import (
    ForgeFixture, ForgejoFixture, TASK, finding, is_write,
)
from agent_squad.conventions import MERGE_INSTRUCTION

GENERAL = ("--finding", "none")
HOLD = {"merge-hold": "Item 3: changes merge authority."}
QUOTE = "> Developer: go ahead."


def record(kind: str, recommendation: str) -> str:
    return f"Decision record: kind={kind} recommendation={recommendation}"


class DecisionRecordTests(unittest.TestCase):
    def each_fake(self, check, *, issue_task: bool = False) -> None:
        for fixture in (ForgeFixture, ForgejoFixture):
            with self.subTest(forge=fixture.__name__):
                f = fixture()
                self.addCleanup(f.close)
                f.initialize()
                f.candidate()
                f.create_pr(issue_task=issue_task)
                check(f)

    def post(
        self, f: ForgeFixture, *options: str, body: str = QUOTE,
        expected: int = 0,
    ) -> dict:
        return f.cli(
            "decision", "post", "--as", "implementer", "--pr", "1",
            *options, "--body", f.write("decision.md", body),
            expected=expected,
        )

    def wire(self, f: ForgeFixture) -> str:
        """The latest conversation comment as the forge stores it."""
        return f.read_model()["prs"]["1"]["conversation"][-1]["body"]

    def stop(self, f: ForgeFixture) -> None:
        f.cli(
            "stop", "post", "--as", "implementer", "--pr", "1",
            "--head", f.git("rev-parse", "HEAD", cwd=f.worktree),
            "--reason", "judgement",
            "--body", f.write("stop.md", "Scripted stop."),
        )

    def refused(
        self, f: ForgeFixture, options: tuple, message: str, code: int,
        body: str = QUOTE,
    ) -> None:
        """A refusal that sends no write request to the forge."""
        before = f.read_model()
        error = self.post(f, *options, body=body, expected=code)
        self.assertIn(message, error["error"])
        after = f.read_model()
        self.assertEqual(
            [c for c in after["calls"][len(before["calls"]):]
             if is_write(c)], [])
        self.assertEqual(after["prs"], before["prs"])

    def merge(self, f: ForgeFixture, *options: str, expected: int) -> dict:
        return f.cli("pr", "merge", "--as", "implementer", "--pr", "1",
                     *options, cwd=f.repo, expected=expected)

    def test_each_kind_writes_its_record_line_and_status_reads_it(
        self,
    ) -> None:
        self.each_fake(self.check_each_kind)

    def check_each_kind(self, f: ForgeFixture) -> None:
        f.review("changes_requested", [finding()], sections=HOLD)
        header = "AGENT_SQUAD/0.5.0 DECISION finding="
        expected = []
        for options, line, kind, recommendation in (
            (("--kind", "task-approval"), "none", "task-approval",
             "followed"),
            (("--kind", "hold-release"), "none", "hold-release", "followed"),
            (("--kind", "answer"), "none", "answer", "changed"),
            (("--kind", "budget", "--budget", "5"), "none budget=5",
             "budget", "none"),
        ):
            with self.subTest(kind=kind):
                self.post(f, *GENERAL, *options,
                          "--recommendation", recommendation)
                self.assertEqual(self.wire(f), (
                    f"{header}{line}\n\n{record(kind, recommendation)}"
                    f"\n\n{QUOTE}"
                ))
                expected.append((kind, recommendation))
        self.post(f, "--finding", "REV-1", "--recommendation", "followed")
        self.assertEqual(self.wire(f), (
            f"{header}REV-1\n\n{record('finding', 'followed')}\n\n{QUOTE}"))
        task = TASK.replace("Exercise", "Amend")
        self.post(f, *GENERAL, "--kind", "task-amendment",
                  "--task", f.write("amended.md", task),
                  "--recommendation", "changed")
        self.assertEqual(self.wire(f), (
            f"{header}none\n\n{record('task-amendment', 'changed')}"
            f"\n\n{QUOTE}\n\n{task.strip()}"
        ))
        self.stop(f)
        self.post(f, *GENERAL, "--kind", "continuation", "--budget", "6",
                  "--recommendation", "followed")
        self.assertEqual(self.wire(f), (
            f"{header}none budget=6\n\n"
            f"{record('continuation', 'followed')}\n\n{QUOTE}"
        ))
        self.post(f, *GENERAL, "--merge-instruction", "record",
                  body="> Start #1.")
        # A standing instruction keeps the text it had before #147.
        self.assertEqual(self.wire(f), (
            f"{header}none\n\n{MERGE_INSTRUCTION}\n\n> Start #1."))
        expected += [("finding", "followed"), ("task-amendment", "changed"),
                     ("continuation", "followed"),
                     ("merge-instruction", None)]
        state = f.status()
        self.assertEqual(
            [(d["kind"], d["recommendation"]) for d in state["decisions"]],
            expected)
        self.assertEqual(
            [d["kind"] for d in state["general_decisions"]],
            ["answer", "budget", "task-amendment", "continuation"])
        self.assertEqual(state["diagnostics"], [])
        self.assertEqual(state["budget"]["effective"], 6)
        self.assertIn("Amend", state["effective_task"])

    def test_each_refusal_sends_no_write_request(self) -> None:
        self.each_fake(self.check_refusals, issue_task=True)

    def check_refusals(self, f: ForgeFixture) -> None:
        f.review("changes_requested", [finding()])
        task = ("--task", f.write("amended.md", TASK))
        follow = ("--recommendation", "followed")
        for options, message, code in (
            ((*GENERAL, "--kind", "task-approval", *follow),
             "this PR's Task is the unreworded copy of its issue", 4),
            ((*GENERAL, "--kind", "task-amendment", *follow),
             "kind task-amendment requires the amended Task (--task)", 1),
            ((*GENERAL, "--kind", "answer", *task, *follow),
             "a Task amendment is kind task-amendment, not answer", 1),
            ((*GENERAL, "--kind", "task-approval", *task, *follow),
             "a Task amendment is kind task-amendment, not task-approval",
             1),
            ((*GENERAL, "--kind", "hold-release", *task, *follow),
             "a Task amendment is kind task-amendment, not hold-release", 1),
            ((*GENERAL, "--kind", "continuation", *follow),
             "kind continuation requires a stopped PR", 4),
            ((*GENERAL, "--kind", "budget", *follow),
             "kind budget requires budget= (--budget)", 1),
            ((*GENERAL, "--kind", "hold-release", *follow),
             "kind hold-release requires a merge hold in the latest tagged"
             " review", 4),
            ((*GENERAL, "--kind", "hold-release", "--budget", "5", *follow),
             "kind hold-release is a record and carries no budget=", 1),
            ((*GENERAL, "--kind", "task-approval", "--budget", "5", *follow),
             "kind task-approval is a record and carries no budget=", 1),
            (("--finding", "REV-1", "--kind", "answer", *follow),
             "a decision on REV-1 is kind finding, not answer", 1),
            ((*GENERAL, "--merge-instruction", "record", "--kind", "answer"),
             "--kind and --recommendation do not apply", 1),
            ((*GENERAL, "--merge-instruction", "withdraw", *follow),
             "--kind and --recommendation do not apply", 1),
            ((*GENERAL, *follow), "a general decision requires --kind", 1),
            ((*GENERAL, "--kind", "answer"),
             "decision post requires --recommendation", 1),
            (("--finding", "REV-1"),
             "decision post requires --recommendation", 1),
        ):
            with self.subTest(options=options):
                self.refused(f, options, message, code)
        for options, body in (
            ((*GENERAL, "--kind", "answer", *follow),
             record("answer", "followed") + "\n\nTyped by hand."),
            (("--finding", "REV-1", *follow), "Decision record: approved"),
            ((*GENERAL, "--merge-instruction", "record"),
             "**Decision record:** kind=answer"),
        ):
            with self.subTest(body=body):
                self.refused(f, options, "must not open with a Decision"
                             " record line", 1, body=body)
        self.stop(f)
        for options, kind in (
            ((*GENERAL, "--kind", "budget", "--budget", "5", *follow),
             "budget"),
            ((*GENERAL, "--kind", "answer", *follow), "answer"),
        ):
            with self.subTest(stopped=kind):
                self.refused(f, options, "the PR is stopped: the decision"
                             f" that continues it is kind continuation, not"
                             f" {kind}", 4)

    def test_decisions_without_an_accepted_line_are_unknown(self) -> None:
        self.each_fake(self.check_unknown)

    def check_unknown(self, f: ForgeFixture) -> None:
        for _ in range(3):
            self.post(f, *GENERAL, "--kind", "answer",
                      "--recommendation", "followed")
        model = f.read_model()
        rows = model["prs"]["1"]["conversation"]
        header = "AGENT_SQUAD/0.5.0 DECISION finding=none"
        rows[0]["body"] = header + "\n\nWritten by hand before #147."
        rows[1]["body"] = (header + "\n\n" + record("approach", "followed")
                           + "\n\nTyped by hand.")
        rows[2]["body"] = (header + " budget=4\n\n"
                           + record("hold-release", "followed")
                           + "\n\nA release that extends the budget.")
        f.save_model(model)
        state = f.status()
        self.assertEqual(
            [(d["kind"], d["recommendation"]) for d in state["decisions"]],
            [("unknown", "unknown")] * 3)
        self.assertEqual(
            [(d["kind"], d["id"]) for d in state["diagnostics"]],
            [("malformed_decision_record", rows[1]["id"]),
             ("malformed_decision_record", rows[2]["id"])])
        self.assertIn("malformed decision record line",
                      state["diagnostics"][0]["detail"])
        self.assertIn("carries no budget=", state["diagnostics"][1]["detail"])
        # Each stays in force as an ordinary general decision.
        self.assertEqual(len(state["general_decisions"]), 3)
        self.assertEqual(state["budget"]["effective"], 4)

    def test_only_a_continuation_lifts_a_stop(self) -> None:
        self.each_fake(self.check_stop)

    def check_stop(self, f: ForgeFixture) -> None:
        f.review("changes_requested", [finding()], sections=HOLD)
        self.stop(f)
        for kind in ("hold-release", "task-approval"):
            with self.subTest(kind=kind):
                self.post(f, *GENERAL, "--kind", kind,
                          "--recommendation", "followed")
                state = f.status()
                self.assertTrue(state["gates"]["stopped"])
                self.assertEqual(state["next_action"], "stopped")
                self.assertEqual(state["general_decisions"], [])
        self.post(f, *GENERAL, "--kind", "continuation",
                  "--recommendation", "changed")
        state = f.status()
        self.assertFalse(state["gates"]["stopped"])
        self.assertNotEqual(state["next_action"], "stopped")
        self.assertEqual([d["kind"] for d in state["general_decisions"]],
                         ["continuation"])

    def test_accepting_a_hold_requires_a_newer_release(self) -> None:
        self.each_fake(self.check_hold)

    def check_hold(self, f: ForgeFixture) -> None:
        first = f.review("approved", sections=HOLD)
        state = f.status()
        self.assertEqual(state["next_action"], "approved")
        self.assertEqual(
            (state["merge_hold"]["released"], state["merge_hold"]["release"]),
            (False, None))
        for review in (first, None):
            if review is None:
                # A later review that carries a hold needs its own release.
                review = f.review("approved", sections=HOLD)
                hold = f.status()["merge_hold"]
                self.assertEqual(hold["review_id"], review["review_id"])
                self.assertFalse(hold["released"])
            with self.subTest(review=review["review_id"]):
                before = f.read_model()
                refused = self.merge(f, "--accept-merge-hold", expected=4)
                self.assertIn(
                    f'merge hold in review {review["review_id"]}',
                    refused["error"])
                self.assertIn("requires a hold-release decision newer than"
                              " that review", refused["error"])
                after = f.read_model()
                self.assertEqual(
                    [c for c in after["calls"][len(before["calls"]):]
                     if is_write(c)], [])
                self.assertFalse(after["prs"]["1"]["merged"])
                self.assertTrue(f.worktree.exists())
                posted = self.post(
                    f, *GENERAL, "--kind", "hold-release",
                    "--recommendation", "followed",
                    body="> Hold released, go ahead and merge.")
                state = f.status()
                self.assertTrue(state["merge_hold"]["released"])
                self.assertEqual(state["merge_hold"]["release"]["id"],
                                 posted["decision"]["id"])
                # The release changes neither the next action nor the flag.
                self.assertEqual(state["next_action"], "approved")
                self.assertIn("explicit --accept-merge-hold required",
                              self.merge(f, expected=4)["error"])
        merged = self.merge(f, "--accept-merge-hold", expected=0)
        self.assertTrue(merged["merged"])
        self.assertEqual(merged["integration"], "verified by ancestry")


if __name__ == "__main__":
    unittest.main()
