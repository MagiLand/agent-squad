"""Keep the explicit CI layout complete and its expensive group off PRs."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests._support import PROJECT_ROOT


class CIGroupingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.groups = json.loads(
            (PROJECT_ROOT / "tests/ci_groups.json").read_text(encoding="utf-8")
        )

    def test_every_test_module_belongs_to_exactly_one_group(self) -> None:
        discovered = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (PROJECT_ROOT / "tests").rglob("test_*.py")
        }
        assigned = Counter()
        for group, modules in self.groups.items():
            self.assertIsInstance(modules, list, group)
            self.assertTrue(modules, group)
            assigned.update(modules)
        self.assertEqual(set(assigned), discovered)
        self.assertEqual(
            {module: count for module, count in assigned.items()
             if count != 1},
            {},
            "Every test module must be assigned exactly once",
        )

    def test_main_only_modules_stay_in_the_main_only_group(self) -> None:
        self.assertEqual(set(self.groups["main-only"]), {
            "tests/integration/test_source_export.py",
            "tests/unit/test_packaging.py",
        })

    def test_workflow_runs_each_group_with_the_required_event_policy(
        self,
    ) -> None:
        workflow = (PROJECT_ROOT / ".github/workflows/test.yml").read_text(
            encoding="utf-8")
        # This guard requires explicit jobs, an inline macOS group matrix,
        # and one-line commands,
        # rather than attempting to parse general YAML or GitHub expressions.
        jobs_text = workflow.split("\njobs:\n", 1)[1]
        blocks = re.split(r"^  ([\w-]+):\n", jobs_text, flags=re.MULTILINE)
        jobs = dict(zip(blocks[1::2], blocks[2::2]))
        self.assertEqual(len(jobs), len(blocks[1::2]), "Duplicate job names")
        self.assertEqual(set(self.groups), {
            "doctor", "reviewer", "forge-commands", "smoke", "rest",
            "main-only",
        })
        self.assertEqual(set(jobs), set(self.groups) | {"macos"})
        main_condition = (
            "    if: github.event_name == 'push' || "
            "github.event_name == 'schedule'"
        )
        for name, block in jobs.items():
            with self.subTest(job=name):
                conditions = re.findall(r"^    if:.*$", block, re.MULTILINE)
                self.assertEqual(
                    conditions,
                    [main_condition] if name in {"main-only", "macos"}
                    else [],
                )
                groups = re.findall(
                    r"^      - run: python scripts/run-test-group (.+)$",
                    block, re.MULTILINE,
                )
                self.assertEqual(
                    groups,
                    ["${{ matrix.group }}"] if name == "macos" else [name],
                )
                self.assertIn(
                    "    runs-on: " + (
                        "macos-latest" if name == "macos" else "ubuntu-26.04"
                    ) + "\n",
                    block,
                )
                self.assertRegex(block, r"(?m)^    timeout-minutes: [1-9]\d*$")
                self.assertIn(
                    "          python-version: '" + (
                        "3.12" if name == "macos" else "3.11"
                    ) + "'\n",
                    block,
                )
                self.assertIn(
                    "      - run: python -m pip install 'setuptools>=77' .\n",
                    block,
                )
        macos = jobs["macos"]
        matrices = re.findall(
            r"^    strategy:\n      fail-fast: false\n      matrix:\n"
            r"        group: \[([^\]\n]+)\]$",
            macos, re.MULTILINE,
        )
        self.assertEqual(len(matrices), 1, "Expected one macOS group matrix")
        self.assertEqual(
            Counter(group.strip() for group in matrices[0].split(",")),
            Counter(self.groups.keys()),
            "The macOS matrix must run every group exactly once",
        )
        self.assertIn("    timeout-minutes: 15\n", macos)
        self.assertIn("  pull_request:\n", workflow)
        self.assertIn("  push:\n    branches: [main]\n", workflow)
        self.assert_one_weekly_schedule(workflow)
        self.assertIn("permissions:\n  contents: read\n", workflow)

    def assert_one_weekly_schedule(self, workflow: str) -> None:
        # The scheduled run only catches changes outside the repository
        # (runner image, setuptools, Python patch), so once a week suffices.
        # The block ends at the next key indented by at most two spaces;
        # blank and comment lines inside it do not end it.
        schedule = re.search(
            r"(?m)^  schedule:\n((?:(?! {0,2}[^\s#-]).*\n)*)", workflow)
        self.assertIsNotNone(schedule, "Expected a schedule trigger")
        entries = re.findall(r"^[ \t]*- (.*)$", schedule[1], re.MULTILINE)
        self.assertEqual(len(entries), 1, "Expected one schedule entry")
        cron = re.fullmatch(r"cron: '([^']*)'", entries[0])
        self.assertIsNotNone(cron, "Expected a single-quoted cron entry")
        fields = cron[1].split()
        self.assertEqual(len(fields), 5, "Expected five cron fields")
        self.assertEqual(fields[2:4], ["*", "*"],
                         "Day-of-month and month must be '*'")
        self.assertRegex(fields[4], r"^(?:[0-6]|SUN|MON|TUE|WED|THU|FRI|SAT)$",
                         "Day-of-week must name a single day")

    def test_schedule_guard_accepts_only_one_single_day_entry(self) -> None:
        weekly = "    - cron: '17 5 * * 1'\n"
        daily = "    - cron: '17 5 * * *'\n"
        comment = "  # second schedule\n"
        accepted = {
            "numeric weekday": [weekly],
            "named weekday": ["    - cron: '17 5 * * MON'\n"],
            "blank line before the entry": ["\n", weekly],
            "comment before the entry": [comment, weekly],
            "blank line after the entry": [weekly, "\n"],
        }
        rejected = {
            "daily": [daily],
            "no entry": [],
            "adjacent second entry": [weekly, daily],
            "second entry after a blank line": [weekly, "\n", daily],
            "second entry after a whitespace line": [weekly, "   \n", daily],
            "second entry after a comment": [weekly, comment, daily],
            "second entry indented two spaces": [
                weekly, "  - cron: '17 5 * * *'\n"],
            "double-quoted entry": ["    - cron: \"17 5 * * 1\"\n"],
            "four fields": ["    - cron: '17 5 * 1'\n"],
            "six fields": ["    - cron: '17 5 * * 1 1'\n"],
            "restricted day of month": ["    - cron: '17 5 1 * 1'\n"],
            "restricted month": ["    - cron: '17 5 * 1 1'\n"],
            "weekday range": ["    - cron: '17 5 * * 1-5'\n"],
            "weekday list": ["    - cron: '17 5 * * 1,4'\n"],
            "weekday step": ["    - cron: '17 5 * * */2'\n"],
        }

        def workflow(lines: list[str]) -> str:
            return ("on:\n  push:\n    branches: [main]\n  schedule:\n"
                    + "".join(lines) + "permissions:\n  contents: read\n")

        for label, lines in accepted.items():
            with self.subTest(accepted=label):
                self.assert_one_weekly_schedule(workflow(lines))
        for label, lines in rejected.items():
            with self.subTest(rejected=label):
                with self.assertRaises(AssertionError):
                    self.assert_one_weekly_schedule(workflow(lines))


class GroupRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        shutil.copyfile(PROJECT_ROOT / "scripts/run-test-group",
                        self.root / "scripts/run-test-group")
        (self.root / "tests/__init__.py").touch()
        (self.root / "tests/ci_groups.json").write_text(json.dumps({
            "passing": ["tests/test_passing.py"],
            "failing": ["tests/test_failing.py"],
        }), encoding="utf-8")
        for name, outcome in (("passing", True), ("failing", False)):
            (self.root / f"tests/test_{name}.py").write_text(
                "import unittest\n\n"
                "class Fixture(unittest.TestCase):\n"
                "    def test_result(self):\n"
                f"        self.assertTrue({outcome!r})\n",
                encoding="utf-8",
            )

    def run_group(
        self, group: str, env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.root / "scripts/run-test-group"), group],
            # The script must locate its checkout even from another cwd.
            cwd=self.root.parent,
            env=env,
            text=True, capture_output=True, timeout=15, shell=False,
        )

    def test_runs_only_the_selected_group(self) -> None:
        result = self.run_group("passing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def test_prepends_checkout_src_to_pythonpath(self) -> None:
        src = self.root / "src"
        external = self.root / "external"
        for directory, value in ((src, "checkout"), (external, "external")):
            directory.mkdir()
            (directory / "runner_fixture.py").write_text(
                f"VALUE = {value!r}\n", encoding="utf-8")
        inherited = os.pathsep.join((str(external), str(self.root / "other")))
        for existing in (None, "", inherited):
            with self.subTest(pythonpath=existing):
                env = dict(os.environ, RUNNER_SENTINEL="preserved")
                env.pop("PYTHONPATH", None)
                if existing is not None:
                    env["PYTHONPATH"] = existing
                expected = str(src.resolve())
                if existing:
                    expected += os.pathsep + existing
                (self.root / "tests/test_passing.py").write_text(
                    "import os\n"
                    "import unittest\n"
                    "import runner_fixture\n\n"
                    "class Fixture(unittest.TestCase):\n"
                    "    def test_source_and_environment(self):\n"
                    "        self.assertEqual(runner_fixture.VALUE, "
                    "'checkout')\n"
                    "        self.assertEqual(os.environ['PYTHONPATH'], "
                    f"{expected!r})\n"
                    "        self.assertEqual(os.environ['RUNNER_SENTINEL'], "
                    "'preserved')\n",
                    encoding="utf-8",
                )
                result = self.run_group("passing", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Ran 1 test", result.stderr)

    def test_propagates_test_failure(self) -> None:
        result = self.run_group("failing")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("FAILED (failures=1)", result.stderr)

    def test_rejects_an_unknown_group(self) -> None:
        result = self.run_group("unknown")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("invalid choice", result.stderr)
        self.assertNotIn("Ran ", result.stderr)
