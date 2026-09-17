"""The release exposes the command names in the approved delta's table."""

import argparse
import re
import unittest

from tests._support import PROJECT_ROOT, add_src_to_path

add_src_to_path()

from agent_squad import __version__
from agent_squad.cli import parser
from agent_squad.conventions import TAG


class CliSurfaceTests(unittest.TestCase):
    def test_command_surface_equals_specification_table(self) -> None:
        spec = (PROJECT_ROOT / "docs/agent-squad-v0.5.0-spec.md").read_text()
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
        self.assertEqual(len(expected), 23)

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

    def test_release_and_protocol_versions(self) -> None:
        self.assertEqual(__version__, "0.5.0")
        self.assertEqual(TAG, "AGENT_SQUAD/0.5.0")
