"""Report prerequisite failures and residue without repairing resources."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import tempfile
from typing import Callable, Iterator

from .forge import GitHub, PullRequest
from .herdr import HerdrClient, HerdrError
from .initialization import (
    AgentSquadError,
    LOCAL_EXCLUDE_PATTERNS,
    Repository,
    RetainedError,
    discover_git_worktree,
    git_output,
    list_worktrees,
    load_initialized_repository,
)
from .skills import SKILL_NAMES, packaged_skill

REVIEW_NAME = re.compile(r"reviewer-pr([1-9][0-9]*)-[0-9a-f]{7}")
SCRATCH_NAME = re.compile(r"pr([1-9][0-9]*)")
ISSUE_SCRATCH_NAME = re.compile(r"issue-([1-9][0-9]*)")


@dataclass(frozen=True)
class Diagnostic:
    check: str
    severity: str
    detail: str


@contextmanager
def writable_directory(path: Path) -> Iterator[None]:
    """Probe writes, removing only empty directories created by the probe."""
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    created = []
    try:
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)
        with tempfile.TemporaryFile(dir=path) as stream:
            stream.write(b"agent-squad doctor write probe")
            stream.flush()
        yield
    finally:
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError as error:
                raise AgentSquadError(
                    f"probe directory retained: {directory}: {error}"
                ) from None


def probe_worktree(repository: Repository, root: Path) -> None:
    """Create and remove a detached checkout, retaining any unexpected data."""
    with writable_directory(root):
        path = Path(tempfile.mkdtemp(prefix="doctor-", dir=root))
        try:
            git_output(
                repository.root, "worktree", "add", "--detach", str(path),
                "HEAD",
            )
        except AgentSquadError as error:
            # Git may have registered a partial checkout. Never recursively
            # delete it or remove a registration without a successful add.
            if any(w.root == path for w in list_worktrees(repository.root)):
                raise AgentSquadError(
                    f"worktree probe failed; retained {path}: {error}"
                ) from None
            path.rmdir()
            raise
        try:
            git_output(repository.root, "worktree", "remove", str(path))
        except AgentSquadError as error:
            raise AgentSquadError(
                f"worktree probe cleanup failed; retained {path}: {error}"
            ) from None


def check_skill_copy(home: Path, name: str) -> None:
    path = home / ".agents/skills" / name / "SKILL.md"
    if path.read_bytes() != packaged_skill(name):
        raise AgentSquadError(f"installed skill differs from package: {path}")


def check_skill_link(home: Path, name: str) -> None:
    link = home / ".claude/skills" / name
    target = home / ".agents/skills" / name
    if not link.is_symlink() or link.resolve() != target.resolve():
        raise AgentSquadError(f"skill symlink must point to {target}: {link}")
    if not (link / "SKILL.md").is_file():
        raise AgentSquadError(f"skill symlink target is missing: {link}")


def check_code_review(home: Path) -> None:
    """Check the installed skill's top-level frontmatter name scalar."""
    path = home / ".agents/skills/code-review/SKILL.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    names = []
    if lines and lines[0] == "---":
        for line in lines[1:]:
            if line in ("---", "..."):
                break
            if line.startswith("name:"):
                names.append(line)
        else:
            names = []
    scalar = r'''(?:code-review|'code-review'|"code-review")'''
    if len(names) != 1 or re.fullmatch(
        rf"name:[ \t]+{scalar}[ \t]*(?:[ \t]+#.*)?", names[0]
    ) is None:
        raise AgentSquadError(
            f"skill requires frontmatter name: code-review: {path}"
        )


