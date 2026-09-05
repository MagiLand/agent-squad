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

from agent_squad import (  # noqa: E402
    review_applications,
    review_submissions,
    runs,
    submissions,
)


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


def _marker_confirmed_retired_review(root: Path):
    prepared = _prepare_round(root)
    review = _write_review(prepared)
    submitted = run_cli(
        prepared.review_worktree,
        "review-submit",
        data_home=prepared.data_home,
        env_overrides=prepared.environment,
    )
    if submitted.returncode != 0:
        raise AssertionError(submitted.stderr)
    marker = prepared.bundle / "local-state.json"
    marker.write_bytes(b'{"schema_version":\n')
    recovered = run_cli(
        prepared.repository,
        "retry-handoff",
        data_home=prepared.data_home,
        env_overrides=prepared.environment,
    )
    if recovered.returncode != 0:
        raise AssertionError(recovered.stderr)
    resubmitted = run_cli(
        prepared.review_worktree,
        "review-submit",
        data_home=prepared.data_home,
        env_overrides=prepared.environment,
    )
    if resubmitted.returncode != 0:
        raise AssertionError(resubmitted.stderr)
    return prepared, review


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


def _prepare_correction_round(root: Path):
    prepared, review = _marker_confirmed_review(
        root,
        verdict="changes_requested",
    )
    applied = review_applications.apply_review(
        prepared.repository,
        result_id=str(review["result_id"]),
    )
    if applied.classification is not runs.RoundStatus.APPLIED:
        raise AssertionError(f"first review was not applied: {applied}")

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
    if submitted.returncode != 0:
        raise AssertionError(submitted.stderr)
    state = json.loads(
        (prepared.repository / ".agent-squad/state.json").read_text(
            encoding="utf-8"
        )
    )
    review_worktree = Path(state["active_round"]["review_worktree"])
    bundle = review_worktree / ".agent-squad-review"
    request = json.loads(
        (bundle / "input/request.json").read_text(encoding="utf-8")
    )
    return replace(
        prepared,
        review_worktree=review_worktree,
        bundle=bundle,
        request=request,
    )


