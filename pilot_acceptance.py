"""Bounded Docker acceptance for the fixed coding pilot; no host code execution."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid

import pilot_tasks

IMAGE = 'python@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0'
MAX_SOURCE = 65536
MAX_OUTPUT = 65536
BOOTSTRAP = '''import builtins,json,sys
payload=json.load(sys.stdin)
allowed=BUILTIN_NAMES.split()
namespace={"__builtins__":{key:getattr(builtins,key) for key in allowed}}
exec(compile(payload["source"],"<candidate>","exec"),namespace)
results=[]
for args in payload["args"]:
    try:
        value=namespace["solve"](*args)
        results.append({"value":value,"args":args})
    except BaseException:
        results.append({"error":"candidate-error"})
print(json.dumps(results,allow_nan=False,separators=(",",":")))
'''.replace('BUILTIN_NAMES', repr(pilot_tasks.BUILTINS))


class AcceptanceError(Exception):
    pass


def bounded(command, *, payload=b'', timeout=15):
    """Bound both pipe directions and reap the CLI; container cleanup is separate."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True,
                               env={"PATH": os.defpath, "LANG": "C.UTF-8"})
    output = bytearray()
    position = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdin, process.stdout):
                os.set_blocking(stream.fileno(), False)
            if payload:
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcceptanceError('sandbox command deadline exceeded')
                for key, _ in selector.select(min(remaining, 0.1)):
                    if key.fileobj is process.stdin:
                        try:
                            position += os.write(process.stdin.fileno(), payload[position:position+8192])
                        except BrokenPipeError:
                            position = len(payload)
                        if position == len(payload):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        data = os.read(process.stdout.fileno(), 8192)
                        if not data:
                            selector.unregister(process.stdout)
                            continue
                        output.extend(data)
                        if len(output) > MAX_OUTPUT:
                            raise AcceptanceError('sandbox output limit exceeded')
            process.wait(timeout=max(0.01, deadline-time.monotonic()))
        return process.returncode, bytes(output)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AcceptanceError('sandbox command unavailable') from exc
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        for stream in (process.stdin, process.stdout):
            stream.close()


def source_bytes(path, limit=MAX_SOURCE):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise AcceptanceError('candidate must be a regular file')
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise AcceptanceError('candidate source limit exceeded')
    return raw


def validate_source(raw):
    try:
        tree = ast.parse(raw.decode('utf-8'))
    except (SyntaxError, UnicodeError, ValueError, RecursionError) as exc:
        raise AcceptanceError('candidate syntax invalid') from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 4000:
        raise AcceptanceError('candidate syntax limit exceeded')
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) and not (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            raise AcceptanceError('candidate must contain only functions and docstrings')
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.Global, ast.Nonlocal)):
            raise AcceptanceError('candidate uses unsupported syntax')
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ''
        if name.startswith('__') or name in ('eval', 'exec', 'compile', 'open', 'globals', 'locals', 'getattr', 'setattr', 'vars'):
            raise AcceptanceError('candidate uses unsupported introspection or IO')
        if isinstance(node, ast.FunctionDef) and (node.decorator_list or node.name.startswith('__')):
            raise AcceptanceError('candidate decorators and dunder functions are unsupported')
    return raw.decode('utf-8')


