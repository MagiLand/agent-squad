"""The complete forge smoke also runs from exports without Git metadata."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests._support import PROJECT_ROOT
from tests.smoke_workflow import run_smoke
from tests.forge_support import ForgeFixture


class SmokeTests(unittest.TestCase):
    def test_scripted_forge_scenario(self) -> None:
        result = run_smoke()
        self.assertTrue(result["ok"])
        self.assertIn("removed", result["cleanup"])
        self.assertEqual([s["step"] for s in result["steps"]],
                         list(range(1, 13)))

    def test_failed_command_removes_the_owned_temporary_root(self) -> None:
        roots = []

        def fail_command(fixture, *args, **kwargs):
            roots.append(fixture.root)
            raise RuntimeError("injected CLI failure")

        with patch.object(ForgeFixture, "cli", fail_command):
            with self.assertRaisesRegex(RuntimeError, "injected CLI failure"):
                run_smoke()
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())

    def test_failed_cleanup_reports_the_retained_root(self) -> None:
        fixture = ForgeFixture()
        try:
            with patch.object(fixture.temporary, "cleanup",
                              side_effect=OSError("injected removal failure")):
                with self.assertRaises(RuntimeError) as caught:
                    fixture.close()
            self.assertIn(str(fixture.root), str(caught.exception))
            self.assertIn("retained", str(caught.exception))
            self.assertTrue(fixture.root.exists())
        finally:
            fixture.close()

    def test_entry_point_from_source_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("src", "tests", "scripts"):
                shutil.copytree(
                    PROJECT_ROOT / name,
                    root / name,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            self.assertFalse((root / ".git").exists())
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/run-smoke-tests"),
                    "--json",
                ],
                cwd=root.parent,
                # Keep the runner's fixtures inside this owned directory,
                # including when a timeout prevents its normal cleanup.
                env={**os.environ, "TMPDIR": str(root)},
                text=True,
                capture_output=True,
                # The twelve-step run exceeded three minutes on a busy host.
                timeout=300,
                shell=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(result.stdout)
            self.assertTrue(evidence["ok"])
            self.assertEqual([s["step"] for s in evidence["steps"]],
                             list(range(1, 13)))
