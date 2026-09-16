"""Skill installation boundaries and the specification's verbatim contract."""

from agent_squad.skills import SKILL_NAMES, install, packaged_skill
from agent_squad.initialization import AgentSquadError
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests._support import PROJECT_ROOT, SRC_ROOT, add_src_to_path

add_src_to_path()


class SkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def test_cli_install_runs_without_repository_configuration(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "agent_squad", "skill", "install",
             "--codex", "--json"],
            cwd=self.home, env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
            shell=False, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".claude").exists())
        self.assertEqual(
            (self.home / ".agents/skills/squad-reviewer/SKILL.md")
            .read_bytes(),
            packaged_skill("squad-reviewer"),
        )

    def test_default_install_is_idempotent_and_shared_by_both_harnesses(
        self,
    ) -> None:
        first = install()
        self.assertEqual(len(first["steps"]), 4)
        for name in SKILL_NAMES:
            target = self.home / ".agents/skills" / name / "SKILL.md"
            link = self.home / ".claude/skills" / name
            self.assertEqual(target.read_bytes(), packaged_skill(name))
            self.assertEqual(
                str(link.readlink()), f"../../.agents/skills/{name}"
            )
            self.assertEqual((link / "SKILL.md").read_bytes(),
                             target.read_bytes())
        self.assertTrue(
            all(s["action"] == "unchanged" for s in install()["steps"]))

    def test_flags_select_only_the_requested_operations(self) -> None:
        install(claude=True)
        self.assertFalse((self.home / ".agents").exists())
        self.assertTrue(
            (self.home / ".claude/skills/squad-reviewer").is_symlink())
        install(codex=True)
        self.assertTrue(
            (self.home / ".agents/skills/squad-reviewer/SKILL.md").exists())
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"HOME": temporary}):
                install(codex=True)
                self.assertFalse((Path(temporary) / ".claude").exists())
                self.assertEqual(
                    len(install(codex=True, claude=True)["steps"]), 4)

    def test_conflict_preflight_preserves_every_destination_until_forced(
        self,
    ) -> None:
        install()
        other = self.home / ".agents/skills/other/SKILL.md"
        other.parent.mkdir()
        other.write_text("another skill")
        for name in ("AGENTS.md", "CLAUDE.md"):
            (self.home / name).write_text("leave alone")
        first = self.home / ".agents/skills/squad-implementer/SKILL.md"
        last = self.home / ".agents/skills/squad-reviewer/SKILL.md"
        first.unlink()
        last.write_text("user modification")
        with self.assertRaisesRegex(AgentSquadError, "differing skill"):
            install()
        self.assertFalse(first.exists())
        self.assertEqual(last.read_text(), "user modification")
        install(force=True)
        self.assertEqual(last.read_bytes(), packaged_skill("squad-reviewer"))
        self.assertEqual(other.read_text(), "another skill")
        for name in ("AGENTS.md", "CLAUDE.md"):
            self.assertEqual((self.home / name).read_text(), "leave alone")

    def test_foreign_link_requires_force_and_never_changes_its_target(
        self,
    ) -> None:
        foreign = self.home / "foreign"
        foreign.mkdir()
        (foreign / "SKILL.md").write_text("foreign skill")
        link = self.home / ".claude/skills/squad-reviewer"
        link.parent.mkdir(parents=True)
        link.symlink_to(foreign, target_is_directory=True)
        with self.assertRaisesRegex(AgentSquadError, "differing symlink"):
            install()
        self.assertFalse((self.home / ".agents").exists())
        install(force=True)
        self.assertEqual((foreign / "SKILL.md").read_text(), "foreign skill")
        self.assertEqual(str(link.readlink()),
                         "../../.agents/skills/squad-reviewer")

    def test_force_does_not_follow_skill_file_symlink_or_remove_directory(
        self,
    ) -> None:
        foreign = self.home / "foreign.md"
        foreign.write_text("keep")
        target = self.home / ".agents/skills/squad-implementer/SKILL.md"
        target.parent.mkdir(parents=True)
        target.symlink_to(foreign)
        with self.assertRaises(AgentSquadError):
            install(codex=True)
        install(codex=True, force=True)
        self.assertFalse(target.is_symlink())
        self.assertEqual(foreign.read_text(), "keep")
        directory = self.home / ".claude/skills/squad-implementer"
        directory.mkdir(parents=True)
        (directory / "notes.md").write_text("keep directory")
        with self.assertRaisesRegex(AgentSquadError, "not a symlink"):
            install(force=True)
        self.assertEqual((directory / "notes.md").read_text(),
                         "keep directory")

    def test_every_required_verbatim_block_is_packaged_byte_for_byte(
        self,
    ) -> None:
        spec = (PROJECT_ROOT / "docs/agent-squad-v0.5.0-spec.md").read_bytes()
        markers = [
            (b"### 8.8 Asynchronous handoff discipline", "squad-implementer"),
            (b"6. **Non-blocking and optional findings [verbatim]:**",
             "squad-implementer"),
            (b"6. **Severity and follow-up policy [verbatim]:**",
             "squad-reviewer"),
            (b"10. **Asynchronous handoff [verbatim]:**", "squad-reviewer"),
        ]
        for marker, name in markers:
            with self.subTest(marker=marker):
                lines = spec.split(marker, 1)[1].splitlines(keepends=True)
                block = []
                for line in lines:
                    if line.lstrip().startswith(b">"):
                        block.append(line)
                    elif block:
                        break
                self.assertTrue(block)
                self.assertIn(b"".join(block), packaged_skill(name))
        for name in SKILL_NAMES:
            self.assertTrue(packaged_skill(name).startswith(
                f"---\nname: {name}\ndescription: ".encode()
            ))