def save(path, record, *, initial=False):
    raw = json.dumps(record, indent=2).encode() + b'\n'
    if initial:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    else:
        fd, temporary = tempfile.mkstemp(prefix='.acceptance-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def check(task_id, candidate, record_path):
    tasks = {row['id']: row for row in pilot_tasks.load()}
    if task_id not in tasks:
        raise AcceptanceError('unknown pilot task')
    row = tasks[task_id]
    raw = source_bytes(candidate)
    record_path = Path(record_path)
    record = {'schema_version': 1, 'task_id': task_id, 'corpus_sha256': pilot_tasks.CORPUS_SHA256,
              'source_sha256': hashlib.sha256(raw).hexdigest(), 'image': IMAGE,
              'status': 'preparing', 'accepted': False, 'cleanup': 'not-created'}
    try:
        source = validate_source(raw)
    except AcceptanceError as exc:
        record.update(status='rejected', error=str(exc))
        save(record_path, record, initial=True)
        return record
    docker = shutil.which('docker')
    if not docker:
        raise AcceptanceError('Docker unavailable; host execution is forbidden')
    code, output = bounded([docker, 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'])
    endpoint = output.decode().strip()
    if code or not endpoint.startswith('unix:///') or '\n' in endpoint:
        raise AcceptanceError('a local Unix Docker endpoint is required')
    name = 'rightsize-pilot-' + uuid.uuid4().hex
    record.update(container_name=name, docker_endpoint=endpoint, cleanup='pending')
    save(record_path, record, initial=True)
    with tempfile.TemporaryDirectory(prefix='rightsize-docker-config-') as config:
        cli = [docker, '--config', config, '--host', endpoint]
        try:
            # --config excludes host proxy/credential configuration. No mounts or env forwarding.
            command = cli + ['create', '--name', name, '--pull', 'never', '--interactive',
                             '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                             '--security-opt', 'no-new-privileges:true', '--user', '65534:65534',
                             '--memory', '256m', '--memory-swap', '256m', '--cpus', '0.5',
                             '--pids-limit', '16', '--ulimit', 'cpu=2:2', '--ulimit', 'nofile=64:64',
                             '--ulimit', 'core=0:0', '--log-driver', 'none',
                             '--entrypoint', '/usr/local/bin/python3', IMAGE, '-I', '-S', '-B', '-c', BOOTSTRAP]
            code, output = bounded(command)
            identifier = output.decode().strip()
            if code or not re.fullmatch('[0-9a-f]{64}', identifier):
                raise AcceptanceError('sandbox creation unavailable; no candidate started')
            record.update(container_id=identifier, status='created')
            save(record_path, record)
            code, output = bounded(cli + ['inspect', identifier])
            if code:
                raise AcceptanceError('sandbox configuration inspection unavailable')
            inspected = json.loads(output)[0]
            host = inspected['HostConfig']
            limits = {row['Name']: (row['Soft'], row['Hard']) for row in host['Ulimits']}
            if (inspected['Id'] != identifier or inspected['Mounts'] or
                    host['NetworkMode'] != 'none' or not host['ReadonlyRootfs'] or
                    host['Memory'] != 256*1024*1024 or host['MemorySwap'] != 256*1024*1024 or
                    host['NanoCpus'] != 500000000 or host['PidsLimit'] != 16 or
                    host['CapDrop'] != ['ALL'] or host.get('Privileged') or
                    host.get('Devices') or host.get('DeviceRequests') or host.get('PidMode') or
                    host.get('IpcMode') == 'host' or
                    limits.get('cpu') != (2, 2) or limits.get('nofile') != (64, 64) or
                    limits.get('core') != (0, 0) or
                    'no-new-privileges:true' not in host['SecurityOpt'] or
                    inspected['Config']['User'] != '65534:65534' or
                    inspected['Config']['Image'] != IMAGE or
                    inspected['Config']['Entrypoint'] != ['/usr/local/bin/python3']):
                raise AcceptanceError('sandbox configuration mismatch')
            record['status'] = 'running'
            save(record_path, record)
            payload = json.dumps({'source': source, 'args': [case['args'] for case in row['cases']]}).encode()
            code, output = bounded(cli + ['start', '--attach', '--interactive', identifier], payload=payload, timeout=10)
            if code:
                raise AcceptanceError('candidate failed or exceeded resource limit')
            results = json.loads(output)
            if not isinstance(results, list) or len(results) != len(row['cases']):
                raise AcceptanceError('candidate result shape invalid')
            passed = 0
            for result, case in zip(results, row['cases']):
                expected = {'value': case['expected'], 'args': case['args']}
                passed += json.dumps(result, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)
            record.update(status='checked', passed=passed, total=len(row['cases']), accepted=passed == len(row['cases']))
        except (AcceptanceError, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
            record.update(status='error', accepted=False,
                          error=str(exc) if isinstance(exc, AcceptanceError) else 'sandbox evidence invalid')
        finally:
            try:
                # Cleanup by pre-recorded random name also handles a lost create acknowledgement.
                bounded(cli + ['rm', '--force', name])
                code, output = bounded(cli + ['ps', '--all', '--quiet', '--filter', 'name=^/' + name + '$'])
                record['cleanup'] = 'removed' if code == 0 and not output.strip() else 'unresolved'
            except (AcceptanceError, OSError):
                record['cleanup'] = 'unresolved'
            if record['cleanup'] != 'removed':
                record.update(status='unresolved', accepted=False)
            save(record_path, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--record', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = check(args.task, args.candidate, args.record)
    except (AcceptanceError, OSError) as exc:
        print(json.dumps({'status': 'error', 'accepted': False, 'error': str(exc) if isinstance(exc, AcceptanceError) else 'acceptance input unavailable'}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result['accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
