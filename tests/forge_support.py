"""Disposable Git repositories using only the committed fake forge."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from tests._support import PROJECT_ROOT, SRC_ROOT, add_src_to_path

add_src_to_path()

REPORT = (
    "## Implementation report\n\n"
    + "\n\n".join(
        f"### {name}\n\nScripted fixture."
        for name in (
            "Summary",
            "Scope",
            "Files changed",
            "Design decisions",
            "Validation performed",
            "Known limitations",
            "Areas worth extra review",
        )
    )
    + "\n"
)
TASK = "## Task\n\nExercise the forge protocol with scripted content.\n"
REVIEW = (
    "## Summary\n\nScripted review; the Developer must decide the requested"
    " policy when needed.\n\n## Verified dispositions\n\nnone\n\n##"
    " Findings\n\nnone\n"
)
FINDING_BODY = "\n\n".join(
    f"**{name}**: Scripted evidence for {name.lower()}."
    for name in (
        "Problem",
        "Evidence",
        "Impact",
        "Required change",
        "Verification",
    )
)


def finding(
    severity: str = "blocking", title: str = "Fixture finding", line: int = 2
) -> dict:
    return {
        "severity": severity,
        "category": "tests",
        "title": title,
        "path": "example.py",
        "line": line,
        "body": FINDING_BODY,
    }


class ForgeFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="agent-squad-forge-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repository"
        self.origin = self.root / "MagiLand/trial.git"
        self.origin.parent.mkdir()
        self.repo.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for source, name in [("gh", "gh"), ("fake_herdr.py", "herdr")]:
            shutil.copyfile(
                PROJECT_ROOT / "tests/fixtures" / source, self.bin / name
            )
            (self.bin / name).chmod(0o755)
        self.model_path = self.root / "forge.json"
        self.model_path.write_text(
            json.dumps(
                {
                    "origin": str(self.origin),
                    "next_id": 100,
                    "settings": {},
                    "calls": [],
                    "prs": {},
                    "issues": {
                        "1": {
                            "id": 1,
                            "number": 1,
                            "title": "Exercise forge protocol",
                            "body": "Scripted fixture issue",
                            "user": {"login": "developer"},
                            "created_at": "2026-01-01T00:00:00Z",
                            "labels": [{"name": "ready-for-agent"}],
                            "conversation": [],
                        }
                    },
                }
            )
        )
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not (
                key.startswith(("GIT_", "GH_", "GITHUB_", "PYTHON", "FAKE_"))
            )
        }
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            PYTHONPATH=str(SRC_ROOT),
            FAKE_FORGE_MODEL=str(self.model_path),
            FAKE_HERDR_MODEL=str(self.root / "herdr.json"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="Squad fixture",
            GIT_AUTHOR_EMAIL="squad@example.invalid",
            GIT_COMMITTER_NAME="Squad fixture",
            GIT_COMMITTER_EMAIL="squad@example.invalid",
        )
        self.history = []
        self.git(
            "init",
            "--bare",
            "--initial-branch=main",
            str(self.origin),
            cwd=self.root,
        )
        self.git(
            "init", "--initial-branch=main", str(self.repo), cwd=self.root
        )
        (self.repo / "example.py").write_text("value = 1\n")
        self.git("add", "example.py")
        self.git("commit", "-m", "test: seed fixture")
        self.base = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "-u", "origin", "main")
        self.worktree = self.repo
        self.task = self.write("task.md", TASK)
        self.report = self.write("report.md", REPORT)
        self.review_body = self.write("review.md", REVIEW)

    def close(self) -> None:
        self.temporary.cleanup()

    def __enter__(self) -> ForgeFixture:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def run(
        self, args: list[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=cwd or self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=90,
            shell=False,
        )

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = self.run(["git", *args], cwd=cwd)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def write(self, name: str, content: str) -> str:
        path = self.root / name
        path.write_text(content)
        return str(path)

    def read_model(self) -> dict:
        return json.loads(self.model_path.read_text())

    def save_model(self, model: dict) -> None:
        self.model_path.write_text(json.dumps(model))

    def settings(self, **values: object) -> None:
        model = self.read_model()
        model["settings"].update(values)
        self.save_model(model)

    def herdr_model(self) -> dict:
        path = Path(self.env["FAKE_HERDR_MODEL"])
        return (
            json.loads(path.read_text())
            if path.exists()
            else {
                "workspaces": [],
                "tabs": [],
                "panes": [],
                "agents": [],
                "calls": [],
                "next_id": 1,
                "settings": {},
            }
        )

    def save_herdr(self, model: dict) -> None:
        Path(self.env["FAKE_HERDR_MODEL"]).write_text(json.dumps(model))

    def herdr_settings(self, **values: object) -> None:
        model = self.herdr_model()
        model["settings"].update(values)
        self.save_herdr(model)

    def cli(
        self, *args: str, expected: int = 0, cwd: Path | None = None
    ) -> dict:
        result = self.run(
            [sys.executable, "-m", "agent_squad", *args, "--json"],
            cwd=cwd or self.worktree,
        )
        self.history.append(
            {"command": ["agent-squad", *args], "exit": result.returncode}
        )
        if result.returncode != expected:
            raise AssertionError(
                f"{args}: exit {result.returncode}, expected"
                f" {expected}\n{result.stdout}\n{result.stderr}"
            )
        if result.returncode:
            return {"error": result.stderr.strip()}
        return json.loads(result.stdout)

    def initialize(self) -> None:
        self.cli(
            "init",
            "--implementer-account",
            "developer",
            "--reviewer-account",
            "reviewer",
        )

    def candidate(self) -> str:
        self.worktree = self.repo / ".agent-squad/worktrees/issue-1"
        self.git("worktree", "add", "-b", "issue-1", str(self.worktree))
        return self.push("value = 1\nsecond = 2\nthird = 3\n")

    def commit(self, text: str) -> str:
        (self.worktree / "example.py").write_text(text)
        self.git("add", "example.py", cwd=self.worktree)
        self.git("commit", "-m", "test: change fixture", cwd=self.worktree)
        return self.git("rev-parse", "HEAD", cwd=self.worktree)

    def push(self, text: str) -> str:
        head = self.commit(text)
        self.git("push", "-u", "origin", "HEAD", cwd=self.worktree)
        return head

    def create_pr(self) -> dict:
        return self.cli(
            "pr",
            "create",
            "--as",
            "implementer",
            "--issue",
            "1",
            "--task",
            self.task,
            "--report",
            self.report,
        )

    def review(
        self,
        verdict: str,
        threads: list[dict] | None = None,
        *,
        resume: int | None = None,
        head: str | None = None,
        base: str | None = None,
        expected: int = 0,
    ) -> dict:
        file = self.write("threads.json", json.dumps(threads or []))
        args = [
            "review",
            "post",
            "--as",
            "reviewer",
            "--pr",
            "1",
            "--head",
            head or self.git("rev-parse", "HEAD", cwd=self.worktree),
            "--base",
            base or self.base,
            "--verdict",
            verdict,
            "--body",
            self.review_body,
            "--threads",
            file,
        ]
        if resume is not None:
            args += ["--resume", str(resume)]
        return self.cli(*args, expected=expected)

    def status(self) -> dict:
        return self.cli("status", "--pr", "1")

    def reply(
        self, fid: str, body: str, role: str = "implementer", expected: int = 0
    ) -> dict:
        file = self.write("reply.md", body)
        return self.cli(
            "thread",
            "reply",
            "--as",
            role,
            "--pr",
            "1",
            "--finding",
            fid,
            "--body",
            file,
            expected=expected,
        )

    def decision(
        self,
        *,
        fid: str = "none",
        budget: int | None = None,
        task: str | None = None,
        expected: int = 0,
    ) -> dict:
        file = self.write(
            "decision.md",
            "Scripted Developer decision: continue under the fixture policy.",
        )
        args = [
            "decision",
            "post",
            "--as",
            "implementer",
            "--pr",
            "1",
            "--finding",
            fid,
            "--body",
            file,
        ]
        if budget is not None:
            args += ["--budget", str(budget)]
        if task is not None:
            args += ["--task", self.write("amended-task.md", task)]
        return self.cli(*args, expected=expected)
