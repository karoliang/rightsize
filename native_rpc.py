"""Bounded, read-only JSON-RPC calls to a native runtime over stdio."""

import json
import os
import selectors
import subprocess
import time


def read_json(command, *, env=None, timeout=10.0, max_bytes=65536, returncodes=(0,)):
    """Read one native diagnostic JSON document with bounded output and time."""
    proc = None
    try:
        proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        data = bytearray()
        os.set_blocking(proc.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    return None
                chunk = os.read(proc.stdout.fileno(), min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    proc.wait(timeout=max(0.001, deadline - time.monotonic()))
                    if proc.returncode not in returncodes:
                        return None
                    value = json.loads(data)
                    return value if isinstance(value, dict) else None
                data.extend(chunk)
                if len(data) > max_bytes:
                    return None
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError):
        return None
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
    return None


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
                        error = message.get("error")
                        if isinstance(error, dict):
                            # Classify locally; provider messages may contain
                            # credentials or account details and never escape.
                            detail = str(error.get("message", "")).lower()
                            code = error.get("code")
                            if code == 401 or any(word in detail for word in
                                                  ("unauthorized", "login", "expired", "authentication")):
                                return {"status": "reauth-required"}
                            if code == 429 or any(word in detail for word in ("quota", "rate limit")):
                                return {"status": "denied"}
                            return {"status": "unknown"}
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
