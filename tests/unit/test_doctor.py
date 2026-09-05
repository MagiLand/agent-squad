from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.doctor import (  # noqa: E402
    _check_exclusions,
    _check_storage,
    _object_format,
    diagnose,
    git_output,
)
from agent_squad.initialization import (  # noqa: E402
    AgentSquadError,
    LOCAL_EXCLUDE_PATTERNS,
)
from agent_squad.storage import atomic_write  # noqa: E402


class DoctorPrimitiveTests(unittest.TestCase):
    def test_ineffective_exclusions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exclude = root / "exclude"
            exclude.write_text(
                "\n".join(LOCAL_EXCLUDE_PATTERNS), encoding="utf-8"
            )
            for result in (
                subprocess.CompletedProcess([], 1, "", ""),
                subprocess.CompletedProcess([], 0, "wrong/path", ""),
            ):
                with (
                    self.subTest(result=result),
                    mock.patch(
                        "agent_squad.doctor.run_git",
                        return_value=result,
                    ),
                ):
                    with self.assertRaisesRegex(
                        AgentSquadError,
                        "is not effectively excluded",
                    ):
                        _check_exclusions(root, exclude)

    def test_non_atomic_or_incorrect_replacement_is_rejected(self) -> None:
        for mode in ("in-place", "wrong-content"):

            def broken_write(path: Path, content: bytes, *, mode: int) -> None:
                if failure == "in-place":
                    path.write_bytes(content)
                else:
                    atomic_write(
                        path,
                        content if content == b"before" else b"wrong",
                        mode=mode,
                    )

            failure = mode
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with mock.patch(
                    "agent_squad.doctor.atomic_write", side_effect=broken_write
                ):
                    with self.assertRaisesRegex(
                        AgentSquadError,
                        "does not preserve atomic replacement",
                    ):
                        _check_storage(root)
                self.assertEqual(list(root.iterdir()), [])

    def test_lock_must_be_reacquirable_after_release(self) -> None:
        import fcntl

        actual_flock = fcntl.flock
        acquisitions = 0

        def cannot_reacquire(descriptor: int, operation: int) -> None:
            nonlocal acquisitions
            if operation & fcntl.LOCK_EX:
                acquisitions += 1
                if acquisitions == 3:
                    raise BlockingIOError("lock stayed busy after release")
            actual_flock(descriptor, operation)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "agent_squad.doctor.fcntl.flock", side_effect=cannot_reacquire
            ):
                with self.assertRaisesRegex(BlockingIOError, "stayed busy"):
                    _check_storage(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_lock_without_exclusion_is_rejected_and_probe_is_removed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch("agent_squad.doctor.fcntl.flock"):
                with self.assertRaisesRegex(
                    AgentSquadError, "does not enforce"
                ):
                    _check_storage(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_lock_permission_failure_preserves_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "lock"
            existing.write_text("owned by run")
            with mock.patch(
                "agent_squad.doctor.fcntl.flock",
                side_effect=PermissionError("flock denied"),
            ):
                with self.assertRaisesRegex(PermissionError, "flock denied"):
                    _check_storage(root)
            self.assertEqual(existing.read_text(), "owned by run")
            self.assertEqual(list(root.iterdir()), [existing])

    def test_atomic_replacement_failure_removes_only_owned_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch(
                "agent_squad.doctor.atomic_write",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    _check_storage(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_git_failure_and_unknown_object_format_are_actionable(
        self,
    ) -> None:
        process = subprocess.CompletedProcess([], 1, "", "cannot read objects")
        with mock.patch("agent_squad.doctor.run_git", return_value=process):
            with self.assertRaisesRegex(
                AgentSquadError, "git rev-parse.*cannot read objects"
            ):
                git_output(Path.cwd(), "rev-parse", "HEAD")
        with mock.patch(
            "agent_squad.doctor.git_output", return_value="future"
        ):
            with self.assertRaisesRegex(
                AgentSquadError, "unsupported Git object format"
            ):
                _object_format(Path.cwd())

    def test_non_repository_is_reported_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = diagnose(root)
            self.assertFalse(report.ok)
            self.assertEqual(report.diagnostics[0].check, "repository")
            self.assertEqual(list(root.iterdir()), [])

    def test_invalid_live_timeouts_do_not_probe_or_allocate(self) -> None:
        for timeout in (0, -1, float("inf"), float("nan")):
            with (
                self.subTest(timeout=timeout),
                mock.patch(
                    "agent_squad.doctor.discover_git_worktree"
                ) as discover,
            ):
                self.assertFalse(
                    diagnose(
                        Path.cwd(), live_reviewer=True, timeout_seconds=timeout
                    ).ok
                )
                discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
