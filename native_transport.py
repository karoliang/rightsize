"""Bounded native JSON-lines transport with nonblocking stdin and stdout."""

from collections import deque
import json
import os
import selectors
import subprocess
import time


class ProtocolError(ValueError):
    pass


class JsonProcess:
    def __init__(self, argv, *, env=None, cwd=None, max_bytes=8 * 1024 * 1024,
                 max_line=1024 * 1024):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env, cwd=cwd,
                                     start_new_session=True)
        self.selector = selectors.DefaultSelector()
        os.set_blocking(self.proc.stdin.fileno(), False)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.pending = bytearray()
        self.messages = deque()
        self.notifications = deque()
        self.max_bytes, self.max_line = max_bytes, max_line
        self.received = 0
        self.next_id = 1
        self.eof = False

    def send(self, message):
        raw = json.dumps(message, allow_nan=False, ensure_ascii=False).encode() + b"\n"
        if len(self.pending) + len(raw) > 2 * 1024 * 1024:
            raise ProtocolError("native input buffer exceeded")
        if not self.pending:
            self.selector.register(self.proc.stdin, selectors.EVENT_WRITE)
        self.pending.extend(raw)

    def poll(self, timeout=0.2):
        if self.messages:
            return self.messages.popleft()
        if self.eof:
            raise ProtocolError("native process closed its stream")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for key, _ in self.selector.select(max(0, deadline - time.monotonic())):
                if key.fileobj is self.proc.stdin:
                    try:
                        written = os.write(self.proc.stdin.fileno(), self.pending[:65536])
                        del self.pending[:written]
                    except BlockingIOError:
                        continue
                    except OSError as exc:
                        raise ProtocolError("native input stream unavailable") from exc
                    if not self.pending:
                        self.selector.unregister(self.proc.stdin)
                    continue
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    self.eof = True
                    self.selector.unregister(self.proc.stdout)
                    if self.buffer:
                        raise ProtocolError("native stream ended with a partial event")
                    if not self.messages:
                        raise ProtocolError("native process closed its stream")
                    break
                self.received += len(chunk)
                if self.received > self.max_bytes:
                    raise ProtocolError("native output budget exceeded")
                self.buffer.extend(chunk)
                while b"\n" in self.buffer:
                    line, _, remainder = self.buffer.partition(b"\n")
                    self.buffer = bytearray(remainder)
                    if len(line) > self.max_line:
                        raise ProtocolError("native event budget exceeded")
                    try:
                        message = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise ProtocolError("malformed native JSON event") from exc
                    if not isinstance(message, dict):
                        raise ProtocolError("native event must be an object")
                    self.messages.append(message)
                if len(self.buffer) > self.max_line:
                    raise ProtocolError("native partial event budget exceeded")
            if self.messages:
                return self.messages.popleft()
        return None

    def request(self, method, params, *, timeout=15):
        request_id = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.poll(min(0.2, max(0, deadline - time.monotonic())))
            if message is None:
                continue
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise ProtocolError("native request rejected")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ProtocolError("native response must be an object")
                return result
            if "id" in message and "method" in message:
                self.answer_request(message)
            else:
                self.notifications.append(message)
        raise ProtocolError("native request deadline exceeded")

    def answer_request(self, message):
        method = message.get("method")
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            self.send({"id": message["id"], "result": {"decision": "decline"}})
        else:
            self.send({"id": message["id"], "error": {"code": -32601,
                      "message": "Managed adapter does not grant interactive permissions"}})

    def notification(self, timeout=0.2):
        if self.notifications:
            return self.notifications.popleft()
        message = self.poll(timeout)
        if message and "id" in message and "method" in message:
            self.answer_request(message)
            return None
        return message

    def close(self):
        self.selector.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()
