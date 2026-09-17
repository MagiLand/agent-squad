"""Boundary cases for prerequisite probes and installed skill validation."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests._support import PROJECT_ROOT, add_src_to_path

add_src_to_path()

from agent_squad.doctor import check_code_review, writable_directory
from agent_squad.herdr import HerdrClient, HerdrError
from agent_squad.initialization import AgentSquadError


class DirectoryProbeTests(unittest.TestCase):
    def test_missing_ancestors_are_removed_after_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "new/nested"
            with patch("agent_squad.doctor.tempfile.TemporaryFile",
                       side_effect=PermissionError("read-only")):
                with self.assertRaises(PermissionError):
                    with writable_directory(root):
                        self.fail("write failure must prevent use")
            self.assertEqual(list(parent.iterdir()), [])

    def test_existing_root_and_unexpected_new_data_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            with writable_directory(parent):
                pass
            self.assertTrue(parent.is_dir())
            root = parent / "new"
            with self.assertRaisesRegex(AgentSquadError, "retained"):
                with writable_directory(root):
                    (root / "keep").write_text("user data")
            self.assertEqual((root / "keep").read_text(), "user data")

    def test_creation_failure_never_deletes_the_existing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            with patch.object(Path, "mkdir", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    with writable_directory(parent / "new"):
                        self.fail("creation failure must prevent use")
            self.assertTrue(parent.is_dir())


class CodeReviewPrerequisiteTests(unittest.TestCase):
    def test_name_requires_closed_frontmatter_and_one_scalar(self) -> None:
        good = (
            "---\nname: code-review\n---\nbody",
            "---\nname: 'code-review' # comment\n---\n",
            '---\r\nname: "code-review"\r\n...\r\n',
        )
        bad = (
            "name: code-review\n",
            "---\nname: code-review\n",
            "---\nname: wrong\n---\nname: code-review\n",
            "---\nother:\n  name: code-review\n---\n",
            "---\nname: code-review\nname: wrong\n---\n",
            "---\nname: code-review-extra\n---\n",
        )
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / ".agents/skills/code-review/SKILL.md"
            path.parent.mkdir(parents=True)
            for content in good:
                with self.subTest(content=content):
                    path.write_text(content)
                    check_code_review(home)
            for content in bad:
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(AgentSquadError):
                        check_code_review(home)


class HerdrSchemaPrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("FAKE_HERDR_")}
        result = subprocess.run(
            [sys.executable,
             str(PROJECT_ROOT / "tests/fixtures/fake_herdr.py"),
             "api", "schema", "--json"],
            env=env, capture_output=True, text=True, check=True, timeout=10,
            shell=False,
        )
        self.schema = json.loads(result.stdout)
        self.client = HerdrClient(PROJECT_ROOT)

    def test_trial_issue42_wrong_response_definition_name(self) -> None:
        self.client._validate_schema_contract(self.schema)
        definitions = self.schema["schemas"]["success_response"]["$defs"]
        definitions["WrongResult"] = definitions.pop("ResponseResult")
        with self.assertRaisesRegex(HerdrError, "response-result schema"):
            self.client._validate_schema_contract(self.schema)

    def test_all_required_parameter_fields_must_be_advertised(self) -> None:
        definitions = self.schema["schemas"]["request"]["$defs"]
        for name, fields in self.client._REQUIRED_PARAMETER_FIELDS.items():
            for field in fields:
                with self.subTest(definition=name, field=field):
                    value = definitions[name]["properties"].pop(field)
                    with self.assertRaisesRegex(HerdrError, "lacks"):
                        self.client._validate_schema_contract(self.schema)
                    definitions[name]["properties"][field] = value
