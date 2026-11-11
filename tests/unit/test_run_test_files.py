"""Run the per-file test runner against fixture test files."""

import contextlib
import errno
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
from types import ModuleType
import unittest
from unittest import mock

from tests._support import PROJECT_ROOT

RUNNER = "scripts/run-test-files"
# A bound after which a test fails instead of hanging. The tests never wait
# a fixed time: they coordinate with their fixtures through files and a FIFO.
DEADLINE = 120.0
FILE_LINE = re.compile(r"^(tests/\S+\.py: .*)$", re.MULTILINE)
SECONDS = re.compile(r" in \d+\.\d{3}s")
STATE_DIRECTORIES = ("ran", "running", "started", "most", "finished")

# Every fixture module records that it ran. hold() keeps a test running until
# `peers` fixture tests run at once, or until every fixture file has started,
# and records the most fixture tests it saw running at once.
FIXTURE_HEADER = '''\
import os
from pathlib import Path
import time
import unittest

STATE = Path(os.environ["RUNNER_FIXTURE_STATE"])
(STATE / "ran" / __name__).touch()


def hold(peers, files):
    running = STATE / "running" / __name__
    running.touch()
    (STATE / "started" / __name__).touch()
    most = 0
    deadline = time.monotonic() + 60
    try:
        while True:
            now = len(list(running.parent.iterdir()))
            most = max(most, now)
            if (now >= peers
                    or len(list((STATE / "started").iterdir())) >= files):
                break
            if time.monotonic() > deadline:
                raise AssertionError("no other fixture test started")
            time.sleep(0.01)
    finally:
        running.unlink()
        (STATE / "most" / __name__).write_text(str(most))
'''


def normalized(text: str) -> str:
    return SECONDS.sub(" in Xs", text)


def file_lines(output: str) -> list[str]:
    """The per-file lines, sorted, with their seconds replaced."""
    return sorted(normalized(line) for line in FILE_LINE.findall(output))


class FileRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "checkout"
        self.state = self.base / "state"
        self.tmp = self.base / "tmp"
        self.elsewhere = self.base / "elsewhere"
        for directory in (self.root / "scripts", self.tmp, self.elsewhere):
            directory.mkdir(parents=True)
        shutil.copyfile(PROJECT_ROOT / RUNNER, self.root / RUNNER)
        (self.root / "tests").mkdir()
        (self.root / "tests/__init__.py").touch()
        self.reset_state()

    def reset_state(self) -> None:
        shutil.rmtree(self.state, ignore_errors=True)
        for name in STATE_DIRECTORIES:
            (self.state / name).mkdir(parents=True)

    def write_module(self, relative: str, *tests: str, top: str = "") -> None:
        """Write a fixture module with one test method per body."""
        path = self.root / relative
        for package in path.relative_to(self.root / "tests").parents:
            directory = self.root / "tests" / package
            directory.mkdir(exist_ok=True)
            (directory / "__init__.py").touch()
        methods = "".join(
            f"    def test_{index}(self):\n"
            + textwrap.indent(body, " " * 8) + "\n"
            for index, body in enumerate(tests))
        path.write_text(
            FIXTURE_HEADER + top + "\n\nclass Fixture(unittest.TestCase):\n"
            + methods, encoding="utf-8")

    def environment(
        self, env: dict[str, str] | None = None, **extra: str,
    ) -> dict[str, str]:
        return {**(os.environ if env is None else env),
                "RUNNER_FIXTURE_STATE": str(self.state),
                # The runner keeps captured output under TMPDIR.
                "TMPDIR": str(self.tmp), **extra}

    def run_files(
        self, *arguments: str, env: dict[str, str] | None = None,
        **extra: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.root / RUNNER), *arguments],
            # The script must find its checkout from another cwd.
            cwd=self.elsewhere, env=self.environment(env, **extra),
            text=True, capture_output=True, timeout=DEADLINE, shell=False,
        )

    def ran(self) -> set[str]:
        return {path.name for path in (self.state / "ran").iterdir()}

    def checkout_files(self) -> set[str]:
        return {path.relative_to(self.root).as_posix()
                for path in self.root.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts}

    def assert_summary(self, output: str, ran: str, result: str) -> None:
        lines = output.splitlines()
        self.assertEqual(
            [lines[-4], normalized(lines[-3]), lines[-2], lines[-1]],
            ["-" * 70, ran, "", result], output)

    def test_all_files_pass_and_the_summary_adds_their_tests(self) -> None:
        self.write_module("tests/test_one.py", "pass")
        self.write_module("tests/test_two.py", "pass", "pass")
        self.write_module("tests/sub/test_three.py", "pass", "pass", "pass")
        # Neither name matches test_*.py, so neither file runs.
        for name in ("tests/helpers.py", "tests/sub/sample_test.py"):
            (self.root / name).write_text(
                "raise AssertionError('imported')\n", encoding="utf-8")
        before = self.checkout_files()

        result = self.run_files()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(file_lines(result.stdout), [
            "tests/sub/test_three.py: Ran 3 tests in Xs, OK",
            "tests/test_one.py: Ran 1 test in Xs, OK",
            "tests/test_two.py: Ran 2 tests in Xs, OK",
        ])
        self.assert_summary(result.stdout, "Ran 6 tests in Xs", "OK")
        self.assertEqual(self.ran(), {
            "tests.test_one", "tests.test_two", "tests.sub.test_three"})
        self.assertNotIn("Output of", result.stdout)
        self.assertEqual(self.checkout_files(), before)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_runs_files_of_the_same_name_in_different_folders(self) -> None:
        self.write_module("tests/unit/test_same.py", "pass")
        self.write_module("tests/integration/test_same.py", "pass", "pass")

        result = self.run_files()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(file_lines(result.stdout), [
            "tests/integration/test_same.py: Ran 2 tests in Xs, OK",
            "tests/unit/test_same.py: Ran 1 test in Xs, OK",
        ])
        self.assert_summary(result.stdout, "Ran 3 tests in Xs", "OK")
        self.assertEqual(self.ran(), {
            "tests.unit.test_same", "tests.integration.test_same"})

    def test_prints_only_the_output_of_a_failed_file(self) -> None:
        self.write_module(
            "tests/test_failing.py",
            "print('failing file output')\nself.fail('fixture failure')")
        self.write_module("tests/test_passing.py",
                          "print('passing file output')")
        self.write_module("tests/sub/test_passing.py", "pass")
        before = self.checkout_files()

        result = self.run_files("--jobs", "1")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.ran(), {
            "tests.test_failing", "tests.test_passing",
            "tests.sub.test_passing"})
        self.assertEqual(file_lines(result.stdout), [
            "tests/sub/test_passing.py: Ran 1 test in Xs, OK",
            "tests/test_failing.py: Ran 1 test in Xs, FAILED (failures=1)",
            "tests/test_passing.py: Ran 1 test in Xs, OK",
        ])
        header = f"{'#' * 20} Output of tests/test_failing.py {'#' * 20}\n"
        self.assertEqual(result.stdout.count(header), 1, result.stdout)
        printed = result.stdout.split(header, 1)[1].rsplit(
            f"{'-' * 70}\nRan 3 tests in ", 1)[0]
        alone = subprocess.run(
            [sys.executable, "-m", "unittest", "tests/test_failing.py"],
            cwd=self.root, env=self.environment(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=DEADLINE, shell=False)
        self.assertEqual(normalized(printed), normalized(alone.stdout))
        self.assertIn("failing file output", printed)
        self.assertIn("AssertionError: fixture failure", printed)
        self.assertNotIn("passing file output", result.stdout)
        self.assertEqual(result.stdout.count("Output of"), 1)
        self.assert_summary(
            result.stdout, "Ran 3 tests in Xs",
            "FAILED (1 of 3 files): tests/test_failing.py")
        self.assertEqual(self.checkout_files(), before)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_a_file_without_a_unittest_result_fails(self) -> None:
        self.write_module("tests/test_exits.py", "pass", top="os._exit(0)")
        self.write_module(
            "tests/test_crashes.py", "pass",
            top="print('partial output', flush=True)\nos._exit(3)")
        self.write_module("tests/test_passing.py", "pass")

        result = self.run_files()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(file_lines(result.stdout), [
            "tests/test_crashes.py: exit status 3 without a unittest result",
            "tests/test_exits.py: exit status 0 without a unittest result",
            "tests/test_passing.py: Ran 1 test in Xs, OK",
        ])
        self.assertIn(
            f"{'#' * 20} Output of tests/test_crashes.py {'#' * 20}\n"
            "partial output\n", result.stdout)
        self.assertIn(
            f"{'#' * 20} Output of tests/test_exits.py {'#' * 20}\n"
            "(no output)\n", result.stdout)
        self.assert_summary(
            result.stdout, "Ran 1 test in Xs",
            "FAILED (2 of 3 files): tests/test_crashes.py, "
            "tests/test_exits.py")

    def test_skipped_tests_show_in_the_file_line_and_the_summary(
        self,
    ) -> None:
        skip = "self.skipTest('fixture')"
        self.write_module("tests/test_plain.py", "pass")
        self.write_module("tests/test_skip_one.py", "pass", skip)
        self.write_module("tests/test_skip_two.py", skip, skip, "pass")

        result = self.run_files()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(file_lines(result.stdout), [
            "tests/test_plain.py: Ran 1 test in Xs, OK",
            "tests/test_skip_one.py: Ran 2 tests in Xs, OK (skipped=1)",
            "tests/test_skip_two.py: Ran 3 tests in Xs, OK (skipped=2)",
        ])
        self.assert_summary(
            result.stdout, "Ran 6 tests in Xs", "OK (skipped=3)")

    def test_runs_at_most_the_job_limit_at_once(self) -> None:
        files = 5
        for index in range(files):
            self.write_module(
                f"tests/test_hold_{index}.py",
                f"hold(int(os.environ['RUNNER_FIXTURE_PEERS']), {files})")
        reports = {}
        for jobs in (2, 1):
            with self.subTest(jobs=jobs):
                self.reset_state()
                result = self.run_files(
                    "--jobs", str(jobs), RUNNER_FIXTURE_PEERS=str(jobs))
                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)
                most = [int(path.read_text(encoding="utf-8"))
                        for path in (self.state / "most").iterdir()]
                self.assertEqual(len(most), files)
                # hold() makes the first tests wait for a peer, so the
                # limit is reached as well as never exceeded.
                self.assertEqual(max(most), jobs, most)
                lines = result.stdout.splitlines()
                reports[jobs] = (file_lines(result.stdout),
                                 normalized("\n".join(lines[-3:])))
        self.assertEqual(reports[1], reports[2])
        self.assertEqual(len(reports[1][0]), files)

    def test_rejects_a_job_limit_that_is_not_a_positive_integer(
        self,
    ) -> None:
        self.write_module("tests/test_one.py", "pass")
        for value in ("0", "-1", "two", "1.5", ""):
            with self.subTest(jobs=value):
                result = self.run_files("--jobs", value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("argument --jobs", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.ran(), set())

    def test_prepends_checkout_src_to_pythonpath(self) -> None:
        src = self.root / "src"
        external = self.base / "external"
        for directory, value in ((src, "checkout"), (external, "external")):
            directory.mkdir()
            (directory / "runner_fixture.py").write_text(
                f"VALUE = {value!r}\n", encoding="utf-8")
        inherited = os.pathsep.join((str(external), str(self.base / "other")))
        for existing in (None, "", inherited):
            with self.subTest(pythonpath=existing):
                env = dict(os.environ, RUNNER_SENTINEL="preserved")
                env.pop("PYTHONPATH", None)
                if existing is not None:
                    env["PYTHONPATH"] = existing
                expected = str(src)
                if existing:
                    expected += os.pathsep + existing
                self.write_module(
                    "tests/test_environment.py",
                    "import runner_fixture\n"
                    "self.assertEqual(runner_fixture.VALUE, 'checkout')\n"
                    "self.assertEqual(os.environ['PYTHONPATH'], "
                    f"{expected!r})\n"
                    "self.assertEqual(os.environ['RUNNER_SENTINEL'], "
                    "'preserved')")
                result = self.run_files(env=env)
                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)
                self.assertEqual(file_lines(result.stdout), [
                    "tests/test_environment.py: Ran 1 test in Xs, OK"])

    def test_a_signal_stops_the_running_tests_and_starts_no_more(
        self,
    ) -> None:
        # Each fixture test, and a child it starts, hold a FIFO open for
        # writing. Reading end of file proves that both have ended, and the
        # finished markers that they were stopped rather than waited for.
        body = (
            "import subprocess\n"
            "import sys\n"
            "fifo = os.open(os.environ['RUNNER_FIXTURE_FIFO'], os.O_WRONLY)\n"
            "subprocess.Popen([sys.executable, '-c', "
            "'import pathlib, sys, time; time.sleep(60); "
            "pathlib.Path(sys.argv[1]).touch()', "
            "str(STATE / 'finished' / (__name__ + '.child'))], "
            "pass_fds=(fifo,))\n"
            "os.write(fifo, (__name__ + '\\n').encode())\n"
            "time.sleep(60)\n"
            "(STATE / 'finished' / __name__).touch()")
        self.write_module("tests/test_hold_a.py", body)
        self.write_module("tests/test_hold_b.py", body)
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum.name):
                self.reset_state()
                fifo = self.state / "fifo"
                os.mkfifo(fifo)
                reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
                self.addCleanup(os.close, reader)
                # Until a fixture writes, this keeps the FIFO from
                # reporting end of file.
                keeper = os.open(fifo, os.O_WRONLY)
                try:
                    runner = subprocess.Popen(
                        [sys.executable, str(self.root / RUNNER),
                         "--jobs", "1"],
                        cwd=self.elsewhere,
                        env=self.environment(RUNNER_FIXTURE_FIFO=str(fifo)),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, shell=False)
                    self.addCleanup(self.stop_runner, runner)
                    readable, _, _ = select.select(
                        [reader], [], [], DEADLINE)
                    self.assertTrue(readable, "No fixture test started")
                    started = os.read(reader, 4096).decode().splitlines()
                finally:
                    os.close(keeper)

                runner.send_signal(signum)
                stdout, stderr = runner.communicate(timeout=DEADLINE)

                self.assertEqual(runner.returncode, -signum, stderr)
                self.assertIn(
                    f"Stopped by {signum.name}; test files stopped: 1, "
                    "not started: 1", stderr)
                self.assertEqual(len(started), 1, started)
                # The runner waits for the test process but not for its
                # child, which ends on its own after the group's SIGTERM.
                rest = b""
                while True:
                    readable, _, _ = select.select(
                        [reader], [], [], DEADLINE)
                    self.assertTrue(
                        readable, "A stopped test process is still running")
                    chunk = os.read(reader, 4096)
                    if not chunk:
                        break
                    rest += chunk
                self.assertEqual(rest, b"", "Another test file started")
                self.assertEqual(
                    list((self.state / "finished").iterdir()), [])
                self.assertNotIn("Ran ", stdout)
                self.assertEqual(list(self.tmp.iterdir()), [])

    def test_fails_when_there_is_no_test_file(self) -> None:
        result = self.run_files()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("No test_*.py file under", result.stderr)
        self.assertEqual(result.stdout, "")

    def load_runner(self) -> ModuleType:
        """Import the copied script, which has no .py suffix."""
        loader = importlib.machinery.SourceFileLoader(
            "run_test_files", str(self.root / RUNNER))
        module = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        return module

    def test_starts_files_up_to_the_job_limit_and_never_more(self) -> None:
        # In-process, a stand-in for Popen sees every start, which a test
        # file cannot: a process may start without having run any code yet.
        for index in range(5):
            (self.root / f"tests/test_{index}.py").touch()
        runner = self.load_runner()
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))
        for jobs in (1, 2, 3):
            live: list[object] = []
            most: list[int] = []

            class Process:
                def __init__(self, arguments: list[str], **options) -> None:
                    options["stdout"].write(b"Ran 1 test in 0.001s\n\nOK\n")
                    live.append(self)
                    most.append(len(live))
                    self.polls, self.returncode, self.pid = 3, None, 0

                def poll(self) -> int | None:
                    self.polls -= 1
                    if self.polls == 0:
                        self.returncode = 0
                        live.remove(self)
                    return self.returncode

            arguments = [RUNNER, f"--jobs={jobs}"]
            with self.subTest(jobs=jobs), \
                    mock.patch.object(runner.subprocess, "Popen", Process), \
                    mock.patch.object(runner.time, "sleep"), \
                    mock.patch.object(sys, "argv", arguments), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(runner.main(), 0, output.getvalue())
                self.assertEqual(len(most), 5)
                self.assertEqual(max(most), jobs, most)

    def test_stopping_tolerates_a_group_whose_leader_has_exited(self) -> None:
        # macOS answers EPERM to killpg when the group holds only its exited,
        # not yet reaped leader.
        runner = self.load_runner()
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"], start_new_session=True)
        refused = PermissionError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(runner.os, "killpg",
                               side_effect=refused) as killpg:
            runner.stop_processes({process: ("tests/test_x.py", Path())})
        killpg.assert_any_call(process.pid, signal.SIGTERM)
        self.assertEqual(process.returncode, 0)

    @staticmethod
    def stop_runner(runner: subprocess.Popen) -> None:
        if runner.poll() is None:
            runner.kill()
            runner.communicate()
