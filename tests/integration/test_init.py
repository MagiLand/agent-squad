from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._support import (
    add_src_to_path,
    initialize_git_repository,
    run,
    run_cli,
)


add_src_to_path()

from agent_squad import initialization  # noqa: E402


class InitCommandTests(unittest.TestCase):
    def test_help_is_available_without_a_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)

            result = run_cli(
                temporary_root,
                "--help",
                data_home=temporary_root / "data",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: agent-squad", result.stdout)
        self.assertIn("init", result.stdout)

    def test_init_creates_config_and_local_exclusions_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            control_root = repository / ".agent-squad"
            control_root.mkdir()
            unrelated = control_root / "developer-note.txt"
            unrelated.write_text("keep me\n", encoding="utf-8")
            exclude_path = repository / ".git/info/exclude"
            exclude_path.write_bytes(
                b"# existing local rules\nprivate-output/\n"
            )

            result = run_cli(
                repository,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            configuration = json.loads(
                (control_root / "config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(configuration["schema_version"], 1)
            self.assertEqual(
                configuration["review_worktree_root"],
                str((temporary_root / "data/agent-squad/worktrees").resolve()),
            )
            self.assertEqual(
                unrelated.read_text(encoding="utf-8"),
                "keep me\n",
            )
            self.assertFalse((control_root / "state.json").exists())
            self.assertFalse((control_root / "lock").exists())
            self.assertFalse((control_root / "task.md").exists())
            self.assertFalse((repository / ".agent-squad-review").exists())
            self.assertFalse((repository / ".gitignore").exists())

            exclude = exclude_path.read_text(encoding="utf-8")
            self.assertTrue(
                exclude.startswith("# existing local rules\nprivate-output/\n")
            )
            self.assertIn(".agent-squad/\n", exclude)
            self.assertIn(".agent-squad-review/\n", exclude)
            self.assertEqual(
                run(["git", "status", "--short"], cwd=repository).stdout,
                "",
            )

    def test_repeated_init_preserves_config_and_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            data_home = temporary_root / "data"

            first = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(first.returncode, 0, first.stderr)
            config_path = repository / ".agent-squad/config.json"
            exclude_path = repository / ".git/info/exclude"
            original_config = config_path.read_bytes()
            original_exclude = exclude_path.read_bytes()

            second = run_cli(repository, "init", data_home=data_home)

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(config_path.read_bytes(), original_config)
            self.assertEqual(exclude_path.read_bytes(), original_exclude)
            self.assertEqual(original_exclude.count(b".agent-squad/\n"), 1)
            self.assertEqual(
                original_exclude.count(b".agent-squad-review/\n"),
                1,
            )

    def test_existing_valid_configuration_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            data_home = temporary_root / "data"
            first = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(first.returncode, 0, first.stderr)
            config_path = repository / ".agent-squad/config.json"
            configuration = json.loads(config_path.read_text(encoding="utf-8"))
            configuration["base_ref"] = "refs/heads/trunk"
            custom_bytes = json.dumps(
                configuration,
                separators=(",", ":"),
            ).encode()
            config_path.write_bytes(custom_bytes)

            result = run_cli(repository, "init", data_home=data_home)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config_path.read_bytes(), custom_bytes)

    def test_invalid_existing_config_causes_no_other_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            control_root = repository / ".agent-squad"
            control_root.mkdir()
            config_path = control_root / "config.json"
            invalid_config = b'{"schema_version": 1,\n'
            config_path.write_bytes(invalid_config)
            exclude_path = repository / ".git/info/exclude"
            original_exclude = exclude_path.read_bytes()

            result = run_cli(
                repository,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("config.json contains invalid JSON", result.stderr)
            self.assertEqual(config_path.read_bytes(), invalid_config)
            self.assertEqual(exclude_path.read_bytes(), original_exclude)
            self.assertEqual(list(control_root.iterdir()), [config_path])

    def test_non_git_directory_has_no_partial_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)

            result = run_cli(
                temporary_root,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not inside a Git worktree", result.stderr)
            self.assertFalse((temporary_root / ".agent-squad").exists())

    def test_conflicting_control_root_preserves_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            control_root = repository / ".agent-squad"
            control_root.write_text("not a directory\n", encoding="utf-8")
            exclude_path = repository / ".git/info/exclude"
            original_exclude = exclude_path.read_bytes()

            result = run_cli(
                repository,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be a directory", result.stderr)
            self.assertEqual(exclude_path.read_bytes(), original_exclude)
            self.assertEqual(
                control_root.read_text(encoding="utf-8"),
                "not a directory\n",
            )

    def test_inner_review_root_is_rejected_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)

            for suffix in (Path(), Path("reviews")):
                with self.subTest(suffix=suffix):
                    repository = temporary_root / (
                        f"repository-{len(suffix.parts)}"
                    )
                    initialize_git_repository(repository)
                    exclude_path = repository / ".git/info/exclude"
                    original_exclude = exclude_path.read_bytes()
                    review_root = repository / suffix

                    with self.assertRaisesRegex(
                        initialization.ConfigurationError,
                        "must be outside the implementation worktree",
                    ):
                        initialization.initialize_repository(
                            repository,
                            review_worktree_root=review_root,
                        )

                    self.assertFalse((repository / ".agent-squad").exists())
                    self.assertEqual(
                        exclude_path.read_bytes(),
                        original_exclude,
                    )

    def test_existing_symlinked_review_root_inside_worktree_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            data_home = temporary_root / "data"
            first = run_cli(repository, "init", data_home=data_home)
            self.assertEqual(first.returncode, 0, first.stderr)

            inside_root = repository / "reviews"
            inside_root.mkdir()
            linked_root = temporary_root / "linked-reviews"
            linked_root.symlink_to(inside_root, target_is_directory=True)
            config_path = repository / ".agent-squad/config.json"
            configuration = json.loads(config_path.read_text(encoding="utf-8"))
            configuration["review_worktree_root"] = str(linked_root)
            config_path.write_text(
                json.dumps(configuration),
                encoding="utf-8",
            )
            original_config = config_path.read_bytes()
            exclude_path = repository / ".git/info/exclude"
            original_exclude = exclude_path.read_bytes()

            result = run_cli(repository, "init", data_home=data_home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "must be outside the implementation worktree",
                result.stderr,
            )
            self.assertEqual(config_path.read_bytes(), original_config)
            self.assertEqual(exclude_path.read_bytes(), original_exclude)

    def test_linked_worktree_updates_common_git_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            primary = temporary_root / "primary"
            linked = temporary_root / "linked"
            initialize_git_repository(primary)
            run(
                ["git", "config", "user.name", "Agent Squad Tests"],
                cwd=primary,
            )
            run(
                ["git", "config", "user.email", "agent-squad@example.invalid"],
                cwd=primary,
            )
            (primary / "README.md").write_text("fixture\n", encoding="utf-8")
            run(["git", "add", "README.md"], cwd=primary)
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "--no-verify",
                    "-m",
                    "test: seed repository",
                ],
                cwd=primary,
            )
            run(
                ["git", "worktree", "add", "--detach", str(linked), "HEAD"],
                cwd=primary,
            )
            nested = linked / "nested"
            nested.mkdir()

            result = run_cli(
                nested,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((linked / ".agent-squad/config.json").is_file())
            self.assertFalse((primary / ".agent-squad").exists())
            common_exclude = primary / ".git/info/exclude"
            exclude = common_exclude.read_text(encoding="utf-8")
            self.assertIn(".agent-squad/\n", exclude)
            self.assertIn(".agent-squad-review/\n", exclude)
            status = run(["git", "status", "--short"], cwd=linked)
            self.assertEqual(status.stdout, "")

    def test_exclude_write_failure_rolls_back_new_control_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            exclude_path = repository / ".git/info/exclude"
            original_exclude = exclude_path.read_bytes()
            real_atomic_write = initialization._atomic_write

            def fail_exclude_write(
                path: Path,
                content: bytes,
                *,
                mode: int,
            ) -> None:
                if path.name == "exclude":
                    raise PermissionError("simulated read-only Git metadata")
                real_atomic_write(path, content, mode=mode)

            with mock.patch.object(
                initialization,
                "_atomic_write",
                side_effect=fail_exclude_write,
            ):
                with self.assertRaisesRegex(
                    initialization.InitializationError,
                    "could not complete initialization",
                ):
                    initialization.initialize_repository(
                        repository,
                        review_worktree_root=temporary_root / "reviews",
                    )

            self.assertFalse((repository / ".agent-squad").exists())
            self.assertEqual(exclude_path.read_bytes(), original_exclude)


if __name__ == "__main__":
    unittest.main()
