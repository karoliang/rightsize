"""Codex app-server adapter: native auth, exact thread/turn identity and events."""

import math
from pathlib import Path
import time
import uuid

import accounts
from managed_ledger import ACTIVE, LedgerError, identity, units
from managed_router import NoLaunch, launch_once
from native_transport import JsonProcess, ProtocolError


def native_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProtocolError("native thread/turn identifier is invalid") from exc


def failure_kind(error):
    code = (error or {}).get("codexErrorInfo") if isinstance(error, dict) else None
    if code in ("usageLimitExceeded", "rateLimitExceeded"):
        return "quota_failed"
    if code == "unauthorized":
        return "auth_failed"
    if code == "sandboxError":
        return "tool_failed"
    return "runtime_failed"


class Codex:
    def __init__(self, ledger, config, prompt, worktree, *, transport=JsonProcess, sandbox="read-only"):
        if sandbox not in ("read-only", "workspace-write"):
            raise LedgerError("unsupported managed sandbox")
        self.ledger, self.config, self.prompt = ledger, config, prompt
        self.worktree = Path(worktree).resolve()
        self.transport = transport
        self.sandbox = sandbox
        self.rpc = None
        self.thread_id = self.turn_id = None
        self.metrics = {}
        self.output_items = set()
        self.turn_requested = False

    def launch(self, attempt):
        binding = accounts.select("codex", self.config)
        if attempt["account"] != binding.public():
            raise NoLaunch("native account changed")
        try:
            self.rpc = self.transport(["codex", "app-server"], env=binding.environment(), cwd=self.worktree)
            self.rpc.request("initialize", {"clientInfo": {"name": "rightsize", "version": "1"}})
            self.rpc.send({"jsonrpc": "2.0", "method": "initialized"})
            auth = self.rpc.request("account/read", {"refreshToken": False})
            if (auth.get("account") or {}).get("type") != "chatgpt":
                raise NoLaunch("subscription quota cannot authorize API billing")
            quota = self.rpc.request("account/rateLimits/read", {})
            if quota.get("ordinaryUsageAllowed") is not True:
                raise NoLaunch("included native usage not confirmed")
            limits = quota.get("rateLimits") or {}
            if not isinstance(limits, dict) or not isinstance(limits.get("primary"), dict):
                raise NoLaunch("native quota measurement unavailable before turn")
            for slot in ("primary", "secondary"):
                bucket = limits.get(slot)
                if bucket and (not isinstance(bucket, dict) or type(bucket.get("usedPercent")) not in (int, float)
                               or not math.isfinite(bucket["usedPercent"])
                               or not 0 <= bucket["usedPercent"] < 100):
                    raise NoLaunch("native quota denied or unavailable before turn")
            expected_quota = attempt["observation"].get("quota_account_ref")
            actual_quota = accounts.digest("codex:" + quota["accountId"]) if quota.get("accountId") else None
            if (not expected_quota or expected_quota != actual_quota
                    or accounts.select("codex", self.config).public() != attempt["account"]):
                raise NoLaunch("native quota identity changed")
            used = max(bucket["usedPercent"] for bucket in (limits.get("primary"), limits.get("secondary")) if bucket)
            held = sum(units(row["points"]) for row in self.ledger.read() if row["state"] in ACTIVE
                       and row["pick"]["provider"] == "codex"
                       and row["quota_ref"] in (attempt["quota_ref"], "codex:*"))
            held += units(attempt.get("external_points", 0))
            reserve = max(float(self.config.get("reserves", {}).get("codex", 10)), attempt.get("reserve_points", 0))
            if units(100 - used, capacity=True) - units(reserve) < held:
                raise NoLaunch("native headroom changed before inference")
            pick = attempt["pick"]
            prepared = self.rpc.request("thread/start", {
                "model": pick["model"], "cwd": str(self.worktree), "sandbox": self.sandbox,
                "approvalPolicy": "on-request", "config": {"model_reasoning_effort": pick["effort"]}})
            self.thread_id = native_id(prepared.get("thread", {}).get("id"))
            sandbox_type = {"read-only": "readOnly", "workspace-write": "workspaceWrite"}[self.sandbox]
            if (prepared.get("model") != pick["model"] or prepared.get("reasoningEffort") != pick["effort"]
                    or prepared.get("modelProvider") != "openai"
                    or (prepared.get("sandbox") or {}).get("type") != sandbox_type
                    or prepared.get("approvalPolicy") != "on-request"
                    or Path(prepared.get("cwd", "")).resolve() != self.worktree):
                raise NoLaunch("native setup differs from admitted selection")
            receipt = {"provider": "codex", "model": prepared["model"], "effort": prepared["reasoningEffort"],
                       "account_ref": binding.account_ref, "fingerprint": binding.fingerprint,
                       "session_id": self.thread_id, "worktree": str(self.worktree), "pid": self.rpc.proc.pid}
            self.ledger.event(attempt["attempt_id"], attempt["launch_key"] + ":prepared", "prepared", receipt=receipt)
        except (OSError, ProtocolError, KeyError, TypeError, ValueError) as exc:
            # No inference request has been sent; native setup is not a task.
            raise NoLaunch("native setup failed before turn request") from exc
        self.turn_requested = True
        result = self.rpc.request("turn/start", {"threadId": self.thread_id,
            "model": pick["model"], "effort": pick["effort"],
            "clientUserMessageId": attempt["launch_key"],
            "input": [{"type": "text", "text": self.prompt}]})
        self.turn_id = native_id(result.get("turn", {}).get("id"))
        return {**receipt, "dispatch_id": self.turn_id}

    def poll(self, timeout=0.2):
        message = self.rpc.notification(timeout)
        if not message:
            return []
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict) or params.get("threadId") != self.thread_id:
            return []
        if params.get("turnId") and params["turnId"] != self.turn_id:
            return []
        if method == "item/agentMessage/delta":
            if not isinstance(params.get("delta"), str):
                raise ProtocolError("malformed native output delta")
            return [{"kind": "producing"}] if params["delta"] else []
        if method == "item/completed":
            item = params.get("item")
            if not isinstance(item, dict):
                raise ProtocolError("malformed native item")
            if item.get("type") == "agentMessage":
                if not isinstance(item.get("text"), str) or not isinstance(item.get("id"), str):
                    raise ProtocolError("malformed native message")
                if item["id"] in self.output_items:
                    return []
                self.output_items.add(item["id"])
                return [{"kind": "producing", "text": item["text"]}]
            if item.get("type") == "commandExecution" and item.get("status") == "failed":
                self.metrics["tool_failures"] = self.metrics.get("tool_failures", 0) + 1
        if method == "thread/tokenUsage/updated":
            usage_data = params.get("tokenUsage")
            usage = usage_data.get("total") if isinstance(usage_data, dict) else None
            if not isinstance(usage, dict):
                raise ProtocolError("native usage is malformed")
            fields = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
                      "cachedInputTokens": "cached_input_tokens", "reasoningOutputTokens": "reasoning_tokens",
                      "totalTokens": "total_tokens"}
            for source, target in fields.items():
                if source in usage:
                    if type(usage[source]) is not int or usage[source] < 0:
                        raise ProtocolError("native usage count is invalid")
                    self.metrics[target] = usage[source]
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict) or turn.get("id") != self.turn_id:
                raise ProtocolError("completion is not bound to the launched turn")
            status = turn.get("status")
            if not isinstance(status, str):
                raise ProtocolError("native turn status is malformed")
            kind = {"completed": "completed", "interrupted": "cancelled", "failed": failure_kind(turn.get("error"))}.get(status)
            if not kind:
                raise ProtocolError("native turn status is unsupported")
            return [{"kind": kind, "evidence": identity({"thread": self.thread_id, "turn": self.turn_id, "status": status})}]
        return []

    def cancel(self):
        if not self.thread_id or not self.turn_id:
            raise ProtocolError("native turn identity is unresolved")
        # An interrupt response alone is not cancellation proof. The runner
        # waits for the matching interrupted terminal notification.
        self.rpc.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, timeout=5)

    def close(self):
        if self.rpc:
            self.rpc.close()


