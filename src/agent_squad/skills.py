"""Install the two packaged role skills without modifying other skills."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from .initialization import AgentSquadError

SKILL_NAMES = ("squad-implementer", "squad-reviewer")


def packaged_skill(name: str) -> bytes:
    if name not in SKILL_NAMES:
        raise AgentSquadError(f"unknown packaged skill: {name}")
    resource = files("agent_squad").joinpath("skills", name, "SKILL.md")
    return resource.read_bytes()


def install(
    *, codex: bool = False, claude: bool = False, force: bool = False
) -> dict:
    """Preflight destinations, then copy files and create relative links."""
    copy = codex or not claude
    link = claude or not codex
    home = Path.home()
    plans = []
    for name in SKILL_NAMES:
        directory = home / ".agents/skills" / name
        target = directory / "SKILL.md"
        destination = home / ".claude/skills" / name
        relative = f"../../.agents/skills/{name}"
        content = packaged_skill(name)
        if copy:
            if directory.is_symlink():
                raise AgentSquadError(
                    f"skill directory is a symlink; retained: {directory}"
                )
            if target.exists() and not target.is_file():
                raise AgentSquadError(f"not a skill file: {target}")
            if (
                target.is_symlink()
                or (target.exists() and target.read_bytes() != content)
            ) and not force:
                raise AgentSquadError(
                    f"differing skill; use --force: {target}")
        if link:
            if destination.is_symlink():
                if str(destination.readlink()) != relative and not force:
                    raise AgentSquadError(
                        f"differing symlink; use --force: {destination}"
                    )
            elif destination.exists():
                raise AgentSquadError(
                    f"not a symlink; retained: {destination}"
                )
        plans.append((target, destination, relative, content))

    steps = []
    for target, destination, relative, content in plans:
        if copy:
            if target.is_symlink():
                target.unlink()
            unchanged = target.exists() and target.read_bytes() == content
            if not unchanged:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            steps.append({
                "path": str(target),
                "action": "unchanged" if unchanged else "copied",
            })
        if link:
            unchanged = (
                destination.is_symlink()
                and str(destination.readlink()) == relative
            )
            if not unchanged:
                if destination.is_symlink():
                    destination.unlink()
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(relative, target_is_directory=True)
            steps.append({
                "path": str(destination),
                "action": "unchanged" if unchanged else "linked",
                "target": relative,
            })
    return {"steps": steps}
