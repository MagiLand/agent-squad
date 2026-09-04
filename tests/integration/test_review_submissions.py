from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from tests._support import (
    add_src_to_path,
    install_fake_herdr,
    run,
    run_cli,
    seed_git_repository,
)


add_src_to_path()

from agent_squad.artifacts import ReviewRequest  # noqa: E402
from agent_squad.initialization import AgentKind  # noqa: E402
from agent_squad.review_submissions import (  # noqa: E402
    ReviewSubmissionError,
    _validate_bundle_inputs,
    submit_review_result,
)
from agent_squad.artifacts import deterministic_reviewer_name  # noqa: E402


@dataclass(frozen=True)
class _PreparedRound:
    repository: Path
    data_home: Path
    environment: dict[str, str]
    review_worktree: Path
    bundle: Path
    request: dict[str, object]


_BUNDLE_DAMAGE_CASES = (
    "tamper bundle input",
    "delete bundle input",
    "delete bundle",
)
_REVIEW_WORKTREE_DAMAGE_CASES = (
    *_BUNDLE_DAMAGE_CASES,
    "delete worktree",
    "untracked scratch file",
)


def _damage_reviewer_worktree(
    prepared: _PreparedRound,
    damage: str,
) -> None:
    if damage == "tamper bundle input":
        target = prepared.bundle / "input/task.md"
        target.chmod(0o600)
        target.write_text("tampered\n", encoding="utf-8")
    elif damage == "delete bundle input":
        (prepared.bundle / "input/task.md").unlink()
    elif damage == "delete bundle":
        shutil.rmtree(prepared.bundle)
    elif damage == "delete worktree":
        shutil.rmtree(prepared.review_worktree)
    elif damage == "untracked scratch file":
        (prepared.review_worktree / "scratch.txt").write_text(
            "reviewer scratch\n",
            encoding="utf-8",
        )
    else:
        raise AssertionError(f"unknown Reviewer-worktree damage: {damage}")


def _prepare_round(
    root: Path,
    *,
    allowed_generated_paths: tuple[str, ...] = (),
    review_limit: int | None = None,
    include_tracked_symlink: bool = False,
    include_nested_tracked_file: bool = False,
) -> _PreparedRound:
    repository = root / "repository"
    seed_git_repository(repository)
    data_home = root / "data"
    _, environment = install_fake_herdr(root / "fake-install")
    initialized = run_cli(
        repository,
        "init",
        data_home=data_home,
        env_overrides=environment,
    )
    if initialized.returncode != 0:
        raise AssertionError(initialized.stderr)
    if allowed_generated_paths or review_limit is not None:
        configuration_path = repository / ".agent-squad/config.json"
        configuration = json.loads(
            configuration_path.read_text(encoding="utf-8")
        )
        configuration["allowed_generated_paths"] = list(
            allowed_generated_paths
        )
        if review_limit is not None:
            configuration["max_completed_change_reviews"] = review_limit
        _write_json_fixture(configuration_path, configuration)
    task = root / "task.md"
    task.write_text(
        "# Task\n\nReview the exact candidate.\n",
        encoding="utf-8",
    )
    started = run_cli(
        repository,
        "start",
        "--task",
        str(task),
        "--base",
        "main",
        data_home=data_home,
        env_overrides=environment,
    )
    if started.returncode != 0:
        raise AssertionError(started.stderr)
    (repository / "feature.txt").write_text("candidate\n", encoding="utf-8")
    candidate_paths = ["feature.txt"]
    if include_tracked_symlink:
        (repository / "feature-link").symlink_to("feature.txt")
        candidate_paths.append("feature-link")
    if include_nested_tracked_file:
        nested_path = "nested/deeper/leaf/feature.txt"
        nested_file = repository / nested_path
        nested_file.parent.mkdir(parents=True)
        nested_file.write_text("nested candidate\n", encoding="utf-8")
        candidate_paths.append(nested_path)
    run(["git", "add", *candidate_paths], cwd=repository)
    run(
        [
            "git",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "-m",
            "feat: add candidate",
        ],
        cwd=repository,
    )
    report = root / "implementation-report.md"
    report.write_text(
        "# Implementation Report\n\nAdded and tested the candidate.\n",
        encoding="utf-8",
    )
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
    if submitted.returncode != 0:
        raise AssertionError(submitted.stderr)
    state = json.loads(
        (repository / ".agent-squad/state.json").read_text(encoding="utf-8")
    )
    review_worktree = Path(state["active_round"]["review_worktree"])
    bundle = review_worktree / ".agent-squad-review"
    request = json.loads(
        (bundle / "input/request.json").read_text(encoding="utf-8")
    )
    return _PreparedRound(
        repository=repository,
        data_home=data_home,
        environment=environment,
        review_worktree=review_worktree,
        bundle=bundle,
        request=request,
    )


def _review_result(
    request: dict[str, object],
    verdict: str,
) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    summary = "The exact revision satisfies the task."
    if verdict == "changes_requested":
        summary = "One correctness issue blocks approval."
        findings.append(
            {
                "id": "REV-001",
                "severity": "high",
                "blocking": True,
                "category": "correctness",
                "file": "feature.txt",
                "line_start": 1,
                "line_end": 1,
                "problem": "The candidate value is incomplete.",
                "evidence": "The committed file contains only a placeholder.",
                "impact": "Consumers receive incomplete content.",
                "required_change": "Replace the placeholder value.",
                "verification": "Assert the complete value in a test.",
            }
        )
    elif verdict == "needs_human":
        summary = "The Developer must decide the compatibility policy."
    result_ids = {
        "approved": "11111111-1111-4111-8111-111111111111",
        "changes_requested": "22222222-2222-4222-8222-222222222222",
        "needs_human": "33333333-3333-4333-8333-333333333333",
    }
    return {
        "schema_version": 1,
        "created_at": "2026-08-26T12:00:00Z",
        "result_id": result_ids[verdict],
        "request_id": request["request_id"],
        "run_id": request["run_id"],
        "round": request["round"],
        "base_oid": request["base_oid"],
        "head_oid": request["head_oid"],
        "verdict": verdict,
        "summary": summary,
        "findings": findings,
        "non_blocking_observations": [],
    }


