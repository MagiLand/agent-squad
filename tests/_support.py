"""Shared helpers for deterministic repository tests."""

from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"


def add_src_to_path() -> None:
    """Make the src-layout package importable without installing it."""

    source = str(SRC_ROOT)
    if source not in sys.path:
        sys.path.insert(0, source)


def run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command with captured UTF-8 text output."""

    return subprocess.run(
        list(arguments),
        cwd=cwd,
        env=env,
        check=check,
        shell=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def initialize_git_repository(
    path: Path,
    *,
    object_format: str | None = None,
) -> None:
    """Create an empty Git repository for an integration test."""

    path.mkdir()
    arguments = ["git", "init"]
    if object_format is not None:
        arguments.append(f"--object-format={object_format}")
    arguments.extend(("--initial-branch=main", str(path)))
    run(arguments, cwd=path.parent)


def seed_commit(repository: Path, *, content: str = "fixture\n") -> str:
    """Configure a fixed test author and create one fixture commit."""

    run(
        ["git", "config", "user.name", "Agent Squad Tests"],
        cwd=repository,
    )
    run(
        [
            "git",
            "config",
            "user.email",
            "agent-squad@example.invalid",
        ],
        cwd=repository,
    )
    (repository / "README.md").write_text(content, encoding="utf-8")
    run(["git", "add", "README.md"], cwd=repository)
    run(
        [
            "git",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "-m",
            "test: seed repository",
        ],
        cwd=repository,
    )
    return run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
    ).stdout.strip()


def seed_git_repository(
    path: Path,
    *,
    object_format: str | None = None,
) -> str:
    """Create a Git repository containing one fixture commit."""

    initialize_git_repository(path, object_format=object_format)
    return seed_commit(path)


def git_path(repository: Path, argument: str) -> Path:
    """Resolve a path reported by ``git rev-parse``."""

    text = run(
        ["git", "rev-parse", argument],
        cwd=repository,
    ).stdout.strip()
    path = Path(text)
    if not path.is_absolute():
        path = repository / path
    return path.resolve()


def run_cli(
    repository: Path,
    *arguments: str,
    data_home: Path,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing_python_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(SRC_ROOT)
        if not existing_python_path
        else os.pathsep.join((str(SRC_ROOT), existing_python_path))
    )
    env["XDG_DATA_HOME"] = str(data_home)
    if env_overrides is not None:
        env.update(env_overrides)
    return run(
        [sys.executable, "-m", "agent_squad.cli", *arguments],
        cwd=repository,
        env=env,
        check=False,
    )


def install_fake_herdr(root: Path) -> tuple[Path, dict[str, str]]:
    """Install the tracked fake as ``herdr`` and return its environment."""

    binary_directory = root / "bin"
    binary_directory.mkdir(parents=True)
    executable = binary_directory / "herdr"
    shutil.copyfile(PROJECT_ROOT / "tests/fixtures/fake_herdr.py", executable)
    executable.chmod(0o755)
    state_directory = root / "fake-herdr-state"
    path_entries = [str(binary_directory)]
    existing_path = os.environ.get("PATH")
    if existing_path:
        path_entries.append(existing_path)
    path = os.pathsep.join(path_entries)
    return executable, {
        "PATH": path,
        "FAKE_HERDR_STATE_DIR": str(state_directory),
    }
