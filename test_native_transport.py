"""Real subprocess backpressure, framing and deadline regression tests."""

import sys
import time
import unittest

from native_transport import JsonProcess, ProtocolError


class TransportTests(unittest.TestCase):
    def process(self, script, **kwargs):
        client = JsonProcess([sys.executable, "-u", "-c", script], **kwargs)
        self.addCleanup(client.close)
        return client

    def test_nonreading_stdin_does_not_block_request_deadline(self):
        client = self.process("import time; time.sleep(30)")
        start = time.monotonic()
        with self.assertRaisesRegex(ProtocolError, "deadline"):
            client.request("test", {"text": "x" * 200000}, timeout=0.1)
        self.assertLess(time.monotonic() - start, 1)

    def test_partial_line_cannot_defeat_deadline(self):
        client = self.process("import time; print('{',end='',flush=True); time.sleep(30)")
        start = time.monotonic()
        with self.assertRaisesRegex(ProtocolError, "deadline"):
            client.request("test", {}, timeout=0.1)
        self.assertLess(time.monotonic() - start, 1)

    def test_output_and_line_budgets(self):
        for options in ({"max_line": 100}, {"max_bytes": 100}):
            with self.subTest(options=options):
                client = self.process("print('x' * 10000)", **options)
                with self.assertRaisesRegex(ProtocolError, "budget"):
                    client.poll(2)

    def test_malformed_events_never_escape_in_error_text(self):
        for script in ("print('secret-sentinel')", "print('[]')", "print('{',end='',flush=True)"):
            with self.subTest(script=script):
                client = self.process(script)
                with self.assertRaises(ProtocolError) as error:
                    client.poll(2)
                self.assertNotIn("secret-sentinel", str(error.exception))

    def test_approval_is_declined_not_executed(self):
        client = self.process('''
import sys,json
request=json.loads(sys.stdin.readline())
print(json.dumps({"id":90,"method":"item/commandExecution/requestApproval","params":{"command":"untrusted command"}}),flush=True)
answer=json.loads(sys.stdin.readline())
assert answer["result"]["decision"] == "decline"
print(json.dumps({"id":request["id"],"result":{"declined":True}}),flush=True)
''')
        self.assertEqual(client.request("test", {}, timeout=2), {"declined": True})


if __name__ == "__main__":
    unittest.main()
