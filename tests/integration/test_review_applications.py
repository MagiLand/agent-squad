from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests._support import add_src_to_path, run, run_cli, seed_git_repository
from tests.integration.test_review_submissions import (
    _prepare_round,
    _write_review,
)


add_src_to_path()

from agent_squad import review_applications, runs, submissions  # noqa: E402


def _marker_confirmed_review(
    root: Path,
    *,
    verdict: str = "approved",
    review_limit: int | None = None,
):
    prepared = _prepare_round(root, review_limit=review_limit)
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


def _write_fixed_response(
    path: Path,
    prepared,
    review: dict[str, object],
) -> dict[str, object]:
    response = {
        "schema_version": 1,
        "created_at": "2026-08-26T12:30:00Z",
        "response_id": "44444444-4444-4444-8444-444444444444",
        "supersedes_response_id": None,
        "resolution_ids": [],
        "run_id": prepared.request["run_id"],
        "review_round": prepared.request["round"],
        "review_result_id": review["result_id"],
        "reviewed_head_oid": review["head_oid"],
        "responses": [
            {
                "finding_id": "REV-001",
                "disposition": "fixed",
                "rationale": "The placeholder now contains the full value.",
                "changed_files": ["feature.txt"],
                "evidence": [],
                "verification": "python -m unittest discover -s tests",
            }
        ],
    }
    path.write_text(
        f"{json.dumps(response, indent=2)}\n",
        encoding="utf-8",
    )
    return response


