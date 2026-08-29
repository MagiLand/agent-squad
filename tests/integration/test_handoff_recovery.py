from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests._support import run_cli
from tests.integration.test_submissions import (
    _artifacts,
    _commit_candidate,
    _start_run,
)
from tests.integration.test_review_submissions import (
    _prepare_round,
    _write_review,
)


def _actual_herdr_invocations(environment: dict[str, str]) -> list[list[str]]:
    events = [
        json.loads(line)
        for line in (
            Path(environment["FAKE_HERDR_STATE_DIR"])
            / "invocations.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    return [
        event["arguments"]
        for event in events
        if event["arguments"]
        not in (
            ["agent", "start", "--help"],
            ["agent", "prompt", "--help"],
            ["agent", "get", "--help"],
            ["agent", "read", "--help"],
        )
    ]


class ReviewHandoffRecoveryTests(unittest.TestCase):
    def test_prompt_failure_reprompts_the_same_reviewer_and_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            _commit_candidate(repository)
            failing_environment = dict(environment)
            failing_environment["FAKE_HERDR_FAIL_PROMPT"] = "1"

            submitted = run_cli(
                repository,
                "submit",
                "--report",
                str(report),
                "--mode",
                "new_revision",
                data_home=data_home,
                env_overrides=failing_environment,
            )
            self.assertEqual(submitted.returncode, 1)
            self.assertIn(
                "Next action: agent-squad retry-handoff",
                submitted.stdout,
            )
            original_state, run_directory = _artifacts(repository)
            original_round = original_state["active_round"]
            review_worktree = Path(original_round["review_worktree"])
            request_path = run_directory / "rounds/001/request.json"
            request_bytes = request_path.read_bytes()
            bundle_request = (
                review_worktree
                / ".agent-squad-review/input/request.json"
            )
            bundle_bytes = bundle_request.read_bytes()

            recovered = run_cli(
                repository,
                "retry-handoff",
                data_home=data_home,
                env_overrides=environment,
            )

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn("Request handoff: sent", recovered.stdout)
            self.assertIn(
                "Recovery action: re-prompted Reviewer",
                recovered.stdout,
            )
            recovered_state, recovered_run_directory = _artifacts(repository)
            self.assertEqual(
                recovered_state["active_run_id"],
                original_state["active_run_id"],
            )
            self.assertEqual(recovered_state["current_round"], 1)
            self.assertEqual(recovered_state["active_round"], original_round)
            self.assertEqual(recovered_state["handoff"]["status"], "sent")
            self.assertEqual(
                recovered_state["handoff"]["target"],
                original_round["reviewer_name"],
            )
            self.assertEqual(recovered_run_directory, run_directory)
            self.assertEqual(request_path.read_bytes(), request_bytes)
            self.assertEqual(bundle_request.read_bytes(), bundle_bytes)
            self.assertEqual(
                [path.name for path in (run_directory / "rounds").iterdir()],
                ["001"],
            )

            invocations = _actual_herdr_invocations(environment)
            starts = [
                arguments
                for arguments in invocations
                if arguments[:2] == ["agent", "start"]
            ]
            prompts = [
                arguments
                for arguments in invocations
                if arguments[:2] == ["agent", "prompt"]
                and arguments[2] == original_round["reviewer_name"]
            ]
            history_reads = [
                arguments
                for arguments in invocations
                if arguments[:2] == ["agent", "read"]
            ]
            self.assertEqual(len(starts), 1)
            self.assertEqual(len(prompts), 2)
            self.assertEqual(prompts[0][3], prompts[1][3])
            self.assertEqual(len(history_reads), 1)
            prompt_positions = [
                index
                for index, arguments in enumerate(invocations)
                if arguments[:2] == ["agent", "prompt"]
                and arguments[2] == original_round["reviewer_name"]
            ]
            history_position = next(
                index
                for index, arguments in enumerate(invocations)
                if arguments[:2] == ["agent", "read"]
            )
            self.assertLess(
                history_position,
                prompt_positions[1],
            )

    def test_lost_result_notification_uses_marker_without_herdr_probe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            review = _write_review(prepared)
            failing_environment = dict(prepared.environment)
            failing_environment["FAKE_HERDR_FAIL_PROMPT"] = "1"
            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=failing_environment,
            )
            self.assertEqual(submitted.returncode, 1)
            state_path = prepared.repository / ".agent-squad/state.json"
            state_bytes = state_path.read_bytes()
            invocations_before = _actual_herdr_invocations(
                prepared.environment
            )

            for _ in range(2):
                recovered = run_cli(
                    prepared.repository,
                    "retry-handoff",
                    data_home=prepared.data_home,
                    env_overrides=failing_environment,
                )

                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertIn(
                    "Recovery action: use marker-confirmed result",
                    recovered.stdout,
                )
                self.assertIn(
                    f"Result ID: {review['result_id']}",
                    recovered.stdout,
                )
                self.assertIn(
                    "Next action: agent-squad apply-review --result-id "
                    f"{review['result_id']}",
                    recovered.stdout,
                )

            self.assertEqual(state_path.read_bytes(), state_bytes)
            self.assertEqual(
                _actual_herdr_invocations(prepared.environment),
                invocations_before,
            )

    def test_status_marks_unmarked_output_incomplete_and_recoverable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            review = _write_review(prepared)

            status = run_cli(
                prepared.repository,
                "status",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Phase: reviewing", status.stdout)
            self.assertIn("Round status: reviewing", status.stdout)
            self.assertIn(
                "Marker-confirmed unapplied result: none",
                status.stdout,
            )
            self.assertIn(
                "Unmarked review output: present and incomplete",
                status.stdout,
            )
            self.assertNotIn("Apply command:", status.stdout)
            self.assertIn(
                "Next action: agent-squad retry-handoff",
                status.stdout,
            )

            recovered = run_cli(
                prepared.repository,
                "retry-handoff",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn(
                "Recovery action: re-prompted Reviewer",
                recovered.stdout,
            )
            self.assertEqual(
                json.loads(
                    (prepared.bundle / "output/review.json").read_text(
                        encoding="utf-8"
                    )
                )["result_id"],
                review["result_id"],
            )
            state, run_directory = _artifacts(prepared.repository)
            self.assertEqual(state["current_round"], 1)
            self.assertEqual(
                [path.name for path in (run_directory / "rounds").iterdir()],
                ["001"],
            )

    def test_history_failure_sends_no_prompt_and_allows_an_exact_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            _commit_candidate(repository)
            prompt_failure = dict(environment)
            prompt_failure["FAKE_HERDR_FAIL_PROMPT"] = "1"
            submitted = run_cli(
                repository,
                "submit",
                "--report",
                str(report),
                "--mode",
                "new_revision",
                data_home=data_home,
                env_overrides=prompt_failure,
            )
            self.assertEqual(submitted.returncode, 1)
            original_state, run_directory = _artifacts(repository)
            request_id = original_state["active_round"]["request_id"]
            history_failure = dict(environment)
            history_failure["FAKE_HERDR_FAIL_HISTORY"] = "1"

            failed = run_cli(
                repository,
                "retry-handoff",
                data_home=data_home,
                env_overrides=history_failure,
            )

            self.assertEqual(failed.returncode, 1)
            self.assertIn("injected history failure", failed.stderr)
            self.assertIn(
                "Next action: agent-squad retry-handoff",
                failed.stdout,
            )
            failed_state, _ = _artifacts(repository)
            self.assertEqual(failed_state["current_round"], 1)
            self.assertEqual(
                failed_state["active_round"]["request_id"],
                request_id,
            )
            self.assertEqual(failed_state["handoff"]["status"], "failed")
            invocations = _actual_herdr_invocations(environment)
            reviewer_prompts = [
                arguments
                for arguments in invocations
                if arguments[:2] == ["agent", "prompt"]
                and arguments[2]
                == original_state["active_round"]["reviewer_name"]
            ]
            self.assertEqual(len(reviewer_prompts), 1)

            status = run_cli(
                repository,
                "status",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn(
                "Next action: agent-squad retry-handoff",
                status.stdout,
            )

            recovered = run_cli(
                repository,
                "retry-handoff",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            recovered_state, _ = _artifacts(repository)
            self.assertEqual(recovered_state["current_round"], 1)
            self.assertEqual(
                recovered_state["active_round"]["request_id"],
                request_id,
            )
            self.assertEqual(
                [path.name for path in (run_directory / "rounds").iterdir()],
                ["001"],
            )

    def test_start_failure_relaunches_the_deterministic_reviewer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            _commit_candidate(repository)
            start_failure = dict(environment)
            start_failure["FAKE_HERDR_FAIL_START"] = "1"

            submitted = run_cli(
                repository,
                "submit",
                "--report",
                str(report),
                "--mode",
                "new_revision",
                data_home=data_home,
                env_overrides=start_failure,
            )
            self.assertEqual(submitted.returncode, 1)
            original_state, run_directory = _artifacts(repository)
            original_round = original_state["active_round"]

            recovered = run_cli(
                repository,
                "retry-handoff",
                data_home=data_home,
                env_overrides=environment,
            )

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn(
                "Recovery action: relaunched Reviewer",
                recovered.stdout,
            )
            recovered_state, _ = _artifacts(repository)
            self.assertEqual(
                recovered_state["active_run_id"],
                original_state["active_run_id"],
            )
            self.assertEqual(recovered_state["active_round"], original_round)
            self.assertEqual(recovered_state["handoff"]["status"], "sent")
            invocations = _actual_herdr_invocations(environment)
            starts = [
                arguments
                for arguments in invocations
                if arguments[:2] == ["agent", "start"]
            ]
            history_reads = [
                arguments
                for arguments in invocations
                if arguments[:2] == ["agent", "read"]
            ]
            reviewer_prompts = [
                arguments
                for arguments in invocations
                if arguments[:2] == ["agent", "prompt"]
                and arguments[2] == original_round["reviewer_name"]
            ]
            self.assertEqual(len(starts), 2)
            self.assertEqual(len(history_reads), 0)
            self.assertEqual(len(reviewer_prompts), 1)
            self.assertEqual(
                [path.name for path in (run_directory / "rounds").iterdir()],
                ["001"],
            )

    def test_history_adopts_a_delivered_request_whose_state_is_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            _commit_candidate(repository)
            submitted = run_cli(
                repository,
                "submit",
                "--report",
                str(report),
                "--mode",
                "new_revision",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            state_path = repository / ".agent-squad/state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            original_round = state["active_round"]
            state["handoff"]["status"] = "pending"
            state_path.write_text(
                json.dumps(state, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            _, run_directory = _artifacts(repository)
            events_path = run_directory / "events.jsonl"
            event_lines = events_path.read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(
                json.loads(event_lines[-1])["event"],
                "review_request_sent",
            )
            events_path.write_text(
                "\n".join(event_lines[:-1]) + "\n",
                encoding="utf-8",
            )
            invocations_before = _actual_herdr_invocations(environment)

            recovered = run_cli(
                repository,
                "retry-handoff",
                data_home=data_home,
                env_overrides=environment,
            )

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn(
                "Recovery action: adopted existing request",
                recovered.stdout,
            )
            recovered_state, recovered_run_directory = _artifacts(repository)
            self.assertEqual(recovered_run_directory, run_directory)
            self.assertEqual(recovered_state["active_round"], original_round)
            self.assertEqual(recovered_state["handoff"]["status"], "sent")
            new_invocations = _actual_herdr_invocations(environment)[
                len(invocations_before):
            ]
            self.assertTrue(
                any(
                    arguments[:2] == ["agent", "read"]
                    for arguments in new_invocations
                )
            )
            self.assertFalse(
                any(
                    arguments[:2] == ["agent", "prompt"]
                    for arguments in new_invocations
                )
            )
            self.assertEqual(
                [path.name for path in (run_directory / "rounds").iterdir()],
                ["001"],
            )

    def test_repeated_recovery_and_notifications_keep_one_logical_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            original_state, run_directory = _artifacts(prepared.repository)
            original_round = original_state["active_round"]

            for _ in range(2):
                recovered = run_cli(
                    prepared.repository,
                    "retry-handoff",
                    data_home=prepared.data_home,
                    env_overrides=prepared.environment,
                )
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertIn(
                    "Recovery action: re-prompted Reviewer",
                    recovered.stdout,
                )

            review = _write_review(prepared)
            marker_path = prepared.bundle / "local-state.json"
            marker_bytes: bytes | None = None
            for attempt in range(2):
                submitted = run_cli(
                    prepared.review_worktree,
                    "review-submit",
                    data_home=prepared.data_home,
                    env_overrides=prepared.environment,
                )
                self.assertEqual(submitted.returncode, 0, submitted.stderr)
                self.assertIn(
                    "Reviewer-local marker "
                    + ("created" if attempt == 0 else "reused"),
                    submitted.stdout,
                )
                if marker_bytes is None:
                    marker_bytes = marker_path.read_bytes()
                else:
                    self.assertEqual(marker_path.read_bytes(), marker_bytes)

            rediscovered = run_cli(
                prepared.repository,
                "retry-handoff",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(rediscovered.returncode, 0, rediscovered.stderr)
            self.assertIn(
                "Recovery action: use marker-confirmed result",
                rediscovered.stdout,
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

            final_state, _ = _artifacts(prepared.repository)
            self.assertEqual(final_state["current_round"], 1)
            self.assertEqual(
                final_state["active_round"]["request_id"],
                original_round["request_id"],
            )
            self.assertEqual(
                final_state["active_round"]["result_id"],
                review["result_id"],
            )
            self.assertEqual(
                [path.name for path in (run_directory / "rounds").iterdir()],
                ["001"],
            )
            approval = json.loads(
                (run_directory / "rounds/001/approval.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                approval["request_id"],
                original_round["request_id"],
            )
            self.assertEqual(approval["result_id"], review["result_id"])
            invocations = _actual_herdr_invocations(prepared.environment)
            reviewer_prompts = [
                arguments
                for arguments in invocations
                if arguments[:2] == ["agent", "prompt"]
                and arguments[2] == original_round["reviewer_name"]
            ]
            self.assertEqual(len(reviewer_prompts), 3)


if __name__ == "__main__":
    unittest.main()
