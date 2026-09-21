"""Pilot-only source scope and observed-usage stop controls for native adapters."""

from pathlib import Path
import sys

from managed_ledger import ACTIVE
from managed_router import NoLaunch
import native_codex
import native_opencode_execution
from native_transport import JsonProcess, ProtocolError

PROFILE = 'rightsize-pilot-scoped-v1'


def profile(worktree):
    filesystem = {':root': 'deny', ':minimal': 'read', ':tmpdir': 'deny', ':slash_tmp': 'deny',
                  str(Path(worktree).resolve()): 'write',
                  str(Path(worktree).resolve()/'.git'): 'read',
                  str(Path(worktree).resolve()/'TASK.md'): 'read'}
    # The installed macOS Codex helper cannot exec through a leaf-only grant.
    # This is a tool installation root, never a user home or project root.
    if sys.platform == 'darwin' and Path('/opt/homebrew').is_dir():
        filesystem['/opt/homebrew'] = 'read'
    return {'filesystem': filesystem, 'network': {'enabled': False}}


class ScopedCodexProcess(JsonProcess):
    def __init__(self, *args, cwd, **kwargs):
        self.worktree = Path(cwd).resolve()
        super().__init__(*args, cwd=cwd, **kwargs)

    def request(self, method, params, **kwargs):
        if method == 'initialize':
            params = {**params, 'capabilities': {**params.get('capabilities', {}), 'experimentalApi': True}}
        if method != 'thread/start':
            return super().request(method, params, **kwargs)
        resolved = super().request('config/read', {'includeLayers': False}).get('config')
        if not isinstance(resolved, dict):
            raise ProtocolError('pilot native configuration unavailable')
        settings = {**params.get('config', {}), 'permissions': {PROFILE: profile(self.worktree)},
                    'shell_environment_policy': {'inherit': 'none', 'set': {
                        'PATH': '/opt/homebrew/bin:/usr/bin:/bin',
                        'PYTHONDONTWRITEBYTECODE': '1'}},
                    'web_search': 'disabled', 'features': {'apps': False}}
        for section in ('mcp_servers', 'plugins'):
            entries = resolved.get(section, {})
            if not isinstance(entries, dict):
                raise ProtocolError('pilot native tool inventory unavailable')
            settings[section] = {name: {'enabled': False} for name in entries}
        request = {**params, 'permissions': PROFILE, 'config': settings}
        request.pop('sandbox', None)
        result = super().request(method, request, **kwargs)
        sandbox = result.get('sandbox') or {}
        if (result.get('activePermissionProfile') != {'id': PROFILE, 'extends': None} or
                sandbox.get('type') != 'workspaceWrite' or sandbox.get('networkAccess') is not False or
                sandbox.get('excludeTmpdirEnvVar') is not True or sandbox.get('excludeSlashTmp') is not True or
                any(Path(root).resolve() != self.worktree for root in sandbox.get('writableRoots', []))):
            raise ProtocolError('pilot effective native permissions differ')
        return result


class BudgetedAdapter:
    def __init__(self, adapter, ledger, attempt_id, remaining):
        self.adapter, self.ledger, self.attempt_id, self.remaining = adapter, ledger, attempt_id, remaining

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def poll(self, *args, **kwargs):
        events = self.adapter.poll(*args, **kwargs)
        if any(self.adapter.metrics.get(key, 0) >= ceiling for key, ceiling in self.remaining.items()):
            attempt = self.ledger.read(self.attempt_id)
            if attempt['state'] in ACTIVE and attempt['state'] != 'cancelling':
                self.ledger.event(self.attempt_id, attempt['launch_key']+':pilot-usage-stop', 'cancelling')
            # The owner observes cancellation on its next iteration. Do not
            # emit a producing transition after this wrapper changed the state.
            events = [event for event in events if event['kind'] != 'producing']
        return events


class ScopedOpenCode(native_opencode_execution.OpenCode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rules = native_opencode_execution.permissions('read-only')
        for name in ('edit', 'write', 'apply_patch'):
            for pattern in ('solution.py', str(self.worktree/'solution.py')):
                self.rules.append({'permission': name, 'pattern': pattern, 'action': 'allow'})


def factory(attempt_id, remaining):
    def create(provider, ledger, config, prompt, cwd, *, sandbox):
        if sandbox != 'workspace-write':
            raise ValueError('pilot requires scoped workspace editing')
        if provider == 'codex':
            adapter = native_codex.Codex(ledger, config, prompt, cwd, sandbox=sandbox, transport=ScopedCodexProcess)
        elif provider == 'opencode':
            # This adapter denies external-directory, shell, delegation and all
            # tools except explicit native reads/edits in its worktree.
            adapter = ScopedOpenCode(ledger, config, prompt, cwd, sandbox=sandbox)
        else:
            raise NoLaunch('pilot source restriction is not verified for this provider')
        return BudgetedAdapter(adapter, ledger, attempt_id, remaining)
    return create
