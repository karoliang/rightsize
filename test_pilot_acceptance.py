"""Bounded acceptance, refusal controls and opt-in real container isolation proof."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pilot_acceptance as acceptance
import pilot_tasks


class AcceptanceTests(unittest.TestCase):
    def test_source_boundary_and_all_reference_syntax(self):
        for row in pilot_tasks.load():
            acceptance.validate_source(row['reference_source'].encode())
        for source in ('import os', 'print(1)', 'def solve(x): return x.__class__',
                       'def solve(x): return open("secret")', 'def solve(: pass'):
            with self.subTest(source=source), self.assertRaises(acceptance.AcceptanceError):
                acceptance.validate_source(source.encode())
        acceptance.validate_source(b'def solve(x): return [1 for _ in x]')

    def test_regular_bounded_file_and_no_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.py'
            source.write_bytes(b'x' * (acceptance.MAX_SOURCE + 1))
            with self.assertRaises(acceptance.AcceptanceError):
                acceptance.source_bytes(source)
            link = root / 'link.py'
            link.symlink_to(source)
            with self.assertRaises(OSError):
                acceptance.source_bytes(link)
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            with self.assertRaises(acceptance.AcceptanceError):
                acceptance.source_bytes(fifo)

    def test_cli_pipes_have_deadlines_and_output_limits(self):
        with self.assertRaisesRegex(acceptance.AcceptanceError, 'deadline'):
            acceptance.bounded([sys.executable, '-I', '-S', '-c', 'while True: pass'], timeout=0.1)
        with self.assertRaisesRegex(acceptance.AcceptanceError, 'output limit'):
            acceptance.bounded([sys.executable, '-I', '-S', '-c', 'print("x"*100000)'])
        code, output = acceptance.bounded([sys.executable, '-I', '-S', '-c',
                                          'import sys;print(len(sys.stdin.buffer.read()))'], payload=b'x'*200000)
        self.assertEqual((code, output), (0, b'200000\n'))

    def test_invalid_source_has_durable_rejection_without_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'solution.py'
            source.write_text('import os')
            record = root / 'review.json'
            with patch.object(acceptance, 'bounded', side_effect=AssertionError('must not launch')):
                result = acceptance.check(pilot_tasks.load()[0]['id'], source, record)
            self.assertEqual((result['status'], result['cleanup']), ('rejected', 'not-created'))
            self.assertFalse(result['accepted'])
            self.assertEqual(json.loads(record.read_text()), result)
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                acceptance.check(pilot_tasks.load()[0]['id'], source, record)

    def test_lost_creation_ack_is_not_retried_and_cleanup_unknown_is_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'solution.py'
            source.write_text(pilot_tasks.load()[0]['reference_source'])
            calls = []
            def command(argv, **kwargs):
                calls.append(argv)
                if 'context' in argv:
                    return 0, b'unix:///fixture/docker.sock\n'
                raise acceptance.AcceptanceError('fixture lost acknowledgement')
            with patch.object(acceptance.shutil, 'which', return_value='/fixture/docker'), \
                    patch.object(acceptance, 'bounded', side_effect=command):
                result = acceptance.check(pilot_tasks.load()[0]['id'], source, root/'review.json')
            self.assertEqual(result['status'], 'unresolved')
            self.assertFalse(result['accepted'])
            self.assertEqual(sum('create' in argv for argv in calls), 1)
            self.assertFalse(any('start' in argv for argv in calls))
            self.assertTrue(any('rm' in argv for argv in calls))
            self.assertIn('container_name', json.loads((root/'review.json').read_text()))


@unittest.skipUnless(os.environ.get('RIGHTSIZE_TEST_DOCKER') == '1', 'explicit Docker integration gate')
class ContainerAcceptanceTests(unittest.TestCase):
    def run_source(self, source, bootstrap=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'solution.py'
            path.write_text(source)
            with patch.object(acceptance, 'BOOTSTRAP', bootstrap or acceptance.BOOTSTRAP):
                result = acceptance.check(pilot_tasks.load()[0]['id'], path, root/'review.json')
            self.assertEqual(result['cleanup'], 'removed')
            self.assertEqual(json.loads((root/'review.json').read_text()), result)
            return result

    def test_all_references_accepted_and_all_initial_defects_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for row in pilot_tasks.load():
                for kind, expected in [('reference', True), ('initial', False)]:
                    with self.subTest(task=row['id'], kind=kind):
                        path = root/(row['id']+'-'+kind+'.py')
                        path.write_text(row[kind+'_source'])
                        result = acceptance.check(row['id'], path, path.with_suffix('.json'))
                        self.assertEqual(result['accepted'], expected, result)
                        self.assertEqual(result['cleanup'], 'removed')

    def test_isolation_probe_has_no_host_file_secret_network_or_root_write(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory)/'host-secret'
            secret.write_text('sentinel')
            probe = '''import os,socket,resource
assert os.getuid()==65534
assert not os.path.exists(HOST_PATH)
assert "RIGHTSIZE_ACCEPTANCE_SECRET" not in os.environ
assert resource.getrlimit(resource.RLIMIT_CPU)==(2,2)
try:
    open("/escape", "w").write("bad")
    raise AssertionError("root write allowed")
except OSError:
    pass
try:
    socket.create_connection(("1.1.1.1",53),timeout=1)
    raise AssertionError("network allowed")
except OSError:
    pass
'''.replace('HOST_PATH', repr(str(secret))) + acceptance.BOOTSTRAP
            with patch.dict(os.environ, {'RIGHTSIZE_ACCEPTANCE_SECRET':'not-forwarded'}):
                result = self.run_source(pilot_tasks.load()[0]['reference_source'], probe)
            self.assertTrue(result['accepted'], result)
            self.assertEqual(secret.read_text(), 'sentinel')

    def test_effective_configuration_mismatch_prevents_start(self):
        actual = acceptance.bounded
        calls = []
        def inspect_mismatch(command, **kwargs):
            calls.append(command)
            code, output = actual(command, **kwargs)
            if 'inspect' in command and 'context' not in command and code == 0:
                evidence = json.loads(output)
                evidence[0]['HostConfig']['NetworkMode'] = 'bridge'
                output = json.dumps(evidence).encode()
            return code, output
        with patch.object(acceptance, 'bounded', side_effect=inspect_mismatch):
            result = self.run_source(pilot_tasks.load()[0]['reference_source'])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error'], 'sandbox configuration mismatch')
        self.assertFalse(any('start' in command for command in calls))

    def test_cpu_memory_output_and_wall_limits_remove_container(self):
        sources = ['def solve(record):\n    while True: pass\n',
                   'def solve(record):\n    return "x"*(1024**3)\n',
                   'def solve(record):\n    return "x"*100000\n']
        for source in sources:
            with self.subTest(source=source):
                result = self.run_source(source)
                self.assertFalse(result['accepted'])
                self.assertEqual(result['status'], 'error')
        result = self.run_source('def solve(record): return record',
                                 'import time;time.sleep(60)\n'+acceptance.BOOTSTRAP)
        self.assertEqual(result['status'], 'error')
        self.assertIn('deadline', result['error'])


if __name__ == '__main__':
    unittest.main()