def _write_review(
    prepared: _PreparedRound,
    *,
    verdict: str = "approved",
) -> dict[str, object]:
    review = _review_result(prepared.request, verdict)
    output = prepared.bundle / "output"
    _write_json_fixture(output / "review.json", review)
    (output / "review.md").write_text(
        f"# Review\n\n{review['summary']}\n",
        encoding="utf-8",
    )
    return review


def _write_request(
    prepared: _PreparedRound,
    request: dict[str, object],
) -> _PreparedRound:
    request_path = prepared.bundle / "input/request.json"
    request_path.chmod(0o600)
    _write_json_fixture(request_path, request)
    request_path.chmod(0o400)
    return _PreparedRound(
        repository=prepared.repository,
        data_home=prepared.data_home,
        environment=prepared.environment,
        review_worktree=prepared.review_worktree,
        bundle=prepared.bundle,
        request=request,
    )


def _write_json_fixture(path: Path, value: object) -> None:
    path.write_text(
        f"{json.dumps(value, indent=2)}\n",
        encoding="utf-8",
    )


def _hide_tracked_path(
    review_worktree: Path,
    path: str,
    *,
    flag: str = "--assume-unchanged",
) -> None:
    run(
        ["git", "update-index", flag, path],
        cwd=review_worktree,
    )


def _prepare_multi_input_round(
    root: Path,
) -> tuple[_PreparedRound, list[dict[str, object]]]:
    first_round = _prepare_round(root)
    review_worktree = first_round.review_worktree.parent / "round-002"
    run(
        [
            "git",
            "worktree",
            "move",
            str(first_round.review_worktree),
            str(review_worktree),
        ],
        cwd=first_round.repository,
    )
    prepared = _PreparedRound(
        repository=first_round.repository,
        data_home=first_round.data_home,
        environment=first_round.environment,
        review_worktree=review_worktree,
        bundle=review_worktree / ".agent-squad-review",
        request=copy.deepcopy(first_round.request),
    )
    (review_worktree / "feature.txt").write_text(
        "corrected candidate\n",
        encoding="utf-8",
    )
    run(["git", "add", "feature.txt"], cwd=review_worktree)
    run(
        [
            "git",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "-m",
            "fix: correct candidate",
        ],
        cwd=review_worktree,
    )
    corrected_head = run(
        ["git", "rev-parse", "HEAD"],
        cwd=review_worktree,
    ).stdout.strip()
    input_root = prepared.bundle / "input"
    previous_review = _review_result(
        first_round.request,
        "changes_requested",
    )
    previous_review["result_id"] = (
        "44444444-4444-4444-8444-444444444444"
    )
    _write_json_fixture(
        input_root / "previous-review.json",
        previous_review,
    )
    _write_json_fixture(
        input_root / "previous-response.json",
        {
            "schema_version": 1,
            "created_at": "2026-08-26T12:00:30Z",
            "response_id": (
                "55555555-5555-4555-8555-555555555555"
            ),
            "supersedes_response_id": None,
            "resolution_ids": [],
            "run_id": previous_review["run_id"],
            "review_round": previous_review["round"],
            "review_result_id": previous_review["result_id"],
            "reviewed_head_oid": previous_review["head_oid"],
            "responses": [
                {
                    "finding_id": "REV-001",
                    "disposition": "fixed",
                    "rationale": "Completed the requested correction.",
                    "changed_files": ["feature.txt"],
                    "evidence": [],
                    "verification": "python -m unittest discover -s tests",
                }
            ],
        },
    )
    resolution_root = input_root / "resolutions"
    resolution_root.mkdir()
    resolutions: list[dict[str, object]] = []
    resolution_paths: list[str] = []
    for index in (1, 2):
        companion_name = f"{index:03d}-resolution.md"
        companion = f"# Resolution {index}\n\nUse policy {index}.\n".encode()
        (resolution_root / companion_name).write_bytes(companion)
        resolution_name = f"{index:03d}-resolution.json"
        resolution = {
            "schema_version": 1,
            "created_at": f"2026-08-26T12:0{index}:00Z",
            "resolution_id": (
                f"{index:08d}-1111-4111-8111-{index:012d}"
            ),
            "run_id": prepared.request["run_id"],
            "resolves_escalation_id": (
                f"{index + 2:08d}-2222-4222-8222-"
                f"{index + 2:012d}"
            ),
            "applies_to_finding_ids": [f"REV-{index:03d}"],
            "resolution_path": companion_name,
            "resolution_sha256": hashlib.sha256(companion).hexdigest(),
            "additional_rounds_granted": 0,
        }
        _write_json_fixture(resolution_root / resolution_name, resolution)
        resolutions.append(resolution)
        resolution_paths.append(f"input/resolutions/{resolution_name}")

    request = copy.deepcopy(prepared.request)
    request.update(
        round=2,
        head_oid=corrected_head,
        reviewer_name=deterministic_reviewer_name(
            str(request["run_id"]),
            2,
        ),
        previous_review_path="input/previous-review.json",
        previous_response_path="input/previous-response.json",
        resolution_paths=resolution_paths,
    )
    prepared = _write_request(prepared, request)
    _write_review(prepared)
    return prepared, resolutions


