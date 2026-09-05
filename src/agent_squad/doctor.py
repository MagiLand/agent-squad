"""Non-destructive operational checks and residual-resource reporting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import fcntl
import math
import os
import stat
from pathlib import Path
import tempfile
from typing import Literal

from .artifacts import ReviewRoundRecord, RoundStatus
from .herdr import HerdrClient, HerdrError, format_herdr_error
from .initialization import (
    AgentSquadError,
    InitializedRepository,
    LOCAL_EXCLUDE_PATTERNS,
    discover_git_worktree,
    load_initialized_repository,
    run_git,
)
from .runs import (
    inspect_status,
    inspect_status_locked,
    LOCK_FILE_NAME,
    load_json_object,
    repository_identity,
    load_cancelled_run,
    load_completed_run,
)
from .storage import atomic_write


@dataclass(frozen=True)
class Diagnostic:
    """One independently reported prerequisite or residual resource."""

    check: str
    severity: Literal["ok", "warning", "error"]
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    """Collected checks; warnings do not prevent operation."""

    diagnostics: tuple[Diagnostic, ...]

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)


def git_output(root: Path, *arguments: str) -> str:
    """Run one diagnostic Git query with its command in any failure."""

    result = run_git(root, *arguments)
    if result.returncode:
        raise AgentSquadError(
            f"git {' '.join(arguments)}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.rstrip("\r\n")


def diagnose(
    start: Path,
    *,
    live_reviewer: bool = False,
    timeout_seconds: float = 120.0,
    herdr_client: HerdrClient | None = None,
) -> DoctorReport:
    """Check prerequisites and report leftovers without adopting or deleting them."""

    diagnostics: list[Diagnostic] = []
    if live_reviewer and (
        not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        return DoctorReport(
            (
                Diagnostic(
                    "live Reviewer",
                    "error",
                    "timeout must be finite and positive",
                ),
            )
        )

    def check(name: str, action: Callable[[], str]) -> bool:
        try:
            detail = action()
        except (AgentSquadError, OSError, ValueError, RuntimeError) as error:
            diagnostics.append(
                Diagnostic(name, "error", format_herdr_error(str(error)))
            )
            return False
        diagnostics.append(Diagnostic(name, "ok", detail))
        return True

    try:
        worktree = discover_git_worktree(start)
    except (AgentSquadError, OSError, RuntimeError) as error:
        return DoctorReport((Diagnostic("repository", "error", str(error)),))
    diagnostics.append(Diagnostic("repository", "ok", str(worktree.root)))
    check(
        "local exclusions",
        lambda: _check_exclusions(worktree.root, worktree.local_exclude_path),
    )
    check("Git object format", lambda: _object_format(worktree.root))
    check(
        "committed HEAD",
        lambda: git_output(
            worktree.root, "rev-parse", "--verify", "HEAD^{commit}"
        ),
    )
    if (worktree.root / ".gitmodules").exists():
        diagnostics.append(
            Diagnostic(
                "submodules",
                "warning",
                "detached review worktrees may lack initialized submodule content; "
                "prepare submodules with a project-specific preflight",
            )
        )
    try:
        repository = load_initialized_repository(start)
    except (AgentSquadError, OSError, RuntimeError) as error:
        diagnostics.append(Diagnostic("configuration", "error", str(error)))
        return DoctorReport(tuple(diagnostics))
    diagnostics.append(
        Diagnostic(
            "configuration and placement",
            "ok",
            str(repository.configuration_path),
        )
    )
    check("control storage", lambda: _check_storage(repository.control_root))
    check(
        "stored identity and active paths",
        lambda: _check_status(worktree.root),
    )
    check(
        "configured base",
        lambda: git_output(
            worktree.root,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{repository.configuration.base_ref}^{{commit}}",
        ),
    )
    check(
        "review worktree placement and Git lifecycle",
        lambda: _check_worktree(repository),
    )

    client = herdr_client or HerdrClient(worktree.root)
    config = repository.configuration

    def discover() -> str:
        installation = client.discover(
            config.reviewer.kind,
            role="Reviewer",
            diagnostics=True,
            live=live_reviewer,
        )
        client.discover(config.implementer.kind, role="Implementer")
        return f"{installation.version}; protocol {installation.protocol}"

    herdr_ok = check("Herdr capabilities", discover)
    if herdr_ok:
        check("Implementer", lambda: _check_implementer(repository, client))
    _inspect_residuals(repository, client if herdr_ok else None, diagnostics)
    if live_reviewer:
        if any(item.severity == "error" for item in diagnostics):
            diagnostics.append(
                Diagnostic(
                    "live Reviewer",
                    "error",
                    "not started: prerequisite checks failed",
                )
            )
        else:
            from .preflight import live_preflight

            check(
                "live Reviewer",
                lambda: live_preflight(
                    repository,
                    client,
                    timeout_seconds=timeout_seconds,
                ),
            )
    return DoctorReport(tuple(diagnostics))


def _object_format(root: Path) -> str:
    value = git_output(root, "rev-parse", "--show-object-format")
    if value not in {"sha1", "sha256"}:
        raise AgentSquadError(f"unsupported Git object format: {value}")
    return value


def _check_exclusions(root: Path, path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    missing = set(LOCAL_EXCLUDE_PATTERNS) - set(lines)
    if missing:
        raise AgentSquadError(
            f"{path} lacks {', '.join(sorted(missing))}; run agent-squad init"
        )
    for pattern in LOCAL_EXCLUDE_PATTERNS:
        candidate = f"{pattern}doctor-probe"
        result = run_git(root, "check-ignore", "--no-index", "--", candidate)
        if result.returncode != 0 or result.stdout.strip() != candidate:
            raise AgentSquadError(
                f"{path}: {pattern} is not effectively excluded"
            )
    return str(path)


def _check_storage(root: Path) -> str:
    # Unique owned files exercise the actual filesystem, never the run lock.
    with tempfile.TemporaryDirectory(prefix=".doctor-", dir=root) as temporary:
        directory = Path(temporary)
        target = directory / "replace"
        atomic_write(target, b"before", mode=0o600)
        with target.open("rb") as old:
            atomic_write(target, b"after", mode=0o600)
            if old.read() != b"before" or target.read_bytes() != b"after":
                raise AgentSquadError(
                    "filesystem does not preserve atomic replacement"
                )
        with (directory / "lock").open("w+b") as first:
            with (directory / "lock").open("r+b") as second:
                fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AgentSquadError(
                        "filesystem does not enforce exclusive locks"
                    )
                finally:
                    fcntl.flock(first, fcntl.LOCK_UN)
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(second, fcntl.LOCK_UN)
    return (
        f"{root}: writable, atomic replacement and exclusive locking verified"
    )


def _check_status(root: Path) -> str:
    repository = load_initialized_repository(root)
    lock_path = repository.control_root / LOCK_FILE_NAME
    if not os.path.lexists(lock_path):
        # An initialized worktree without a run has no lock to acquire.
        status = inspect_status(root)
        load_cancelled_run(repository)
        load_completed_run(repository)
    else:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise AgentSquadError(
                    f"run lock is not a regular file: {lock_path}"
                )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise AgentSquadError(
                    f"run lock is busy: {lock_path}; retry doctor after the active operation"
                ) from error
            status = inspect_status_locked(root)
            load_cancelled_run(repository)
            load_completed_run(repository)
        finally:
            os.close(descriptor)
    active = status.active_run
    if active is not None:
        if active.review_worktree_available is False:
            raise AgentSquadError(
                "active review worktree is missing: "
                f"{active.active_round.review_worktree}"
            )
        if active.review_bundle_intact is False:
            raise AgentSquadError(
                f"active review bundle: {active.review_bundle_error}"
            )
    return f"repository {status.repository_id}; " + (
        f"active run {active.run_id}"
        if active is not None
        else "no active run"
    )


def _check_implementer(
    repository: InitializedRepository, client: HerdrClient
) -> str:
    config = repository.configuration.implementer
    client.inspect_agent(
        config.agent_name,
        config.kind,
        role="Implementer",
    )
    return config.agent_name


def _check_worktree(repository: InitializedRepository) -> str:
    root = repository.configuration.review_worktree_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    # mkdtemp establishes ownership; Git creates only its child.
    parent = Path(tempfile.mkdtemp(prefix=".doctor-", dir=root))
    worktree = parent / "snapshot"
    try:
        head = git_output(
            repository.worktree.root, "rev-parse", "--verify", "HEAD^{commit}"
        )
        git_output(
            repository.worktree.root,
            "-c",
            "core.hooksPath=/dev/null",
            "worktree",
            "add",
            "--detach",
            str(worktree),
            head,
        )
        discovered = discover_git_worktree(worktree)
        if (
            discovered.root != worktree
            or discovered.common_directory
            != repository.worktree.common_directory
        ):
            raise AgentSquadError(
                f"disposable worktree identity mismatch: {worktree}"
            )
        if git_output(worktree, "rev-parse", "HEAD") != head:
            raise AgentSquadError(
                f"disposable worktree HEAD mismatch: {worktree}"
            )
        git_output(
            repository.worktree.root, "worktree", "remove", str(worktree)
        )
        parent.rmdir()
    except (AgentSquadError, OSError) as error:
        raise AgentSquadError(
            f"{error}; owned probe retained at {parent}"
        ) from error
    return str(root)


def _inspect_residuals(
    repository: InitializedRepository,
    client: HerdrClient | None,
    diagnostics: list[Diagnostic],
) -> None:
    """Inspect this repository's namespace, including invalid/unregistered bundles."""

    def warn(detail: str) -> None:
        diagnostics.append(Diagnostic("residual resources", "warning", detail))

    try:
        identity = repository_identity(repository.worktree)
        namespace = (
            repository.configuration.review_worktree_root.resolve()
            / identity.repository_id
        )
        registered: dict[Path, str | None] = {}
        listing = git_output(
            repository.worktree.root, "worktree", "list", "--porcelain", "-z"
        )
        for block in listing.split("\0\0"):
            fields = block.split("\0")
            paths = [
                value[9:] for value in fields if value.startswith("worktree ")
            ]
            if paths:
                head = next(
                    (
                        value[5:]
                        for value in fields
                        if value.startswith("HEAD ")
                    ),
                    None,
                )
                registered[Path(paths[0])] = head
        records: dict[Path, ReviewRoundRecord] = {}
        terminal_runs: set[str] = set()
        for implementation in sorted(registered):
            control = implementation / ".agent-squad"
            if control.is_symlink():
                warn(f"control storage is a symlink; not traversed: {control}")
                continue
            runs_root = control / "runs"
            if runs_root.is_symlink():
                warn(f"run history is a symlink; not traversed: {runs_root}")
                continue
            if not runs_root.exists():
                continue
            for run_directory in sorted(runs_root.iterdir()):
                if run_directory.is_symlink() or not run_directory.is_dir():
                    warn(f"uninspectable run history: {run_directory}")
                    continue
                try:
                    run_record = load_json_object(
                        run_directory / "run.json", "run record"
                    )
                    stored = run_record.get("repository")
                    if (
                        not isinstance(stored, dict)
                        or stored.get("repository_id")
                        != identity.repository_id
                    ):
                        raise AgentSquadError(
                            "run repository identity differs"
                        )
                    if run_record.get("phase") in {"completed", "cancelled"}:
                        terminal_runs.add(run_directory.name)
                except (AgentSquadError, OSError) as error:
                    warn(f"cannot inspect {run_directory}: {error}")
                    continue
                rounds = run_directory / "rounds"
                if rounds.is_symlink():
                    warn(f"round history is a symlink: {rounds}")
                    continue
                if not rounds.exists():
                    continue
                for round_directory in sorted(rounds.iterdir()):
                    try:
                        if (
                            round_directory.is_symlink()
                            or not round_directory.is_dir()
                        ):
                            raise AgentSquadError(
                                "not a regular round directory"
                            )
                        record = ReviewRoundRecord.from_dict(
                            load_json_object(
                                round_directory / "round.json", "round record"
                            ),
                            label="round record",
                        )
                        if (
                            record.run_id != run_directory.name
                            or f"{record.round_number:03d}"
                            != round_directory.name
                        ):
                            raise AgentSquadError(
                                "record identity differs from history path"
                            )
                        records[record.review_worktree] = record
                    except (AgentSquadError, ValueError, OSError) as error:
                        warn(f"cannot inspect {round_directory}: {error}")
        candidates = {
            path for path in registered if path.is_relative_to(namespace)
        } | set(records)
        if namespace.is_symlink():
            warn(f"review namespace is a symlink; not traversed: {namespace}")
        elif namespace.exists():
            for run_path in sorted(namespace.iterdir()):
                if run_path.is_symlink() or not run_path.is_dir():
                    warn(f"unrecognized review resource: {run_path}")
                    continue
                candidates.update(run_path.iterdir())
        for path in sorted(candidates):
            record = records.get(path)
            if not os.path.lexists(path) and path not in registered:
                continue
            bundle = path / ".agent-squad-review"
            suffix = f"; bundle {bundle}" if os.path.lexists(bundle) else ""
            if path.is_symlink():
                warn(f"review resource is a symlink; not traversed: {path}")
            elif record is None:
                warn(
                    "orphaned review resource with no corresponding "
                    f"run/round record: {path}{suffix}"
                )
            elif (
                record.status
                not in {RoundStatus.PREPARED, RoundStatus.REVIEWING}
                or record.run_id in terminal_runs
            ):
                warn(
                    f"retained {record.status.value} round worktree: {path}{suffix}"
                )
            if path not in registered:
                warn(f"unregistered review worktree or bundle: {path}{suffix}")
            elif record is not None and registered[path] != record.head_oid:
                warn(
                    f"review HEAD differs from recorded {record.head_oid}: "
                    f"{path}{suffix}"
                )
        # Probe leftovers have explicit names but are never garbage-collected here.
        root = namespace.parent
        if root.exists():
            for path in sorted(root.iterdir()):
                if path.name.startswith((".doctor-", ".preflight-")):
                    warn(f"retained owned diagnostic evidence: {path}")
        if client is not None:
            agents = client.snapshot().get("agents")
            if not isinstance(agents, list):
                raise HerdrError("Herdr snapshot lacks agent inventory")
            names = {
                record.reviewer_name: record for record in records.values()
            }
            for agent in agents:
                if not isinstance(agent, dict):
                    raise HerdrError(
                        "Herdr agent inventory contains an invalid entry"
                    )
                name = agent.get("name")
                if not isinstance(name, str) or not name.startswith("asq-"):
                    continue
                record = names.get(name)
                cwd = agent.get("cwd")
                if record is None:
                    # A shared Herdr session can contain other repositories.
                    if isinstance(cwd, str) and Path(cwd).is_relative_to(
                        namespace
                    ):
                        warn(
                            f"orphaned Reviewer {name}: {cwd}; "
                            f"pane {agent.get('pane_id')}"
                        )
                elif (
                    record.status
                    not in {RoundStatus.PREPARED, RoundStatus.REVIEWING}
                    or record.run_id in terminal_runs
                    or cwd != str(record.review_worktree)
                ):
                    warn(
                        f"Reviewer {name} outlives or differs from its round: "
                        f"{cwd}; expected {record.review_worktree}; "
                        f"pane {agent.get('pane_id')}"
                    )
    except (AgentSquadError, OSError, ValueError, RuntimeError) as error:
        diagnostics.append(
            Diagnostic("residual inspection", "error", str(error))
        )
