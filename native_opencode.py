"""Owned OpenCode loopback transport. No model calls or auth-store writes."""

import base64
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

from native_transport import ProtocolError


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
                    raise ProtocolError("native request was not accepted")
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
