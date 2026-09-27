"""Recorded read mappings, bounded HTTP transport, and private token files."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from tests._support import add_src_to_path
from tests.unit.test_conventions import config

add_src_to_path()

from tests.fixtures.fake_forgejo import recording
from agent_squad.initialization import (
    Configuration,
    ConfigurationError,
    Repository,
    Worktree,
)
from agent_squad.forgejo import (
    Forgejo,
    hunk_anchor,
    parse_comments,
    parse_evidence,
    parse_pullrequest,
    parse_review,
    read_token,
)
from agent_squad.forge import ForgeError


def forgejo_config():
    data = config().to_dict()
    data["forge"].update(
        kind="forgejo", base_url="https://forge.example/sub/path"
    )
    for role in ("implementer", "reviewer"):
        data[role]["token_file"] = "/outside/" + role + ".token"
    return Configuration.from_dict(data)


class URLConfigurationTests(unittest.TestCase):
    def test_valid_urls_and_kind_specific_fields(self) -> None:
        for url in (
            "https://forge.example/sub/path",
            "http://127.0.0.1:1234",
            "http://localhost:1234",
            "http://[::1]:1234",
        ):
            with self.subTest(url=url):
                data = forgejo_config().to_dict()
                data["forge"]["base_url"] = url
                self.assertEqual(
                    Configuration.from_dict(data).forge.base_url, url
                )
        for group, key in (
            ("forge", "base_url"),
            ("implementer", "token_file"),
            ("reviewer", "token_file"),
        ):
            with self.subTest(group=group):
                data = forgejo_config().to_dict()
                del data[group][key]
                with self.assertRaisesRegex(ConfigurationError, key):
                    Configuration.from_dict(data)
                data = config().to_dict()
                for value in ("/outside/token", None):
                    data[group][key] = value
                    with self.assertRaisesRegex(ConfigurationError, key):
                        Configuration.from_dict(data)

    def test_refuses_unsafe_or_malformed_base_urls(self) -> None:
        for url in (
            "http://forge.example",
            "http://127.0.0.2",
            "http://localhost.evil",
            "https://forge.example?x=1",
            "https://forge.example#anchor",
            "https://forge.example?",
            "https://forge.example#",
            "/relative",
            "https://user:private@forge.example",
            "ftp://localhost",
            "https://",
            "https://forge.example:bad",
            "https://forge.example:99999",
            "https://forge.example\n",
            "https://forge.example\\evil",
        ):
            with self.subTest(url=url):
                data = forgejo_config().to_dict()
                data["forge"]["base_url"] = url
                with self.assertRaisesRegex(
                    ConfigurationError,
                    "forge.base_url",
                ) as caught:
                    Configuration.from_dict(data)
                self.assertNotIn("private", str(caught.exception))

    def test_single_mode_may_share_one_token_file(self) -> None:
        data = forgejo_config().to_dict()
        data.update(identity_mode="single", approver_accounts=["human"])
        data["reviewer"].update(
            forge_account=data["implementer"]["forge_account"],
            token_file=data["implementer"]["token_file"],
        )
        self.assertEqual(Configuration.from_dict(data).identity_mode, "single")


class TokenFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.worktree = self.root / "checkout"
        self.worktree.mkdir()
        self.token = self.root / "private.token"
        self.token.write_text("synthetic-token\n")
        self.token.chmod(0o600)
        self.repo = Repository(
            self.worktree,
            self.worktree,
            self.worktree / ".git",
            forgejo_config(),
        )
        trees = patch(
            "agent_squad.forgejo.list_worktrees",
            return_value=(Worktree(self.worktree, "a" * 40, "main"),),
        )
        self.trees = trees.start()
        self.addCleanup(trees.stop)

    def read(self, path: Path | str | None = None) -> str:
        cfg = self.repo.configuration
        repo = replace(
            self.repo,
            configuration=replace(
                cfg,
                implementer=replace(
                    cfg.implementer, token_file=str(path or self.token)
                ),
            ),
        )
        return read_token(repo, "implementer")

    def test_private_modes_and_revalidation_on_each_read(self) -> None:
        for mode in (0o600, 0o400):
            self.token.chmod(mode)
            self.assertEqual(self.read(), "synthetic-token")
        for mode in (0o640, 0o604):
            self.token.chmod(mode)
            with self.assertRaisesRegex(
                ForgeError,
                "no group or other permissions",
            ):
                self.read()
        self.token.chmod(0o600)
        self.token.write_text("changed-token")
        self.assertEqual(self.read(), "changed-token")
        self.trees.return_value += (Worktree(self.root, "a" * 40, None),)
        with self.assertRaisesRegex(
            ForgeError,
            "outside repository worktrees",
        ):
            self.read()

    def test_refuses_paths_and_aliases_inside_any_worktree(self) -> None:
        inside = self.worktree / "secret"
        inside.write_text("synthetic-token")
        inside.chmod(0o600)
        link = self.root / "symlink"
        link.symlink_to(self.token)
        alias = self.root / "alias"
        alias.symlink_to(self.worktree, target_is_directory=True)
        for path in (
            "relative",
            self.root / "missing",
            self.root,
            link,
            inside,
            alias / "secret",
        ):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(
                    ForgeError,
                    "absolute regular file outside repository worktrees",
                ),
            ):
                self.read(path)
        second = self.root / "linked"
        second.mkdir()
        file = second / "token"
        file.write_text("synthetic-token")
        file.chmod(0o600)
        self.trees.return_value += (Worktree(second, "a" * 40, None),)
        with self.assertRaisesRegex(
            ForgeError,
            "outside repository worktrees",
        ):
            self.read(file)

    def test_refuses_empty_multiline_and_nonregular_files_without_contents(
        self,
    ) -> None:
        for content in (
            "",
            "\n",
            " \n",
            "private\nsecond\n",
            "private\n\n",
            "private\x00",
        ):
            self.token.write_text(content)
            with self.assertRaisesRegex(
                ForgeError,
                "one non-empty line",
            ) as caught:
                self.read()
            self.assertNotIn("private", str(caught.exception))
        import os

        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(ForgeError, "absolute regular file"):
            self.read(fifo)


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path("/repository")
        self.forge = Forgejo(
            Repository(root, root, root / ".git", forgejo_config()),
            "implementer",
            timeout=0.25,
        )
        token = patch(
            "agent_squad.forgejo.read_token", return_value="synthetic-secret"
        )
        self.read_token = token.start()
        self.addCleanup(token.stop)
        self.open = Mock()
        self.forge._opener.open = self.open

    @staticmethod
    def response(value):
        return BytesIO(json.dumps(value).encode())

    def test_path_prefix_headers_encoding_timeout_and_reads_skip_verification(
        self,
    ) -> None:
        self.open.return_value = self.response({"login": "human/name"})
        self.assertTrue(self.forge.user_exists("human/name"))
        request = self.open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://forge.example/sub/path/api/v1/users/human%2Fname",
        )
        self.assertEqual(
            request.get_header("Authorization"), "token synthetic-secret"
        )
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(self.open.call_args.kwargs["timeout"], 0.25)
        self.assertEqual(self.open.call_count, 1)

    def test_http_status_message_and_redaction_on_captured_failure(
        self,
    ) -> None:
        for status in (401, 403, 404, 409, 422):
            message = 'Exact message: "unchanged".'
            self.open.side_effect = HTTPError(
                "url",
                status,
                "reason",
                {},
                self.response({"message": message}),
            )
            with (
                self.subTest(status=status),
                self.assertRaises(ForgeError) as caught,
            ):
                self.forge.api("/user")
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(str(caught.exception), message)
        self.open.side_effect = HTTPError(
            "url",
            401,
            "reason",
            {},
            self.response({"message": "bad synthetic-secret\nsecond line"}),
        )
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(ForgeError) as caught:
                self.forge.api("/user")
        self.assertEqual(str(caught.exception), "bad [redacted] second line")
        self.assertNotIn(
            "synthetic-secret",
            str(caught.exception) + stdout.getvalue() + stderr.getvalue(),
        )

    def test_timeout_invalid_json_and_transport_errors_never_leak_token(
        self,
    ) -> None:
        for error in (
            TimeoutError("synthetic-secret"),
            ValueError("synthetic-secret"),
        ):
            self.open.side_effect = error
            with self.assertRaisesRegex(
                ForgeError,
                "failed or timed out",
            ) as caught:
                self.forge.api("/user")
            self.assertNotIn("synthetic-secret", str(caught.exception))
        self.open.side_effect = None
        for value in (
            b"bad json synthetic-secret",
            b'{"synthetic-secret":1,"synthetic-secret":2}',
            b"\xff",
        ):
            self.open.return_value = BytesIO(value)
            with self.assertRaisesRegex(ForgeError, "invalid JSON"):
                self.forge.api("/user")

    def test_mutation_verifies_once_checks_floor_and_never_retries(
        self,
    ) -> None:
        self.open.side_effect = [
            self.response({"login": "DEV"}),
            self.response({"version": "16.0.3"}),
            HTTPError(
                "url",
                409,
                "reason",
                {},
                self.response({"message": "head changed"}),
            ),
            self.response({"ok": True}),
        ]
        with self.assertRaises(ForgeError) as caught:
            self.forge.api("/mutation", method="POST", body={"value": 1})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.open.call_count, 3)
        self.forge.api("/mutation", method="POST", body={})
        self.assertEqual(self.open.call_count, 4)
        self.assertEqual(
            [c.args[0].get_method() for c in self.open.call_args_list],
            ["GET", "GET", "POST", "POST"],
        )
        self.assertEqual(self.read_token.call_count, 2)

    def test_identity_mismatch_and_old_version_prevent_mutation(self) -> None:
        for responses, message in (
            ([{"login": "wrong"}], "does not match"),
            ([{"login": "dev"}, {"version": "15.0.9"}], "16.0.0"),
        ):
            with self.subTest(message=message):
                self.forge._verified_token = None
                self.open.reset_mock()
                self.open.side_effect = [self.response(r) for r in responses]
                with self.assertRaisesRegex(ForgeError, message):
                    self.forge.api("/mutation", method="POST", body={})
                self.assertTrue(
                    all(
                        c.args[0].get_method() == "GET"
                        for c in self.open.call_args_list
                    )
                )

    def test_version_numeric_comparison_and_development_suffix(self) -> None:
        for version, accepted in (
            ("16.0.3", True),
            ("16.0.0", True),
            ("15.0.9", False),
            ("16.0.0-dev-753+gitea-1.22.0", True),
            ("9.99.99", False),
            ("100.0.0", True),
        ):
            with self.subTest(version=version):
                self.open.return_value = self.response({"version": version})
                if accepted:
                    self.assertEqual(self.forge.version(), version)
                else:
                    with self.assertRaisesRegex(ForgeError, "at least 16.0.0"):
                        self.forge.version()

    def test_pagination_uses_fifty_and_stops_on_short_page(self) -> None:
        self.open.side_effect = [
            self.response(list(range(50))),
            self.response([50, 51]),
        ]
        self.assertEqual(
            self.forge.listing("/reviews?state=all"), list(range(52))
        )
        self.assertEqual(
            [
                c.args[0].full_url.rsplit("/", 1)[1]
                for c in self.open.call_args_list
            ],
            [
                "reviews?state=all&limit=50&page=1",
                "reviews?state=all&limit=50&page=2",
            ],
        )

    def test_branch_rule_visibility_and_repository_push_permission(
        self,
    ) -> None:
        for status in (403, 404, 500):
            self.open.side_effect = HTTPError(
                "url",
                status,
                "reason",
                {},
                self.response({"message": "unavailable"}),
            )
            if status == 500:
                with self.assertRaises(ForgeError):
                    self.forge.branch_rules("main")
            else:
                self.assertEqual(
                    self.forge.branch_rules("main"),
                    {"visibility": "not visible"},
                )
        self.open.side_effect = None
        value = recording("009-setup-repository-author.json")
        value["full_name"] = "org/repo"
        for permission, expected in (
            ({"admin": False, "push": True, "pull": True}, "write"),
            ({"admin": True, "push": True, "pull": True}, "admin"),
            ({"admin": False, "push": False, "pull": True}, "read"),
        ):
            value["permissions"] = permission
            self.open.return_value = self.response(value)
            self.assertEqual(self.forge.repository_permission(), expected)


class RecordedParserTests(unittest.TestCase):
    def test_request_review_is_skipped_before_empty_sha_validation(
        self,
    ) -> None:
        row = recording("015-setup-requested-reviews.json")[0]
        self.assertEqual(row["commit_id"], "")
        self.assertIsNone(parse_review(row))

    def test_states_dismissal_and_ignored_flags(self) -> None:
        rows = recording("059-e9-after-new-approval.json")
        self.assertTrue(any(r["dismissed"] for r in rows))
        for row in rows:
            if row["state"] == "REQUEST_REVIEW":
                continue
            parsed = parse_review(row)
            changed = dict(
                row, stale=not row["stale"], official=not row["official"]
            )
            self.assertEqual(parse_review(changed), parsed)
            self.assertEqual(parsed.dismissed, row["dismissed"])
        template = recording("022-e2-body-first.json")
        for wire, neutral in (
            ("APPROVED", "approved"),
            ("REQUEST_CHANGES", "changes_requested"),
            ("COMMENT", "commented"),
            ("PENDING", "pending"),
        ):
            self.assertEqual(
                parse_review(dict(template, state=wire)).state, neutral
            )
        with self.assertRaisesRegex(ForgeError, "unknown review state"):
            parse_review(dict(template, state="UNEXPECTED"))

    def test_three_recorded_orders_sort_by_id(self) -> None:
        expected = None
        for i in (1, 2, 3):
            rows = recording(f"{18+i:03d}-e1-comments-order-{i}.json")
            # The live reads were identical; reversal is deliberately
            # synthetic.
            result, _ = parse_comments(list(reversed(rows)), 2)
            self.assertEqual([c.evidence.id for c in result], [3, 4])
            if expected is None:
                expected = result
            self.assertEqual(result, expected)

    def test_empty_hunk_is_unanchored_and_position_is_never_a_line(
        self,
    ) -> None:
        rows = recording("028-e3-comments.json")
        comments, _ = parse_comments(rows, 4)
        self.assertTrue(all(c.line is None for c in comments))
        rows = recording("026-e2-comments.json")
        for row in rows:
            row["position"] = 987654
        comments, _ = parse_comments(rows, 3)
        self.assertEqual([c.line for c in comments], [25, 25, 25])
        self.assertTrue(all(c.line != 987654 for c in comments))

    def test_tag_identifies_root_and_groups_only_same_review_path_position(
        self,
    ) -> None:
        rows = recording("026-e2-comments.json")
        rows[1]["body"] = "[REV-1][blocking][tests] Tagged root"
        rows[1]["resolver"] = {"login": "human"}
        comments, states = parse_comments(list(reversed(rows)), 3)
        self.assertEqual([c.in_reply_to_id for c in comments], [8, None, 8])
        self.assertEqual(
            [(s.root_id, s.resolved) for s in states], [(8, True)]
        )
        rows[2]["position"] += 1
        rows[1].pop("resolver")
        comments, states = parse_comments(rows, 3)
        self.assertIsNone(comments[2].in_reply_to_id)
        self.assertIsNone(states[0].resolved)

    def test_range_uses_last_displayed_new_line_and_extra_count(self) -> None:
        rows = recording("034-e5-comments.json")
        parsed, _ = parse_comments(rows, 7)
        self.assertEqual((parsed[0].start_line, parsed[0].line), (2, 4))
        for hunk in (
            "", "invalid", "@@ -0,0 +1,1 @@\n+one\n+too many",
            "@@ -23,3 +23,2 @@\n a\n b\n-c",
            "@@ -23,3 +23,2 @@\n a\n b\n-c\n"
            "\\ No newline at end of file",
        ):
            with self.subTest(hunk=hunk):
                self.assertEqual(hunk_anchor(hunk, 0), (None, None))
        for hunk in (
            "@@ -23,3 +23,2 @@\n a\n-c\n b",
            "@@ -23,3 +23,2 @@\n a\n-c\n b\n"
            "\\ No newline at end of file",
        ):
            with self.subTest(hunk=hunk):
                self.assertEqual(hunk_anchor(hunk, 0), (24, None))

    def test_live_base_is_accepted_and_merge_base_is_validated(self) -> None:
        row = recording("017-setup-pr-after-base-push.json")
        self.assertNotEqual(row["merge_base"], row["base"]["sha"])
        parsed = parse_pullrequest(row)
        self.assertEqual(parsed.base, row["base"]["sha"])
        self.assertEqual(parsed.head, row["head"]["sha"])
        self.assertIsNone(parsed.mergeable_state)
        with self.assertRaisesRegex(ForgeError, "merge_base"):
            parse_pullrequest(dict(row, merge_base="short"))
        merged = parse_pullrequest(recording("061-e7-merged-pr.json"))
        self.assertTrue(merged.merged)
        self.assertIsNotNone(merged.merge_commit)

    def test_timestamp_offsets_are_validated_and_reviews_order_by_instant_id(
        self,
    ) -> None:
        root = Path("/repo")
        forge = Forgejo(
            Repository(root, root, root / ".git", forgejo_config()), "reviewer"
        )
        row = recording("022-e2-body-first.json")
        rows = [
            dict(row, id=20, submitted_at="2026-01-01T02:00:00+02:00"),
            dict(row, id=10, submitted_at="2025-12-31T19:00:00-05:00"),
            dict(row, id=5, submitted_at="2026-01-01T00:00:01Z"),
        ]
        with patch.object(forge, "listing", return_value=rows):
            self.assertEqual(
                [r.evidence.id for r in forge.reviews(1)], [10, 20, 5]
            )
        with self.assertRaisesRegex(ForgeError, "offset"):
            parse_evidence(
                dict(row, submitted_at="2026-01-01T00:00:00"), review=True
            )

    def test_body_crlf_and_issue_comments_preserve_content_sorted_by_id(
        self,
    ) -> None:
        rows = recording("065-final-issue-comments.json")
        row = dict(rows[0], body="first\r\nsecond")
        self.assertEqual(parse_evidence(row).body, "first\nsecond")
        root = Path("/repo")
        forge = Forgejo(
            Repository(root, root, root / ".git", forgejo_config()), "reviewer"
        )
        with patch.object(
            forge,
            "api",
            return_value=[dict(row, id=30), dict(row, id=20)],
        ) as api:
            self.assertEqual([c.id for c in forge._conversation(1)], [20, 30])
            self.assertEqual(
                api.call_args.args[0], "/repos/org/repo/issues/1/comments"
            )
