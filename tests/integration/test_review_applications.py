from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path, run, run_cli, seed_git_repository
from tests.integration.test_review_submissions import (
    _prepare_round,
    _write_review,
)


add_src_to_path()

from agent_squad import review_applications, runs  # noqa: E402


def _marker_confirmed_review(
    root: Path,
    *,
    verdict: str = "approved",
):
    prepared = _prepare_round(root)
    return prepared, _submit_with_lost_notification(
        prepared,
        verdict=verdict,
    )


def _submit_with_lost_notification(prepared, *, verdict: str = "approved"):
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
    return review


class ApprovedReviewLifecycleTests(unittest.TestCase):
    def test_commands_report_basic_application_and_completion_guards(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            seed_git_repository(repository)
            data_home = root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            cases = (
                (
                    ("complete",),
                    "there is no active run to complete",
                ),
                (
                    ("apply-review",),
                    "there is no active run with a review result to apply",
                ),
                (
                    ("apply-review", "--result-id", "not-a-uuid"),
                    "--result-id must be a canonical UUID",
                ),
            )
            for arguments, message in cases:
                with self.subTest(command=arguments):
                    result = run_cli(
                        repository,
                        *arguments,
                        data_home=data_home,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(message, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_invalid_marker_evidence_remains_visible_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            (prepared.review_worktree / "reviewer-notes.md").write_text(
                "scratch notes\n",
                encoding="utf-8",
            )

            status = run_cli(
                prepared.repository,
                "status",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Active run:", status.stdout)
            self.assertIn("Phase: reviewing", status.stdout)
            self.assertIn(
                "Marker-confirmed unapplied result: present but invalid",
                status.stdout,
            )
            self.assertIn(
                "review worktree contains tracked changes or unexpected",
                status.stdout,
            )
            self.assertIn(
                "Next action: inspect the review worktree; its "
                "marker-confirmed result did not revalidate",
                status.stdout,
            )

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
                "review worktree contains tracked changes or unexpected",
                applied.stderr,
            )

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

    def test_approved_archive_and_authority_tampering_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            review_applications.apply_review(
                prepared.repository,
                result_id=str(review["result_id"]),
            )
            control_root = prepared.repository / ".agent-squad"
            state = json.loads(
                (control_root / "state.json").read_text(encoding="utf-8")
            )
            round_directory = (
                control_root
                / "runs"
                / str(state["active_run_id"])
                / "rounds/001"
            )
            round_path = round_directory / "round.json"
            approval_path = round_directory / "approval.json"

            def assert_json_rejected(
                path: Path,
                mutate,
                message: str,
            ) -> None:
                original = path.read_bytes()
                original_mode = stat.S_IMODE(path.stat().st_mode)
                value = json.loads(original.decode("utf-8"))
                mutate(value)
                path.chmod(0o600)
                path.write_text(
                    f"{json.dumps(value, indent=2)}\n",
                    encoding="utf-8",
                )
                try:
                    with self.assertRaisesRegex(
                        runs.RunStateError,
                        message,
                    ):
                        runs.inspect_status(prepared.repository)
                finally:
                    path.write_bytes(original)
                    path.chmod(original_mode)
                self.assertEqual(
                    runs.inspect_status(prepared.repository).active_run.phase,
                    runs.RunPhase.APPROVED,
                )

            def assert_approval_rejected(
                content: bytes,
                message: str,
            ) -> None:
                original_approval = approval_path.read_bytes()
                original_round = round_path.read_bytes()
                round_value = json.loads(original_round.decode("utf-8"))
                round_value["artifacts"]["approval"]["sha256"] = (
                    hashlib.sha256(content).hexdigest()
                )
                approval_path.chmod(0o600)
                approval_path.write_bytes(content)
                round_path.write_text(
                    f"{json.dumps(round_value, indent=2)}\n",
                    encoding="utf-8",
                )
                try:
                    with self.assertRaisesRegex(
                        runs.RunStateError,
                        message,
                    ):
                        runs.inspect_status(prepared.repository)
                finally:
                    approval_path.write_bytes(original_approval)
                    approval_path.chmod(0o400)
                    round_path.write_bytes(original_round)

            round_cases = (
                (
                    "wrong status",
                    lambda value: value.update(status="reviewing"),
                    "cannot record a result before classification",
                ),
                (
                    "wrong verdict",
                    lambda value: value.update(verdict="needs_human"),
                    "approval artifact must exist exactly",
                ),
                (
                    "missing approval artifact",
                    lambda value: value["artifacts"].update(approval=None),
                    "approval artifact must exist exactly",
                ),
                (
                    "wrong result artifact path",
                    lambda value: value["artifacts"][
                        "review_result"
                    ].update(path="different-review.json"),
                    "review result path must be review.json",
                ),
                (
                    "bundle path outside archive",
                    lambda value: value["artifacts"]["bundle_archive"][0]
                    .update(path="outside/request.json"),
                    "manifest paths must start with bundle/",
                ),
            )
            for case, mutate, message in round_cases:
                with self.subTest(case=case):
                    assert_json_rejected(round_path, mutate, message)

            with self.subTest(case="approval identity mismatch"):
                approval_value = json.loads(
                    approval_path.read_text(encoding="utf-8")
                )
                approval_value["result_id"] = (
                    "99999999-9999-4999-8999-999999999999"
                )
                assert_approval_rejected(
                    f"{json.dumps(approval_value, indent=2)}\n".encode(
                        "utf-8"
                    ),
                    "approval record result ID does not match",
                )

            with self.subTest(case="invalid approval JSON"):
                assert_approval_rejected(
                    b"{\n",
                    "approval record contains invalid JSON",
                )

            review_path = round_directory / "review.json"
            with self.subTest(case="tampered convenience result"):
                original = review_path.read_bytes()
                review_path.chmod(0o600)
                review_path.write_bytes(original + b"tampered\n")
                try:
                    with self.assertRaisesRegex(
                        runs.RunStateError,
                        "digest does not match",
                    ):
                        runs.inspect_status(prepared.repository)
                finally:
                    review_path.write_bytes(original)
                    review_path.chmod(0o400)

            archive_root = round_directory / "bundle"
            archived_result = archive_root / "output/review.json"
            with self.subTest(case="removed archived result"):
                original = archived_result.read_bytes()
                archived_result.parent.chmod(0o700)
                archived_result.unlink()
                try:
                    with self.assertRaisesRegex(
                        runs.RunStateError,
                        "does not resolve to a safe captured file",
                    ):
                        runs.inspect_status(prepared.repository)
                finally:
                    archived_result.write_bytes(original)
                    archived_result.chmod(0o400)
                    archived_result.parent.chmod(0o500)

            with self.subTest(case="extra archive file"):
                archive_root.chmod(0o700)
                extra = archive_root / "unexpected.txt"
                extra.write_text("unexpected\n", encoding="utf-8")
                archive_root.chmod(0o500)
                try:
                    with self.assertRaisesRegex(
                        runs.RunStateError,
                        "does not match its authoritative manifest",
                    ):
                        runs.inspect_status(prepared.repository)
                finally:
                    archive_root.chmod(0o700)
                    extra.unlink()
                    archive_root.chmod(0o500)

            with self.subTest(case="symlinked archive directory"):
                archive_root.chmod(0o700)
                linked = archive_root / "linked"
                linked.symlink_to(root, target_is_directory=True)
                archive_root.chmod(0o500)
                try:
                    with self.assertRaisesRegex(
                        runs.RunStateError,
                        "directory must not be a symlink",
                    ):
                        runs.inspect_status(prepared.repository)
                finally:
                    archive_root.chmod(0o700)
                    linked.unlink()
                    archive_root.chmod(0o500)

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
            approval_path = round_directory / "approval.json"
            first_approval = approval_path.read_bytes()

            with mock.patch.object(
                review_applications,
                "utc_timestamp",
                return_value="2099-01-01T00:00:00Z",
            ):
                retried = review_applications.apply_review(
                    prepared.repository,
                    result_id=str(review["result_id"]),
                )
            self.assertFalse(retried.replayed)
            self.assertEqual(approval_path.read_bytes(), first_approval)
            self.assertNotIn(b"2099-01-01T00:00:00Z", first_approval)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["phase"],
                "approved",
            )

    def test_interruptions_rollback_uncommitted_state_transitions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_directory = control_root / "runs" / str(
                state["active_run_id"]
            )
            round_path = run_directory / "rounds/001/round.json"
            run_path = run_directory / "run.json"
            real_atomic_write = review_applications.atomic_write

            def interrupt_approval(path, content, *, mode):
                if (
                    path.resolve() == state_path.resolve()
                    and b'"phase": "approved"' in content
                ):
                    raise KeyboardInterrupt("approval interrupted")
                real_atomic_write(path, content, mode=mode)

            originals = (
                state_path.read_bytes(),
                round_path.read_bytes(),
                run_path.read_bytes(),
            )
            with (
                mock.patch.object(
                    review_applications,
                    "atomic_write",
                    side_effect=interrupt_approval,
                ),
                self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "approval interrupted",
                ),
            ):
                review_applications.apply_review(
                    prepared.repository,
                    result_id=str(review["result_id"]),
                )
            self.assertEqual(
                (
                    state_path.read_bytes(),
                    round_path.read_bytes(),
                    run_path.read_bytes(),
                ),
                originals,
            )

            applied = review_applications.apply_review(
                prepared.repository,
                result_id=str(review["result_id"]),
            )
            self.assertFalse(applied.replayed)
            approved_state = state_path.read_bytes()
            approved_run = run_path.read_bytes()

            def interrupt_completion(path, content, *, mode):
                if (
                    path.resolve() == state_path.resolve()
                    and b'"phase": "completed"' in content
                ):
                    raise KeyboardInterrupt("completion interrupted")
                real_atomic_write(path, content, mode=mode)

            with (
                mock.patch.object(
                    review_applications,
                    "atomic_write",
                    side_effect=interrupt_completion,
                ),
                self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "completion interrupted",
                ),
            ):
                review_applications.complete_run(prepared.repository)
            self.assertEqual(state_path.read_bytes(), approved_state)
            self.assertEqual(run_path.read_bytes(), approved_run)

            completed = review_applications.complete_run(
                prepared.repository
            )
            self.assertFalse(completed.already_completed)

    def test_interrupt_after_state_commit_preserves_authority_for_replay(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            state_path = prepared.repository / ".agent-squad/state.json"
            real_atomic_write = review_applications.atomic_write

            def commit_then_interrupt(path, content, *, mode):
                real_atomic_write(path, content, mode=mode)
                if (
                    path.resolve() == state_path.resolve()
                    and b'"phase": "approved"' in content
                ):
                    raise KeyboardInterrupt("post-commit interruption")

            with (
                mock.patch.object(
                    review_applications,
                    "atomic_write",
                    side_effect=commit_then_interrupt,
                ),
                self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "post-commit interruption",
                ),
            ):
                review_applications.apply_review(
                    prepared.repository,
                    result_id=str(review["result_id"]),
                )

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "approved")
            replay = review_applications.apply_review(
                prepared.repository,
                result_id=str(review["result_id"]),
            )
            self.assertTrue(replay.replayed)

    def test_completed_replay_validates_terminal_state_and_repairs_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            review_applications.apply_review(
                prepared.repository,
                result_id=str(review["result_id"]),
            )
            completed = review_applications.complete_run(
                prepared.repository
            )
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_directory = control_root / "runs" / completed.run_id
            events_path = run_directory / "events.jsonl"
            events = [
                json.loads(line)
                for line in events_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            events_path.write_text(
                "".join(
                    f"{json.dumps(event, separators=(',', ':'))}\n"
                    for event in events
                    if event["event"] != "run_completed"
                ),
                encoding="utf-8",
            )

            replay = review_applications.complete_run(prepared.repository)

            self.assertTrue(replay.already_completed)
            repaired = [
                json.loads(line)
                for line in events_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                sum(event["event"] == "run_completed" for event in repaired),
                1,
            )

            original_state = state_path.read_bytes()
            cases = (
                (
                    "missing approved round",
                    lambda value: value.update(current_round=0),
                    "has no valid approved round",
                ),
                (
                    "changed approved head",
                    lambda value: value.update(approved_head_oid="c" * 40),
                    "retain one exact approved head",
                ),
                (
                    "changed repository identity",
                    lambda value: value.update(repository_id="other"),
                    "repository ID does not match run metadata",
                ),
            )
            for case, mutate, message in cases:
                with self.subTest(case=case):
                    changed = copy.deepcopy(state)
                    mutate(changed)
                    state_path.write_text(
                        f"{json.dumps(changed, indent=2)}\n",
                        encoding="utf-8",
                    )
                    try:
                        with self.assertRaisesRegex(
                            review_applications.ReviewApplicationError,
                            message,
                        ):
                            review_applications.complete_run(
                                prepared.repository
                            )
                    finally:
                        state_path.write_bytes(original_state)

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
            self.assertIn(
                str(prepared.review_worktree),
                completed.stderr,
            )
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
            review = _submit_with_lost_notification(prepared)
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