def reconcile(ledger, attempt_id, config, *, transport=JsonProcess):
    """Read native history for this exact thread. Never starts or resumes a turn."""
    attempt = ledger.read(attempt_id)
    if not attempt:
        raise LedgerError("attempt not found")
    if attempt["state"] not in ("launching", "started", "producing", "cancelling", "reconciling"):
        return {"status": "existing", "attempt": attempt}
    receipt = attempt.get("receipt") or attempt.get("prepared_receipt") or {}
    if not receipt.get("session_id"):
        return {"status": "reconciling", "reason": "native session identity unavailable", "attempt": attempt}
    binding = accounts.select("codex", config)
    if binding.account_ref != attempt["account"]["account_ref"]:
        return {"status": "reconciling", "reason": "select the original native context", "attempt": attempt}
    rpc = None
    try:
        rpc = transport(["codex", "app-server"], env=binding.environment())
        rpc.request("initialize", {"clientInfo": {"name": "rightsize", "version": "1"}})
        rpc.send({"jsonrpc": "2.0", "method": "initialized"})
        quota = rpc.request("account/rateLimits/read", {})
        expected = attempt["observation"].get("quota_account_ref")
        actual = accounts.digest("codex:" + quota["accountId"]) if quota.get("accountId") else None
        if expected != actual or (not expected and binding.fingerprint != attempt["account"]["fingerprint"]):
            raise ProtocolError("reconciliation account cannot be verified")
        thread = rpc.request("thread/read", {"threadId": receipt["session_id"], "includeTurns": True}).get("thread")
        if not isinstance(thread, dict) or thread.get("id") != receipt["session_id"]:
            raise ProtocolError("reconciliation returned another thread")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise ProtocolError("native turn history unavailable")
        dispatch = (attempt.get("receipt") or {}).get("dispatch_id")
        if dispatch:
            matches = [turn for turn in turns if isinstance(turn, dict) and turn.get("id") == dispatch]
        else:
            matches = [turn for turn in turns if isinstance(turn, dict) and any(
                isinstance(item, dict) and item.get("type") == "userMessage"
                and isinstance(item.get("id"), str) and item["id"].replace("-", "") == attempt["launch_key"]
                for item in (turn.get("items") or []))]
        if len(matches) != 1:
            raise ProtocolError("native history does not uniquely identify this launch")
        turn = matches[0]
        if not dispatch:
            if thread.get("model") != attempt["pick"]["model"] or thread.get("reasoningEffort") != attempt["pick"]["effort"]:
                raise ProtocolError("recovered model or effort differs")
            bound = {**receipt, "dispatch_id": native_id(turn["id"])}
            ledger.event(attempt_id, attempt["launch_key"] + ":recovered-receipt", "started", receipt=bound)
        status = turn.get("status")
        kind = {"completed": "completed", "interrupted": "cancelled", "failed": failure_kind(turn.get("error"))}.get(status)
        if not kind:
            raise ProtocolError("native turn has no confirmed terminal outcome")
        result = ledger.event(attempt_id, attempt["launch_key"] + ":recovered-outcome", kind,
                              evidence=identity({"thread": receipt["session_id"], "turn": turn["id"], "status": status}))
        return {"status": result["attempt"]["state"], "attempt": result["attempt"]}
    except (ProtocolError, OSError, KeyError, TypeError, ValueError):
        current = ledger.read(attempt_id)
        if current["state"] != "reconciling":
            current = ledger.event(attempt_id, attempt["launch_key"] + ":unresolved-recovery", "reconciling",
                                   evidence="native-terminal-evidence-unavailable")["attempt"]
        return {"status": "reconciling", "reason": "native terminal evidence unavailable", "attempt": current}
    finally:
        if rpc:
            rpc.close()


