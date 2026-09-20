"""Regression cases for GitHub's quota, launch and audit reliability backlog."""
import contextlib
import importlib.util
import io
import json
import shlex
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rightsize as r

spec = importlib.util.spec_from_file_location('hook', Path('hooks/claude_pretooluse.py'))
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
CONFIG = json.loads(Path('config.json').read_text())
TASK = 'Review calculator input validation and preserve existing boundary behavior'


class Reliability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = patch.object(r, 'STATE', Path(self.tmp.name) / 'state.json')
        self.state.start()
        self.addCleanup(self.state.stop)

    def probes(self):
        return {'opencode': {'name': 'opencode', 'status': 'ok', 'buckets': [
            {'id': 'rolling', 'percent': 0, 'source': 'live', 'raw_status': 'ok', 'resets_at': r.now() + 100},
            {'id': 'weekly', 'percent': None, 'source': 'unavailable', 'raw_status': 'rate-limited', 'resets_at': r.now() + 1000},
            {'id': 'monthly', 'percent': 50, 'source': 'live', 'raw_status': 'ok', 'resets_at': r.now() + 10000}]}}

    def test_denial_vetoes_every_band_and_remembered_capacity(self):
        r.save_json(r.STATE, {'snapshots': {'opencode:weekly': {'at': r.now(), 'percent': 1}}})
        elig = r.eligibility(CONFIG, self.probes(), record=False)
        for band in (1, 2, 3):
            pick, _ = r.pick(CONFIG['bands'][str(band)], elig, band, config=CONFIG, relax_pace=True)
            self.assertIsNone(pick)
        self.assertIn('weekly', elig['opencode']['blocked'])
        healthy = self.probes()
        healthy['opencode']['buckets'][1].update(percent=1, source='live', raw_status='ok')
        self.assertTrue(r.eligibility(CONFIG, healthy, record=False)['opencode']['eligible'])

    def test_cooldown_uses_denied_windows_not_order(self):
        for reverse in (False, True):
            probes = self.probes()
            expected = probes['opencode']['buckets'][1]['resets_at']
            if reverse:
                probes['opencode']['buckets'].reverse()
            r.save_json(r.STATE, {'probe_cache': {'at': r.now(), 'probes': probes}})
            with contextlib.redirect_stdout(io.StringIO()):
                r.main(['report', 'opencode', '--quota-error'])
            state = r.load_json(r.STATE)
            self.assertEqual(state['exhausted']['opencode'], expected)
            self.assertNotIn('probe_cache', state)
            with contextlib.redirect_stdout(io.StringIO()):
                r.main(['report', 'opencode', '--minutes', '1'])
            self.assertEqual(r.exhausted_until('opencode'), expected)

    def test_unknown_failure_does_not_borrow_healthy_reset(self):
        probes = self.probes()
        probes['opencode']['buckets'][1].update(raw_status='unavailable')
        r.save_json(r.STATE, {'probe_cache': {'probes': probes}})
        before = r.now()
        with contextlib.redirect_stdout(io.StringIO()):
            r.main(['report', 'opencode', '--minutes', '2'])
        self.assertAlmostEqual(r.exhausted_until('opencode'), before + 120, delta=2)

    def test_shipped_launcher_finds_actual_worker(self):
        command = CONFIG['launchers']['orca']['default'].format(
            name='review', model_ref='opencode-go/glm-5.3', spec="'" + TASK + "'")
        self.assertEqual(hook.spec_text(hook.launch_segment(command)), TASK)

    def test_started_receipt_is_exact_and_idempotent(self):
        with patch.object(r, 'judge', side_effect=AssertionError('must not reroute')):
            args = ['report', 'opencode', '--started', '--model', 'glm-5.3',
                    '--task', TASK, '--dispatch', 'ctx_proof', '--worktree', '/repo/actual-name']
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(r.main(args), 0)
                self.assertEqual(r.main(args), 0)
        state = r.load_json(r.STATE)
        self.assertEqual(len(state['reservations']), 1)
        self.assertEqual(state['reservations'][0]['provider'], 'opencode')
        self.assertEqual(state['decisions'][0]['dispatch'], 'ctx_proof')
        self.assertEqual(state['decisions'][0]['name'], 'actual-name')

    def test_missing_evidence_is_not_never_launched(self):
        r.save_json(r.STATE, {'decisions': [{'at': r.now(), 'provider': 'opencode',
            'model': 'glm-5.3', 'band': 3, 'name': '425', 'task': TASK, 'dispatched': False}]})
        with patch.object(r, 'orca_settled', return_value=([], None)), \
             patch.object(r, 'orca_task_index', return_value={}), \
             patch.object(r, 'opencode_sessions_since', return_value=[]):
            result = r.audit(CONFIG)
        self.assertEqual(result['decisions'][0]['verdict'], 'unknown dispatch')

    def test_denial_survives_missing_refresh_until_healthy(self):
        r.eligibility(CONFIG, self.probes(), record=True)
        missing = self.probes()
        missing['opencode']['buckets'][1]['raw_status'] = 'unavailable'
        self.assertFalse(r.eligibility(CONFIG, missing, record=True)['opencode']['eligible'])
        missing['opencode']['buckets'][1].update(percent=0, source='live', raw_status='ok')
        self.assertTrue(r.eligibility(CONFIG, missing, record=True)['opencode']['eligible'])

    def test_multiple_denied_windows_and_unknown_reset(self):
        probes = self.probes()
        monthly = probes['opencode']['buckets'][2]
        monthly.update(percent=100)
        r.save_json(r.STATE, {'probe_cache': {'probes': probes}})
        with contextlib.redirect_stdout(io.StringIO()):
            r.main(['report', 'opencode'])
        self.assertEqual(r.exhausted_until('opencode'), monthly['resets_at'])
        monthly['resets_at'] = None
        r.save_json(r.STATE, {'probe_cache': {'probes': probes}})
        before = r.now()
        with contextlib.redirect_stdout(io.StringIO()):
            r.main(['report', 'opencode', '--minutes', '2'])
        self.assertAlmostEqual(r.exhausted_until('opencode'), before + 120, delta=2)

    def test_hook_records_shipped_launch_without_second_judgment(self):
        task = TASK + "; don't change outputs | preserve tests"
        command = CONFIG['launchers']['orca']['default'].format(
            name='review', model_ref='opencode-go/glm-5.3', spec=shlex.quote(task))
        response = {'stdout': json.dumps({'ok': True, 'result': {'worker': {
            'dispatchId': 'ctx_hook', 'resource': {'worktreeId': 'repo::/repo/review'}}}})}
        event = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                 'tool_use_id': 'tool_hook', 'tool_input': {'command': command}, 'tool_response': response}
        with patch.object(hook.sys, 'stdin', io.StringIO(json.dumps(event))), \
             patch.object(hook, 'advise', side_effect=AssertionError('must not judge')), \
             patch.object(hook.subprocess, 'run') as run, \
             contextlib.redirect_stdout(io.StringIO()):
            hook.main()
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:4], ['report', 'opencode', '--started'])
        self.assertEqual(argv[argv.index('--task') + 1], task)
        self.assertEqual(argv[argv.index('--model') + 1], 'glm-5.3')
        self.assertEqual(argv[argv.index('--dispatch') + 1], 'ctx_hook')
        self.assertEqual(argv[argv.index('--worktree') + 1], '/repo/review')

    def test_hook_rejects_failed_or_unknown_launch(self):
        command = 'orca orchestration worker-start --agent codex --model gpt-6-astra --spec ' + shlex.quote(TASK)
        segment = hook.launch_segment(command)
        for response in ({'success': False}, {'exit_code': 1}, {'is_error': True},
                         {'stdout': '{"ok": false}'}, {'ok': False}):
            self.assertIsNone(hook.launch_receipt(command, segment, {
                'tool_response': response, 'tool_use_id': 'tool_failed'}))
        unknown = 'orca orchestration worker-start --agent opencode --spec ' + shlex.quote(TASK)
        self.assertIsNone(hook.launch_receipt(unknown, hook.launch_segment(unknown), {
            'tool_response': {}, 'tool_use_id': 'tool_unknown'}))

    def test_new_receipt_releases_only_its_dispatch(self):
        r.record_launch(CONFIG, 'codex', 'gpt-6-astra', TASK, 'ctx_new', '/repo/reused')
        with patch.object(r, 'orca_settled', return_value=([
                {'dispatch': 'ctx_old', 'state': 'succeeded', 'worktree': 'reused'}], None)), \
             patch.object(r, 'orca_settled_tasks', return_value={r.brief_key(TASK)}):
            self.assertEqual(len(r.release_settled()['kept']), 1)
        with patch.object(r, 'orca_settled', return_value=([
                {'dispatch': 'ctx_new', 'state': 'succeeded', 'worktree': 'reused'}], None)), \
             patch.object(r, 'orca_settled_tasks', return_value=set()):
            self.assertEqual(r.release_settled()['released'][0]['matched'], 'dispatch')

    def test_audit_json_and_text_show_untracked_sessions(self):
        result = dict(decisions=[], counts={}, sessions_seen=1, held=[], untracked_sessions=[
            dict(verdict='zero output', directory='/repo/renamed', model='glm-5.3', session_id='s1', tokens_out=0)])
        with patch.object(r, 'audit', return_value=result):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                r.main(['audit', '--json'])
            self.assertEqual(json.loads(output.getvalue()), result)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                r.main(['audit'])
            self.assertIn('zero output', output.getvalue())

    def test_reused_checkout_multiple_attempts_are_ambiguous(self):
        r.record_launch(CONFIG, 'opencode', 'glm-5.3', TASK, 'ctx_a', '/repo/actual')
        sessions = [dict(directory='/repo/actual', session_id=s, model='glm-5.3',
                         provider='opencode-go', messages=2, last_at=r.now(), started_at=r.now(),
                         tokens_out=20) for s in ('s1', 's2')]
        with patch.object(r, 'orca_settled', return_value=([], None)), \
             patch.object(r, 'orca_task_index', return_value={}), \
             patch.object(r, 'opencode_sessions_since', return_value=sessions):
            result = r.audit(CONFIG)
        self.assertEqual(result['decisions'][0]['verdict'], 'ambiguous session')

    def test_sqlite_session_identity_and_zero_output(self):
        dbpath = Path(self.tmp.name) / 'opencode.db'
        with contextlib.closing(sqlite3.connect(dbpath)) as db:
            db.executescript("""
                CREATE TABLE session (id TEXT, directory TEXT, time_created INTEGER,
                  tokens_input INTEGER, tokens_output INTEGER, tokens_cache_read INTEGER, cost REAL);
                CREATE TABLE message (session_id TEXT, data TEXT, time_created INTEGER);
            """)
            stamp = int(r.now() * 1000)
            db.execute('INSERT INTO session VALUES (?,?,?,?,?,?,?)',
                       ('ses_sql', '/repo/renamed', stamp, 0, 0, 0, 0))
            db.execute('INSERT INTO message VALUES (?,?,?)', ('ses_sql', json.dumps({
                'role': 'assistant', 'modelID': 'glm-5.3', 'providerID': 'opencode-go'}), stamp))
            db.commit()
        with patch.object(r, 'OPENCODE_DB', dbpath):
            rows = r.opencode_sessions_since(r.now() - 60)
        self.assertEqual(rows[0]['session_id'], 'ses_sql')
        self.assertEqual(rows[0]['tokens_out'], 0)
        self.assertEqual(rows[0]['started_at'], stamp / 1000)

    def test_exact_launch_matches_renamed_worktree(self):
        r.record_launch(CONFIG, 'opencode', 'glm-5.3', TASK, 'ctx_exact', '/repo/renamed')
        session = dict(directory='/repo/renamed', session_id='ses_exact', model='glm-5.3',
                       provider='opencode-go', messages=2, last_at=r.now(), started_at=r.now(), tokens_out=10)
        with patch.object(r, 'orca_settled', return_value=([
                {'dispatch': 'ctx_exact', 'state': 'succeeded', 'worktree': 'renamed'}], None)), \
             patch.object(r, 'orca_task_index', return_value={}), \
             patch.object(r, 'opencode_sessions_since', return_value=[session]):
            result = r.audit(CONFIG)
        self.assertEqual(result['decisions'][0]['verdict'], 'model verified')
        self.assertEqual(result['decisions'][0]['session_id'], 'ses_exact')
        self.assertEqual(result['untracked_sessions'], [])

    def test_unmatched_zero_output_session_is_visible(self):
        session = dict(directory='/repo/issue-425', session_id='ses_a', model='glm-5.3',
                       provider='opencode-go', messages=1, last_at=r.now(), started_at=r.now(),
                       tokens_out=0)
        with patch.object(r, 'orca_settled', return_value=([], None)), \
             patch.object(r, 'orca_task_index', return_value={}), \
             patch.object(r, 'opencode_sessions_since', return_value=[session]):
            result = r.audit(CONFIG)
        self.assertEqual(result['untracked_sessions'][0]['verdict'], 'zero output')


if __name__ == '__main__':
    unittest.main()
