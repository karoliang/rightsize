"""Managed Go execution through owned native OpenCode sessions."""

from pathlib import Path
import json
import re
import time

import accounts
from managed_ledger import ACTIVE, LedgerError, identity, units
from managed_router import NoLaunch
from native_opencode import Server, credentials, verify_provider, ResponseError
from native_transport import ProtocolError


def native_id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r"[A-Za-z0-9_-]{1,128}", value):
        raise ProtocolError("native identity unavailable")
    return value


def permissions(sandbox):
    if sandbox not in ("read-only", "workspace-write"):
        raise LedgerError("unsupported native permissions")
    names = ["read", "glob", "grep"]
    if sandbox == "workspace-write":
        names += ["edit", "write", "apply_patch"]
    return [{"permission": "*", "pattern": "*", "action": "deny"},
            *[{"permission": name, "pattern": "*", "action": "allow"} for name in names]]


def terminal_kind(info):
    error = info.get("error")
    if error:
        if not isinstance(error, dict):
            raise ProtocolError("native error schema unavailable")
        name = error.get("name")
        code = (error.get("data") or {}).get("statusCode")
        if name == "MessageAbortedError":
            return "cancelled"
        if name == "ProviderAuthError" or code in (401, 403):
            return "auth_failed"
        if name == "APIError" and code == 429:
            return "quota_failed"
        return "runtime_failed"
    if info.get("time", {}).get("completed") is not None and info.get("finish") in ("stop", "length", "content-filter"):
        return "completed" if info["finish"] == "stop" else "runtime_failed"
    return None


