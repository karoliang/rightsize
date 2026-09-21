"""Owned OpenCode loopback transport. No model calls or auth-store writes."""

import base64
from dataclasses import dataclass, field
import http.client
import json
import os
import re
import secrets
import selectors
import socket
import subprocess
import threading
import time

import accounts
from native_transport import ProtocolError


class ResponseError(ProtocolError):
    """Only an HTTP status, never the credential-bearing native error body."""

    def __init__(self, status):
        self.status = status
        super().__init__(f"native request returned HTTP {status}")


@dataclass(repr=False)
class Credentials:
    account: dict
    native_binding: dict
    key: str = field(repr=False)
    environment: dict = field(repr=False)


def credentials(api, config, expected):
    """Resolve the admitted Go credential once, then deliver only in child env.

    This uses the existing exact-scoped resolver. It does not add a login, write
    a config/auth file, enumerate a vault, or fall back after a failed read.
    """
    binding = accounts.select("opencode", config)
    if binding.status != "unverified":
        raise ProtocolError("native account selection unavailable")
    key = api.secret("OPENCODE_API_KEY", config)
    if not isinstance(key, str) or not key or any(c in key for c in "\r\n\x00"):
        raise ProtocolError("selected credential unavailable")
    public = binding.public()
    if binding.source == "native" and (api.ROOT / ".infisical.json").exists():
        scope = api.vault_scope()
        if scope is None:
            raise ProtocolError("explicit vault scope unavailable")
        public = {**public, "source": "scoped-vault",
                  "account_ref": accounts.digest(json.dumps(["opencode", scope]))[:24],
                  "fingerprint": accounts.digest(key)}
    if public != expected or accounts.select("opencode", config).public() != binding.public():
        raise ProtocolError("credential binding changed since admission")
    env = binding.environment()
    try:
        settings = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        if not isinstance(settings, dict):
            raise ValueError()
        providers = settings.setdefault("provider", {})
        provider = providers.setdefault("opencode-go", {})
        options = provider.setdefault("options", {})
        options["apiKey"] = "{env:RIGHTSIZE_OPENCODE_GO_KEY}"
        settings["enabled_providers"] = ["opencode-go"]
        settings["share"] = "disabled"
        settings["autoupdate"] = False
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(settings, allow_nan=False)
    except (ValueError, TypeError, AttributeError):
        raise ProtocolError("native inline configuration malformed") from None
    env["RIGHTSIZE_OPENCODE_GO_KEY"] = key
    return Credentials(public, binding.public(), key, env)


def verify_provider(value, credential, model, effort):
    """Prove effective Go key/endpoint/model before creating an inference task.

    Provider results may contain keys. The caller must keep them in memory and
    must never persist this response as routing evidence.
    """
    try:
        rows = [row for row in value["all"] if row["id"] == "opencode-go"]
        if len(rows) != 1 or value["connected"] != ["opencode-go"]:
            raise ValueError()
        provider = rows[0]
        options = provider["options"]
        if set(options) - {"apiKey", "baseURL", "setCacheKey", "timeout", "headerTimeout", "chunkTimeout"}:
            raise ValueError()
        effective_key = options.get("apiKey", provider.get("key"))
        if effective_key != credential.key:
            raise ValueError()
        entry = provider["models"][model]
        if (entry["providerID"] != "opencode-go" or entry["id"] != model
                or entry["api"]["id"] != model
                or entry["api"]["npm"] != "@ai-sdk/openai-compatible"
                or entry["api"]["url"].rstrip("/") != "https://opencode.ai/zen/go/v1"
                or options.get("baseURL", "https://opencode.ai/zen/go/v1").rstrip("/") != "https://opencode.ai/zen/go/v1"
                or options.get("headers") or entry.get("headers")):
            raise ValueError()
        # Model/variant options are applied after provider options. A credential
        # or endpoint override there would invalidate the quota/launch binding.
        layers = [entry.get("options", {})]
        if effort:
            layers.append(entry["variants"][effort])
        sensitive = {"apiKey", "baseURL", "headers", "fetch", "url", "provider", "model"}
        if any(not isinstance(layer, dict) or sensitive.intersection(layer) for layer in layers):
            raise ValueError()
        return {"provider": "opencode", "model": model, "effort": effort,
                "account_ref": credential.account["account_ref"],
                "fingerprint": credential.account["fingerprint"]}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProtocolError("effective native provider differs from admitted binding") from None


