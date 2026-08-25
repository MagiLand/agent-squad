from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import tempfile
from threading import Barrier
import unittest
from unittest import mock
import uuid

from tests._support import (
    add_src_to_path,
    git_path,
    initialize_git_repository,
    run,
    run_cli,
    seed_commit,
    seed_git_repository,
)


add_src_to_path()

from agent_squad import runs  # noqa: E402


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class StartAndStatusCommandTests(unittest.TestCase):
    def test_start_and_status_require_repository_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            task = temporary_root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            data_home = temporary_root / "data"

            started = run_cli(
                repository,
                "start",
                "--task",
                str(task),
                "--base",
                "main",
                data_home=data_home,
            )
            status = run_cli(repository, "status", data_home=data_home)

            for result in (started, status):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not initialized", result.stderr)
                self.assertIn("agent-squad init", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((repository / ".agent-squad").exists())

    def test_start_captures_inputs_identity_roles_base_and_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            base_oid = seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            inputs = temporary_root / "inputs"
            inputs.mkdir()
            task = inputs / "task.md"
            task_bytes = b"# Task\n\nImplement the requested behavior.\n"
            task.write_bytes(task_bytes)
            first_context_root = inputs / "one"
            second_context_root = inputs / "two"
            first_context_root.mkdir()
            second_context_root.mkdir()
            first_context = first_context_root / "notes.txt"
            second_context = second_context_root / "notes.txt"
            first_context.write_bytes(b"first context\n")
            second_context.write_bytes(b"second context\n")

            result = run_cli(
                repository,
                "start",
                "--task",
                str(task),
                "--context",
                str(first_context),
                "--context",
                str(second_context),
                "--implementer",
                "codex-issue-3",
                "--reviewer",
                "codex",
                "--base",
                "main",
                data_home=data_home,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            control_root = repository / ".agent-squad"
            state_path = control_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_id = state["active_run_id"]
            self.assertEqual(str(uuid.UUID(run_id)), run_id)
            run_directory = control_root / "runs" / run_id
            run_record = json.loads(
                (run_directory / "run.json").read_text(encoding="utf-8")
            )

            canonical_repository = repository.resolve()
            common_directory = git_path(repository, "--git-common-dir")
            git_directory = git_path(repository, "--git-dir")
            repository_id = hashlib.sha256(
                str(common_directory).encode()
            ).hexdigest()
            self.assertEqual(state["phase"], "implementing")
            self.assertEqual(
                state["implementation_root"], str(canonical_repository)
            )
            self.assertEqual(state["git_common_dir"], str(common_directory))
            self.assertEqual(state["worktree_git_dir"], str(git_directory))
            self.assertEqual(state["repository_id"], repository_id)
            self.assertEqual(state["base_oid"], base_oid)
            self.assertEqual(state["current_round"], 0)
            self.assertIsNone(state["active_round"])
            self.assertEqual(
                state["review_budget"],
                {
                    "original_limit": 4,
                    "additional_rounds_granted": 0,
                    "effective_limit": 4,
                    "completed_change_reviews": 0,
                },
            )

            self.assertEqual(run_record["run_id"], run_id)
            self.assertEqual(run_record["phase"], "implementing")
            self.assertIsNone(run_record["finished_at"])
            self.assertEqual(run_record["task"]["path"], "task.md")
            self.assertEqual(
                run_record["task"]["sha256"],
                hashlib.sha256(task_bytes).hexdigest(),
            )
            self.assertEqual(
                run_record["repository"],
                {
                    "implementation_root": str(canonical_repository),
                    "git_common_dir": str(common_directory),
                    "worktree_git_dir": str(git_directory),
                    "repository_id": repository_id,
                    "start_branch_ref": "refs/heads/main",
                    "start_head_detached": False,
                },
            )
            self.assertEqual(run_record["base_ref"], "main")
            self.assertEqual(run_record["base_oid"], base_oid)
            self.assertEqual(run_record["git_object_format"], "sha1")
            self.assertEqual(
                run_record["implementer"],
                {"agent_name": "codex-issue-3", "kind": "codex"},
            )
            self.assertEqual(
                run_record["reviewer"],
                {"kind": "codex", "start_args": []},
            )
            self.assertEqual(
                run_record["initial_review_budget"],
                state["review_budget"],
            )
            self.assertEqual(
                [item["path"] for item in run_record["context_files"]],
                ["context/001/notes.txt", "context/002/notes.txt"],
            )
            self.assertEqual(
                [item["sha256"] for item in run_record["context_files"]],
                [
                    hashlib.sha256(b"first context\n").hexdigest(),
                    hashlib.sha256(b"second context\n").hexdigest(),
                ],
            )
            self.assertEqual(
                (run_directory / "task.md").read_bytes(), task_bytes
            )
            self.assertEqual(
                (run_directory / "context/001/notes.txt").read_bytes(),
                b"first context\n",
            )
            self.assertEqual(
                (run_directory / "context/002/notes.txt").read_bytes(),
                b"second context\n",
            )
            for path, expected_mode in (
                (state_path, 0o600),
                (control_root / "lock", 0o600),
                (run_directory / "run.json", 0o600),
                (run_directory / "events.jsonl", 0o600),
                (run_directory / "task.md", 0o400),
                (run_directory / "context/001/notes.txt", 0o400),
            ):
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode),
                    expected_mode,
                )

            events = [
                json.loads(line)
                for line in (run_directory / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "run_started")
            self.assertEqual(events[0]["run_id"], run_id)
            self.assertEqual(events[0]["base_oid"], base_oid)
            self.assertTrue(events[0]["timestamp"].endswith("Z"))

            task.write_text("changed source\n", encoding="utf-8")
            first_context.write_text("changed context\n", encoding="utf-8")
            self.assertEqual(
                (run_directory / "task.md").read_bytes(),
                task_bytes,
            )
            self.assertEqual(
                (run_directory / "context/001/notes.txt").read_bytes(),
                b"first context\n",
            )

            status = run_cli(repository, "status", data_home=data_home)
            self.assertEqual(status.returncode, 0, status.stderr)
            for expected in (
                f"Active run: {run_id}",
                "Phase: implementing",
                f"Repository ID: {repository_id}",
                "Implementer: codex-issue-3 (codex)",
                "Reviewer: codex",
                f"Base: main -> {base_oid}",
                "Started from: refs/heads/main",
                "Review budget: 0/4 completed change reviews",
                "Current round: none",
                "Next action: continue implementing the captured task",
            ):
                self.assertIn(expected, status.stdout)
            self.assertNotIn("Developer resolutions:", status.stdout)
            self.assertNotIn(
                "Marker-confirmed unapplied result:",
                status.stdout,
            )

    def test_start_uses_validated_repository_configuration_as_defaults(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            base_oid = seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            config_path = repository / ".agent-squad/config.json"
            configuration = json.loads(config_path.read_text(encoding="utf-8"))
            configuration["implementer"] = {
                "agent_name": "configured-implementer",
                "kind": "codex",
            }
            configuration["reviewer"] = {
                "kind": "claude",
                "start_args": ["--profile", "careful review"],
            }
            configuration["base_ref"] = "main"
            configuration["max_completed_change_reviews"] = 6
            config_path.write_text(
                json.dumps(configuration),
                encoding="utf-8",
            )
            task = temporary_root / "task.md"
            task.write_text("# Configured task\n", encoding="utf-8")
            nested = repository / "nested"
            nested.mkdir()

            result = run_cli(
                nested,
                "start",
                "--task",
                "../../task.md",
                data_home=data_home,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(
                (repository / ".agent-squad/state.json").read_text(
                    encoding="utf-8"
                )
            )
            run_record = json.loads(
                (
                    repository
                    / ".agent-squad/runs"
                    / state["active_run_id"]
                    / "run.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(run_record["base_ref"], "main")
            self.assertEqual(run_record["base_oid"], base_oid)
            self.assertEqual(
                run_record["implementer"],
                {"agent_name": "configured-implementer", "kind": "codex"},
            )
            self.assertEqual(
                run_record["reviewer"],
                {
                    "kind": "claude",
                    "start_args": ["--profile", "careful review"],
                },
            )
            self.assertEqual(state["review_budget"]["original_limit"], 6)
            self.assertEqual(state["review_budget"]["effective_limit"], 6)
            status = run_cli(nested, "status", data_home=data_home)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn(
                "Implementer: configured-implementer (codex)",
                status.stdout,
            )
            self.assertIn("Reviewer: claude", status.stdout)
            self.assertIn(
                "Review budget: 0/6 completed change reviews",
                status.stdout,
            )

    def test_role_overrides_do_not_cross_agent_kind_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            config_path = repository / ".agent-squad/config.json"
            configuration = json.loads(config_path.read_text(encoding="utf-8"))
            configuration["implementer"] = {
                "agent_name": "configured-codex",
                "kind": "codex",
            }
            configuration["reviewer"] = {
                "kind": "claude",
                "start_args": ["--model", "claude-opus"],
            }
            configuration["base_ref"] = "main"
            config_path.write_text(
                json.dumps(configuration),
                encoding="utf-8",
            )
            task = temporary_root / "task.md"
            task.write_text("# Override roles\n", encoding="utf-8")

            result = run_cli(
                repository,
                "start",
                "--task",
                str(task),
                "--implementer",
                "alternate-codex",
                "--reviewer",
                "codex",
                data_home=data_home,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(
                (repository / ".agent-squad/state.json").read_text(
                    encoding="utf-8"
                )
            )
            run_record = json.loads(
                (
                    repository
                    / ".agent-squad/runs"
                    / state["active_run_id"]
                    / "run.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                run_record["implementer"],
                {"agent_name": "alternate-codex", "kind": "codex"},
            )
            self.assertEqual(
                run_record["reviewer"],
                {"kind": "codex", "start_args": []},
            )
            status = run_cli(repository, "status", data_home=data_home)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn(
                "Implementer: alternate-codex (codex)",
                status.stdout,
            )
            self.assertIn("Reviewer: codex", status.stdout)

    def test_explicit_empty_implementer_and_base_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")

            cases = (
                ("--implementer", "Implementer agent identity"),
                ("--base", "base reference"),
            )
            for option, label in cases:
                with self.subTest(option=option):
                    result = run_cli(
                        repository,
                        "start",
                        "--task",
                        str(task),
                        option,
                        "",
                        data_home=data_home,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        f"{label} must be non-empty",
                        result.stderr,
                    )

            control_root = repository / ".agent-squad"
            self.assertFalse((control_root / "state.json").exists())
            self.assertFalse((control_root / "runs").exists())

    def test_status_reports_initialized_repository_without_side_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            nested = repository / "nested"
            nested.mkdir()

            result = run_cli(nested, "status", data_home=data_home)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Repository: {repository.resolve()}", result.stdout)
            self.assertIn("Active run: none", result.stdout)
            self.assertIn(
                "Next action: agent-squad start --task <task.md>",
                result.stdout,
            )
            control_root = repository / ".agent-squad"
            self.assertFalse((control_root / "state.json").exists())
            self.assertFalse((control_root / "runs").exists())
            self.assertFalse((control_root / "lock").exists())

    def test_second_active_run_is_rejected_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            first_task = temporary_root / "first.md"
            missing_second_task = temporary_root / "missing-second.md"
            first_task.write_text("# First task\n", encoding="utf-8")
            first = run_cli(
                repository,
                "start",
                "--task",
                str(first_task),
                "--base",
                "main",
                data_home=data_home,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            control_root = repository / ".agent-squad"
            before = _snapshot_files(control_root)

            second = run_cli(
                repository,
                "start",
                "--task",
                str(missing_second_task),
                "--base",
                "main",
                data_home=data_home,
            )

            self.assertNotEqual(second.returncode, 0)
            self.assertIn(
                "is already active in phase implementing",
                second.stderr,
            )
            self.assertNotIn("Traceback", second.stderr)
            self.assertEqual(_snapshot_files(control_root), before)
            self.assertEqual(
                len(list((control_root / "runs").iterdir())),
                1,
            )

    def test_status_rejects_inconsistent_authoritative_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
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
            state_path = repository / ".agent-squad/state.json"
            original = json.loads(state_path.read_text(encoding="utf-8"))

            cases = (
                (
                    "terminal phase",
                    "active_run_id must be null when phase is completed",
                ),
                (
                    "phase mismatch",
                    "state.phase does not match",
                ),
                (
                    "budget mismatch",
                    "original limit does not match",
                ),
                (
                    "base mismatch",
                    "state base OID does not match",
                ),
            )
            for case, message in cases:
                with self.subTest(case=case):
                    state = json.loads(json.dumps(original))
                    if case == "terminal phase":
                        state["phase"] = "completed"
                    elif case == "phase mismatch":
                        state["phase"] = "reviewing"
                    elif case == "budget mismatch":
                        state["review_budget"]["original_limit"] = 5
                        state["review_budget"]["effective_limit"] = 5
                    else:
                        state["base_oid"] = "f" * 40
                    state_path.write_text(
                        json.dumps(state),
                        encoding="utf-8",
                    )

                    status = run_cli(
                        repository,
                        "status",
                        data_home=data_home,
                    )

                    self.assertNotEqual(status.returncode, 0)
                    self.assertIn(message, status.stderr)

    def test_status_rejects_invalid_run_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
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
            state = json.loads(
                (repository / ".agent-squad/state.json").read_text(
                    encoding="utf-8"
                )
            )
            record_path = (
                repository
                / ".agent-squad/runs"
                / state["active_run_id"]
                / "run.json"
            )
            original = json.loads(record_path.read_text(encoding="utf-8"))

            cases = (
                (
                    "repository identity",
                    "branch and detached fields are inconsistent",
                ),
                (
                    "reviewer arguments",
                    "non-empty strings",
                ),
                (
                    "finish time",
                    "finished_at must be set exactly when the phase is "
                    "terminal",
                ),
            )
            for case, message in cases:
                with self.subTest(case=case):
                    record = json.loads(json.dumps(original))
                    if case == "repository identity":
                        record["repository"]["start_branch_ref"] = None
                        record["repository"]["start_head_detached"] = False
                    elif case == "reviewer arguments":
                        record["reviewer"]["start_args"] = [""]
                    else:
                        record["finished_at"] = "2026-08-24T10:00:00Z"
                    record_path.write_text(
                        json.dumps(record),
                        encoding="utf-8",
                    )

                    status = run_cli(
                        repository,
                        "status",
                        data_home=data_home,
                    )

                    self.assertNotEqual(status.returncode, 0)
                    self.assertIn(message, status.stderr)

    def test_concurrent_starts_create_exactly_one_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            tasks = [temporary_root / "first.md", temporary_root / "second.md"]
            for index, task in enumerate(tasks, start=1):
                task.write_text(f"# Task {index}\n", encoding="utf-8")
            barrier = Barrier(2)

            def attempt_start(task: Path) -> tuple[str, str]:
                barrier.wait()
                try:
                    result = runs.start_run(
                        repository,
                        task_path=task,
                        base_ref="main",
                    )
                except runs.RunStartError as error:
                    return "rejected", str(error)
                return "started", result.run_id

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(attempt_start, tasks))

            self.assertEqual(
                sorted(outcome[0] for outcome in outcomes),
                ["rejected", "started"],
            )
            rejection = next(
                detail for outcome, detail in outcomes if outcome == "rejected"
            )
            self.assertIn("is already active in phase implementing", rejection)
            control_root = repository / ".agent-squad"
            state = json.loads(
                (control_root / "state.json").read_text(encoding="utf-8")
            )
            started_run_id = next(
                detail for outcome, detail in outcomes if outcome == "started"
            )
            self.assertEqual(state["active_run_id"], started_run_id)
            self.assertEqual(len(list((control_root / "runs").iterdir())), 1)

    def test_unresolvable_base_leaves_repository_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")

            result = run_cli(
                repository,
                "start",
                "--task",
                str(task),
                "--base",
                "refs/heads/does-not-exist",
                data_home=data_home,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot resolve base reference", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            control_root = repository / ".agent-squad"
            self.assertFalse((control_root / "state.json").exists())
            self.assertFalse((control_root / "runs").exists())
            status = run_cli(repository, "status", data_home=data_home)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Active run: none", status.stdout)

    def test_fixed_base_does_not_move_when_branch_advances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            original_base_oid = seed_git_repository(repository)
            run(
                ["git", "branch", "base-branch", original_base_oid],
                cwd=repository,
            )
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            started = run_cli(
                repository,
                "start",
                "--task",
                str(task),
                "--base",
                "base-branch",
                data_home=data_home,
            )
            self.assertEqual(started.returncode, 0, started.stderr)

            advanced_oid = seed_commit(
                repository,
                content="advanced fixture\n",
            )
            run(
                ["git", "branch", "-f", "base-branch", advanced_oid],
                cwd=repository,
            )
            self.assertNotEqual(advanced_oid, original_base_oid)

            status = run_cli(repository, "status", data_home=data_home)

            state = json.loads(
                (repository / ".agent-squad/state.json").read_text(
                    encoding="utf-8"
                )
            )
            record = json.loads(
                (
                    repository
                    / ".agent-squad/runs"
                    / state["active_run_id"]
                    / "run.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(state["base_oid"], original_base_oid)
            self.assertEqual(record["base_oid"], original_base_oid)
            self.assertIn(
                f"Base: base-branch -> {original_base_oid}",
                status.stdout,
            )
            self.assertNotIn(advanced_oid, status.stdout)

    def test_lock_failure_is_reported_without_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")

            with mock.patch.object(
                runs,
                "exclusive_file_lock",
                side_effect=PermissionError("simulated lock failure"),
            ):
                with self.assertRaisesRegex(
                    runs.RunStartError,
                    "could not acquire or use the local run lock",
                ):
                    runs.start_run(
                        repository,
                        task_path=task,
                        base_ref="main",
                    )

            control_root = repository / ".agent-squad"
            self.assertFalse((control_root / "state.json").exists())
            self.assertFalse((control_root / "runs").exists())
            self.assertFalse((control_root / "lock").exists())

    def test_state_write_failure_rolls_back_complete_staged_run_under_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            real_atomic_write = runs.atomic_write
            real_lock = runs.exclusive_file_lock
            lock_held = False
            written_names: list[str] = []

            @contextmanager
            def observed_lock(path: Path):
                nonlocal lock_held
                with real_lock(path):
                    lock_held = True
                    try:
                        yield
                    finally:
                        lock_held = False

            def failing_state_write(
                path: Path,
                content: bytes,
                *,
                mode: int,
            ) -> None:
                self.assertTrue(lock_held)
                written_names.append(path.name)
                if path.name == "state.json":
                    raise PermissionError("simulated state write failure")
                real_atomic_write(path, content, mode=mode)

            with (
                mock.patch.object(
                    runs,
                    "exclusive_file_lock",
                    side_effect=observed_lock,
                ),
                mock.patch.object(
                    runs,
                    "atomic_write",
                    side_effect=failing_state_write,
                ),
            ):
                with self.assertRaisesRegex(
                    runs.RunStartError,
                    "could not start the run atomically",
                ):
                    runs.start_run(
                        repository,
                        task_path=task,
                        base_ref="main",
                    )

            self.assertEqual(
                written_names,
                ["task.md", "run.json", "events.jsonl", "state.json"],
            )
            control_root = repository / ".agent-squad"
            self.assertFalse((control_root / "state.json").exists())
            self.assertFalse((control_root / "runs").exists())
            self.assertTrue((control_root / "lock").is_file())

    def test_rollback_failure_is_reported_with_the_start_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            real_atomic_write = runs.atomic_write

            def failing_state_write(
                path: Path,
                content: bytes,
                *,
                mode: int,
            ) -> None:
                if path.name == "state.json":
                    raise PermissionError("simulated state write failure")
                real_atomic_write(path, content, mode=mode)

            with (
                mock.patch.object(
                    runs,
                    "atomic_write",
                    side_effect=failing_state_write,
                ),
                mock.patch.object(
                    runs.shutil,
                    "rmtree",
                    side_effect=PermissionError(
                        "simulated rollback failure"
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    runs.RunStartError,
                    "Rollback also encountered: could not remove newly "
                    "created",
                ):
                    runs.start_run(
                        repository,
                        task_path=task,
                        base_ref="main",
                    )

    def test_detached_start_records_detached_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            run(["git", "checkout", "--detach"], cwd=repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
            task.write_text("# Detached task\n", encoding="utf-8")

            result = run_cli(
                repository,
                "start",
                "--task",
                str(task),
                "--base",
                "HEAD",
                data_home=data_home,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(
                (repository / ".agent-squad/state.json").read_text(
                    encoding="utf-8"
                )
            )
            run_record = json.loads(
                (
                    repository
                    / ".agent-squad/runs"
                    / state["active_run_id"]
                    / "run.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIsNone(run_record["repository"]["start_branch_ref"])
            self.assertTrue(run_record["repository"]["start_head_detached"])
            status = run_cli(repository, "status", data_home=data_home)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Started from: detached HEAD", status.stdout)

    def test_linked_worktree_records_common_and_per_worktree_git_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            primary = temporary_root / "primary"
            base_oid = seed_git_repository(primary)
            linked = temporary_root / "linked"
            run(
                ["git", "worktree", "add", "--detach", str(linked), "HEAD"],
                cwd=primary,
            )
            data_home = temporary_root / "data"
            initialized = run_cli(linked, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
            task.write_text("# Linked worktree task\n", encoding="utf-8")

            result = run_cli(
                linked,
                "start",
                "--task",
                str(task),
                "--base",
                "HEAD",
                data_home=data_home,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(
                (linked / ".agent-squad/state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["base_oid"], base_oid)
            self.assertEqual(
                state["git_common_dir"],
                str(git_path(linked, "--git-common-dir")),
            )
            self.assertEqual(
                state["worktree_git_dir"], str(git_path(linked, "--git-dir"))
            )
            self.assertNotEqual(
                state["git_common_dir"], state["worktree_git_dir"]
            )

    def test_sha256_repository_records_full_configured_object_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            try:
                initialize_git_repository(
                    repository,
                    object_format="sha256",
                )
            except subprocess.CalledProcessError:
                self.skipTest(
                    "installed Git cannot create SHA-256 repositories"
                )
            expected_oid = seed_commit(
                repository,
                content="sha256 fixture\n",
            )
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
            task.write_text("# SHA-256 task\n", encoding="utf-8")

            result = run_cli(
                repository,
                "start",
                "--task",
                str(task),
                "--base",
                "main",
                data_home=data_home,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(expected_oid), 64)
            state = json.loads(
                (repository / ".agent-squad/state.json").read_text(
                    encoding="utf-8"
                )
            )
            run_record = json.loads(
                (
                    repository
                    / ".agent-squad/runs"
                    / state["active_run_id"]
                    / "run.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(state["base_oid"], expected_oid)
            self.assertEqual(run_record["base_oid"], expected_oid)
            self.assertEqual(run_record["git_object_format"], "sha256")

    def test_duplicate_context_source_is_rejected_before_runtime_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
            context = temporary_root / "context.txt"
            task.write_text("# Task\n", encoding="utf-8")
            context.write_text("context\n", encoding="utf-8")

            result = run_cli(
                repository,
                "start",
                "--task",
                str(task),
                "--context",
                str(context),
                "--context",
                str(context),
                "--base",
                "main",
                data_home=data_home,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("provided more than once", result.stderr)
            control_root = repository / ".agent-squad"
            self.assertFalse((control_root / "state.json").exists())
            self.assertFalse((control_root / "runs").exists())
            self.assertTrue((control_root / "lock").is_file())

    def test_status_detects_changed_captured_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
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
            state = json.loads(
                (repository / ".agent-squad/state.json").read_text(
                    encoding="utf-8"
                )
            )
            captured_task = (
                repository
                / ".agent-squad/runs"
                / state["active_run_id"]
                / "task.md"
            )
            captured_task.chmod(0o600)
            captured_task.write_text("changed\n", encoding="utf-8")

            status = run_cli(repository, "status", data_home=data_home)

            self.assertNotEqual(status.returncode, 0)
            self.assertIn(
                "captured task digest does not match run metadata",
                status.stderr,
            )
            self.assertNotIn("Traceback", status.stderr)

    def test_status_revalidates_repository_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            seed_git_repository(repository)
            data_home = temporary_root / "data"
            initialized = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            task = temporary_root / "task.md"
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
            state_path = repository / ".agent-squad/state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            run_record_path = (
                repository
                / ".agent-squad/runs"
                / state["active_run_id"]
                / "run.json"
            )
            run_record = json.loads(
                run_record_path.read_text(encoding="utf-8")
            )
            foreign_repository_id = "0" * 64
            state["repository_id"] = foreign_repository_id
            run_record["repository"]["repository_id"] = foreign_repository_id
            state_path.write_text(json.dumps(state), encoding="utf-8")
            run_record_path.write_text(
                json.dumps(run_record),
                encoding="utf-8",
            )

            status = run_cli(repository, "status", data_home=data_home)

            self.assertNotEqual(status.returncode, 0)
            self.assertIn(
                "active run repository ID does not match the current worktree",
                status.stderr,
            )
            self.assertNotIn("Traceback", status.stderr)


if __name__ == "__main__":
    unittest.main()
