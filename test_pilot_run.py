"""Paired-policy fidelity, durable interruption handling and pilot budget gates."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pilot_run as pilot
import pilot_tasks
import record_replay_baseline
import replay


class FakeBackend:
    def __init__(self):
        self.attempts, self.requests = {}, {}
        self.launches, self.reviews, self.snapshots = 0, 0, 0
        self.crash_after_launch = self.crash_after_admission = False
        self.unknown = self.missing_metrics = self.reject = False

    def snapshot(self, pair, config):
        self.snapshots += 1
        case = record_replay_baseline.cases()[0]
        case.update(id=pair['id'], task_sha256=pair['task_sha256'],
                    judgment=pair['judgment']['judgment'], config=config)
        return case

    def admit(self, request, pair, entry, case, config):
        if request in self.requests:
            return {'status': 'existing', 'attempt': self.attempts[self.requests[request]]}
        aid = 'attempt'+str(len(self.attempts))
        self.requests[request] = aid
        self.attempts[aid] = {'attempt_id': aid, 'state': 'admitted', 'launch_key': aid,
                             'metrics': {'input_tokens': 100, 'output_tokens': 10}}
        if self.crash_after_admission:
            self.crash_after_admission = False
            raise RuntimeError('simulated crash after ledger commit')
        return {'status': 'admitted', 'attempt': self.attempts[aid]}

    def launch(self, root, pair, entry, bundle, remaining):
        self.launches += 1
        attempt = self.attempts[entry['attempt_id']]
        attempt['state'] = 'reconciling' if self.unknown else 'completed'
        attempt['receipt'] = {'session_id': 'native-session', 'dispatch_id': 'native-dispatch'}
        if self.missing_metrics:
            attempt['metrics'] = {}
        if self.crash_after_launch:
            self.crash_after_launch = False
            raise RuntimeError('simulated crash after native completion')

    def outcome(self, aid, config):
        return self.attempts[aid]

    def review(self, root, pair, entry, attempt, require_existing=False):
        self.reviews += 1
        return {'accepted': not self.reject, 'status': 'checked', 'cleanup': 'removed', 'evidence': 'fixture-review'}

    def record_review(self, attempt, review):
        attempt.update(state='accepted' if review['accepted'] else 'rejected', review_evidence='sha256:'+review['evidence'])
        return attempt


class PilotRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'pilot'
        # Git pinning is tested separately; synthetic runs never launch Git/model work.
        patcher = patch.object(pilot.native_runs, 'git', side_effect=lambda argv, cwd:
                               '' if argv[0] == 'status' else 'fixture-revision')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = record_replay_baseline.cases()[0]['config']
        self.config['dispatch_cost'] = {'_default': 0.1}
        self.limits = {name: {'attempts': 80, 'dispatch_points': 100,
                             'input_tokens': 20000, 'output_tokens': 2000} for name in ('codex', 'opencode')}
        pilot.initialize(self.root, self.limits, self.config)
        self.backend = FakeBackend()

    def test_entire_pair_sequence_uses_one_shared_snapshot_and_no_extra_attempts(self):
        for _ in range(40):
            self.assertEqual(pilot.step(self.root, self.backend)['outcome'], 'accepted')
        final = pilot.step(self.root, self.backend)
        self.assertEqual(final['status'], 'finished')
        self.assertEqual((self.backend.launches, self.backend.snapshots), (40, 20))
        state = pilot.strict_read(self.root/'run.json')
        for first, second in zip(state['entries'][::2], state['entries'][1::2]):
            self.assertEqual(first['snapshot_sha256'], second['snapshot_sha256'])
        for row in final['report']['variants'].values():
            self.assertEqual(row['accepted_tasks'], 20)
        self.assertFalse(final['report']['promotion'])

    def test_per_arm_limits_do_not_spend_the_partners_allowance(self):
        root = self.root.with_name('separate-arms')
        limits = copy.deepcopy(self.limits)
        for limit in limits.values():
            limit['input_tokens'] = 150
        pilot.initialize(root, limits, self.config, budget_scope='per-arm')
        with patch.object(self.backend, 'launch', wraps=self.backend.launch) as launch:
            first = pilot.step(root, self.backend)
            second = pilot.step(root, self.backend)
            self.assertEqual(first['outcome'], 'accepted')
            self.assertEqual(second['outcome'], 'accepted')
            self.assertNotEqual(first['variant'], second['variant'])
            self.assertEqual([call.args[-1]['input_tokens'] for call in launch.call_args_list], [150, 150])
            state = pilot.strict_read(root/'run.json')
            self.assertIsNone(state['halt'])
            manifest = pilot.strict_read(root/'manifest.json')
            report = pilot.report(state, manifest)
            for variant in ('baseline', 'candidate'):
                self.assertEqual(report['arm_usage'][variant][first['provider']]['input_tokens'], 100)
            pilot.step(root, self.backend)
            self.assertEqual(launch.call_args_list[-1].args[-1]['input_tokens'], 50)
        self.assertEqual(pilot.step(root, self.backend)['status'], 'halted')

    def test_crash_after_admission_reuses_request_and_after_launch_never_relaunches(self):
        self.backend.crash_after_admission = True
        with self.assertRaises(RuntimeError):
            pilot.step(self.root, self.backend)
        self.backend.crash_after_launch = True
        with self.assertRaises(RuntimeError):
            pilot.step(self.root, self.backend)
        result = pilot.step(self.root, self.backend)
        self.assertEqual(result['outcome'], 'accepted')
        self.assertEqual((self.backend.launches, len(self.backend.attempts)), (1, 1))

    def test_uncertain_native_attempt_is_observed_without_replay_or_next_task(self):
        self.backend.unknown = True
        first = pilot.step(self.root, self.backend)
        second = pilot.step(self.root, self.backend)
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'unresolved')
        self.assertEqual((self.backend.launches, len(self.backend.attempts)), (1, 1))
        report = pilot.report(pilot.strict_read(self.root/'run.json'), pilot.strict_read(self.root/'manifest.json'))
        baseline = report['variants']['baseline']
        self.assertEqual((baseline['attempted_tasks'], baseline['accepted_per_attempted_task']), (1, 0))

    def test_missing_native_usage_stops_before_more_inference(self):
        self.backend.missing_metrics = True
        pilot.step(self.root, self.backend)
        result = pilot.step(self.root, self.backend)
        self.assertEqual(result['status'], 'halted')
        self.assertEqual(self.backend.launches, 1)

    def test_reviewed_rejection_has_one_retry_only(self):
        self.backend.reject = True
        for _ in range(4):
            pilot.step(self.root, self.backend)
        entries = pilot.strict_read(self.root/'run.json')['entries']
        self.assertEqual([e['number'] for e in entries], [1, 1, 2, 2])
        self.assertEqual(len({e['task_id'] for e in entries}), 1)
        pilot.step(self.root, self.backend)
        self.assertNotEqual(pilot.strict_read(self.root/'run.json')['entries'][-1]['task_id'], entries[0]['task_id'])

    def test_budget_ceilings_and_unknown_usage_are_conservative(self):
        limit = {'codex': {'attempts': 1, 'dispatch_points': 1, 'input_tokens': 100, 'output_tokens': 20}}
        state = {'entries': []}
        self.assertIsNone(pilot.budget_reason(state, 'codex', 1, limit))
        self.assertIsNotNone(pilot.budget_reason(state, 'codex', 1.000001, limit))
        state['entries'] = [{'provider': 'codex', 'budget_booked': True, 'points': 1,
                             'phase': 'dispatching'}]
        self.assertIn('unresolved', pilot.budget_reason(state, 'codex', 0, limit))
        state['entries'][0].update(phase='done', metrics={'input_tokens': 100, 'output_tokens': 10})
        self.assertIsNotNone(pilot.budget_reason(state, 'codex', 0, limit))
        for invalid in ({}, {'codex': {**limit['codex'], 'attempts': True}},
                        {'codex': {**limit['codex'], 'dispatch_points': float('nan')}}):
            with self.assertRaises(ValueError): pilot.validate_limits(invalid)

    def test_changed_bundle_or_source_refuses_before_snapshot_or_launch(self):
        path = self.root/'bundle.json'
        document = json.loads(path.read_text())
        document['limits']['codex']['attempts'] = 79
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, 'changed'):
            pilot.step(self.root, self.backend)
        self.assertEqual(self.backend.snapshots, 0)

    def test_high_stakes_fixture_is_not_misclassified_as_a_destructive_operation(self):
        rows = pilot.strict_read(self.root/'manifest.json')['pairs']
        self.assertEqual(len([r for r in rows if r['tier'] == 'high_stakes']), 5)
        self.assertTrue(all(r['judgment']['judgment']['destructive'] == 0 for r in rows))

    def test_custom_capture_retains_fixed_baseline_without_mutating_case(self):
        case = record_replay_baseline.cases()[0]
        before = copy.deepcopy(case)
        captured = record_replay_baseline.capture([case])
        self.assertEqual(captured['baseline']['revision'], replay.BASELINE)
        self.assertEqual(set(captured['baseline']['records']), {case['id']})
        self.assertEqual(case, before)

    def test_missing_baseline_fails_before_trial_creation(self):
        destination = self.root.parent/'missing-baseline'
        with patch.object(record_replay_baseline, 'capture', side_effect=RuntimeError('baseline unavailable')):
            with self.assertRaises(RuntimeError):
                pilot.initialize(destination, self.limits, self.config)
        self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
