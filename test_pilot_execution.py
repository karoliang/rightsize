"""Pilot-only native permissions and observed usage cancellation boundaries."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pilot_execution as execution
from native_transport import JsonProcess, ProtocolError


class PilotExecutionTests(unittest.TestCase):
    def test_codex_profile_is_explicit_and_does_not_forward_native_tools_or_shell_secrets(self):
        rpc = execution.ScopedCodexProcess.__new__(execution.ScopedCodexProcess)
        rpc.worktree = Path('/fixture/worktree')
        replies = [{}, {'config': {'mcp_servers': {'private-tool': {'token': 'SENTINEL'}},
                                  'plugins': {'private-plugin': {'enabled': True}}}},
                   {'activePermissionProfile': {'id': execution.PROFILE, 'extends': None},
                    'sandbox': {'type': 'workspaceWrite', 'networkAccess': False,
                                'excludeTmpdirEnvVar': True, 'excludeSlashTmp': True}}]
        with patch.object(JsonProcess, 'request', side_effect=replies) as call:
            rpc.request('initialize', {'clientInfo': {'name': 'fixture', 'version': '1'}})
            rpc.request('thread/start', {'sandbox': 'workspace-write', 'config': {'model_reasoning_effort': 'low'}})
        self.assertTrue(call.call_args_list[0].args[1]['capabilities']['experimentalApi'])
        request = call.call_args_list[-1].args[1]
        self.assertNotIn('sandbox', request)
        self.assertEqual(request['config']['mcp_servers'], {'private-tool': {'enabled': False}})
        self.assertEqual(request['config']['plugins'], {'private-plugin': {'enabled': False}})
        self.assertNotIn('SENTINEL', str(request))
        self.assertEqual(request['config']['shell_environment_policy']['inherit'], 'none')
        environment = request['config']['shell_environment_policy']['set']
        self.assertEqual(environment['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertLess(environment['PATH'].split(':').index('/opt/homebrew/bin'),
                        environment['PATH'].split(':').index('/usr/bin'))
        fs = request['config']['permissions'][execution.PROFILE]['filesystem']
        self.assertEqual(fs[':root'], 'deny')
        self.assertEqual(fs[str(rpc.worktree)], 'write')
        self.assertEqual(fs[str(rpc.worktree/'.git')], 'read')
        self.assertEqual(fs[str(rpc.worktree/'TASK.md')], 'read')

    def test_changed_effective_profile_or_extra_root_fails_before_inference(self):
        rpc = execution.ScopedCodexProcess.__new__(execution.ScopedCodexProcess)
        rpc.worktree = Path('/fixture/worktree')
        for altered in ({'activePermissionProfile': {'id': 'different'}},
                        {'activePermissionProfile': {'id': execution.PROFILE, 'extends': None},
                         'sandbox': {'type': 'workspaceWrite', 'networkAccess': False,
                                     'excludeTmpdirEnvVar': True, 'excludeSlashTmp': True,
                                     'writableRoots': ['/other']}}):
            with patch.object(JsonProcess, 'request', side_effect=[{'config': {}}, altered]):
                with self.assertRaises(ProtocolError):
                    rpc.request('thread/start', {'sandbox': 'workspace-write'})

    def test_opencode_only_grants_file_edits_to_solution(self):
        adapter = execution.ScopedOpenCode(None, {}, 'task', '/fixture/worktree', sandbox='workspace-write')
        edits = [r for r in adapter.rules if r['permission'] in ('edit', 'write', 'apply_patch')]
        self.assertEqual({r['pattern'] for r in edits}, {'solution.py', '/fixture/worktree/solution.py'})
        self.assertEqual(adapter.rules[0], {'permission': '*', 'pattern': '*', 'action': 'deny'})

    def test_budget_crossing_requests_cancel_without_inventing_terminal_evidence(self):
        attempts = {'state': 'started', 'launch_key': 'key'}
        events = []
        def event(*args):
            events.append(args)
            attempts['state'] = 'cancelling'
        ledger = SimpleNamespace(read=lambda aid: attempts, event=event)
        adapter = SimpleNamespace(metrics={'input_tokens': 101, 'output_tokens': 2},
                                  poll=lambda: [{'kind': 'producing', 'text': 'after threshold'}])
        wrapped = execution.BudgetedAdapter(adapter, ledger, 'attempt', {'input_tokens': 100, 'output_tokens': 20})
        self.assertEqual(wrapped.poll(), [])
        self.assertEqual(wrapped.poll(), [])
        self.assertEqual(events, [('attempt', 'key:pilot-usage-stop', 'cancelling')])
        self.assertEqual(attempts['state'], 'cancelling')


if __name__ == '__main__':
    unittest.main()
