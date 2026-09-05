from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path, run, run_cli
from tests.integration.test_review_applications import _marker_confirmed_review
from tests.integration.test_review_submissions import (
    _prepare_round, _write_review,
)
from tests.integration.test_supersessions import (
    _LateSubmittingReviewerClient, _prepared_from_active,
)

add_src_to_path()
from agent_squad import review_applications, runs  # noqa: E402
from agent_squad.herdr import HerdrError  # noqa: E402


class TerminalTransitionTests(unittest.TestCase):
    def command(self, prepared, *arguments):
        return run_cli(
            prepared.repository, *arguments, data_home=prepared.data_home,
            env_overrides=prepared.environment,
        )

    def state(self, prepared):
        return json.loads(
            (prepared.repository / '.agent-squad/state.json').read_text()
        )

    def approve(self, root, *, retain_review=False):
        prepared, review = _marker_confirmed_review(root)
        if retain_review:
            with mock.patch.object(
                review_applications, '_cleanup_review_resources',
                return_value=(),
            ):
                review_applications.apply_review(
                    prepared.repository, result_id=review['result_id']
                )
        else:
            result = self.command(
                prepared, 'apply-review', '--result-id', review['result_id']
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        return prepared

    def test_changed_post_approval_submission_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = self.approve(root)
            state = self.state(prepared)
            report = root / 'next.md'
            report.write_text('# Report\n\nChanged candidate.\n')
            for mode, message in (
                ('new_revision', 'from approved requires a different HEAD'),
                ('reconsideration', 'from approved must use new_revision'),
            ):
                result = self.command(
                    prepared, 'submit', '--report', str(report), '--mode', mode
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.state(prepared), state)
            run(
                ['git', '-c', 'commit.gpgSign=false', 'commit',
                 '--allow-empty',
                 '-m', 'changed candidate'], cwd=prepared.repository,
            )
            self.assertEqual(self.command(prepared, 'complete').returncode, 1)
            refused = self.command(
                prepared, 'submit', '--report', str(report),
                '--mode', 'new_revision', '--response', str(report),
            )
            self.assertEqual(refused.returncode, 1)
            self.assertIn('without --response', refused.stderr)
            self.assertEqual(self.state(prepared), state)
            result = self.command(
                prepared, 'submit', '--report', str(report),
                '--mode', 'new_revision',
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            updated = self.state(prepared)
            self.assertEqual(updated['phase'], 'reviewing')
            self.assertIsNone(updated['approved_head_oid'])
            self.assertEqual(updated['current_round'], 2)
            self.assertNotEqual(
                updated['current_head_oid'], state['approved_head_oid']
            )
            fresh = _prepared_from_active(prepared)
            review = _write_review(fresh)
            review['result_id'] = '77777777-7777-4777-8777-777777777777'
            (fresh.bundle / 'output/review.json').write_text(
                json.dumps(review)
            )
            submitted = run_cli(
                fresh.review_worktree, 'review-submit',
                data_home=fresh.data_home, env_overrides=fresh.environment,
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr)
            applied = self.command(
                fresh, 'apply-review', '--result-id', review['result_id']
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            for _ in range(2):
                completed = self.command(fresh, 'complete')
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_cancel_all_nonterminal_phases_and_restart(self) -> None:
        for phase in ('implementing', 'reviewing', 'approved', 'needs_human'):
            with (
                self.subTest(phase=phase),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                prepared = (self.approve(root, retain_review=True)
                            if phase == 'approved'
                            else _prepare_round(root))
                if phase == 'implementing':
                    result = self.command(
                        prepared, 'supersede', '--reason', 'replace review'
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                if phase == 'needs_human':
                    result = self.command(prepared, 'escalate')
                    self.assertEqual(result.returncode, 0, result.stderr)
                state = self.state(prepared)
                directory = (prepared.repository / '.agent-squad/runs'
                             / state['active_run_id'])
                evidence = {
                    path: path.read_bytes() for path in directory.rglob('*')
                    if path.is_file() and path.name not in {
                        'run.json', 'round.json', 'events.jsonl'
                    }
                }
                result = self.command(
                    prepared, 'cancel', '--reason', 'Developer stopped task'
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    {p: p.read_bytes() for p in evidence}, evidence
                )
                self.assertFalse(prepared.review_worktree.exists())
                terminal = self.state(prepared)
                self.assertEqual(terminal['phase'], 'cancelled')
                for field in ('active_run_id', 'approved_head_oid',
                              'active_escalation_id'):
                    self.assertIsNone(terminal[field])
                events_path = directory / 'events.jsonl'
                events = events_path.read_bytes()
                event = [json.loads(line) for line in events.splitlines()
                         if json.loads(line)['event'] == 'run_cancelled'][0]
                self.assertEqual(event['cause'], 'Developer stopped task')
                self.assertEqual(event['previous_phase'], phase)
                self.assertTrue(event['actor'])
                if phase == 'reviewing':
                    closed = json.loads(
                        (directory / 'rounds/001/round.json').read_text()
                    )
                    self.assertEqual(closed['status'], 'superseded')
                    self.assertIn(
                        'run_cancelled', closed['supersession']['cause']
                    )
                    invocation_path = (
                        Path(prepared.environment['FAKE_HERDR_STATE_DIR'])
                        / 'invocations.jsonl'
                    )
                    invocations = [json.loads(line) for line in
                                   invocation_path.read_text().splitlines()]
                    notices = [
                        event['arguments'][3] for event in invocations
                        if event['arguments'][:2] == ['agent', 'prompt']
                        and len(event['arguments']) > 3
                        and 'RUN_CANCELLED' in event['arguments'][3]
                    ]
                    self.assertEqual(notices, [
                        'AGENT_SQUAD/0.4.4 RUN_CANCELLED\n\n'
                        f"run_id: {state['active_run_id']}\n"
                        'round: 1\nreason: Developer stopped task\n\n'
                        'The Agent Squad run has been cancelled.\n'
                        'Do not continue or submit a current review result.'
                    ])
                for _ in range(2):
                    replay = self.command(
                        prepared, 'cancel', '--reason', 'retry'
                    )
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                self.assertEqual(events_path.read_bytes(), events)
                self.assertEqual(self.state(prepared), terminal)
                self.assertEqual(
                    self.command(prepared, 'complete').returncode, 1
                )
                started = self.command(
                    prepared, 'start', '--task', str(root / 'task.md'),
                    '--base', 'main',
                )
                self.assertEqual(started.returncode, 0, started.stderr)
                self.assertNotEqual(
                    self.state(prepared)['active_run_id'],
                    state['active_run_id'],
                )
                cancelled = self.command(
                    prepared, 'cancel', '--reason', 'stop before first review'
                )
                self.assertEqual(cancelled.returncode, 0, cancelled.stderr)

    def test_late_review_cannot_change_cancelled_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = _prepare_round(Path(temporary))
            review = _write_review(prepared)
            client = _LateSubmittingReviewerClient(prepared)
            result = review_applications.cancel_run(
                prepared.repository, reason='stop', herdr_client=client
            )
            self.assertFalse(result.already_cancelled)
            self.assertEqual(client.phase_at_notice, 'cancelled')
            state = self.state(prepared)
            applied = self.command(
                prepared, 'apply-review', '--result-id', review['result_id']
            )
            self.assertEqual(applied.returncode, 1)
            self.assertEqual(self.state(prepared), state)

    def test_notice_and_cleanup_failures_do_not_undo_cancellation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = _prepare_round(Path(temporary))
            client = mock.Mock()
            client.discover.side_effect = HerdrError('unavailable')
            with mock.patch.object(
                review_applications, '_cleanup_superseded_review_resources',
                side_effect=OSError('cleanup unavailable'),
            ):
                result = review_applications.cancel_run(
                    prepared.repository, reason='stop', herdr_client=client
                )
            self.assertEqual(len(result.cleanup_warnings), 2)
            self.assertIsNone(
                runs.inspect_status(prepared.repository).active_run
            )
            self.assertTrue(prepared.review_worktree.exists())

    def test_cancellation_write_failure_preserves_active_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = _prepare_round(Path(temporary))
            state = self.state(prepared)
            directory = (prepared.repository / '.agent-squad/runs'
                         / state['active_run_id'])
            paths = [directory / 'run.json', directory / 'events.jsonl',
                     directory / 'rounds/001/round.json']
            original = {path: path.read_bytes() for path in paths}
            write = review_applications.atomic_write

            def fail_state(path, content, **kwargs):
                if path.name == 'state.json':
                    raise OSError('injected state write failure')
                return write(path, content, **kwargs)

            client = mock.Mock()
            with mock.patch.object(
                review_applications, 'atomic_write', side_effect=fail_state,
            ):
                with self.assertRaises(
                    review_applications.ReviewApplicationError
                ):
                    review_applications.cancel_run(
                        prepared.repository, reason='stop', herdr_client=client
                    )
            self.assertEqual(self.state(prepared), state)
            self.assertEqual({p: p.read_bytes() for p in paths}, original)
            client.discover.assert_not_called()
            self.assertTrue(prepared.bundle.exists())

    def test_completion_cleanup_failure_still_releases_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = self.approve(Path(temporary))
            with mock.patch.object(
                review_applications, '_cleanup_review_resources',
                side_effect=OSError('cleanup unavailable'),
            ):
                result = review_applications.complete_run(prepared.repository)
            self.assertEqual(len(result.cleanup_warnings), 1)
            self.assertIsNone(
                runs.inspect_status(prepared.repository).active_run
            )
            replay = review_applications.complete_run(prepared.repository)
            self.assertTrue(replay.already_completed)

    def test_invalid_report_preserves_prior_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = self.approve(root)
            state = self.state(prepared)
            run(
                ['git', '-c', 'commit.gpgSign=false', 'commit',
                 '--allow-empty',
                 '-m', 'changed candidate'], cwd=prepared.repository,
            )
            result = self.command(
                prepared, 'submit', '--report', str(root / 'missing.md'),
                '--mode', 'new_revision',
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(self.state(prepared), state)

    def test_cancel_after_completion_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = self.approve(Path(temporary))
            review_applications.complete_run(prepared.repository)
            state = self.state(prepared)
            result = self.command(prepared, 'cancel', '--reason', 'stop')
            self.assertEqual(result.returncode, 1)
            self.assertIn('there is no active run to cancel', result.stderr)
            self.assertEqual(self.state(prepared), state)

    def test_cancel_refuses_changed_branch_and_moved_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = _prepare_round(root)
            state = self.state(prepared)
            run(['git', 'switch', '-c', 'different-branch'],
                cwd=prepared.repository)
            result = self.command(prepared, 'cancel', '--reason', 'stop')
            self.assertEqual(result.returncode, 1)
            self.assertIn('branch identity', result.stderr)
            self.assertEqual(self.state(prepared), state)
            moved = root / 'moved-repository'
            prepared.repository.rename(moved)
            result = run_cli(
                moved, 'cancel', '--reason', 'stop',
                data_home=prepared.data_home,
                env_overrides=prepared.environment,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn('does not match', result.stderr)
            self.assertEqual(
                json.loads((moved / '.agent-squad/state.json').read_text()),
                state,
            )

    def test_reviewer_unavailable_warns_and_still_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = _prepare_round(Path(temporary))
            client = mock.Mock()
            client.dispatch_reviewer_notice.return_value = False
            result = review_applications.cancel_run(
                prepared.repository, reason='stop', herdr_client=client
            )
            self.assertEqual(result.cleanup_warnings, (
                'Reviewer cancellation notice could not be delivered',
            ))
            self.assertFalse(prepared.review_worktree.exists())
            self.assertIsNone(self.state(prepared)['active_run_id'])

    def test_cancel_replay_rejects_corrupt_terminal_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = _prepare_round(Path(temporary))
            review_applications.cancel_run(
                prepared.repository, reason='stop', herdr_client=mock.Mock()
            )
            state_path = prepared.repository / '.agent-squad/state.json'
            state = self.state(prepared)
            directory = (prepared.repository / '.agent-squad/runs'
                         / state['terminal_run_id'])
            run_path = directory / 'run.json'
            run_record = json.loads(run_path.read_text())
            cases = [
                (state_path, 'active_run_id', state['terminal_run_id']),
                (state_path, 'terminal_run_id', '../foreign'),
                (state_path, 'updated_at', 'invalid'),
                (state_path, 'base_oid', 'a' * 40),
                (state_path, 'repository_id', 'different'),
                (state_path, 'implementation_root', '/different'),
                (state_path, 'git_common_dir', '/different'),
                (state_path, 'worktree_git_dir', '/different'),
                (state_path, 'approved_head_oid', 'a' * 40),
                (state_path, 'active_escalation_id', 'unexpected'),
                (run_path, 'phase', 'completed'),
                (run_path, 'finished_at', None),
            ]
            for path, field, value in cases:
                with self.subTest(field=field, path=path.name):
                    original = path.read_bytes()
                    changed = copy.deepcopy(
                        state if path == state_path else run_record
                    )
                    changed[field] = value
                    path.write_text(json.dumps(changed))
                    corrupted = path.read_bytes()
                    try:
                        with self.assertRaises(
                            review_applications.ReviewApplicationError
                        ):
                            review_applications.cancel_run(
                                prepared.repository, reason='retry'
                            )
                        self.assertEqual(path.read_bytes(), corrupted)
                    finally:
                        path.write_bytes(original)
            moved = Path(temporary) / 'moved-repository'
            prepared.repository.rename(moved)
            with self.assertRaisesRegex(
                review_applications.ReviewApplicationError,
                'does not match the current worktree',
            ):
                review_applications.cancel_run(moved, reason='retry')

    def test_authoritative_approved_followup_rejects_reconsideration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = self.approve(root)
            previous_head = self.state(prepared)['approved_head_oid']
            run(
                ['git', '-c', 'commit.gpgSign=false', 'commit',
                 '--allow-empty',
                 '-m', 'changed candidate'], cwd=prepared.repository,
            )
            report = root / 'report.md'
            report.write_text('# Changed candidate\n')
            result = self.command(
                prepared, 'submit', '--report', str(report),
                '--mode', 'new_revision',
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = self.state(prepared)
            directory = (prepared.repository / '.agent-squad/runs'
                         / state['active_run_id'] / 'rounds/002')
            request_path = directory / 'request.json'
            round_path = directory / 'round.json'
            request = json.loads(request_path.read_text())
            request.update(mode='reconsideration', head_oid=previous_head)
            request_path.chmod(0o600)
            request_path.write_text(json.dumps(request))
            digest = hashlib.sha256(request_path.read_bytes()).hexdigest()
            record = json.loads(round_path.read_text())
            record.update(mode='reconsideration', head_oid=previous_head)
            record['artifacts']['request']['sha256'] = digest
            for artifact in record['artifacts']['bundle_inputs']:
                if artifact['path'] == 'input/request.json':
                    artifact['sha256'] = digest
            round_path.write_text(json.dumps(record))
            state['active_round']['mode'] = 'reconsideration'
            state['current_head_oid'] = previous_head
            (prepared.repository / '.agent-squad/state.json').write_text(
                json.dumps(state)
            )
            with self.assertRaisesRegex(
                runs.RunStateError,
                'a request after approved must use new_revision',
            ):
                runs.inspect_status(prepared.repository)

    def test_cancel_invalid_reason_names_the_cancel_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = _prepare_round(Path(temporary))
            state = self.state(prepared)
            for reason in ('   ', 'first\nsecond'):
                result = self.command(prepared, 'cancel', '--reason', reason)
                self.assertEqual(result.returncode, 1)
                self.assertIn('cancel --reason must', result.stderr)
                self.assertNotIn('supersede', result.stderr)
                self.assertEqual(self.state(prepared), state)

    def test_cancel_releases_previously_retained_superseded_worktree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = _prepare_round(Path(temporary))
            with mock.patch.object(
                review_applications, '_cleanup_superseded_review_resources',
                return_value=(None, ()),
            ):
                review_applications.supersede_review(
                    prepared.repository, reason='obsolete',
                    herdr_client=mock.Mock(),
                )
            self.assertTrue(prepared.review_worktree.exists())
            result = review_applications.cancel_run(
                prepared.repository, reason='stop', herdr_client=mock.Mock()
            )
            self.assertEqual(result.cleanup_warnings, ())
            self.assertFalse(prepared.review_worktree.exists())
