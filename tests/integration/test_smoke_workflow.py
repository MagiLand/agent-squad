"""The complete forge smoke also runs from exports without Git metadata."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests._support import PROJECT_ROOT
from tests.smoke_workflow import run_smoke


class SmokeTests(unittest.TestCase):
    def test_scripted_forge_scenario(self) -> None:
        result = run_smoke()
        self.assertTrue(result["ok"])
        self.assertIn("removed", result["cleanup"])

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
                text=True,
                capture_output=True,
                timeout=180,
                shell=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
