"""Schema types, defaults, root boundaries, and atomic configuration writes."""

import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.unit.test_conventions import config
from agent_squad.initialization import (
    Configuration,
    ConfigurationError,
    Repository,
    Worktree,
    atomic_config,
    decode_json,
    initialize_repository,
    remote_coordinates,
    validate_roots,
)


class ConfigurationTests(unittest.TestCase):
    def test_init_validates_both_token_files_before_git_network_or_api(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            primary = root / "primary"
            primary.mkdir()
            repository = Repository(primary, primary, primary / ".git")
            token = root / "private.token"
            token.write_text("synthetic-token\n")
            token.chmod(0o600)

            def local_git(root, *args):
                self.assertEqual(args, ("remote", "get-url", "origin"))
                return "git@unknown-alias:prefix/Owner/repository.git"

            with (
                patch("agent_squad.initialization.discover_git_worktree",
                      return_value=repository),
                patch("agent_squad.initialization.git_output",
                      side_effect=local_git),
                patch("agent_squad.initialization.list_worktrees",
                      return_value=(Worktree(primary, "a" * 40, "main"),)),
                patch("agent_squad.forgejo.list_worktrees",
                      return_value=(Worktree(primary, "a" * 40, "main"),)),
                patch("agent_squad.forge.make_forge") as factory,
            ):
                for role in ("implementer", "reviewer"):
                    with self.subTest(role=role):
                        paths = {"implementer_token_file": str(token),
                                 "reviewer_token_file": str(token)}
                        paths[role + "_token_file"] = str(root / "missing")
                        with self.assertRaisesRegex(ConfigurationError, role):
                            initialize_repository(
                                primary, implementer_account="dev",
                                reviewer_account="reviewer", forge="forgejo",
                                base_url="https://forge.example/prefix/",
                                **paths,
                            )
                        factory.assert_not_called()
                        self.assertFalse(repository.control_root.exists())

    def test_schema_round_trip_and_optional_developer_accounts(self) -> None:
        value = config().to_dict()
        self.assertEqual(Configuration.from_dict(value), config())
        value.pop("developer_accounts")
        self.assertEqual(Configuration.from_dict(value).developer_accounts, ())

    def test_unknown_fields_wrong_types_and_limits(self) -> None:
        mutations = [
            ("schema_version", True),
            ("schema_version", 3),
            ("max_review_passes", 0),
            ("max_review_passes", True),
            ("max_review_passes", "3"),
            ("merge_method", "rebase"),
            ("base_branch", "foo..bar"),
            ("base_branch", "two words"),
            ("scratch_root", ""),
            ("worktree_root", "bad\x00path"),
            ("developer_accounts", "dev"),
            ("developer_accounts", ["reviewer"]),
            ("developer_accounts", [None]),
        ]
        for key, value in mutations:
            with (
                self.subTest(key=key, value=value),
                self.assertRaises(ConfigurationError),
            ):
                data = config().to_dict()
                data[key] = value
                Configuration.from_dict(data)
        for parent in (None, "forge", "implementer", "reviewer"):
            with (
                self.subTest(parent=parent),
                self.assertRaises(ConfigurationError),
            ):
                data = config().to_dict()
                (data if parent is None else data[parent])["unknown"] = True
                Configuration.from_dict(data)
        for group, key, value in [
            ("forge", "kind", "unsupported"),
            ("forge", "owner", "a/b"),
            ("forge", "repo", "white space"),
            ("forge", "owner", 1),
            ("implementer", "kind", "other"),
            ("implementer", "agent_name", "Upper"),
            ("reviewer", "forge_account", "DEV"),
            ("reviewer", "start_args", [None]),
            ("reviewer", "start_args", ["bad\x00arg"]),
        ]:
            with (
                self.subTest(group=group, key=key, value=value),
                self.assertRaises(ConfigurationError),
            ):
                data = config().to_dict()
                data[group][key] = value
                Configuration.from_dict(data)

    def test_schema_one_refuses_migration(self) -> None:
        with self.assertRaisesRegex(
            ConfigurationError, "move config.json aside"
        ):
            Configuration.from_dict({"schema_version": 1})

    def test_remote_coordinates(self) -> None:
        for url in [
            "git@github.com:Owner/repo.git",
            "git@github-alias:Owner/repo.git",
            "ssh://git@github-alias/Owner/repo.git",
            "https://github.com/Owner/repo.git",
            "https://github.com/prefix/Owner/repo",
            "/fixture/Owner/repo.git",
        ]:
            with self.subTest(url=url):
                self.assertEqual(remote_coordinates(url), ("Owner", "repo"))
        with self.assertRaises(ConfigurationError):
            remote_coordinates("nopath")

    def test_root_boundaries_overlap_and_writability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            primary = Path(temporary).resolve() / "primary"
            primary.mkdir()
            sibling = Path(temporary).resolve() / "linked"
            sibling.mkdir()
            repo = Repository(primary, primary, primary / ".git", config())
            trees = (
                Worktree(primary, "a" * 40, "refs/heads/main"),
                Worktree(sibling, "a" * 40, None),
            )
            with patch(
                "agent_squad.initialization.list_worktrees", return_value=trees
            ):
                validate_roots(repo, writable=True)
                for first, second in [
                    (".agent-squad/worktrees", ".agent-squad/worktrees/sub"),
                    (".agent-squad", ".agent-squad/scratch"),
                    ("unexcluded", ".agent-squad/scratch"),
                    (str(sibling / "unexcluded"), ".agent-squad/scratch"),
                ]:
                    with (
                        self.subTest(first=first, second=second),
                        self.assertRaises(ConfigurationError),
                    ):
                        validate_roots(
                            replace(
                                repo,
                                configuration=replace(
                                    config(),
                                    worktree_root=first,
                                    scratch_root=second,
                                ),
                            )
                        )
                external = Path(temporary) / "external"
                validate_roots(
                    replace(
                        repo,
                        configuration=replace(
                            config(), worktree_root=str(external)
                        ),
                    ),
                    writable=True,
                )
                self.assertTrue(external.is_dir())
                (primary / ".agent-squad/scratch-alias").symlink_to(
                    primary / ".agent-squad/worktrees",
                    target_is_directory=True,
                )
                with self.assertRaises(ConfigurationError):
                    validate_roots(
                        replace(
                            repo,
                            configuration=replace(
                                config(),
                                scratch_root=".agent-squad/scratch-alias",
                            ),
                        )
                    )

    def test_atomic_config_preserves_existing_and_cleans_temporary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            with patch(
                "agent_squad.initialization.os.replace",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    atomic_config(path, config())
            self.assertEqual(list(path.parent.iterdir()), [])
            atomic_config(path, config())
            original = path.read_bytes()
            with self.assertRaises(ConfigurationError):
                atomic_config(path, replace(config(), max_review_passes=5))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_values(
        self,
    ) -> None:
        for text in ['{"a": 1, "a": 2}', '{"a": NaN}', '{"a": Infinity}']:
            with self.assertRaises(ValueError):
                decode_json(text)


class LegacyIdentityKeyTests(unittest.TestCase):
    """v0.6.x wrote identity_mode and approver_accounts into every config."""

    REMOVED = "single-identity mode was removed in v0.7.0"

    def test_v061_dual_configuration_loads_unchanged(self) -> None:
        for legacy in (
            {"identity_mode": "dual", "approver_accounts": []},
            {"identity_mode": "dual"},
            {"approver_accounts": []},
        ):
            with self.subTest(legacy=legacy):
                data = config().to_dict()
                data.update(legacy)
                loaded = Configuration.from_dict(data)
                self.assertEqual(loaded, config())
                self.assertNotIn("identity_mode", loaded.to_dict())
                self.assertNotIn("approver_accounts", loaded.to_dict())

    def test_single_identity_values_are_refused_with_one_message(
        self,
    ) -> None:
        single = config().to_dict()
        single.update(identity_mode="single", approver_accounts=["human"])
        single["reviewer"]["forge_account"] = "DEV"
        values = [("identity_mode", value) for value in (
            "single", "unknown", "Dual", None, True, [],
        )] + [("approver_accounts", value) for value in (
            ["human"], ["dev"], [""], "human", None,
        )]
        for key, value in values:
            with self.subTest(key=key, value=value):
                data = config().to_dict()
                data[key] = value
                with self.assertRaisesRegex(ConfigurationError, (
                    self.REMOVED + "; move config.json aside and rerun"
                    " agent-squad init with two accounts$"
                )):
                    Configuration.from_dict(data)
        with self.assertRaisesRegex(ConfigurationError, self.REMOVED):
            Configuration.from_dict(single)

    def test_equal_role_accounts_are_refused(self) -> None:
        data = config().to_dict()
        data["reviewer"]["forge_account"] = "DEV"
        with self.assertRaisesRegex(
            ConfigurationError,
            "^Implementer and Reviewer accounts must differ$",
        ):
            Configuration.from_dict(data)
