from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests._support import PROJECT_ROOT, run, seed_git_repository
from tests import smoke_workflow


class SmokeWorkflowTests(unittest.TestCase):
    def test_workflow_ignores_inherited_git_and_fake_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unrelated = root / "unrelated"
            seed_git_repository(unrelated)
            sentinel = unrelated / "untracked.txt"
            sentinel.write_text("preserve me\n", encoding="utf-8")

            def snapshot() -> dict[str, str]:
                return {
                    str(path.relative_to(unrelated)):
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in unrelated.rglob("*") if path.is_file()
                }

            before = snapshot()
            environment = dict(os.environ)
            environment.update(
                GIT_DIR=str(unrelated / ".git"),
                GIT_WORK_TREE=str(unrelated),
                GIT_INDEX_FILE=str(unrelated / ".git/index"),
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="core.hooksPath",
                GIT_CONFIG_VALUE_0=str(unrelated),
                FAKE_HERDR_FAIL_START="1",
                FAKE_HERDR_STATE_DIR=str(unrelated),
            )
            result = run(
                [sys.executable, str(PROJECT_ROOT / "scripts/run-smoke-tests"),
                 "--json"], cwd=unrelated, env=environment, check=False,
            )
            self.assertEqual(result.returncode, 0,
                             result.stdout + result.stderr)
            evidence = json.loads(result.stdout)
            self.assertEqual(evidence["result"], "passed")
            self.assertEqual(evidence["real_model_calls"], 0)
            self.assertEqual(evidence["rounds"], 2)
            self.assertTrue(evidence["response_replaced"])
            self.assertTrue(evidence["resolution_propagated"])
            self.assertTrue(evidence["lost_notification_discovered"])
            self.assertTrue(evidence["completed"])
            self.assertNotEqual(evidence["first_head_oid"],
                                evidence["approved_head_oid"])
            self.assertEqual(evidence["retained_resources"], [])
            self.assertFalse(Path(evidence["temporary_root"]).exists())
            self.assertEqual(snapshot(), before)

    def test_failure_removes_owned_read_only_archives(self) -> None:
        allocated: list[Path] = []

        def fail(root: Path) -> None:
            allocated.append(root)
            archive = root / "immutable-archive"
            archive.mkdir()
            (archive / "evidence.txt").write_text("evidence", encoding="utf-8")
            archive.chmod(0o500)
            raise AssertionError("injected scenario failure")

        output = io.StringIO()
        with (
            mock.patch.object(smoke_workflow, "exercise", side_effect=fail),
            mock.patch.object(sys, "argv", ["run-smoke-tests"]),
            contextlib.redirect_stderr(output),
        ):
            self.assertEqual(smoke_workflow.main(), 1)
        self.assertIn("injected scenario failure", output.getvalue())
        self.assertEqual(len(allocated), 1)
        self.assertFalse(allocated[0].exists())
