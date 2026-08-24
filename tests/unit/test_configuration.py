from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad.initialization import (  # noqa: E402
    AgentKind,
    Configuration,
    ConfigurationError,
    default_configuration,
    load_configuration,
)


def _configuration_data() -> dict[str, object]:
    return default_configuration(Path("/tmp/reviews")).to_dict()


def _object_at(
    data: dict[str, object],
    path: tuple[str, ...],
) -> dict[str, object]:
    target = data
    for component in path:
        nested = target[component]
        if not isinstance(nested, dict):
            raise AssertionError(f"test path is not an object: {path}")
        target = nested
    return target


def _set_value(
    data: dict[str, object],
    path: tuple[str, ...],
    value: object,
) -> None:
    target = _object_at(data, path[:-1])
    target[path[-1]] = value


class ConfigurationTests(unittest.TestCase):
    def test_default_configuration_is_valid(self) -> None:
        review_root = Path("/tmp/agent-squad-reviews")

        configuration = default_configuration(review_root)

        self.assertEqual(configuration.schema_version, 1)
        self.assertEqual(configuration.implementer.agent_name, "codex-main")
        self.assertIs(configuration.implementer.kind, AgentKind.CODEX)
        self.assertIs(configuration.reviewer.kind, AgentKind.CLAUDE)
        self.assertEqual(configuration.review_worktree_root, review_root)
        self.assertEqual(configuration.max_completed_change_reviews, 4)
        self.assertEqual(configuration.allowed_generated_paths, ())

    def test_agent_name_has_no_extra_format_restriction(self) -> None:
        data = _configuration_data()
        name = "Codex Agent " + ("x" * 40)
        _set_value(data, ("implementer", "agent_name"), name)

        configuration = Configuration.from_dict(data)

        self.assertEqual(configuration.implementer.agent_name, name)

    def test_optional_fields_may_be_omitted(self) -> None:
        data = _configuration_data()
        del data["allowed_generated_paths"]
        reviewer = _object_at(data, ("reviewer",))
        del reviewer["start_args"]

        configuration = Configuration.from_dict(data)

        self.assertEqual(configuration.reviewer.start_args, ())
        self.assertEqual(configuration.allowed_generated_paths, ())

    def test_configuration_rejects_non_object_roots(self) -> None:
        invalid_roots: tuple[object, ...] = ([], {1: "not a string key"})

        for value in invalid_roots:
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ConfigurationError,
                    "must be a JSON object",
                ):
                    Configuration.from_dict(value)

    def test_configuration_rejects_missing_fields(self) -> None:
        cases = (
            ((), "base_ref", "configuration.*missing required field"),
            (
                ("implementer",),
                "agent_name",
                "configuration.implementer.*missing required field",
            ),
            (
                ("reviewer",),
                "kind",
                "configuration.reviewer.*missing required field",
            ),
        )

        for parent_path, field, pattern in cases:
            with self.subTest(parent_path=parent_path, field=field):
                data = _configuration_data()
                target = _object_at(data, parent_path)
                del target[field]
                with self.assertRaisesRegex(ConfigurationError, pattern):
                    Configuration.from_dict(data)

    def test_configuration_rejects_unknown_fields(self) -> None:
        cases = (
            ((), "configuration"),
            (("implementer",), "configuration.implementer"),
            (("reviewer",), "configuration.reviewer"),
        )

        for parent_path, label in cases:
            with self.subTest(parent_path=parent_path):
                data = _configuration_data()
                target = _object_at(data, parent_path)
                target["unexpected"] = True
                pattern = rf"{label} has unknown field: unexpected"
                with self.assertRaisesRegex(ConfigurationError, pattern):
                    Configuration.from_dict(data)

    def test_configuration_rejects_invalid_values(self) -> None:
        cases = (
            (("schema_version",), 2, "schema_version must be 1"),
            (
                ("schema_version",),
                True,
                "schema_version must be an integer",
            ),
            (("implementer",), [], "implementer must be a JSON object"),
            (("reviewer",), [], "reviewer must be a JSON object"),
            (
                ("implementer", "agent_name"),
                "",
                "agent_name must be a non-empty string",
            ),
            (
                ("implementer", "agent_name"),
                1,
                "agent_name must be a non-empty string",
            ),
            (("implementer", "kind"), "other", "kind must be one of"),
            (("reviewer", "kind"), "other", "kind must be one of"),
            (
                ("reviewer", "start_args"),
                "--flag",
                "start_args must be a JSON array",
            ),
            (
                ("reviewer", "start_args"),
                [1],
                r"start_args\[0\] must be a non-empty string",
            ),
            (
                ("reviewer", "start_args"),
                [""],
                r"start_args\[0\] must be a non-empty string",
            ),
            (
                ("reviewer", "start_args"),
                ["bad\x00argument"],
                r"start_args\[0\] must not contain null bytes",
            ),
            (("base_ref",), "", "base_ref must be a non-empty string"),
            (("base_ref",), 1, "base_ref must be a non-empty string"),
            (("base_ref",), " main", "base_ref must not contain"),
            (("base_ref",), "main\x00", "base_ref must not contain"),
            (
                ("review_worktree_root",),
                "",
                "review_worktree_root must be a non-empty string",
            ),
            (
                ("review_worktree_root",),
                1,
                "review_worktree_root must be a non-empty string",
            ),
            (
                ("review_worktree_root",),
                "/tmp/bad\x00root",
                "review_worktree_root must not contain null bytes",
            ),
            (
                ("review_worktree_root",),
                "relative/reviews",
                "review_worktree_root must be an absolute path",
            ),
            (
                ("max_completed_change_reviews",),
                True,
                "max_completed_change_reviews must be an integer",
            ),
            (
                ("max_completed_change_reviews",),
                0,
                "max_completed_change_reviews must be a positive integer",
            ),
            (
                ("allowed_generated_paths",),
                "generated",
                "allowed_generated_paths must be a JSON array",
            ),
            (
                ("allowed_generated_paths",),
                [1],
                r"allowed_generated_paths\[0\] must be a non-empty string",
            ),
            (
                ("allowed_generated_paths",),
                [""],
                r"allowed_generated_paths\[0\] must be a non-empty string",
            ),
            (
                ("allowed_generated_paths",),
                ["bad\x00path"],
                r"allowed_generated_paths\[0\] must not contain null bytes",
            ),
            (
                ("allowed_generated_paths",),
                ["."],
                "narrow repository-relative",
            ),
            (
                ("allowed_generated_paths",),
                ["/tmp"],
                "narrow repository-relative",
            ),
            (
                ("allowed_generated_paths",),
                ["generated/../outside"],
                "narrow repository-relative",
            ),
            (
                ("allowed_generated_paths",),
                ["generated/log", "generated//log"],
                "contains duplicate path",
            ),
        )

        for path, value, pattern in cases:
            with self.subTest(path=path, value=value):
                data = _configuration_data()
                _set_value(data, path, value)
                with self.assertRaisesRegex(ConfigurationError, pattern):
                    Configuration.from_dict(data)

    def test_load_configuration_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_text(
                '{"schema_version":1,"schema_version":1}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "duplicate object key: schema_version",
            ):
                load_configuration(path)

    def test_load_configuration_rejects_non_utf8_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.json"
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(ConfigurationError, "UTF-8 JSON"):
                load_configuration(path)

    def test_load_configuration_reports_read_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "missing.json"

            with self.assertRaisesRegex(ConfigurationError, "cannot read"):
                load_configuration(path)

    def test_configuration_serializes_to_json(self) -> None:
        configuration = default_configuration(Path("/tmp/reviews"))

        encoded = json.dumps(configuration.to_dict())

        self.assertIn('"kind": "codex"', encoded)


if __name__ == "__main__":
    unittest.main()
