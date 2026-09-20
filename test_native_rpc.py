"""Real subprocess tests: no installed runtime, credentials or model calls."""

import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import native_rpc


class NativeRpcTests(unittest.TestCase):
    def call(self, script, **kwargs):
        children = []
        original = subprocess.Popen

        def launch(*args, **options):
            child = original(*args, **options)
            children.append(child)
            return child

        with patch.object(native_rpc.subprocess, "Popen", side_effect=launch):
            result = native_rpc.read_rate_limits(
                [sys.executable, "-u", "-c", script], **kwargs)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertTrue(children[0].stdout.closed)
        self.assertTrue(children[0].stdin.closed)
        return result

    def test_silent_and_partial_line_deadlines_reap(self):
        for prefix in ("", "print('{', end='', flush=True);"):
            with self.subTest(prefix=prefix):
                start = time.monotonic()
                self.assertIsNone(self.call(
                    prefix + "import time; time.sleep(30)", timeout=0.1))
                self.assertLess(time.monotonic() - start, 2)

    def test_handshake_then_result(self):
        result = self.call('''
import sys, json
assert json.loads(sys.stdin.readline())["method"] == "initialize"
print(json.dumps({"id": 1, "result": {}}), flush=True)
assert json.loads(sys.stdin.readline())["method"] == "initialized"
assert json.loads(sys.stdin.readline())["method"] == "account/rateLimits/read"
print("not json")
print("[]")
print(json.dumps({"id": 2, "result": {"rateLimits": {"primary": {"usedPercent": 100}}}}), flush=True)
''', timeout=2)
        self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 100)

    def test_output_ceiling(self):
        self.assertIsNone(self.call("print('x' * 10000)", max_bytes=100, timeout=2))

    def test_failed_initialize_and_early_exit(self):
        for script in ("pass", 'print(\'{"id":1,"error":{"code":-1}}\')'):
            with self.subTest(script=script):
                self.assertIsNone(self.call(script, timeout=2))

    def test_missing_binary(self):
        with patch.object(native_rpc.subprocess, "Popen", side_effect=FileNotFoundError):
            self.assertIsNone(native_rpc.read_rate_limits(["absent"]))

    def test_typed_errors_are_redacted(self):
        for code, status in ((401, "reauth-required"), (429, "denied"), (-1, "unknown")):
            with self.subTest(code=code):
                script = '''
import sys, json
sys.stdin.readline()
print(json.dumps({"id": 1, "result": {}}), flush=True)
sys.stdin.readline()
sys.stdin.readline()
print(json.dumps({"id": 2, "error": {"code": CODE, "message": "secret-sentinel"}}), flush=True)
'''.replace("CODE", str(code))
                self.assertEqual(self.call(script, timeout=2), {"status": status})


if __name__ == "__main__":
    unittest.main()
