from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from tests._support import (
    add_src_to_path,
    initialize_git_repository,
    install_fake_herdr,
    run,
    run_cli,
    seed_commit,
    seed_git_repository,
)


add_src_to_path()

from agent_squad import handoffs, runs, submissions  # noqa: E402
from agent_squad.artifacts import (  # noqa: E402
    ActiveRoundRecord,
    ArtifactValidationError,
    HandoffRecord,
)
from agent_squad.herdr import HerdrInstallation  # noqa: E402
from agent_squad.initialization import AgentKind  # noqa: E402


def _artifacts(repository: Path) -> tuple[dict[str, object], Path]:
    control_root = repository / ".agent-squad"
    state: dict[str, object] = json.loads(
        (control_root / "state.json").read_text(encoding="utf-8")
    )
    run_id = state["active_run_id"]
    if not isinstance(run_id, str):
        raise AssertionError("active run ID must be a string")
    return state, control_root / "runs" / run_id


def _start_run(
    root: Path,
    *,
    with_context: bool = False,
    object_format: str | None = None,
) -> tuple[Path, Path, Path, dict[str, str], str]:
    repository = root / "repository"
    if object_format is None:
        base_oid = seed_git_repository(repository)
    else:
        initialize_git_repository(
            repository,
            object_format=object_format,
        )
        base_oid = seed_commit(repository)
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
        "# Task\n\nReview the committed candidate.\n",
        encoding="utf-8",
    )
    context = root / "context.md"
    context.write_text("explicit context\n", encoding="utf-8")
    arguments = ["start", "--task", str(task), "--base", "main"]
    if with_context:
        arguments.extend(("--context", str(context)))
    started = run_cli(
        repository,
        *arguments,
        data_home=data_home,
        env_overrides=environment,
    )
    if started.returncode != 0:
        raise AssertionError(started.stderr)
    report = root / "implementation-report.md"
    report.write_text(
        "# Implementation Report\n\nImplemented and tested the candidate.\n",
        encoding="utf-8",
    )
    return repository, data_home, report, environment, base_oid