class Server:
    """One private native server; never attach to a caller-provided endpoint."""

    def __init__(self, *, env, cwd, argv=None, timeout=15):
        self.proc = None
        self.port = None
        password = secrets.token_urlsafe(32)
        self._authorization = "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()
        child_env = {**env, "OPENCODE_SERVER_PASSWORD": password,
                     "OPENCODE_SERVER_USERNAME": "opencode"}
        try:
            self.proc = subprocess.Popen(
                argv or ["opencode", "serve", "--hostname", "127.0.0.1", "--port", "0"],
                env=child_env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + timeout
            raw = bytearray()
            received = 0
            with selectors.DefaultSelector() as selector:
                os.set_blocking(self.proc.stdout.fileno(), False)
                selector.register(self.proc.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        break
                    chunk = os.read(self.proc.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    raw.extend(chunk)
                    received += len(chunk)
                    if received > 65536:
                        raise ProtocolError("native server startup output exceeds budget")
                    # Wait for the entire line; a partial port is not an endpoint.
                    while b"\n" in raw:
                        line, _, remainder = raw.partition(b"\n")
                        raw = bytearray(remainder)
                        match = re.fullmatch(rb"opencode server listening on http://127\.0\.0\.1:([0-9]+)\r?", line)
                        if match and 0 < int(match[1]) < 65536:
                            self.port = int(match[1])
                            # Native logs must not fill an unread pipe after startup.
                            self._drain = threading.Thread(target=self._discard_output, daemon=True)
                            self._drain.start()
                            return
            raise ProtocolError("native server did not announce a loopback endpoint")
        except (OSError, ValueError, subprocess.SubprocessError):
            self.close()
            raise ProtocolError("native server setup unavailable") from None

    def _discard_output(self):
        # No raw native output is retained or forwarded. A dead child ends this
        # thread, and close joins it before closing the descriptor.
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.proc.stdout, selectors.EVENT_READ)
                while self.proc.poll() is None:
                    if selector.select(0.2) and not os.read(self.proc.stdout.fileno(), 65536):
                        return
        except (OSError, ValueError):
            return

    def request(self, method, path, body=None, *, timeout=15, max_bytes=4 * 1024 * 1024):
        if (method not in ("GET", "POST") or not isinstance(path, str)
                or not re.fullmatch(r"/[A-Za-z0-9_/?=&.%+~-]*", path) or path.startswith("//")):
            raise ProtocolError("unsupported native request")
        if self.port is None or self.proc.poll() is not None:
            raise ProtocolError("native server is not running")
        try:
            payload = None if body is None else json.dumps(body, allow_nan=False).encode()
        except (ValueError, TypeError, RecursionError):
            raise ProtocolError("native request is not valid JSON") from None
        if payload is not None and len(payload) > 2 * 1024 * 1024:
            raise ProtocolError("native request exceeds budget")
        if timeout <= 0 or max_bytes <= 0:
            raise ProtocolError("invalid native request bounds")
        deadline = time.monotonic() + timeout
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        timer = None
        expired = threading.Event()
        try:
            connection.connect()
            native_socket = connection.sock
            def expire():
                expired.set()
                try:
                    native_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError("native request deadline exceeded")
            # A socket's idle timeout alone permits an endless trickle. Interrupt
            # the exact connected socket at the total deadline, including headers.
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            connection.request(method, path, body=payload, headers={
                "Authorization": self._authorization, "Content-Type": "application/json",
                "Accept": "application/json", "Connection": "close"})
            with connection.getresponse() as response:
                if response.status not in (200, 201, 204):
                    # No redirect following, proxy, or raw provider error body.
                    raise ResponseError(response.status)
                data = bytearray()
                while True:
                    chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ProtocolError("native response exceeds budget")
                if expired.is_set() or time.monotonic() >= deadline:
                    raise ProtocolError("native request deadline exceeded")
                if response.status == 204 and not data:
                    return None
                return json.loads(data)
        except ResponseError:
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
            raise ProtocolError("native response unavailable or invalid") from None
        finally:
            if timer:
                timer.cancel()
                timer.join()
            connection.close()

    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            if hasattr(self, "_drain"):
                self._drain.join(timeout=1)
            self.proc.stdout.close()
        self.port = None
        self._authorization = None