def orphan_diagnostics(
    repository: Repository,
    forge: GitHub | None,
    snapshot: dict | None,
) -> list[Diagnostic]:
    """Read forge state for resources scoped to this repository."""
    config = repository.configuration
    worktree_root = repository.resolve_root(config.worktree_root)
    scratch_root = repository.resolve_root(config.scratch_root)
    resources: list[tuple[str, str, int]] = []
    issue_scratch: list[tuple[Path, int]] = []
    diagnostics = []
    for worktree in list_worktrees(repository.root):
        match = REVIEW_NAME.fullmatch(worktree.root.name)
        if worktree.root.parent == worktree_root and match:
            resources.append(("worktree", str(worktree.root), int(match[1])))
    if scratch_root.exists():
        for path in sorted(scratch_root.iterdir()):
            match = SCRATCH_NAME.fullmatch(path.name)
            if match and path.is_dir():
                resources.append(
                    ("scratch directory", str(path), int(match[1])))
            issue_match = ISSUE_SCRATCH_NAME.fullmatch(path.name)
            if issue_match and path.is_dir():
                issue_scratch.append((path, int(issue_match[1])))
    if snapshot is not None:
        agents = snapshot.get("agents")
        if not isinstance(agents, list):
            raise HerdrError("Herdr snapshot has no agents array")
        for agent in agents:
            if not isinstance(agent, dict):
                raise HerdrError("Herdr snapshot contains an invalid agent")
            name = agent.get("name")
            match = REVIEW_NAME.fullmatch(
                name) if isinstance(name, str) else None
            if not match:
                continue
            cwd = agent.get("cwd")
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                diagnostics.append(Diagnostic(
                    "orphan agent", "fail", f"cannot locate agent {name}",
                ))
                continue
            path = Path(cwd).resolve()
            # A process can outlive its deleted review worktree. The reserved
            # configured path still identifies that residue; otherwise use Git
            # common-directory identity, not a global PR-number match.
            if path == worktree_root / name and not path.exists():
                belongs = True
            else:
                try:
                    belongs = discover_git_worktree(
                        path).common == repository.common
                except AgentSquadError:
                    belongs = False
            if belongs:
                resources.append(("agent", name, int(match[1])))
    cache: dict[int, PullRequest | AgentSquadError] = {}
    for kind, resource, number in resources:
        if number not in cache:
            try:
                if forge is None:
                    raise AgentSquadError("forge identity unavailable")
                pr = forge.pr(number)
                if pr.number != number:
                    raise AgentSquadError("forge returned a different PR")
                cache[number] = pr
            except AgentSquadError as error:
                cache[number] = error
        pr = cache[number]
        if isinstance(pr, AgentSquadError):
            diagnostics.append(Diagnostic(
                f"orphan {kind}", "fail",
                f"cannot determine PR #{number} state for {resource}: {pr}",
            ))
        elif pr.merged or (
            kind != "scratch directory" and pr.state == "closed"
        ):
            state = "merged" if pr.merged else "closed"
            diagnostics.append(Diagnostic(
                f"orphan {kind}", "warn",
                f"PR #{number} is {state}; retained {resource}",
            ))
    for path, number in issue_scratch:
        try:
            if forge is None:
                raise AgentSquadError("forge identity unavailable")
            issue = forge.issue(number)
            if issue["number"] != number or issue["is_pull_request"]:
                raise AgentSquadError("forge returned a different issue")
            if issue["state"] not in ("open", "closed"):
                raise AgentSquadError("forge returned an invalid issue state")
        except AgentSquadError as error:
            diagnostics.append(Diagnostic(
                "orphan issue scratch directory", "fail",
                f"cannot determine issue #{number} state for {path}: {error}",
            ))
        else:
            if issue["state"] == "closed":
                diagnostics.append(Diagnostic(
                    "orphan issue scratch directory", "warn",
                    f"issue #{number} is closed; retained {path}",
                ))
    if not diagnostics:
        diagnostics.append(Diagnostic(
            "orphan resources", "pass", "no orphaned resources found",
        ))
    return diagnostics