def run(ledger, attempt_id, adapter, *, timeout=300, output=None):
    """One foreground owner consumes native events and observes cancellation requests."""
    if not 1 <= timeout <= 3600:
        raise LedgerError("native timeout must be between 1 and 3600 seconds")
    event_number, output_bytes = 0, 0
    deadline = time.monotonic() + timeout
    renewed = time.monotonic()
    cancelling = False
    terminal = False
    try:
        result = launch_once(ledger, attempt_id, adapter.launch)
        if result["status"] not in ("started", "cancelling"):
            return result
        attempt = result["attempt"]
        while True:
            current = ledger.read(attempt_id)
            if (current["state"] == "cancelling" or time.monotonic() >= deadline) and not cancelling:
                ledger.event(attempt_id, attempt["launch_key"] + ":cancel-request", "cancelling")
                cancelling = True
                deadline = time.monotonic() + 10
                adapter.cancel()
            if cancelling and time.monotonic() >= deadline:
                raise ProtocolError("native cancellation could not be confirmed")
            if time.monotonic() - renewed >= 10:
                ledger.renew(attempt_id, attempt["launch_key"])
                renewed = time.monotonic()
            for event in adapter.poll():
                event_number += 1
                kind = event["kind"]
                if kind == "producing":
                    if cancelling:
                        continue
                    if event.get("text"):
                        output_bytes += len(event["text"].encode())
                        if output_bytes > 1024 * 1024:
                            raise ProtocolError("native response exceeds artifact budget")
                        if output:
                            output(event["text"])
                    if current.get("first_output_at"):
                        continue
                else:
                    ledger.event(attempt_id, attempt["launch_key"] + ":metrics", "metrics",
                                 metrics={**adapter.metrics, "output_bytes": output_bytes})
                    terminal = True
                result = ledger.event(attempt_id, f"{attempt['launch_key']}:event:{event_number}", kind,
                                      evidence=event.get("evidence"))
                if terminal:
                    return {"status": result["attempt"]["state"], "attempt": result["attempt"]}
    except (ProtocolError, OSError, KeyboardInterrupt):
        current = ledger.read(attempt_id)
        if current and current["state"] in ("launching", "started", "producing", "cancelling", "reconciling"):
            result = ledger.event(attempt_id, current["launch_key"] + ":disconnected", "reconciling",
                                  evidence="native-stream-or-cancellation-unconfirmed")
            return {"status": "reconciling", "attempt": result["attempt"]}
        raise
    finally:
        adapter.close()