class OpenCode:
    def __init__(self, ledger, config, prompt, worktree, *, transport=Server, sandbox="read-only", api=None):
        if api is None:
            import rightsize as api
        self.api, self.ledger, self.config = api, ledger, config
        self.prompt, self.worktree = prompt, Path(worktree).resolve()
        self.transport, self.rules, self.sandbox = transport, permissions(sandbox), sandbox
        self.server = None
        self.credential = None
        self.receipt = None
        self.metrics, self.texts = {}, {}

    def check_session(self, session):
        model = {"id": self.attempt["pick"]["model"], "providerID": "opencode-go"}
        if self.attempt["pick"]["effort"]:
            model["variant"] = self.attempt["pick"]["effort"]
        if (Path(session.get("directory", "")).resolve() != self.worktree or session.get("model") != model
                or session.get("permission") != self.rules
                or session.get("metadata", {}).get("rightsize_launch_key") != self.attempt["launch_key"]):
            raise ProtocolError("native session differs from admitted setup")
        return native_id(session.get("id"), "ses")

    def launch(self, attempt):
        self.attempt = attempt
        self.message_id = "msg_" + attempt["launch_key"]
        pick = attempt["pick"]
        try:
            self.credential = credentials(self.api, self.config, attempt["account"])
            expected = accounts.digest("opencode-go:" + self.credential.key)
            if attempt["observation"].get("quota_account_ref") != expected:
                raise ProtocolError("quota credential differs from selected native key")
            settings = json.loads(self.credential.environment["OPENCODE_CONFIG_CONTENT"])
            settings["model"] = settings["small_model"] = "opencode-go/" + pick["model"]
            self.credential.environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(settings)
            self.server = self.transport(env=self.credential.environment, cwd=self.worktree)
            verified = verify_provider(self.server.request("GET", "/provider"), self.credential, pick["model"], pick["effort"])
            quota = self.api.probe_opencode(self.config, self.credential.key)
            reserve = max(float(self.config.get("reserves", {}).get("opencode", 15)), attempt.get("reserve_points", 0))
            room = self.api.headroom(quota, reserve, {}, "opencode")
            held = sum(units(row["points"]) for row in self.ledger.read() if row["state"] in ACTIVE
                       and row["pick"]["provider"] == "opencode"
                       and row["quota_ref"] in (attempt["quota_ref"], "opencode:*")) + units(attempt.get("external_points", 0))
            if (quota.get("status") != "ok" or self.api.denied_buckets(quota) or room.get("unknown")
                    or room.get("usable") is None or room["usable"] < 0 or units(room["usable"], capacity=True) < held):
                raise ProtocolError("included quota unavailable before task submission")
            if credentials(self.api, self.config, attempt["account"]).key != self.credential.key:
                raise ProtocolError("credential changed during native setup")
            model = {"id": pick["model"], "providerID": "opencode-go"}
            if pick["effort"]:
                model["variant"] = pick["effort"]
            session = self.server.request("POST", "/session", {"title": "Rightsize managed task", "model": model,
                "permission": self.rules, "metadata": {"rightsize_launch_key": attempt["launch_key"]}})
            session_id = self.check_session(session)
            self.receipt = {**verified, "session_id": session_id, "worktree": str(self.worktree),
                            "pid": self.server.proc.pid, "sandbox": self.sandbox}
            self.ledger.event(attempt["attempt_id"], attempt["launch_key"] + ":prepared", "prepared", receipt=self.receipt)
        except (ProtocolError, OSError, KeyError, TypeError, ValueError) as exc:
            raise NoLaunch("native setup failed before task submission") from exc
        if self.ledger.read(attempt["attempt_id"])["state"] == "cancelling":
            raise NoLaunch("cancelled before task submission")
        body = {"messageID": self.message_id, "model": {"providerID": "opencode-go", "modelID": pick["model"]},
                "agent": "build", "parts": [{"type": "text", "text": self.prompt}]}
        if pick["effort"]:
            body["variant"] = pick["effort"]
        # Crossing this request boundary may run inference. Any failure is
        # uncertain; never replay prompt_async after a missing acknowledgement.
        self.server.request("POST", f"/session/{session_id}/prompt_async", body)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                message = self.server.request("GET", f"/session/{session_id}/message/{self.message_id}", timeout=2)
                self.check_user(message["info"])
                self.receipt = {**self.receipt, "dispatch_id": self.message_id}
                return self.receipt
            except ResponseError as exc:
                if exc.status != 404:
                    raise
                time.sleep(.1)
        raise ProtocolError("native message acknowledgement unavailable")

    def check_user(self, info):
        pick = self.attempt["pick"]
        model = info.get("model", {})
        if (info.get("id") != self.message_id or info.get("sessionID") != self.receipt["session_id"]
                or info.get("role") != "user" or model.get("providerID") != "opencode-go"
                or model.get("modelID") != pick["model"] or model.get("variant") != pick["effort"]):
            raise ProtocolError("native task acknowledgement differs")

    def poll(self, timeout=.2):
        try:
            return self._poll(timeout)
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            raise ProtocolError("native task history unavailable or invalid") from None

    def _poll(self, timeout):
        time.sleep(timeout)
        sid = self.receipt["session_id"]
        rows = self.server.request("GET", f"/session/{sid}/message", timeout=3)
        if not isinstance(rows, list):
            raise ProtocolError("native messages unavailable")
        seen, events, counters, last, failed_tools = set(), [], [], None, set()
        users = 0
        for row in rows:
            info, parts = row["info"], row["parts"]
            mid = native_id(info.get("id"), "msg")
            if mid in seen or info.get("sessionID") != sid:
                raise ProtocolError("ambiguous native message history")
            seen.add(mid)
            if info.get("role") == "user":
                self.check_user(info)
                users += 1
                continue
            if (info.get("role") != "assistant" or info.get("parentID") != self.message_id
                    or info.get("providerID") != "opencode-go" or info.get("modelID") != self.attempt["pick"]["model"]
                    or info.get("variant") != self.attempt["pick"]["effort"]):
                raise ProtocolError("native response belongs to a different model/task")
            # A compaction summary can finish while the user's turn continues.
            if not info.get("summary"):
                last = info
            counters.append(info.get("tokens"))
            for part in parts:
                if part.get("sessionID") != sid or part.get("messageID") != mid:
                    raise ProtocolError("native output identity differs")
                if part.get("type") == "tool" and part.get("state", {}).get("status") == "error":
                    failed_tools.add(native_id(part.get("id"), "prt"))
                if info.get("summary"):
                    continue
                if part.get("type") != "text":
                    continue
                pid = native_id(part.get("id"), "prt")
                text = part.get("text")
                prior = self.texts.get(pid, "")
                if not isinstance(text, str) or not text.startswith(prior):
                    raise ProtocolError("native output changed incompatibly")
                self.texts[pid] = text
                if text[len(prior):]:
                    events.append({"kind": "producing", "text": text[len(prior):]})
        if users != 1:
            raise ProtocolError("native task history is incomplete")
        self.metrics = metrics(counters)
        self.metrics["tool_failures"] = len(failed_tools)
        kind = terminal_kind(last) if last else None
        if kind:
            statuses = self.server.request("GET", "/session/status", timeout=3)
            if not isinstance(statuses, dict):
                raise ProtocolError("native session status unavailable")
            status = statuses.get(sid, {"type": "idle"})
            if status.get("type") in ("busy", "retry"):
                return events
            if status.get("type") != "idle":
                raise ProtocolError("native session status unknown")
            events.append({"kind": kind, "evidence": identity({"session": sid, "message": last["id"],
                "parent": self.message_id, "kind": kind, "completed": last.get("time", {}).get("completed")})})
        return events

    def cancel(self):
        # An abort acknowledgement is not proof the exact task has stopped.
        self.server.request("POST", f"/session/{self.receipt['session_id']}/abort", timeout=3)

    def close(self):
        if self.server:
            self.server.close()
        if self.credential:
            self.credential.environment.pop("RIGHTSIZE_OPENCODE_GO_KEY", None)
            self.credential.key = ""


