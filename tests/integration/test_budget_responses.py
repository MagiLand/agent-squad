from __future__ import annotations

import copy
import json
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from tests._support import add_src_to_path, run, run_cli
from tests.integration.test_review_applications import (
    _marker_confirmed_review,
    _write_fixed_response,
)
from tests.integration.test_review_submissions import (
    _PreparedRound,
    _prepare_round,
    _write_review,
)

add_src_to_path()
from agent_squad import review_applications, submissions  # noqa: E402


class BudgetResponseTests(unittest.TestCase):
    def cli(
        self,
        prepared: SimpleNamespace,
        *arguments: str,
        cwd: Path | None = None,
    ) -> CompletedProcess[str]:
        result = run_cli(
            cwd or prepared.repository, *arguments,
            data_home=prepared.data_home,
            env_overrides=prepared.environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def mixed_finding_review(
        self,
        root: Path,
        *,
        verdict: str,
        review_limit: int | None = None,
    ) -> tuple[_PreparedRound, dict[str, object]]:
        prepared = _prepare_round(root, review_limit=review_limit)
        review = _write_review(prepared, verdict="changes_requested")
        optional = copy.deepcopy(review["findings"][0])
        optional.update(id="REV-002", blocking=False, severity="low")
        review["findings"].append(optional)
        review["verdict"] = verdict
        (prepared.bundle / "output/review.json").write_text(json.dumps(review))
        self.cli(prepared, "review-submit", cwd=prepared.review_worktree)
        return prepared, review

    def test_reviewer_needs_human_escalation_retains_optional_finding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared, review = self.mixed_finding_review(
                Path(temporary), verdict="needs_human",
            )
            self.cli(
                prepared, "apply-review", "--result-id", review["result_id"],
            )
            control = prepared.repository / ".agent-squad"
            state = json.loads((control / "state.json").read_bytes())
            escalation = json.loads((
                control / "runs" / state["active_run_id"]
                / "escalations/001-escalation.json"
            ).read_bytes())
            self.assertEqual(
                escalation["related_finding_ids"], ["REV-001", "REV-002"],
            )
            self.assertEqual(
                state["review_budget"]["completed_change_reviews"], 0,
            )

    def test_exhaustion_extension_and_replay_are_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared, review = self.mixed_finding_review(
                root, verdict="changes_requested", review_limit=1,
            )
            state_path = prepared.repository / ".agent-squad/state.json"
            state = json.loads(state_path.read_bytes())
            run_dir = state_path.parent / "runs" / state["active_run_id"]
            original_state = state_path.read_bytes()
            write = review_applications.atomic_write

            def fail_state(path, content, *, mode):
                if Path(path).name == "state.json":
                    raise OSError("injected exhaustion commit failure")
                return write(path, content, mode=mode)

            with mock.patch.object(
                review_applications, "atomic_write", side_effect=fail_state,
            ):
                with self.assertRaisesRegex(
                    review_applications.ReviewApplicationError, "injected",
                ):
                    review_applications.apply_review(
                        prepared.repository, result_id=review["result_id"],
                    )
            self.assertEqual(state_path.read_bytes(), original_state)
            self.assertFalse((run_dir / "escalations").exists())
            self.cli(
                prepared, "apply-review", "--result-id", review["result_id"],
            )
            state = json.loads(state_path.read_bytes())
            self.assertEqual(state["phase"], "needs_human")
            self.assertEqual(state["review_budget"], {
                "original_limit": 1, "additional_rounds_granted": 0,
                "effective_limit": 1, "completed_change_reviews": 1,
            })
            escalation = json.loads(
                (run_dir / "escalations/001-escalation.json").read_bytes()
            )
            self.assertEqual(escalation["reason"], "review_budget_exhausted")
            self.assertEqual(
                escalation["source_result_id"], review["result_id"],
            )
            self.assertEqual(
                escalation["source_request_id"], review["request_id"],
            )
            self.assertEqual(escalation["related_finding_ids"], ["REV-001"])
            self.assertEqual(
                state["active_escalation_id"], escalation["escalation_id"],
            )
            originals = {path: path.read_bytes() for path in (
                state_path, run_dir / "events.jsonl",
                run_dir / "rounds/001/round.json",
            )}
            for _ in range(2):
                self.cli(
                    prepared, "apply-review", "--result-id",
                    review["result_id"],
                )
                for path, content in originals.items():
                    self.assertEqual(path.read_bytes(), content)
            resolution = root / "resolution.md"
            resolution.write_text(
                "# Decision\n\nPermit two more change reviews.\n"
            )
            refused = run_cli(
                prepared.repository, "resume", "--resolution", str(resolution),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(refused.returncode, 1)
            self.assertIn("--extend-rounds", refused.stderr)
            self.assertEqual(state_path.read_bytes(), originals[state_path])
            self.cli(
                prepared, "resume", "--resolution", str(resolution),
                "--extend-rounds", "2",
            )
            status = self.cli(prepared, "status")
            self.assertIn("1/3", status.stdout)
            self.assertIn("original 1, additional 2", status.stdout)
            for number in (2, 3):
                response_path = root / "response.json"
                response = _write_fixed_response(
                    response_path, prepared, review,
                )
                response["response_id"] = str(uuid.uuid4())
                response["responses"][0].update(
                    disposition="rejected",
                    evidence=["feature.txt preserves the agreed behavior"],
                )
                response_path.write_text(json.dumps(response))
                report = root / "report.md"
                report.write_text(
                    "# Report\n\nRequest reconsideration with evidence.\n"
                )
                self.cli(
                    prepared, "submit", "--mode", "reconsideration",
                    "--report", str(report), "--response", str(response_path),
                )
                state = json.loads(state_path.read_bytes())
                worktree = Path(state["active_round"]["review_worktree"])
                bundle = worktree / ".agent-squad-review"
                prepared = SimpleNamespace(
                    repository=prepared.repository,
                    data_home=prepared.data_home,
                    environment=prepared.environment, review_worktree=worktree,
                    bundle=bundle,
                    request=json.loads(
                        (bundle / "input/request.json").read_bytes()
                    ),
                )
                review = _write_review(prepared, verdict="changes_requested")
                review["result_id"] = str(uuid.uuid4())
                (bundle / "output/review.json").write_text(json.dumps(review))
                self.cli(prepared, "review-submit", cwd=worktree)
                self.cli(
                    prepared, "apply-review", "--result-id",
                    review["result_id"],
                )
                state = json.loads(state_path.read_bytes())
                self.assertEqual(
                    state["review_budget"]["completed_change_reviews"], number,
                )
                self.assertEqual(
                    state["phase"],
                    "implementing" if number == 2 else "needs_human",
                )

    def prepare_response_continuation(
        self,
        root: Path,
        *,
        needs_human: bool = True,
    ) -> SimpleNamespace:
        prepared, review = _marker_confirmed_review(
            root, verdict="changes_requested",
        )
        self.cli(
            prepared, "apply-review", "--result-id", review["result_id"],
        )
        response_path = root / "response.json"
        earlier = _write_fixed_response(response_path, prepared, review)
        if needs_human:
            earlier["responses"][0].update(
                disposition="needs_human",
                rationale="Developer must choose the compatibility policy.",
            )
        response_path.write_text(json.dumps(earlier))
        self.cli(prepared, "escalate", "--response", str(response_path))
        state_path = prepared.repository / ".agent-squad/state.json"
        state = json.loads(state_path.read_bytes())
        run_dir = state_path.parent / "runs" / state["active_run_id"]
        canonical = run_dir / "rounds/001/response.json"
        earlier_bytes = canonical.read_bytes()
        resolution_path = root / "resolution.md"
        resolution_path.write_text(
            "# Decision\n\nUse strict compatibility for REV-001.\n"
        )
        finding_args = (
            ("--applies-to-finding", "REV-001") if needs_human else ()
        )
        self.cli(
            prepared, "resume", "--resolution", str(resolution_path),
            *finding_args,
        )
        resolution_record = run_dir / "resolutions/001-resolution.json"
        resolution = json.loads(resolution_record.read_bytes())
        response = _write_fixed_response(response_path, prepared, review)
        response.update(
            response_id=str(uuid.uuid4()),
            supersedes_response_id=earlier["response_id"],
            resolution_ids=[resolution["resolution_id"]],
        )
        response["responses"][0]["rationale"] = (
            "Applied strict compatibility as required by Developer "
            "resolution " + resolution["resolution_id"]
        )
        (prepared.repository / "feature.txt").write_text(
            "strict compatibility\n"
        )
        run(["git", "add", "feature.txt"], cwd=prepared.repository)
        run(
            [
                "git", "-c", "commit.gpgSign=false", "commit",
                "--no-verify", "-m", "fix: apply resolution",
            ],
            cwd=prepared.repository,
        )
        report = root / "report.md"
        report.write_text("# Report\n\nApplied strict compatibility.\n")
        original_state = state_path.read_bytes()
        diagnostic = (
            run_dir / "rounds/001/diagnostics/replaced-responses"
            / (earlier["response_id"] + ".json")
        )
        response_path.write_text(json.dumps(response))
        return SimpleNamespace(
            prepared=prepared, response_path=response_path, earlier=earlier,
            state_path=state_path, canonical=canonical,
            earlier_bytes=earlier_bytes, resolution_record=resolution_record,
            response=response, report=report, original_state=original_state,
            diagnostic=diagnostic,
        )

    def test_clean_replacement_creates_immutable_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.prepare_response_continuation(Path(temporary))
            self.assertFalse(fixture.diagnostic.exists())
            self.cli(
                fixture.prepared, "submit", "--mode", "new_revision",
                "--report", str(fixture.report),
                "--response", str(fixture.response_path),
            )
            self.assertTrue(fixture.diagnostic.exists())
            self.assertEqual(
                fixture.diagnostic.read_bytes(), fixture.earlier_bytes,
            )
            self.assertEqual(fixture.diagnostic.stat().st_mode & 0o777, 0o400)
            self.assertEqual(
                fixture.canonical.read_bytes(),
                fixture.response_path.read_bytes(),
            )
            state = json.loads(fixture.state_path.read_bytes())
            bundle = (
                Path(state["active_round"]["review_worktree"])
                / ".agent-squad-review"
            )
            self.assertEqual(
                (bundle / "input/previous-response.json").read_bytes(),
                fixture.response_path.read_bytes(),
            )
            self.assertEqual(
                bundle.joinpath(
                    "input/resolutions/001-resolution.json"
                ).read_bytes(),
                fixture.resolution_record.read_bytes(),
            )

    def test_submit_reuses_unchanged_escalation_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.prepare_response_continuation(
                Path(temporary), needs_human=False,
            )
            fixture.response_path.write_bytes(fixture.earlier_bytes)
            self.cli(
                fixture.prepared, "submit", "--mode", "new_revision",
                "--report", str(fixture.report),
                "--response", str(fixture.response_path),
            )
            self.assertEqual(
                fixture.canonical.read_bytes(), fixture.earlier_bytes,
            )
            self.assertFalse(fixture.diagnostic.exists())
            state = json.loads(fixture.state_path.read_bytes())
            bundle = (
                Path(state["active_round"]["review_worktree"])
                / ".agent-squad-review"
            )
            self.assertEqual(
                (bundle / "input/previous-response.json").read_bytes(),
                fixture.earlier_bytes,
            )

    def test_response_diagnostic_symlink_is_refused_before_preparation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.prepare_response_continuation(Path(temporary))
            outside = Path(temporary) / "outside"
            outside.mkdir()
            fixture.diagnostic.parent.parent.symlink_to(outside)
            with mock.patch.object(
                submissions, "_create_detached_review_worktree",
                side_effect=AssertionError("review preparation began"),
            ):
                with self.assertRaisesRegex(
                    submissions.SubmissionError,
                    "response diagnostic directory must not be "
                    "a symbolic link",
                ):
                    submissions.submit_candidate(
                        fixture.prepared.repository, mode="new_revision",
                        report_path=fixture.report,
                        response_path=fixture.response_path,
                    )
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(
                fixture.state_path.read_bytes(), fixture.original_state,
            )
            self.assertEqual(
                fixture.canonical.read_bytes(), fixture.earlier_bytes,
            )

    def test_resolved_response_replacement_and_corrected_precommit_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.prepare_response_continuation(Path(temporary))
            prepared = fixture.prepared
            response_path = fixture.response_path
            earlier = fixture.earlier
            state_path = fixture.state_path
            canonical = fixture.canonical
            earlier_bytes = fixture.earlier_bytes
            resolution_record = fixture.resolution_record
            response = fixture.response
            report = fixture.report
            original_state = fixture.original_state
            diagnostic = fixture.diagnostic
            cases = (
                (
                    {"supersedes_response_id": None},
                    "replacement must identify the escalation-time response",
                ),
                (
                    {"response_id": earlier["response_id"]},
                    "supersedes_response_id cannot equal response_id",
                ),
                (
                    {"resolution_ids": []},
                    "replacement must identify recorded Developer "
                    "resolution_ids",
                ),
                (
                    {"resolution_ids": [str(uuid.uuid4())]},
                    "replacement must identify recorded Developer "
                    "resolution_ids",
                ),
                (
                    {"responses": earlier["responses"]},
                    "implementation response requires Developer authority",
                ),
            )
            for patch, message in cases:
                invalid = copy.deepcopy(response)
                invalid.update(patch)
                response_path.write_text(json.dumps(invalid))
                refused = run_cli(
                    prepared.repository, "submit", "--mode", "new_revision",
                    "--report", str(report), "--response", str(response_path),
                    data_home=prepared.data_home,
                    env_overrides=prepared.environment,
                )
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn(message, refused.stderr)
                self.assertEqual(state_path.read_bytes(), original_state)
                self.assertEqual(canonical.read_bytes(), earlier_bytes)
                self.assertFalse(diagnostic.exists())
            response_path.write_text(json.dumps(response))
            write = submissions.atomic_write

            def fail_state(path, content, *, mode):
                if Path(path).name == "state.json":
                    raise OSError("injected replacement commit failure")
                return write(path, content, mode=mode)

            with mock.patch.object(
                submissions, "atomic_write", side_effect=fail_state,
            ):
                with self.assertRaisesRegex(
                    submissions.SubmissionError, "injected",
                ):
                    submissions.submit_candidate(
                        prepared.repository, mode="new_revision",
                        report_path=report, response_path=response_path,
                    )
            self.assertEqual(state_path.read_bytes(), original_state)
            self.assertEqual(canonical.read_bytes(), earlier_bytes)
            self.assertFalse(diagnostic.exists())

            def fail_after_response_write(path, content, *, mode):
                result = write(path, content, mode=mode)
                if (
                    Path(path).resolve() == canonical.resolve()
                    and content == response_path.read_bytes()
                ):
                    raise OSError("injected post-write response failure")
                return result

            with mock.patch.object(
                submissions, "atomic_write",
                side_effect=fail_after_response_write,
            ):
                with self.assertRaisesRegex(
                    submissions.SubmissionError, "post-write response failure",
                ):
                    submissions.submit_candidate(
                        prepared.repository, mode="new_revision",
                        report_path=report, response_path=response_path,
                    )
            self.assertEqual(state_path.read_bytes(), original_state)
            self.assertEqual(canonical.read_bytes(), earlier_bytes)
            self.assertFalse(diagnostic.exists())
            # Simulate a crash after archival/replacement but before state.
            diagnostic.parent.mkdir(parents=True)
            diagnostic.write_bytes(earlier_bytes)
            diagnostic.chmod(0o400)
            canonical.chmod(0o600)
            canonical.write_bytes(response_path.read_bytes())
            canonical.chmod(0o400)
            response["response_id"] = str(uuid.uuid4())
            response_path.write_text(json.dumps(response))
            self.cli(
                prepared, "submit", "--mode", "new_revision", "--report",
                str(report), "--response", str(response_path),
            )
            self.assertEqual(diagnostic.read_bytes(), earlier_bytes)
            self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o400)
            self.assertEqual(
                canonical.read_bytes(), response_path.read_bytes(),
            )
            state = json.loads(state_path.read_bytes())
            worktree = Path(state["active_round"]["review_worktree"])
            bundle = worktree / ".agent-squad-review"
            self.assertEqual(
                (bundle / "input/previous-response.json").read_bytes(),
                canonical.read_bytes(),
            )
            self.assertEqual(
                bundle.joinpath(
                    "input/resolutions/001-resolution.json"
                ).read_bytes(),
                resolution_record.read_bytes(),
            )
            self.cli(prepared, "supersede", "--reason", "Retry fresh review")
            response["response_id"] = str(uuid.uuid4())
            response_path.write_text(json.dumps(response))
            refused = run_cli(
                prepared.repository, "submit", "--mode", "new_revision",
                "--report", str(report), "--response", str(response_path),
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(refused.returncode, 1)
            self.assertIn("already references", refused.stderr)
            response_path.write_bytes(canonical.read_bytes())
            self.cli(
                prepared, "submit", "--mode", "new_revision", "--report",
                str(report), "--response", str(response_path),
            )
            state = json.loads(state_path.read_bytes())
            worktree = Path(state["active_round"]["review_worktree"])
            bundle = worktree / ".agent-squad-review"
            self.assertEqual(diagnostic.read_bytes(), earlier_bytes)
            second = SimpleNamespace(
                bundle=bundle,
                request=json.loads(
                    (bundle / "input/request.json").read_bytes()
                ),
            )
            approval = _write_review(second)
            approval["result_id"] = str(uuid.uuid4())
            (bundle / "output/review.json").write_text(json.dumps(approval))
            self.cli(prepared, "review-submit", cwd=worktree)
            self.cli(
                prepared, "apply-review", "--result-id", approval["result_id"],
            )
            self.assertEqual(
                json.loads(state_path.read_bytes())["phase"], "approved",
            )
            self.cli(prepared, "complete")
            self.cli(prepared, "complete")
