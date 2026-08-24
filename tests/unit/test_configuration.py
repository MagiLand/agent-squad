from __future__ import annotations

from pathlib import Path
import unittest

from tests._support import add_src_to_path


add_src_to_path()

from agent_squad.initialization import (  # noqa: E402
    Configuration,
    ConfigurationError,
    default_configuration,
)


class ConfigurationTests(unittest.TestCase):
    def test_default_configuration_is_valid_and_has_no_cleanup_allowlist(self) -> None:
        review_root = Path("/tmp/agent-squad-reviews")

        configuration = default_configuration(review_root)

        self.assertEqual(configuration.schema_version, 1)
        self.assertEqual(configuration.implementer.agent_name, "codex-main")
        self.assertEqual(configuration.implementer.kind, "codex")
        self.assertEqual(configuration.reviewer.kind, "claude")
        self.assertEqual(configuration.review_worktree_root, review_root)
        self.assertEqual(configuration.max_completed_change_reviews, 4)
        self.assertEqual(configuration.allowed_generated_paths, ())

    def test_configuration_rejects_unknown_fields(self) -> None:
        data = default_configuration(Path("/tmp/reviews")).to_dict()
        data["unexpected"] = True

        with self.assertRaisesRegex(ConfigurationError, "unknown field.*unexpected"):
            Configuration.from_dict(data)

    def test_configuration_rejects_relative_review_root(self) -> None:
        data = default_configuration(Path("/tmp/reviews")).to_dict()
        data["review_worktree_root"] = "relative/reviews"

        with self.assertRaisesRegex(ConfigurationError, "must be an absolute path"):
            Configuration.from_dict(data)

    def test_configuration_rejects_boolean_review_limit(self) -> None:
        data = default_configuration(Path("/tmp/reviews")).to_dict()
        data["max_completed_change_reviews"] = True

        with self.assertRaisesRegex(ConfigurationError, "positive integer"):
            Configuration.from_dict(data)

    def test_configuration_rejects_repository_root_cleanup_allowance(self) -> None:
        data = default_configuration(Path("/tmp/reviews")).to_dict()
        data["allowed_generated_paths"] = ["."]

        with self.assertRaisesRegex(ConfigurationError, "narrow repository-relative"):
            Configuration.from_dict(data)


if __name__ == "__main__":
    unittest.main()
