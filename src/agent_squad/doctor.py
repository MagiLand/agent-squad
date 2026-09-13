"""The retained, read-only prerequisite checks for Increment 1."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import tempfile
from typing import Callable

from .herdr import HerdrClient, HerdrError
from .initialization import (
    AgentSquadError,
    LOCAL_EXCLUDE_PATTERNS,
    git_output,
    load_initialized_repository,
    validate_roots,
)


@dataclass(frozen=True)
class Diagnostic:
    check: str
    severity: str
    detail: str


def diagnose(
    start: Path,
    *,
    herdr_client: HerdrClient | None = None,
    live_reviewer: bool = False,
) -> dict:
    diagnostics = []

    def check(name: str, action: Callable[[], object]) -> bool:
        try:
            value = action()
        except (AgentSquadError, OSError, ValueError) as error:
            diagnostics.append(Diagnostic(name, "error", str(error)))
            return False
        diagnostics.append(Diagnostic(name, "ok", str(value or "verified")))
        return True

    try:
        repository = load_initialized_repository(start)
    except AgentSquadError as error:
        return {
            "ok": False,
            "diagnostics": [
                asdict(
                    Diagnostic(
                        "configuration and repository", "error", str(error)
                    )
                )
            ],
        }
    diagnostics.append(
        Diagnostic(
            "repository and configuration",
            "ok",
            str(repository.configuration_path),
        )
    )

    def exclusions() -> None:
        path = repository.common / "info/exclude"
        content = path.read_text(encoding="utf-8")
        if not all(p in content.splitlines() for p in LOCAL_EXCLUDE_PATTERNS):
            raise AgentSquadError(
                "local exclusions are missing; run agent-squad init"
            )

    check("local exclusions", exclusions)

    def control_writable() -> None:
        with tempfile.TemporaryFile(dir=repository.control_root) as stream:
            stream.write(b"control root probe")
            stream.flush()

    check("control root", control_writable)
    check(
        "worktree and scratch roots",
        lambda: validate_roots(repository, writable=True),
    )
    check(
        "Git object format",
        lambda: git_output(
            repository.root, "rev-parse", "--show-object-format"
        ),
    )
    check(
        "committed HEAD",
        lambda: git_output(
            repository.root, "rev-parse", "--verify", "HEAD^{commit}"
        ),
    )
    if (repository.root / ".gitmodules").exists():
        diagnostics.append(
            Diagnostic(
                "submodules",
                "warning",
                "prepare submodule-dependent validation separately",
            )
        )
    client = herdr_client or HerdrClient(repository.root)
    config = repository.configuration
    herdr_ok = check(
        "Herdr Reviewer discovery and integration",
        lambda: client.discover(config.reviewer.kind, role="Reviewer"),
    )
    herdr_ok &= check(
        "Herdr Implementer discovery and integration",
        lambda: client.discover(config.implementer.kind, role="Implementer"),
    )
    if herdr_ok:
        try:
            client.inspect_agent(
                config.implementer.agent_name,
                config.implementer.kind,
                role="Implementer",
            )
            diagnostics.append(
                Diagnostic(
                    "Implementer identity", "ok", config.implementer.agent_name
                )
            )
        except HerdrError as error:
            severity = (
                "warning" if "is not available" in str(error) else "error"
            )
            diagnostics.append(
                Diagnostic("Implementer identity", severity, str(error))
            )
    live = None
    retained = False
    if live_reviewer and not any(d.severity == "error" for d in diagnostics):
        from .reviewer import live_probe
        from .initialization import RetainedError

        try:
            live = live_probe(repository, client)
            diagnostics.append(Diagnostic("live Reviewer", "ok", str(live)))
        except RetainedError as error:
            retained = True
            diagnostics.append(
                Diagnostic("live Reviewer", "error", str(error))
            )
    return {
        "ok": not any(d.severity == "error" for d in diagnostics),
        "diagnostics": [asdict(d) for d in diagnostics],
        "live_reviewer": live,
        "exit_code": (
            3
            if retained
            else 1 if any(d.severity == "error" for d in diagnostics) else 0
        ),
    }
