from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path, run_cli
from tests.integration.test_review_submissions import (
    _PreparedRound,
    _prepare_round,
    _write_review,
)


add_src_to_path()

from agent_squad import review_applications, review_submissions  # noqa: E402
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
