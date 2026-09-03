from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import handoffs, runs  # noqa: E402
from agent_squad.artifacts import (  # noqa: E402
    ActiveRoundRecord,
    HandoffStatus,
    RoundStatus,
    SubmissionMode,
)
from agent_squad.herdr import HerdrInstallation  # noqa: E402
from agent_squad.storage import encode_json  # noqa: E402


RUN_ID = "12345678-1234-5678-9234-567812345678"
REQUEST_ID = "87654321-4321-6789-a234-678912345678"
REVIEWER_NAME = "asq-12345678-r001-reviewer"


def _repository(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        control_root=root / ".agent-squad",
        worktree=SimpleNamespace(
            invocation_directory=root,
            root=root,
        ),
    )


def _active_round(root: Path) -> ActiveRoundRecord:
    return ActiveRoundRecord(
        round_number=1,
        status=RoundStatus.REVIEWING,
        mode=SubmissionMode.NEW_REVISION,
        request_id=REQUEST_ID,
        result_id=None,
        review_worktree=root / "review-worktree",
        reviewer_name=REVIEWER_NAME,
    )


class RetryHandoffGuardTests(unittest.TestCase):
    def _retry_with_active(
        self,
        root: Path,
        active: object | None,
    ) -> handoffs.RetryHandoffResult:
        repository = _repository(root)
        with (
            mock.patch.object(
                handoffs,
                "load_initialized_repository",
                return_value=repository,
            ),
            mock.patch.object(
                handoffs,
                "exclusive_file_lock",
                return_value=nullcontext(),
            ),
            mock.patch.object(
                handoffs.runs,
                "inspect_status_locked",
                return_value=SimpleNamespace(active_run=active),
            ),
        ):
            return handoffs.retry_handoff(
                root,
                herdr_client=mock.Mock(),
            )

    def test_rejects_missing_run_wrong_phase_and_missing_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            active_round = _active_round(root)
            cases = (
                (None, "there is no active review handoff"),
                (
                    SimpleNamespace(phase=runs.RunPhase.IMPLEMENTING),
                    "requires an active reviewing round",
                ),
                (
                    SimpleNamespace(
                        phase=runs.RunPhase.REVIEWING,
                        active_round=None,
                        handoff=object(),
                    ),
                    "has no durable handoff",
                ),
                (
                    SimpleNamespace(
                        phase=runs.RunPhase.REVIEWING,
                        active_round=active_round,
                        handoff=None,
                    ),
                    "has no durable handoff",
                ),
            )
            for active, message in cases:
                with (
                    self.subTest(message=message, active=active),
                    self.assertRaisesRegex(
                        handoffs.HandoffRecoveryError,
                        message,
                    ),
                ):
                    self._retry_with_active(root, active)

    def test_rejects_an_unavailable_review_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            active = SimpleNamespace(
                phase=runs.RunPhase.REVIEWING,
                active_round=_active_round(root),
                handoff=SimpleNamespace(status=HandoffStatus.SENT),
                unapplied_review=None,
                review_worktree_available=False,
            )

            with self.assertRaisesRegex(
                handoffs.HandoffRecoveryError,
                "expected review worktree is unavailable",
            ):
                self._retry_with_active(root, active)

    def test_lock_os_error_is_converted_to_a_recovery_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = _repository(root)
            with (
                mock.patch.object(
                    handoffs,
                    "load_initialized_repository",
                    return_value=repository,
                ),
                mock.patch.object(
                    handoffs,
                    "exclusive_file_lock",
                    side_effect=OSError("lock unavailable"),
                ),
                self.assertRaisesRegex(
                    handoffs.HandoffRecoveryError,
                    "could not acquire or use the local recovery lock",
                ),
            ):
                handoffs.retry_handoff(root, herdr_client=mock.Mock())

    def test_invalid_active_request_is_reported_as_recovery_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root = root / ".agent-squad"
            active_round = _active_round(root)
            request_path = (
                control_root
                / runs.RUNS_DIRECTORY_NAME
                / RUN_ID
                / "rounds/001/request.json"
            )
            request_path.parent.mkdir(parents=True)
            request_path.write_bytes(encode_json({}))

            with self.assertRaisesRegex(
                handoffs.HandoffRecoveryError,
                "active review request is invalid",
            ):
                handoffs._load_active_request(
                    control_root,
                    run_id=RUN_ID,
                    active_round=active_round,
                )


