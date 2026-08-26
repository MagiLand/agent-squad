from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import runs  # noqa: E402
from agent_squad.artifacts import (  # noqa: E402
    ActiveRoundRecord,
    HandoffRecord,
    HandoffStatus,
    RoundStatus,
    SubmissionMode,
)


def _active_round(
    status: RoundStatus = RoundStatus.REVIEWING,
) -> ActiveRoundRecord:
    return ActiveRoundRecord(
        round_number=1,
        status=status,
        mode=SubmissionMode.NEW_REVISION,
        request_id="12345678-1234-5678-9234-567812345678",
        result_id=(
            "87654321-4321-6789-a234-678912345678"
            if status is RoundStatus.APPLIED
            else None
        ),
        review_worktree=Path("/tmp/review"),
        reviewer_name="asq-12345678-r001-reviewer",
    )


def _handoff() -> HandoffRecord:
    return HandoffRecord(
        round_number=1,
        status=HandoffStatus.SENT,
        target="asq-12345678-r001-reviewer",
        last_error=None,
        updated_at="2026-08-26T12:00:00Z",
        herdr_version="herdr test",
        herdr_protocol=20,
    )


class ReviewBudgetTests(unittest.TestCase):
    def test_initial_budget_round_trips(self) -> None:
        budget = runs.ReviewBudget.initial(4)

        self.assertEqual(
            runs.ReviewBudget.from_dict(budget.to_dict()),
            budget,
        )

    def test_invalid_budget_invariants_are_rejected(self) -> None:
        cases = (
            (
                {
                    "original_limit": 0,
                    "additional_rounds_granted": 0,
                    "effective_limit": 0,
                    "completed_change_reviews": 0,
                },
                "original_limit must be positive",
            ),
            (
                {
                    "original_limit": 4,
                    "additional_rounds_granted": -1,
                    "effective_limit": 3,
                    "completed_change_reviews": 0,
                },
                "additional_rounds_granted must not be negative",
            ),
            (
                {
                    "original_limit": 4,
                    "additional_rounds_granted": 1,
                    "effective_limit": 4,
                    "completed_change_reviews": 0,
                },
                "effective_limit must equal",
            ),
            (
                {
                    "original_limit": 4,
                    "additional_rounds_granted": 0,
                    "effective_limit": 4,
                    "completed_change_reviews": 5,
                },
                "completed_change_reviews must be between",
            ),
        )
        for value, message in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(runs.RunStateError, message):
                    runs.ReviewBudget.from_dict(value)

    def test_budget_rejects_missing_unknown_and_boolean_fields(self) -> None:
        valid = runs.ReviewBudget.initial(4).to_dict()
        cases = (
            (
                {
                    key: value
                    for key, value in valid.items()
                    if key != "effective_limit"
                },
                "missing required field",
            ),
            ({**valid, "unexpected": 1}, "unknown field"),
            ({**valid, "original_limit": True}, "must be an integer"),
        )
        for value, message in cases:
            with self.subTest(value=value):
                with self.assertRaisesRegex(runs.RunStateError, message):
                    runs.ReviewBudget.from_dict(value)


class ProtocolValueValidationTests(unittest.TestCase):
    def test_full_object_ids_support_sha1_and_sha256(self) -> None:
        sha1 = "a" * 40
        sha256 = "b" * 64

        self.assertEqual(runs._require_oid(sha1, "sha1", "oid"), sha1)
        self.assertEqual(runs._require_oid(sha256, "sha256", "oid"), sha256)

    def test_abbreviated_and_uppercase_object_ids_are_rejected(self) -> None:
        for value, object_format in (("a" * 12, "sha1"), ("A" * 40, "sha1")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    runs.RunStateError,
                    "full lowercase",
                ):
                    runs._require_oid(value, object_format, "oid")

    def test_timestamps_require_rfc3339_utc_form(self) -> None:
        valid = "2026-08-24T12:34:56Z"
        self.assertEqual(runs._require_timestamp(valid, "timestamp"), valid)

        for value in (
            "2026-08-24 12:34:56Z",
            "2026-08-24T12:34:56+00:00",
            "2026-13-24T12:34:56Z",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    runs.RunStateError,
                    "RFC 3339 UTC timestamp",
                ):
                    runs._require_timestamp(value, "timestamp")

    def test_start_selections_reject_ambiguous_text(self) -> None:
        for value in ("", " main", "main ", "main\x00other"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    runs.RunStartError,
                    "must be non-empty",
                ):
                    runs._validate_selection(value, "base reference")

    def test_other_protocol_validators_reject_malformed_values(self) -> None:
        cases = (
            (
                lambda: runs._require_uuid("not-a-uuid", "run_id"),
                "canonical UUID",
            ),
            (
                lambda: runs._require_phase("waiting", "phase"),
                "must be one of",
            ),
            (
                lambda: runs._require_digest("A" * 64, "digest"),
                "lowercase SHA-256",
            ),
        )
        for validator, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(runs.RunStateError, message):
                    validator()

    def test_next_action_covers_implemented_active_phases(self) -> None:
        self.assertEqual(
            runs._next_action(runs.RunPhase.IMPLEMENTING),
            "continue implementing the captured task",
        )
        self.assertEqual(
            runs._next_action(runs.RunPhase.REVIEWING),
            "wait for the Reviewer result",
        )
        self.assertEqual(
            runs._next_action(
                runs.RunPhase.REVIEWING,
                handoff_status=runs.HandoffStatus.FAILED,
            ),
            "recover the preserved review-request handoff",
        )
        with self.assertRaisesRegex(
            runs.RunStateError,
            "approved is not supported",
        ):
            runs._next_action(runs.RunPhase.APPROVED)


