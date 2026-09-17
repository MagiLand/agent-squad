from __future__ import annotations

import tomllib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from email.parser import BytesParser
from importlib.util import find_spec

from tests._support import PROJECT_ROOT


class PackagingTests(unittest.TestCase):
    @unittest.skipUnless(find_spec("setuptools"), "build backend unavailable")
    def test_wheel_and_source_distribution_ship_both_skill_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copyfile(PROJECT_ROOT / name, root / name)
            shutil.copytree(PROJECT_ROOT / "src", root / "src")
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, "-c", "from setuptools.build_meta import "
                 "build_wheel, build_sdist; build_wheel('dist'); "
                 "build_sdist('dist')"],
                cwd=root, env=env, shell=False, capture_output=True,
                text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0,
                             result.stdout + result.stderr)
            wheel = next((root / "dist").glob("*.whl"))
            source = next((root / "dist").glob("*.tar.gz"))
            with (
                zipfile.ZipFile(wheel) as built,
                tarfile.open(source) as archive,
            ):
                metadata = next(name for name in built.namelist()
                                if name.endswith(".dist-info/METADATA"))
                self.assertEqual(BytesParser().parsebytes(
                    built.read(metadata))["Version"], "0.5.0")
                package_info = next(m for m in archive.getmembers()
                                    if m.name.count("/") == 1
                                    and m.name.endswith("/PKG-INFO"))
                self.assertEqual(BytesParser().parsebytes(
                    archive.extractfile(package_info).read())["Version"],
                    "0.5.0")
                for name in ("squad-implementer", "squad-reviewer"):
                    path = f"agent_squad/skills/{name}/SKILL.md"
                    expected = (PROJECT_ROOT / "src" / path).read_bytes()
                    self.assertEqual(built.read(path), expected)
                    member = next(
                        m for m in archive.getmembers()
                        if m.name.endswith("/src/" + path)
                    )
                    self.assertEqual(archive.extractfile(
                        member).read(), expected)

    def test_package_metadata_uses_runtime_version_source(self) -> None:
        pyproject = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = pyproject["project"]

        self.assertNotIn("version", project)
        self.assertEqual(project["dynamic"], ["version"])
        self.assertEqual(
            pyproject["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "agent_squad.__version__"},
        )
        self.assertEqual(project["dependencies"], [])


if __name__ == "__main__":
    unittest.main()
