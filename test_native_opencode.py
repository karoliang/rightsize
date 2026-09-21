"""Loopback transport proof with an owned fake child, no provider calls."""

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from native_opencode import Server
from native_transport import ProtocolError


FAKE = r'''
import base64,json,os,socket,time
mode=os.environ.get('FAKE_MODE','normal')
if mode=='silent':time.sleep(10)
if mode=='exit':raise SystemExit(0)
if mode=='flood':print('SECRET_SENTINEL'*6000,flush=True);time.sleep(10)
listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
port=listener.getsockname()[1]
if mode=='remote':print('opencode server listening on http://example.invalid:1234',flush=True);time.sleep(10)
line='opencode server listening on http://127.0.0.1:'+str(port)
if mode=='partial':
 print(line[:-1],end='',flush=True);time.sleep(.05);print(line[-1:],flush=True)
else:print(line,flush=True)
expected='Basic '+base64.b64encode(('opencode:'+os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()
while True:
 connection,_=listener.accept()
 try:
  request=b''
  while b'\r\n\r\n' not in request:request+=connection.recv(65536)
  if ('Authorization: '+expected).encode() not in request:raise RuntimeError('wrong auth')
  if os.environ['OPENCODE_SERVER_USERNAME']!='opencode':raise RuntimeError('wrong username')
  path=request.split(b' ')[1]
  if path==b'/redirect':connection.sendall(b'HTTP/1.1 302 Found\r\nLocation: http://example.invalid/SECRET_SENTINEL\r\nContent-Length: 0\r\n\r\n');continue
  if path==b'/error':connection.sendall(b'HTTP/1.1 500 Error\r\nContent-Length: 15\r\n\r\nSECRET_SENTINEL');continue
  if path==b'/headers':
   connection.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
   for _ in range(50):connection.sendall(b'x');time.sleep(.05)
   continue
  if path==b'/slow':
   connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n')
   for _ in range(50):connection.sendall(b'x');time.sleep(.05)
   continue
  if path==b'/empty':connection.sendall(b'HTTP/1.1 204 No Content\r\n\r\n');continue
  payload=b'{' if path==b'/malformed' else (b'x'*8192 if path==b'/large' else b'{"healthy":true}')
  connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload)
 except (OSError,RuntimeError):pass
 finally:connection.close()
'''


class ServerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name)

    def server(self, mode="normal", **kwargs):
        value = Server(env={**os.environ, "FAKE_MODE": mode,
                            "OPENCODE_SERVER_USERNAME": "wrong-inherited-user",
                            "OPENCODE_SERVER_PASSWORD": "SECRET_SENTINEL"}, cwd=self.cwd,
                       argv=[sys.executable, "-u", "-c", FAKE], **kwargs)
        self.addCleanup(value.close)
        return value

    def test_owned_auth_json_and_empty_ack(self):
        server = self.server()
        self.assertEqual(server.request("GET", "/global/health"), {"healthy": True})
        self.assertIsNone(server.request("POST", "/empty", {"value": "task"}))
        server.close()
        self.assertIsNotNone(server.proc.returncode)
        self.assertTrue(server.proc.stdout.closed)
        with self.assertRaises(ProtocolError):server.request("GET", "/global/health")

    def test_partial_endpoint_waits_for_complete_line(self):
        self.assertEqual(self.server("partial").request("GET", "/health"), {"healthy": True})

    def test_failed_start_reaps_child_and_redacts_output(self):
        import subprocess
        popen = subprocess.Popen
        for mode in ("silent", "exit", "flood", "remote"):
            children = []
            def create(*args, **kwargs):
                child = popen(*args, **kwargs)
                children.append(child)
                return child
            with self.subTest(mode=mode), patch("native_opencode.subprocess.Popen", side_effect=create):
                with self.assertRaises(ProtocolError) as error:self.server(mode, timeout=.2)
                self.assertNotIn("SECRET_SENTINEL", str(error.exception))
                self.assertIsNotNone(children[0].returncode)
                self.assertTrue(children[0].stdout.closed)

    def test_total_deadline_includes_trickled_headers_and_body(self):
        for path in ("/headers", "/slow"):
            with self.subTest(path=path):
                server = self.server()
                start = time.monotonic()
                with self.assertRaises(ProtocolError):server.request("GET", path, timeout=.2)
                self.assertLess(time.monotonic() - start, 1)
                server.close()

    def test_untrusted_errors_redirects_malformed_and_oversized_bodies(self):
        server = self.server()
        for path in ("/redirect", "/error", "/malformed", "/large"):
            with self.subTest(path=path):
                with self.assertRaises(ProtocolError) as error:server.request("GET", path, max_bytes=1024)
                self.assertNotIn("SECRET_SENTINEL", str(error.exception))

    def test_request_surface_and_input_budget(self):
        server = self.server()
        for method, path, body in (("PUT", "/auth/provider", {}), ("GET", "//other", None),
                                   ("GET", "/x\r\nInjected: secret", None),
                                   ("POST", "/message", {"text": "x" * (2 * 1024 * 1024)})):
            with self.subTest(method=method, path=path):
                with self.assertRaises(ProtocolError):server.request(method, path, body)


if __name__ == "__main__":
    unittest.main()
