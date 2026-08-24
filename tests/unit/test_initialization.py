from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import initialization  # noqa: E402


class ReviewRootValidationTests(unittest.TestCase):
    def test_existing_lineage_identity_rejects_case_variant(self) -> None:
        implementation_root = Path("/agent-squad-tests/repository")
        case_variant_root = Path("/agent-squad-tests/REPOSITORY")
        configuration = initialization.default_configuration(
            case_variant_root / "reviews"
        )
        implementation_identity = (7, 11)

        def fake_identity(path: Path) -> tuple[int, int] | None:
            if path in (implementation_root, case_variant_root):
                return implementation_identity
            return None

        with mock.patch.object(
            initialization,
            "_existing_path_identity",
            side_effect=fake_identity,
        ):
            with self.assertRaisesRegex(
                initialization.ConfigurationError,
                "must be outside the implementation worktree",
            ):
                initialization._validate_review_worktree_root(
                    configuration,
                    implementation_root,
                )

    def test_resolution_os_error_is_actionable(self) -> None:
        implementation_root = Path("/agent-squad-tests/repository")
        review_root = Path("/agent-squad-tests/reviews")
        configuration = initialization.default_configuration(review_root)

        def fake_resolve(path: Path, *, strict: bool) -> Path:
            if path == implementation_root:
                return path
            raise PermissionError("simulated inaccessible path")

        with mock.patch.object(
            Path,
            "resolve",
            autospec=True,
            side_effect=fake_resolve,
        ):
            with self.assertRaisesRegex(
                initialization.ConfigurationError,
                "cannot be resolved: simulated inaccessible path",
            ):
                initialization._validate_review_worktree_root(
                    configuration,
                    implementation_root,
                )

    def test_lineage_inspection_os_error_is_actionable(self) -> None:
        implementation_root = Path("/agent-squad-tests/repository")
        review_root = Path("/agent-squad-tests/reviews")
        configuration = initialization.default_configuration(review_root)
        implementation_identity = (7, 11)

        def fake_identity(path: Path) -> tuple[int, int] | None:
            if path == implementation_root:
                return implementation_identity
            raise PermissionError("simulated inaccessible ancestor")

        with mock.patch.object(
            initialization,
            "_existing_path_identity",
            side_effect=fake_identity,
        ):
            with self.assertRaisesRegex(
                initialization.ConfigurationError,
                "cannot be inspected: simulated inaccessible ancestor",
            ):
                initialization._validate_review_worktree_root(
                    configuration,
                    implementation_root,
                )

    def test_missing_implementation_root_is_actionable(self) -> None:
        implementation_root = Path("/agent-squad-tests/repository")
        configuration = initialization.default_configuration(
            Path("/agent-squad-tests/reviews")
        )

        with mock.patch.object(
            initialization,
            "_existing_path_identity",
            return_value=None,
        ):
            with self.assertRaisesRegex(
                initialization.RepositoryError,
                "implementation worktree disappeared",
            ):
                initialization._validate_review_worktree_root(
                    configuration,
                    implementation_root,
                )

    def test_implementation_root_inspection_error_is_actionable(self) -> None:
        implementation_root = Path("/agent-squad-tests/repository")
        configuration = initialization.default_configuration(
            Path("/agent-squad-tests/reviews")
        )

        with mock.patch.object(
            initialization,
            "_existing_path_identity",
            side_effect=PermissionError("simulated inaccessible worktree"),
        ):
            with self.assertRaisesRegex(
                initialization.RepositoryError,
                "cannot inspect the implementation worktree",
            ):
                initialization._validate_review_worktree_root(
                    configuration,
                    implementation_root,
                )


if __name__ == "__main__":
    unittest.main()
