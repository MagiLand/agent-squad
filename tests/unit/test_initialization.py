from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest import mock

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad import initialization  # noqa: E402


IMPLEMENTATION_ROOT = Path("/agent-squad-tests/repository")
REVIEW_ROOT = Path("/agent-squad-tests/reviews")


class DefaultReviewWorktreeRootTests(unittest.TestCase):
    def test_resolution_error_is_actionable(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"XDG_DATA_HOME": "/agent-squad-tests/data"},
            ),
            mock.patch.object(
                Path,
                "resolve",
                autospec=True,
                side_effect=PermissionError("simulated inaccessible path"),
            ),
        ):
            with self.assertRaisesRegex(
                initialization.ConfigurationError,
                "default review worktree root cannot be resolved; check "
                "XDG_DATA_HOME and HOME: simulated inaccessible path",
            ):
                initialization.default_review_worktree_root()


class ReviewRootValidationTests(unittest.TestCase):
    def _assert_validation_error(
        self,
        error_type: type[Exception],
        pattern: str,
        *,
        review_root: Path = REVIEW_ROOT,
    ) -> None:
        configuration = initialization.default_configuration(review_root)
        with self.assertRaisesRegex(error_type, pattern):
            initialization._validate_review_worktree_root(
                configuration,
                IMPLEMENTATION_ROOT,
            )

    def test_existing_lineage_identity_rejects_case_variant(self) -> None:
        case_variant_root = Path("/agent-squad-tests/REPOSITORY")
        implementation_identity = (7, 11)

        def fake_identity(path: Path) -> tuple[int, int] | None:
            if path in (IMPLEMENTATION_ROOT, case_variant_root):
                return implementation_identity
            return None

        with mock.patch.object(
            initialization,
            "_existing_path_identity",
            side_effect=fake_identity,
        ):
            self._assert_validation_error(
                initialization.ConfigurationError,
                "must be outside the implementation worktree",
                review_root=case_variant_root / "reviews",
            )

    def test_resolution_os_error_is_actionable(self) -> None:
        def fake_resolve(path: Path, *, strict: bool) -> Path:
            if path == IMPLEMENTATION_ROOT:
                return path
            raise PermissionError("simulated inaccessible path")

        with mock.patch.object(
            Path,
            "resolve",
            autospec=True,
            side_effect=fake_resolve,
        ):
            self._assert_validation_error(
                initialization.ConfigurationError,
                "cannot be resolved: simulated inaccessible path",
            )

    def test_lineage_inspection_os_error_is_actionable(self) -> None:
        implementation_identity = (7, 11)

        def fake_identity(path: Path) -> tuple[int, int] | None:
            if path == IMPLEMENTATION_ROOT:
                return implementation_identity
            raise PermissionError("simulated inaccessible ancestor")

        with mock.patch.object(
            initialization,
            "_existing_path_identity",
            side_effect=fake_identity,
        ):
            self._assert_validation_error(
                initialization.ConfigurationError,
                "cannot be inspected: simulated inaccessible ancestor",
            )

    def test_missing_implementation_root_is_actionable(self) -> None:
        with mock.patch.object(
            initialization,
            "_existing_path_identity",
            return_value=None,
        ):
            self._assert_validation_error(
                initialization.RepositoryError,
                "implementation worktree disappeared",
            )

    def test_implementation_root_inspection_error_is_actionable(self) -> None:
        with mock.patch.object(
            initialization,
            "_existing_path_identity",
            side_effect=PermissionError("simulated inaccessible worktree"),
        ):
            self._assert_validation_error(
                initialization.RepositoryError,
                "cannot inspect the implementation worktree",
            )


if __name__ == "__main__":
    unittest.main()
