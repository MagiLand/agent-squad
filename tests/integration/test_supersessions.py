from __future__ import annotations

import copy
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path, run, run_cli
from tests.integration.test_review_applications import (
    _write_fixed_response,
    _write_rejected_response,
)
from tests.integration.test_review_submissions import (
    _PreparedRound,
    _prepare_round,
    _write_review,
)


add_src_to_path()

from agent_squad import (  # noqa: E402
    review_applications,
    review_submissions,
    runs,
)
from agent_squad.artifacts import ReviewRoundRecord  # noqa: E402
from agent_squad.herdr import HerdrError, HerdrInstallation  # noqa: E402


def _state_and_round(
    repository: Path,
) -> tuple[dict[str, object], Path, dict[str, object]]:
    state = json.loads(
        (repository / ".agent-squad/state.json").read_text(encoding="utf-8")
    )
    run_id = state["active_run_id"]
    round_number = state["current_round"]
    if not isinstance(run_id, str) or not isinstance(round_number, int):
        raise AssertionError("active run identity must be persisted")
    round_directory = (
        repository
        / ".agent-squad/runs"
        / run_id
        / "rounds"
        / f"{round_number:03d}"
    )
    round_record = json.loads(
        (round_directory / "round.json").read_text(encoding="utf-8")
    )
    return state, round_directory, round_record


class _LateSubmittingReviewerClient:
    def __init__(self, prepared: _PreparedRound) -> None:
        self.prepared = prepared
        self.phase_at_notice: str | None = None

    def discover(self, *_args: object, **_kwargs: object) -> HerdrInstallation:
        return HerdrInstallation(
            executable=Path("/fake/herdr"),
            version="herdr test-0.8.2",
            protocol=20,
        )

    def dispatch_reviewer_notice(self, **_kwargs: object) -> bool:
        state = json.loads(
            (
                self.prepared.repository / ".agent-squad/state.json"
            ).read_text(encoding="utf-8")
        )
        self.phase_at_notice = state["phase"]
        result_client = mock.Mock()
        review_submissions.submit_review_result(
            self.prepared.review_worktree,
            herdr_client=result_client,
        )
        return True


def _start_recovery_round(root: Path) -> tuple[_PreparedRound, _PreparedRound]:
    prepared = _prepare_round(root)
    superseded = run_cli(
        prepared.repository,
        "supersede",
        "--reason",
        "The original review is obsolete.",
        data_home=prepared.data_home,
        env_overrides=prepared.environment,
    )
    if superseded.returncode != 0:
        raise AssertionError(superseded.stderr)
    report = root / "recovery-report.md"
    report.write_text(
        "# Recovery Report\n\nResubmit the unchanged candidate.\n",
        encoding="utf-8",
    )
    submitted = run_cli(
        prepared.repository,
        "submit",
        "--report",
        str(report),
        "--mode",
        "new_revision",
        data_home=prepared.data_home,
        env_overrides=prepared.environment,
    )
    if submitted.returncode != 0:
        raise AssertionError(submitted.stderr)
    state, _, _ = _state_and_round(prepared.repository)
    review_worktree = Path(state["active_round"]["review_worktree"])
    request = json.loads(
        (
            review_worktree / ".agent-squad-review/input/request.json"
        ).read_text(encoding="utf-8")
    )
    return prepared, _PreparedRound(
        repository=prepared.repository,
        data_home=prepared.data_home,
        environment=prepared.environment,
        review_worktree=review_worktree,
        bundle=review_worktree / ".agent-squad-review",
        request=request,
    )


def _prepared_from_active(prepared: _PreparedRound) -> _PreparedRound:
    state, _, _ = _state_and_round(prepared.repository)
    active_round = state["active_round"]
    if not isinstance(active_round, dict):
        raise AssertionError("active round must be persisted")
    review_worktree = Path(str(active_round["review_worktree"]))
    bundle = review_worktree / ".agent-squad-review"
    request = json.loads(
        (bundle / "input/request.json").read_text(encoding="utf-8")
    )
    return _PreparedRound(
        repository=prepared.repository,
        data_home=prepared.data_home,
        environment=prepared.environment,
        review_worktree=review_worktree,
        bundle=bundle,
        request=request,
    )