def _assert_invalid_review_apply(
    test_case: unittest.TestCase,
    prepared,
    review: dict[str, object],
    *,
    expected_reason: str,
    round_number: int = 1,
    completed_change_reviews: int = 0,
) -> None:
    applied = run_cli(
        prepared.repository,
        "apply-review",
        "--result-id",
        str(review["result_id"]),
        data_home=prepared.data_home,
        env_overrides=prepared.environment,
    )

    test_case.assertEqual(applied.returncode, 0, applied.stderr)
    test_case.assertIn("classified invalid", applied.stdout)
    test_case.assertIn(expected_reason, applied.stdout)
    control_root = prepared.repository / ".agent-squad"
    state = json.loads(
        (control_root / "state.json").read_text(encoding="utf-8")
    )
    round_directory = (
        control_root
        / "runs"
        / str(state["active_run_id"])
        / "rounds"
        / f"{round_number:03d}"
    )
    round_record = json.loads(
        (round_directory / "round.json").read_text(encoding="utf-8")
    )
    test_case.assertEqual(state["phase"], "implementing")
    test_case.assertEqual(state["active_round"]["status"], "invalid")
    test_case.assertEqual(
        state["review_budget"]["completed_change_reviews"],
        completed_change_reviews,
    )
    test_case.assertEqual(round_record["artifacts"]["bundle_archive"], [])
    test_case.assertFalse((round_directory / "bundle").exists())


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

    def test_invalid_marker_evidence_is_visible_then_classified(self) -> None:
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
                "Recovery command: agent-squad retry-handoff",
                status.stdout,
            )
            self.assertIn(
                "Next action: agent-squad retry-handoff",
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
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn("classified invalid", applied.stdout)
            self.assertIn(
                "review worktree contains tracked changes or unexpected",
                applied.stdout,
            )
            classified_state = json.loads(
                (
                    prepared.repository / ".agent-squad/state.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(classified_state["phase"], "implementing")
            self.assertEqual(
                classified_state["active_round"]["status"],
                "invalid",
            )

    def test_evidence_access_failures_leave_the_result_retryable(self) -> None:
        for failure_kind in (
            "filesystem",
            "git object format",
            "base existence",
            "base type",
        ):
            with self.subTest(failure_kind=failure_kind):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    prepared, review = _marker_confirmed_review(root)
                    control_root = prepared.repository / ".agent-squad"
                    state_path = control_root / "state.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    run_directory = (
                        control_root
                        / "runs"
                        / str(state["active_run_id"])
                    )
                    round_path = run_directory / "rounds/001/round.json"
                    run_path = run_directory / "run.json"
                    events_path = run_directory / "events.jsonl"
                    marker_path = prepared.bundle / "local-state.json"
                    originals = (
                        state_path.read_bytes(),
                        round_path.read_bytes(),
                        run_path.read_bytes(),
                        events_path.read_bytes(),
                        marker_path.read_bytes(),
                    )

                    if failure_kind == "filesystem":
                        request_path = prepared.bundle / "input/request.json"
                        real_read_bytes = Path.read_bytes

                        def fail_evidence_read(path):
                            if path == request_path:
                                raise PermissionError(
                                    "simulated evidence read failure"
                                )
                            return real_read_bytes(path)

                        failure = mock.patch.object(
                            Path,
                            "read_bytes",
                            new=fail_evidence_read,
                        )
                        message = "simulated evidence read failure"
                    else:
                        real_run_git = review_submissions.run_git
                        if failure_kind == "git object format":
                            failing_arguments = (
                                "rev-parse",
                                "--show-object-format",
                            )
                            message = "simulated Git inspection failure"
                        elif failure_kind == "base existence":
                            failing_arguments = (
                                "cat-file",
                                "-e",
                                str(prepared.request["base_oid"]),
                            )
                            message = "simulated base existence failure"
                        else:
                            failing_arguments = (
                                "cat-file",
                                "-t",
                                str(prepared.request["base_oid"]),
                            )
                            message = "simulated base type failure"

                        def fail_evidence_git(start, *arguments):
                            if (
                                Path(start) == prepared.review_worktree
                                and arguments == failing_arguments
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
                    with failure, self.assertRaisesRegex(
                        review_applications.ReviewApplicationError,
                        message,
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
                            events_path.read_bytes(),
                            marker_path.read_bytes(),
                        ),
                        originals,
                    )
                    self.assertFalse(
                        (
                            round_path.parent
                            / "diagnostics/invalid-results"
                        ).exists()
                    )

                    retried = review_applications.apply_review(
                        prepared.repository,
                        result_id=str(review["result_id"]),
                    )

                    self.assertIs(
                        retried.classification,
                        runs.RoundStatus.APPLIED,
                    )
                    self.assertIs(retried.verdict, runs.ReviewVerdict.APPROVED)
                    self.assertFalse(retried.replayed)

    def test_authoritative_manifest_access_failure_leaves_result_retryable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            round_path = run_directory / "rounds/001/round.json"
            run_path = run_directory / "run.json"
            events_path = run_directory / "events.jsonl"
            originals = (
                state_path.read_bytes(),
                round_path.read_bytes(),
                run_path.read_bytes(),
                events_path.read_bytes(),
            )
            real_read_bytes = Path.read_bytes

            def fail_manifest_read(path):
                if path.resolve() == round_path.resolve():
                    raise PermissionError("simulated manifest read failure")
                return real_read_bytes(path)

            with (
                mock.patch.object(
                    Path,
                    "read_bytes",
                    new=fail_manifest_read,
                ),
                self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "simulated manifest read failure",
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
                    events_path.read_bytes(),
                ),
                originals,
            )
            self.assertFalse(
                (round_path.parent / "diagnostics/invalid-results").exists()
            )

            retried = review_applications.apply_review(
                prepared.repository,
                result_id=str(review["result_id"]),
            )

            self.assertIs(retried.classification, runs.RoundStatus.APPLIED)
            self.assertIs(retried.verdict, runs.ReviewVerdict.APPROVED)
            self.assertFalse(retried.replayed)

    def test_apply_rejects_request_input_changed_after_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            request_path = prepared.bundle / "input/request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["created_at"] = "2026-08-26T11:59:00Z"
            request_path.chmod(0o600)
            request_path.write_bytes(review_applications.encode_json(request))

            _assert_invalid_review_apply(
                self,
                prepared,
                review,
                expected_reason="input/request.json",
            )

    def test_apply_rejects_request_input_missing_after_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            (prepared.bundle / "input/request.json").unlink()

            _assert_invalid_review_apply(
                self,
                prepared,
                review,
                expected_reason="review request",
            )

    def test_apply_rejects_changed_or_missing_base_authority_inputs(
        self,
    ) -> None:
        cases = (
            ("task", "input/task.md"),
            ("implementation report", "input/implementation-report.md"),
            ("explicit context", "input/context/001/context.md"),
        )
        for label, expected_path in cases:
            for damage in ("paired tampering", "missing"):
                with self.subTest(input=label, damage=damage):
                    with tempfile.TemporaryDirectory() as temporary_directory:
                        root = Path(temporary_directory)
                        prepared = _prepare_round(root, with_context=True)
                        review = _submit_with_lost_notification(prepared)
                        request_path = prepared.bundle / "input/request.json"
                        request = json.loads(
                            request_path.read_text(encoding="utf-8")
                        )
                        if label == "task":
                            artifact = request["task"]
                        elif label == "implementation report":
                            artifact = request["implementation_report"]
                        else:
                            artifact = request["context_files"][0]
                        target = prepared.bundle.joinpath(*Path(
                            artifact["path"]
                        ).parts)

                        if damage == "paired tampering":
                            changed = f"changed {label}\n".encode("utf-8")
                            target.chmod(0o600)
                            target.write_bytes(changed)
                            artifact["sha256"] = hashlib.sha256(
                                changed
                            ).hexdigest()
                            request_path.chmod(0o600)
                            request_path.write_bytes(
                                review_applications.encode_json(request)
                            )
                        else:
                            target.unlink()
                            if label == "explicit context":
                                target.parent.rmdir()
                                target.parent.parent.rmdir()
                                request["context_files"] = []
                                request_path.chmod(0o600)
                                request_path.write_bytes(
                                    review_applications.encode_json(request)
                                )

                        _assert_invalid_review_apply(
                            self,
                            prepared,
                            review,
                            expected_reason=expected_path,
                        )

    def test_valid_manifest_bound_bundle_is_applied_and_archived(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root, with_context=True)
            review = _submit_with_lost_notification(prepared)
            expected_inputs = {
                path.relative_to(prepared.bundle).as_posix(): path.read_bytes()
                for path in (prepared.bundle / "input").rglob("*")
                if path.is_file()
            }

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
            round_record = json.loads(
                (round_directory / "round.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["phase"], "approved")
            self.assertEqual(
                {
                    item["path"]
                    for item in round_record["artifacts"]["bundle_inputs"]
                },
                set(expected_inputs),
            )
            for path, expected in expected_inputs.items():
                with self.subTest(path=path):
                    self.assertEqual(
                        (round_directory / "bundle" / path).read_bytes(),
                        expected,
                    )

    def test_apply_rejects_changed_or_missing_prior_authority_inputs(
        self,
    ) -> None:
        cases = (
            ("previous review", "input/previous-review.json"),
            ("previous response", "input/previous-response.json"),
        )
        for label, expected_path in cases:
            for damage in ("semantic tampering", "missing"):
                with self.subTest(input=label, damage=damage):
                    with tempfile.TemporaryDirectory() as temporary_directory:
                        root = Path(temporary_directory)
                        prepared = _prepare_correction_round(root)
                        target = prepared.bundle.joinpath(
                            *Path(expected_path).parts
                        )
                        if damage == "semantic tampering":
                            value = json.loads(
                                target.read_text(encoding="utf-8")
                            )
                            if label == "previous review":
                                value["summary"] = "Changed review summary."
                            else:
                                value["responses"][0]["rationale"] = (
                                    "Changed response rationale."
                                )
                            target.chmod(0o600)
                            target.write_bytes(
                                review_applications.encode_json(value)
                            )
                            review = _submit_with_lost_notification(prepared)
                        else:
                            review = _submit_with_lost_notification(prepared)
                            target.unlink()

                        _assert_invalid_review_apply(
                            self,
                            prepared,
                            review,
                            expected_reason=expected_path,
                            round_number=2,
                            completed_change_reviews=1,
                        )

    def test_missing_base_object_is_classified_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            real_run_git = review_submissions.run_git
            missing_arguments = (
                "cat-file",
                "-e",
                str(prepared.request["base_oid"]),
            )

            def report_missing_base(start, *arguments):
                if (
                    Path(start) == prepared.review_worktree
                    and arguments == missing_arguments
                ):
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="",
                    )
                return real_run_git(start, *arguments)

            with mock.patch.object(
                review_submissions,
                "run_git",
                side_effect=report_missing_base,
            ):
                applied = review_applications.apply_review(
                    prepared.repository,
                    result_id=str(review["result_id"]),
                )

            self.assertIs(applied.classification, runs.RoundStatus.INVALID)
            self.assertIsNone(applied.verdict)
            self.assertIn(
                "review request base object is not an available commit",
                str(applied.reason),
            )
            state = json.loads(
                (
                    prepared.repository / ".agent-squad/state.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(state["phase"], "implementing")
            self.assertEqual(state["active_round"]["status"], "invalid")

    def test_invalid_authoritative_retired_ledger_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_retired_review(root)
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            round_directory = run_directory / "rounds/001"
            round_path = round_directory / "round.json"
            run_path = run_directory / "run.json"
            events_path = run_directory / "events.jsonl"
            authority = round_directory / "retired-results.json"
            authority.write_bytes(b'{"schema_version":\n')
            diagnostics_root = round_directory / "diagnostics/invalid-results"
            diagnostics_before = {
                path.relative_to(diagnostics_root): path.read_bytes()
                for path in diagnostics_root.rglob("*")
                if path.is_file()
            }
            originals = (
                state_path.read_bytes(),
                round_path.read_bytes(),
                run_path.read_bytes(),
                events_path.read_bytes(),
            )

            with self.assertRaisesRegex(
                review_applications.ReviewApplicationError,
                "authoritative retired review identities are invalid",
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
                    events_path.read_bytes(),
                ),
                originals,
            )
            self.assertEqual(
                {
                    path.relative_to(diagnostics_root): path.read_bytes()
                    for path in diagnostics_root.rglob("*")
                    if path.is_file()
                },
                diagnostics_before,
            )

    def test_active_round_replay_uses_historical_authority(self) -> None:
        for verdict in ("approved", "changes_requested"):
            with self.subTest(verdict=verdict):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    prepared, review = _marker_confirmed_review(
                        Path(temporary_directory),
                        verdict=verdict,
                    )
                    first = review_applications.apply_review(
                        prepared.repository,
                        result_id=str(review["result_id"]),
                    )
                    self.assertFalse(first.replayed)
                    historical_replay = (
                        review_applications._historical_result_replay
                    )

                    with mock.patch.object(
                        review_applications,
                        "_historical_result_replay",
                        wraps=historical_replay,
                    ) as historical:
                        replayed = review_applications.apply_review(
                            prepared.repository,
                        )

                    historical.assert_called_once()
                    self.assertTrue(replayed.replayed)
                    self.assertIs(
                        replayed.classification,
                        runs.RoundStatus.APPLIED,
                    )
                    self.assertIs(
                        replayed.verdict,
                        runs.ReviewVerdict(verdict),
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
            "changed implementation branch",
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
                    else:
                        run(
                            ["git", "switch", "-c", "other-branch"],
                            cwd=prepared.repository,
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

    def test_advanced_implementation_head_is_stale_and_replays_safely(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            original_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            run_id = str(original_state["active_run_id"])
            round_directory = (
                control_root / "runs" / run_id / "rounds/001"
            )
            round_path = round_directory / "round.json"
            events_path = control_root / "runs" / run_id / "events.jsonl"
            original_budget = original_state["review_budget"]

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
            observed_head = run(
                ["git", "rev-parse", "HEAD"],
                cwd=prepared.repository,
            ).stdout.strip()

            classified = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(classified.returncode, 0, classified.stderr)
            self.assertIn("classified stale", classified.stdout)
            self.assertIn(
                f"Observed implementation head: {observed_head}",
                classified.stdout,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            round_record = json.loads(
                round_path.read_text(encoding="utf-8")
            )
            self.assertEqual(state["phase"], "implementing")
            self.assertEqual(state["active_round"]["status"], "stale")
            self.assertEqual(
                state["active_round"]["result_id"],
                review["result_id"],
            )
            self.assertEqual(state["review_budget"], original_budget)
            self.assertIsNone(state["approved_head_oid"])
            self.assertEqual(round_record["status"], "stale")
            self.assertEqual(round_record["verdict"], "approved")
            self.assertEqual(
                round_record["observed_head_oid"],
                observed_head,
            )
            self.assertIn(
                str(review["head_oid"]),
                round_record["classification_reason"],
            )
            self.assertTrue((round_directory / "bundle").is_dir())
            self.assertFalse(prepared.review_worktree.exists())
            events = [
                json.loads(line)
                for line in events_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(events[-1]["event"], "review_stale")
            self.assertEqual(
                events[-1]["observed_head_oid"],
                observed_head,
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
            self.assertIn("already classified stale", replay.stdout)
            self.assertEqual(state_path.read_bytes(), state_before_replay)
            self.assertEqual(round_path.read_bytes(), round_before_replay)
            self.assertEqual(events_path.read_bytes(), events_before_replay)

            recovery_report = root / "recovery-report.md"
            recovery_report.write_text(
                "# Recovery report\n\nThe advanced revision is ready.\n",
                encoding="utf-8",
            )
            submitted = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(recovery_report),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            second_round = (
                control_root / "runs" / run_id / "rounds/002/round.json"
            )
            newer_state = state_path.read_bytes()
            newer_round = second_round.read_bytes()
            newer_events = events_path.read_bytes()

            historical = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(historical.returncode, 0, historical.stderr)
            self.assertIn("already classified stale", historical.stdout)
            self.assertEqual(state_path.read_bytes(), newer_state)
            self.assertEqual(second_round.read_bytes(), newer_round)
            self.assertEqual(events_path.read_bytes(), newer_events)

    def test_marker_owned_invalid_result_is_archived_and_replays_safely(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(root)
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            original_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            run_id = str(original_state["active_run_id"])
            round_directory = (
                control_root / "runs" / run_id / "rounds/001"
            )
            round_path = round_directory / "round.json"
            events_path = control_root / "runs" / run_id / "events.jsonl"
            original_budget = original_state["review_budget"]
            review_path = prepared.bundle / "output/review.json"
            rewritten = json.loads(review_path.read_text(encoding="utf-8"))
            rewritten["summary"] = "Marker-confirmed content was rewritten."
            review_path.write_text(
                f"{json.dumps(rewritten, indent=2)}\n",
                encoding="utf-8",
            )

            state_before_foreign = state_path.read_bytes()
            round_before_foreign = round_path.read_bytes()
            events_before_foreign = events_path.read_bytes()
            foreign = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                "99999999-9999-4999-8999-999999999999",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(foreign.returncode, 1)
            self.assertIn(
                "does not match the active Reviewer-local marker",
                foreign.stderr,
            )
            self.assertEqual(state_path.read_bytes(), state_before_foreign)
            self.assertEqual(round_path.read_bytes(), round_before_foreign)
            self.assertEqual(events_path.read_bytes(), events_before_foreign)
            self.assertFalse(
                (round_directory / "diagnostics/invalid-results").exists()
            )

            classified = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(classified.returncode, 0, classified.stderr)
            self.assertIn("classified invalid", classified.stdout)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            round_record = json.loads(
                round_path.read_text(encoding="utf-8")
            )
            self.assertEqual(state["phase"], "implementing")
            self.assertEqual(state["active_round"]["status"], "invalid")
            self.assertEqual(state["review_budget"], original_budget)
            self.assertIsNone(state["approved_head_oid"])
            self.assertEqual(round_record["status"], "invalid")
            self.assertEqual(
                round_record["result_id"],
                review["result_id"],
            )
            self.assertIsNone(round_record["verdict"])
            diagnostic_id = str(round_record["diagnostic_id"])
            diagnostic = (
                round_directory
                / "diagnostics/invalid-results"
                / diagnostic_id
            )
            self.assertIn(str(diagnostic), classified.stdout)
            self.assertEqual(
                {path.name for path in diagnostic.iterdir()},
                {
                    "review.json",
                    "review.md",
                    "local-state.json",
                    "validation-error.json",
                },
            )
            validation_error = json.loads(
                (diagnostic / "validation-error.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(validation_error["diagnostic_id"], diagnostic_id)
            self.assertEqual(
                validation_error["reason"],
                round_record["classification_reason"],
            )
            self.assertIn("review digest", validation_error["reason"])
            self.assertTrue(prepared.review_worktree.exists())
            events = [
                json.loads(line)
                for line in events_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(events[-1]["event"], "review_invalid")
            self.assertEqual(events[-1]["diagnostic_id"], diagnostic_id)

            state_before_replay = state_path.read_bytes()
            round_before_replay = round_path.read_bytes()
            events_before_replay = events_path.read_bytes()
            diagnostic_before_replay = {
                path.name: path.read_bytes() for path in diagnostic.iterdir()
            }
            replay = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertIn("already classified invalid", replay.stdout)
            self.assertEqual(state_path.read_bytes(), state_before_replay)
            self.assertEqual(round_path.read_bytes(), round_before_replay)
            self.assertEqual(events_path.read_bytes(), events_before_replay)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in diagnostic.iterdir()
                },
                diagnostic_before_replay,
            )

            recovery_report = root / "recovery-report.md"
            recovery_report.write_text(
                "# Recovery report\n\n"
                "The candidate is ready for a new review.\n",
                encoding="utf-8",
            )
            submitted = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(recovery_report),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            second_round = (
                control_root / "runs" / run_id / "rounds/002/round.json"
            )
            newer_state = state_path.read_bytes()
            newer_round = second_round.read_bytes()
            newer_events = events_path.read_bytes()

            historical = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(historical.returncode, 0, historical.stderr)
            self.assertIn("already classified invalid", historical.stdout)
            self.assertEqual(state_path.read_bytes(), newer_state)
            self.assertEqual(second_round.read_bytes(), newer_round)
            self.assertEqual(events_path.read_bytes(), newer_events)

    def test_hidden_tracked_review_change_is_classified_invalid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared, review = _marker_confirmed_review(
                Path(temporary_directory)
            )
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

            classified = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(classified.returncode, 0, classified.stderr)
            self.assertIn("classified invalid", classified.stdout)
            self.assertIn("tracked", classified.stdout.lower())
            state = json.loads(
                (prepared.repository / ".agent-squad/state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["phase"], "implementing")
            self.assertEqual(state["active_round"]["status"], "invalid")
            self.assertEqual(
                state["review_budget"]["completed_change_reviews"],
                0,
            )
            self.assertTrue(prepared.review_worktree.exists())

    def test_classification_commit_failure_reuses_preserved_evidence(
        self,
    ) -> None:
        for classification in ("stale", "invalid"):
            with self.subTest(classification=classification):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    prepared, review = _marker_confirmed_review(root)
                    if classification == "stale":
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
                    else:
                        review_path = prepared.bundle / "output/review.json"
                        changed = json.loads(
                            review_path.read_text(encoding="utf-8")
                        )
                        changed["summary"] = "Rewritten after marking."
                        review_path.write_text(
                            f"{json.dumps(changed, indent=2)}\n",
                            encoding="utf-8",
                        )

                    control_root = prepared.repository / ".agent-squad"
                    state_path = control_root / "state.json"
                    state = json.loads(
                        state_path.read_text(encoding="utf-8")
                    )
                    run_directory = (
                        control_root / "runs" / str(state["active_run_id"])
                    )
                    round_path = run_directory / "rounds/001/round.json"
                    events_path = run_directory / "events.jsonl"
                    state_before = state_path.read_bytes()
                    round_before = round_path.read_bytes()
                    events_before = events_path.read_bytes()
                    real_atomic_write = review_applications.atomic_write

                    def fail_state_commit(path, content, *, mode):
                        if (
                            path.resolve() == state_path.resolve()
                            and b'"phase": "implementing"' in content
                        ):
                            raise OSError(
                                "injected classification commit failure"
                            )
                        real_atomic_write(path, content, mode=mode)

                    with (
                        mock.patch.object(
                            review_applications,
                            "atomic_write",
                            side_effect=fail_state_commit,
                        ),
                        self.assertRaisesRegex(
                            review_applications.ReviewApplicationError,
                            "injected classification commit failure",
                        ),
                    ):
                        review_applications.apply_review(
                            prepared.repository,
                            result_id=str(review["result_id"]),
                        )

                    self.assertEqual(state_path.read_bytes(), state_before)
                    self.assertEqual(round_path.read_bytes(), round_before)
                    self.assertEqual(events_path.read_bytes(), events_before)
                    if classification == "stale":
                        preserved = run_directory / "rounds/001/bundle"
                        self.assertTrue(preserved.is_dir())
                    else:
                        diagnostics = (
                            run_directory
                            / "rounds/001/diagnostics/invalid-results"
                        )
                        self.assertEqual(len(list(diagnostics.iterdir())), 1)

                    retried = review_applications.apply_review(
                        prepared.repository,
                        result_id=str(review["result_id"]),
                    )
                    self.assertIs(
                        retried.classification,
                        runs.RoundStatus(classification),
                    )
                    self.assertFalse(retried.replayed)
                    if classification == "invalid":
                        self.assertEqual(len(list(diagnostics.iterdir())), 1)

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

    def test_changes_requested_status_accepts_retained_clean_worktree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="changes_requested",
            )
            run(
                [
                    "git",
                    "worktree",
                    "lock",
                    str(prepared.review_worktree),
                ],
                cwd=prepared.repository,
            )
            try:
                applied = run_cli(
                    prepared.repository,
                    "apply-review",
                    "--result-id",
                    str(review["result_id"]),
                    data_home=prepared.data_home,
                    env_overrides=prepared.environment,
                )

                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertIn(
                    "could not remove review worktree",
                    applied.stderr,
                )
                self.assertTrue(prepared.review_worktree.is_dir())
                self.assertFalse(
                    (
                        prepared.review_worktree / ".agent-squad-review"
                    ).exists()
                )

                status = run_cli(
                    prepared.repository,
                    "status",
                    data_home=prepared.data_home,
                    env_overrides=prepared.environment,
                )

                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertIn("Phase: implementing", status.stdout)
                self.assertIn(
                    f"Review worktree: {prepared.review_worktree}",
                    status.stdout,
                )
                self.assertNotIn(
                    "Review worktree available:",
                    status.stdout,
                )
                self.assertIn(
                    "Next action: agent-squad submit --report <report.md> "
                    "--response <response.json>",
                    status.stdout,
                )
            finally:
                run(
                    [
                        "git",
                        "worktree",
                        "unlock",
                        str(prepared.review_worktree),
                    ],
                    cwd=prepared.repository,
                    check=False,
                )
                run(
                    [
                        "git",
                        "worktree",
                        "remove",
                        str(prepared.review_worktree),
                    ],
                    cwd=prepared.repository,
                    check=False,
                )

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
            (run_directory / "events.jsonl").write_bytes(b"")
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
                classification_reason=None,
                observed_head_oid=None,
                updated_at="2026-08-29T00:00:00Z",
                request_id="abcdefab-1234-5678-9234-567812345678",
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
                classification_reason=None,
                observed_head_oid=None,
                updated_at="2026-08-29T00:00:00Z",
                request_id="abcdefab-1234-5678-9234-567812345678",
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
                "prior-artifact paths do not match round history",
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

    def test_same_head_reconsideration_accepts_evidence_and_rejects_false_fix(
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

            report = root / "reconsideration-report.md"
            report.write_text(
                "# Implementation Report\n\nNo code change is required.\n",
                encoding="utf-8",
            )
            response_path = root / "reconsideration-response.json"
            _write_fixed_response(response_path, prepared, review)
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state_before = state_path.read_bytes()
            state = json.loads(state_before)
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            first_round = run_directory / "rounds/001"
            second_round = run_directory / "rounds/002"

            def assert_rejected_without_round(result) -> None:
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    "reconsideration requires rejected dispositions with "
                    "evidence",
                    result.stderr,
                )
                self.assertEqual(state_path.read_bytes(), state_before)
                self.assertFalse((first_round / "response.json").exists())
                self.assertFalse(second_round.exists())

            false_fix = run_cli(
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

            assert_rejected_without_round(false_fix)

            needs_human_response = json.loads(
                response_path.read_text(encoding="utf-8")
            )
            needs_human_response["responses"][0].update(
                disposition="needs_human",
                changed_files=[],
                evidence=[],
                verification="",
            )
            response_path.write_text(
                f"{json.dumps(needs_human_response, indent=2)}\n",
                encoding="utf-8",
            )
            unresolved = run_cli(
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

            assert_rejected_without_round(unresolved)

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
            state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            second_request = json.loads(
                (second_round / "request.json").read_text(encoding="utf-8")
            )
            second_worktree = Path(state["active_round"]["review_worktree"])
            second_bundle = second_worktree / ".agent-squad-review"
            self.assertEqual(state["current_round"], 2)
            self.assertEqual(state["current_head_oid"], review["head_oid"])
            self.assertEqual(second_request["mode"], "reconsideration")
            self.assertEqual(second_request["head_oid"], review["head_oid"])
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
                review["head_oid"],
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
                (second_round / "implementation-report.md").read_bytes(),
                report.read_bytes(),
            )
            stored_response = json.loads(
                (first_round / "response.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                stored_response["reviewed_head_oid"],
                review["head_oid"],
            )
            self.assertEqual(
                stored_response["responses"][0]["evidence"],
                ["feature.txt:1 contains the complete candidate value"],
            )
            self.assertEqual(
                stored_response["responses"][0]["verification"],
                "",
            )
            self.assertEqual(
                (second_bundle / "input/previous-review.json").read_bytes(),
                (first_round / "review.json").read_bytes(),
            )
            self.assertEqual(
                (second_bundle / "input/previous-response.json").read_bytes(),
                response_path.read_bytes(),
            )
            for immutable_path in (
                second_round / "request.json",
                second_round / "implementation-report.md",
                first_round / "response.json",
                second_bundle / "input/request.json",
                second_bundle / "input/implementation-report.md",
                second_bundle / "input/previous-review.json",
                second_bundle / "input/previous-response.json",
            ):
                self.assertEqual(
                    stat.S_IMODE(immutable_path.stat().st_mode),
                    0o400,
                )

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
            approved_state = json.loads(
                (control_root / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(approved_state["phase"], "approved")
            self.assertEqual(
                approved_state["approved_head_oid"],
                review["head_oid"],
            )

    def test_status_rejects_relabelled_same_head_new_revision(self) -> None:
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
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            round_path = run_directory / "rounds/002/round.json"
            request_path = run_directory / "rounds/002/request.json"
            bundle_request_path = (
                Path(state["active_round"]["review_worktree"])
                / ".agent-squad-review/input/request.json"
            )
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["mode"] = "new_revision"
            request_bytes = review_applications.encode_json(request)
            request_digest = hashlib.sha256(request_bytes).hexdigest()
            round_record = json.loads(
                round_path.read_text(encoding="utf-8")
            )
            round_record["mode"] = "new_revision"
            round_record["artifacts"]["request"]["sha256"] = (
                request_digest
            )
            request_input = next(
                artifact
                for artifact in round_record["artifacts"]["bundle_inputs"]
                if artifact["path"] == "input/request.json"
            )
            request_input["sha256"] = request_digest
            state["active_round"]["mode"] = "new_revision"
            for path, content in (
                (state_path, review_applications.encode_json(state)),
                (
                    round_path,
                    review_applications.encode_json(round_record),
                ),
                (request_path, request_bytes),
                (bundle_request_path, request_bytes),
            ):
                path.chmod(0o600)
                path.write_bytes(content)

            status = run_cli(
                prepared.repository,
                "status",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(status.returncode, 1)
            self.assertIn(
                "a new_revision submission after changes_requested requires "
                "a new committed HEAD",
                status.stderr,
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
            self.assertFalse(
                (
                    round_directory
                    / "bundle/output/.review-submit.lock"
                ).exists()
            )
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(round_path.read_bytes(), round_before)
            self.assertEqual(run_path.read_bytes(), run_before)
            archived_marker = round_directory / "bundle/local-state.json"
            original_archived_marker = archived_marker.read_bytes()
            archived_marker.chmod(0o600)
            archived_marker.write_bytes(original_archived_marker + b" ")
            try:
                with self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "digest mismatch for local-state.json",
                ):
                    review_applications.apply_review(
                        prepared.repository,
                        result_id=str(review["result_id"]),
                    )
            finally:
                archived_marker.write_bytes(original_archived_marker)
                archived_marker.chmod(0o400)
            self.assertFalse(
                (
                    round_directory
                    / "diagnostics/retired-apply-attempts"
                ).exists()
            )
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

            with (
                mock.patch.object(
                    review_applications,
                    "utc_timestamp",
                    return_value="2099-01-01T00:00:00Z",
                ),
                mock.patch.object(
                    review_applications,
                    "append_event",
                    side_effect=OSError("injected event append failure"),
                ),
                self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "injected event append failure",
                ),
            ):
                review_applications.apply_review(
                    prepared.repository,
                    result_id=str(review["result_id"]),
                )
            self.assertEqual(approval_path.read_bytes(), first_approval)
            self.assertNotIn(b"2099-01-01T00:00:00Z", first_approval)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["phase"],
                "approved",
            )
            self.assertEqual(
                json.loads(round_path.read_text(encoding="utf-8"))[
                    "updated_at"
                ],
                "2099-01-01T00:00:00Z",
            )

            replayed = review_applications.apply_review(
                prepared.repository,
                result_id=str(review["result_id"]),
            )

            self.assertTrue(replayed.replayed)
            applied_events = [
                event
                for event in (
                    json.loads(line)
                    for line in (
                        run_directory / "events.jsonl"
                    ).read_text(encoding="utf-8").splitlines()
                )
                if event["event"] == "review_applied"
            ]
            self.assertEqual(len(applied_events), 1)
            self.assertEqual(
                applied_events[0]["timestamp"],
                "2099-01-01T00:00:00Z",
            )

    def test_retired_failed_apply_is_quarantined_for_corrected_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, retired_review = _marker_confirmed_review(root)
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state_before = state_path.read_bytes()
            state = json.loads(state_before.decode("utf-8"))
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            round_directory = run_directory / "rounds/001"
            real_atomic_write = review_applications.atomic_write

            def fail_state_commit(path, content, *, mode):
                if (
                    path.resolve() == state_path.resolve()
                    and b'"phase": "approved"' in content
                ):
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
                        result_id=str(retired_review["result_id"]),
                    )

            self.assertEqual(state_path.read_bytes(), state_before)
            archive_root = round_directory / "bundle"
            archive_before = {
                path.relative_to(archive_root).as_posix(): path.read_bytes()
                for path in archive_root.rglob("*")
                if path.is_file()
            }
            provisional_names = (
                "review.json",
                "review.md",
                "review-marker.json",
                "approval.json",
            )
            provisional_before = {
                name: (round_directory / name).read_bytes()
                for name in provisional_names
            }

            marker = prepared.bundle / "local-state.json"
            marker.write_bytes(b'{"schema_version":\n')
            recovered = run_cli(
                prepared.repository,
                "retry-handoff",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)

            corrected_review = _write_review(prepared)
            corrected_review["result_id"] = (
                "44444444-4444-4444-8444-444444444444"
            )
            (prepared.bundle / "output/review.json").write_text(
                f"{json.dumps(corrected_review, indent=2)}\n",
                encoding="utf-8",
            )
            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)

            external = root / "outside-diagnostics"
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            attempts_parent = (
                round_directory / "diagnostics/retired-apply-attempts"
            )
            attempts_parent.symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(
                review_applications.ReviewApplicationError,
                "must be a non-symlink directory",
            ):
                review_applications.apply_review(
                    prepared.repository,
                    result_id=str(corrected_review["result_id"]),
                )
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"),
                "unchanged\n",
            )
            attempts_parent.unlink()

            real_replace = Path.replace

            def fail_archive_move(path, target):
                if path.name == "bundle" and Path(target).name == "bundle":
                    raise OSError("injected archive move failure")
                return real_replace(path, target)

            with (
                mock.patch.object(Path, "replace", new=fail_archive_move),
                self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "injected archive move failure",
                ),
            ):
                review_applications.apply_review(
                    prepared.repository,
                    result_id=str(corrected_review["result_id"]),
                )
            self.assertTrue(archive_root.is_dir())
            for name in provisional_names:
                self.assertFalse((round_directory / name).exists())

            applied = review_applications.apply_review(
                prepared.repository,
                result_id=str(corrected_review["result_id"]),
            )

            self.assertFalse(applied.replayed)
            final_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(final_state["phase"], "approved")
            quarantine = (
                round_directory
                / "diagnostics/retired-apply-attempts"
                / str(retired_review["result_id"])
            )
            quarantined_archive = quarantine / "bundle"
            self.assertEqual(
                {
                    path.relative_to(quarantined_archive).as_posix(): (
                        path.read_bytes()
                    )
                    for path in quarantined_archive.rglob("*")
                    if path.is_file()
                },
                archive_before,
            )
            for name, content in provisional_before.items():
                self.assertEqual((quarantine / name).read_bytes(), content)
            self.assertEqual(
                json.loads(
                    (archive_root / "output/review.json").read_text(
                        encoding="utf-8"
                    )
                )["result_id"],
                corrected_review["result_id"],
            )
            self.assertFalse(
                (archive_root / "output/.review-submit.lock").exists()
            )
            self.assertEqual(
                json.loads(
                    (round_directory / "review.json").read_text(
                        encoding="utf-8"
                    )
                )["result_id"],
                corrected_review["result_id"],
            )

    def test_failed_apply_retry_ignores_live_advisory_changes(self) -> None:
        cases = ("missing", "malformed", "mismatched", "directory")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    prepared, review = _marker_confirmed_retired_review(root)
                    control_root = prepared.repository / ".agent-squad"
                    state_path = control_root / "state.json"
                    state_before = state_path.read_bytes()
                    state = json.loads(state_before.decode("utf-8"))
                    run_directory = (
                        control_root
                        / "runs"
                        / str(state["active_run_id"])
                    )
                    round_directory = run_directory / "rounds/001"
                    advisory = prepared.bundle / "retired-results.json"
                    authority = round_directory / "retired-results.json"
                    self.assertEqual(
                        advisory.read_bytes(),
                        authority.read_bytes(),
                    )
                    real_atomic_write = review_applications.atomic_write

                    def fail_state_commit(path, content, *, mode):
                        if (
                            path.resolve() == state_path.resolve()
                            and b'"phase": "approved"' in content
                        ):
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

                    self.assertEqual(state_path.read_bytes(), state_before)
                    archive_root = round_directory / "bundle"
                    archive_before = {
                        path.relative_to(archive_root).as_posix(): (
                            path.read_bytes()
                        )
                        for path in archive_root.rglob("*")
                        if path.is_file()
                    }
                    self.assertIn("retired-results.json", archive_before)
                    if case == "missing":
                        advisory.unlink()
                    elif case == "malformed":
                        advisory.write_bytes(b'{"schema_version":\n')
                    elif case == "mismatched":
                        mismatched = json.loads(
                            authority.read_text(encoding="utf-8")
                        )
                        mismatched["request_id"] = (
                            "99999999-9999-4999-8999-999999999999"
                        )
                        advisory.write_text(
                            f"{json.dumps(mismatched, indent=2)}\n",
                            encoding="utf-8",
                        )
                    else:
                        advisory.unlink()
                        advisory.mkdir()

                    retried = review_applications.apply_review(
                        prepared.repository,
                        result_id=str(review["result_id"]),
                    )

                    self.assertFalse(retried.replayed)
                    archive_after = {
                        path.relative_to(archive_root).as_posix(): (
                            path.read_bytes()
                        )
                        for path in archive_root.rglob("*")
                        if path.is_file()
                    }
                    self.assertEqual(archive_after, archive_before)
                    final_state = json.loads(
                        state_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual(final_state["phase"], "approved")
                    round_record = json.loads(
                        (round_directory / "round.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    manifest = {
                        str(item["path"])[len("bundle/"):]: item["sha256"]
                        for item in round_record["artifacts"]["bundle_archive"]
                    }
                    self.assertEqual(
                        manifest,
                        {
                            path: hashlib.sha256(content).hexdigest()
                            for path, content in archive_before.items()
                        },
                    )
                    events_path = run_directory / "events.jsonl"
                    applied_events = [
                        event
                        for event in (
                            json.loads(line)
                            for line in events_path.read_text(
                                encoding="utf-8"
                            ).splitlines()
                        )
                        if event["event"] == "review_applied"
                    ]
                    self.assertEqual(len(applied_events), 1)
                    replayed = review_applications.apply_review(
                        prepared.repository,
                        result_id=str(review["result_id"]),
                    )
                    self.assertTrue(replayed.replayed)
                    replay_events = [
                        event
                        for event in (
                            json.loads(line)
                            for line in events_path.read_text(
                                encoding="utf-8"
                            ).splitlines()
                        )
                        if event["event"] == "review_applied"
                    ]
                    self.assertEqual(len(replay_events), 1)

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

    def test_applied_cleanup_reports_raised_git_failures_as_warnings(
        self,
    ) -> None:
        cases = (
            (
                "status",
                "could not verify review worktree cleanup",
            ),
            (
                "remove",
                "could not remove review worktree",
            ),
        )
        for operation, expected in cases:
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    prepared, review = _marker_confirmed_review(
                        root,
                        verdict="changes_requested",
                    )
                    state_path = (
                        prepared.repository / ".agent-squad/state.json"
                    )
                    real_run_git = review_applications.run_git

                    def fail_applied_cleanup(
                        start: Path,
                        *arguments: str,
                    ) -> object:
                        state = json.loads(
                            state_path.read_text(encoding="utf-8")
                        )
                        matches = state["phase"] == "implementing" and (
                            (
                                operation == "status"
                                and start == prepared.review_worktree
                                and arguments
                                == (
                                    "status",
                                    "--porcelain=v1",
                                    "-z",
                                    "--untracked-files=all",
                                    "--ignore-submodules=none",
                                )
                            )
                            or (
                                operation == "remove"
                                and arguments
                                == (
                                    "worktree",
                                    "remove",
                                    str(prepared.review_worktree),
                                )
                            )
                        )
                        if matches:
                            raise review_applications.AgentSquadError(
                                f"injected {operation} failure"
                            )
                        return real_run_git(start, *arguments)

                    with mock.patch.object(
                        review_applications,
                        "run_git",
                        side_effect=fail_applied_cleanup,
                    ):
                        applied = review_applications.apply_review(
                            prepared.repository,
                            result_id=str(review["result_id"]),
                        )

                    state = json.loads(
                        state_path.read_text(encoding="utf-8")
                    )
                    run_id = str(state["active_run_id"])
                    round_path = (
                        prepared.repository
                        / ".agent-squad/runs"
                        / run_id
                        / "rounds/001/round.json"
                    )
                    round_record = json.loads(
                        round_path.read_text(encoding="utf-8")
                    )

                    self.assertEqual(len(applied.cleanup_warnings), 1)
                    self.assertIn(expected, applied.cleanup_warnings[0])
                    self.assertIn(
                        f"injected {operation} failure",
                        applied.cleanup_warnings[0],
                    )
                    self.assertEqual(state["phase"], "implementing")
                    self.assertEqual(
                        state["active_round"]["status"],
                        "applied",
                    )
                    self.assertEqual(round_record["status"], "applied")
                    self.assertTrue(prepared.review_worktree.is_dir())

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
