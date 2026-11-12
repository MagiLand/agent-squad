"""Forge response boundaries, including values produced by the browser."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests._support import FakeClock, add_src_to_path

add_src_to_path()

from agent_squad.forge import (  # noqa: E402
    ForgeError,
    ThreadState,
    boolean,
    oid,
)
from agent_squad.github import (  # noqa: E402
    REVIEWS, CommandFailed, GitHub, actor, database_id, parse_comment,
    parse_evidence, parse_pullrequest, parse_review, rest_pullrequest,
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


def page(nodes: list, cursor: str | None = None) -> dict:
    return {"nodes": nodes, "pageInfo": {
        "hasNextPage": cursor is not None, "endCursor": cursor,
    }}


def graphql_comment(ident: int = 30, reply: int | None = None) -> dict:
    return {
        "fullDatabaseId": str(ident),
        "author": {"__typename": "User", "login": "reviewer"},
        "body": "finding", "createdAt": "2026-01-01T00:00:30Z",
        "path": "example.py", "line": 4, "originalLine": 4,
        "startLine": None, "originalStartLine": None,
        "replyTo": None if reply is None else {"fullDatabaseId": str(reply)},
        "pullRequestReview": {"fullDatabaseId": "20"},
    }


def graphql_thread(*comments: dict) -> dict:
    return {
        "id": "T-30", "isResolved": False, "diffSide": "RIGHT",
        "comments": page(list(comments) or [graphql_comment()]),
    }


def graphql_pull(**lists: list) -> dict:
    """A SquadSnapshot response for one open PR at H (#149)."""
    return {"repository": {"pullRequest": {
        "id": "PR-1", "fullDatabaseId": "10",
        "author": {"__typename": "User", "login": "dev"}, "body": "",
        "createdAt": "2026-01-01T00:00:10Z", "number": 1, "title": "Task",
        "headRefOid": H, "headRefName": "feat/topic", "baseRefOid": A,
        "baseRefName": "main", "state": "OPEN", "merged": False,
        "mergeCommit": None, "potentialMergeCommit": None,
        "mergeStateStatus": "CLEAN",
        **{key: page(lists.get(key, []))
           for key in ("reviews", "reviewThreads", "comments")},
    }}}


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

    def test_open_issues_skip_pull_requests_and_read_every_page(
        self,
    ) -> None:
        def row(number: int, **extra: object) -> dict:
            return dict(forge_evidence(), number=number,
                        title=f' Title {number} ', **extra)

        first = [row(i) for i in range(1, 100)]
        first.append(row(100, pull_request={'url': 'pulls/100'}))
        with patch.object(self.forge, 'api', side_effect=[
            first, [row(101)],
        ]) as api:
            found = self.forge.open_issues()
        self.assertEqual(sorted(found), [*range(1, 100), 101])
        self.assertEqual(found[101], ' Title 101 ')
        self.assertEqual([call.args[0] for call in api.call_args_list], [
            '/repos/org/repo/issues?state=open&per_page=100&page=1',
            '/repos/org/repo/issues?state=open&per_page=100&page=2',
        ])

    def test_labels_read_every_page_and_creation_names_the_label(
        self,
    ) -> None:
        names = [{'name': f'label-{i}'} for i in range(100)]
        with patch.object(self.forge, 'api', side_effect=[
            names, [{'name': 'needs-triage'}],
        ]) as api:
            labels = self.forge.labels()
        self.assertEqual(labels[-1], 'needs-triage')
        self.assertEqual(len(labels), 101)
        self.assertEqual(api.call_args_list[1].args[0],
                         '/repos/org/repo/labels?per_page=100&page=2')
        created = dict(forge_evidence('Body\r\n'), number=7, title='T',
                       state='open', labels=[{'name': 'needs-triage'}])
        with patch.object(self.forge, 'api', return_value=created) as api:
            record = self.forge.create_issue('T', 'Body', 'needs-triage')
        api.assert_called_once_with(
            '/repos/org/repo/issues', method='POST',
            body={'title': 'T', 'body': 'Body', 'labels': ['needs-triage']},
        )
        self.assertEqual(record, {
            'number': 7, 'title': 'T', 'state': 'open',
            'is_pull_request': False, 'body': 'Body\n',
            'labels': ['needs-triage'], 'milestone': None, 'comments': (),
        })

    def test_dependencies_read_both_lists_and_null_a_refused_one(
        self,
    ) -> None:
        def row(number: int, url: str = 'repos/org/repo') -> dict:
            return dict(forge_evidence(), number=number, title=f'T{number}',
                        state='closed' if number % 2 else 'open',
                        repository_url=f'https://api.github.com/{url}')

        blocking = [row(i) for i in range(1, 101)]
        with patch.object(self.forge, 'api', side_effect=[
            blocking, [row(101, 'repos/other-org/other.repo')],
            [row(102)],
        ]) as api:
            found = self.forge.issue_dependencies(5)
        self.assertEqual([call.args[0] for call in api.call_args_list], [
            '/repos/org/repo/issues/5/dependencies/blocking'
            '?per_page=100&page=1',
            '/repos/org/repo/issues/5/dependencies/blocking'
            '?per_page=100&page=2',
            '/repos/org/repo/issues/5/dependencies/blocked_by'
            '?per_page=100&page=1',
        ])
        self.assertEqual(len(found['blocks']), 101)
        self.assertEqual(found['blocks'][-1], {
            'number': 101, 'title': 'T101', 'state': 'closed',
            'repository': 'other-org/other.repo',
        })
        self.assertEqual(found['blocked_by'], [{
            'number': 102, 'title': 'T102', 'state': 'open',
            'repository': 'org/repo',
        }])
        for status in (401, 403, 404):
            with self.subTest(status=status):
                with patch.object(self.forge, 'api', side_effect=[
                    CommandFailed('refused', status, ''), [],
                ]):
                    self.assertEqual(
                        self.forge.issue_dependencies(5),
                        {'blocks': None, 'blocked_by': []},
                    )
        for failure in (
            CommandFailed('server error', 500, ''), ForgeError('timeout'),
        ):
            with self.subTest(failure=str(failure)):
                with patch.object(self.forge, 'api', side_effect=failure):
                    with self.assertRaises(ForgeError):
                        self.forge.issue_dependencies(5)
        for field, value, message in (
            ('repository_url', 'https://api.github.com/repos/org',
             'repository_url must name a repository'),
            ('repository_url', 'https://api.github.com/users/org/repo',
             'repository_url must name a repository'),
            ('repository_url', None, 'repository_url must be a string'),
            ('state', 'merged', 'state must be open or closed'),
            ('number', 0, 'related issue number must be positive'),
        ):
            with self.subTest(field=field, value=value):
                with patch.object(self.forge, 'api', return_value=[
                    {**row(3), field: value},
                ]):
                    with self.assertRaisesRegex(ForgeError, message):
                        self.forge.issue_dependencies(5)

    def test_label_changes_add_once_then_remove_each_by_encoded_name(
        self,
    ) -> None:
        with patch.object(self.forge, 'api', return_value=[]) as api:
            self.forge.change_issue_labels(
                5, ('ready-for-agent', 'area: x'), ('a/b c', 'stale'))
        self.assertEqual([(c.args, c.kwargs) for c in api.call_args_list], [
            (('/repos/org/repo/issues/5/labels',),
             {'method': 'POST',
              'body': {'labels': ['ready-for-agent', 'area: x']}}),
            (('/repos/org/repo/issues/5/labels/a%2Fb%20c',),
             {'method': 'DELETE'}),
            (('/repos/org/repo/issues/5/labels/stale',),
             {'method': 'DELETE'}),
        ])
        # An empty add list is never sent.
        with patch.object(self.forge, 'api', return_value=[]) as api:
            self.forge.change_issue_labels(5, (), ('stale',))
        api.assert_called_once_with(
            '/repos/org/repo/issues/5/labels/stale', method='DELETE')

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

        original = snapshot(
            reviews=(review(10, 'changes_requested',
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
        state = {'target': {'head': H}, 'approval': {'approved': True},
                 'closes_issue': True}
        with (
            patch('agent_squad.merging.state_for', return_value=state),
            patch('agent_squad.merging.check_merge_gate', return_value=False),
            patch('agent_squad.merging.write_merge_record'),
        ):
            result = merge_pr(self.repo, forge, 1)
        self.assertEqual(result['branch_rules'], {'visibility': 'not visible'})
        self.assertFalse(result['merged'])
        forge.branch_rules.assert_not_called()

    def test_snapshot_skips_unreadable_resolution(self) -> None:
        self.forge.can_read_thread_resolution = False
        with patch.object(self.forge, 'graphql', return_value=graphql_pull(
            reviewThreads=[graphql_thread()]
        )):
            result = self.forge.snapshot(1)
        self.assertFalse(result.can_read_thread_resolution)
        self.assertEqual(result.threads, ())
        # The thread's comment remains evidence.
        self.assertEqual([c.evidence.id for c in result.comments], [30])


class GraphQLReadTests(unittest.TestCase):
    """The GitHub snapshot read and its REST-shaped records (#149)."""

    def setUp(self) -> None:
        path = Path('/repo')
        with patch('agent_squad.github.shutil.which',
                   return_value='/fake/gh'):
            self.forge = GitHub(
                Repository(path, path, path / '.git', config()), 'reviewer'
            )
        self.forge._token = 'secret-token'

    def failing(self, response: object, stderr: str = 'gh: failed'):
        """``gh`` exits 1 and prints ``response`` on standard output."""
        output = response if isinstance(response, str) else (
            json.dumps(response)
        )
        return patch.object(self.forge, '_run', side_effect=CommandFailed(
            stderr, None, output
        ))

    def test_errors_under_the_optional_field_null_it(self) -> None:
        data = {'repository': {'pullRequest': {'id': 'PR-1'}, 'head': {}}}
        error = {'message': 'Resource not accessible by integration',
                 'path': ['repository', 'head', 'checkSuites']}
        with self.failing({'data': data, 'errors': [error]}):
            result = self.forge.graphql('query Q', {}, optional='head')
        self.assertEqual(result['repository'], {
            'pullRequest': {'id': 'PR-1'}, 'head': None,
        })

    def test_other_errors_fail_the_read(self) -> None:
        missing = {
            'data': {'repository': {'pullRequest': None}},
            'errors': [{'type': 'NOT_FOUND', 'message': 'Could not resolve'
                        ' to a PullRequest with the number of 9.',
                        'path': ['repository', 'pullRequest']}],
        }
        for response, optional, message in (
            (missing, 'head', 'gh: Could not resolve'),
            (missing, None, 'gh: Could not resolve'),
            ({'data': None, 'errors': [{'message': 'syntax'}]}, 'head',
             'gh: syntax'),
            ('not JSON', 'head', 'gh: HTTP 502'),
            ('', None, 'gh: HTTP 502'),
        ):
            with self.subTest(response=response, optional=optional), \
                    self.failing(response, message):
                with self.assertRaisesRegex(ForgeError, message):
                    self.forge.graphql('query Q', {}, optional=optional)

    def test_errors_after_success_are_redacted(self) -> None:
        response = json.dumps({'data': {}, 'errors': [
            {'message': 'bad secret-token'}, {'message': 'second'},
        ]})
        with patch.object(self.forge, '_run', return_value=response):
            with self.assertRaises(ForgeError) as raised:
                self.forge.graphql('query Q', {})
        self.assertEqual(str(raised.exception), 'bad [redacted]; second')

    def test_a_list_is_read_to_its_last_page(self) -> None:
        pages = [
            {'node': {'reviews': page([{'id': 'R-2', 'comments': page(
                [{'n': 3}], 'c1')}], None)}},
            {'node': {'comments': page([{'n': 4}], None)}},
        ]
        first = page([{'id': 'R-1', 'comments': page([{'n': 1}])}], 'p1')
        with patch.object(self.forge, 'graphql', side_effect=pages) as read:
            nodes = self.forge.complete(first, REVIEWS, 'PR-1')
        self.assertEqual(
            [(n['id'], n['comments']) for n in nodes],
            [('R-1', [{'n': 1}]), ('R-2', [{'n': 3}, {'n': 4}])],
        )
        self.assertEqual(
            [call.args[1] for call in read.call_args_list],
            [{'id': 'PR-1', 'cursor': 'p1'}, {'id': 'R-2', 'cursor': 'c1'}],
        )
        self.assertIn(
            '... on PullRequestReview { comments(first:100, after:$cursor',
            read.call_args_list[1].args[0],
        )

    def test_a_repeated_cursor_fails_the_read(self) -> None:
        again = {'node': {'reviews': page([], 'p1')}}
        with patch.object(self.forge, 'graphql', return_value=again):
            with self.assertRaisesRegex(ForgeError, 'repeated a cursor'):
                self.forge.complete(page([], 'p1'), REVIEWS, 'PR-1')

    def test_actors_and_ids_read_as_rest_names_them(self) -> None:
        self.assertEqual(actor({'__typename': 'Bot', 'login': 'ci'}, 'a'),
                         {'login': 'ci[bot]'})
        self.assertEqual(actor({'__typename': 'User', 'login': 'dev'}, 'a'),
                         {'login': 'dev'})
        self.assertEqual(actor(None, 'a'), {'login': 'ghost'})
        self.assertEqual(database_id('6753922008', 'id'), 6753922008)
        for invalid in ('0', '01', '-1', '1.5', '', 7, None):
            with self.subTest(value=invalid):
                with self.assertRaises(ForgeError):
                    database_id(invalid, 'id')

    def test_pull_request_states_and_merge_commits(self) -> None:
        node = graphql_pull()['repository']['pullRequest']
        for fields, expected in (
            ({}, ('open', False, None, 'clean')),
            ({'state': 'CLOSED', 'potentialMergeCommit': {'oid': A},
              'mergeStateStatus': 'CLEAN'}, ('closed', False, A, 'clean')),
            ({'state': 'MERGED', 'merged': True, 'mergeCommit': {'oid': H},
              'potentialMergeCommit': {'oid': A},
              'mergeStateStatus': 'UNKNOWN'}, ('closed', True, H, 'unknown')),
        ):
            with self.subTest(fields=fields):
                pr = parse_pullrequest(rest_pullrequest(node | fields))
                self.assertEqual(
                    (pr.state, pr.merged, pr.merge_commit,
                     pr.mergeable_state), expected,
                )
        with self.assertRaises(ForgeError):
            parse_pullrequest(rest_pullrequest(node | {'state': 'DRAFT'}))

    def test_snapshot_comments_follow_the_rest_listings(self) -> None:
        root, reply, extra, later = (
            graphql_comment(), graphql_comment(31, reply=30),
            graphql_comment(32, reply=30), graphql_comment(40),
        )
        review = {
            'id': 'PRR-20', 'fullDatabaseId': '20', 'author': None,
            'body': '', 'state': 'PENDING', 'submittedAt': None,
            'commit': {'oid': H},
            'comments': page([root, extra, reply, later]),
        }
        response = graphql_pull(reviews=[review], reviewThreads=[
            graphql_thread(later) | {'id': 'T-40', 'diffSide': 'LEFT'},
            graphql_thread(root, reply),
        ])
        with patch.object(self.forge, 'graphql', return_value=response):
            result = self.forge.snapshot(1)
        self.assertEqual(result.threads, (
            ThreadState(40, 'T-40', False), ThreadState(30, 'T-30', False),
        ))
        # Thread comments by ID, as the flat listing orders them, then those
        # read only under a review, which carry no anchor; a reply has its
        # thread's side.
        self.assertEqual(
            [(c.evidence.id, c.line, c.side) for c in result.comments],
            [(30, 4, 'RIGHT'), (31, 4, 'RIGHT'), (40, 4, 'LEFT'),
             (32, None, None)],
        )
        pending = result.reviews[0]
        self.assertEqual(pending.evidence.author, 'ghost')
        self.assertEqual(pending.evidence.created_at, '1970-01-01T00:00:00Z')
        self.assertIsNone(result.head_checks)

    def test_a_thread_without_comments_fails_the_read(self) -> None:
        thread = graphql_thread() | {'comments': page([])}
        with patch.object(self.forge, 'graphql',
                          return_value=graphql_pull(reviewThreads=[thread])):
            with self.assertRaisesRegex(ForgeError, 'no root comment'):
                self.forge.snapshot(1)


class GitHubBranchDeletionTests(unittest.TestCase):
    """GitHub's automatic head-branch deletion races merge cleanup (#120)."""

    REPOSITORY = '/repos/org/repo'
    READ = '/repos/org/repo/git/ref/heads/feat%2Ftopic'
    DELETE = '/repos/org/repo/git/refs/heads/feat%2Ftopic'

    def setUp(self) -> None:
        path = Path('/repo')
        repo = Repository(path, path, path / '.git', config())
        self.clock = FakeClock()
        for patcher in (
            patch('agent_squad.github.shutil.which', return_value='/fake/gh'),
            patch('agent_squad.github.time', self.clock),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.forge = GitHub(repo, 'implementer')
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def ref(sha: str = H) -> dict:
        return {'ref': 'refs/heads/feat/topic', 'object': {'sha': sha}}

    @staticmethod
    def absent() -> ForgeError:
        return ForgeError('gh: Not Found (HTTP 404)', 404)

    @staticmethod
    def refused(status: int) -> ForgeError:
        return ForgeError(
            f'gh: Reference does not exist (HTTP {status})', status)

    @staticmethod
    def repository(**fields: object) -> dict:
        return {'id': 1, 'full_name': 'org/repo', **fields}

    def delete(self, *responses: object) -> list[tuple[str, str]]:
        """Run delete_branch over scripted API responses."""
        with patch.object(self.forge, 'api', side_effect=responses) as api:
            try:
                self.forge.delete_branch('feat/topic', H)
            finally:
                self.calls = [(c.args[0], c.kwargs.get('method', 'GET'))
                              for c in api.call_args_list]
        return self.calls

    def test_absent_or_moved_branch_is_read_once(self) -> None:
        self.assertEqual(self.delete(self.absent()), [(self.READ, 'GET')])
        with self.assertRaisesRegex(
            ForgeError, '^remote branch no longer matches approved head$'
        ):
            self.delete(self.ref(A))
        self.assertEqual(self.calls, [(self.READ, 'GET')])
        self.assertEqual(self.clock.sleeps, [])

    def test_enabled_automatic_deletion_is_awaited_without_delete(
        self,
    ) -> None:
        calls = self.delete(
            self.ref(), self.repository(delete_branch_on_merge=True),
            self.ref(), self.ref(), self.absent(),
        )
        self.assertEqual(calls, [
            (self.READ, 'GET'), (self.REPOSITORY, 'GET'),
            *[(self.READ, 'GET')] * 3,
        ])
        self.assertEqual(self.clock.sleeps, [1.0] * 3)

    def test_skipped_automatic_deletion_gets_one_delete_after_the_bound(
        self,
    ) -> None:
        calls = self.delete(
            self.ref(), self.repository(delete_branch_on_merge=True),
            *[self.ref()] * 30, None, self.absent(),
        )
        self.assertEqual(self.clock.sleeps, [1.0] * 30)
        self.assertEqual(calls[2:], [
            *[(self.READ, 'GET')] * 30,
            (self.DELETE, 'DELETE'), (self.READ, 'GET'),
        ])

    def test_wait_never_sleeps_past_the_bound(self) -> None:
        self.clock.now = 0.25
        with patch('agent_squad.github.AUTO_DELETION_WAIT', 2.5):
            self.delete(
                self.ref(), self.repository(delete_branch_on_merge=True),
                *[self.ref()] * 3, None, self.absent(),
            )
        self.assertEqual(self.clock.sleeps, [1.0, 1.0, 0.5])
        self.assertEqual(
            [m for _, m in self.calls].count('DELETE'), 1)

    def test_head_change_during_the_wait_is_refused_without_delete(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ForgeError, '^remote branch no longer matches approved head$'
        ):
            self.delete(
                self.ref(), self.repository(delete_branch_on_merge=True),
                self.ref(), self.ref(A),
            )
        self.assertNotIn('DELETE', [m for _, m in self.calls])
        self.assertEqual(self.clock.sleeps, [1.0, 1.0])

    def test_setting_not_exactly_true_deletes_without_waiting(self) -> None:
        for record in (
            self.repository(delete_branch_on_merge=False),
            self.repository(),
            self.repository(delete_branch_on_merge='true'),
            self.repository(delete_branch_on_merge=None),
            self.repository(delete_branch_on_merge=1),
            ForgeError('gh: Forbidden (HTTP 403)', 403),
        ):
            with self.subTest(record=record):
                calls = self.delete(self.ref(), record, None, self.absent())
                self.assertEqual(calls, [
                    (self.READ, 'GET'), (self.REPOSITORY, 'GET'),
                    (self.DELETE, 'DELETE'), (self.READ, 'GET'),
                ])
                self.assertEqual(self.clock.sleeps, [])

    def test_failed_delete_counts_only_when_a_fresh_read_shows_absence(
        self,
    ) -> None:
        for status in (404, 422, 500):
            with self.subTest(status=status):
                calls = self.delete(
                    self.ref(), self.repository(), self.refused(status),
                    self.absent(),
                )
                self.assertEqual(calls[2:], [
                    (self.DELETE, 'DELETE'), (self.READ, 'GET'),
                ])
                with self.assertRaises(ForgeError) as caught:
                    self.delete(
                        self.ref(), self.repository(), self.refused(status),
                        self.ref(),
                    )
                self.assertEqual(
                    str(caught.exception),
                    f'gh: Reference does not exist (HTTP {status})',
                )
                self.assertEqual(caught.exception.status, status)

    def test_failed_delete_then_moved_or_unreadable_branch_fails(
        self,
    ) -> None:
        for read, detail in (
            (self.ref(A), 'remote branch no longer matches approved head'),
            (ForgeError('gh: branch unreadable (HTTP 403)', 403),
             'gh: branch unreadable (HTTP 403)'),
        ):
            with self.subTest(detail=detail):
                with self.assertRaises(ForgeError) as caught:
                    self.delete(
                        self.ref(), self.repository(), self.refused(422),
                        read,
                    )
                self.assertEqual(
                    str(caught.exception),
                    'gh: Reference does not exist (HTTP 422); '
                    f'after the failed DELETE: {detail}',
                )

    def test_successful_delete_still_requires_confirmed_absence(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ForgeError, '^remote branch remains: feat/topic$'
        ):
            self.delete(self.ref(), self.repository(), None, self.ref())
        with self.assertRaisesRegex(
            ForgeError, '^remote branch no longer matches approved head$'
        ):
            self.delete(self.ref(), self.repository(), None, self.ref(A))


def listed_node(number: int, **fields: object) -> dict:
    """One SquadIssues node (#164), closed in milestone v1 by default."""
    return {
        'id': f'I-{number}', 'number': number, 'title': f'T{number}',
        'state': 'CLOSED', 'stateReason': 'COMPLETED',
        'milestone': {'title': 'v1'},
        'labels': page([{'name': 'bug'}]),
        'closedByPullRequestsReferences': page([]),
        **fields,
    }


def closing(number: int, merged: bool = True,
            repository: str = 'org/repo') -> dict:
    return {'number': number, 'merged': merged,
            'repository': {'nameWithOwner': repository}}


class IssueListingTests(unittest.TestCase):
    """The GitHub reads behind ``issue list`` (#164)."""

    def setUp(self) -> None:
        from agent_squad.forge import IssueQuery, Milestone

        path = Path('/repo')
        with patch('agent_squad.github.shutil.which',
                   return_value='/fake/gh'):
            self.forge = GitHub(
                Repository(path, path, path / '.git', config()), 'reviewer'
            )
        self.query = IssueQuery
        self.v1 = Milestone(5, 'v1', 'closed')

    def listing(self, *pages: dict) -> list[dict]:
        return [{'repository': {'issues': value}} for value in pages]

    def test_milestones_read_every_state_and_page(self) -> None:
        from agent_squad.forge import Milestone

        rows = [{'number': i, 'title': f'v{i}', 'state': 'open'}
                for i in range(1, 101)]
        with patch.object(self.forge, 'api', side_effect=[
            rows, [{'number': 101, 'title': 'v0.8.0', 'state': 'closed'}],
        ]) as api:
            found = self.forge.milestones()
        self.assertEqual(len(found), 101)
        self.assertEqual(found[-1], Milestone(101, 'v0.8.0', 'closed'))
        self.assertEqual([c.args[0] for c in api.call_args_list], [
            '/repos/org/repo/milestones?state=all&per_page=100&page=1',
            '/repos/org/repo/milestones?state=all&per_page=100&page=2',
        ])
        for row, message in (
            ({'number': 1, 'title': 'v1', 'state': 'gone'}, 'state'),
            ({'number': 0, 'title': 'v1', 'state': 'open'}, 'number'),
            ({'number': 1, 'title': None, 'state': 'open'}, 'title'),
        ):
            with self.subTest(row=row), \
                    patch.object(self.forge, 'api', return_value=[row]):
                with self.assertRaisesRegex(ForgeError, message):
                    self.forge.milestones()

    def test_issue_milestone_is_read_and_checked(self) -> None:
        from agent_squad.github import parse_issue

        base = dict(forge_evidence('Body'), number=7, title='T',
                    state='open', labels=[])
        for value, expected in (
            (None, None), ({'number': 5, 'title': 'v1'}, 'v1'),
        ):
            with self.subTest(value=value):
                record = parse_issue({**base, 'milestone': value})
                self.assertEqual(record['milestone'], expected)
        # A record without the field reads as no milestone.
        self.assertIsNone(parse_issue(base)['milestone'])
        for value in ('v1', {'title': 5}, {'number': 5}, []):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ForgeError, 'issue.milestone'):
                    parse_issue({**base, 'milestone': value})

    def test_listing_sends_variables_and_pages_with_a_cursor(self) -> None:
        query = self.query('closed', self.v1, ('bug', 'a "b"'))
        pages = self.listing(
            page([listed_node(1, labels=page(
                [{'name': 'bug'}, {'name': 'a "b"'}]))], 'c1'),
            page([listed_node(2)]),
        )
        with patch.object(self.forge, 'graphql', side_effect=pages) as gql:
            found = self.forge.issues(query)
        # The second issue lacks one label and is dropped.
        self.assertEqual([row['number'] for row in found], [1])
        first, second = gql.call_args_list
        self.assertIn('query SquadIssues(', first.args[0])
        self.assertNotIn('a "b"', first.args[0])
        self.assertNotIn('v1', first.args[0])
        self.assertEqual(first.args[1], {
            'owner': 'org', 'repo': 'repo', 'states': ['CLOSED'],
            'labels': ['bug', 'a "b"'],
            'filter': {'milestoneNumber': '5'}, 'cursor': None,
        })
        self.assertEqual(second.args[1]['cursor'], 'c1')
        with patch.object(self.forge, 'graphql', side_effect=self.listing(
            page([], 'c1'), page([], 'c1'),
        )):
            with self.assertRaisesRegex(ForgeError, 'repeated a cursor'):
                self.forge.issues(query)

    def test_only_the_closed_listing_asks_for_closing_prs(self) -> None:
        # PR #167 REV-3: an open issue's closing pull requests are never
        # asked for, and --state all reads one listing per state.
        with patch.object(self.forge, 'graphql', side_effect=self.listing(
            page([listed_node(1, state='OPEN', stateReason=None)], 'o1'),
            page([]), page([listed_node(2)]),
        )) as gql:
            found = self.forge.issues(self.query('all', None, ()))
        self.assertEqual([row['number'] for row in found], [1, 2])
        self.assertEqual([call.args[1] for call in gql.call_args_list], [
            {'owner': 'org', 'repo': 'repo', 'states': [state],
             'cursor': cursor}
            for state, cursor in (('OPEN', None), ('OPEN', 'o1'),
                                  ('CLOSED', None))
        ])
        open_text, _, closed_text = (
            call.args[0] for call in gql.call_args_list)
        from agent_squad.github import CLOSING_PRS, selection

        self.assertNotIn('closedByPullRequestsReferences', open_text)
        # The texts differ only by the closing pull requests' selection.
        self.assertEqual(
            closed_text.replace('\n        ' + selection(CLOSING_PRS), ''),
            open_text)
        self.assertNotEqual(closed_text, open_text)
        for state in ('open', 'closed'):
            with self.subTest(state=state), patch.object(
                self.forge, 'graphql', side_effect=self.listing(page([])),
            ) as gql:
                self.forge.issues(self.query(state, None, ()))
            self.assertEqual(gql.call_args.args[1]['states'],
                             [state.upper()])
            self.assertEqual(
                'closedByPullRequestsReferences' in gql.call_args.args[0],
                state == 'closed')

    def test_rows_hold_merged_closing_prs_in_order(self) -> None:
        node = listed_node(
            3, stateReason='NOT_PLANNED',
            closedByPullRequestsReferences=page([
                closing(9), closing(4, merged=False), closing(2, True, 'x/y'),
                closing(9),
            ], 'p1'),
        )
        rest = {'node': {'closedByPullRequestsReferences': page([
            closing(1)])}}
        with patch.object(self.forge, 'graphql', side_effect=[
            *self.listing(page([node])), rest,
        ]) as gql:
            found = self.forge.issues(self.query('closed', None, ()))
        self.assertEqual(found, [{
            'number': 3, 'title': 'T3', 'state': 'closed',
            'state_reason': 'not_planned', 'labels': ['bug'],
            'milestone': 'v1', 'is_pull_request': False,
            'closed_by': [
                {'number': 1, 'repository': 'org/repo'},
                {'number': 9, 'repository': 'org/repo'},
                {'number': 2, 'repository': 'x/y'},
            ],
        }])
        # The further page is read through the issue's node.
        self.assertIn('... on Issue', gql.call_args.args[0])
        self.assertEqual(gql.call_args.args[1], {'id': 'I-3', 'cursor': 'p1'})

    def test_closing_prs_are_not_read_for_a_row_that_fails(self) -> None:
        open_issue = listed_node(4, state='OPEN', stateReason=None)
        # The open listing does not select the closing pull requests.
        del open_issue['closedByPullRequestsReferences']
        other = listed_node(
            5, milestone=None,
            closedByPullRequestsReferences=page([closing(8)], 'p1'),
        )
        with patch.object(self.forge, 'graphql', side_effect=self.listing(
            page([open_issue]), page([other]),
        )) as gql:
            found = self.forge.issues(self.query('all', self.v1, ()))
        # One listing per state and no further page.
        self.assertEqual(gql.call_count, 2)
        self.assertEqual(found, [{
            'number': 4, 'title': 'T4', 'state': 'open',
            'state_reason': None, 'labels': ['bug'], 'milestone': 'v1',
            'is_pull_request': False, 'closed_by': [],
        }])

    def test_a_refused_further_closing_page_leaves_that_issue_unknown(
        self,
    ) -> None:
        # PR #167 REV-2: the first page came with the listing, so a refused
        # further page leaves only that issue's closing pull requests unknown.
        from agent_squad.github import CommandFailed

        def nodes() -> list[dict]:
            return self.listing(page([
                listed_node(7, closedByPullRequestsReferences=page(
                    [closing(8)], 'p1')),
                listed_node(6, closedByPullRequestsReferences=page(
                    [closing(5)])),
            ]))

        for status in (401, 403, 404):
            with self.subTest(status=status), patch.object(
                self.forge, 'graphql', side_effect=[
                    *nodes(), CommandFailed('refused', status, ''),
                ],
            ):
                found = self.forge.issues(self.query('closed', None, ()))
                self.assertEqual(
                    [(row['number'], row['closed_by']) for row in found],
                    [(7, None),
                     (6, [{'number': 5, 'repository': 'org/repo'}])])
        # A service error, and a typed GraphQL error, which GitHub answers
        # with HTTP 200 and so carries no status, still fail the read.
        for error in (CommandFailed('server', 502, ''),
                      ForgeError('FORBIDDEN: no access')):
            with self.subTest(error=str(error)), patch.object(
                self.forge, 'graphql', side_effect=[*nodes(), error],
            ):
                with self.assertRaisesRegex(ForgeError, str(error)):
                    self.forge.issues(self.query('closed', None, ()))

    def test_malformed_listing_rows_fail_the_read(self) -> None:
        missing = listed_node(6)
        del missing['milestone']
        for node, message in (
            (listed_node(6, state='MERGED'), 'issue.state'),
            (missing, 'issue.milestone is missing'),
            (listed_node(6, milestone={'title': None}), 'milestone.title'),
            (listed_node(6, stateReason=3), 'stateReason'),
            (listed_node(6, labels=page([{'name': ''}])), 'label.name'),
            (listed_node(6, closedByPullRequestsReferences=page(
                [closing(7, True, 'no-owner')])), 'owner/name'),
            (listed_node(6, closedByPullRequestsReferences=page(
                [{**closing(7), 'merged': None}])), 'merged'),
        ):
            with self.subTest(message=message), patch.object(
                self.forge, 'graphql',
                side_effect=self.listing(page([node])),
            ):
                with self.assertRaisesRegex(ForgeError, message):
                    self.forge.issues(self.query('closed', None, ()))
