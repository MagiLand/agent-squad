from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.doctor import (
    _check_storage,
    _object_format,
    diagnose,
    git_output,
)
from agent_squad.initialization import AgentSquadError


class DoctorPrimitiveTests(unittest.TestCase):
    def test_lock_without_exclusion_is_rejected_and_probe_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch("agent_squad.doctor.fcntl.flock"):
                with self.assertRaisesRegex(
                    AgentSquadError, "does not enforce"
                ):
                    _check_storage(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_lock_permission_failure_preserves_existing_files(self):
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

    def test_atomic_replacement_failure_removes_only_owned_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch(
                "agent_squad.doctor.atomic_write",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    _check_storage(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_git_failure_and_unknown_object_format_are_actionable(self):
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

    def test_non_repository_is_reported_without_creating_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = diagnose(root)
            self.assertFalse(report.ok)
            self.assertEqual(report.diagnostics[0].check, "repository")
            self.assertEqual(list(root.iterdir()), [])

    def test_invalid_live_timeouts_do_not_probe_or_allocate(self):
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
