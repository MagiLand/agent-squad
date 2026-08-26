from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest

from tests._support import (
    add_src_to_path,
    install_fake_herdr,
    run,
    run_cli,
    seed_git_repository,
)


add_src_to_path()


@dataclass(frozen=True)
class _PreparedRound:
    repository: Path
    data_home: Path
    environment: dict[str, str]
    review_worktree: Path
    bundle: Path
    request: dict[str, object]


def _prepare_round(root: Path) -> _PreparedRound:
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
    run(["git", "add", "feature.txt"], cwd=repository)
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
    (output / "review.json").write_text(
        f"{json.dumps(review, indent=2)}\n",
        encoding="utf-8",
    )
    (output / "review.md").write_text(
        f"# Review\n\n{review['summary']}\n",
        encoding="utf-8",
    )
    return review


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
            self.assertIn(
                "marker-confirmed result remains valid",
                first.stderr,
            )
            marker_path = prepared.bundle / "local-state.json"
            marker_bytes = marker_path.read_bytes()
            marker = json.loads(marker_bytes)
            self.assertEqual(marker["result_id"], review["result_id"])

            retried = run_cli(
                prepared.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertIn("Reviewer-local marker reused", retried.stdout)
            self.assertEqual(marker_path.read_bytes(), marker_bytes)
            prompts = _result_prompt_events(prepared)
            self.assertEqual(len(prompts), 2)
            self.assertTrue(
                all(
                    event["review_marker_at_prompt"]["result_id"]
                    == review["result_id"]
                    for event in prompts
                )
            )

    def test_rejected_result_can_be_corrected_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            prepared = _prepare_round(Path(temporary_directory))
            review = _write_review(prepared)
            review["verdict"] = "changes_requested"
            review["findings"] = []
            review_path = prepared.bundle / "output/review.json"
            review_path.write_text(
                f"{json.dumps(review, indent=2)}\n",
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

    def test_integrity_violations_create_no_marker_or_prompt(self) -> None:
        cases = (
            "request identity",
            "review identity",
            "task digest",
            "tracked file",
            "assume-unchanged tracked file",
            "changed head",
            "unexpected bundle location",
            "unexpected worktree file",
            "symlinked review",
            "missing Markdown",
        )
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    prepared = _prepare_round(root)
                    review = _write_review(prepared)
                    if case == "request identity":
                        request_path = prepared.bundle / "input/request.json"
                        request_path.chmod(0o600)
                        request = dict(prepared.request)
                        request["reviewer_name"] = "asq-wrong-r001-reviewer"
                        request_path.write_text(
                            f"{json.dumps(request, indent=2)}\n",
                            encoding="utf-8",
                        )
                    elif case == "review identity":
                        review["request_id"] = (
                            "99999999-9999-4999-8999-999999999999"
                        )
                        (prepared.bundle / "output/review.json").write_text(
                            f"{json.dumps(review, indent=2)}\n",
                            encoding="utf-8",
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
                            "hidden change\n",
                            encoding="utf-8",
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
            (prepared.bundle / "output/review.json").write_text(
                f"{json.dumps(review, indent=2)}\n",
                encoding="utf-8",
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
