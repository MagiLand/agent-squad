from __future__ import annotations

import tomllib
import unittest

from tests._support import PROJECT_ROOT


class PackagingTests(unittest.TestCase):
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
