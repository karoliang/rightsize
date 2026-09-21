"""Consumer routing/receipt/acceptance integration, offline in isolated state."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rightsize as r
import outcome_cli


class ConsumerOutcomes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state.json'
        self.patch = patch.object(r, 'STATE', self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.config = json.loads(r.CONFIG.read_text())
        self.spec = 'Correct token validation in the prescribed auth function; run regressions.'
        self.judgment = dict(tier='high_stakes', size=.4, second_opinion=.1,
                             spec_complete=1, destructive=0, source='caller (test)')
        self.elig = {p: dict(eligible=True, blocked=None, usable=80, unknown=False,
                            overrun=False, resets_at=r.now()+3600, bucket='weekly',
                            inflight=0, reserved=0, buckets=[])
                     for p in ('codex', 'claude', 'opencode')}

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = r.main(args)
        return code, out.getvalue(), err.getvalue()

    def event(self, kind, data, event_id=None):
        path = Path(self.temp.name) / 'event.json'
        path.write_text(json.dumps(data))
        return self.invoke(['outcome', 'record', '--dispatch', 'ctx_test',
                            '--event-id', event_id or kind, '--kind', kind, '--data', str(path)])

    def launch(self, decision, **extra):
        pick = decision['pick']
        args = ['report', pick['provider'], '--started', '--model', pick['model'],
                '--task', self.spec, '--dispatch', 'ctx_test', '--decision-id', decision['decision_id']]
        if pick.get('effort'):
            args += ['--effort', pick['effort']]
        for key, value in extra.items():
            args += ['--' + key.replace('_', '-'), value]
        return self.invoke(args)

    def decision(self):
        d = r.decide(self.judgment, self.config, self.elig)
        r.log_decision(d, self.spec, config=self.config)
        return d

    def test_high_stakes_requires_review_even_below_score_threshold(self):
        d = self.decision()
        self.assertTrue(d['review_required'])
        self.assertIsNotNone(d['review'])
        self.assertNotEqual(d['pick']['provider'], d['review']['provider'])
        self.assertEqual(self.launch(d)[0], 0)
        self.assertEqual(self.launch(d)[0], 0)
        evidence = hashlib.sha256(b'independent checked artifacts').hexdigest()
        self.assertEqual(self.event('completed', {'evidence_sha256': evidence})[0], 0)
        code, _, _ = self.event('accepted', {'actor': 'coordinator', 'evidence_sha256': evidence})
        self.assertEqual(code, 2)
        review = {'actor': 'reviewer', **outcome_cli.model_identity(d['review'], self.config),
                  'verdict': 'accepted', 'evidence_sha256': evidence}
        review.pop('effort')
        self.assertEqual(self.event('review', review)[0], 0)
        self.assertEqual(self.event('accepted', {'actor': 'coordinator', 'evidence_sha256': evidence})[0], 0)
        self.assertEqual(self.event('accepted', {'actor': 'coordinator', 'evidence_sha256': evidence})[0], 0)

    def test_missing_reviewer_is_outstanding_not_weaker_review(self):
        self.elig = {'codex': self.elig['codex']}
        d = self.decision()
        self.assertTrue(d['review_required'])
        self.assertIsNone(d['review'])
        self.assertEqual(d['review_band'], 3)

    def test_override_requires_reason_and_never_reroutes(self):
        d = self.decision()
        with patch.object(r, 'judge', side_effect=AssertionError('must not judge')):
            args = ['report', 'minimax', '--started', '--model', 'MiniMax-M3', '--task', self.spec,
                    '--dispatch', 'ctx_override', '--decision-id', d['decision_id']]
            self.assertEqual(self.invoke(args)[0], 2)
            self.assertFalse(self.state.exists() and r.load_json(self.state).get('reservations'))
            self.assertEqual(self.invoke(args + ['--override-reason', 'explicit user pilot'])[0], 0)

    def test_route_returns_durable_id_without_judge_and_retains_blocked_decision(self):
        with patch.object(r, 'route', return_value=r.decide(self.judgment, self.config, {})), \
             patch.object(r, 'judge', side_effect=AssertionError('no judge')):
            code, text, _ = self.invoke(['route', '--task', self.spec, '--json'])
        self.assertEqual(code, 0)  # Legacy JSON exit semantics.
        d = json.loads(text)
        self.assertIsNone(d['pick'])
        self.assertIsNotNone(outcome_cli.store(r).get_decision(d['decision_id']))

    def test_read_only_audit_does_not_create_store(self):
        code, text, _ = self.invoke(['outcome', 'audit'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)['attempts'], [])
        self.assertFalse(self.state.with_name('outcomes.sqlite3').exists())

    def test_corrupt_store_reports_error_without_overwriting(self):
        path = self.state.with_name('outcomes.sqlite3')
        path.write_bytes(b'not a database')
        code, _, _ = self.invoke(['outcome', 'audit'])
        self.assertEqual(code, 2)
        self.assertEqual(path.read_bytes(), b'not a database')

    def test_cli_rejects_duplicate_json_fields(self):
        path = Path(self.temp.name) / 'event.json'
        path.write_text('{"actor":"first","actor":"second"}')
        code, _, _ = self.invoke(['outcome', 'record', '--dispatch', 'ctx_test',
                                  '--event-id', 'duplicate', '--kind', 'accepted', '--data', str(path)])
        self.assertEqual(code, 2)

    def test_cli_rejects_invented_reviewer_family(self):
        d = self.decision()
        self.assertEqual(self.launch(d)[0], 0)
        evidence = hashlib.sha256(b'checks').hexdigest()
        review = {'actor': 'reviewer', 'provider': d['pick']['provider'],
                  'model': d['pick']['model'], 'family': 'invented-independent-family',
                  'verdict': 'accepted', 'evidence_sha256': evidence}
        self.assertEqual(self.event('review', review)[0], 2)

    def test_cli_rejects_reviewer_below_recorded_task_floor(self):
        d = self.decision()
        self.assertEqual(self.launch(d)[0], 0)
        profile = self.config['model_profiles']['minimax:MiniMax-M3']
        review = {'actor': 'reviewer', 'provider': 'minimax', 'model': 'MiniMax-M3',
                  'family': profile['family'], 'verdict': 'accepted',
                  'evidence_sha256': hashlib.sha256(b'checks').hexdigest()}
        self.assertEqual(self.event('review', review)[0], 2)

    def test_decision_hashes_exact_text_and_working_source(self):
        d = self.decision()
        record = outcome_cli.store(r).get_decision(d['decision_id'])
        self.assertEqual(record['task_sha256'], hashlib.sha256(self.spec.encode()).hexdigest())
        self.assertEqual(record['config_sha256'], outcome_cli.digest(self.config))
        self.assertEqual(len(record['policy_revision']), 64)
        self.assertEqual(record['project'], str(Path.cwd().resolve()))
        code, _, _ = self.invoke(['report', d['pick']['provider'], '--started',
                                  '--model', d['pick']['model'], '--task', self.spec+' ',
                                  '--dispatch', 'ctx_changed', '--decision-id', d['decision_id']])
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