def diagnose(
    start: Path,
    *,
    herdr_client: HerdrClient | None = None,
    live_reviewer: bool = False,
) -> dict:
    diagnostics: list[Diagnostic] = []

    def add(name: str, severity: str, detail: object) -> None:
        diagnostics.append(Diagnostic(
            name, severity, " ".join(str(detail).splitlines()),
        ))

    def check(name: str, action: Callable[[], object]) -> bool:
        # Path.resolve uses RuntimeError for symlink cycles on Python 3.11/12.
        try:
            value = action()
        except (AgentSquadError, OSError, ValueError, RuntimeError) as error:
            add(name, "fail", error)
            return False
        add(name, "pass", value or "verified")
        return True

    def result(live: dict | None = None, retained: bool = False) -> dict:
        failed = any(d.severity == "fail" for d in diagnostics)
        return {
            "ok": not failed,
            "diagnostics": [asdict(d) for d in diagnostics],
            "live_reviewer": live,
            "exit_code": 3 if retained else 1 if failed else 0,
        }

    try:
        repository = load_initialized_repository(start)
    except (AgentSquadError, OSError, ValueError, RuntimeError) as error:
        add("repository and configuration", "fail", error)
        return result()
    add("repository and configuration", "pass", repository.configuration_path)
    config = repository.configuration

    def exclusions() -> None:
        path = repository.common / "info/exclude"
        if path.is_symlink():
            raise AgentSquadError(
                f"local exclusions must not be a symlink: {path}")
        content = path.read_text(encoding="utf-8")
        if not all(p in content.splitlines() for p in LOCAL_EXCLUDE_PATTERNS):
            raise AgentSquadError(
                "local exclusions are missing; run agent-squad init")

    check("local exclusions", exclusions)

    def writable(path: Path) -> None:
        with writable_directory(path):
            pass

    check("control root", lambda: writable(repository.control_root))
    worktree_root = repository.resolve_root(config.worktree_root)
    scratch_root = repository.resolve_root(config.scratch_root)
    roots_ok = check("worktree root", lambda: writable(worktree_root))
    check("scratch root", lambda: writable(scratch_root))
    check("Git object format", lambda: git_output(
        repository.root, "rev-parse", "--show-object-format",
    ))
    head_ok = check("committed HEAD", lambda: git_output(
        repository.root, "rev-parse", "--verify", "HEAD^{commit}",
    ))
    if roots_ok and head_ok:
        check("disposable worktree", lambda: probe_worktree(
            repository, worktree_root))
    add(
        "submodules", "warn" if (
            repository.root / ".gitmodules").exists() else "pass",
        "prepare submodule-dependent validation separately"
        if (repository.root / ".gitmodules").exists() else "no .gitmodules",
    )

    def base_branch() -> str:
        expected = f"refs/heads/{config.base_branch}"
        refs = git_output(repository.root, "ls-remote",
                          "--heads", "origin", expected)
        if not any(line.split()[1:] == [expected]
                   for line in refs.splitlines()):
            raise AgentSquadError(
                f"base_branch does not exist on origin: {config.base_branch}")
        return config.base_branch

    check("remote base branch", base_branch)
    forges = {}
    for role in ("implementer", "reviewer"):
        try:
            forge = GitHub(repository, role)
        except AgentSquadError as error:
            add(f"{role} forge", "fail", error)
            continue
        if role == "implementer":
            check("GitHub CLI", forge.version)
        # verify_identity resolves the token internally. Never return a token
        # as a diagnostic value, or interpolate API bodies in success output.
        if check(f"{role} forge identity", forge.verify_identity):
            forges[role] = forge

            def readable() -> None:
                forge.repository_record()

            check(f"{role} repository access", readable)
            if role == "reviewer":
                def reviewer_write() -> str:
                    permission = forge.repository_permission()
                    if permission not in ("write", "admin"):
                        raise AgentSquadError(
                            "Reviewer account requires repository write"
                            " permission"
                        )
                    return permission

                check("Reviewer write permission", reviewer_write)

    client = herdr_client or HerdrClient(repository.root)
    herdr_ok = check(
        "Herdr Reviewer discovery and integration",
        lambda: client.discover(
            config.reviewer.kind,
            role="Reviewer",
        ))
    herdr_ok &= check(
        "Herdr Implementer discovery and integration",
        lambda: client.discover(
            config.implementer.kind,
            role="Implementer",
        ))
    if herdr_ok:
        try:
            client.inspect_agent(config.implementer.agent_name,
                                 config.implementer.kind, role="Implementer")
            add("Implementer identity", "pass", config.implementer.agent_name)
        except HerdrError as error:
            severity = "warn" if "is not available" in str(error) else "fail"
            add("Implementer identity", severity, error)

    home = Path.home()
    for name in SKILL_NAMES:
        check(f"installed {name}", lambda: check_skill_copy(home, name))
        check(f"Claude symlink {name}", lambda: check_skill_link(home, name))
    check("installed code-review", lambda: check_code_review(home))

    snapshot = None
    try:
        snapshot = client.snapshot()
        add(
            "Herdr socket and inventory", "pass",
            "reachable from this process",
        )
    except (AgentSquadError, OSError, ValueError) as error:
        add("Herdr socket and inventory", "fail", error)
    try:
        for diagnostic in orphan_diagnostics(repository, forges.get(
                "implementer") or forges.get("reviewer"), snapshot):
            add(diagnostic.check, diagnostic.severity, diagnostic.detail)
    except (AgentSquadError, OSError, ValueError, RuntimeError) as error:
        add("orphan resources", "fail", error)

    live = None
    retained = False
    if live_reviewer and not any(d.severity == "fail" for d in diagnostics):
        from .reviewer import live_probe

        try:
            live = live_probe(repository, client)
            add("live Reviewer", "pass", live)
        except RetainedError as error:
            retained = True
            add("live Reviewer", "fail", error)
        except (AgentSquadError, OSError, ValueError) as error:
            add("live Reviewer", "fail", error)
    return result(live, retained)