def metrics(rows):
    result = {key: 0 for key in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_input_tokens", "cache_write_input_tokens")}
    totals = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProtocolError("native usage unavailable")
        values = {"input_tokens": row.get("input"), "output_tokens": row.get("output"),
                  "reasoning_tokens": row.get("reasoning"), "cached_input_tokens": row.get("cache", {}).get("read"),
                  "cache_write_input_tokens": row.get("cache", {}).get("write")}
        for key, value in values.items():
            if type(value) is not int or value < 0:
                raise ProtocolError("invalid native token counter")
            result[key] += value
        if row.get("total") is not None:
            if type(row["total"]) is not int or row["total"] < 0:
                raise ProtocolError("invalid native token total")
            totals.append(row["total"])
    if rows and len(totals) == len(rows):
        result["total_tokens"] = sum(totals)
    return result


def reconcile(ledger, attempt_id, config, *, transport=Server, api=None):
    attempt = ledger.read(attempt_id)
    if not attempt or attempt["state"] not in ACTIVE:
        return {"status": "existing", "attempt": attempt}
    receipt = attempt.get("receipt") or attempt.get("prepared_receipt") or {}
    adapter = None
    try:
        sid = native_id(receipt.get("session_id"), "ses")
        sandbox = receipt["sandbox"]
        adapter = OpenCode(ledger, config, "", receipt["worktree"], transport=transport, sandbox=sandbox, api=api)
        adapter.attempt, adapter.receipt = attempt, receipt
        adapter.message_id = "msg_" + attempt["launch_key"]
        adapter.credential = credentials(adapter.api, config, attempt["account"])
        adapter.server = transport(env=adapter.credential.environment, cwd=adapter.worktree)
        if adapter.check_session(adapter.server.request("GET", f"/session/{sid}")) != sid:
            raise ProtocolError("native recovery session differs")
        events = adapter.poll(0)
        terminal = next((event for event in events if event["kind"] != "producing"), None)
        if not terminal:
            raise ProtocolError("native history has no terminal task outcome")
        if not attempt.get("receipt"):
            ledger.event(attempt_id, attempt["launch_key"] + ":recovered-receipt", "started",
                         receipt={**receipt, "dispatch_id": adapter.message_id})
        ledger.event(attempt_id, attempt["launch_key"] + ":recovered-metrics", "metrics", metrics=adapter.metrics)
        result = ledger.event(attempt_id, attempt["launch_key"] + ":recovered-outcome", terminal["kind"], evidence=terminal["evidence"])
        return {"status": result["attempt"]["state"], "attempt": result["attempt"]}
    except (ProtocolError, OSError, KeyError, TypeError, ValueError):
        current = ledger.read(attempt_id)
        if current["state"] != "reconciling":
            current = ledger.event(attempt_id, attempt["launch_key"] + ":unresolved-recovery", "reconciling",
                                   evidence="native-terminal-evidence-unavailable")["attempt"]
        return {"status": "reconciling", "attempt": current}
    finally:
        if adapter:
            adapter.close()
