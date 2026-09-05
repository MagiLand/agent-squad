from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._support import add_src_to_path, run, run_cli
from tests.integration.test_review_applications import _marker_confirmed_review
from tests.integration.test_review_submissions import _prepare_round, _write_review
from tests.integration.test_supersessions import (
    _LateSubmittingReviewerClient, _prepared_from_active,
)

add_src_to_path()
from agent_squad import review_applications, runs
from agent_squad.herdr import HerdrError


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

    def approve(self, root):
        prepared, review = _marker_confirmed_review(root)
        result = self.command(
            prepared, 'apply-review', '--result-id', review['result_id']
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return prepared

    def test_changed_post_approval_submission_and_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = self.approve(root)
            state = self.state(prepared)
            report = root / 'next.md'
            report.write_text('# Report\n\nChanged candidate.\n')
            for mode in ('new_revision', 'reconsideration'):
                result = self.command(
                    prepared, 'submit', '--report', str(report), '--mode', mode
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.state(prepared), state)
            run(['git', '-c', 'commit.gpgSign=false', 'commit', '--allow-empty',
                 '-m', 'changed candidate'], cwd=prepared.repository)
            self.assertEqual(self.command(prepared, 'complete').returncode, 1)
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
            (fresh.bundle / 'output/review.json').write_text(json.dumps(review))
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

    def test_cancel_all_nonterminal_phases_and_restart(self):
        for phase in ('implementing', 'reviewing', 'approved', 'needs_human'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                prepared = (self.approve(root) if phase == 'approved'
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
                self.assertEqual({p: p.read_bytes() for p in evidence}, evidence)
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
                    self.assertIn('run_cancelled', closed['supersession']['cause'])
                for _ in range(2):
                    replay = self.command(
                        prepared, 'cancel', '--reason', 'retry'
                    )
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                self.assertEqual(events_path.read_bytes(), events)
                self.assertEqual(self.state(prepared), terminal)
                self.assertEqual(self.command(prepared, 'complete').returncode, 1)
                started = self.command(
                    prepared, 'start', '--task', str(root / 'task.md'),
                    '--base', 'main',
                )
                self.assertEqual(started.returncode, 0, started.stderr)
                self.assertNotEqual(
                    self.state(prepared)['active_run_id'], state['active_run_id']
                )
                cancelled = self.command(
                    prepared, 'cancel', '--reason', 'stop before first review'
                )
                self.assertEqual(cancelled.returncode, 0, cancelled.stderr)

    def test_late_review_cannot_change_cancelled_state(self):
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

    def test_notice_and_cleanup_failures_do_not_undo_cancellation(self):
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
            self.assertIsNone(runs.inspect_status(prepared.repository).active_run)
            self.assertTrue(prepared.review_worktree.exists())

    def test_cancellation_write_failure_preserves_active_review(self):
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
                with self.assertRaises(review_applications.ReviewApplicationError):
                    review_applications.cancel_run(
                        prepared.repository, reason='stop', herdr_client=client
                    )
            self.assertEqual(self.state(prepared), state)
            self.assertEqual({p: p.read_bytes() for p in paths}, original)
            client.discover.assert_not_called()
            self.assertTrue(prepared.bundle.exists())

    def test_completion_cleanup_failure_still_releases_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            prepared = self.approve(Path(temporary))
            with mock.patch.object(
                review_applications, '_cleanup_review_resources',
                side_effect=OSError('cleanup unavailable'),
            ):
                result = review_applications.complete_run(prepared.repository)
            self.assertEqual(len(result.cleanup_warnings), 1)
            self.assertIsNone(runs.inspect_status(prepared.repository).active_run)
            self.assertTrue(
                review_applications.complete_run(prepared.repository).already_completed
            )

    def test_invalid_report_preserves_prior_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = self.approve(root)
            state = self.state(prepared)
            run(['git', '-c', 'commit.gpgSign=false', 'commit', '--allow-empty',
                 '-m', 'changed candidate'], cwd=prepared.repository)
            result = self.command(
                prepared, 'submit', '--report', str(root / 'missing.md'),
                '--mode', 'new_revision',
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(self.state(prepared), state)
