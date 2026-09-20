"""Caller judgment contract: offline, no credentials, model calls or real state."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rightsize as r

TASK = 'Rename the local counter in parser.py; preserve behavior and run its tests.'
CONFIG = json.loads(Path('config.json').read_text())


class CallerJudgment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / 'judgment.json'
        self.state = Path(self.tmp.name) / 'state.json'
        state_patch = patch.object(r, 'STATE', self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.document = {
            'schema_version': 1, 'actor': 'active-agent',
            'task_sha256': hashlib.sha256(TASK.encode('utf-8')).hexdigest(),
            'judgment': {'tier': 'mechanical', 'size': 0.2,
                         'second_opinion': 0.1, 'spec_complete': 0.9, 'destructive': 0.0}}
        self.probes = {'opencode': {'name': 'opencode', 'status': 'ok', 'buckets': [
            {'id': 'weekly', 'percent': 10, 'source': 'live', 'resets_at': r.now() + 3600}]}}

    def invoke(self, document=None):
        self.file.write_text(json.dumps(document or self.document))
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(r, 'probes_cached', return_value=(self.probes, False)) as probe, \
             patch.object(r, 'judge', side_effect=AssertionError('caller path cannot judge')), \
             patch.object(r, 'secret', side_effect=AssertionError('no judgment key lookup')), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = r.main(['route', '--task', TASK, '--judgment', str(self.file), '--json'])
        return code, output.getvalue(), errors.getvalue(), probe

    def test_routes_without_judge_and_records_source(self):
        code, output, _, _ = self.invoke()
        self.assertEqual(code, 0)
        decision = json.loads(output)
        self.assertEqual(decision['judgment']['source'], 'caller (active-agent)')
        self.assertEqual(decision['pick']['provider'], 'opencode')
        entry = r.load_json(self.state)['decisions'][0]
        self.assertEqual(entry['judgment_source'], 'caller (active-agent)')
        self.assertEqual(entry['task_sha256'], self.document['task_sha256'])

    def test_invalid_documents_fail_before_probes_or_state(self):
        variants = []
        for key, value in [('schema_version', 2), ('schema_version', True),
                           ('task_sha256', '0' * 64), ('actor', 'bad\nactor')]:
            d = json.loads(json.dumps(self.document)); d[key] = value; variants.append(d)
        for key, value in [('tier', 'unknown'), ('size', -1), ('size', 3),
                           ('size', True), ('size', 10 ** 500), ('size', float('nan')),
                           ('destructive', float('inf')), ('spec_complete', 1.1),
                           ('second_opinion', '0.5'), ('provider', 'opencode')]:
            d = json.loads(json.dumps(self.document)); d['judgment'][key] = value; variants.append(d)
        d = json.loads(json.dumps(self.document)); del d['judgment']['tier']; variants.append(d)
        for d in variants:
            with self.subTest(document=d):
                code, output, errors, probe = self.invoke(d)
                self.assertEqual(code, 2)
                self.assertEqual(output, '')
                self.assertIn('judgment', errors)
                probe.assert_not_called()
                self.assertFalse(self.state.exists())

    def test_quota_denial_still_vetoes_caller_request(self):
        self.probes['opencode']['buckets'][0].update(percent=None, raw_status='rate-limited')
        code, output, _, _ = self.invoke()
        self.assertEqual(code, 0)  # Preserve the existing JSON-output exit contract.
        decision = json.loads(output)
        self.assertIsNone(decision['pick'])
        self.assertFalse(decision['quota']['opencode']['eligible'])

    def test_legacy_route_still_uses_its_judge(self):
        judgment = {**self.document['judgment'], 'source': 'legacy', 'tier_confidence': None}
        with patch.object(r, 'judge', return_value=judgment) as judge:
            result = r.route(TASK, CONFIG, probes=self.probes)
        judge.assert_called_once_with(TASK)
        self.assertEqual(result['judgment']['source'], 'legacy')

    def test_duplicate_fields_and_oversize_rejected(self):
        for text in ('{"schema_version":1,"schema_version":1}', ' ' * 65537):
            self.file.write_text(text)
            with self.assertRaises(ValueError):
                r.load_judgment(self.file, TASK)

    def test_documented_example_matches_its_task(self):
        task = Path('examples/caller-task.md').read_text()
        result = r.load_judgment(Path('examples/caller-judgment.json'), task)
        self.assertEqual(result['source'], 'caller (active-agent)')

    def test_unicode_task_bound_to_exact_routed_text(self):
        task = 'Rename café; preserve Māori labels.\n'
        self.document['task_sha256'] = hashlib.sha256(task.encode('utf-8')).hexdigest()
        self.file.write_text(json.dumps(self.document))
        self.assertEqual(r.load_judgment(self.file, task)['tier'], 'mechanical')
        with self.assertRaises(ValueError):
            r.load_judgment(self.file, task.strip())


if __name__ == '__main__':
    unittest.main()
