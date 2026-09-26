"""The complete forge smoke runs from exports without Git metadata."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests._support import PROJECT_ROOT


class SmokeTests(unittest.TestCase):
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
                             list(range(1, 14)))
