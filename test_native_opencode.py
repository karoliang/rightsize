"""Loopback transport proof with an owned fake child, no provider calls."""

import os
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import accounts
import rightsize as r
from native_opencode import Server, ResponseError, credentials, verify_provider
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
        with self.assertRaises(ResponseError) as error:server.request("GET", "/error")
        self.assertEqual(error.exception.status, 500)

    def test_request_surface_and_input_budget(self):
        server = self.server()
        for method, path, body in (("PUT", "/auth/provider", {}), ("GET", "//other", None),
                                   ("GET", "/x\r\nInjected: secret", None),
                                   ("POST", "/message", {"text": "x" * (2 * 1024 * 1024)})):
            with self.subTest(method=method, path=path):
                with self.assertRaises(ProtocolError):server.request(method, path, body)


class CredentialBindingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for patcher in (patch.dict(os.environ, {}, clear=True),
                        patch.object(accounts.Path, "home", return_value=self.root),
                        patch.object(r, "ROOT", self.root)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.key = "SECRET_SENTINEL"
        secret = patch.object(r, "secret", return_value=self.key)
        self.secret = secret.start()
        self.addCleanup(secret.stop)
        self.binding = accounts.select("opencode", {})
        self.expected = self.binding.public()

    def provider(self):
        return {"connected": ["opencode-go"], "all": [{"id": "opencode-go", "key": "other-native-key",
            "options": {"apiKey": self.key}, "models": {"fixture": {"id": "fixture", "providerID": "opencode-go",
            "api": {"id": "fixture", "url": "https://opencode.ai/zen/go/v1", "npm": "@ai-sdk/openai-compatible"},
            "headers": {}, "options": {}, "variants": {"high": {"reasoningEffort": "high"}}}}}]}

    def test_selected_key_only_in_child_environment(self):
        value = credentials(r, {}, self.expected)
        self.secret.assert_called_once_with("OPENCODE_API_KEY", {})
        self.assertEqual(value.environment["RIGHTSIZE_OPENCODE_GO_KEY"], self.key)
        self.assertNotIn(self.key, repr(value))
        self.assertNotIn(self.key, json.dumps(value.account))
        self.assertNotIn(self.key, value.environment["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn("RIGHTSIZE_OPENCODE_GO_KEY", os.environ)
        self.assertEqual(list(self.root.iterdir()), [])
        receipt = verify_provider(self.provider(), value, "fixture", "high")
        self.assertEqual(receipt["account_ref"], self.expected["account_ref"])
        self.assertNotIn(self.key, json.dumps(receipt))

    def test_quota_identity_tracks_exact_key_without_exposing_it(self):
        with patch.object(r, "get", return_value={"usage": {"weekly": {"status": "ok", "percent": 10}}}) as get:
            first = r.probe_opencode({}, self.key)
            self.assertIsNone(first["buckets"][0]["resets_at"])
            self.assertEqual(first["quota_account_ref"], r.probe_opencode({}, self.key)["quota_account_ref"])
            self.assertNotEqual(first["quota_account_ref"], r.probe_opencode({}, "rotated")["quota_account_ref"])
            calls = get.call_count
            for malformed in (12, "bad\nkey", ""):
                self.assertEqual(r.probe_opencode({}, malformed)["status"], "no-credential")
            self.assertEqual(get.call_count, calls)
        self.assertNotIn(self.key, json.dumps(first))

    def test_exact_vault_identity_and_rotation_rejected(self):
        scope = ("project", "test", "/router")
        link = self.root / ".infisical.json"
        link.write_text(json.dumps(dict(zip(("workspaceId", "defaultEnvironment", "rightsizePath"), scope))))
        expected = {**self.expected, "source": "scoped-vault",
            "account_ref": accounts.digest(json.dumps(["opencode", scope]))[:24], "fingerprint": accounts.digest(self.key)}
        self.assertEqual(credentials(r, {}, expected).account, expected)
        self.secret.return_value = "rotated"
        with self.assertRaises(ProtocolError):credentials(r, {}, expected)
        self.secret.return_value = None
        with self.assertRaises(ProtocolError):credentials(r, {}, expected)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [".infisical.json"])

    def test_inline_configuration_preserved_without_persisting_credentials(self):
        original = {"instructions": ["AGENTS.md"], "provider": {"opencode-go": {"options": {"timeout": 1000}}}}
        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(original)}):
            result = credentials(r, {}, self.expected)
        settings = json.loads(result.environment["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(settings["instructions"], original["instructions"])
        self.assertEqual(settings["provider"]["opencode-go"]["options"]["timeout"], 1000)
        self.assertEqual(settings["enabled_providers"], ["opencode-go"])
        self.assertEqual(settings["share"], "disabled")
        for malformed in ("{", "[]", '{"provider":false}'):
            with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": malformed}):
                with self.assertRaises(ProtocolError):credentials(r, {}, self.expected)

    def test_effective_key_endpoint_model_variant_and_overrides(self):
        value = credentials(r, {}, self.expected)
        for change in ("key", "endpoint", "model", "variant", "model-options", "headers", "package", "extra-provider"):
            with self.subTest(change=change):
                data = self.provider()
                provider = data["all"][0]
                model = provider["models"]["fixture"]
                if change == "key":provider["options"]["apiKey"] = "wrong"
                if change == "endpoint":provider["options"]["baseURL"] = "https://example.invalid"
                if change == "model":model["api"]["id"] = "another"
                if change == "variant":model["variants"] = {}
                if change == "model-options":model["options"]["apiKey"] = "wrong"
                if change == "headers":model["headers"]["Authorization"] = "wrong"
                if change == "package":model["api"]["npm"] = "unverified-transport"
                if change == "extra-provider":data["connected"].append("opencode")
                with self.assertRaises(ProtocolError) as error:verify_provider(data, value, "fixture", "high")
                self.assertNotIn(self.key, str(error.exception))


if __name__ == "__main__":
    unittest.main()
