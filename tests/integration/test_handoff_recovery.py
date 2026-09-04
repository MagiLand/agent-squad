from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests._support import SRC_ROOT, run_cli
from tests.integration.test_submissions import (
    _artifacts,
    _commit_candidate,
    _start_run,
)
from tests.integration.test_review_submissions import (
    _BUNDLE_DAMAGE_CASES,
    _damage_reviewer_worktree,
    _prepare_round,
    _write_review,
)

from agent_squad import (  # noqa: E402
    handoffs,
    review_applications,
    review_submissions,
    runs,
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


def _invocations_of(
    environment: dict[str, str],
    *prefix: str,
) -> list[list[str]]:
    """Return real fake-Herdr calls beginning with one command prefix."""

    return [
        arguments
        for arguments in _actual_herdr_invocations(environment)
        if _is_invocation(arguments, *prefix)
    ]


def _is_invocation(arguments: list[str], *prefix: str) -> bool:
    return arguments[:len(prefix)] == list(prefix)


def _assert_single_round(
    test_case: unittest.TestCase,
    run_directory: Path,
) -> None:
    """Assert recovery retained the one authoritative review round."""

    test_case.assertEqual(
        [path.name for path in (run_directory / "rounds").iterdir()],
        ["001"],
    )


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
            _assert_single_round(self, run_directory)

            invocations = _actual_herdr_invocations(environment)
            starts = _invocations_of(environment, "agent", "start")
            prompts = [
                arguments
                for arguments in _invocations_of(
                    environment,
                    "agent",
                    "prompt",
                )
                if arguments[2] == original_round["reviewer_name"]
            ]
            history_reads = _invocations_of(environment, "agent", "read")
            self.assertEqual(len(starts), 1)
            self.assertEqual(len(prompts), 2)
            self.assertEqual(prompts[0][3], prompts[1][3])
            self.assertEqual(len(history_reads), 1)
            prompt_positions = [
                index
                for index, arguments in enumerate(invocations)
                if _is_invocation(arguments, "agent", "prompt")
                and arguments[2] == original_round["reviewer_name"]
            ]
            history_position = next(
                index
                for index, arguments in enumerate(invocations)
                if _is_invocation(arguments, "agent", "read")
            )
            self.assertLess(
                history_position,
                prompt_positions[1],
            )

    def test_retry_prompt_failure_preserves_the_same_recoverable_round(
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
            original_round = original_state["active_round"]

            failed = run_cli(
                repository,
                "retry-handoff",
                data_home=data_home,
                env_overrides=prompt_failure,
            )

            self.assertEqual(failed.returncode, 1)
            self.assertIn("injected prompt failure", failed.stderr)
            self.assertIn(
                "Next action: agent-squad retry-handoff",
                failed.stdout,
            )
            failed_state, _ = _artifacts(repository)
            self.assertEqual(failed_state["active_round"], original_round)
            self.assertEqual(failed_state["handoff"]["status"], "failed")
            _assert_single_round(self, run_directory)
            events = [
                json.loads(line)
                for line in (run_directory / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                events[-1]["event"],
                "review_request_recovery_failed",
            )
            self.assertIsNone(events[-1]["action"])

    def test_retry_handoff_survives_reviewer_bundle_damage(self) -> None:
        for damage in _BUNDLE_DAMAGE_CASES:
            with self.subTest(damage=damage):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    prepared = _prepare_round(Path(temporary_directory))
                    _damage_reviewer_worktree(prepared, damage)

                    recovered = run_cli(
                        prepared.repository,
                        "retry-handoff",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )

                    self.assertEqual(
                        recovered.returncode,
                        0,
                        recovered.stderr,
                    )
                    self.assertIn("Request handoff: sent", recovered.stdout)
                    state, run_directory = _artifacts(prepared.repository)
                    self.assertEqual(state["phase"], "reviewing")
                    self.assertEqual(state["current_round"], 1)
                    self.assertEqual(state["handoff"]["status"], "sent")
                    _assert_single_round(self, run_directory)

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
            _assert_single_round(self, run_directory)

    def test_access_faults_refuse_recovery_without_mutating_evidence(
        self,
    ) -> None:
        for failure_kind in ("filesystem", "git"):
            with self.subTest(failure_kind=failure_kind):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    prepared = _prepare_round(Path(temporary_directory))
                    review = _write_review(prepared)
                    lost_notification = dict(prepared.environment)
                    lost_notification["FAKE_HERDR_FAIL_PROMPT"] = "1"
                    submitted = run_cli(
                        prepared.review_worktree,
                        "review-submit",
                        data_home=prepared.data_home,
                        env_overrides=lost_notification,
                    )
                    self.assertEqual(submitted.returncode, 1)
                    state, run_directory = _artifacts(prepared.repository)
                    round_directory = run_directory / "rounds/001"
                    marker_path = prepared.bundle / "local-state.json"
                    authoritative_ledger = (
                        round_directory / "retired-results.json"
                    )
                    reviewer_ledger = prepared.bundle / "retired-results.json"
                    evidence_paths = (
                        prepared.repository / ".agent-squad/state.json",
                        round_directory / "round.json",
                        run_directory / "run.json",
                        run_directory / "events.jsonl",
                        marker_path,
                        authoritative_ledger,
                        reviewer_ledger,
                    )

                    def snapshot() -> tuple[bytes | None, ...]:
                        return tuple(
                            path.read_bytes() if path.exists() else None
                            for path in evidence_paths
                        )

                    originals = snapshot()
                    prompts_before = _invocations_of(
                        prepared.environment,
                        "agent",
                        "prompt",
                    )
                    message = "simulated Git inspection failure"
                    if failure_kind == "filesystem":
                        request_path = prepared.bundle / "input/request.json"
                        request_path.chmod(0)
                        failure = mock.patch.object(
                            review_submissions,
                            "run_git",
                            wraps=review_submissions.run_git,
                        )
                        message = "Permission denied"
                    else:
                        real_run_git = review_submissions.run_git

                        def fail_evidence_git(start, *arguments):
                            if (
                                Path(start) == prepared.review_worktree
                                and arguments
                                == ("rev-parse", "--show-object-format")
                            ):
                                return SimpleNamespace(
                                    returncode=128,
                                    stdout="",
                                    stderr=message,
                                )
                            return real_run_git(start, *arguments)

                        failure = mock.patch.object(
                            review_submissions,
                            "run_git",
                            side_effect=fail_evidence_git,
                        )

                    try:
                        with failure:
                            status = runs.inspect_status(prepared.repository)
                            active = status.active_run
                            self.assertIsNotNone(active)
                            assert active is not None
                            self.assertIsInstance(
                                active.unapplied_review,
                                runs.UnavailableReviewEvidence,
                            )
                            assert isinstance(
                                active.unapplied_review,
                                runs.UnavailableReviewEvidence,
                            )
                            self.assertIn(
                                message,
                                active.unapplied_review.reason,
                            )
                            self.assertEqual(
                                status.next_action,
                                "agent-squad retry-handoff",
                            )

                            if failure_kind == "filesystem":
                                cli_status = run_cli(
                                    prepared.repository,
                                    "status",
                                    data_home=prepared.data_home,
                                    env_overrides=prepared.environment,
                                )
                                self.assertEqual(
                                    cli_status.returncode,
                                    0,
                                    cli_status.stderr,
                                )
                                self.assertIn(
                                    "Marker-confirmed unapplied result: "
                                    "present but unavailable",
                                    cli_status.stdout,
                                )
                                self.assertIn(message, cli_status.stdout)
                                self.assertNotIn(
                                    "present but invalid",
                                    cli_status.stdout,
                                )

                            with self.assertRaisesRegex(
                                handoffs.HandoffRecoveryError,
                                message,
                            ) as raised:
                                handoffs.retry_handoff(prepared.repository)
                            self.assertNotIn(
                                "invalid",
                                str(raised.exception).lower(),
                            )
                    finally:
                        if failure_kind == "filesystem":
                            request_path.chmod(0o600)

                    self.assertEqual(snapshot(), originals)
                    self.assertFalse(
                        (
                            round_directory
                            / "diagnostics/invalid-results"
                        ).exists()
                    )
                    self.assertEqual(
                        _invocations_of(
                            prepared.environment,
                            "agent",
                            "prompt",
                        ),
                        prompts_before,
                    )

                    recovered = handoffs.retry_handoff(prepared.repository)
                    self.assertIs(
                        recovered.action,
                        handoffs.HandoffRecoveryAction.RESULT_READY,
                    )
                    self.assertEqual(recovered.result_id, review["result_id"])
                    self.assertEqual(
                        _invocations_of(
                            prepared.environment,
                            "agent",
                            "prompt",
                        ),
                        prompts_before,
                    )
                    applied = review_applications.apply_review(
                        prepared.repository,
                        result_id=str(review["result_id"]),
                    )
                    self.assertIs(
                        applied.classification,
                        runs.RoundStatus.APPLIED,
                    )
                    self.assertEqual(state["active_run_id"], applied.run_id)

    def test_history_failure_falls_back_to_reprompting_the_same_request(
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

            recovered = run_cli(
                repository,
                "retry-handoff",
                data_home=data_home,
                env_overrides=history_failure,
            )

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn(
                "Recovery action: re-prompted Reviewer",
                recovered.stdout,
            )
            recovered_state, _ = _artifacts(repository)
            self.assertEqual(recovered_state["current_round"], 1)
            self.assertEqual(
                recovered_state["active_round"]["request_id"],
                request_id,
            )
            self.assertEqual(
                recovered_state["handoff"]["status"],
                "sent",
            )
            reviewer_prompts = [
                arguments
                for arguments in _invocations_of(
                    environment,
                    "agent",
                    "prompt",
                )
                if arguments[2]
                == original_state["active_round"]["reviewer_name"]
            ]
            self.assertEqual(len(reviewer_prompts), 2)
            self.assertEqual(
                len(_invocations_of(environment, "agent", "read")),
                1,
            )
            _assert_single_round(self, run_directory)
            events = [
                json.loads(line)
                for line in (run_directory / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                events[-1]["action"],
                "reprompted",
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
            starts = _invocations_of(environment, "agent", "start")
            history_reads = _invocations_of(environment, "agent", "read")
            reviewer_prompts = [
                arguments
                for arguments in _invocations_of(
                    environment,
                    "agent",
                    "prompt",
                )
                if arguments[2] == original_round["reviewer_name"]
            ]
            self.assertEqual(len(starts), 2)
            self.assertEqual(len(history_reads), 0)
            self.assertEqual(len(reviewer_prompts), 1)
            _assert_single_round(self, run_directory)

    def test_sent_request_relaunches_a_reviewer_who_disappeared(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            original_state, run_directory = _artifacts(prepared.repository)
            original_round = original_state["active_round"]
            reviewer_gone = dict(prepared.environment)
            reviewer_gone["FAKE_HERDR_AGENT_GONE"] = "1"

            recovered = run_cli(
                prepared.repository,
                "retry-handoff",
                data_home=prepared.data_home,
                env_overrides=reviewer_gone,
            )

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn(
                "Recovery action: relaunched Reviewer",
                recovered.stdout,
            )
            recovered_state, _ = _artifacts(prepared.repository)
            self.assertEqual(
                recovered_state["active_run_id"],
                original_state["active_run_id"],
            )
            self.assertEqual(recovered_state["active_round"], original_round)
            self.assertEqual(recovered_state["handoff"]["status"], "sent")
            self.assertEqual(
                len(
                    _invocations_of(
                        prepared.environment,
                        "agent",
                        "start",
                    )
                ),
                2,
            )
            _assert_single_round(self, run_directory)

    def test_invalid_marker_evidence_can_reprompt_the_same_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            _write_review(prepared)
            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            review_json = prepared.bundle / "output/review.json"
            review_markdown = prepared.bundle / "output/review.md"
            marker = prepared.bundle / "local-state.json"
            original_evidence = {
                "review.json": review_json.read_bytes(),
                "review.md": review_markdown.read_bytes(),
                "local-state.json": marker.read_bytes(),
            }
            (prepared.review_worktree / "reviewer-notes.md").write_text(
                "untracked scratch output\n",
                encoding="utf-8",
            )
            original_state, run_directory = _artifacts(prepared.repository)
            original_round = original_state["active_round"]

            status = run_cli(
                prepared.repository,
                "status",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn(
                "Marker-confirmed unapplied result: present but invalid",
                status.stdout,
            )
            self.assertIn(
                "Recovery command: agent-squad retry-handoff",
                status.stdout,
            )
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
            recovered_state, _ = _artifacts(prepared.repository)
            self.assertEqual(recovered_state["active_round"], original_round)
            self.assertEqual(recovered_state["handoff"]["status"], "sent")
            _assert_single_round(self, run_directory)
            repeated = run_cli(
                prepared.repository,
                "retry-handoff",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(
                marker.read_bytes(),
                original_evidence["local-state.json"],
            )

            invalid_results = (
                run_directory
                / "rounds/001/diagnostics/invalid-results"
            )
            diagnostics = list(invalid_results.iterdir())
            self.assertEqual(len(diagnostics), 1)
            diagnostic = diagnostics[0]
            self.assertIn(diagnostic.name, recovered.stdout)
            self.assertIn(diagnostic.name, repeated.stdout)
            for name, content in original_evidence.items():
                self.assertEqual((diagnostic / name).read_bytes(), content)
            validation_error = json.loads(
                (diagnostic / "validation-error.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                validation_error["diagnostic_id"],
                diagnostic.name,
            )
            self.assertEqual(
                validation_error["run_id"],
                original_state["active_run_id"],
            )
            self.assertEqual(validation_error["round"], 1)
            self.assertEqual(
                validation_error["request_id"],
                original_round["request_id"],
            )
            self.assertRegex(
                validation_error["created_at"],
                r"^\d{4}-\d{2}-\d{2}T.*Z$",
            )
            self.assertEqual(
                validation_error["captured_files"],
                ["local-state.json", "review.json", "review.md"],
            )
            self.assertIn(
                "unexpected non-ignored files",
                validation_error["reason"],
            )
            events = [
                json.loads(line)
                for line in (run_directory / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(events[-1]["diagnostic_id"], diagnostic.name)

    def test_malformed_marker_retires_identity_before_corrected_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            original_review = _write_review(prepared)
            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            marker = prepared.bundle / "local-state.json"
            malformed_marker = b'{"schema_version":\n'
            marker.write_bytes(malformed_marker)
            original_state, run_directory = _artifacts(prepared.repository)
            original_round = original_state["active_round"]

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
            self.assertFalse(marker.exists())
            diagnostics = list(
                (
                    run_directory
                    / "rounds/001/diagnostics/invalid-results"
                ).iterdir()
            )
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(
                (diagnostics[0] / "local-state.json").read_bytes(),
                malformed_marker,
            )
            retired = json.loads(
                (prepared.bundle / "retired-results.json").read_text(
                    encoding="utf-8"
                )
            )
            authoritative_ledger = (
                run_directory / "rounds/001/retired-results.json"
            )
            self.assertEqual(
                authoritative_ledger.read_bytes(),
                (prepared.bundle / "retired-results.json").read_bytes(),
            )
            self.assertEqual(
                retired["run_id"],
                original_state["active_run_id"],
            )
            self.assertEqual(retired["round"], 1)
            self.assertEqual(
                retired["request_id"],
                original_round["request_id"],
            )
            self.assertEqual(
                retired["results"],
                [
                    {
                        "retired_at": retired["created_at"],
                        "result_id": original_review["result_id"],
                        "review_sha256": hashlib.sha256(
                            (prepared.bundle / "output/review.json")
                            .read_bytes()
                        ).hexdigest(),
                    }
                ],
            )

            corrected_review = _write_review(
                prepared,
                verdict="changes_requested",
            )
            corrected_review["result_id"] = original_review["result_id"]
            (prepared.bundle / "output/review.json").write_text(
                f"{json.dumps(corrected_review, indent=2)}\n",
                encoding="utf-8",
            )
            rejected = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn(
                "corrected review content needs a new result ID",
                rejected.stderr,
            )
            self.assertFalse(marker.exists())

            corrected_review["result_id"] = (
                "44444444-4444-4444-8444-444444444444"
            )
            (prepared.bundle / "output/review.json").write_text(
                f"{json.dumps(corrected_review, indent=2)}\n",
                encoding="utf-8",
            )
            resubmitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(resubmitted.returncode, 0, resubmitted.stderr)
            self.assertIn(
                "Reviewer-local marker created",
                resubmitted.stdout,
            )
            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(corrected_review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)

            final_state, final_run_directory = _artifacts(
                prepared.repository
            )
            self.assertEqual(final_run_directory, run_directory)
            self.assertEqual(final_state["current_round"], 1)
            self.assertEqual(
                final_state["active_round"]["request_id"],
                original_round["request_id"],
            )
            self.assertEqual(
                final_state["active_round"]["result_id"],
                corrected_review["result_id"],
            )
            _assert_single_round(self, run_directory)

    def test_invalid_marker_recovery_repairs_non_regular_advisory(
        self,
    ) -> None:
        cases = ("directory", "symlink")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    prepared = _prepare_round(root)
                    review = _write_review(prepared)
                    submitted = run_cli(
                        prepared.review_worktree,
                        "review-submit",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )
                    self.assertEqual(
                        submitted.returncode,
                        0,
                        submitted.stderr,
                    )
                    marker = prepared.bundle / "local-state.json"
                    malformed_marker = b'{"schema_version":\n'
                    marker.write_bytes(malformed_marker)
                    advisory = prepared.bundle / "retired-results.json"
                    external = root / "external-advisory"
                    if case == "directory":
                        advisory.mkdir()
                    else:
                        external.write_bytes(b"external\n")
                        advisory.symlink_to(external)

                    original_state, run_directory = _artifacts(
                        prepared.repository
                    )
                    original_round = original_state["active_round"]
                    reviewer_name = str(original_round["reviewer_name"])
                    prompts_before = [
                        arguments
                        for arguments in _invocations_of(
                            prepared.environment,
                            "agent",
                            "prompt",
                        )
                        if arguments[2] == reviewer_name
                    ]

                    recovered = run_cli(
                        prepared.repository,
                        "retry-handoff",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )

                    self.assertEqual(
                        recovered.returncode,
                        0,
                        recovered.stderr,
                    )
                    self.assertIn(
                        "Recovery action: re-prompted Reviewer",
                        recovered.stdout,
                    )
                    self.assertFalse(marker.exists())
                    self.assertTrue(advisory.is_file())
                    self.assertFalse(advisory.is_symlink())
                    authority = (
                        run_directory / "rounds/001/retired-results.json"
                    )
                    self.assertEqual(
                        advisory.read_bytes(),
                        authority.read_bytes(),
                    )
                    if case == "symlink":
                        self.assertEqual(
                            external.read_bytes(),
                            b"external\n",
                        )
                    recovered_state, recovered_run_directory = _artifacts(
                        prepared.repository
                    )
                    self.assertEqual(recovered_run_directory, run_directory)
                    self.assertEqual(
                        recovered_state["active_run_id"],
                        original_state["active_run_id"],
                    )
                    self.assertEqual(recovered_state["current_round"], 1)
                    self.assertEqual(
                        recovered_state["active_round"]["request_id"],
                        original_round["request_id"],
                    )
                    reviewer_prompts = [
                        arguments
                        for arguments in _invocations_of(
                            prepared.environment,
                            "agent",
                            "prompt",
                        )
                        if arguments[2] == reviewer_name
                    ]
                    self.assertEqual(
                        len(reviewer_prompts),
                        len(prompts_before) + 1,
                    )
                    diagnostics = list(
                        (
                            run_directory
                            / "rounds/001/diagnostics/invalid-results"
                        ).iterdir()
                    )
                    self.assertEqual(len(diagnostics), 1)
                    self.assertEqual(
                        (diagnostics[0] / "local-state.json").read_bytes(),
                        malformed_marker,
                    )
                    validation_error = json.loads(
                        (diagnostics[0] / "validation-error.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(
                        validation_error["captured_files"],
                        ["local-state.json", "review.json", "review.md"],
                    )

                    notification_retry = run_cli(
                        prepared.review_worktree,
                        "review-submit",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )
                    self.assertEqual(
                        notification_retry.returncode,
                        0,
                        notification_retry.stderr,
                    )
                    recreated = json.loads(marker.read_text(encoding="utf-8"))
                    self.assertEqual(
                        recreated["result_id"],
                        review["result_id"],
                    )
                    self.assertEqual(
                        authority.read_bytes(),
                        advisory.read_bytes(),
                    )
                    _assert_single_round(self, run_directory)

    def test_apply_rejects_retired_id_after_reviewer_ledger_deletion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            original_review = _write_review(prepared)
            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            marker = prepared.bundle / "local-state.json"
            marker.write_bytes(b'{"schema_version":\n')

            recovered = run_cli(
                prepared.repository,
                "retry-handoff",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertFalse(marker.exists())
            reviewer_ledger = prepared.bundle / "retired-results.json"
            _, run_directory = _artifacts(prepared.repository)
            authoritative_ledger = (
                run_directory / "rounds/001/retired-results.json"
            )
            self.assertTrue(reviewer_ledger.is_file())
            self.assertEqual(
                authoritative_ledger.read_bytes(),
                reviewer_ledger.read_bytes(),
            )
            reviewer_ledger.unlink()
            authoritative_before = authoritative_ledger.read_bytes()

            corrected_review = _write_review(
                prepared,
                verdict="changes_requested",
            )
            corrected_review["result_id"] = original_review["result_id"]
            (prepared.bundle / "output/review.json").write_text(
                f"{json.dumps(corrected_review, indent=2)}\n",
                encoding="utf-8",
            )
            reviewer_accepted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(
                reviewer_accepted.returncode,
                0,
                reviewer_accepted.stderr,
            )
            state_path = prepared.repository / ".agent-squad/state.json"
            state_before = state_path.read_bytes()

            rejected = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(original_review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(rejected.returncode, 1)
            self.assertIn(
                "corrected review content needs a new result ID",
                rejected.stderr,
            )
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(
                authoritative_ledger.read_bytes(),
                authoritative_before,
            )

            recovered_again = run_cli(
                prepared.repository,
                "retry-handoff",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(
                recovered_again.returncode,
                0,
                recovered_again.stderr,
            )
            self.assertFalse(marker.exists(), recovered_again.stdout)
            self.assertTrue(reviewer_ledger.is_file())
            self.assertEqual(
                authoritative_ledger.read_bytes(),
                reviewer_ledger.read_bytes(),
            )

            corrected_review["result_id"] = (
                "44444444-4444-4444-8444-444444444444"
            )
            (prepared.bundle / "output/review.json").write_text(
                f"{json.dumps(corrected_review, indent=2)}\n",
                encoding="utf-8",
            )
            resubmitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(resubmitted.returncode, 0, resubmitted.stderr)
            applied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(corrected_review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)

            final_state, final_run_directory = _artifacts(
                prepared.repository
            )
            self.assertEqual(final_run_directory, run_directory)
            self.assertEqual(final_state["current_round"], 1)
            self.assertEqual(
                final_state["active_round"]["result_id"],
                corrected_review["result_id"],
            )
            _assert_single_round(self, run_directory)

    def test_invalid_reviewer_ledger_cannot_veto_authoritative_result(
        self,
    ) -> None:
        cases = ("malformed", "mismatched", "directory")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    prepared = _prepare_round(Path(temporary_directory))
                    review = _write_review(prepared)
                    submitted = run_cli(
                        prepared.review_worktree,
                        "review-submit",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )
                    self.assertEqual(
                        submitted.returncode,
                        0,
                        submitted.stderr,
                    )
                    marker = prepared.bundle / "local-state.json"
                    marker.write_bytes(b'{"schema_version":\n')
                    recovered = run_cli(
                        prepared.repository,
                        "retry-handoff",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )
                    self.assertEqual(
                        recovered.returncode,
                        0,
                        recovered.stderr,
                    )
                    self.assertFalse(marker.exists())

                    resubmitted = run_cli(
                        prepared.review_worktree,
                        "review-submit",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )
                    self.assertEqual(
                        resubmitted.returncode,
                        0,
                        resubmitted.stderr,
                    )
                    _, run_directory = _artifacts(prepared.repository)
                    authoritative_ledger = (
                        run_directory
                        / "rounds/001/retired-results.json"
                    )
                    authoritative_before = (
                        authoritative_ledger.read_bytes()
                    )
                    reviewer_ledger = (
                        prepared.bundle / "retired-results.json"
                    )
                    if case == "malformed":
                        reviewer_ledger.write_bytes(
                            b'{"schema_version":\n'
                        )
                    elif case == "mismatched":
                        mismatched = json.loads(
                            authoritative_before.decode("utf-8")
                        )
                        mismatched["request_id"] = (
                            "99999999-9999-4999-8999-999999999999"
                        )
                        reviewer_ledger.write_text(
                            f"{json.dumps(mismatched, indent=2)}\n",
                            encoding="utf-8",
                        )
                    else:
                        reviewer_ledger.unlink()
                        reviewer_ledger.mkdir()
                        reviewer_rejected = run_cli(
                            prepared.review_worktree,
                            "review-submit",
                            data_home=prepared.data_home,
                            env_overrides=prepared.environment,
                        )
                        self.assertEqual(reviewer_rejected.returncode, 1)
                        self.assertIn(
                            "retired review identities must be a regular "
                            "non-symlink file",
                            reviewer_rejected.stderr,
                        )
                    advisory_before = (
                        None
                        if case == "directory"
                        else reviewer_ledger.read_bytes()
                    )
                    marker_before = marker.read_bytes()
                    prompts_before = _invocations_of(
                        prepared.environment,
                        "agent",
                        "prompt",
                    )

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
                    self.assertIn(
                        f"Result ID: {review['result_id']}",
                        status.stdout,
                    )

                    rediscovered = run_cli(
                        prepared.repository,
                        "retry-handoff",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )
                    self.assertEqual(
                        rediscovered.returncode,
                        0,
                        rediscovered.stderr,
                    )
                    self.assertIn(
                        "Recovery action: use marker-confirmed result",
                        rediscovered.stdout,
                    )
                    if case == "directory":
                        self.assertTrue(reviewer_ledger.is_dir())
                        self.assertEqual(list(reviewer_ledger.iterdir()), [])
                    else:
                        self.assertEqual(
                            reviewer_ledger.read_bytes(),
                            advisory_before,
                        )
                    self.assertEqual(marker.read_bytes(), marker_before)
                    self.assertEqual(
                        authoritative_ledger.read_bytes(),
                        authoritative_before,
                    )
                    self.assertEqual(
                        _invocations_of(
                            prepared.environment,
                            "agent",
                            "prompt",
                        ),
                        prompts_before,
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
                    final_state, final_run_directory = _artifacts(
                        prepared.repository
                    )
                    self.assertEqual(final_run_directory, run_directory)
                    self.assertEqual(final_state["phase"], "approved")
                    self.assertEqual(
                        final_state["active_round"]["result_id"],
                        review["result_id"],
                    )
                    _assert_single_round(self, run_directory)

    def test_retired_identity_write_failure_keeps_malformed_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            _write_review(prepared)
            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            marker = prepared.bundle / "local-state.json"
            malformed_marker = b'{"schema_version":\n'
            marker.write_bytes(malformed_marker)
            status = runs.inspect_status(prepared.repository)
            active = status.active_run
            self.assertIsNotNone(active)
            assert active is not None
            active_round = active.active_round
            self.assertIsNotNone(active_round)
            assert active_round is not None
            self.assertIsInstance(
                active.unapplied_review,
                runs.InvalidUnappliedReviewResult,
            )
            assert isinstance(
                active.unapplied_review,
                runs.InvalidUnappliedReviewResult,
            )
            control_root = prepared.repository / ".agent-squad"
            request = handoffs._load_active_request(
                control_root,
                run_id=active.run_id,
                active_round=active_round,
            )

            with (
                mock.patch.object(
                    handoffs,
                    "record_retired_review_identity",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(
                    handoffs.HandoffRecoveryError,
                    "could not preserve invalid marker-confirmed review",
                ),
            ):
                handoffs._preserve_invalid_review_evidence(
                    control_root,
                    active=active,
                    active_round=active_round,
                    request=request,
                    invalid_review=active.unapplied_review,
                )

            self.assertEqual(marker.read_bytes(), malformed_marker)
            self.assertFalse(
                (prepared.bundle / "retired-results.json").exists()
            )
            _, run_directory = _artifacts(prepared.repository)
            self.assertFalse(
                (
                    run_directory
                    / "rounds/001/retired-results.json"
                ).exists()
            )
            diagnostics = list(
                (
                    run_directory
                    / "rounds/001/diagnostics/invalid-results"
                ).iterdir()
            )
            self.assertEqual(len(diagnostics), 1)
            self.assertTrue(
                (diagnostics[0] / "validation-error.json").is_file()
            )

    def test_recovery_serializes_marker_retirement_with_review_submit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            review = _write_review(prepared)
            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            marker = prepared.bundle / "local-state.json"
            malformed_marker = b'{"schema_version":\n'
            marker.write_bytes(malformed_marker)

            status = runs.inspect_status(prepared.repository)
            active = status.active_run
            self.assertIsNotNone(active)
            assert active is not None
            active_round = active.active_round
            self.assertIsNotNone(active_round)
            assert active_round is not None
            self.assertIsInstance(
                active.unapplied_review,
                runs.InvalidUnappliedReviewResult,
            )
            assert isinstance(
                active.unapplied_review,
                runs.InvalidUnappliedReviewResult,
            )
            control_root = prepared.repository / ".agent-squad"
            request = handoffs._load_active_request(
                control_root,
                run_id=active.run_id,
                active_round=active_round,
            )
            original_archive = handoffs.archive_invalid_review_evidence
            archive_ready = Event()
            release_recovery = Event()

            def paused_archive(*args: object, **kwargs: object) -> str:
                diagnostic_id = original_archive(*args, **kwargs)
                archive_ready.set()
                if not release_recovery.wait(timeout=10):
                    raise AssertionError("test did not release recovery")
                return diagnostic_id

            child_code = """
from contextlib import contextmanager
from pathlib import Path
import sys
from agent_squad import review_submissions

original_lock = review_submissions.exclusive_file_lock

@contextmanager
def observed_lock(path):
    Path(sys.argv[2]).write_text("attempting\\n", encoding="utf-8")
    with original_lock(path):
        yield

review_submissions.exclusive_file_lock = observed_lock
result = review_submissions.submit_review_result(Path(sys.argv[1]))
print(result.marker_created)
"""
            environment = os.environ.copy()
            existing_python_path = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                str(SRC_ROOT)
                if not existing_python_path
                else os.pathsep.join(
                    (str(SRC_ROOT), existing_python_path)
                )
            )
            environment["XDG_DATA_HOME"] = str(prepared.data_home)
            environment.update(prepared.environment)
            lock_attempted = root / "review-submit-lock-attempted"
            process: subprocess.Popen[str] | None = None
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    with mock.patch.object(
                        handoffs,
                        "archive_invalid_review_evidence",
                        paused_archive,
                    ):
                        future = executor.submit(
                            handoffs._preserve_invalid_review_evidence,
                            control_root,
                            active=active,
                            active_round=active_round,
                            request=request,
                            invalid_review=active.unapplied_review,
                        )
                        try:
                            self.assertTrue(archive_ready.wait(timeout=10))
                            process = subprocess.Popen(
                                [
                                    sys.executable,
                                    "-c",
                                    child_code,
                                    str(prepared.review_worktree),
                                    str(lock_attempted),
                                ],
                                cwd=prepared.review_worktree,
                                env=environment,
                                text=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                            )
                            deadline = time.monotonic() + 10
                            while (
                                not lock_attempted.exists()
                                and process.poll() is None
                                and time.monotonic() < deadline
                            ):
                                time.sleep(0.01)
                            self.assertTrue(lock_attempted.exists())
                            self.assertIsNone(process.poll())
                            self.assertEqual(
                                marker.read_bytes(),
                                malformed_marker,
                            )
                            self.assertFalse(
                                (
                                    prepared.bundle
                                    / "retired-results.json"
                                ).exists()
                            )
                            _, run_directory = _artifacts(
                                prepared.repository
                            )
                            authoritative_ledger = (
                                run_directory
                                / "rounds/001/retired-results.json"
                            )
                            self.assertFalse(
                                authoritative_ledger.exists()
                            )
                            diagnostics = list(
                                (
                                    run_directory
                                    / "rounds/001/diagnostics/invalid-results"
                                ).iterdir()
                            )
                            self.assertEqual(len(diagnostics), 1)
                            before_release = {
                                path.name: path.read_bytes()
                                for path in diagnostics[0].iterdir()
                            }
                        finally:
                            release_recovery.set()

                        diagnostic_id = future.result(timeout=10)
                        assert process is not None
                        stdout, stderr = process.communicate(timeout=10)

                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout.strip(), "True")
                self.assertTrue(marker.exists())
                self.assertTrue(
                    (prepared.bundle / "retired-results.json").exists()
                )
                self.assertEqual(
                    authoritative_ledger.read_bytes(),
                    (
                        prepared.bundle / "retired-results.json"
                    ).read_bytes(),
                )
                self.assertEqual(diagnostics[0].name, diagnostic_id)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in diagnostics[0].iterdir()
                    },
                    before_release,
                )
                ready = runs.inspect_status(prepared.repository).active_run
                self.assertIsNotNone(ready)
                assert ready is not None
                self.assertIsInstance(
                    ready.unapplied_review,
                    runs.UnappliedReviewResult,
                )
                assert isinstance(
                    ready.unapplied_review,
                    runs.UnappliedReviewResult,
                )
                self.assertEqual(
                    ready.unapplied_review.result_id,
                    review["result_id"],
                )
                _assert_single_round(self, run_directory)
            finally:
                release_recovery.set()
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate()

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
                    _is_invocation(arguments, "agent", "read")
                    for arguments in new_invocations
                )
            )
            self.assertFalse(
                any(
                    _is_invocation(arguments, "agent", "prompt")
                    for arguments in new_invocations
                )
            )
            _assert_single_round(self, run_directory)

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
            _assert_single_round(self, run_directory)
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
            reviewer_prompts = [
                arguments
                for arguments in _invocations_of(
                    prepared.environment,
                    "agent",
                    "prompt",
                )
                if arguments[2] == original_round["reviewer_name"]
            ]
            self.assertEqual(len(reviewer_prompts), 3)


if __name__ == "__main__":
    unittest.main()
