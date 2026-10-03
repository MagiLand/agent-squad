"""Disposable Git repositories using only the committed fake forge."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

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
                            "state": "open",
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
                key.startswith(
                    ("GIT_", "GH_", "GITHUB_", "PYTHON", "FAKE_", "HERDR_")
                )
            )
        }
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            HOME=str(self.root / "home"),
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
        self.prepare_skills()

    def prepare_skills(self) -> None:
        """Provide local prerequisites without reading the user's HOME."""
        from agent_squad.skills import SKILL_NAMES, packaged_skill

        self.home = Path(self.env["HOME"])
        for name in SKILL_NAMES:
            path = self.home / ".agents/skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(packaged_skill(name))
            link = self.home / ".claude/skills" / name
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(f"../../.agents/skills/{name}")
        path = self.home / ".agents/skills/code-review/SKILL.md"
        path.parent.mkdir()
        path.write_text("---\nname: code-review\n---\nScripted skill.\n")

    def close(self) -> None:
        try:
            self.temporary.cleanup()
        except OSError as error:
            raise RuntimeError(
                f"temporary resources retained at {self.root}: {error}"
            ) from error

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

    def seed_read_scenario(self) -> None:
        """Load the same scripted evidence for either transport."""
        values = {
            "$HEAD": self.git("rev-parse", "HEAD", cwd=self.worktree),
            "$BASE": self.base, "$TASK": TASK.strip(),
            "$REPORT": REPORT.strip(), "$REVIEW": REVIEW.strip(),
            "$FINDING_BODY": FINDING_BODY,
        }

        def substitute(value):
            if isinstance(value, dict):
                return {k: substitute(v) for k, v in value.items()}
            if isinstance(value, list):
                return [substitute(v) for v in value]
            if isinstance(value, str):
                for key, replacement in values.items():
                    value = value.replace(key, replacement)
            return value

        source = PROJECT_ROOT / "tests/fixtures/forge_read_scenario.json"
        model = self.read_model()
        model["prs"]["1"] = substitute(json.loads(source.read_text()))
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

    def herdr_session(self, name: str, **settings: object) -> None:
        """Add a further fake session; ``fixture`` is the default one."""
        model = self.herdr_model()
        model.setdefault("sessions", {})[name] = {"settings": settings}
        self.save_herdr(model)

    def herdr_socket(self, name: str = "fixture") -> str:
        return str(self.root / f"herdr-{name}.sock")

    def inherit_herdr(self, name: str | None) -> None:
        """Point the inherited Herdr variables at a session, or clear them."""
        for key in ("HERDR_SESSION", "HERDR_SOCKET_PATH"):
            self.env.pop(key, None)
        if name is not None:
            self.env.update(
                HERDR_SESSION=name, HERDR_SOCKET_PATH=self.herdr_socket(name)
            )

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
        if result.returncode and args[0] != "doctor":
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

    def single_identity(self) -> None:
        """Initialize a shared account and scripted human approver."""
        self.cli(
            "init", "--implementer-account", "developer",
            "--reviewer-account", "developer", "--identity-mode", "single",
            "--approver-account", "human",
        )
        self.settings(human_accounts=["human"])

    def human_review(
        self, event: str, *, head: str | None = None, login: str = "human",
    ) -> dict:
        """Script a person-operated review through the fake transport only."""
        payload = self.write("human-review.json", json.dumps({
            "body": "Scripted human review.", "event": event,
            "commit_id": head or self.git(
                "rev-parse", "HEAD", cwd=self.worktree,
            ),
        }))
        result = subprocess.run(
            [str(self.bin / "gh"), "api",
             "repos/MagiLand/trial/pulls/1/reviews",
             "--method", "POST", "--input", "-"],
            input=Path(payload).read_text(), cwd=self.repo,
            env={**self.env, "GH_TOKEN": "fake-token-" + login},
            text=True, capture_output=True, timeout=90, shell=False,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def commit(self, text: str) -> str:
        (self.worktree / "example.py").write_text(text)
        self.git("add", "example.py", cwd=self.worktree)
        self.git("commit", "-m", "test: change fixture", cwd=self.worktree)
        return self.git("rev-parse", "HEAD", cwd=self.worktree)

    def push(self, text: str) -> str:
        head = self.commit(text)
        self.git("push", "-u", "origin", "HEAD", cwd=self.worktree)
        return head

    def create_pr(self, *, issue_task: bool = False) -> dict:
        return self.cli(
            "pr",
            "create",
            "--as",
            "implementer",
            "--issue",
            "1",
            *([] if issue_task else ["--task", self.task]),
            "--report",
            self.report,
        )

    def review(
        self,
        verdict: str,
        threads: list[dict] | None = None,
        *,
        resume: int | None = None,
        discard_draft: int | None = None,
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
        if discard_draft is not None:
            args += ["--discard-draft", str(discard_draft)]
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
        body: str = (
            "Scripted Developer decision: continue under the fixture policy."
        ),
    ) -> dict:
        file = self.write(
            "decision.md",
            body,
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


class ForgejoFixture(ForgeFixture):
    def __init__(self, *, path_prefix: str = "") -> None:
        super().__init__()
        from tests.fixtures.fake_forgejo import FakeForgejo

        with patch.dict(os.environ, FAKE_FORGE_MODEL=str(self.model_path)):
            self.server = FakeForgejo(path_prefix=path_prefix)
        self.server.server.git_env = self.env

    def close(self) -> None:
        self.server.close()
        super().close()

    def initialize(self) -> None:
        # Forgejo init belongs to Increment 5. The fixture supplies a valid
        # configuration directly, without invoking even the fake gh.
        self.configure()

    def single_identity(self) -> None:
        self.configure(single=True)

    def configure(self, *, single: bool = False) -> None:
        from agent_squad.initialization import Configuration

        tokens = {}
        for role, account in (
            ("implementer", "developer"),
            ("reviewer", "developer" if single else "reviewer"),
        ):
            path = self.root / f"{account}.token"
            path.write_text(f"fake-token-{account}\n")
            path.chmod(0o600)
            tokens[role] = str(path)
        config = Configuration.from_dict(
            {
                "schema_version": 2,
                "forge": {
                    "kind": "forgejo",
                    "owner": "MagiLand",
                    "repo": "trial",
                    "base_url": self.server.base_url,
                },
                "implementer": {
                    "agent_name": "implementer",
                    "kind": "codex",
                    "forge_account": "developer",
                    "token_file": tokens["implementer"],
                },
                "reviewer": {
                    "kind": "claude",
                    "start_args": [],
                    "forge_account": "developer" if single else "reviewer",
                    "token_file": tokens["reviewer"],
                },
                "identity_mode": "single" if single else "dual",
                "approver_accounts": ["human"] if single else [],
                "base_branch": "main",
                "max_review_passes": 3,
                "merge_method": "merge",
                "worktree_root": ".agent-squad/worktrees",
                "scratch_root": ".agent-squad/review-scratch",
            }
        )
        control = self.repo / ".agent-squad"
        control.mkdir(exist_ok=True)
        (control / "config.json").write_text(json.dumps(config.to_dict()))
        with (self.repo / ".git/info/exclude").open("a") as stream:
            stream.write("\n.agent-squad/\n.agent-squad-review/\n")
