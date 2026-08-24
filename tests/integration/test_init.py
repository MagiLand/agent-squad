from __future__ import annotations

from collections.abc import Callable
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


def _set_review_root(config_path: Path, review_root: Path) -> None:
    configuration = json.loads(config_path.read_text(encoding="utf-8"))
    configuration["review_worktree_root"] = str(review_root)
    config_path.write_text(
        json.dumps(configuration),
        encoding="utf-8",
    )


def _failing_exclude_write(
    before_failure: Callable[[], None] | None = None,
) -> Callable[..., None]:
    real_atomic_write = initialization._atomic_write

    def write(path: Path, content: bytes, *, mode: int) -> None:
        if path.name == "exclude":
            if before_failure is not None:
                before_failure()
            raise PermissionError("simulated read-only Git metadata")
        real_atomic_write(path, content, mode=mode)

    return write


class InitCommandTests(unittest.TestCase):
    def _assert_reinit_rejects_review_root(
        self,
        repository: Path,
        *,
        data_home: Path,
        review_root: Path,
        expected_message: str,
    ) -> None:
        first = run_cli(repository, "init", data_home=data_home)
        self.assertEqual(first.returncode, 0, first.stderr)
        config_path = repository / ".agent-squad/config.json"
        _set_review_root(config_path, review_root)
        original_config = config_path.read_bytes()
        exclude_path = repository / ".git/info/exclude"
        original_exclude = exclude_path.read_bytes()

        result = run_cli(repository, "init", data_home=data_home)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected_message, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(config_path.read_bytes(), original_config)
        self.assertEqual(exclude_path.read_bytes(), original_exclude)

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
            self.assertIn(
                "Git could not confirm that the current directory",
                result.stderr,
            )
            self.assertIn("non-bare Git checkout", result.stderr)
            self.assertFalse((temporary_root / ".agent-squad").exists())

    def test_bare_repository_is_rejected_as_not_a_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository.git"
            run(
                ["git", "init", "--bare", str(repository)],
                cwd=temporary_root,
            )

            result = run_cli(
                repository,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not inside a Git worktree", result.stderr)
            self.assertFalse((repository / ".agent-squad").exists())

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

    def test_symlinked_control_root_preserves_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            external_root = temporary_root / "external-control"
            external_root.mkdir()
            control_root = repository / ".agent-squad"
            control_root.symlink_to(
                external_root,
                target_is_directory=True,
            )
            exclude_path = repository / ".git/info/exclude"
            original_exclude = exclude_path.read_bytes()

            result = run_cli(
                repository,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symbolic link", result.stderr)
            self.assertEqual(exclude_path.read_bytes(), original_exclude)
            self.assertEqual(list(external_root.iterdir()), [])

    def test_symlinked_configuration_preserves_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            control_root = repository / ".agent-squad"
            control_root.mkdir()
            external_config = temporary_root / "external-config.json"
            external_config.write_text("external\n", encoding="utf-8")
            config_path = control_root / "config.json"
            config_path.symlink_to(external_config)
            exclude_path = repository / ".git/info/exclude"
            original_exclude = exclude_path.read_bytes()

            result = run_cli(
                repository,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symbolic link", result.stderr)
            self.assertEqual(exclude_path.read_bytes(), original_exclude)
            self.assertEqual(
                external_config.read_text(encoding="utf-8"),
                "external\n",
            )

    def test_configuration_directory_preserves_excludes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            config_path = repository / ".agent-squad/config.json"
            config_path.mkdir(parents=True)
            exclude_path = repository / ".git/info/exclude"
            original_exclude = exclude_path.read_bytes()

            result = run_cli(
                repository,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be a regular file", result.stderr)
            self.assertEqual(exclude_path.read_bytes(), original_exclude)

    def test_symlinked_local_exclude_prevents_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            exclude_path = repository / ".git/info/exclude"
            original_exclude = exclude_path.read_bytes()
            external_exclude = temporary_root / "external-exclude"
            external_exclude.write_bytes(original_exclude)
            exclude_path.unlink()
            exclude_path.symlink_to(external_exclude)

            result = run_cli(
                repository,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symbolic link", result.stderr)
            self.assertFalse((repository / ".agent-squad").exists())
            self.assertEqual(
                external_exclude.read_bytes(),
                original_exclude,
            )

    def test_local_exclude_directory_prevents_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            exclude_path = repository / ".git/info/exclude"
            exclude_path.unlink()
            exclude_path.mkdir()

            result = run_cli(
                repository,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be a regular file", result.stderr)
            self.assertFalse((repository / ".agent-squad").exists())

    def test_missing_local_exclude_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            exclude_path = repository / ".git/info/exclude"
            exclude_path.unlink()

            result = run_cli(
                repository,
                "init",
                data_home=temporary_root / "data",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                exclude_path.read_text(encoding="utf-8"),
                ".agent-squad/\n.agent-squad-review/\n",
            )

    def test_local_exclude_read_error_prevents_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            exclude_path = repository / ".git/info/exclude"
            real_read_bytes = Path.read_bytes

            def fail_exclude_read(path: Path) -> bytes:
                if path.name == "exclude":
                    raise PermissionError("simulated unreadable exclude")
                return real_read_bytes(path)

            with mock.patch.object(
                Path,
                "read_bytes",
                autospec=True,
                side_effect=fail_exclude_read,
            ):
                with self.assertRaisesRegex(
                    initialization.InitializationError,
                    "cannot read Git local exclude",
                ):
                    initialization.initialize_repository(
                        repository,
                        review_worktree_root=temporary_root / "reviews",
                    )

            self.assertFalse((repository / ".agent-squad").exists())

    def test_invalid_git_info_paths_prevent_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)

            for kind in ("symlink", "file"):
                with self.subTest(kind=kind):
                    repository = temporary_root / f"repository-{kind}"
                    initialize_git_repository(repository)
                    info_path = repository / ".git/info"
                    (info_path / "exclude").unlink()
                    info_path.rmdir()
                    if kind == "symlink":
                        external_info = temporary_root / "external-info"
                        external_info.mkdir(exist_ok=True)
                        info_path.symlink_to(
                            external_info,
                            target_is_directory=True,
                        )
                    else:
                        info_path.write_text(
                            "not a directory\n",
                            encoding="utf-8",
                        )

                    result = run_cli(
                        repository,
                        "init",
                        data_home=temporary_root / "data",
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        "Git metadata path must be a directory",
                        result.stderr,
                    )
                    self.assertFalse(
                        (repository / ".agent-squad").exists()
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

            inside_root = repository / "reviews"
            inside_root.mkdir()
            linked_root = temporary_root / "linked-reviews"
            linked_root.symlink_to(inside_root, target_is_directory=True)
            self._assert_reinit_rejects_review_root(
                repository,
                data_home=data_home,
                review_root=linked_root,
                expected_message=(
                    "must be outside the implementation worktree"
                ),
            )

    def test_case_variant_review_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            case_variant = temporary_root / "REPOSITORY"
            if not case_variant.exists():
                self.skipTest("temporary volume is case-sensitive")
            data_home = temporary_root / "data"
            self._assert_reinit_rejects_review_root(
                repository,
                data_home=data_home,
                review_root=case_variant / "reviews",
                expected_message=(
                    "must be outside the implementation worktree"
                ),
            )

    def test_symlink_loop_review_root_has_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            data_home = temporary_root / "data"

            loop = temporary_root / "review-loop"
            loop.symlink_to(loop, target_is_directory=True)
            self._assert_reinit_rejects_review_root(
                repository,
                data_home=data_home,
                review_root=loop / "reviews",
                expected_message="cannot be resolved",
            )

    def test_default_review_root_symlink_loop_has_actionable_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            loop = temporary_root / "data-loop"
            loop.symlink_to(loop, target_is_directory=True)
            exclude_path = repository / ".git/info/exclude"
            original_exclude = exclude_path.read_bytes()

            result = run_cli(repository, "init", data_home=loop)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "default review worktree root cannot be resolved",
                result.stderr,
            )
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((repository / ".agent-squad").exists())
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

            with mock.patch.object(
                initialization,
                "_atomic_write",
                side_effect=_failing_exclude_write(),
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

    def test_rollback_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository = temporary_root / "repository"
            initialize_git_repository(repository)
            exclude_path = repository / ".git/info/exclude"
            original_exclude = exclude_path.read_bytes()
            obstruction = repository / ".agent-squad/obstruction"

            def obstruct_rollback() -> None:
                obstruction.write_text("keep root\n", encoding="utf-8")

            with mock.patch.object(
                initialization,
                "_atomic_write",
                side_effect=_failing_exclude_write(obstruct_rollback),
            ):
                with self.assertRaisesRegex(
                    initialization.InitializationError,
                    "Rollback also encountered: could not remove newly "
                    "created",
                ):
                    initialization.initialize_repository(
                        repository,
                        review_worktree_root=temporary_root / "reviews",
                    )

            self.assertTrue(obstruction.is_file())
            self.assertFalse(
                (repository / ".agent-squad/config.json").exists()
            )
            self.assertEqual(exclude_path.read_bytes(), original_exclude)


if __name__ == "__main__":
    unittest.main()