def _commit_candidate(repository: Path, name: str = "feature.txt") -> str:
    (repository / name).write_text("candidate\n", encoding="utf-8")
    run(["git", "add", name], cwd=repository)
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
    return run(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()


class SubmitCommandTests(unittest.TestCase):
    def test_submit_persists_exact_round_bundle_then_notifies_reviewer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, base_oid = _start_run(
                root,
                with_context=True,
            )
            head_oid = _commit_candidate(repository)
            report_bytes = report.read_bytes()

            original_umask = os.umask(0o022)
            try:
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
            finally:
                os.umask(original_umask)

            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            state, run_directory = _artifacts(repository)
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["current_round"], 1)
            self.assertEqual(state["current_head_oid"], head_oid)
            self.assertEqual(state["handoff"]["status"], "sent")
            self.assertEqual(state["handoff"]["herdr_protocol"], 20)
            inspected = runs.inspect_status(repository)
            active_status = inspected.active_run
            self.assertIsNotNone(active_status)
            if active_status is None:
                self.fail("submitted run must remain active")
            self.assertIsInstance(
                active_status.active_round,
                ActiveRoundRecord,
            )
            self.assertIsInstance(active_status.handoff, HandoffRecord)
            active_round = state["active_round"]
            request_id = active_round["request_id"]
            review_worktree = Path(active_round["review_worktree"])
            reviewer_name = active_round["reviewer_name"]
            self.assertEqual(
                reviewer_name,
                f"asq-{state['active_run_id'].replace('-', '')[:12]}-"
                "r001-reviewer",
            )

            round_directory = run_directory / "rounds/001"
            round_record = json.loads(
                (round_directory / "round.json").read_text(encoding="utf-8")
            )
            request_bytes = (round_directory / "request.json").read_bytes()
            request = json.loads(request_bytes)
            self.assertEqual(round_record["status"], "reviewing")
            self.assertEqual(round_record["request_id"], request_id)
            self.assertEqual(round_record["base_oid"], base_oid)
            self.assertEqual(round_record["head_oid"], head_oid)
            self.assertEqual(round_record["reviewer"]["name"], reviewer_name)
            self.assertEqual(
                round_record["artifacts"]["request"]["sha256"],
                hashlib.sha256(request_bytes).hexdigest(),
            )
            self.assertEqual(
                (round_directory / "implementation-report.md").read_bytes(),
                report_bytes,
            )
            self.assertEqual(request["mode"], "new_revision")
            self.assertEqual(request["base_oid"], base_oid)
            self.assertEqual(request["head_oid"], head_oid)
            self.assertEqual(request["reviewer_name"], reviewer_name)
            self.assertEqual(request["task"]["path"], "input/task.md")
            self.assertEqual(
                request["implementation_report"]["path"],
                "input/implementation-report.md",
            )
            self.assertEqual(
                request["context_files"][0]["path"],
                "input/context/001/context.md",
            )
            self.assertFalse(Path(request["task"]["path"]).is_absolute())

            bundle = review_worktree / ".agent-squad-review"
            for directory in (
                bundle,
                bundle / "input",
                bundle / "input/context",
                bundle / "input/context/001",
                bundle / "output",
            ):
                with self.subTest(directory=directory):
                    self.assertEqual(
                        stat.S_IMODE(directory.stat().st_mode),
                        0o700,
                    )
            self.assertEqual(
                (bundle / "input/request.json").read_bytes(),
                request_bytes,
            )
            self.assertEqual(
                (bundle / "input/implementation-report.md").read_bytes(),
                report_bytes,
            )
            self.assertEqual(
                run(["git", "rev-parse", "HEAD"], cwd=review_worktree)
                .stdout.strip(),
                head_oid,
            )
            self.assertNotEqual(
                run(
                    ["git", "symbolic-ref", "--quiet", "HEAD"],
                    cwd=review_worktree,
                    check=False,
                ).returncode,
                0,
            )
            self.assertEqual(
                run(
                    [
                        "git",
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    ],
                    cwd=review_worktree,
                ).stdout,
                "",
            )

            events = [
                json.loads(line)
                for line in (
                    Path(environment["FAKE_HERDR_STATE_DIR"])
                    / "invocations.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            prompts = [
                event
                for event in events
                if event["arguments"][:2] == ["agent", "prompt"]
                and event["arguments"] != ["agent", "prompt", "--help"]
            ]
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["phase_at_prompt"], "reviewing")
            self.assertEqual(prompts[0]["request_id_at_prompt"], request_id)
            prompt = prompts[0]["arguments"][3]
            self.assertIn("AGENT_SQUAD/0.4.4 REVIEW_REQUEST", prompt)
            self.assertIn(f"head_oid: {head_oid}", prompt)
            self.assertIn(str(bundle / "input/request.json"), prompt)

            report.unlink()
            (root / "task.md").unlink()
            (root / "context.md").unlink()
            self.assertEqual(
                (bundle / "input/implementation-report.md").read_bytes(),
                report_bytes,
            )
            status = run_cli(
                repository,
                "status",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Phase: reviewing", status.stdout)
            self.assertIn(f"Request ID: {request_id}", status.stdout)
            self.assertIn(f"Reviewer session: {reviewer_name}", status.stdout)
            self.assertIn("Request handoff: sent", status.stdout)

    def test_submit_does_not_require_optional_history_capabilities(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            _commit_candidate(repository)
            environment["FAKE_HERDR_SCHEMA_MISSING"] = "agent.read"

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
            state, _ = _artifacts(repository)
            self.assertEqual(state["handoff"]["status"], "sent")

    def test_precommit_git_and_mode_failures_create_no_round(self) -> None:
        cases = (
            "no change",
            "tracked change",
            "untracked file",
            "ignored untracked file",
            "branch changed",
            "base not ancestor",
            "reconsideration",
        )
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    (
                        repository,
                        data_home,
                        report,
                        environment,
                        _,
                    ) = _start_run(root)
                    if case not in {"no change", "reconsideration"}:
                        _commit_candidate(repository)
                    if case == "tracked change":
                        (repository / "README.md").write_text(
                            "uncommitted\n",
                            encoding="utf-8",
                        )
                    elif case == "untracked file":
                        (repository / "notes.tmp").write_text(
                            "preserve me\n",
                            encoding="utf-8",
                        )
                    elif case == "ignored untracked file":
                        (repository / ".gitignore").write_text(
                            "secret.local\n",
                            encoding="utf-8",
                        )
                        run(["git", "add", ".gitignore"], cwd=repository)
                        run(
                            [
                                "git",
                                "-c",
                                "commit.gpgSign=false",
                                "commit",
                                "--no-verify",
                                "-m",
                                "test: ignore local secret",
                            ],
                            cwd=repository,
                        )
                        (repository / "secret.local").write_text(
                            "preserve me too\n",
                            encoding="utf-8",
                        )
                    elif case == "branch changed":
                        run(
                            ["git", "checkout", "-b", "other"],
                            cwd=repository,
                        )
                    elif case == "base not ancestor":
                        tree = run(
                            ["git", "rev-parse", "HEAD^{tree}"],
                            cwd=repository,
                        ).stdout.strip()
                        unrelated = run(
                            ["git", "commit-tree", tree, "-m", "unrelated"],
                            cwd=repository,
                        ).stdout.strip()
                        run(
                            ["git", "reset", "--hard", unrelated],
                            cwd=repository,
                        )
                    mode = (
                        "reconsideration"
                        if case == "reconsideration"
                        else "new_revision"
                    )

                    submitted = run_cli(
                        repository,
                        "submit",
                        "--report",
                        str(report),
                        "--mode",
                        mode,
                        data_home=data_home,
                        env_overrides=environment,
                    )

                    self.assertNotEqual(submitted.returncode, 0)
                    state, run_directory = _artifacts(repository)
                    self.assertEqual(state["phase"], "implementing")
                    self.assertEqual(state["current_round"], 0)
                    self.assertFalse((run_directory / "rounds").exists())
                    fake_log = (
                        Path(environment["FAKE_HERDR_STATE_DIR"])
                        / "invocations.jsonl"
                    )
                    self.assertFalse(fake_log.exists())

    def test_first_round_rejects_response_before_round_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            _commit_candidate(repository)
            response = root / "response.json"
            response.write_text("{}\n", encoding="utf-8")
            state_path = repository / ".agent-squad/state.json"
            state_before = state_path.read_bytes()

            submitted = run_cli(
                repository,
                "submit",
                "--report",
                str(report),
                "--response",
                str(response),
                "--mode",
                "new_revision",
                data_home=data_home,
                env_overrides=environment,
            )

            self.assertEqual(submitted.returncode, 1)
            self.assertIn(
                "the first review round does not accept --response",
                submitted.stderr,
            )
            self.assertEqual(state_path.read_bytes(), state_before)
            _, run_directory = _artifacts(repository)
            self.assertFalse((run_directory / "rounds").exists())

    def test_sha256_submission_preserves_full_object_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            try:
                (
                    repository,
                    data_home,
                    report,
                    environment,
                    base_oid,
                ) = _start_run(root, object_format="sha256")
            except subprocess.CalledProcessError:
                self.skipTest(
                    "installed Git cannot create SHA-256 repositories"
                )
            head_oid = _commit_candidate(repository)

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
            self.assertEqual(len(base_oid), 64)
            self.assertEqual(len(head_oid), 64)
            _, run_directory = _artifacts(repository)
            request = json.loads(
                (run_directory / "rounds/001/request.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(request["git_object_format"], "sha256")
            self.assertEqual(request["base_oid"], base_oid)
            self.assertEqual(request["head_oid"], head_oid)

    def test_submit_preserves_canonical_non_ascii_state_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = (
                Path(temporary_directory)
                / "caf\N{LATIN SMALL LETTER E WITH ACUTE}"
            )
            root.mkdir()
            repository, data_home, report, environment, _ = _start_run(root)
            state_path = repository / ".agent-squad/state.json"
            escaped = b"caf\\u00e9"
            encoded = "caf\N{LATIN SMALL LETTER E WITH ACUTE}".encode("utf-8")
            self.assertIn(escaped, state_path.read_bytes())
            self.assertNotIn(encoded, state_path.read_bytes())
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
            self.assertIn(escaped, state_path.read_bytes())
            self.assertNotIn(encoded, state_path.read_bytes())

    def test_head_advance_during_preparation_rolls_back_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, _, report, _, _ = _start_run(root)
            _commit_candidate(repository)
            verify = submissions._verify_review_worktree
            validate_cleanliness = (
                submissions._validate_implementation_cleanliness
            )

            def verify_then_advance(*args: object, **kwargs: object) -> None:
                verify(*args, **kwargs)
                _commit_candidate(repository, "advanced.txt")

            with (
                mock.patch.object(
                    submissions,
                    "_verify_review_worktree",
                    side_effect=verify_then_advance,
                ),
                mock.patch.object(
                    submissions,
                    "_validate_implementation_cleanliness",
                    wraps=validate_cleanliness,
                ) as cleanliness,
                self.assertRaisesRegex(
                    submissions.SubmissionError,
                    "HEAD changed while the review request was being "
                    "prepared",
                ),
            ):
                submissions.submit_candidate(
                    repository,
                    report_path=report,
                    mode="new_revision",
                )

            self.assertEqual(cleanliness.call_count, 2)
            state, run_directory = _artifacts(repository)
            self.assertEqual(state["phase"], "implementing")
            self.assertFalse((run_directory / "rounds").exists())
            worktrees = run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repository,
            ).stdout
            self.assertEqual(worktrees.count("worktree "), 1)

    def test_reviewer_name_failure_is_converted_defensively(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, _, report, _, _ = _start_run(root)
            _commit_candidate(repository)

            with (
                mock.patch.object(
                    submissions,
                    "deterministic_reviewer_name",
                    side_effect=ArtifactValidationError("invalid name"),
                ),
                self.assertRaisesRegex(
                    submissions.SubmissionError,
                    "cannot derive deterministic Reviewer name: invalid name",
                ),
            ):
                submissions.submit_candidate(
                    repository,
                    report_path=report,
                    mode="new_revision",
                )

            state, run_directory = _artifacts(repository)
            self.assertEqual(state["phase"], "implementing")
            self.assertFalse((run_directory / "rounds").exists())

    def test_tracked_reserved_bundle_path_rolls_back_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            reserved_file = repository / ".agent-squad-review/tracked.txt"
            reserved_file.parent.mkdir()
            reserved_file.write_text("candidate content\n", encoding="utf-8")
            run(
                ["git", "add", "-f", ".agent-squad-review/tracked.txt"],
                cwd=repository,
            )
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    "test: track reserved review path",
                ],
                cwd=repository,
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

            self.assertNotEqual(submitted.returncode, 0)
            self.assertIn(
                "candidate revision already contains the reserved "
                "review-bundle path",
                submitted.stderr,
            )
            self.assertEqual(
                reserved_file.read_text(encoding="utf-8"),
                "candidate content\n",
            )
            state, run_directory = _artifacts(repository)
            self.assertEqual(state["phase"], "implementing")
            self.assertFalse((run_directory / "rounds").exists())
            worktrees = run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repository,
            ).stdout
            self.assertEqual(worktrees.count("worktree "), 1)

    def test_case_colliding_tracked_bundle_path_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            collision_file = (
                repository / ".Agent-Squad-Review/tracked.txt"
            )
            collision_file.parent.mkdir()
            collision_file.write_text(
                "candidate content\n",
                encoding="utf-8",
            )
            lowercase_alias = repository / ".agent-squad-review"
            if not lowercase_alias.exists():
                self.skipTest("requires a case-insensitive filesystem")
            run(
                ["git", "add", "-f", ".Agent-Squad-Review/tracked.txt"],
                cwd=repository,
            )
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    "test: track colliding review path",
                ],
                cwd=repository,
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

            self.assertNotEqual(submitted.returncode, 0)
            self.assertIn(
                "candidate revision already contains the reserved "
                "review-bundle path",
                submitted.stderr,
            )
            self.assertEqual(
                collision_file.read_text(encoding="utf-8"),
                "candidate content\n",
            )
            worktrees = run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repository,
            ).stdout
            self.assertEqual(worktrees.count("worktree "), 1)

    def test_partially_built_owned_bundle_is_removed_on_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, _, report, _, _ = _start_run(root)
            _commit_candidate(repository)

            def fail_after_partial_write(**arguments: object) -> None:
                bundle_root = arguments["bundle_root"]
                if not isinstance(bundle_root, Path):
                    raise AssertionError("bundle root must be a path")
                (bundle_root / "partial.txt").write_text(
                    "partial\n",
                    encoding="utf-8",
                )
                raise submissions.SubmissionError("injected bundle failure")

            with (
                mock.patch.object(
                    submissions,
                    "_build_review_bundle",
                    side_effect=fail_after_partial_write,
                ),
                self.assertRaisesRegex(
                    submissions.SubmissionError,
                    "injected bundle failure",
                ),
            ):
                submissions.submit_candidate(
                    repository,
                    report_path=report,
                    mode="new_revision",
                )

            state, run_directory = _artifacts(repository)
            self.assertEqual(state["phase"], "implementing")
            self.assertFalse((run_directory / "rounds").exists())
            worktrees = run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repository,
            ).stdout
            self.assertEqual(worktrees.count("worktree "), 1)

    def test_unignored_review_bundle_rolls_back_and_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            ignore_file = repository / ".gitignore"
            ignore_file.write_text(
                "!.agent-squad-review/\n",
                encoding="utf-8",
            )
            run(["git", "add", ".gitignore"], cwd=repository)
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    "test: expose review bundle",
                ],
                cwd=repository,
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

            self.assertNotEqual(submitted.returncode, 0)
            self.assertIn(
                ".agent-squad-review is not excluded",
                submitted.stderr,
            )
            state, run_directory = _artifacts(repository)
            self.assertEqual(state["phase"], "implementing")
            self.assertFalse((run_directory / "rounds").exists())
            worktrees = run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repository,
            ).stdout
            self.assertEqual(worktrees.count("worktree "), 1)

            ignore_file.write_text(
                ".agent-squad-review/\n",
                encoding="utf-8",
            )
            run(["git", "add", ".gitignore"], cwd=repository)
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    "test: exclude review bundle",
                ],
                cwd=repository,
            )

            retried = run_cli(
                repository,
                "submit",
                "--report",
                str(report),
                "--mode",
                "new_revision",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(retried.returncode, 0, retried.stderr)

    def test_postcommit_event_failure_preserves_pending_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            _commit_candidate(repository)
            client = mock.Mock()

            with (
                mock.patch.object(
                    submissions,
                    "append_event",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(
                    submissions.SubmissionError,
                    "review request is durable",
                ),
            ):
                submissions.submit_candidate(
                    repository,
                    report_path=report,
                    mode="new_revision",
                    herdr_client=client,
                )

            client.discover.assert_not_called()
            state, run_directory = _artifacts(repository)
            self.assertEqual(state["phase"], "reviewing")
            self.assertEqual(state["current_round"], 1)
            self.assertEqual(state["handoff"]["status"], "pending")
            self.assertTrue(
                Path(state["active_round"]["review_worktree"]).is_dir()
            )
            run_record = json.loads(
                (run_directory / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(run_record["phase"], "reviewing")
            events = (
                (run_directory / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            self.assertEqual(len(events), 1)

            status = run_cli(
                repository,
                "status",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Request handoff: pending", status.stdout)
            self.assertIn("Review worktree available: yes", status.stdout)

    def test_handoff_guards_do_not_overwrite_changed_state(self) -> None:
        cases = (
            ("run", "active run changed"),
            ("request", "active round changed"),
        )
        for mutation, message in cases:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    repository, _, report, _, _ = _start_run(root)
                    _commit_candidate(repository)
                    state_path = repository / ".agent-squad/state.json"
                    changed_state: list[bytes] = []
                    client = mock.Mock()
                    client.discover.return_value = HerdrInstallation(
                        executable=Path("/fake/herdr"),
                        version="herdr test",
                        protocol=20,
                    )

                    def mutate_state(**_arguments: object) -> None:
                        state = json.loads(
                            state_path.read_text(encoding="utf-8")
                        )
                        if mutation == "run":
                            state["active_run_id"] = (
                                "87654321-4321-6789-9234-567812345678"
                            )
                        else:
                            state["active_round"]["request_id"] = (
                                "87654321-4321-6789-9234-567812345678"
                            )
                        state_path.write_text(
                            json.dumps(state, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        changed_state.append(state_path.read_bytes())

                    client.dispatch_review_request.side_effect = mutate_state

                    with self.assertRaisesRegex(
                        submissions.SubmissionError,
                        message,
                    ):
                        submissions.submit_candidate(
                            repository,
                            report_path=report,
                            mode="new_revision",
                            herdr_client=client,
                        )

                    client.discover.assert_called_once_with(
                        AgentKind.CLAUDE,
                        role="Reviewer",
                    )
                    self.assertEqual(state_path.read_bytes(), changed_state[0])
                    state = json.loads(
                        state_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual(state["handoff"]["status"], "pending")

    def test_handoff_event_failure_preserves_sent_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, _, report, _, _ = _start_run(root)
            _commit_candidate(repository)
            client = mock.Mock()
            client.discover.return_value = HerdrInstallation(
                executable=Path("/fake/herdr"),
                version="herdr test",
                protocol=20,
            )
            append_event = handoffs.append_event

            def fail_sent_event(
                path: Path,
                event: dict[str, object],
            ) -> None:
                if event["event"] == "review_request_sent":
                    raise OSError("disk full")
                append_event(path, event)

            with (
                mock.patch.object(
                    handoffs,
                    "append_event",
                    side_effect=fail_sent_event,
                ),
                self.assertRaisesRegex(
                    submissions.SubmissionError,
                    "handoff state is durable",
                ),
            ):
                submissions.submit_candidate(
                    repository,
                    report_path=report,
                    mode="new_revision",
                    herdr_client=client,
                )

            state, run_directory = _artifacts(repository)
            self.assertEqual(state["handoff"]["status"], "sent")
            events = [
                json.loads(line)["event"]
                for line in (run_directory / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                events,
                ["run_started", "review_request_persisted"],
            )

    def test_status_reports_a_missing_disposable_review_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            head_oid = _commit_candidate(repository)
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
            state, _ = _artifacts(repository)
            review_worktree = Path(
                state["active_round"]["review_worktree"]
            )
            run(
                ["git", "worktree", "remove", str(review_worktree)],
                cwd=repository,
            )

            status = run_cli(
                repository,
                "status",
                data_home=data_home,
                env_overrides=environment,
            )

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Phase: reviewing", status.stdout)
            self.assertIn(f"Current requested head: {head_oid}", status.stdout)
            self.assertIn("Request handoff: sent", status.stdout)
            self.assertIn(f"Review worktree: {review_worktree}", status.stdout)
            self.assertIn("Review worktree available: no", status.stdout)
            self.assertIn("Next action: wait for the Reviewer", status.stdout)

            target = root / "replacement-worktree"
            target.mkdir()
            review_worktree.parent.mkdir(parents=True, exist_ok=True)
            review_worktree.symlink_to(target, target_is_directory=True)
            invalid = run_cli(
                repository,
                "status",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("not a normal directory", invalid.stderr)

    def test_sensitive_change_warns_but_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, report, environment, _ = _start_run(root)
            _commit_candidate(repository, "AGENTS.md")

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
            self.assertIn("agent-squad: warning:", submitted.stderr)
            self.assertIn("AGENTS.md", submitted.stderr)
            _, run_directory = _artifacts(repository)
            round_record = json.loads(
                (run_directory / "rounds/001/round.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("AGENTS.md", round_record["warnings"][0])

    def test_known_generated_files_and_untracked_report_are_accepted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, _, environment, _ = _start_run(root)
            _commit_candidate(repository)
            configuration_path = repository / ".agent-squad/config.json"
            configuration = json.loads(
                configuration_path.read_text(encoding="utf-8")
            )
            configuration["allowed_generated_paths"] = ["build/"]
            configuration_path.write_text(
                json.dumps(configuration),
                encoding="utf-8",
            )
            generated = repository / "build/output.bin"
            generated.parent.mkdir()
            generated.write_bytes(b"generated\n")
            report = repository / "implementation-report.md"
            report_bytes = b"# Implementation Report\n\nReady for review.\n"
            report.write_bytes(report_bytes)

            submitted = run_cli(
                repository,
                "submit",
                "--report",
                report.name,
                "--mode",
                "new_revision",
                data_home=data_home,
                env_overrides=environment,
            )

            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            state, _ = _artifacts(repository)
            review_worktree = Path(
                state["active_round"]["review_worktree"]
            )
            request = json.loads(
                (
                    review_worktree
                    / ".agent-squad-review/input/request.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(request["allowed_generated_paths"], ["build/"])
            self.assertFalse(
                (review_worktree / "implementation-report.md").exists()
            )
            self.assertFalse((review_worktree / "build/output.bin").exists())
            self.assertEqual(
                (
                    review_worktree
                    / ".agent-squad-review/input/implementation-report.md"
                ).read_bytes(),
                report_bytes,
            )

    def test_untracked_report_through_symlink_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository, data_home, _, environment, _ = _start_run(root)
            _commit_candidate(repository)
            report = repository / "implementation-report.md"
            report_bytes = b"# Implementation Report\n\nReady for review.\n"
            report.write_bytes(report_bytes)
            repository_alias = root / "repository-alias"
            repository_alias.symlink_to(repository, target_is_directory=True)

            submitted = run_cli(
                repository,
                "submit",
                "--report",
                str(repository_alias / report.name),
                "--mode",
                "new_revision",
                data_home=data_home,
                env_overrides=environment,
            )

            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            state, _ = _artifacts(repository)
            review_worktree = Path(
                state["active_round"]["review_worktree"]
            )
            captured_report = (
                review_worktree
                / ".agent-squad-review/input/implementation-report.md"
            )
            self.assertEqual(captured_report.read_bytes(), report_bytes)

    def test_status_rejects_authoritative_tampering_and_warns_for_live_bundle(
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
            state, run_directory = _artifacts(repository)
            request_path = run_directory / "rounds/001/request.json"
            request_bytes = request_path.read_bytes()
            request_path.chmod(0o600)
            request_path.write_text("{}\n", encoding="utf-8")

            status = run_cli(
                repository,
                "status",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertNotEqual(status.returncode, 0)
            self.assertIn("request digest does not match", status.stderr)

            request_path.write_bytes(request_bytes)
            request_path.chmod(0o400)
            review_worktree = Path(
                state["active_round"]["review_worktree"]
            )
            bundle_task = (
                review_worktree / ".agent-squad-review/input/task.md"
            )
            bundle_task.chmod(0o600)
            bundle_task.write_text("tampered\n", encoding="utf-8")
            status = run_cli(
                repository,
                "status",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Review bundle intact: no", status.stdout)
            self.assertIn(
                "Review bundle warning: active review bundle input "
                "input/task.md digest does not match run metadata",
                status.stdout,
            )

            applied = run_cli(
                repository,
                "apply-review",
                "--result-id",
                "99999999-9999-4999-8999-999999999999",
                data_home=data_home,
                env_overrides=environment,
            )
            self.assertNotEqual(applied.returncode, 0)
            self.assertIn("bundle input input/task.md", applied.stderr)
            self.assertIn("digest does not match", applied.stderr)

    def test_status_rejects_live_round_mislabeled_as_implementing(
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

            state, run_directory = _artifacts(repository)
            state["phase"] = "implementing"
            state_path = repository / ".agent-squad/state.json"
            state_path.write_text(
                json.dumps(state, indent=2) + "\n",
                encoding="utf-8",
            )
            run_record_path = run_directory / "run.json"
            run_record = json.loads(
                run_record_path.read_text(encoding="utf-8")
            )
            run_record["phase"] = "implementing"
            run_record_path.write_text(
                json.dumps(run_record, indent=2) + "\n",
                encoding="utf-8",
            )

            status = run_cli(
                repository,
                "status",
                data_home=data_home,
                env_overrides=environment,
            )

            self.assertNotEqual(status.returncode, 0)
            self.assertEqual(status.stdout, "")
            self.assertIn(
                "implementing run must record a closed active round",
                status.stderr,
            )

    def test_start_or_prompt_failure_preserves_one_recoverable_round(
        self,
    ) -> None:
        for failure_variable in (
            "FAKE_HERDR_FAIL_START",
            "FAKE_HERDR_FAIL_PROMPT",
        ):
            with self.subTest(failure=failure_variable):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    (
                        repository,
                        data_home,
                        report,
                        environment,
                        _,
                    ) = _start_run(root)
                    _commit_candidate(repository)
                    environment[failure_variable] = "1"

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

                    self.assertNotEqual(submitted.returncode, 0)
                    self.assertIn("preserved for recovery", submitted.stderr)
                    state, run_directory = _artifacts(repository)
                    self.assertEqual(state["phase"], "reviewing")
                    self.assertEqual(state["current_round"], 1)
                    self.assertEqual(state["handoff"]["status"], "failed")
                    self.assertIn("injected", state["handoff"]["last_error"])
                    self.assertEqual(
                        [
                            path.name
                            for path in (run_directory / "rounds").iterdir()
                        ],
                        ["001"],
                    )
                    status = run_cli(
                        repository,
                        "status",
                        data_home=data_home,
                        env_overrides=environment,
                    )
                    self.assertEqual(status.returncode, 0, status.stderr)
                    self.assertIn("Request handoff: failed", status.stdout)
                    self.assertIn(
                        "Next action: agent-squad retry-handoff",
                        status.stdout,
                    )
                    retry = run_cli(
                        repository,
                        "submit",
                        "--report",
                        str(report),
                        "--mode",
                        "new_revision",
                        data_home=data_home,
                        env_overrides=environment,
                    )
                    self.assertNotEqual(retry.returncode, 0)
                    self.assertIn("phase reviewing", retry.stderr)
                    self.assertEqual(
                        [
                            path.name
                            for path in (run_directory / "rounds").iterdir()
                        ],
                        ["001"],
                    )
                    worktrees = run(
                        ["git", "worktree", "list", "--porcelain"],
                        cwd=repository,
                    ).stdout
                    self.assertEqual(worktrees.count("worktree "), 2)


if __name__ == "__main__":
    unittest.main()
