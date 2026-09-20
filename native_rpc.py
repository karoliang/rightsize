"""Bounded, read-only JSON-RPC calls to a native runtime over stdio."""

import json
import os
import selectors
import subprocess
import time


def read_rate_limits(command, *, env=None, timeout=15.0, max_bytes=1024 * 1024):
    """Initialize Codex before querying; always close pipes and reap the child.

    A partial line must not defeat the deadline. The byte ceiling applies to
    all output, including notifications and malformed lines.
    """
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(command, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    try:
        def send(message):
            proc.stdin.write(json.dumps(message).encode() + b"\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "rightsize", "version": "1"}}})
        buffer = b""
        received = 0
        initialized = False
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    return None
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    return None
                received += len(chunk)
                if received > max_bytes:
                    return None
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        message = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get("id") == 1 and not initialized:
                        if "error" in message or "result" not in message:
                            return None
                        initialized = True
                        send({"jsonrpc": "2.0", "method": "initialized"})
                        send({"jsonrpc": "2.0", "id": 2,
                              "method": "account/rateLimits/read", "params": {}})
                    elif initialized and message.get("id") == 2:
                        result = message.get("result")
                        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()
    return None
