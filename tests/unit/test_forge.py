"""Forge response boundaries, including values produced by the browser."""

from pathlib import Path
import unittest
from unittest.mock import patch

from tests._support import add_src_to_path

add_src_to_path()

from agent_squad.forge import (  # noqa: E402
    ForgeError,
    boolean,
    oid,
)
from agent_squad.github import (  # noqa: E402
    GitHub, parse_comment, parse_evidence, parse_pullrequest, parse_review,
)
from agent_squad.initialization import Repository  # noqa: E402
from tests.unit.test_conventions import (  # noqa: E402
    A,
    H,
    config,
    derive_state,
    evidence,
    render_line,
    snapshot,
)


def forge_evidence(body: object = "") -> dict:
    return {
        "id": 10,
        "user": {"login": "dev"},
        "created_at": "2026-01-01T00:00:10Z",
        "body": body,
    }


class ForgeValidationTests(unittest.TestCase):
    def test_outdated_comments_preserve_original_anchors(self) -> None:
        record = dict(
            forge_evidence(), pull_request_review_id=20,
            path="example.py", side="RIGHT", line=None, start_line=None,
            original_line=4, original_start_line=2,
        )
        for fields, expected in (
            ({}, (4, 2)),
            ({"original_start_line": None}, (4, None)),
            ({"line": 8, "start_line": 6}, (8, 6)),
            ({"original_line": None, "original_start_line": None},
             (None, None)),
        ):
            with self.subTest(fields=fields):
                parsed = parse_comment(record | fields)
                self.assertEqual((parsed.line, parsed.start_line), expected)
                self.assertEqual(parsed.side, "RIGHT")
        for field in ("original_line", "original_start_line"):
            for invalid in (0, -1, True, "4"):
                with self.subTest(field=field, value=invalid):
                    with self.assertRaises(ForgeError):
                        parse_comment(record | {field: invalid})

    def test_null_body_is_empty_but_other_non_strings_are_refused(
        self,
    ) -> None:
        self.assertEqual(parse_evidence(forge_evidence(None)).body, "")
        for invalid in (False, 0, [], {}, 42):
            with (
                self.subTest(body=invalid),
                self.assertRaisesRegex(
                    ForgeError, "evidence.body must be a string"
                ),
            ):
                parse_evidence(forge_evidence(invalid))

    def test_browser_line_endings_preserve_decisions_stops_and_task(
        self,
    ) -> None:
        bodies = [
            render_line("decision", finding="none", budget=5)
            + "\n\nDecided.\n\n## Task\n\nAmended objective.\n",
            render_line("stop", head=H, reason="design") + "\n\nStop.\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                normalized = parse_evidence(
                    forge_evidence(body.replace("\n", "\r\n"))
                )
                self.assertEqual(normalized.body, body)
                self.assertEqual(
                    derive_state(snapshot(conversation=(normalized,))),
                    derive_state(
                        snapshot(conversation=(evidence(10, body, "dev"),))
                    ),
                )
        state = derive_state(
            snapshot(
                conversation=(
                    parse_evidence(
                        forge_evidence(bodies[0].replace("\n", "\r\n"))
                    ),
                )
            )
        )
        self.assertEqual(state["budget"]["effective"], 5)
        self.assertEqual(
            state["effective_task"], "## Task\n\nAmended objective."
        )

    def test_boolean_and_forge_state_validators(self) -> None:
        for value in (H[:7], H.upper(), "g" * 40, H + "0"):
            with (
                self.subTest(sha=value),
                self.assertRaisesRegex(ForgeError, "full lowercase Git SHA"),
            ):
                oid(value, "head.sha")
        for value in (None, 0, 1, "false"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ForgeError, "must be a boolean"),
            ):
                boolean(value, "merged")
        record = dict(
            forge_evidence(),
            commit_id=H,
            state="UNKNOWN",
            submitted_at="2026-01-01T00:00:10Z",
        )
        with self.assertRaisesRegex(ForgeError, "unknown review state"):
            parse_review(record)
        pr = dict(
            forge_evidence(),
            number=1,
            title="Fixture",
            merged=False,
            head={"sha": H, "ref": "feature"},
            base={"sha": A, "ref": "main"},
            state="UNKNOWN",
        )
        with self.assertRaisesRegex(
            ForgeError, "state must be open or closed"
        ):
            parse_pullrequest(pr)

    def test_token_and_resolution_confirmation_validators(self) -> None:
        path = Path("/repo")
        repo = Repository(path, path, path / ".git", config())
        for value in ("", "\n", "token\nsecond", "token\rsecond"):
            forge = GitHub(repo, "implementer")
            with (
                self.subTest(value=value),
                patch.object(forge, "_run", return_value=value),
                self.assertRaisesRegex(ForgeError, "invalid token response"),
            ):
                forge.token()
        forge = GitHub(repo, "reviewer")
        for thread in (
            {"id": "wrong", "isResolved": True},
            {"id": "T-1", "isResolved": False},
            {"id": "T-1", "isResolved": 1},
        ):
            with (
                self.subTest(thread=thread),
                patch.object(
                    forge,
                    "api",
                    return_value={
                        "data": {"resolveReviewThread": {"thread": thread}}
                    },
                ),
                self.assertRaisesRegex(ForgeError, "did not confirm"),
            ):
                forge.resolve("T-1")


class ForgeBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        path = Path('/repo')
        self.repo = Repository(path, path, path / '.git', config())
        self.executable = patch('agent_squad.github.shutil.which',
                                return_value='/fake/gh')
        self.executable.start()
        self.addCleanup(self.executable.stop)
        self.forge = GitHub(self.repo, 'reviewer')

    def test_factory_selects_protocol_without_accessing_credentials(
        self,
    ) -> None:
        from agent_squad.forge import Forge, make_forge

        with patch.object(GitHub, '_run') as transport:
            for role in ('implementer', 'reviewer'):
                forge = make_forge(self.repo, role)
                self.assertIsInstance(forge, Forge)
                self.assertIsInstance(forge, GitHub)
                self.assertEqual(forge.role, role)
                self.assertEqual(forge.account, config().account(role))
                self.assertTrue(forge.can_resolve_threads)
                self.assertTrue(forge.can_read_thread_resolution)
                self.assertTrue(forge.can_read_branch_rules)
            transport.assert_not_called()

    def test_factory_selects_forgejo_without_reading_credentials(self) -> None:
        from dataclasses import replace
        from agent_squad.forge import Forge, make_forge
        from agent_squad.forgejo import Forgejo
        from agent_squad.initialization import Configuration

        data = config().to_dict()
        data["forge"].update(kind="forgejo", base_url="https://forge.example")
        for role in ("implementer", "reviewer"):
            data[role]["token_file"] = "/outside/" + role + ".token"
        repository = replace(
            self.repo, configuration=Configuration.from_dict(data),
        )
        with patch("agent_squad.forgejo.read_token") as credential:
            forge = make_forge(repository, "implementer")
            self.assertIsInstance(forge, Forge)
            self.assertIsInstance(forge, Forgejo)
            self.assertFalse(forge.can_resolve_threads)
            self.assertTrue(forge.can_read_thread_resolution)
            self.assertTrue(forge.can_read_branch_rules)
            credential.assert_not_called()

    def test_response_states_and_publication_events_are_adapter_owned(
        self,
    ) -> None:
        from agent_squad.forge import ReviewState, requested_state
        from agent_squad.github import review_event

        for wire, neutral, dismissed in (
            ('APPROVED', ReviewState.APPROVED, False),
            ('CHANGES_REQUESTED', ReviewState.CHANGES_REQUESTED, False),
            ('COMMENTED', ReviewState.COMMENTED, False),
            ('PENDING', ReviewState.PENDING, False),
            ('DISMISSED', ReviewState.COMMENTED, True),
        ):
            with self.subTest(state=wire):
                review = parse_review(self.record(10, wire))
                self.assertEqual(review.state, neutral)
                self.assertEqual(review.dismissed, dismissed)
                self.assertEqual(review.state_label, wire)
        for verdict, event in (
            ('approved', 'APPROVE'),
            ('changes_requested', 'REQUEST_CHANGES'),
            ('needs_human', 'COMMENT'),
        ):
            with self.subTest(verdict=verdict):
                self.assertEqual(review_event(requested_state(verdict)), event)
        with self.assertRaisesRegex(ForgeError, 'cannot publish'):
            review_event(ReviewState.PENDING)

    def test_single_and_range_anchors_build_exact_publication_payloads(
        self,
    ) -> None:
        from agent_squad.forge import (
            Anchor, ReviewComment, ReviewPublication, ReviewState,
        )
        from agent_squad.github import anchor_payload

        for anchor, expected in (
            (Anchor('example.py', 4),
             {'path': 'example.py', 'line': 4, 'side': 'RIGHT'}),
            (Anchor('example.py', 4, 2),
             {'path': 'example.py', 'line': 4, 'side': 'RIGHT',
              'start_line': 2, 'start_side': 'RIGHT'}),
        ):
            with self.subTest(anchor=anchor):
                self.assertFalse(hasattr(anchor, 'payload'))
                self.assertEqual(anchor_payload(anchor), expected)
                publication = ReviewPublication(
                    H, ReviewState.CHANGES_REQUESTED, 'review', 'fallback',
                    (ReviewComment(anchor, 'finding'),), A,
                )
                with patch.object(
                    self.forge, 'api',
                    return_value=self.record(10, 'CHANGES_REQUESTED'),
                ) as api:
                    self.forge.post_review(1, publication)
                api.assert_called_once_with(
                    '/repos/org/repo/pulls/1/reviews', method='POST', body={
                        'commit_id': H, 'event': 'REQUEST_CHANGES',
                        'body': 'review',
                        'comments': [{**expected, 'body': 'finding'}],
                    })

    @staticmethod
    def record(
        ident: int, state: str, author: str = 'human',
        timestamp: str = '2026-01-01T00:00:10Z',
    ) -> dict:
        return dict(forge_evidence(), id=ident, state=state,
                    user={'login': author}, commit_id=H,
                    submitted_at=timestamp)

    def test_reviews_read_every_page(self) -> None:
        records = [self.record(i, 'APPROVED') for i in range(1, 101)]
        with patch.object(self.forge, 'api', side_effect=[
            records, [self.record(101, 'CHANGES_REQUESTED')],
        ]) as api:
            reviews = self.forge.reviews(1)
        self.assertEqual(len(reviews), 101)
        self.assertEqual([call.args[0] for call in api.call_args_list], [
            '/repos/org/repo/pulls/1/reviews?per_page=100&page=1',
            '/repos/org/repo/pulls/1/reviews?per_page=100&page=2',
        ])

    def test_unsupported_resolution_exits_before_reading_or_mutating(
        self,
    ) -> None:
        from contextlib import redirect_stderr
        from io import StringIO
        from agent_squad.cli import main

        class UnsupportedForge:
            can_resolve_threads = False

            def snapshot(self, number):
                raise AssertionError('unsupported resolution must not read')

            def resolve(self, node_id):
                raise AssertionError('unsupported resolution must not mutate')

        errors = StringIO()
        with (
            patch('agent_squad.cli.load_initialized_repository',
                  return_value=self.repo),
            patch('agent_squad.cli.make_forge',
                  return_value=UnsupportedForge()),
            redirect_stderr(errors),
        ):
            result = main([
                'thread', 'resolve', '--pr', '1', '--as', 'reviewer',
                '--finding', 'REV-1',
            ])
        self.assertEqual(result, 1)
        self.assertEqual(
            errors.getvalue(),
            'error: thread resolve is not supported on this forge\n',
        )

    def test_unknown_resolution_is_null_and_never_settles_findings(
        self,
    ) -> None:
        from dataclasses import replace
        from tests.unit.test_conventions import review, root

        original = snapshot(reviews=(review(10, 'changes_requested',
            findings='REV-1 [blocking] Finding'),),
                            comments=(root(),))
        readable = derive_state(original)
        unknown = derive_state(replace(
            original, can_read_thread_resolution=False,
        ))
        self.assertTrue(readable['findings'][0]['resolved'])
        self.assertIsNone(unknown['findings'][0]['resolved'])
        self.assertFalse(unknown['findings'][0]['settled'])
        self.assertEqual(readable['next_action'], unknown['next_action'])
        self.assertFalse(unknown['can_read_thread_resolution'])

    def test_display_labels_cannot_supply_or_remove_approval(self) -> None:
        from dataclasses import replace
        from agent_squad.forge import ReviewState
        from tests.unit.test_conventions import review

        approved = replace(review(10), display_state='display only')
        state = derive_state(snapshot(reviews=(approved,)))
        self.assertTrue(state['approval']['approved'])
        for invalid in (replace(approved, dismissed=True),
                        replace(approved, state=ReviewState.COMMENTED,
                                display_state='APPROVED')):
            state = derive_state(snapshot(reviews=(invalid,)))
            self.assertFalse(state['approval']['approved'])

    def test_unreadable_branch_rules_are_reported_without_querying(
        self,
    ) -> None:
        from unittest.mock import Mock
        from agent_squad.forge import Forge
        from agent_squad.merging import merge_pr

        forge = Mock(spec=Forge)
        forge.can_read_branch_rules = False
        forge.snapshot.return_value = snapshot()
        forge.branch_rules.side_effect = AssertionError('must not read rules')
        forge.merge.side_effect = ForgeError('refused', 405)
        state = {'target': {'head': H}, 'approval': {'approved': True}}
        with (
            patch('agent_squad.merging.state_for', return_value=state),
            patch('agent_squad.merging.check_merge_gate', return_value=False),
        ):
            result = merge_pr(self.repo, forge, 1)
        self.assertEqual(result['branch_rules'], {'visibility': 'not visible'})
        self.assertFalse(result['merged'])
        forge.branch_rules.assert_not_called()

    def test_snapshot_skips_unreadable_resolution(self) -> None:
        self.forge.can_read_thread_resolution = False
        with (
            patch.object(self.forge, 'pr', return_value=snapshot().pr),
            patch.object(self.forge, 'reviews', return_value=()),
            patch.object(self.forge, 'listing', return_value=[]),
            patch.object(self.forge, 'thread_states') as threads,
        ):
            result = self.forge.snapshot(1)
        self.assertFalse(result.can_read_thread_resolution)
        self.assertEqual(result.threads, ())
        threads.assert_not_called()