def _result_prompt_events(prepared: _PreparedRound) -> list[dict[str, object]]:
    events_path = (
        Path(prepared.environment["FAKE_HERDR_STATE_DIR"])
        / "invocations.jsonl"
    )
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    return [
        event
        for event in events
        if event["arguments"][:2] == ["agent", "prompt"]
        and len(event["arguments"]) > 3
        and "AGENT_SQUAD/0.4.4 REVIEW_RESULT" in event["arguments"][3]
    ]


class ReviewSubmitCommandTests(unittest.TestCase):
    def test_notification_preflights_the_implementer_integration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            _write_review(prepared)
            client = mock.Mock()

            result = submit_review_result(
                prepared.review_worktree,
                herdr_client=client,
            )

            self.assertTrue(result.notification_sent)
            client.discover.assert_called_once_with(
                AgentKind.CODEX,
                role="Implementer",
            )
            client.dispatch_review_result.assert_called_once()

    def test_configured_generated_output_is_carried_and_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(
                Path(temporary_directory),
                allowed_generated_paths=("build/",),
            )
            self.assertEqual(
                prepared.request["allowed_generated_paths"],
                ["build/"],
            )
            generated = prepared.review_worktree / "build/output.bin"
            generated.parent.mkdir()
            generated.write_bytes(b"generated output\n")
            _write_review(prepared)

            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            self.assertTrue((prepared.bundle / "local-state.json").is_file())

    def test_all_verdicts_are_marker_confirmed_before_neutral_notification(
        self,
    ) -> None:
        for verdict in ("approved", "changes_requested", "needs_human"):
            with self.subTest(verdict=verdict):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    prepared = _prepare_round(Path(temporary_directory))
                    review = _write_review(prepared, verdict=verdict)

                    submitted = run_cli(
                        prepared.review_worktree,
                        "review-submit",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )

                    self.assertEqual(submitted.returncode, 0, submitted.stderr)
                    marker_path = prepared.bundle / "local-state.json"
                    marker = json.loads(
                        marker_path.read_text(encoding="utf-8")
                    )
                    review_bytes = (
                        prepared.bundle / "output/review.json"
                    ).read_bytes()
                    self.assertEqual(marker["status"], "result_submitted")
                    self.assertEqual(
                        marker["request_id"],
                        review["request_id"],
                    )
                    self.assertEqual(marker["result_id"], review["result_id"])
                    self.assertEqual(
                        marker["review_sha256"],
                        hashlib.sha256(review_bytes).hexdigest(),
                    )
                    self.assertEqual(
                        stat.S_IMODE(marker_path.stat().st_mode),
                        0o600,
                    )
                    self.assertIn(
                        f"Verdict: {verdict}",
                        submitted.stdout,
                    )
                    prompts = _result_prompt_events(prepared)
                    self.assertEqual(len(prompts), 1)
                    self.assertEqual(
                        prompts[0]["review_marker_at_prompt"],
                        marker,
                    )
                    prompt = prompts[0]["arguments"][3]
                    self.assertIn(
                        f"result_id: {review['result_id']}",
                        prompt,
                    )
                    self.assertIn(
                        "agent-squad apply-review --result-id",
                        prompt,
                    )
                    for outcome in (
                        "approved",
                        "changes_requested",
                        "needs_human",
                    ):
                        self.assertNotIn(outcome, prompt)

    def test_lost_notification_preserves_marker_and_retry_reuses_result(
        self,
    ) -> None:
        cases = (
            (
                "prompt failure",
                "FAKE_HERDR_FAIL_PROMPT",
                "1",
                "injected prompt failure",
                2,
            ),
            (
                "Implementer unavailable",
                "FAKE_HERDR_IMPLEMENTER_NAME",
                "codex-other",
                "Implementer 'codex-main' is not available",
                1,
            ),
            (
                "Implementer kind mismatch",
                "FAKE_HERDR_IMPLEMENTER_KIND",
                "claude",
                "Implementer 'codex-main' uses agent kind 'claude', "
                "expected 'codex'",
                1,
            ),
        )
        for case, variable, value, message, expected_prompt_count in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    prepared = _prepare_round(Path(temporary_directory))
                    review = _write_review(prepared)
                    failing_environment = dict(prepared.environment)
                    failing_environment[variable] = value

                    first = run_cli(
                        prepared.review_worktree,
                        "review-submit",
                        data_home=prepared.data_home,
                        env_overrides=failing_environment,
                    )

                    self.assertEqual(first.returncode, 1)
                    self.assertIn(
                        "Reviewer-local marker created:",
                        first.stdout,
                    )
                    self.assertIn(
                        "Result notification: failed",
                        first.stdout,
                    )
                    self.assertIn(
                        "marker-confirmed result remains valid",
                        first.stderr,
                    )
                    self.assertIn(message, first.stderr)
                    marker_path = prepared.bundle / "local-state.json"
                    marker_bytes = marker_path.read_bytes()
                    marker = json.loads(marker_bytes)
                    self.assertEqual(
                        marker["result_id"],
                        review["result_id"],
                    )

                    retried = run_cli(
                        prepared.review_worktree,
                        "review-submit",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )

                    self.assertEqual(retried.returncode, 0, retried.stderr)
                    self.assertIn(
                        "Reviewer-local marker reused",
                        retried.stdout,
                    )
                    self.assertEqual(marker_path.read_bytes(), marker_bytes)
                    prompts = _result_prompt_events(prepared)
                    self.assertEqual(
                        len(prompts),
                        expected_prompt_count,
                    )
                    self.assertTrue(
                        all(
                            event["review_marker_at_prompt"]["result_id"]
                            == review["result_id"]
                            for event in prompts
                        )
                    )

    def test_invalid_existing_marker_is_preserved_without_notification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            _write_review(prepared)
            marker_path = prepared.bundle / "local-state.json"
            _write_json_fixture(marker_path, {"schema_version": 1})
            marker_bytes = marker_path.read_bytes()

            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(submitted.returncode, 1)
            self.assertEqual(
                submitted.stderr,
                "agent-squad: error: existing review marker failed "
                "validation: review marker is missing required field(s): "
                "request_id, result_id, review_json_path, review_sha256, "
                "status, submitted_at\n",
            )
            self.assertEqual(marker_path.read_bytes(), marker_bytes)
            self.assertEqual(_result_prompt_events(prepared), [])

    def test_rejected_result_can_be_corrected_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            review = _write_review(prepared)
            review["verdict"] = "changes_requested"
            review["findings"] = []
            review_path = prepared.bundle / "output/review.json"
            _write_json_fixture(review_path, review)

            rejected = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(rejected.returncode, 1)
            self.assertIn(
                "must contain at least one blocking",
                rejected.stderr,
            )
            self.assertFalse((prepared.bundle / "local-state.json").exists())
            self.assertEqual(_result_prompt_events(prepared), [])

            _write_review(prepared)
            corrected = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            self.assertTrue((prepared.bundle / "local-state.json").is_file())

    def test_multi_input_bundle_and_resolution_integrity_are_verified(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared, resolutions = _prepare_multi_input_round(
                Path(temporary_directory)
            )
            request = ReviewRequest.from_dict(prepared.request)
            resolution_root = prepared.bundle / "input/resolutions"
            previous_review_path = (
                prepared.bundle / "input/previous-review.json"
            )
            previous_response_path = (
                prepared.bundle / "input/previous-response.json"
            )
            previous_review = json.loads(
                previous_review_path.read_text(encoding="utf-8")
            )
            previous_response = json.loads(
                previous_response_path.read_text(encoding="utf-8")
            )

            def restore_inputs() -> None:
                _write_json_fixture(previous_review_path, previous_review)
                _write_json_fixture(
                    previous_response_path,
                    previous_response,
                )
                for index, resolution in enumerate(resolutions, start=1):
                    resolution_json = (
                        resolution_root / f"{index:03d}-resolution.json"
                    )
                    _write_json_fixture(resolution_json, resolution)
                    resolution_markdown = (
                        resolution_root / f"{index:03d}-resolution.md"
                    )
                    resolution_markdown.write_text(
                        f"# Resolution {index}\n\nUse policy {index}.\n",
                        encoding="utf-8",
                    )

            _validate_bundle_inputs(prepared.bundle, request)

            invalid_cases = (
                (
                    "previous review from another run",
                    lambda: _write_json_fixture(
                        previous_review_path,
                        {
                            **previous_review,
                            "run_id": (
                                "99999999-9999-4999-8999-999999999999"
                            ),
                        },
                    ),
                    "previous review run ID does not match",
                ),
                (
                    "previous review from current round",
                    lambda: _write_json_fixture(
                        previous_review_path,
                        {**previous_review, "round": 2},
                    ),
                    "previous review round must precede",
                ),
                (
                    "invalid previous response",
                    lambda: _write_json_fixture(
                        previous_response_path,
                        {**previous_response, "schema_version": 2},
                    ),
                    "previous response failed validation: implementation "
                    "response.schema_version must be 1",
                ),
                (
                    "tampered companion",
                    lambda: (
                        resolution_root / "001-resolution.md"
                    ).write_text("tampered\n", encoding="utf-8"),
                    "digest",
                ),
                (
                    "wrong run",
                    lambda: _write_json_fixture(
                        resolution_root / "001-resolution.json",
                        {
                            **resolutions[0],
                            "run_id": (
                                "99999999-9999-4999-8999-999999999999"
                            ),
                        },
                    ),
                    "run ID does not match",
                ),
                (
                    "duplicate IDs",
                    lambda: _write_json_fixture(
                        resolution_root / "002-resolution.json",
                        {
                            **resolutions[1],
                            "resolution_id": resolutions[0]["resolution_id"],
                        },
                    ),
                    "duplicate Developer resolution IDs",
                ),
                (
                    "non-increasing timestamps",
                    lambda: _write_json_fixture(
                        resolution_root / "002-resolution.json",
                        {
                            **resolutions[1],
                            "created_at": resolutions[0]["created_at"],
                        },
                    ),
                    "strictly increasing created_at",
                ),
            )
            for label, mutate, message in invalid_cases:
                with self.subTest(case=label):
                    restore_inputs()
                    mutate()
                    with self.assertRaisesRegex(
                        ReviewSubmissionError,
                        message,
                    ):
                        _validate_bundle_inputs(prepared.bundle, request)

            restore_inputs()
            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            self.assertEqual(len(_result_prompt_events(prepared)), 1)

    def test_previous_review_verdict_controls_required_bundle_inputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared, _ = _prepare_multi_input_round(
                Path(temporary_directory)
            )
            previous_review_path = (
                prepared.bundle / "input/previous-review.json"
            )
            original_previous = json.loads(
                previous_review_path.read_text(encoding="utf-8")
            )

            def candidate(
                verdict: str,
                **request_updates: object,
            ) -> tuple[ReviewRequest, dict[str, object]]:
                request_value = copy.deepcopy(prepared.request)
                request_value.update(request_updates)
                previous = _review_result(original_previous, verdict)
                return ReviewRequest.from_dict(request_value), previous

            wrong_mode_request, wrong_mode_review = candidate(
                "needs_human",
                mode="reconsideration",
                previous_response_path=None,
            )
            wrong_mode_review["head_oid"] = wrong_mode_request.head_oid
            cases = (
                (
                    candidate(
                        "changes_requested",
                        previous_response_path=None,
                    ),
                    "changes_requested must reference the previous review "
                    "and response",
                ),
                (
                    (wrong_mode_request, wrong_mode_review),
                    "request after needs_human must use new_revision",
                ),
                (
                    candidate("needs_human"),
                    "needs_human result does not accept a previous response",
                ),
                (
                    candidate(
                        "needs_human",
                        previous_response_path=None,
                        resolution_paths=[],
                    ),
                    "request after needs_human must include a Developer "
                    "resolution",
                ),
                (
                    candidate("approved"),
                    "follow-up request cannot use a previous review with "
                    "verdict approved",
                ),
            )
            for (request, previous), message in cases:
                with self.subTest(message=message):
                    _write_json_fixture(previous_review_path, previous)
                    with self.assertRaisesRegex(
                        ReviewSubmissionError,
                        message,
                    ):
                        _validate_bundle_inputs(prepared.bundle, request)

    def test_new_revision_bundle_cannot_reuse_the_reviewed_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared, _ = _prepare_multi_input_round(
                Path(temporary_directory)
            )
            previous_review_path = (
                prepared.bundle / "input/previous-review.json"
            )
            previous_response_path = (
                prepared.bundle / "input/previous-response.json"
            )
            previous_review = json.loads(
                previous_review_path.read_text(encoding="utf-8")
            )
            previous_review["head_oid"] = prepared.request["head_oid"]
            _write_json_fixture(previous_review_path, previous_review)
            previous_response = json.loads(
                previous_response_path.read_text(encoding="utf-8")
            )
            previous_response["reviewed_head_oid"] = prepared.request[
                "head_oid"
            ]
            _write_json_fixture(previous_response_path, previous_response)

            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(submitted.returncode, 1)
            self.assertIn(
                "a new_revision submission after changes_requested requires "
                "a new committed HEAD",
                submitted.stderr,
            )
            self.assertFalse((prepared.bundle / "local-state.json").exists())
            self.assertEqual(_result_prompt_events(prepared), [])

    def test_reconsideration_bundle_cannot_use_a_changed_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared, _ = _prepare_multi_input_round(
                Path(temporary_directory)
            )
            request = copy.deepcopy(prepared.request)
            request["mode"] = "reconsideration"
            prepared = _write_request(prepared, request)

            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(submitted.returncode, 1)
            self.assertIn(
                "a reconsideration submission must keep the exact "
                "previously reviewed HEAD",
                submitted.stderr,
            )
            self.assertFalse((prepared.bundle / "local-state.json").exists())
            self.assertEqual(_result_prompt_events(prepared), [])

    def test_correction_bundle_cannot_hide_previous_review_inputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared, _ = _prepare_multi_input_round(
                Path(temporary_directory)
            )
            request = copy.deepcopy(prepared.request)
            request["previous_review_path"] = None
            request["previous_response_path"] = None
            prepared = _write_request(prepared, request)
            (prepared.bundle / "input/previous-review.json").unlink()
            (prepared.bundle / "input/previous-response.json").unlink()

            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(submitted.returncode, 1)
            self.assertIn(
                "a correction-round request must reference the previous "
                "review and response",
                submitted.stderr,
            )
            self.assertFalse((prepared.bundle / "local-state.json").exists())
            self.assertEqual(_result_prompt_events(prepared), [])

    def test_multi_input_document_validation_failures_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared, _ = _prepare_multi_input_round(
                Path(temporary_directory)
            )
            previous_review_path = (
                prepared.bundle / "input/previous-review.json"
            )
            resolution_path = (
                prepared.bundle / "input/resolutions/001-resolution.json"
            )
            previous_review = json.loads(
                previous_review_path.read_text(encoding="utf-8")
            )
            resolution = json.loads(
                resolution_path.read_text(encoding="utf-8")
            )
            cases = (
                (
                    "shape-invalid previous review",
                    previous_review_path,
                    {"schema_version": 1, "verdict": "approved"},
                    "previous review failed validation: review result is "
                    "missing required field(s): base_oid, created_at, "
                    "findings, head_oid, non_blocking_observations, "
                    "request_id, result_id, round, run_id, summary",
                ),
                (
                    "shape-invalid Developer resolution",
                    resolution_path,
                    {"schema_version": 1},
                    "Developer resolution 1 failed validation: Developer "
                    "resolution 1 is missing required field(s): "
                    "additional_rounds_granted, applies_to_finding_ids, "
                    "created_at, resolution_id, resolution_path, "
                    "resolution_sha256, resolves_escalation_id, run_id",
                ),
            )
            for case, path, invalid_value, message in cases:
                with self.subTest(case=case):
                    _write_json_fixture(
                        previous_review_path,
                        previous_review,
                    )
                    _write_json_fixture(resolution_path, resolution)
                    _write_json_fixture(path, invalid_value)

                    submitted = run_cli(
                        prepared.review_worktree,
                        "review-submit",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )

                    self.assertEqual(submitted.returncode, 1)
                    self.assertEqual(
                        submitted.stderr,
                        f"agent-squad: error: {message}\n",
                    )
                    self.assertFalse(
                        (prepared.bundle / "local-state.json").exists()
                    )
                    self.assertEqual(_result_prompt_events(prepared), [])

    def test_previous_response_without_review_is_a_request_shape_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared, _ = _prepare_multi_input_round(
                Path(temporary_directory)
            )
            request_value = copy.deepcopy(prepared.request)
            request_value["previous_review_path"] = None
            request = ReviewRequest.from_dict(request_value)
            (
                prepared.bundle / "input/previous-review.json"
            ).unlink()

            with self.assertRaisesRegex(
                ReviewSubmissionError,
                "^a previous response requires a previous review$",
            ):
                _validate_bundle_inputs(prepared.bundle, request)

    def test_previous_round_result_id_is_rejected_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared, _ = _prepare_multi_input_round(
                Path(temporary_directory)
            )
            previous_review = json.loads(
                (
                    prepared.bundle / "input/previous-review.json"
                ).read_text(encoding="utf-8")
            )
            review = _write_review(prepared, verdict="changes_requested")
            review["result_id"] = previous_review["result_id"]
            _write_json_fixture(
                prepared.bundle / "output/review.json",
                review,
            )

            submitted = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(submitted.returncode, 1)
            self.assertIn(
                "review result.result_id repeats the result ID of round 1; "
                "each result needs a fresh ID",
                submitted.stderr,
            )
            self.assertFalse((prepared.bundle / "local-state.json").exists())
            self.assertEqual(_result_prompt_events(prepared), [])

    def test_bundle_input_and_review_shape_violations_are_rejected(
        self,
    ) -> None:
        cases = (
            (
                "extra bundle input file",
                "review bundle input files do not match the request "
                "(unexpected: input/context/notes.md)",
            ),
            (
                "unlistable bundle input directory",
                "cannot inspect review bundle:",
            ),
            (
                "extra bundle input directory",
                "review bundle input contains unexpected or missing "
                "directories",
            ),
            (
                "missing required bundle input",
                "input/task.md is missing",
            ),
            (
                "blank human-readable review",
                "human-readable review must contain non-whitespace text",
            ),
            (
                "malformed review JSON",
                "review result contains invalid JSON",
            ),
            (
                "non-UTF-8 review JSON",
                "review result must contain UTF-8 JSON",
            ),
            (
                "non-UTF-8 human-readable review",
                "human-readable review must contain UTF-8 Markdown",
            ),
            (
                "null byte in human-readable review",
                "human-readable review must not contain null bytes",
            ),
            (
                "shape-invalid review request",
                "review request failed validation: review request "
                "is missing required field(s): head_oid",
            ),
        )
        for case, message in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    prepared = _prepare_round(Path(temporary_directory))
                    _write_review(prepared)
                    context = prepared.bundle / "input/context"
                    if case == "extra bundle input file":
                        context.mkdir()
                        (context / "notes.md").write_text(
                            "smuggled reviewer input\n",
                            encoding="utf-8",
                        )
                    elif case == "unlistable bundle input directory":
                        context.mkdir()
                        notes = context / "notes.md"
                        notes.write_text(
                            "declared reviewer context\n",
                            encoding="utf-8",
                        )
                        request = copy.deepcopy(prepared.request)
                        request["context_files"] = [
                            {
                                "path": "input/context/notes.md",
                                "sha256": hashlib.sha256(
                                    notes.read_bytes()
                                ).hexdigest(),
                            }
                        ]
                        _write_request(prepared, request)
                        context.chmod(0o111)
                    elif case == "extra bundle input directory":
                        (prepared.bundle / "input/scratch").mkdir()
                    elif case == "missing required bundle input":
                        (prepared.bundle / "input/task.md").unlink()
                    elif case == "blank human-readable review":
                        (prepared.bundle / "output/review.md").write_text(
                            "   \n\n",
                            encoding="utf-8",
                        )
                    elif case == "malformed review JSON":
                        (prepared.bundle / "output/review.json").write_text(
                            "{not json",
                            encoding="utf-8",
                        )
                    elif case == "non-UTF-8 review JSON":
                        review_json = (
                            prepared.bundle / "output/review.json"
                        )
                        review_json.write_bytes(
                            review_json.read_bytes().replace(
                                b'"summary": "',
                                b'"summary": "\xff',
                                1,
                            )
                        )
                    elif case == "non-UTF-8 human-readable review":
                        (prepared.bundle / "output/review.md").write_bytes(
                            b"# Review\n\xff\xfe not utf-8\n"
                        )
                    elif case == "null byte in human-readable review":
                        (prepared.bundle / "output/review.md").write_text(
                            "# Review\n\x00 embedded\n",
                            encoding="utf-8",
                        )
                    elif case == "shape-invalid review request":
                        request = copy.deepcopy(prepared.request)
                        del request["head_oid"]
                        _write_request(prepared, request)

                    try:
                        submitted = run_cli(
                            prepared.review_worktree,
                            "review-submit",
                            data_home=prepared.data_home,
                            env_overrides=prepared.environment,
                        )
                    finally:
                        if case == "unlistable bundle input directory":
                            context.chmod(0o755)

                    self.assertEqual(submitted.returncode, 1)
                    self.assertIn(message, submitted.stderr)
                    self.assertFalse(
                        (prepared.bundle / "local-state.json").exists()
                    )
                    self.assertEqual(_result_prompt_events(prepared), [])

    def test_integrity_violations_create_no_marker_or_prompt(self) -> None:
        cases = (
            (
                "request identity",
                "review request Reviewer name does not match its run and "
                "round",
            ),
            (
                "round worktree identity",
                "review-submit must run from round-002, not round-001",
            ),
            (
                "run worktree identity",
                "review worktree path does not match the request run ID",
            ),
            (
                "object format mismatch",
                "review worktree Git object format does not match the "
                "request",
            ),
            (
                "attached review worktree",
                "review-submit requires the expected detached review "
                "worktree",
            ),
            (
                "unavailable base commit",
                "review request base object is not an available commit",
            ),
            (
                "base outside review history",
                "review request base is not an ancestor of its head",
            ),
            (
                "staged tracked file",
                "review worktree index differs from HEAD",
            ),
            (
                "missing bundle exclusion",
                ".agent-squad-review is not Git-excluded in the review "
                "worktree",
            ),
            (
                "review identity",
                "review result request ID does not match the review request",
            ),
            ("task digest", "review bundle digest mismatch for input/task.md"),
            (
                "tracked file",
                "review worktree tracked files differ from HEAD",
            ),
            (
                "assume-unchanged tracked file",
                "tracked review file differs from HEAD: feature.txt",
            ),
            (
                "assume-unchanged tracked symlink",
                "tracked review file differs from HEAD: feature-link",
            ),
            (
                "assume-unchanged tracked symlink changed type",
                "tracked review symlink changed type: feature-link",
            ),
            (
                "assume-unchanged tracked executable mode",
                "tracked review file mode differs from HEAD: feature.txt",
            ),
            (
                "assume-unchanged tracked file removed",
                "tracked review file is missing: feature.txt",
            ),
            (
                "assume-unchanged tracked file changed type",
                "tracked review file changed type: feature.txt",
            ),
            (
                "skip-worktree tracked parent changed type",
                "tracked review directory changed type: nested/deeper\n",
            ),
            ("changed head", "review worktree HEAD is"),
            (
                "missing review bundle",
                "review bundle is missing: ",
            ),
            (
                "bundle input replaced by a file",
                "review bundle input must be a normal directory: ",
            ),
            (
                "missing bundle output directory",
                "review bundle output is missing: ",
            ),
            (
                "unexpected bundle location",
                "review bundle contains files outside documented input, "
                "output, marker, and retired-result locations: scratch.txt",
            ),
            (
                "unexpected worktree file",
                "review worktree contains tracked changes or unexpected "
                "non-ignored files outside the review bundle",
            ),
            ("case-colliding input paths", "case-colliding paths"),
            (
                "symlinked bundle directory",
                "review bundle directory must not be a symlink",
            ),
            (
                "symlinked review",
                "review bundle output file must be a regular non-symlink file",
            ),
            ("missing Markdown", "human-readable review is missing"),
        )
        for case, message in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    include_tracked_symlink = case in {
                        "assume-unchanged tracked symlink",
                        "assume-unchanged tracked symlink changed type",
                    }
                    include_nested_file = (
                        case == "skip-worktree tracked parent changed type"
                    )
                    prepared = _prepare_round(
                        root,
                        include_tracked_symlink=include_tracked_symlink,
                        include_nested_tracked_file=include_nested_file,
                    )
                    review = _write_review(prepared)
                    if case == "request identity":
                        request = dict(prepared.request)
                        request["reviewer_name"] = "asq-wrong-r001-reviewer"
                        _write_request(prepared, request)
                    elif case == "round worktree identity":
                        request = dict(prepared.request)
                        request["round"] = 2
                        request["reviewer_name"] = deterministic_reviewer_name(
                            str(request["run_id"]),
                            2,
                        )
                        _write_request(prepared, request)
                    elif case == "run worktree identity":
                        request = dict(prepared.request)
                        request["run_id"] = (
                            "99999999-9999-4999-8999-999999999999"
                        )
                        request["reviewer_name"] = deterministic_reviewer_name(
                            str(request["run_id"]),
                            int(request["round"]),
                        )
                        _write_request(prepared, request)
                    elif case == "object format mismatch":
                        request = dict(prepared.request)
                        object_format = str(request["git_object_format"])
                        mismatched_format = (
                            "sha256" if object_format == "sha1" else "sha1"
                        )
                        oid_length = (
                            64 if mismatched_format == "sha256" else 40
                        )
                        request["git_object_format"] = mismatched_format
                        request["base_oid"] = "0" * oid_length
                        request["head_oid"] = "1" * oid_length
                        _write_request(prepared, request)
                    elif case == "attached review worktree":
                        run(
                            ["git", "checkout", "-b", "attached-review"],
                            cwd=prepared.review_worktree,
                        )
                    elif case == "unavailable base commit":
                        request = dict(prepared.request)
                        request["base_oid"] = "f" * len(
                            str(request["base_oid"])
                        )
                        _write_request(prepared, request)
                    elif case == "base outside review history":
                        run(
                            [
                                "git",
                                "-c",
                                "commit.gpgSign=false",
                                "commit",
                                "--allow-empty",
                                "--no-verify",
                                "-m",
                                "test: create non-ancestor base",
                            ],
                            cwd=prepared.repository,
                        )
                        non_ancestor = run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=prepared.repository,
                        ).stdout.strip()
                        request = dict(prepared.request)
                        request["base_oid"] = non_ancestor
                        _write_request(prepared, request)
                    elif case == "staged tracked file":
                        tracked_file = prepared.review_worktree / "feature.txt"
                        tracked_file.write_text(
                            "staged change\n",
                            encoding="utf-8",
                        )
                        run(
                            ["git", "add", "feature.txt"],
                            cwd=prepared.review_worktree,
                        )
                    elif case == "missing bundle exclusion":
                        request = dict(prepared.request)
                        request["allowed_generated_paths"] = [
                            ".agent-squad-review/"
                        ]
                        _write_request(prepared, request)
                        exclude_path = (
                            prepared.repository / ".git/info/exclude"
                        )
                        exclude_lines = exclude_path.read_text(
                            encoding="utf-8"
                        ).splitlines(keepends=True)
                        exclude_path.write_text(
                            "".join(
                                line
                                for line in exclude_lines
                                if line.strip() != ".agent-squad-review/"
                            ),
                            encoding="utf-8",
                        )
                    elif case == "review identity":
                        review["request_id"] = (
                            "99999999-9999-4999-8999-999999999999"
                        )
                        _write_json_fixture(
                            prepared.bundle / "output/review.json",
                            review,
                        )
                    elif case == "task digest":
                        task_path = prepared.bundle / "input/task.md"
                        task_path.chmod(0o600)
                        task_path.write_text("tampered\n", encoding="utf-8")
                    elif case == "tracked file":
                        (prepared.review_worktree / "feature.txt").write_text(
                            "changed\n",
                            encoding="utf-8",
                        )
                    elif case == "assume-unchanged tracked file":
                        _hide_tracked_path(
                            prepared.review_worktree,
                            "feature.txt",
                        )
                        (prepared.review_worktree / "feature.txt").write_text(
                            "hidden change\n",
                            encoding="utf-8",
                        )
                    elif case == "assume-unchanged tracked symlink":
                        _hide_tracked_path(
                            prepared.review_worktree,
                            "feature-link",
                        )
                        tracked_link = (
                            prepared.review_worktree / "feature-link"
                        )
                        tracked_link.unlink()
                        tracked_link.symlink_to("README.md")
                    elif case == (
                        "assume-unchanged tracked symlink changed type"
                    ):
                        _hide_tracked_path(
                            prepared.review_worktree,
                            "feature-link",
                        )
                        tracked_link = (
                            prepared.review_worktree / "feature-link"
                        )
                        tracked_link.unlink()
                        tracked_link.write_text(
                            "not a symlink\n",
                            encoding="utf-8",
                        )
                    elif case == "assume-unchanged tracked executable mode":
                        _hide_tracked_path(
                            prepared.review_worktree,
                            "feature.txt",
                        )
                        (prepared.review_worktree / "feature.txt").chmod(
                            0o755
                        )
                    elif case == "assume-unchanged tracked file removed":
                        _hide_tracked_path(
                            prepared.review_worktree,
                            "feature.txt",
                        )
                        (prepared.review_worktree / "feature.txt").unlink()
                    elif case == (
                        "assume-unchanged tracked file changed type"
                    ):
                        _hide_tracked_path(
                            prepared.review_worktree,
                            "feature.txt",
                        )
                        tracked_file = (
                            prepared.review_worktree / "feature.txt"
                        )
                        tracked_file.unlink()
                        tracked_file.symlink_to("README.md")
                    elif case == "skip-worktree tracked parent changed type":
                        _hide_tracked_path(
                            prepared.review_worktree,
                            "nested/deeper/leaf/feature.txt",
                            flag="--skip-worktree",
                        )
                        nested_file = (
                            prepared.review_worktree
                            / "nested/deeper/leaf/feature.txt"
                        )
                        nested_file.unlink()
                        leaf_directory = nested_file.parent
                        leaf_directory.rmdir()
                        deeper_directory = leaf_directory.parent
                        deeper_directory.rmdir()
                        deeper_directory.symlink_to(
                            root,
                            target_is_directory=True,
                        )
                    elif case == "changed head":
                        run(
                            [
                                "git",
                                "-c",
                                "user.name=Agent Squad Tests",
                                "-c",
                                "user.email=agent-squad@example.invalid",
                                "-c",
                                "commit.gpgSign=false",
                                "commit",
                                "--allow-empty",
                                "--no-verify",
                                "-m",
                                "test: advance review head",
                            ],
                            cwd=prepared.review_worktree,
                        )
                    elif case == "missing review bundle":
                        shutil.rmtree(prepared.bundle)
                    elif case == "bundle input replaced by a file":
                        shutil.rmtree(prepared.bundle / "input")
                        (prepared.bundle / "input").write_text(
                            "not a directory\n",
                            encoding="utf-8",
                        )
                    elif case == "missing bundle output directory":
                        shutil.rmtree(prepared.bundle / "output")
                    elif case == "unexpected bundle location":
                        (prepared.bundle / "scratch.txt").write_text(
                            "unexpected\n",
                            encoding="utf-8",
                        )
                    elif case == "unexpected worktree file":
                        (prepared.review_worktree / "scratch.txt").write_text(
                            "unexpected\n",
                            encoding="utf-8",
                        )
                    elif case == "case-colliding input paths":
                        context_root = prepared.bundle / "input/context"
                        context_root.mkdir()
                        content = b"same content\n"
                        (context_root / "Plan.md").write_bytes(content)
                        (context_root / "plan.md").write_bytes(content)
                        request = copy.deepcopy(prepared.request)
                        request["context_files"] = [
                            {
                                "path": "input/context/Plan.md",
                                "sha256": hashlib.sha256(content).hexdigest(),
                            },
                            {
                                "path": "input/context/plan.md",
                                "sha256": hashlib.sha256(content).hexdigest(),
                            },
                        ]
                        prepared = _write_request(prepared, request)
                    elif case == "symlinked bundle directory":
                        nested = prepared.bundle / "input/nested"
                        nested.mkdir()
                        nested.rmdir()
                        nested.symlink_to(root, target_is_directory=True)
                    elif case == "symlinked review":
                        review_markdown = prepared.bundle / "output/review.md"
                        review_markdown.unlink()
                        review_markdown.symlink_to(root / "outside-review.md")
                    elif case == "missing Markdown":
                        (prepared.bundle / "output/review.md").unlink()

                    submitted = run_cli(
                        prepared.review_worktree,
                        "review-submit",
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )

                    self.assertEqual(submitted.returncode, 1)
                    self.assertIn(message, submitted.stderr)
                    self.assertFalse(
                        (prepared.bundle / "local-state.json").exists()
                    )
                    self.assertEqual(_result_prompt_events(prepared), [])

    def test_marker_confirmed_review_content_cannot_be_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            review = _write_review(prepared)
            failing_environment = dict(prepared.environment)
            failing_environment["FAKE_HERDR_FAIL_PROMPT"] = "1"
            first = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=failing_environment,
            )
            self.assertEqual(first.returncode, 1)
            marker_path = prepared.bundle / "local-state.json"
            marker_bytes = marker_path.read_bytes()

            review["summary"] = "A different marker-confirmed review."
            _write_json_fixture(
                prepared.bundle / "output/review.json",
                review,
            )
            rejected = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(rejected.returncode, 1)
            self.assertIn("review digest cannot be changed", rejected.stderr)
            self.assertEqual(marker_path.read_bytes(), marker_bytes)
            self.assertEqual(len(_result_prompt_events(prepared)), 1)


if __name__ == "__main__":
    unittest.main()