class RunArtifactValidationTests(unittest.TestCase):
    def test_event_log_rejects_missing_corrupt_and_inconsistent_start(
        self,
    ) -> None:
        run_id = "12345678-1234-5678-9234-567812345678"
        other_run_id = "87654321-4321-6789-a234-678912345678"
        base_oid = "a" * 40
        event = {
            "timestamp": "2026-08-24T12:34:56Z",
            "event": "run_started",
            "run_id": run_id,
            "phase": "implementing",
            "base_oid": base_oid,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "events.jsonl"
            with self.assertRaisesRegex(
                runs.RunStateError,
                "regular non-symlink file",
            ):
                runs._validate_event_log(path, run_id, base_oid)

            path.write_text(f"{json.dumps(event)}\n", encoding="utf-8")
            runs._validate_event_log(path, run_id, base_oid)

            cases = (
                ("", "must contain run_started"),
                ("{\n", "contains invalid JSON"),
                (
                    '{"event":"run_started","event":"changed"}\n',
                    "duplicate object key",
                ),
                (
                    f"{json.dumps({**event, 'run_id': other_run_id})}\n",
                    "identifies a different run",
                ),
                (
                    f"{json.dumps({**event, 'timestamp': 'yesterday'})}\n",
                    r"event log line 1\.timestamp",
                ),
                (
                    f"{json.dumps({**event, 'base_oid': 'b' * 40})}\n",
                    "base OID does not match",
                ),
            )
            for content, message in cases:
                with self.subTest(message=message):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(
                        runs.RunStateError,
                        message,
                    ):
                        runs._validate_event_log(path, run_id, base_oid)
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(
                runs.RunStateError,
                "cannot read active run event log",
            ):
                runs._validate_event_log(path, run_id, base_oid)

    def test_captured_path_rejects_traversal_and_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            task = root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            self.assertEqual(
                runs._captured_path(root, "task.md", "task"),
                task,
            )
            with self.assertRaisesRegex(
                runs.RunStateError,
                "must stay inside",
            ):
                runs._captured_path(
                    root,
                    "../outside.md",
                    "task",
                )

            link = root / "link.md"
            link.symlink_to(task)
            with self.assertRaisesRegex(
                runs.RunStateError,
                "must not contain symbolic links",
            ):
                runs._captured_path(root, "link.md", "task")

    def test_capture_input_rejects_nonregular_non_utf8_and_empty_tasks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            binary = root / "binary.md"
            binary.write_bytes(b"\xff")
            empty = root / "empty.md"
            empty.write_text(" \n", encoding="utf-8")
            cases = (
                (Path("/dev/null"), "regular file"),
                (binary, "UTF-8 text"),
                (empty, "must not be empty"),
            )
            for path, message in cases:
                with self.subTest(path=path):
                    with self.assertRaisesRegex(
                        runs.RunStartError,
                        message,
                    ):
                        runs._capture_input(
                            path,
                            root,
                            "task.md",
                            label="task specification",
                            require_text=True,
                        )

    def test_capture_input_rejects_fifo_before_opening(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            fifo = root / "task.pipe"
            os.mkfifo(fifo)

            with mock.patch.object(
                Path,
                "open",
                side_effect=AssertionError("FIFO must not be opened"),
            ) as open_file:
                with self.assertRaisesRegex(
                    runs.RunStartError,
                    "must be a regular file",
                ):
                    runs._capture_input(
                        fifo,
                        root,
                        "task.md",
                        label="task specification",
                        require_text=True,
                    )

            open_file.assert_not_called()

    def test_authoritative_path_guards_reject_unsafe_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.write_text("target\n", encoding="utf-8")
            state_link = root / "state-link.json"
            state_link.symlink_to(target)
            with self.assertRaisesRegex(runs.RunStateError, "symbolic link"):
                runs._ensure_state_path_is_safe(state_link)

            state_directory = root / "state-directory"
            state_directory.mkdir()
            with self.assertRaisesRegex(runs.RunStateError, "regular file"):
                runs._ensure_state_path_is_safe(state_directory)

            runs_file = root / "runs-file"
            runs_file.write_text("not a directory\n", encoding="utf-8")
            with self.assertRaisesRegex(runs.RunStateError, "directory"):
                runs._ensure_runs_root(runs_file)

            runs_link = root / "runs-link"
            runs_link.symlink_to(state_directory)
            with self.assertRaisesRegex(runs.RunStateError, "symbolic link"):
                runs._ensure_runs_root(runs_link)

            control = root / "control"
            control.mkdir()
            (control / "runs").write_text(
                "not a directory\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                runs.RunStateError,
                "run history path must be a non-symlink directory",
            ):
                runs.safe_run_directory(control, "run-id")

            other_control = root / "other-control"
            run_root = other_control / "runs"
            run_root.mkdir(parents=True)
            (run_root / "run-id").write_text(
                "not a directory\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                runs.RunStateError,
                "active run directory must be a non-symlink directory",
            ):
                runs.safe_run_directory(other_control, "run-id")

            symlink_control = root / "symlink-control"
            symlink_run_root = symlink_control / "runs"
            symlink_run_root.mkdir(parents=True)
            run_target = root / "run-target"
            run_target.mkdir()
            (symlink_run_root / "run-id").symlink_to(run_target)
            with self.assertRaisesRegex(
                runs.RunStateError,
                "active run directory must be a non-symlink directory",
            ):
                runs.safe_run_directory(symlink_control, "run-id")

    def test_repository_and_reviewer_records_reject_inconsistent_data(
        self,
    ) -> None:
        repository = {
            "implementation_root": "/tmp/repository",
            "git_common_dir": "/tmp/repository/.git",
            "worktree_git_dir": "/tmp/repository/.git",
            "repository_id": "a" * 64,
            "start_branch_ref": None,
            "start_head_detached": False,
        }
        with self.assertRaisesRegex(
            runs.RunStateError,
            "branch and detached fields are inconsistent",
        ):
            runs._validate_repository_record(repository)
        with self.assertRaisesRegex(
            runs.RunStateError,
            "non-empty strings",
        ):
            runs._validate_reviewer(
                {"kind": "codex", "start_args": [""]}
            )

    def test_typed_role_records_preserve_field_names(self) -> None:
        captured = runs._validate_captured_record(
            {
                "source_path": "/tmp/task.md",
                "path": "task.md",
                "sha256": "a" * 64,
            },
            "run record.task",
        )
        implementer = runs._validate_implementer(
            {"agent_name": "codex-main", "kind": "codex"}
        )
        reviewer = runs._validate_reviewer(
            {"kind": "claude", "start_args": ["--strict"]}
        )
        self.assertEqual(captured.source_path, Path("/tmp/task.md"))
        self.assertEqual(captured.run_path, "task.md")
        self.assertEqual(captured.sha256, "a" * 64)
        self.assertEqual(implementer.agent_name, "codex-main")
        self.assertEqual(implementer.kind.value, "codex")
        self.assertEqual(reviewer.kind.value, "claude")
        self.assertEqual(reviewer.start_args, ("--strict",))

    def test_state_match_errors_name_the_real_json_field(self) -> None:
        with self.assertRaisesRegex(
            runs.RunStateError,
            r"state\.git_common_dir must be an absolute path",
        ):
            runs._assert_matching_state_value(
                "relative",
                Path("/tmp/repository/.git"),
                field="git_common_dir",
                label="Git common directory",
            )

    def test_reviewing_state_requires_an_active_round_record(self) -> None:
        with self.assertRaisesRegex(
            runs.RunStateError,
            "must record an active round",
        ):
            runs._validate_active_state_shape(
                phase=runs.RunPhase.REVIEWING,
                current_round=1,
                current_head_oid="a" * 40,
                approved_head_oid=None,
                active_escalation_id=None,
                active_round=None,
                handoff=None,
                object_format="sha1",
            )

    def test_reviewing_state_returns_the_validated_active_round(self) -> None:
        active_round = _active_round()

        result = runs._validate_active_state_shape(
            phase=runs.RunPhase.REVIEWING,
            current_round=1,
            current_head_oid="a" * 40,
            approved_head_oid=None,
            active_escalation_id=None,
            active_round=active_round,
            handoff=_handoff(),
            object_format="sha1",
        )

        self.assertIs(result, active_round)

    def test_active_state_shape_rejects_inconsistent_relationships(
        self,
    ) -> None:
        oid = "a" * 40
        reviewing_round = _active_round()
        handoff = _handoff()
        valid_reviewing = {
            "phase": runs.RunPhase.REVIEWING,
            "current_round": 1,
            "current_head_oid": oid,
            "approved_head_oid": None,
            "active_escalation_id": None,
            "active_round": reviewing_round,
            "handoff": handoff,
            "object_format": "sha1",
        }
        unused_implementing = {
            **valid_reviewing,
            "phase": runs.RunPhase.IMPLEMENTING,
            "current_round": 0,
            "current_head_oid": None,
            "active_round": None,
            "handoff": None,
        }
        cases = (
            (
                "unused implementing head",
                {**unused_implementing, "current_head_oid": oid},
                "an unused implementing run must have no current round or "
                "requested head",
            ),
            (
                "unused implementing active records",
                {**unused_implementing, "active_round": reviewing_round},
                "an unused implementing run must have no active round or "
                "handoff",
            ),
            (
                "reviewing round number",
                {**valid_reviewing, "current_round": 0},
                "a reviewing run must identify a current round and head",
            ),
            (
                "reviewing approval",
                {**valid_reviewing, "approved_head_oid": "b" * 40},
                "a reviewing run cannot retain approval or active escalation",
            ),
            (
                "reviewing status",
                {
                    **valid_reviewing,
                    "active_round": replace(
                        reviewing_round,
                        status=RoundStatus.APPLIED,
                    ),
                },
                "the active round status must be reviewing while the run is "
                "reviewing",
            ),
            (
                "reviewing result",
                {
                    **valid_reviewing,
                    "active_round": replace(
                        reviewing_round,
                        result_id="87654321-4321-6789-a234-678912345678",
                    ),
                },
                "a reviewing round cannot have an authoritative result ID",
            ),
            (
                "active round number",
                {
                    **valid_reviewing,
                    "active_round": replace(
                        reviewing_round,
                        round_number=2,
                    ),
                },
                "state.active_round.round must match state.current_round",
            ),
            (
                "missing handoff",
                {**valid_reviewing, "handoff": None},
                "a reviewing run must record a handoff",
            ),
            (
                "handoff round number",
                {
                    **valid_reviewing,
                    "handoff": replace(handoff, round_number=2),
                },
                "state.handoff.round must match state.current_round",
            ),
            (
                "handoff target",
                {
                    **valid_reviewing,
                    "handoff": replace(handoff, target="asq-other-reviewer"),
                },
                "state.handoff.target must match the active Reviewer name",
            ),
        )

        for case, arguments, message in cases:
            with self.subTest(case=case):
                with self.assertRaises(runs.RunStateError) as error:
                    runs._validate_active_state_shape(**arguments)
                self.assertEqual(str(error.exception), message)

    def test_implementing_state_requires_a_consistent_closed_round(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            runs.RunStateError,
            "must record a closed active round",
        ):
            runs._validate_active_state_shape(
                phase=runs.RunPhase.IMPLEMENTING,
                current_round=1,
                current_head_oid="a" * 40,
                approved_head_oid=None,
                active_escalation_id=None,
                active_round=_active_round(),
                handoff=_handoff(),
                object_format="sha1",
            )

        cases = (
            (
                None,
                None,
                None,
                "must identify a current head",
            ),
            (
                "a" * 40,
                "b" * 40,
                None,
                "cannot retain approval or active escalation",
            ),
            (
                "a" * 40,
                None,
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "cannot retain approval or active escalation",
            ),
        )
        for current_head, approved_head, escalation_id, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(runs.RunStateError, message):
                    runs._validate_active_state_shape(
                        phase=runs.RunPhase.IMPLEMENTING,
                        current_round=1,
                        current_head_oid=current_head,
                        approved_head_oid=approved_head,
                        active_escalation_id=escalation_id,
                        active_round=_active_round(RoundStatus.APPLIED),
                        handoff=_handoff(),
                        object_format="sha1",
                    )

        result = runs._validate_active_state_shape(
            phase=runs.RunPhase.IMPLEMENTING,
            current_round=1,
            current_head_oid="a" * 40,
            approved_head_oid=None,
            active_escalation_id=None,
            active_round=_active_round(RoundStatus.APPLIED),
            handoff=_handoff(),
            object_format="sha1",
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
