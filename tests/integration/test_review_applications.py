from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path, run, run_cli
from tests.integration.test_review_submissions import (
    _prepare_round,
    _write_review,
)


add_src_to_path()

from agent_squad import review_applications  # noqa: E402


def _marker_confirmed_review(
    root: Path,
    *,
    verdict: str = "approved",
):
    prepared = _prepare_round(root)
    review = _write_review(prepared, verdict=verdict)
    failing_environment = dict(prepared.environment)
    failing_environment["FAKE_HERDR_FAIL_PROMPT"] = "1"
    submitted = run_cli(
        prepared.review_worktree,
        "review-submit",
        data_home=prepared.data_home,
        env_overrides=failing_environment,
    )
    if submitted.returncode != 1:
        raise AssertionError(
            "fake lost-notification submission did not fail as expected: "
            f"{submitted.stderr}"
        )
    return prepared, review


class ApprovedReviewLifecycleTests(unittest.TestCase):
    def test_lost_notification_is_discovered_applied_and_completed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)

            status = run_cli(
                prepared.repository,
                "status",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn(
                "Marker-confirmed unapplied result: ready",
                status.stdout,
            )
            self.assertIn(f"Result ID: {review['result_id']}", status.stdout)
            apply_command = (
                "agent-squad apply-review --result-id "
                f"{review['result_id']}"
            )
            self.assertIn(f"Apply command: {apply_command}", status.stdout)
            self.assertIn(f"Next action: {apply_command}", status.stdout)

            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn("Verdict: approved", applied.stdout)
            self.assertIn("Next action: agent-squad complete", applied.stdout)

            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_id = str(state["active_run_id"])
            round_directory = control_root / "runs" / run_id / "rounds/001"
            round_record = json.loads(
                (round_directory / "round.json").read_text(encoding="utf-8")
            )
            approval = json.loads(
                (round_directory / "approval.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["phase"], "approved")
            self.assertEqual(state["approved_head_oid"], review["head_oid"])
            self.assertEqual(state["active_round"]["status"], "applied")
            self.assertEqual(
                state["active_round"]["result_id"],
                review["result_id"],
            )
            self.assertEqual(round_record["status"], "applied")
            self.assertEqual(round_record["verdict"], "approved")
            self.assertEqual(approval["run_id"], run_id)
            self.assertEqual(approval["round"], 1)
            self.assertEqual(approval["result_id"], review["result_id"])
            self.assertEqual(approval["head_oid"], review["head_oid"])
            self.assertEqual(
                approval["reviewer"]["name"],
                prepared.request["reviewer_name"],
            )
            self.assertTrue((round_directory / "bundle").is_dir())
            self.assertEqual(
                (round_directory / "bundle/output/review.json").read_bytes(),
                (round_directory / "review.json").read_bytes(),
            )

            events_path = control_root / "runs" / run_id / "events.jsonl"
            events_before_replay = events_path.read_bytes()
            round_before_replay = (round_directory / "round.json").read_bytes()
            replay = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertIn("already applied", replay.stdout)
            self.assertEqual(events_path.read_bytes(), events_before_replay)
            self.assertEqual(
                (round_directory / "round.json").read_bytes(),
                round_before_replay,
            )

            completed = run_cli(
                prepared.repository,
                "complete",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Active run slot: released", completed.stdout)
            terminal_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            run_record = json.loads(
                (control_root / "runs" / run_id / "run.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsNone(terminal_state["active_run_id"])
            self.assertEqual(terminal_state["phase"], "completed")
            self.assertEqual(terminal_state["terminal_run_id"], run_id)
            self.assertEqual(run_record["phase"], "completed")
            self.assertIsNotNone(run_record["finished_at"])
            self.assertFalse(prepared.review_worktree.exists())
            self.assertTrue((round_directory / "bundle").is_dir())

            events_before_completion_replay = events_path.read_bytes()
            completion_replay = run_cli(
                prepared.repository,
                "complete",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(
                completion_replay.returncode,
                0,
                completion_replay.stderr,
            )
            self.assertIn("already completed", completion_replay.stdout)
            self.assertEqual(
                events_path.read_bytes(),
                events_before_completion_replay,
            )
            next_task = root / "next-task.md"
            next_task.write_text("# Next task\n", encoding="utf-8")
            next_run = run_cli(
                prepared.repository,
                "start",
                "--task",
                str(next_task),
                "--base",
                "main",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(next_run.returncode, 0, next_run.stderr)
            self.assertEqual(
                len(list((control_root / "runs").iterdir())),
                2,
            )

    def test_application_revalidates_authority_without_mutating_on_failure(
        self,
    ) -> None:
        cases = (
            "foreign result ID",
            "rewritten result",
            "changed implementation head",
            "changed implementation branch",
            "hidden tracked review change",
        )
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    prepared, review = _marker_confirmed_review(root)
                    control_root = prepared.repository / ".agent-squad"
                    state_path = control_root / "state.json"
                    state = json.loads(
                        state_path.read_text(encoding="utf-8")
                    )
                    round_path = (
                        control_root
                        / "runs"
                        / str(state["active_run_id"])
                        / "rounds/001/round.json"
                    )
                    state_before = state_path.read_bytes()
                    round_before = round_path.read_bytes()
                    result_id = str(review["result_id"])

                    if case == "foreign result ID":
                        result_id = (
                            "99999999-9999-4999-8999-999999999999"
                        )
                    elif case == "rewritten result":
                        review_path = prepared.bundle / "output/review.json"
                        changed = json.loads(
                            review_path.read_text(encoding="utf-8")
                        )
                        changed["summary"] = "Marker-confirmed rewrite."
                        review_path.write_text(
                            f"{json.dumps(changed, indent=2)}\n",
                            encoding="utf-8",
                        )
                    elif case == "changed implementation head":
                        run(
                            [
                                "git",
                                "-c",
                                "commit.gpgSign=false",
                                "commit",
                                "--allow-empty",
                                "--no-verify",
                                "-m",
                                "test: advance implementation head",
                            ],
                            cwd=prepared.repository,
                        )
                    elif case == "changed implementation branch":
                        run(
                            ["git", "switch", "-c", "other-branch"],
                            cwd=prepared.repository,
                        )
                    else:
                        run(
                            [
                                "git",
                                "update-index",
                                "--assume-unchanged",
                                "feature.txt",
                            ],
                            cwd=prepared.review_worktree,
                        )
                        (prepared.review_worktree / "feature.txt").write_text(
                            "tampered review content\n",
                            encoding="utf-8",
                        )

                    applied = run_cli(
                        prepared.repository,
                        "apply-review",
                        "--result-id",
                        result_id,
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )

                    self.assertEqual(applied.returncode, 1)
                    self.assertEqual(state_path.read_bytes(), state_before)
                    self.assertEqual(round_path.read_bytes(), round_before)
                    self.assertFalse((round_path.parent / "bundle").exists())

    def test_nonapproved_result_remains_ready_for_later_increment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="changes_requested",
            )
            state_path = prepared.repository / ".agent-squad/state.json"
            state_before = state_path.read_bytes()

            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(applied.returncode, 1)
            self.assertIn(
                "not supported by this command version",
                applied.stderr,
            )
            self.assertEqual(state_path.read_bytes(), state_before)

    def test_archive_precedes_state_and_failed_commit_is_retryable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_directory = control_root / "runs" / str(state["active_run_id"])
            round_directory = run_directory / "rounds/001"
            round_path = round_directory / "round.json"
            run_path = run_directory / "run.json"
            state_before = state_path.read_bytes()
            round_before = round_path.read_bytes()
            run_before = run_path.read_bytes()
            real_atomic_write = review_applications.atomic_write
            observed_archive_before_state = False

            def fail_state_commit(path, content, *, mode):
                nonlocal observed_archive_before_state
                if (
                    path.resolve() == state_path.resolve()
                    and b'"phase": "approved"' in content
                ):
                    observed_archive_before_state = (
                        (
                            round_directory / "bundle/output/review.json"
                        ).is_file()
                        and (round_directory / "approval.json").is_file()
                    )
                    raise OSError("injected state commit failure")
                real_atomic_write(path, content, mode=mode)

            with mock.patch.object(
                review_applications,
                "atomic_write",
                side_effect=fail_state_commit,
            ):
                with self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "injected state commit failure",
                ):
                    review_applications.apply_review(
                        prepared.repository,
                        result_id=str(review["result_id"]),
                    )

            self.assertTrue(observed_archive_before_state)
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(round_path.read_bytes(), round_before)
            self.assertEqual(run_path.read_bytes(), run_before)

            retried = review_applications.apply_review(
                prepared.repository,
                result_id=str(review["result_id"]),
            )
            self.assertFalse(retried.replayed)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["phase"],
                "approved",
            )

    def test_completion_requires_exact_head_and_clean_worktree(self) -> None:
        cases = (
            "new commit",
            "tracked edit",
            "hidden tracked edit",
            "unexpected untracked file",
        )
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    prepared, review = _marker_confirmed_review(root)
                    applied = run_cli(
                        prepared.repository,
                        "apply-review",
                        "--result-id",
                        str(review["result_id"]),
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )
                    self.assertEqual(applied.returncode, 0, applied.stderr)
                    state_path = (
                        prepared.repository / ".agent-squad/state.json"
                    )
                    state_before = state_path.read_bytes()

                    if case == "new commit":
                        run(
                            [
                                "git",
                                "-c",
                                "commit.gpgSign=false",
                                "commit",
                                "--allow-empty",
                                "--no-verify",
                                "-m",
                                "test: advance approved head",
                            ],
                            cwd=prepared.repository,
                        )
                    elif case in {"tracked edit", "hidden tracked edit"}:
                        if case == "hidden tracked edit":
                            run(
                                [
                                    "git",
                                    "update-index",
                                    "--assume-unchanged",
                                    "feature.txt",
                                ],
                                cwd=prepared.repository,
                            )
                        (prepared.repository / "feature.txt").write_text(
                            "uncommitted tracked change\n",
                            encoding="utf-8",
                        )
                    else:
                        (prepared.repository / "notes.tmp").write_text(
                            "untracked\n",
                            encoding="utf-8",
                        )

                    completed = run_cli(
                        prepared.repository,
                        "complete",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )

                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(state_path.read_bytes(), state_before)
                    self.assertTrue(prepared.review_worktree.is_dir())

    def test_cleanup_failure_releases_slot_and_preserves_review_worktree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            (prepared.review_worktree / "late-untracked.txt").write_text(
                "preserve me\n",
                encoding="utf-8",
            )

            completed = run_cli(
                prepared.repository,
                "complete",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("retained review worktree", completed.stderr)
            state = json.loads(
                (prepared.repository / ".agent-squad/state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsNone(state["active_run_id"])
            self.assertTrue(prepared.review_worktree.is_dir())
            self.assertTrue(
                (prepared.review_worktree / "late-untracked.txt").is_file()
            )

    def test_completion_accepts_configured_output_without_deleting_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(
                root,
                allowed_generated_paths=("build/",),
            )
            review_output = prepared.review_worktree / "build/review.bin"
            review_output.parent.mkdir()
            review_output.write_bytes(b"review output\n")
            review = _write_review(prepared, verdict="approved")
            failing_environment = dict(prepared.environment)
            failing_environment["FAKE_HERDR_FAIL_PROMPT"] = "1"
            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=failing_environment,
            )
            self.assertEqual(submitted.returncode, 1)
            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            implementation_output = prepared.repository / "build/local.bin"
            implementation_output.parent.mkdir()
            implementation_output.write_bytes(b"local output\n")

            completed = run_cli(
                prepared.repository,
                "complete",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(implementation_output.is_file())
            self.assertFalse(prepared.review_worktree.exists())


if __name__ == "__main__":
    unittest.main()
