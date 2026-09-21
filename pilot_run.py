"""Durable paired pilot coordinator. Initialization and reports never launch models."""

import argparse
import accounts
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import time
from types import SimpleNamespace
import uuid

import managed_router
from managed_ledger import ACTIVE, Ledger, LedgerError, units
import native_runs
import pilot_acceptance
import pilot_tasks
import record_replay_baseline
import replay
import rightsize as router
import state_migration

POLICY_FIELDS = ('bands', 'agents', 'review_ladder', 'thresholds', 'reserves',
                 'max_inflight', 'dispatch_cost', 'effort', 'expensive_band')
TOKEN_FIELDS = ('input_tokens', 'output_tokens')


def strict_read(path):
    raw = pilot_acceptance.source_bytes(path, 4*1024*1024)
    return json.loads(raw, object_pairs_hook=replay.object_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite value')))


def validate_limits(limits):
    if not isinstance(limits, dict) or not limits or set(limits)-{'codex', 'claude', 'opencode'}:
        raise ValueError('explicit native-provider pilot ceilings required')
    for row in limits.values():
        if not isinstance(row, dict) or set(row) != {'attempts', 'dispatch_points', *TOKEN_FIELDS}:
            raise ValueError('pilot ceiling fields invalid')
        if type(row['attempts']) is not int or not 1 <= row['attempts'] <= 80:
            raise ValueError('pilot attempt ceiling invalid')
        if type(row['dispatch_points']) not in (int, float) or not math.isfinite(row['dispatch_points']) or not 0 < row['dispatch_points'] <= 100:
            raise ValueError('pilot dispatch point ceiling invalid')
        if any(type(row[k]) is not int or not 1 <= row[k] <= 10000000 for k in TOKEN_FIELDS):
            raise ValueError('pilot token ceiling invalid')


def initialize(root, limits, config, *, budget_scope='shared'):
    validate_limits(limits)
    if budget_scope not in ('shared', 'per-arm'):
        raise ValueError('invalid pilot budget scope')
    # Pin actual code, not a label supplied by the caller. Uncommitted changes
    # cannot be mixed into a trial that claims a committed candidate revision.
    repo = Path(__file__).resolve().parent
    if native_runs.git(['status', '--porcelain'], repo):
        raise ValueError('commit candidate changes before freezing the pilot')
    revision = native_runs.git(['rev-parse', 'HEAD'], repo)
    record_replay_baseline.capture([])
    manifest = pilot_tasks.prepare(root)
    root = Path(root)
    manifest.update(live_ready=True, usage_ceilings=limits, budget_scope=budget_scope)
    pilot_acceptance.save(root/'manifest.json', manifest)
    bundle = {'schema_version': 1, 'run_id': uuid.uuid4().hex, 'candidate_revision': revision,
              'manifest_sha256': replay.digest(manifest), 'limits': limits, 'config': config,
              'budget_scope': budget_scope,
              'timeout_seconds': 120, 'retry_rule': 'one retry after reviewed rejection only'}
    pilot_acceptance.save(root/'bundle.json', bundle, initial=True)
    pilot_acceptance.save(root/'run.json', {'schema_version': 1, 'bundle_sha256': replay.digest(bundle),
                                          'entries': [], 'snapshots': {}, 'halt': None}, initial=True)
    return bundle


def validate_bundle(root, bundle, state):
    validate_limits(bundle['limits'])
    manifest = strict_read(root/'manifest.json')
    if (bundle.get('budget_scope', 'shared') not in ('shared', 'per-arm') or
            manifest.get('budget_scope', 'shared') != bundle.get('budget_scope', 'shared')):
        raise ValueError('pilot budget scope differs')
    if (bundle['schema_version'] != 1 or state['schema_version'] != 1 or
            state['bundle_sha256'] != replay.digest(bundle) or
            bundle['manifest_sha256'] != replay.digest(manifest) or
            manifest['corpus_sha256'] != pilot_tasks.CORPUS_SHA256 or
            manifest['baseline_revision'] != replay.BASELINE or
            manifest['baseline_source_sha256'] != replay.BASELINE_SOURCE):
        raise ValueError('pilot bundle or manifest changed')
    repo = Path(__file__).resolve().parent
    if native_runs.git(['rev-parse', 'HEAD'], repo) != bundle['candidate_revision'] or native_runs.git(['status', '--porcelain'], repo):
        raise ValueError('candidate revision changed during pilot')
    tasks = {row['id']: row for row in pilot_tasks.load()}
    for pair in manifest['pairs']:
        row = tasks[pair['id']]
        for variant in ('baseline', 'candidate'):
            path = root/pair['id']/variant
            if ((path/'solution.py').read_text() != row['initial_source'] or
                    (path/'TASK.md').read_text() != pilot_tasks.prompt(row)):
                raise ValueError('initial pilot snapshot changed')
    return manifest


def budget(state, provider, variant=None):
    result = {'attempts': 0, 'dispatch_units': 0, 'input_tokens': 0, 'output_tokens': 0, 'unknown': False}
    for entry in state['entries']:
        if (entry.get('provider') != provider or not entry.get('budget_booked') or
                (variant is not None and entry['variant'] != variant)):
            continue
        result['attempts'] += 1
        result['dispatch_units'] += units(entry['points'])
        if entry['phase'] == 'done':
            metrics = entry.get('metrics', {})
            if entry.get('native_no_launch'):
                continue
            if any(type(metrics.get(k)) is not int or metrics[k] < 0 for k in TOKEN_FIELDS):
                result['unknown'] = True
            else:
                for key in TOKEN_FIELDS:
                    result[key] += metrics[key]
        else:
            result['unknown'] = True
    return result


def budget_reason(state, provider, points, limits, variant=None):
    if provider not in limits:
        return 'selected provider has no predeclared pilot ceiling'
    used, limit = budget(state, provider, variant), limits[provider]
    if used['unknown']:
        return 'previous pilot usage is unresolved'
    if used['attempts'] >= limit['attempts'] or used['dispatch_units'] + units(points) > units(limit['dispatch_points']):
        return 'pilot attempt or conservative dispatch-point ceiling reached'
    if any(used[k] >= limit[k] for k in TOKEN_FIELDS):
        return 'pilot native-token stop threshold reached'
    return None


def next_entry(manifest, entries):
    for pair in manifest['pairs']:
        for number in (1, 2):
            for variant in pair['order']:
                previous = [e for e in entries if e['task_id'] == pair['id'] and e['variant'] == variant]
                if number == 2 and (not previous or previous[0].get('outcome') != 'rejected'):
                    continue
                match = next((e for e in previous if e['number'] == number), None)
                if match is None:
                    return pair, {'task_id': pair['id'], 'variant': variant, 'number': number, 'phase': 'planned'}
                if match['phase'] != 'done':
                    return pair, match
    return None, None


def step(root, backend):
    """At most one new native attempt. Durable markers precede external effects."""
    root = Path(root).resolve()
    with (root/'owner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bundle, state = strict_read(root/'bundle.json'), strict_read(root/'run.json')
        manifest = validate_bundle(root, bundle, state)
        if state['halt']:
            return {'status': 'halted', 'reason': state['halt']}
        pair, entry = next_entry(manifest, state['entries'])
        if entry is None:
            return {'status': 'finished', 'report': report(state, manifest)}
        budget_variant = entry['variant'] if bundle.get('budget_scope') == 'per-arm' else None
        def persist():
            pilot_acceptance.save(root/'run.json', state)
        if entry not in state['entries']:
            state['entries'].append(entry)
            persist()
        case_key = pair['id'] + ':' + str(entry['number'])
        if case_key not in state['snapshots']:
            case = backend.snapshot(pair, bundle['config'])
            replay.validate(case)
            baseline = record_replay_baseline.capture([case])['baseline']['records'][case['id']]['verdict']
            candidate = replay.shadow(case, router)['candidate']
            state['snapshots'][case_key] = {'case': case, 'baseline': baseline, 'candidate': candidate}
            persist()
        capture = state['snapshots'][case_key]
        decision = capture[entry['variant']]
        request = f"pilot:{bundle['run_id']}:{pair['id']}:{entry['variant']}:{entry['number']}"
        if entry['phase'] == 'planned':
            entry.update(request_id=request, decision=decision, snapshot_sha256=replay.digest(capture['case']))
            if decision['status'] != 'recommend':
                entry.update(phase='done', outcome='refused', reason='frozen policy did not select a provider')
                persist()
                return entry
            provider = decision['pick']['provider']
            points = router.dispatch_cost(bundle['config'], provider, decision['band'])
            entry.update(provider=provider, points=points)
            reason = budget_reason(state, provider, points, bundle['limits'], budget_variant)
            if reason:
                state['halt'] = reason
                persist()
                return {'status': 'halted', 'reason': reason}
            entry.update(phase='admitting', budget_booked=True)
            persist()
        if entry['phase'] == 'admitting':
            admission = backend.admit(request, pair, entry, capture['case'], bundle['config'])
            if admission['status'] == 'wait':
                entry.update(phase='done', outcome='refused', reason=admission['reason'], native_no_launch=True)
                persist()
                return entry
            attempt = admission['attempt']
            entry.update(attempt_id=attempt['attempt_id'], phase='admitted')
            persist()
        if entry['phase'] == 'admitted':
            used = budget(state, entry['provider'], budget_variant)
            # This newly booked entry has no usage yet; other entries must have
            # been resolved by the gate before it was booked.
            remaining = {k: bundle['limits'][entry['provider']][k]-used[k] for k in TOKEN_FIELDS}
            entry['phase'] = 'dispatching'
            persist()
            backend.launch(root, pair, entry, bundle, remaining)
        # A resumed dispatching entry only inspects/reconciles its exact attempt.
        # It never calls launch again, even if its last visible state is admitted.
        attempt = backend.outcome(entry['attempt_id'], bundle['config'])
        if attempt['state'] in ACTIVE:
            entry.update(phase='dispatching', outcome='unresolved')
            persist()
            return {'status': 'unresolved', 'attempt_id': entry['attempt_id']}
        if attempt['state'] == 'completed':
            review = backend.review(root, pair, entry, attempt)
            if review.get('cleanup') not in ('removed', 'not-created'):
                state['halt'] = 'acceptance cleanup unresolved'
                persist()
                return {'status': 'halted', 'reason': state['halt']}
            attempt = backend.record_review(attempt, review)
        elif attempt['state'] in ('accepted', 'rejected'):
            review = backend.review(root, pair, entry, attempt, require_existing=True)
            if (attempt.get('review_evidence') != 'sha256:'+review['evidence'] or
                    (attempt['state'] == 'accepted') != (review.get('accepted') is True)):
                raise ValueError('ledger review does not match independent pilot evidence')
        entry.update(phase='done', outcome=attempt['state'], metrics=attempt.get('metrics', {}),
                     native_no_launch=attempt['state'] == 'launch_failed',
                     receipt=attempt.get('receipt'), review_evidence=attempt.get('review_evidence'),
                     at=attempt.get('at'), first_output_at=attempt.get('first_output_at'),
                     settled_at=attempt.get('settled_at'), reviewed_at=time.time())
        if budget(state, entry['provider'], budget_variant)['unknown']:
            state['halt'] = 'native usage missing after attempted execution'
        spent = budget(state, entry['provider'], budget_variant)
        entry['usage_thresholds_reached'] = [key for key in TOKEN_FIELDS
                                           if spent[key] >= bundle['limits'][entry['provider']][key]]
        if entry['usage_thresholds_reached']:
            state['halt'] = 'pilot native-token stop threshold reached'
        if entry['outcome'] in ('quota_failed', 'auth_failed') or attempt.get('launch_mismatch'):
            state['halt'] = 'native quota, authentication or identity safety stop'
        persist()
        return entry


def report(state, manifest):
    usage = {p: budget(state, p) for p in ('codex', 'claude', 'opencode')}
    for row in usage.values():
        row['dispatch_points'] = row.pop('dispatch_units')/1000000
    result = {'planned_pairs': len(manifest['pairs']), 'halt': state['halt'], 'variants': {},
              'usage': usage, 'budget_scope': manifest.get('budget_scope', 'shared'), 'arm_usage': {}}
    for variant in ('baseline', 'candidate'):
        arm_usage = {p: budget(state, p, variant) for p in usage}
        for row in arm_usage.values():
            row['dispatch_points'] = row.pop('dispatch_units')/1000000
        result['arm_usage'][variant] = arm_usage
        rows = [e for e in state['entries'] if e['variant'] == variant]
        accepted = {e['task_id'] for e in rows if e.get('outcome') == 'accepted'}
        attempted = {e['task_id'] for e in rows}
        result['variants'][variant] = {'accepted_tasks': len(accepted), 'attempted_tasks': len(attempted),
            'accepted_per_attempted_task': len(accepted)/len(attempted) if attempted else None,
            'unstarted_tasks': len(manifest['pairs'])-len(attempted),
            'unresolved_attempts': sum(e['phase'] != 'done' for e in rows),
            'refusals': sum(e.get('outcome') == 'refused' for e in rows)}
    # No automatic promotion: adapter, source-permission, high-stakes review,
    # usage-coverage and rollback gates require separate evidence.
    result['promotion'] = False
    return result


class NativeBackend:
    def __init__(self):
        router.STATE = state_migration.active_path(router.STATE)
        self.ledger = Ledger(managed_router.ledger_path(router))

    def snapshot(self, pair, config):
        probes = router.probe_all(config)
        with router.state_lock():
            state = managed_router.legacy_state(router)
            legacy = {k: state.get(k, [] if k == 'reservations' else {})
                      for k in ('snapshots', 'exhausted', 'quota_denials', 'reservations')}
            attempts = [{k: row[k] for k in ('state', 'account', 'pick', 'points')}
                        for row in self.ledger.read() if row['state'] in ACTIVE]
        return {'id': pair['id'], 'at': time.time(), 'task_sha256': pair['task_sha256'],
                'context_sha256': 'none', 'judgment': pair['judgment']['judgment'], 'floor': 1,
                'config': {k: config[k] for k in POLICY_FIELDS if k in config},
                'probes': probes, 'legacy': legacy, 'attempts': attempts}

    def admit(self, request, pair, entry, case, config):
        prior = next((a for a in self.ledger.read() if a['request_key'] == request), None)
        if prior:
            return {'status': 'existing', 'attempt': prior}
        docker = shutil.which('docker')
        if not docker:
            return {'status': 'wait', 'reason': 'pilot acceptance Docker unavailable'}
        code, endpoint = pilot_acceptance.bounded([docker, 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'])
        if code or not endpoint.decode().strip().startswith('unix:///'):
            return {'status': 'wait', 'reason': 'pilot requires local acceptance Docker'}
        code, image = pilot_acceptance.bounded([docker, 'image', 'inspect', pilot_acceptance.IMAGE, '--format', '{{.Id}}'])
        if code or not re.fullmatch(rb'sha256:[a-f0-9]{64}\s*', image):
            return {'status': 'wait', 'reason': 'pinned acceptance image unavailable'}
        decision, provider = entry['decision'], entry['provider']
        if provider not in ('codex', 'opencode'):
            return {'status': 'wait', 'reason': 'pilot source restriction not verified for selected provider'}
        floor = max(case['floor'], router.band_for(case['judgment'], config)[0])
        if decision['band'] < floor:
            return {'status': 'wait', 'reason': 'frozen selection is below shared capability floor'}
        probes = router.probe_all(config)
        probe = probes.get(provider, {})
        if probe.get('account') != case['probes'].get(provider, {}).get('account'):
            return {'status': 'wait', 'reason': 'account changed after paired observation'}
        with router.state_lock():
            state = managed_router.legacy_state(router)
            eligibility = managed_router.eligibility(router, config, probes, state, self.ledger.read())
            if not eligibility.get(provider, {}).get('eligible'):
                return {'status': 'wait', 'reason': eligibility.get(provider, {}).get('blocked') or 'fresh native quota unavailable'}
            points, slots = managed_router.external_load(state, provider, time.time())
            task = {'task_id': request.rsplit(':', 1)[0], 'spec_hash': pair['task_sha256'],
                    'context_hash': 'none', 'judgment_source': 'caller (active-agent)', 'floor': floor}
            return self.ledger.admit(request_key=request, task=task,
                pick={**decision['pick'], 'band': decision['band']}, observation=probe, points=entry['points'],
                slot_limit=int(config.get('max_inflight', {}).get(provider, config.get('max_inflight', {}).get('_default', 8))),
                reserve=float(config.get('reserves', {}).get(provider, 10)),
                binding_now=lambda: managed_router.current_binding(router, provider, config, probe),
                external_points=points, external_slots=slots,
                external_block=managed_router.external_block(router, state, provider, probe, time.time()))

    def launch(self, root, pair, entry, bundle, remaining):
        import pilot_execution
        repo = root/pair['id']/entry['variant']
        if not (repo/'.git').exists():
            native_runs.git(['init', '--template=', '-q', str(repo)], root)
            native_runs.git(['add', '--', 'solution.py', 'TASK.md'], repo)
            native_runs.git(['-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgSign=false',
                             '-c', 'user.name=Rightsize Pilot', '-c', 'user.email=pilot@example.invalid',
                             'commit', '-qm', 'fixed pilot snapshot'], repo)
        target = router.STATE.parent/'executions'/entry['attempt_id']/'worktree'
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        cwd = native_runs.worktree(repo, target)
        initial = {'git_sha256': hashlib.sha256(pilot_acceptance.source_bytes(cwd/'.git')).hexdigest(),
                   'task_sha256': hashlib.sha256(pilot_acceptance.source_bytes(cwd/'TASK.md')).hexdigest(),
                   'source_sha256': hashlib.sha256(pilot_acceptance.source_bytes(cwd/'solution.py')).hexdigest()}
        if initial['task_sha256'] != pair['task_sha256'] or initial['source_sha256'] != pair['initial_source_sha256']:
            raise ValueError('native pilot worktree differs from paired initial snapshot')
        pilot_acceptance.save(root/(entry['attempt_id']+'-initial.json'), initial, initial=True)
        return native_runs.execute(SimpleNamespace(attempt=entry['attempt_id'], spec=repo/'TASK.md',
            repo=repo, sandbox='workspace-write', timeout=bundle['timeout_seconds']), bundle['config'], router,
            adapter_factory=pilot_execution.factory(entry['attempt_id'], remaining))

    def outcome(self, attempt_id, config):
        attempt = self.ledger.read(attempt_id)
        if attempt['state'] in ACTIVE and attempt['state'] != 'admitted':
            import pilot_execution
            native_runs.reconcile(SimpleNamespace(attempt=attempt_id), config, router,
                                  opencode_adapter_class=pilot_execution.ScopedOpenCode)
            attempt = self.ledger.read(attempt_id)
        return attempt

    def review(self, root, pair, entry, attempt, require_existing=False):
        cwd = router.STATE.parent/'executions'/attempt['attempt_id']/'worktree'
        target = root/(attempt['attempt_id']+'-review.json')
        source = pilot_acceptance.source_bytes(cwd/'solution.py')
        initial = strict_read(root/(attempt['attempt_id']+'-initial.json'))
        if require_existing and not target.exists():
            raise ValueError('independent pilot review missing')
        if target.exists():
            record = strict_read(target)
            if (record.get('source_sha256') != hashlib.sha256(source).hexdigest() or
                    record.get('task_id') != pair['id'] or record.get('corpus_sha256') != pilot_tasks.CORPUS_SHA256):
                raise ValueError('acceptance evidence no longer matches native worktree')
        else:
            allowed = {'solution.py', 'TASK.md', '.git'}
            if (set(path.name for path in cwd.iterdir())-allowed or
                    hashlib.sha256(pilot_acceptance.source_bytes(cwd/'TASK.md')).hexdigest() != pair['task_sha256'] or
                    hashlib.sha256(pilot_acceptance.source_bytes(cwd/'.git')).hexdigest() != initial['git_sha256']):
                record = {'task_id': pair['id'], 'source_sha256': hashlib.sha256(source).hexdigest(),
                          'corpus_sha256': pilot_tasks.CORPUS_SHA256, 'accepted': False,
                          'cleanup': 'not-created', 'status': 'rejected', 'reason': 'task file contract changed'}
                pilot_acceptance.save(target, record, initial=True)
            else:
                record = pilot_acceptance.check(pair['id'], cwd/'solution.py', target)
        return {**record, 'evidence': hashlib.sha256(target.read_bytes()).hexdigest()}

    def record_review(self, attempt, review):
        kind = 'accepted' if review.get('accepted') is True and review.get('status') == 'checked' else 'rejected'
        return self.ledger.event(attempt['attempt_id'], attempt['launch_key']+':pilot-review', kind,
                                 evidence='sha256:'+review['evidence'])['attempt']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    init = commands.add_parser('init')
    init.add_argument('directory', type=Path)
    init.add_argument('--limits', type=Path, required=True)
    init.add_argument('--native-opencode', action='store_true', help='explicitly use the existing native Go login for this trial')
    init.add_argument('--per-arm', action='store_true', help='apply identical provider ceilings independently to each arm (twice the aggregate allowance)')
    for name in ('step', 'report'):
        command = commands.add_parser(name)
        command.add_argument('directory', type=Path)
    args = parser.parse_args()
    if args.action == 'init':
        config = router.load_config()[0]
        if args.native_opencode:
            binding = accounts.select('opencode', config)
            config = {**config, 'accounts': {**config.get('accounts', {}), 'opencode': 'pilot-native-opencode'},
                      'account_bindings': {**config.get('account_bindings', {}),
                                           'pilot-native-opencode': {'runtime': 'opencode', 'home': str(binding.home)}}}
        result = initialize(args.directory, strict_read(args.limits), config,
                            budget_scope='per-arm' if args.per_arm else 'shared')
        result = {'status': 'initialized', 'run_id': result['run_id'], 'candidate_revision': result['candidate_revision']}
    elif args.action == 'step':
        result = step(args.directory, NativeBackend())
    else:
        result = report(strict_read(args.directory/'run.json'), strict_read(args.directory/'manifest.json'))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
