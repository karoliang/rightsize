"""Real ledger/Git/review wiring with a fake native server and acceptance result."""

import hashlib
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import native_runs
import pilot_acceptance
import pilot_execution
import pilot_run
import pilot_tasks
import rightsize as router
import test_opencode_execution as fixtures


class NativePilotTests(unittest.TestCase):
    def test_managed_attempt_is_bound_to_owned_worktree_and_independent_review(self):
        fixture = fixtures.ExecutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.root/'pilot'
        code_repo = Path(pilot_run.__file__).resolve().parent
        actual_git = native_runs.git
        def git(argv, cwd):
            if Path(cwd) == code_repo:
                return '' if argv[0] == 'status' else 'fixture-revision'
            return actual_git(argv, cwd)
        config = {'bands': {str(i): ['opencode:fixture'] for i in (1, 2, 3)},
                  'agents': {'opencode': 'opencode'}, 'review_ladder': [], 'reserves': {'opencode': 10},
                  'max_inflight': {'opencode': 8}, 'dispatch_cost': {'_default': 0.1}}
        limits = {'opencode': {'attempts': 80, 'dispatch_points': 100, 'input_tokens': 20000, 'output_tokens': 2000}}
        server = fixtures.Backend()
        reference = pilot_tasks.load()[0]['reference_source']
        def transport(**kwargs):
            child = server.connect(**kwargs)
            actual = child.request
            def request(method, path, body=None, **options):
                result = actual(method, path, body, **options)
                if path.endswith('/prompt_async'):
                    (Path(kwargs['cwd'])/'solution.py').write_text(reference)
                return result
            child.request = request
            return child
        cls = pilot_execution.ScopedOpenCode
        def adapter(*args, **kwargs):
            return cls(*args, **kwargs, transport=transport)
        def docker(command, **kwargs):
            return (0, b'unix:///fixture/docker.sock\n') if 'context' in command else (0, b'sha256:'+b'a'*64+b'\n')
        def review(task_id, candidate, target):
            raw = candidate.read_bytes()
            record = {'task_id': task_id, 'source_sha256': hashlib.sha256(raw).hexdigest(),
                      'corpus_sha256': pilot_tasks.CORPUS_SHA256, 'accepted': raw.decode()==reference,
                      'status': 'checked', 'cleanup': 'removed'}
            pilot_acceptance.save(target, record, initial=True)
            return record
        with patch.object(native_runs, 'git', side_effect=git), \
                patch.object(router, 'probe_all', side_effect=lambda config: {'opencode': {
                    **fixture.quota, 'account': fixture.account, 'observed_at': time.time()}}), \
                patch.object(pilot_run.shutil, 'which', return_value='/fixture/docker'), \
                patch.object(pilot_acceptance, 'bounded', side_effect=docker), \
                patch.object(pilot_execution, 'ScopedOpenCode', side_effect=adapter), \
                patch.object(pilot_acceptance, 'check', side_effect=review):
            pilot_run.initialize(root, limits, config)
            result = pilot_run.step(root, pilot_run.NativeBackend())
        self.assertEqual(result['outcome'], 'accepted')
        attempt = fixture.ledger.read(result['attempt_id'])
        self.assertEqual(attempt['state'], 'accepted')
        self.assertEqual(attempt['spec_hash'], pilot_tasks.digest((root/pilot_tasks.load()[0]['id']/'baseline'/'TASK.md').read_bytes()))
        self.assertEqual(attempt['review_evidence'], result['review_evidence'])
        self.assertNotEqual(Path(attempt['receipt']['worktree']), root/pilot_tasks.load()[0]['id']/'baseline')
        self.assertTrue(all(child.closed for child in server.children))
        self.assertEqual(sum(path.endswith('/prompt_async') for _, path in server.calls), 1)
        self.assertEqual((root/pilot_tasks.load()[0]['id']/'baseline'/'solution.py').read_text(), pilot_tasks.load()[0]['initial_source'])


if __name__ == '__main__':
    unittest.main()