class InvalidEvidencePreservationTests(unittest.TestCase):
    def _prepare_archive(
        self,
        root: Path,
    ) -> tuple[Path, dict[str, bytes]]:
        control_root = root / ".agent-squad"
        (
            control_root
            / runs.RUNS_DIRECTORY_NAME
            / RUN_ID
            / "rounds/001"
        ).mkdir(parents=True)
        return control_root, {
            "review.json": b'{"invalid":true}\n',
            "review.md": b"# Invalid\n",
            "local-state.json": b'{"marker":true}\n',
        }

    def _archive(
        self,
        control_root: Path,
        captured: dict[str, bytes],
    ) -> str:
        return handoffs.archive_invalid_review_evidence(
            control_root,
            run_id=RUN_ID,
            round_number=1,
            request_id=REQUEST_ID,
            reason="digest mismatch",
            captured=captured,
        )

    def test_preserves_evidence_idempotently_with_validation_reason(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root, captured = self._prepare_archive(root)

            with mock.patch.object(
                handoffs,
                "utc_timestamp",
                return_value="2026-08-31T12:00:00Z",
            ) as timestamp:
                first_id = self._archive(control_root, captured)
                second_id = self._archive(control_root, captured)

            self.assertEqual(second_id, first_id)
            self.assertEqual(timestamp.call_count, 1)
            diagnostic_root = (
                control_root
                / runs.RUNS_DIRECTORY_NAME
                / RUN_ID
                / "rounds/001/diagnostics/invalid-results"
            )
            self.assertEqual(
                [path.name for path in diagnostic_root.iterdir()],
                [first_id],
            )
            diagnostic = diagnostic_root / first_id
            self.assertEqual(
                (diagnostic / "review.json").read_bytes(),
                b'{"invalid":true}\n',
            )
            summary = json.loads(
                (diagnostic / "validation-error.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                summary,
                {
                    "schema_version": 1,
                    "created_at": "2026-08-31T12:00:00Z",
                    "run_id": RUN_ID,
                    "round": 1,
                    "request_id": REQUEST_ID,
                    "diagnostic_id": first_id,
                    "reason": "digest mismatch",
                    "captured_files": [
                        "local-state.json",
                        "review.json",
                        "review.md",
                    ],
                },
            )

    def test_hash_framing_distinguishes_previous_null_collision(self) -> None:
        first = handoffs._invalid_review_diagnostic_id(
            "same reason",
            {"review.json": b"A\0review.md\0B"},
        )
        second = handoffs._invalid_review_diagnostic_id(
            "same reason",
            {"review.json": b"A", "review.md": b"B"},
        )

        self.assertNotEqual(first, second)

    def test_symlinked_diagnostic_parent_cannot_escape_run_storage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root, captured = self._prepare_archive(root)
            diagnostics = (
                control_root
                / runs.RUNS_DIRECTORY_NAME
                / RUN_ID
                / "rounds/001/diagnostics"
            )
            diagnostics.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (diagnostics / "invalid-results").symlink_to(
                outside,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                handoffs.HandoffRecoveryError,
                "invalid-result diagnostics must be a non-symlink directory",
            ):
                self._archive(control_root, captured)

            self.assertEqual(list(outside.iterdir()), [])

    def test_existing_diagnostic_must_match_the_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root, captured = self._prepare_archive(root)
            diagnostic_id = self._archive(control_root, captured)
            diagnostic = (
                control_root
                / runs.RUNS_DIRECTORY_NAME
                / RUN_ID
                / "rounds/001/diagnostics/invalid-results"
                / diagnostic_id
            )
            (diagnostic / "stale.txt").write_text(
                "stale\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                handoffs.HandoffRecoveryError,
                "does not match captured evidence",
            ):
                self._archive(control_root, captured)

    def test_write_failure_refuses_to_risk_overwriting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root, captured = self._prepare_archive(root)
            active_round = _active_round(root)
            bundle = active_round.review_worktree / ".agent-squad-review"
            (bundle / "output").mkdir(parents=True)
            (bundle / "output/review.json").write_bytes(
                captured["review.json"]
            )
            (bundle / "output/review.md").write_bytes(
                captured["review.md"]
            )
            marker = bundle / "local-state.json"
            marker.write_bytes(captured["local-state.json"])

            with (
                mock.patch.object(
                    handoffs,
                    "atomic_write",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(
                    handoffs.HandoffRecoveryError,
                    "could not preserve invalid marker-confirmed review",
                ),
            ):
                handoffs._preserve_invalid_review_evidence(
                    control_root,
                    active=SimpleNamespace(run_id=RUN_ID),
                    active_round=active_round,
                    request=SimpleNamespace(),
                    invalid_review=runs.InvalidUnappliedReviewResult(
                        "digest mismatch"
                    ),
                )

            invalid_results = (
                control_root
                / runs.RUNS_DIRECTORY_NAME
                / RUN_ID
                / "rounds/001/diagnostics/invalid-results"
            )
            self.assertEqual(list(invalid_results.iterdir()), [])
            self.assertEqual(
                marker.read_bytes(),
                captured["local-state.json"],
            )


class HandoffPersistenceTests(unittest.TestCase):
    def _prepare_control_root(self, root: Path) -> tuple[Path, Path]:
        control_root = root / ".agent-squad"
        state_path = control_root / runs.STATE_FILE_NAME
        event_path = (
            control_root
            / runs.RUNS_DIRECTORY_NAME
            / RUN_ID
            / runs.EVENT_LOG_FILE_NAME
        )
        event_path.parent.mkdir(parents=True)
        event_path.write_bytes(b"")
        state_path.write_bytes(
            encode_json(
                {
                    "active_run_id": RUN_ID,
                    "active_round": {"request_id": REQUEST_ID},
                    "handoff": None,
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            )
        )
        return control_root, event_path

    def _record(
        self,
        control_root: Path,
        *,
        status: HandoffStatus = HandoffStatus.SENT,
    ) -> None:
        handoffs.record_review_request_handoff(
            control_root,
            run_id=RUN_ID,
            round_number=1,
            request_id=REQUEST_ID,
            target=REVIEWER_NAME,
            status=status,
            error=(
                "dispatch failed"
                if status is HandoffStatus.FAILED
                else None
            ),
            installation=HerdrInstallation(
                executable=Path("/fake/herdr"),
                version="herdr test-0.8.2",
                protocol=20,
            ),
            sent_event_name="review_request_recovered",
            failed_event_name="review_request_recovery_failed",
            error_type=handoffs.HandoffRecoveryError,
            extra_event_fields={"action": "reprompted"},
        )

    def test_writes_state_before_an_event_with_a_stable_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root, event_path = self._prepare_control_root(root)

            with mock.patch.object(
                handoffs,
                "utc_timestamp",
                return_value="2026-08-30T12:00:00Z",
            ):
                self._record(control_root)

            state = json.loads(
                (control_root / runs.STATE_FILE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["handoff"]["status"], "sent")
            self.assertEqual(
                state["handoff"]["herdr_version"],
                "herdr test-0.8.2",
            )
            event = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertEqual(event["event"], "review_request_recovered")
            self.assertEqual(event["action"], "reprompted")

    def test_selects_the_failed_event_name_from_handoff_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root, event_path = self._prepare_control_root(root)

            self._record(control_root, status=HandoffStatus.FAILED)

            event = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertEqual(
                event["event"],
                "review_request_recovery_failed",
            )
            self.assertEqual(event["error"], "dispatch failed")

    def test_concurrent_run_or_round_change_is_not_overwritten(self) -> None:
        cases = (
            ("active_run_id", "different-run", "active run changed"),
            (
                "request_id",
                "different-request",
                "active round changed",
            ),
        )
        for field, replacement, message in cases:
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    control_root, event_path = self._prepare_control_root(root)
                    state_path = control_root / runs.STATE_FILE_NAME
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if field == "active_run_id":
                        state[field] = replacement
                    else:
                        state["active_round"][field] = replacement
                    state_path.write_bytes(encode_json(state))
                    original = state_path.read_bytes()

                    with self.assertRaisesRegex(
                        handoffs.HandoffRecoveryError,
                        message,
                    ):
                        self._record(control_root)

                    self.assertEqual(state_path.read_bytes(), original)
                    self.assertEqual(event_path.read_bytes(), b"")

    def test_state_write_failure_does_not_append_an_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root, event_path = self._prepare_control_root(root)
            state_path = control_root / runs.STATE_FILE_NAME
            original = state_path.read_bytes()

            with (
                mock.patch.object(
                    handoffs,
                    "atomic_write",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(
                    handoffs.HandoffRecoveryError,
                    "could not record review handoff state: disk full",
                ),
            ):
                self._record(control_root)

            self.assertEqual(state_path.read_bytes(), original)
            self.assertEqual(event_path.read_bytes(), b"")

    def test_event_failure_leaves_the_updated_state_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            control_root, _ = self._prepare_control_root(root)

            with (
                mock.patch.object(
                    handoffs,
                    "append_event",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(
                    handoffs.HandoffRecoveryError,
                    "handoff state is durable",
                ),
            ):
                self._record(control_root)

            state = json.loads(
                (control_root / runs.STATE_FILE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["handoff"]["status"], "sent")


if __name__ == "__main__":
    unittest.main()
