"""The release exposes the command names in the specification's table."""

import argparse
import re
import unittest

from tests._support import PROJECT_ROOT, add_src_to_path

add_src_to_path()

from agent_squad import __version__  # noqa: E402
from agent_squad.cli import parser  # noqa: E402
from agent_squad.conventions import PROTOCOL_VERSION, TAG  # noqa: E402


class CliSurfaceTests(unittest.TestCase):
    def test_command_surface_equals_specification_table(self) -> None:
        spec = (PROJECT_ROOT / "docs/agent-squad-spec.md").read_text()
        table = spec.split("### 10.2 Command table\n", 1)[1].split(
            "### 10.3", 1)[0]
        expected = set()
        for line in table.splitlines():
            if not line.startswith("| ") or "`" not in line:
                continue
            command = line.split("`", 2)[1]
            words = command.split()
            expected.add(tuple(word for word in words[:2]
                               if re.fullmatch(r"[a-z][a-z-]*", word)))
        self.assertEqual(len(expected), 26)

        def leaves(current, prefix=()):
            subparsers = [a for a in current._actions
                          if isinstance(a, argparse._SubParsersAction)]
            if not subparsers:
                return {prefix}
            return set().union(*(
                leaves(child, (*prefix, name))
                for name, child in subparsers[0].choices.items()
            ))

        self.assertEqual(leaves(parser()), expected)

    def test_command_options_equal_specification_table(self) -> None:
        spec = (PROJECT_ROOT / "docs/agent-squad-spec.md").read_text()
        table = spec.split("### 10.2 Command table\n", 1)[1].split(
            "### 10.3", 1)[0]
        checked = 0
        for line in table.splitlines():
            if not line.startswith("| ") or "`" not in line:
                continue
            command = line.split("`", 2)[1]
            current = parser()
            for word in command.split()[:2]:
                if not re.fullmatch(r"[a-z][a-z-]*", word):
                    break
                current = next(
                    a for a in current._actions
                    if isinstance(a, argparse._SubParsersAction)
                ).choices[word]
            options = {
                o for a in current._actions for o in a.option_strings
                if o.startswith("--")
            }
            # §10.1 gives every command --json and the reads an optional --as.
            with self.subTest(command=command):
                self.assertEqual(
                    options - {"--help", "--json", "--as"},
                    set(re.findall(r"--[a-z][a-z-]*", command))
                    - {"--json", "--as"},
                )
            checked += 1
        self.assertEqual(checked, 26)

    def test_release_and_protocol_versions(self) -> None:
        # The one deliberate pin; a release edits it with __version__.
        self.assertEqual(__version__, "0.6.1")
        self.assertEqual(PROTOCOL_VERSION, "0.5.0")
        self.assertEqual(TAG, f"AGENT_SQUAD/{PROTOCOL_VERSION}")

    def test_documents_state_package_version(self) -> None:
        # Each pattern must match exactly once, so a reworded sentence
        # fails instead of passing silently.
        statements = [
            ("README.md", r"package\s+version\s+is\s+`([^`]+)`"),
            ("README.md", r"The\s+version\s+is\s+`([^`]+)`"),
            ("docs/workflow-verification.md",
             r"require\s+package\s+metadata\s+`([^`]+)`"),
            ("docs/agent-squad-spec.md",
             r"(?m)^- \*\*Package version:\*\* (\S+)$"),
            ("docs/agent-squad-spec.md",
             r"baseline\s+for\s+package\s+version\s+`([^`]+)`"),
        ]
        for path, pattern in statements:
            text = (PROJECT_ROOT / path).read_text(encoding="utf-8")
            with self.subTest(path=path, pattern=pattern):
                self.assertEqual(re.findall(pattern, text), [__version__])