def _write_rejected_response(
    path: Path,
    prepared,
    review: dict[str, object],
) -> dict[str, object]:
    response = {
        "schema_version": 1,
        "created_at": "2026-08-26T12:30:00Z",
        "response_id": "55555555-5555-4555-8555-555555555555",
        "supersedes_response_id": None,
        "resolution_ids": [],
        "run_id": prepared.request["run_id"],
        "review_round": prepared.request["round"],
        "review_result_id": review["result_id"],
        "reviewed_head_oid": review["head_oid"],
        "responses": [
            {
                "finding_id": "REV-001",
                "disposition": "rejected",
                "rationale": (
                    "The reviewed revision already contains the complete "
                    "value."
                ),
                "changed_files": [],
                "evidence": [
                    "feature.txt:1 contains the complete candidate value"
                ],
                "verification": "",
            }
        ],
    }
    path.write_text(
        f"{json.dumps(response, indent=2)}\n",
        encoding="utf-8",
    )
    return response


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

            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            started = run_cli(
                repository,
                "start",
                "--task",
                str(task),
                "--base",
                "main",
                data_home=data_home,
            )
            self.assertEqual(started.returncode, 0, started.stderr)

            phase_cases = (
                (
                    ("apply-review",),
                    "apply-review requires an active reviewing round",
                ),
                (
                    ("complete",),
                    "complete requires an approved run",
                ),
            )
            for arguments, message in phase_cases:
                with self.subTest(command=arguments, phase="implementing"):
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
            archived_request = archive_root / "input/request.json"
            with self.subTest(case="archive core digest diverges"):
                original_request = archived_request.read_bytes()
                original_round = round_path.read_bytes()
                changed_request = original_request + b" "
                round_value = json.loads(original_round.decode("utf-8"))
                manifest = round_value["artifacts"]["bundle_archive"]
                request_entry = next(
                    item
                    for item in manifest
                    if item["path"] == "bundle/input/request.json"
                )
                request_entry["sha256"] = hashlib.sha256(
                    changed_request
                ).hexdigest()
                archived_request.chmod(0o600)
                archived_request.write_bytes(changed_request)
                round_path.write_text(
                    f"{json.dumps(round_value, indent=2)}\n",
                    encoding="utf-8",
                )
                try:
                    with self.assertRaisesRegex(
                        runs.RunStateError,
                        "archive does not preserve bundle/input/request.json",
                    ):
                        runs.inspect_status(prepared.repository)
                finally:
                    archived_request.write_bytes(original_request)
                    archived_request.chmod(0o400)
                    round_path.write_bytes(original_round)

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

    def test_changes_requested_is_applied_once_and_returns_to_implementation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="changes_requested",
            )
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"

            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn("Verdict: changes_requested", applied.stdout)
            self.assertIn(
                "Next action: agent-squad submit --report <report.md> "
                "--response <response.json> --mode "
                "<new_revision|reconsideration> after addressing every "
                "blocking finding",
                applied.stdout,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_id = str(state["active_run_id"])
            round_directory = control_root / "runs" / run_id / "rounds/001"
            round_path = round_directory / "round.json"
            round_record = json.loads(round_path.read_text(encoding="utf-8"))
            events_path = control_root / "runs" / run_id / "events.jsonl"

            self.assertEqual(state["phase"], "implementing")
            self.assertEqual(state["approved_head_oid"], None)
            self.assertEqual(state["current_head_oid"], review["head_oid"])
            self.assertEqual(
                state["review_budget"]["completed_change_reviews"],
                1,
            )
            self.assertEqual(state["active_round"]["status"], "applied")
            self.assertEqual(
                state["active_round"]["result_id"],
                review["result_id"],
            )
            self.assertEqual(round_record["status"], "applied")
            self.assertEqual(round_record["verdict"], "changes_requested")
            self.assertIsNone(round_record["artifacts"]["approval"])
            self.assertTrue((round_directory / "review.json").is_file())
            self.assertTrue((round_directory / "bundle").is_dir())

            status = run_cli(
                prepared.repository,
                "status",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn(
                "Next action: agent-squad submit --report <report.md> "
                "--response <response.json> --mode "
                "<new_revision|reconsideration> after addressing every "
                "blocking finding",
                status.stdout,
            )

            state_before_replay = state_path.read_bytes()
            round_before_replay = round_path.read_bytes()
            events_before_replay = events_path.read_bytes()
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
            self.assertEqual(state_path.read_bytes(), state_before_replay)
            self.assertEqual(round_path.read_bytes(), round_before_replay)
            self.assertEqual(events_path.read_bytes(), events_before_replay)

    def test_budget_exhaustion_refuses_before_authoritative_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="changes_requested",
                review_limit=1,
            )
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            round_directory = run_directory / "rounds/001"
            events_path = run_directory / "events.jsonl"
            original_state = state_path.read_bytes()
            original_round = {
                path.relative_to(round_directory).as_posix()
                for path in round_directory.rglob("*")
            }
            original_events = events_path.read_bytes()

            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(applied.returncode, 1)
            self.assertIn("review budget exhaustion", applied.stderr)
            self.assertIn("no state was changed", applied.stderr)
            self.assertEqual(state_path.read_bytes(), original_state)
            self.assertEqual(events_path.read_bytes(), original_events)
            self.assertEqual(
                {
                    path.relative_to(round_directory).as_posix()
                    for path in round_directory.rglob("*")
                },
                original_round,
            )
            for name in (
                "bundle",
                "review.json",
                "review.md",
                "review-marker.json",
            ):
                self.assertFalse((round_directory / name).exists())

    def test_corrected_revision_archives_response_and_starts_fresh_round(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="changes_requested",
            )
            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertFalse(prepared.review_worktree.exists())

            (prepared.repository / "feature.txt").write_text(
                "complete candidate\n",
                encoding="utf-8",
            )
            run(["git", "add", "feature.txt"], cwd=prepared.repository)
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    "fix: complete candidate",
                ],
                cwd=prepared.repository,
            )
            corrected_head = run(
                ["git", "rev-parse", "HEAD"],
                cwd=prepared.repository,
            ).stdout.strip()
            report = root / "corrected-report.md"
            report.write_text(
                "# Implementation Report\n\nCompleted the candidate value.\n",
                encoding="utf-8",
            )
            response_path = root / "response.json"
            _write_fixed_response(response_path, prepared, review)

            submitted = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report),
                "--response",
                str(response_path),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            control_root = prepared.repository / ".agent-squad"
            state = json.loads(
                (control_root / "state.json").read_text(encoding="utf-8")
            )
            run_id = str(state["active_run_id"])
            run_directory = control_root / "runs" / run_id
            first_round = run_directory / "rounds/001"
            second_round = run_directory / "rounds/002"
            second_request = json.loads(
                (second_round / "request.json").read_text(encoding="utf-8")
            )
            second_worktree = Path(state["active_round"]["review_worktree"])
            second_bundle = second_worktree / ".agent-squad-review"

            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["current_round"], 2)
            self.assertEqual(state["current_head_oid"], corrected_head)
            self.assertEqual(
                second_request["base_oid"],
                prepared.request["base_oid"],
            )
            self.assertEqual(second_request["head_oid"], corrected_head)
            self.assertNotEqual(
                second_request["head_oid"],
                review["head_oid"],
            )
            self.assertEqual(
                second_request["previous_review_path"],
                "input/previous-review.json",
            )
            self.assertEqual(
                second_request["previous_response_path"],
                "input/previous-response.json",
            )
            self.assertNotEqual(
                second_request["reviewer_name"],
                prepared.request["reviewer_name"],
            )
            self.assertNotEqual(second_worktree, prepared.review_worktree)
            self.assertEqual(
                run(["git", "rev-parse", "HEAD"], cwd=second_worktree)
                .stdout.strip(),
                corrected_head,
            )
            self.assertEqual(
                run(
                    ["git", "symbolic-ref", "--quiet", "HEAD"],
                    cwd=second_worktree,
                    check=False,
                ).returncode,
                1,
            )
            self.assertEqual(
                (first_round / "response.json").read_bytes(),
                response_path.read_bytes(),
            )
            self.assertEqual(
                (second_bundle / "input/previous-review.json").read_bytes(),
                (first_round / "review.json").read_bytes(),
            )
            self.assertEqual(
                (second_bundle / "input/previous-response.json").read_bytes(),
                response_path.read_bytes(),
            )

            state_path = control_root / "state.json"
            events_path = run_directory / "events.jsonl"
            replay_snapshot = (
                state_path.read_bytes(),
                (second_round / "round.json").read_bytes(),
                events_path.read_bytes(),
            )
            historical_replay = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(
                historical_replay.returncode,
                0,
                historical_replay.stderr,
            )
            self.assertIn("already applied", historical_replay.stdout)
            self.assertIn("Round: 1", historical_replay.stdout)
            self.assertIn(
                "Next action: wait for the Reviewer result",
                historical_replay.stdout,
            )
            self.assertEqual(
                (
                    state_path.read_bytes(),
                    (second_round / "round.json").read_bytes(),
                    events_path.read_bytes(),
                ),
                replay_snapshot,
            )

            canonical_response = first_round / "response.json"
            canonical_bytes = canonical_response.read_bytes()
            canonical_response.chmod(0o600)
            canonical_response.write_bytes(canonical_bytes + b"\n")
            try:
                with self.assertRaisesRegex(
                    runs.RunStateError,
                    "previous response does not match the active round",
                ):
                    runs.inspect_status(prepared.repository)
            finally:
                canonical_response.write_bytes(canonical_bytes)
                canonical_response.chmod(0o400)

            second_prepared = replace(
                prepared,
                review_worktree=second_worktree,
                bundle=second_bundle,
                request=second_request,
            )
            approval_review = _submit_with_lost_notification(
                second_prepared,
                verdict="approved",
            )
            approved = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(approval_review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertIn("Verdict: approved", approved.stdout)
            approved_state = json.loads(
                (control_root / "state.json").read_text(encoding="utf-8")
            )
            approved_round = json.loads(
                (second_round / "round.json").read_text(encoding="utf-8")
            )
            self.assertEqual(approved_state["phase"], "approved")
            self.assertEqual(
                approved_state["approved_head_oid"],
                corrected_head,
            )
            self.assertEqual(
                approved_state["review_budget"][
                    "completed_change_reviews"
                ],
                1,
            )
            self.assertEqual(approved_round["status"], "applied")
            self.assertEqual(approved_round["verdict"], "approved")
            self.assertEqual(
                approved_round["result_id"],
                approval_review["result_id"],
            )
            self.assertTrue((second_round / "approval.json").is_file())
            self.assertEqual(
                (
                    second_round
                    / "bundle/input/previous-response.json"
                ).read_bytes(),
                response_path.read_bytes(),
            )

    def test_historical_approved_replay_validates_approval_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_directory = root / "run"
            round_directory = run_directory / "rounds/001"
            round_directory.mkdir(parents=True)
            approval_content = b'{"approved":true}\n'
            approval_path = round_directory / "approval.json"
            approval_path.write_bytes(approval_content)
            approval_artifact = review_applications.BundleArtifact(
                path="approval.json",
                sha256=hashlib.sha256(approval_content).hexdigest(),
            )
            round_record = SimpleNamespace(
                status=runs.RoundStatus.APPLIED,
                round_number=1,
                approval=approval_artifact,
                bundle_archive=(),
            )
            authority = SimpleNamespace(
                review=SimpleNamespace(
                    verdict=runs.ReviewVerdict.APPROVED,
                    head_oid="a" * 40,
                )
            )
            repository = SimpleNamespace(control_root=root / "control")
            active = SimpleNamespace(
                run_id="12345678-1234-5678-9234-567812345678",
                current_round=2,
                base_oid="b" * 40,
                git_object_format="sha1",
            )
            result_id = "87654321-4321-6789-a234-678912345678"

            with (
                mock.patch.object(
                    runs,
                    "safe_run_directory",
                    return_value=run_directory,
                ),
                mock.patch.object(
                    runs,
                    "find_recorded_review_round",
                    return_value=(round_directory, round_record),
                ),
                mock.patch.object(
                    runs,
                    "validate_applied_review_round",
                    return_value=authority,
                ),
                mock.patch.object(review_applications, "_verify_bundle_tree"),
            ):
                replay = review_applications._historical_result_replay(
                    repository,
                    active,
                    result_id=result_id,
                    next_action="wait for the Reviewer result",
                )

            self.assertIsNotNone(replay)
            assert replay is not None
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.approval_path, approval_path)

            missing_approval = SimpleNamespace(
                status=runs.RoundStatus.APPLIED,
                round_number=1,
                approval=None,
                bundle_archive=(),
            )
            with (
                mock.patch.object(
                    runs,
                    "safe_run_directory",
                    return_value=run_directory,
                ),
                mock.patch.object(
                    runs,
                    "find_recorded_review_round",
                    return_value=(round_directory, missing_approval),
                ),
                mock.patch.object(
                    runs,
                    "validate_applied_review_round",
                    return_value=authority,
                ),
                mock.patch.object(review_applications, "_verify_bundle_tree"),
                self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "missing approval authority",
                ),
            ):
                review_applications._historical_result_replay(
                    repository,
                    active,
                    result_id=result_id,
                    next_action="wait for the Reviewer result",
                )

    def test_correction_round_request_history_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="changes_requested",
            )
            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)

            (prepared.repository / "feature.txt").write_text(
                "complete candidate\n",
                encoding="utf-8",
            )
            run(["git", "add", "feature.txt"], cwd=prepared.repository)
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    "fix: complete candidate",
                ],
                cwd=prepared.repository,
            )
            report = root / "corrected-report.md"
            report.write_text(
                "# Implementation Report\n\nCompleted the candidate value.\n",
                encoding="utf-8",
            )
            response_path = root / "response.json"
            _write_fixed_response(response_path, prepared, review)
            submitted = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report),
                "--response",
                str(response_path),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)

            control_root = prepared.repository / ".agent-squad"
            state = json.loads(
                (control_root / "state.json").read_text(encoding="utf-8")
            )
            run_directory = control_root / "runs" / str(state["active_run_id"])
            first_round = run_directory / "rounds/001"
            second_round = run_directory / "rounds/002"
            first_round_path = first_round / "round.json"
            first_review_path = first_round / "review.json"
            second_round_path = second_round / "round.json"

            def assert_rejected(
                replacements: tuple[tuple[Path, bytes], ...],
                message: str,
            ) -> None:
                originals = tuple(
                    (
                        path,
                        path.read_bytes(),
                        stat.S_IMODE(path.stat().st_mode),
                    )
                    for path, _ in replacements
                )
                try:
                    for path, content in replacements:
                        path.chmod(0o600)
                        path.write_bytes(content)
                    with self.assertRaisesRegex(runs.RunStateError, message):
                        runs.inspect_status(prepared.repository)
                finally:
                    for path, content, mode in originals:
                        path.write_bytes(content)
                        path.chmod(mode)

            second_record = json.loads(
                second_round_path.read_text(encoding="utf-8")
            )
            wrong_manifest = copy.deepcopy(second_record)
            previous_review_artifact = next(
                artifact
                for artifact in wrong_manifest["artifacts"]["bundle_inputs"]
                if artifact["path"] == "input/previous-review.json"
            )
            previous_review_artifact["path"] = "input/unexpected-review.json"
            assert_rejected(
                ((second_round_path, review_applications.encode_json(
                    wrong_manifest
                )),),
                "prior-artifact manifest does not match the request",
            )

            first_record = json.loads(
                first_round_path.read_text(encoding="utf-8")
            )
            first_review = json.loads(
                first_review_path.read_text(encoding="utf-8")
            )
            first_review["verdict"] = "approved"
            first_review["findings"] = []
            first_review_bytes = review_applications.encode_json(first_review)
            first_record["verdict"] = "approved"
            first_record["artifacts"]["review_result"]["sha256"] = (
                hashlib.sha256(first_review_bytes).hexdigest()
            )
            first_record["artifacts"]["approval"] = {
                "path": "approval.json",
                "sha256": "d" * 64,
            }
            assert_rejected(
                (
                    (
                        first_round_path,
                        review_applications.encode_json(first_record),
                    ),
                    (first_review_path, first_review_bytes),
                ),
                "must follow the most recent applied changes_requested result",
            )

            wrong_previous_review = copy.deepcopy(second_record)
            previous_review_artifact = next(
                artifact
                for artifact in wrong_previous_review["artifacts"][
                    "bundle_inputs"
                ]
                if artifact["path"] == "input/previous-review.json"
            )
            previous_review_artifact["sha256"] = "e" * 64
            assert_rejected(
                ((second_round_path, review_applications.encode_json(
                    wrong_previous_review
                )),),
                "previous review bundle input does not match the most recent",
            )

            self.assertEqual(
                runs.inspect_status(prepared.repository).active_run.phase,
                runs.RunPhase.REVIEWING,
            )

    def test_same_head_reconsideration_can_reach_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="changes_requested",
            )
            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)

            report = root / "reconsideration-report.md"
            report.write_text(
                "# Implementation Report\n\nNo code change is required.\n",
                encoding="utf-8",
            )
            response_path = root / "reconsideration-response.json"
            _write_rejected_response(response_path, prepared, review)

            submitted = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report),
                "--response",
                str(response_path),
                "--mode",
                "reconsideration",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            control_root = prepared.repository / ".agent-squad"
            state = json.loads(
                (control_root / "state.json").read_text(encoding="utf-8")
            )
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            second_round = run_directory / "rounds/002"
            second_request = json.loads(
                (second_round / "request.json").read_text(encoding="utf-8")
            )
            second_worktree = Path(state["active_round"]["review_worktree"])
            self.assertEqual(state["current_round"], 2)
            self.assertEqual(state["current_head_oid"], review["head_oid"])
            self.assertEqual(second_request["mode"], "reconsideration")
            self.assertEqual(second_request["head_oid"], review["head_oid"])
            stored_response = json.loads(
                (run_directory / "rounds/001/response.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                stored_response["responses"][0]["verification"],
                "",
            )

            second_prepared = replace(
                prepared,
                review_worktree=second_worktree,
                bundle=second_worktree / ".agent-squad-review",
                request=second_request,
            )
            approval_review = _submit_with_lost_notification(
                second_prepared,
                verdict="approved",
            )
            approved = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(approval_review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(approved.returncode, 0, approved.stderr)
            approved_state = json.loads(
                (control_root / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(approved_state["phase"], "approved")
            self.assertEqual(
                approved_state["approved_head_oid"],
                review["head_oid"],
            )

    def test_invalid_correction_stays_correctable_before_round_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="changes_requested",
            )
            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)

            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state_before = state_path.read_bytes()
            state = json.loads(state_before)
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            canonical_response = run_directory / "rounds/001/response.json"
            second_round = run_directory / "rounds/002"
            report = root / "corrected-report.md"
            report.write_text(
                "# Implementation Report\n\nCompleted the correction.\n",
                encoding="utf-8",
            )
            response_path = root / "response.json"
            response = _write_fixed_response(
                response_path,
                prepared,
                review,
            )

            uncommitted = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report),
                "--response",
                str(response_path),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(uncommitted.returncode, 1)
            self.assertIn("requires a new committed HEAD", uncommitted.stderr)
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertFalse(canonical_response.exists())
            self.assertFalse(second_round.exists())

            (prepared.repository / "feature.txt").write_text(
                "complete candidate\n",
                encoding="utf-8",
            )
            run(["git", "add", "feature.txt"], cwd=prepared.repository)
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    "fix: complete candidate",
                ],
                cwd=prepared.repository,
            )
            wrong_mode = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report),
                "--response",
                str(response_path),
                "--mode",
                "reconsideration",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(wrong_mode.returncode, 1)
            self.assertIn(
                "reconsideration submission must keep the exact previously "
                "reviewed HEAD",
                wrong_mode.stderr,
            )
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertFalse(canonical_response.exists())
            self.assertFalse(second_round.exists())

            dirty_path = prepared.repository / "unexpected-untracked.txt"
            dirty_path.write_text(
                "not part of the candidate\n",
                encoding="utf-8",
            )
            response["responses"] = []
            response_path.write_text(
                f"{json.dumps(response, indent=2)}\n",
                encoding="utf-8",
            )
            dirty_and_invalid = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report),
                "--response",
                str(response_path),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            dirty_path.unlink()

            self.assertEqual(dirty_and_invalid.returncode, 1)
            self.assertIn(
                "implementation worktree has unexpected untracked files",
                dirty_and_invalid.stderr,
            )
            self.assertNotIn(
                "missing blocking finding IDs",
                dirty_and_invalid.stderr,
            )
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertFalse(canonical_response.exists())
            self.assertFalse(second_round.exists())

            response_guards = (
                (
                    "superseding response",
                    lambda value: value.update(
                        supersedes_response_id=(
                            "66666666-6666-4666-8666-666666666666"
                        )
                    ),
                    "response replacement after Developer resolution",
                ),
                (
                    "resolution-linked response",
                    lambda value: value.update(
                        resolution_ids=[
                            "77777777-7777-4777-8777-777777777777"
                        ]
                    ),
                    "response replacement after Developer resolution",
                ),
                (
                    "needs-human response",
                    lambda value: value["responses"][0].update(
                        disposition="needs_human",
                        changed_files=[],
                        evidence=[],
                        verification="",
                    ),
                    "requires Developer authority",
                ),
            )
            for case, mutate, message in response_guards:
                with self.subTest(case=case):
                    guarded_response = _write_fixed_response(
                        response_path,
                        prepared,
                        review,
                    )
                    mutate(guarded_response)
                    response_path.write_text(
                        f"{json.dumps(guarded_response, indent=2)}\n",
                        encoding="utf-8",
                    )
                    guarded = run_cli(
                        prepared.repository,
                        "submit",
                        "--report",
                        str(report),
                        "--response",
                        str(response_path),
                        "--mode",
                        "new_revision",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )

                    self.assertEqual(guarded.returncode, 1)
                    self.assertIn(message, guarded.stderr)
                    self.assertEqual(state_path.read_bytes(), state_before)
                    self.assertFalse(canonical_response.exists())
                    self.assertFalse(second_round.exists())

            missing_response = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(missing_response.returncode, 1)
            self.assertIn(
                "requires --response <response.json>",
                missing_response.stderr,
            )
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertFalse(canonical_response.exists())
            self.assertFalse(second_round.exists())

            response["responses"] = []
            response_path.write_text(
                f"{json.dumps(response, indent=2)}\n",
                encoding="utf-8",
            )

            incomplete = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report),
                "--response",
                str(response_path),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(incomplete.returncode, 1)
            self.assertIn(
                "missing blocking finding IDs: REV-001",
                incomplete.stderr,
            )
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertFalse(canonical_response.exists())
            self.assertFalse(second_round.exists())

            _write_fixed_response(response_path, prepared, review)
            corrected = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report),
                "--response",
                str(response_path),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            self.assertTrue(canonical_response.is_file())
            self.assertTrue(second_round.is_dir())

    def test_precommit_failure_rolls_back_the_staged_response(self) -> None:
        for fail_response_restore in (False, True):
            with self.subTest(fail_response_restore=fail_response_restore):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    prepared, review = _marker_confirmed_review(
                        root,
                        verdict="changes_requested",
                    )
                    review_applications.apply_review(
                        prepared.repository,
                        result_id=str(review["result_id"]),
                    )
                    (prepared.repository / "feature.txt").write_text(
                        "complete candidate\n",
                        encoding="utf-8",
                    )
                    run(
                        ["git", "add", "feature.txt"],
                        cwd=prepared.repository,
                    )
                    run(
                        [
                            "git",
                            "-c",
                            "commit.gpgSign=false",
                            "commit",
                            "--no-verify",
                            "-m",
                            "fix: complete candidate",
                        ],
                        cwd=prepared.repository,
                    )
                    report = root / "corrected-report.md"
                    report.write_text(
                        "# Implementation Report\n\nCorrected.\n",
                        encoding="utf-8",
                    )
                    response_path = root / "response.json"
                    _write_fixed_response(response_path, prepared, review)
                    control_root = prepared.repository / ".agent-squad"
                    state_path = control_root / "state.json"
                    original_state = state_path.read_bytes()
                    state = json.loads(original_state)
                    run_directory = (
                        control_root
                        / "runs"
                        / str(state["active_run_id"])
                    )
                    run_path = run_directory / "run.json"
                    canonical_response = (
                        run_directory / "rounds/001/response.json"
                    )
                    second_round = run_directory / "rounds/002"
                    real_atomic_write = submissions.atomic_write
                    real_read_file = submissions._read_file

                    def fail_run_record(path, content, *, mode):
                        if path.resolve() == run_path.resolve() and (
                            b'"phase": "reviewing"' in content
                        ):
                            raise OSError("injected run-record failure")
                        real_atomic_write(path, content, mode=mode)

                    def read_for_rollback(path: Path) -> bytes:
                        if fail_response_restore and (
                            path.resolve() == canonical_response.resolve()
                        ):
                            raise submissions.SubmissionError(
                                "injected response read failure"
                            )
                        return real_read_file(path)

                    expected = (
                        "could not restore the previous-round response"
                        if fail_response_restore
                        else "injected run-record failure"
                    )
                    with (
                        mock.patch.object(
                            submissions,
                            "atomic_write",
                            side_effect=fail_run_record,
                        ),
                        mock.patch.object(
                            submissions,
                            "_read_file",
                            side_effect=read_for_rollback,
                        ),
                        self.assertRaisesRegex(
                            submissions.SubmissionError,
                            expected,
                        ),
                    ):
                        submissions.submit_candidate(
                            prepared.repository,
                            report_path=report,
                            response_path=response_path,
                            mode="new_revision",
                            herdr_client=mock.Mock(),
                        )

                    self.assertEqual(state_path.read_bytes(), original_state)
                    self.assertFalse(second_round.exists())
                    self.assertEqual(
                        canonical_response.exists(),
                        fail_response_restore,
                    )

    def test_state_commit_error_preserves_response_and_new_round(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="changes_requested",
            )
            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)

            (prepared.repository / "feature.txt").write_text(
                "complete candidate\n",
                encoding="utf-8",
            )
            run(["git", "add", "feature.txt"], cwd=prepared.repository)
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    "fix: complete candidate",
                ],
                cwd=prepared.repository,
            )
            report = root / "corrected-report.md"
            report.write_text(
                "# Implementation Report\n\nCompleted the correction.\n",
                encoding="utf-8",
            )
            response_path = root / "response.json"
            _write_fixed_response(response_path, prepared, review)
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            original_atomic_write = submissions.atomic_write

            def fail_after_state_commit(
                path: Path,
                content: bytes,
                *,
                mode: int,
            ) -> None:
                original_atomic_write(path, content, mode=mode)
                if path.name != "state.json":
                    return
                written = json.loads(content)
                if written.get("phase") == "reviewing" and (
                    written.get("current_round") == 2
                ) and written.get("handoff", {}).get("status") == "pending":
                    raise OSError("injected post-replace state failure")

            client = mock.Mock()
            with (
                mock.patch.object(
                    submissions,
                    "atomic_write",
                    side_effect=fail_after_state_commit,
                ),
                self.assertRaisesRegex(
                    submissions.SubmissionError,
                    "review request is durable",
                ),
            ):
                submissions.submit_candidate(
                    prepared.repository,
                    report_path=report,
                    response_path=response_path,
                    mode="new_revision",
                    herdr_client=client,
                )

            client.discover.assert_not_called()
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["current_round"], 2)
            self.assertEqual(state["handoff"]["status"], "pending")
            self.assertEqual(
                (run_directory / "rounds/001/response.json").read_bytes(),
                response_path.read_bytes(),
            )
            self.assertTrue((run_directory / "rounds/002").is_dir())
            status = runs.inspect_status(prepared.repository)
            self.assertEqual(status.active_run.phase, runs.RunPhase.REVIEWING)

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
            approval_mode = stat.S_IMODE(approval_path.stat().st_mode)
            tampered_approval = json.loads(first_approval.decode("utf-8"))
            tampered_approval["head_oid"] = "f" * 40
            retry_cases = (
                (
                    "corrupt leftover",
                    b"{not json\n",
                    "existing approval artifact is invalid",
                ),
                (
                    "tampered leftover",
                    (
                        f"{json.dumps(tampered_approval, indent=2)}\n".encode(
                            "utf-8"
                        )
                    ),
                    "existing immutable artifact differs",
                ),
            )
            for case, content, message in retry_cases:
                with self.subTest(case=case):
                    approval_path.chmod(0o600)
                    approval_path.write_bytes(content)
                    try:
                        with self.assertRaisesRegex(
                            review_applications.ReviewApplicationError,
                            message,
                        ):
                            review_applications.apply_review(
                                prepared.repository,
                                result_id=str(review["result_id"]),
                            )
                    finally:
                        approval_path.write_bytes(first_approval)
                        approval_path.chmod(approval_mode)
                    self.assertEqual(state_path.read_bytes(), state_before)
                    self.assertEqual(round_path.read_bytes(), round_before)
                    self.assertEqual(run_path.read_bytes(), run_before)

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

            repository = review_applications.load_initialized_repository(
                prepared.repository
            )
            current_identity = runs.repository_identity(repository.worktree)
            changed_identity = replace(
                current_identity,
                repository_id="different-live-repository",
            )
            with (
                mock.patch.object(
                    runs,
                    "repository_identity",
                    return_value=changed_identity,
                ),
                self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "active run repository ID does not match the current",
                ),
            ):
                review_applications.complete_run(prepared.repository)

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
