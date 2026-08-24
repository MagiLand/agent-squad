"""Shared helpers for deterministic repository tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence


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


def initialize_git_repository(path: Path) -> None:
    path.mkdir()
    run(["git", "init", "--initial-branch=main", str(path)], cwd=path.parent)


def run_cli(
    repository: Path,
    *arguments: str,
    data_home: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing_python_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(SRC_ROOT)
        if not existing_python_path
        else os.pathsep.join((str(SRC_ROOT), existing_python_path))
    )
    env["XDG_DATA_HOME"] = str(data_home)
    return run(
        [sys.executable, "-m", "agent_squad.cli", *arguments],
        cwd=repository,
        env=env,
        check=False,
    )
