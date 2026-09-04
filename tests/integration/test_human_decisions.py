from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests._support import (
    add_src_to_path,
    install_fake_herdr,
    run,
    run_cli,
    seed_git_repository,
)
from tests.integration.test_review_applications import (
    _marker_confirmed_review,
    _write_fixed_response,
)
from tests.integration.test_review_submissions import (
    _prepare_round,
    _write_review,
)


add_src_to_path()

from agent_squad import review_applications  # noqa: E402


class HumanDecisionCommandTests(unittest.TestCase):
    def test_direct_escalation_and_developer_resolution_resume_the_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            head_oid = seed_git_repository(repository)
            data_home = root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = root / "task.md"
            task.write_text("# Task\n\nChoose a compatibility policy.\n")
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

            note = root / "decision-needed.md"
            note_bytes = b"# Decision needed\n\nChoose strict compatibility.\n"
            note.write_bytes(note_bytes)
            escalated = run_cli(
                repository,
                "escalate",
                "--note",
                str(note),
                data_home=data_home,
            )

            self.assertEqual(escalated.returncode, 0, escalated.stderr)
            self.assertIn("Escalated Agent Squad run", escalated.stdout)
            control_root = repository / ".agent-squad"
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_id = str(state["active_run_id"])
            run_directory = control_root / "runs" / run_id
            escalation_id = str(state["active_escalation_id"])
            escalation_path = (
                run_directory / "escalations/001-escalation.json"
            )
            escalation_note = (
                run_directory / "escalations/001-escalation.md"
            )
            escalation = json.loads(
                escalation_path.read_text(encoding="utf-8")
            )
            run_record = json.loads(
                (run_directory / "run.json").read_text(encoding="utf-8")
            )

            self.assertEqual(state["phase"], "needs_human")
            self.assertEqual(run_record["phase"], "needs_human")
            self.assertEqual(escalation["escalation_id"], escalation_id)
            self.assertEqual(escalation["run_id"], run_id)
            self.assertEqual(escalation["round"], 0)
            self.assertEqual(escalation["head_oid"], head_oid)
            self.assertEqual(escalation["previous_phase"], "implementing")
            self.assertEqual(escalation["reason"], "implementer_requested")
            self.assertIsNone(escalation["source_request_id"])
            self.assertIsNone(escalation["source_result_id"])
            self.assertIsNone(escalation["previous_approved_head_oid"])
            self.assertIsNone(escalation["response_id"])
            self.assertEqual(escalation["related_finding_ids"], [])
            self.assertEqual(
                escalation["note_sha256"],
                hashlib.sha256(note_bytes).hexdigest(),
            )
            self.assertEqual(escalation_note.read_bytes(), note_bytes)

            resolution = root / "resolution.md"
            resolution_bytes = (
                b"# Resolution\n\nUse strict compatibility for this run.\n"
            )
            resolution.write_bytes(resolution_bytes)
            resumed = run_cli(
                repository,
                "resume",
                "--resolution",
                str(resolution),
                "--extend-rounds",
                "1",
                data_home=data_home,
            )

            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertIn("Resumed Agent Squad run", resumed.stdout)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            resolution_path = (
                run_directory / "resolutions/001-resolution.json"
            )
            resolution_copy = (
                run_directory / "resolutions/001-resolution.md"
            )
            resolution_record = json.loads(
                resolution_path.read_text(encoding="utf-8")
            )
            run_record = json.loads(
                (run_directory / "run.json").read_text(encoding="utf-8")
            )

            self.assertEqual(state["phase"], "implementing")
            self.assertIsNone(state["active_escalation_id"])
            self.assertEqual(run_record["phase"], "implementing")
            self.assertEqual(
                state["review_budget"],
                {
                    "original_limit": 4,
                    "additional_rounds_granted": 1,
                    "effective_limit": 5,
                    "completed_change_reviews": 0,
                },
            )
            self.assertEqual(
                resolution_record["resolves_escalation_id"],
                escalation_id,
            )
            self.assertEqual(resolution_record["run_id"], run_id)
            self.assertEqual(
                resolution_record["additional_rounds_granted"], 1
            )
            self.assertEqual(
                resolution_record["resolution_sha256"],
                hashlib.sha256(resolution_bytes).hexdigest(),
            )
            self.assertEqual(resolution_copy.read_bytes(), resolution_bytes)

            events = [
                json.loads(line)
                for line in (
                    run_directory / "events.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["event"] for event in events],
                ["run_started", "run_escalated", "run_resumed"],
            )
            self.assertEqual(events[1]["escalation_id"], escalation_id)
            self.assertEqual(
                events[2]["resolution_id"],
                resolution_record["resolution_id"],
            )

            status = run_cli(repository, "status", data_home=data_home)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Developer resolutions: 1", status.stdout)
            self.assertIn(
                f"Resolution 1: {resolution_record['resolution_id']}",
                status.stdout,
            )
            resumed_again = run_cli(
                repository,
                "resume",
                "--resolution",
                str(resolution),
                data_home=data_home,
            )
            self.assertEqual(resumed_again.returncode, 1)
            self.assertIn(
                "resume requires a needs_human run",
                resumed_again.stderr,
            )
            self.assertFalse(
                (run_directory / "resolutions/002-resolution.json").exists()
            )

            original_state = state_path.read_bytes()
            inconsistent_state = json.loads(original_state)
            inconsistent_state["review_budget"].update(
                additional_rounds_granted=2,
                effective_limit=6,
            )
            state_path.write_text(
                f"{json.dumps(inconsistent_state, indent=2)}\n",
                encoding="utf-8",
            )
            try:
                inconsistent = run_cli(
                    repository,
                    "status",
                    data_home=data_home,
                )
                self.assertEqual(inconsistent.returncode, 1)
                self.assertIn(
                    "review-budget extension does not match Developer "
                    "resolution history",
                    inconsistent.stderr,
                )
            finally:
                state_path.write_bytes(original_state)

    def test_reviewing_escalation_supersedes_the_active_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared = _prepare_round(root)
            escalated = run_cli(
                prepared.repository,
                "escalate",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(escalated.returncode, 0, escalated.stderr)
            control_root = prepared.repository / ".agent-squad"
            state = json.loads(
                (control_root / "state.json").read_text(encoding="utf-8")
            )
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            round_record = json.loads(
                (
                    run_directory / "rounds/001/round.json"
                ).read_text(encoding="utf-8")
            )
            escalation_id = str(state["active_escalation_id"])
            escalation = json.loads(
                (
                    run_directory / "escalations/001-escalation.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(state["phase"], "needs_human")
            self.assertEqual(state["active_round"]["status"], "superseded")
            self.assertEqual(round_record["status"], "superseded")
            self.assertEqual(
                round_record["supersession"]["cause"],
                f"escalation:{escalation_id}",
            )
            self.assertEqual(escalation["previous_phase"], "reviewing")
            self.assertEqual(
                escalation["source_request_id"],
                prepared.request["request_id"],
            )
            self.assertIsNone(escalation["source_result_id"])
            self.assertFalse(prepared.review_worktree.exists())
            status = run_cli(
                prepared.repository,
                "status",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)

    def test_failed_resume_keeps_the_escalation_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            seed_git_repository(repository)
            data_home = root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = root / "task.md"
            task.write_text("# Task\n\nChoose a policy.\n", encoding="utf-8")
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
            escalated = run_cli(
                repository,
                "escalate",
                data_home=data_home,
            )
            self.assertEqual(escalated.returncode, 0, escalated.stderr)
            control_root = repository / ".agent-squad"
            state_path = control_root / "state.json"
            state_before = state_path.read_bytes()
            state = json.loads(state_before)
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            run_path = run_directory / "run.json"
            event_path = run_directory / "events.jsonl"
            originals = {
                run_path: run_path.read_bytes(),
                event_path: event_path.read_bytes(),
            }
            resolution_path = root / "resolution.md"
            resolution_path.write_text(
                "# Resolution\n\nUse strict compatibility.\n",
                encoding="utf-8",
            )
            write = review_applications.atomic_write

            def fail_state_write(path, content, *, mode):
                if Path(path).name == "state.json":
                    raise OSError("simulated state commit failure")
                return write(path, content, mode=mode)

            with (
                mock.patch.object(
                    review_applications,
                    "atomic_write",
                    side_effect=fail_state_write,
                ),
                self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "simulated state commit failure",
                ),
            ):
                review_applications.resume_run(
                    repository,
                    resolution_path=resolution_path,
                )

            self.assertEqual(state_path.read_bytes(), state_before)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)
            self.assertFalse((run_directory / "resolutions").exists())
            status = run_cli(repository, "status", data_home=data_home)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Phase: needs_human", status.stdout)

    def test_approved_escalation_clears_but_records_approval_authority(
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
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            approved_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            approved_head = str(approved_state["approved_head_oid"])

            escalated = run_cli(
                prepared.repository,
                "escalate",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(escalated.returncode, 0, escalated.stderr)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            escalation = json.loads(
                (
                    run_directory / "escalations/001-escalation.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(state["phase"], "needs_human")
            self.assertIsNone(state["approved_head_oid"])
            self.assertEqual(escalation["previous_phase"], "approved")
            self.assertEqual(
                escalation["previous_approved_head_oid"],
                approved_head,
            )
            self.assertTrue(
                (run_directory / "rounds/001/approval.json").is_file()
            )

    def test_failed_escalation_does_not_freeze_staged_response(self) -> None:
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
            response_path = root / "response.json"
            response = _write_fixed_response(
                response_path,
                prepared,
                review,
            )
            response["responses"][0].update(
                disposition="needs_human",
                changed_files=[],
                evidence=[],
                verification="",
                rationale="Choose the compatibility policy.",
            )
            response_path.write_text(
                f"{json.dumps(response, indent=2)}\n",
                encoding="utf-8",
            )
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state_before = state_path.read_bytes()
            state = json.loads(state_before)
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            run_path = run_directory / "run.json"
            round_path = run_directory / "rounds/001/round.json"
            event_path = run_directory / "events.jsonl"
            response_copy = run_directory / "rounds/001/response.json"
            originals = {
                run_path: run_path.read_bytes(),
                round_path: round_path.read_bytes(),
                event_path: event_path.read_bytes(),
            }
            write = review_applications.atomic_write

            def fail_state_write(path, content, *, mode):
                if Path(path).name == "state.json":
                    raise OSError("simulated state commit failure")
                return write(path, content, mode=mode)

            with (
                mock.patch.object(
                    review_applications,
                    "atomic_write",
                    side_effect=fail_state_write,
                ),
                self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "simulated state commit failure",
                ),
            ):
                review_applications.escalate_run(
                    prepared.repository,
                    response_path=response_path,
                )

            self.assertEqual(state_path.read_bytes(), state_before)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)
            self.assertFalse(response_copy.exists())
            self.assertFalse((run_directory / "escalations").exists())

            response["response_id"] = (
                "55555555-5555-4555-8555-555555555555"
            )
            response["responses"][0]["rationale"] = (
                "Choose strict or permissive compatibility."
            )
            response_path.write_text(
                f"{json.dumps(response, indent=2)}\n",
                encoding="utf-8",
            )
            retried = run_cli(
                prepared.repository,
                "escalate",
                "--response",
                str(response_path),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )

            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertEqual(
                response_copy.read_bytes(),
                response_path.read_bytes(),
            )
            escalation = json.loads(
                (
                    run_directory / "escalations/001-escalation.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                escalation["response_id"],
                response["response_id"],
            )
            self.assertEqual(
                escalation["related_finding_ids"],
                ["REV-001"],
            )
            replayed = run_cli(
                prepared.repository,
                "escalate",
                "--response",
                str(response_path),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(replayed.returncode, 0, replayed.stderr)
            self.assertEqual(
                len(list((run_directory / "escalations").glob("*.json"))),
                1,
            )
            events = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                sum(event["event"] == "run_escalated" for event in events),
                1,
            )

            canonical_response = response_copy.read_bytes()
            response["responses"][0]["rationale"] = (
                "Choose a different compatibility policy."
            )
            response_path.write_text(
                f"{json.dumps(response, indent=2)}\n",
                encoding="utf-8",
            )
            conflicting_retry = run_cli(
                prepared.repository,
                "escalate",
                "--response",
                str(response_path),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(conflicting_retry.returncode, 1)
            self.assertIn(
                "already references a different response",
                conflicting_retry.stderr,
            )
            self.assertEqual(response_copy.read_bytes(), canonical_response)
            resolution_path = root / "resolution.md"
            resolution_path.write_text(
                "# Resolution\n\nUse strict compatibility.\n",
                encoding="utf-8",
            )
            invalid_resolution = run_cli(
                prepared.repository,
                "resume",
                "--resolution",
                str(resolution_path),
                "--applies-to-finding",
                "REV-UNKNOWN",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(invalid_resolution.returncode, 1)
            self.assertIn(
                "finding IDs outside the active escalation: REV-UNKNOWN",
                invalid_resolution.stderr,
            )

    def test_subsequent_review_bundle_contains_every_developer_resolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
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
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = root / "task.md"
            task.write_text("# Task\n\nImplement the decided policy.\n")
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
            self.assertEqual(started.returncode, 0, started.stderr)
            escalated = run_cli(
                repository,
                "escalate",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(escalated.returncode, 0, escalated.stderr)
            resolution = root / "resolution.md"
            resolution.write_text(
                "# Resolution\n\nUse strict compatibility.\n",
                encoding="utf-8",
            )
            resumed = run_cli(
                repository,
                "resume",
                "--resolution",
                str(resolution),
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            escalated_again = run_cli(
                repository,
                "escalate",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(
                escalated_again.returncode,
                0,
                escalated_again.stderr,
            )
            later_resolution = root / "later-resolution.md"
            later_resolution.write_text(
                "# Resolution\n\nUse strict compatibility with warnings.\n",
                encoding="utf-8",
            )
            resumed_again = run_cli(
                repository,
                "resume",
                "--resolution",
                str(later_resolution),
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(
                resumed_again.returncode,
                0,
                resumed_again.stderr,
            )

            (repository / "feature.txt").write_text(
                "strict compatibility\n",
                encoding="utf-8",
            )
            run(["git", "add", "feature.txt"], cwd=repository)
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    "feat: implement policy",
                ],
                cwd=repository,
            )
            report = root / "implementation-report.md"
            report.write_text(
                "# Implementation Report\n\nImplemented the resolution.\n",
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

            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            control_root = repository / ".agent-squad"
            state = json.loads(
                (control_root / "state.json").read_text(encoding="utf-8")
            )
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            review_worktree = Path(
                str(state["active_round"]["review_worktree"])
            )
            bundle = review_worktree / ".agent-squad-review"
            request = json.loads(
                (bundle / "input/request.json").read_text(encoding="utf-8")
            )
            canonical_json = (
                run_directory / "resolutions/001-resolution.json"
            )
            canonical_markdown = (
                run_directory / "resolutions/001-resolution.md"
            )
            later_canonical_json = (
                run_directory / "resolutions/002-resolution.json"
            )
            later_canonical_markdown = (
                run_directory / "resolutions/002-resolution.md"
            )

            self.assertEqual(
                request["resolution_paths"],
                [
                    "input/resolutions/001-resolution.json",
                    "input/resolutions/002-resolution.json",
                ],
            )
            self.assertEqual(
                (
                    bundle / "input/resolutions/001-resolution.json"
                ).read_bytes(),
                canonical_json.read_bytes(),
            )
            self.assertEqual(
                (
                    bundle / "input/resolutions/001-resolution.md"
                ).read_bytes(),
                canonical_markdown.read_bytes(),
            )
            self.assertEqual(
                (
                    bundle / "input/resolutions/002-resolution.json"
                ).read_bytes(),
                later_canonical_json.read_bytes(),
            )
            self.assertEqual(
                (bundle / "input/resolutions/002-resolution.md").read_bytes(),
                later_canonical_markdown.read_bytes(),
            )
            first_record = json.loads(
                canonical_json.read_text(encoding="utf-8")
            )
            later_record = json.loads(
                later_canonical_json.read_text(encoding="utf-8")
            )
            self.assertLess(
                first_record["created_at"],
                later_record["created_at"],
            )
            status = run_cli(
                repository,
                "status",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Developer resolutions: 2", status.stdout)

    def test_reviewer_needs_human_result_can_be_resolved_and_reviewed_again(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="needs_human",
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
            self.assertIn("Verdict: needs_human", applied.stdout)
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_id = str(state["active_run_id"])
            run_directory = control_root / "runs" / run_id
            escalation_id = str(state["active_escalation_id"])
            escalation = json.loads(
                (
                    run_directory / "escalations/001-escalation.json"
                ).read_text(encoding="utf-8")
            )
            first_round = json.loads(
                (
                    run_directory / "rounds/001/round.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(state["phase"], "needs_human")
            self.assertEqual(
                state["review_budget"]["completed_change_reviews"], 0
            )
            self.assertEqual(first_round["status"], "applied")
            self.assertEqual(first_round["verdict"], "needs_human")
            self.assertEqual(escalation["escalation_id"], escalation_id)
            self.assertEqual(escalation["reason"], "reviewer_needs_human")
            self.assertEqual(
                escalation["source_request_id"], review["request_id"]
            )
            self.assertEqual(
                escalation["source_result_id"], review["result_id"]
            )
            self.assertEqual(escalation["previous_phase"], "reviewing")
            self.assertIn(
                str(review["summary"]),
                (
                    run_directory / "escalations/001-escalation.md"
                ).read_text(encoding="utf-8"),
            )

            resolution = root / "reviewer-resolution.md"
            resolution.write_text(
                "# Resolution\n\nPreserve strict compatibility.\n",
                encoding="utf-8",
            )
            resumed = run_cli(
                prepared.repository,
                "resume",
                "--resolution",
                str(resolution),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)

            (prepared.repository / "feature.txt").write_text(
                "candidate with strict compatibility\n",
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
                    "fix: apply developer resolution",
                ],
                cwd=prepared.repository,
            )
            report = root / "resolved-report.md"
            report.write_text(
                "# Implementation Report\n\nApplied the Developer decision.\n",
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

            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["current_round"], 2)
            second_worktree = Path(
                str(state["active_round"]["review_worktree"])
            )
            request = json.loads(
                (
                    second_worktree
                    / ".agent-squad-review/input/request.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                request["previous_review_path"],
                "input/previous-review.json",
            )
            self.assertIsNone(request["previous_response_path"])
            self.assertEqual(
                request["resolution_paths"],
                ["input/resolutions/001-resolution.json"],
            )
            fresh_round = SimpleNamespace(
                repository=prepared.repository,
                data_home=prepared.data_home,
                environment=prepared.environment,
                review_worktree=second_worktree,
                bundle=(second_worktree / ".agent-squad-review"),
                request=request,
            )
            fresh_review = _write_review(fresh_round)
            reviewed = run_cli(
                second_worktree,
                "review-submit",
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            reapplied = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(fresh_review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(reapplied.returncode, 0, reapplied.stderr)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["phase"],
                "approved",
            )

    def test_needs_human_application_rolls_back_as_one_transition(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prepared, review = _marker_confirmed_review(
                root,
                verdict="needs_human",
            )
            control_root = prepared.repository / ".agent-squad"
            state_path = control_root / "state.json"
            state_before = state_path.read_bytes()
            state = json.loads(state_before)
            run_directory = (
                control_root / "runs" / str(state["active_run_id"])
            )
            run_path = run_directory / "run.json"
            round_path = run_directory / "rounds/001/round.json"
            event_path = run_directory / "events.jsonl"
            originals = {
                run_path: run_path.read_bytes(),
                round_path: round_path.read_bytes(),
                event_path: event_path.read_bytes(),
            }
            write = review_applications.atomic_write

            def fail_state_write(path, content, *, mode):
                if Path(path).name == "state.json":
                    raise OSError("simulated state commit failure")
                return write(path, content, mode=mode)

            with (
                mock.patch.object(
                    review_applications,
                    "atomic_write",
                    side_effect=fail_state_write,
                ),
                self.assertRaisesRegex(
                    review_applications.ReviewApplicationError,
                    "simulated state commit failure",
                ),
            ):
                review_applications.apply_review(
                    prepared.repository,
                    result_id=str(review["result_id"]),
                )

            self.assertEqual(state_path.read_bytes(), state_before)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)
            self.assertFalse((run_directory / "escalations").exists())

            retried = run_cli(
                prepared.repository,
                "apply-review",
                "--result-id",
                str(review["result_id"]),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["phase"],
                "needs_human",
            )


if __name__ == "__main__":
    unittest.main()