def _apply_changes_requested(
    prepared: _PreparedRound,
) -> dict[str, object]:
    review = _write_review(prepared, verdict="changes_requested")
    submitted = run_cli(
        prepared.review_worktree,
        "review-submit",
        data_home=prepared.data_home,
        env_overrides=prepared.environment,
    )
    if submitted.returncode != 0:
        raise AssertionError(submitted.stderr)
    applied = run_cli(
        prepared.repository,
        "apply-review",
        "--result-id",
        str(review["result_id"]),
        data_home=prepared.data_home,
        env_overrides=prepared.environment,
    )
    if applied.returncode != 0:
        raise AssertionError(applied.stderr)
    return review


class SupersedeReviewTests(unittest.TestCase):
    def test_reason_must_be_nonempty_single_line_before_state_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            state_path = prepared.repository / ".agent-squad/state.json"
            state_before = state_path.read_bytes()

            for reason, expected in (
                ("   ", "must contain non-whitespace text"),
                ("obsolete\nreplaced", "must be a single line"),
            ):
                with self.subTest(reason=reason):
                    rejected = run_cli(
                        prepared.repository,
                        "supersede",
                        "--reason",
                        reason,
                        data_home=prepared.data_home,
                        env_overrides=prepared.environment,
                    )
                    self.assertEqual(rejected.returncode, 1)
                    self.assertIn(expected, rejected.stderr)
                    self.assertEqual(state_path.read_bytes(), state_before)

    def test_transition_rolls_back_round_run_event_and_state_together(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            state, round_directory, _ = _state_and_round(prepared.repository)
            run_directory = round_directory.parents[1]
            paths = (
                round_directory / "round.json",
                run_directory / "run.json",
                run_directory / "events.jsonl",
                prepared.repository / ".agent-squad/state.json",
            )
            original = {path: path.read_bytes() for path in paths}
            real_atomic_write = review_applications.atomic_write

            def fail_state_write(
                path: Path,
                content: bytes,
                *,
                mode: int,
            ) -> None:
                if path.name == "state.json":
                    raise OSError("injected state failure")
                real_atomic_write(path, content, mode=mode)

            with mock.patch.object(
                review_applications,
                "atomic_write",
                side_effect=fail_state_write,
            ):
                with self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "could not persist superseded review state",
                ):
                    review_applications.supersede_review(
                        prepared.repository,
                        reason="The active review is stuck.",
                        herdr_client=mock.Mock(),
                    )

            self.assertEqual(state["phase"], "reviewing")
            for path, expected in original.items():
                with self.subTest(path=path):
                    self.assertEqual(path.read_bytes(), expected)
            self.assertTrue(prepared.review_worktree.is_dir())

    def test_notice_failure_does_not_block_authoritative_supersession(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            client = mock.Mock()
            client.discover.side_effect = HerdrError("Reviewer unavailable")

            result = review_applications.supersede_review(
                prepared.repository,
                reason="The request has been replaced.",
                herdr_client=client,
            )

            self.assertFalse(result.reviewer_notice_sent)
            self.assertEqual(
                result.reviewer_notice_error,
                "Reviewer unavailable",
            )
            state, _, round_record = _state_and_round(prepared.repository)
            self.assertEqual(state["phase"], "implementing")
            self.assertEqual(round_record["status"], "superseded")
            self.assertFalse(prepared.review_worktree.exists())

    def test_command_records_history_notifies_and_allows_same_head_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            before, first_round_directory, _ = _state_and_round(
                prepared.repository
            )
            first_head = before["current_head_oid"]
            first_reviewer = before["active_round"]["reviewer_name"]
            budget_before = before["review_budget"]

            superseded = run_cli(
                prepared.repository,
                "supersede",
                "--reason",
                "The review is stuck and the request is obsolete.",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(superseded.returncode, 0, superseded.stderr)
            self.assertIn("Superseded review round 1", superseded.stdout)
            self.assertIn("Reviewer notice: sent", superseded.stdout)
            after, _, round_record = _state_and_round(prepared.repository)
            self.assertEqual(after["phase"], "implementing")
            self.assertEqual(after["active_round"]["status"], "superseded")
            self.assertEqual(after["review_budget"], budget_before)
            self.assertIsNone(after["approved_head_oid"])
            self.assertFalse(prepared.review_worktree.exists())
            self.assertEqual(round_record["status"], "superseded")
            self.assertEqual(
                round_record["supersession"]["actor"],
                "codex-main",
            )
            self.assertEqual(
                round_record["supersession"]["cause"],
                "The review is stuck and the request is obsolete.",
            )
            self.assertEqual(
                round_record["supersession"]["created_at"],
                round_record["updated_at"],
            )
            bundle_manifest = round_record["artifacts"]["bundle_archive"]
            self.assertTrue(bundle_manifest)
            archived_paths = {item["path"] for item in bundle_manifest}
            self.assertIn("bundle/input/request.json", archived_paths)
            self.assertIn("bundle/input/task.md", archived_paths)
            self.assertTrue(
                (first_round_directory / "bundle/input/request.json").is_file()
            )
            events = [
                json.loads(line)
                for line in (
                    first_round_directory.parents[1] / "events.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            transition = [
                event
                for event in events
                if event["event"] == "review_superseded"
            ]
            self.assertEqual(len(transition), 1)
            self.assertEqual(transition[0]["actor"], "codex-main")
            self.assertEqual(
                transition[0]["cause"],
                "The review is stuck and the request is obsolete.",
            )

            invocations = [
                json.loads(line)
                for line in (
                    Path(prepared.environment["FAKE_HERDR_STATE_DIR"])
                    / "invocations.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            notices = [
                event
                for event in invocations
                if event["arguments"][:2] == ["agent", "prompt"]
                and len(event["arguments"]) > 3
                and "REVIEW_SUPERSEDED" in event["arguments"][3]
            ]
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0]["phase_at_prompt"], "implementing")
            self.assertEqual(notices[0]["arguments"][2], first_reviewer)
            notice_prompt = notices[0]["arguments"][3]
            self.assertTrue(
                notice_prompt.startswith(
                    "AGENT_SQUAD/0.4.4 REVIEW_SUPERSEDED\n"
                )
            )
            self.assertIn(f"run_id: {before['active_run_id']}", notice_prompt)
            self.assertIn("round: 1", notice_prompt)
            self.assertIn(
                "reason: The review is stuck and the request is obsolete.",
                notice_prompt,
            )

            state_before_rejected_retry = (
                prepared.repository / ".agent-squad/state.json"
            ).read_bytes()
            rejected_retry = run_cli(
                prepared.repository,
                "supersede",
                "--reason",
                "duplicate request",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(rejected_retry.returncode, 1)
            self.assertIn(
                "supersede requires an active reviewing round",
                rejected_retry.stderr,
            )
            self.assertEqual(
                (prepared.repository / ".agent-squad/state.json").read_bytes(),
                state_before_rejected_retry,
            )

            recovery_report = root / "recovery-report.md"
            recovery_report.write_text(
                "# Recovery Report\n\nResubmit the unchanged candidate.\n",
                encoding="utf-8",
            )
            recovered = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(recovery_report),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            recovery_state, _, _ = _state_and_round(prepared.repository)
            self.assertEqual(recovery_state["current_round"], 2)
            self.assertEqual(recovery_state["current_head_oid"], first_head)
            self.assertNotEqual(
                recovery_state["active_round"]["reviewer_name"],
                first_reviewer,
            )
            recovery_worktree = Path(
                recovery_state["active_round"]["review_worktree"]
            )
            request = json.loads(
                (
                    recovery_worktree
                    / ".agent-squad-review/input/request.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(request["round"], 2)
            self.assertIsNone(request["previous_review_path"])
            self.assertIsNone(request["previous_response_path"])
            self.assertEqual(
                request["recovery_round_path"],
                "input/recovery-round.json",
            )
            recovery_authority = json.loads(
                (
                    recovery_worktree
                    / ".agent-squad-review/input/recovery-round.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(recovery_authority["status"], "superseded")
            self.assertEqual(recovery_authority["round"], 1)

            recovered_round = _PreparedRound(
                repository=prepared.repository,
                data_home=prepared.data_home,
                environment=prepared.environment,
                review_worktree=recovery_worktree,
                bundle=recovery_worktree / ".agent-squad-review",
                request=request,
            )
            _write_review(recovered_round)
            review_submitted = run_cli(
                recovery_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(
                review_submitted.returncode,
                0,
                review_submitted.stderr,
            )

    def test_same_head_recovery_after_superseded_reconsideration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            review = _apply_changes_requested(prepared)
            response_path = root / "reconsideration-response.json"
            _write_rejected_response(
                response_path,
                prepared,
                review,
            )
            report_path = root / "reconsideration-report.md"
            report_path.write_text(
                "# Implementation Report\n\nNo code change is required.\n",
                encoding="utf-8",
            )
            reconsideration = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report_path),
                "--response",
                str(response_path),
                "--mode",
                "reconsideration",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(
                reconsideration.returncode,
                0,
                reconsideration.stderr,
            )
            second_round = _prepared_from_active(prepared)

            superseded = run_cli(
                prepared.repository,
                "supersede",
                "--reason",
                "The reconsideration Reviewer did not complete.",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(superseded.returncode, 0, superseded.stderr)

            recovered = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report_path),
                "--response",
                str(response_path),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            third_round = _prepared_from_active(prepared)
            self.assertEqual(third_round.request["round"], 3)
            self.assertEqual(
                third_round.request["head_oid"],
                second_round.request["head_oid"],
            )
            self.assertEqual(
                third_round.request["head_oid"],
                review["head_oid"],
            )
            self.assertEqual(
                third_round.request["previous_review_path"],
                "input/previous-review.json",
            )
            self.assertEqual(
                third_round.request["previous_response_path"],
                "input/previous-response.json",
            )
            self.assertEqual(
                third_round.request["recovery_round_path"],
                "input/recovery-round.json",
            )
            self.assertNotEqual(
                third_round.request["reviewer_name"],
                second_round.request["reviewer_name"],
            )

            status = run_cli(
                prepared.repository,
                "status",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            _write_review(third_round)
            review_submitted = run_cli(
                third_round.review_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(
                review_submitted.returncode,
                0,
                review_submitted.stderr,
            )

    def test_recovery_cannot_roll_back_to_the_applied_review_head(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            review = _apply_changes_requested(prepared)
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
            report_path = root / "corrected-report.md"
            report_path.write_text(
                "# Implementation Report\n\nCompleted the candidate.\n",
                encoding="utf-8",
            )
            response_path = root / "fixed-response.json"
            _write_fixed_response(response_path, prepared, review)
            correction = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report_path),
                "--response",
                str(response_path),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(correction.returncode, 0, correction.stderr)
            second_round = _prepared_from_active(prepared)
            self.assertNotEqual(
                second_round.request["head_oid"],
                review["head_oid"],
            )
            superseded = run_cli(
                prepared.repository,
                "supersede",
                "--reason",
                "The corrected review became obsolete.",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(superseded.returncode, 0, superseded.stderr)
            run(
                ["git", "reset", "--hard", str(review["head_oid"])],
                cwd=prepared.repository,
            )
            state_path = prepared.repository / ".agent-squad/state.json"
            state_before = state_path.read_bytes()

            rolled_back = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report_path),
                "--response",
                str(response_path),
                "--mode",
                "new_revision",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(rolled_back.returncode, 1)
            self.assertIn(
                "a new_revision submission after changes_requested requires "
                "a new committed HEAD",
                rolled_back.stderr,
            )
            self.assertEqual(state_path.read_bytes(), state_before)
            _, round_directory, _ = _state_and_round(prepared.repository)
            self.assertFalse((round_directory.parent / "003").exists())

    def test_status_binds_prior_artifact_paths_to_round_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            review = _apply_changes_requested(prepared)
            response_path = root / "reconsideration-response.json"
            _write_rejected_response(response_path, prepared, review)
            report_path = root / "reconsideration-report.md"
            report_path.write_text(
                "# Implementation Report\n\nNo code change is required.\n",
                encoding="utf-8",
            )
            submitted = run_cli(
                prepared.repository,
                "submit",
                "--report",
                str(report_path),
                "--response",
                str(response_path),
                "--mode",
                "reconsideration",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            second_round = _prepared_from_active(prepared)
            _, round_directory, round_record = _state_and_round(
                prepared.repository
            )
            request_path = round_directory / "request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["previous_review_path"] = None
            request_bytes = review_applications.encode_json(request)
            request_digest = hashlib.sha256(request_bytes).hexdigest()
            round_record["artifacts"]["request"]["sha256"] = request_digest
            request_input = next(
                item
                for item in round_record["artifacts"]["bundle_inputs"]
                if item["path"] == "input/request.json"
            )
            request_input["sha256"] = request_digest
            for path, content in (
                (request_path, request_bytes),
                (
                    second_round.bundle / "input/request.json",
                    request_bytes,
                ),
                (
                    round_directory / "round.json",
                    review_applications.encode_json(round_record),
                ),
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
                "active review request prior-artifact paths do not match "
                "round history",
                status.stderr,
            )

    def test_cleanup_archives_result_after_state_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            review = _write_review(prepared)
            review_bytes = (
                prepared.bundle / "output/review.json"
            ).read_bytes()
            markdown_bytes = (
                prepared.bundle / "output/review.md"
            ).read_bytes()
            client = _LateSubmittingReviewerClient(prepared)

            result = review_applications.supersede_review(
                prepared.repository,
                reason="A newer implementation direction replaced this one.",
                herdr_client=client,
            )

            self.assertEqual(client.phase_at_notice, "implementing")
            self.assertEqual(result.late_result_id, review["result_id"])
            self.assertFalse(prepared.review_worktree.exists())
            state, round_directory, round_record = _state_and_round(
                prepared.repository
            )
            self.assertEqual(state["phase"], "implementing")
            self.assertEqual(
                state["review_budget"]["completed_change_reviews"],
                0,
            )
            self.assertEqual(round_record["status"], "superseded")
            self.assertIsNone(round_record["result_id"])
            self.assertIsNone(round_record["verdict"])
            archived_paths = {
                item["path"]
                for item in round_record["artifacts"]["bundle_archive"]
            }
            self.assertIn("bundle/output/review.json", archived_paths)
            self.assertIn("bundle/output/review.md", archived_paths)
            self.assertIn("bundle/local-state.json", archived_paths)
            self.assertEqual(
                (round_directory / "bundle/output/review.json").read_bytes(),
                review_bytes,
            )
            late_directory = (
                round_directory
                / "diagnostics/late-results"
                / str(review["result_id"])
            )
            self.assertEqual(
                (late_directory / "review.json").read_bytes(),
                review_bytes,
            )
            self.assertEqual(
                (late_directory / "review.md").read_bytes(),
                markdown_bytes,
            )
            marker = json.loads(
                (late_directory / "review-marker.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(marker["result_id"], review["result_id"])
            events = [
                json.loads(line)
                for line in (
                    round_directory.parents[1] / "events.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            archived_events = [
                event
                for event in events
                if event["event"] == "late_review_archived"
            ]
            self.assertEqual(len(archived_events), 1)
            self.assertEqual(
                archived_events[0]["result_id"],
                review["result_id"],
            )

            state_before_apply = (
                prepared.repository / ".agent-squad/state.json"
            ).read_bytes()
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
                "apply-review requires an active reviewing round",
                applied.stderr,
            )
            self.assertEqual(
                (prepared.repository / ".agent-squad/state.json").read_bytes(),
                state_before_apply,
            )

    def test_cleanup_retains_unrelated_worktree_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            notes = prepared.review_worktree / "reviewer-notes.md"
            notes.write_text("unfinished diagnostic notes\n", encoding="utf-8")

            superseded = run_cli(
                prepared.repository,
                "supersede",
                "--reason",
                "The review is no longer relevant.",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(superseded.returncode, 0, superseded.stderr)
            self.assertIn("retained review worktree", superseded.stderr)
            self.assertTrue(prepared.review_worktree.is_dir())
            self.assertEqual(
                notes.read_text(encoding="utf-8"),
                "unfinished diagnostic notes\n",
            )
            self.assertFalse(prepared.bundle.exists())
            state, round_directory, round_record = _state_and_round(
                prepared.repository
            )
            self.assertEqual(state["phase"], "implementing")
            self.assertTrue(round_record["artifacts"]["bundle_archive"])
            self.assertTrue(
                (round_directory / "bundle/input/request.json").is_file()
            )

    def test_cleanup_retains_worktree_when_any_identity_gate_fails(
        self,
    ) -> None:
        cases = (
            (
                "deterministic path",
                "superseded review worktree does not match its "
                "deterministic path",
            ),
            (
                "worktree root",
                "superseded cleanup target is not the review-worktree root",
            ),
            (
                "common directory",
                "superseded review worktree belongs to a different "
                "repository",
            ),
            (
                "head",
                "superseded review worktree HEAD does not match its round",
            ),
            (
                "detached head",
                "superseded review cleanup requires the detached review "
                "worktree",
            ),
            (
                "bundle digest",
                "superseded bundle input digest changed",
            ),
        )
        for mutation, expected in cases:
            with self.subTest(identity=mutation):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    prepared = _prepare_round(root)
                    repository = (
                        review_applications.load_initialized_repository(
                            prepared.repository
                        )
                    )
                    status = runs.inspect_status(prepared.repository)
                    active = status.active_run
                    self.assertIsNotNone(active)
                    assert active is not None
                    _, round_directory, round_value = _state_and_round(
                        prepared.repository
                    )
                    round_record = ReviewRoundRecord.from_dict(
                        round_value,
                        label="cleanup test round",
                    )
                    discovered = (
                        review_applications.discover_git_worktree(
                            prepared.review_worktree
                        )
                    )
                    discovery_patch = nullcontext()
                    if mutation == "deterministic path":
                        repository = replace(
                            repository,
                            configuration=replace(
                                repository.configuration,
                                review_worktree_root=(
                                    root / "different-review-root"
                                ),
                            ),
                        )
                    elif mutation == "worktree root":
                        discovery_patch = mock.patch.object(
                            review_applications,
                            "discover_git_worktree",
                            return_value=replace(
                                discovered,
                                root=discovered.root.parent,
                            ),
                        )
                    elif mutation == "common directory":
                        discovery_patch = mock.patch.object(
                            review_applications,
                            "discover_git_worktree",
                            return_value=replace(
                                discovered,
                                common_directory=(
                                    discovered.common_directory.parent
                                ),
                            ),
                        )
                    elif mutation == "head":
                        run(
                            [
                                "git",
                                "checkout",
                                "--detach",
                                str(prepared.request["base_oid"]),
                            ],
                            cwd=prepared.review_worktree,
                        )
                    elif mutation == "detached head":
                        run(
                            ["git", "switch", "-c", "review-attached"],
                            cwd=prepared.review_worktree,
                        )
                    else:
                        task_path = prepared.bundle / "input/task.md"
                        task_path.chmod(0o600)
                        task_path.write_text(
                            "tampered cleanup input\n",
                            encoding="utf-8",
                        )

                    with discovery_patch:
                        late_result_id, warnings = (
                            review_applications
                            ._cleanup_superseded_review_resources(
                                repository,
                                active=active,
                                round_directory=round_directory,
                                round_record=round_record,
                            )
                        )

                    self.assertIsNone(late_result_id)
                    self.assertEqual(len(warnings), 1)
                    self.assertIn(
                        "safe superseded-round cleanup failed",
                        warnings[0],
                    )
                    self.assertIn(expected, warnings[0])
                    self.assertTrue(prepared.review_worktree.is_dir())
                    self.assertTrue(prepared.bundle.is_dir())

    def test_reviewer_rejects_tampered_recovery_round_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, recovered = _start_recovery_round(root)
            _write_review(recovered)
            authority_path = (
                recovered.bundle / "input/recovery-round.json"
            )
            original = json.loads(authority_path.read_text(encoding="utf-8"))

            def wrong_round(value: dict[str, object]) -> None:
                value["round"] = 99

            def nonterminal_round(value: dict[str, object]) -> None:
                value["status"] = "prepared"
                value["supersession"] = None
                artifacts = value["artifacts"]
                if not isinstance(artifacts, dict):
                    raise AssertionError("round artifacts must be an object")
                artifacts["bundle_archive"] = []

            for name, mutate, expected in (
                ("wrong round", wrong_round, "round number"),
                (
                    "nonterminal round",
                    nonterminal_round,
                    "must record a superseded, stale, or invalid round",
                ),
            ):
                with self.subTest(name=name):
                    changed = copy.deepcopy(original)
                    mutate(changed)
                    authority_path.chmod(0o600)
                    try:
                        authority_path.write_text(
                            f"{json.dumps(changed, indent=2)}\n",
                            encoding="utf-8",
                        )
                    finally:
                        authority_path.chmod(0o400)
                    submitted = run_cli(
                        recovered.review_worktree,
                        "review-submit",
                        data_home=recovered.data_home,
                        env_overrides=recovered.environment,
                    )
                    self.assertEqual(submitted.returncode, 1)
                    self.assertIn(expected, submitted.stderr)
                    self.assertFalse(
                        (recovered.bundle / "local-state.json").exists()
                    )

            authority_path.chmod(0o600)
            try:
                authority_path.write_text(
                    f"{json.dumps(original, indent=2)}\n",
                    encoding="utf-8",
                )
            finally:
                authority_path.chmod(0o400)


if __name__ == "__main__":
    unittest.main()
