"""Claude Code's native stream-json control channel, without token extraction."""

from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

import accounts
from managed_ledger import ACTIVE, LedgerError, identity as evidence_hash, units
from managed_router import NoLaunch

from native_transport import JsonProcess, ProtocolError


class Control(JsonProcess):
    def request(self, subtype, params=None, *, timeout=15):
        request_id = "rightsize-" + str(self.next_id)
        self.next_id += 1
        self.send({"type": "control_request", "request_id": request_id,
                   "request": {**(params or {}), "subtype": subtype}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.poll(min(0.2, max(0, deadline - time.monotonic())))
            if message is None:
                continue
            if message.get("type") == "control_response":
                response = message.get("response")
                if not isinstance(response, dict):
                    raise ProtocolError("malformed native control response")
                if response.get("request_id") == request_id:
                    if response.get("subtype") != "success":
                        raise ProtocolError("native control request rejected")
                    result = response.get("response")
                    if not isinstance(result, dict):
                        raise ProtocolError("native control response must be an object")
                    return result
            if message.get("type") == "control_request":
                self.answer_request(message)
            else:
                self.notifications.append(message)
        raise ProtocolError("native control request deadline exceeded")

    def answer_request(self, message):
        request = message.get("request")
        request_id = message.get("request_id")
        if not isinstance(request, dict) or not isinstance(request_id, str):
            raise ProtocolError("malformed native control request")
        if request.get("subtype") == "can_use_tool":
            response = {"subtype": "success", "request_id": request_id,
                        "response": {"behavior": "deny", "message": "Managed execution cannot grant interactive permissions"}}
        else:
            response = {"subtype": "error", "request_id": request_id,
                        "error": "Unsupported managed control request"}
        self.send({"type": "control_response", "response": response})

    def notification(self, timeout=0.2):
        message = self.notifications.popleft() if self.notifications else self.poll(timeout)
        if message and message.get("type") == "control_request":
            self.answer_request(message)
            return None
        return message


def account_matches(account, identity):
    if not isinstance(account, dict) or account.get("apiProvider") != "firstParty":
        return False
    values = [account.get("email"), account.get("organization")]
    return (all(isinstance(v, str) and v for v in values)
            and accounts.digest(json.dumps(["claude", *values])) == identity.get("session_account_ref"))


def quota(usage):
    """Allowlisted subscription windows only; never turn usage credits into room.

    get_usage is experimental. The SDK contract marks model_scoped present only
    for endpoint answers, absent for cached/unknown data. Missing freshness proof
    is unavailable; it cannot clear a previously observed denial.
    """
    if not isinstance(usage, dict):
        raise ProtocolError("malformed native usage response")
    limits = usage.get("rate_limits")
    if usage.get("rate_limits_available") is not True or not isinstance(limits, dict):
        return {"status": "unknown", "buckets": []}
    fresh = isinstance(limits.get("model_scoped"), list)
    buckets = []
    invalid = False

    def add(name, row):
        nonlocal invalid
        if not isinstance(row, dict):
            invalid = True
            return
        value = row.get("utilization")
        valid = type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100
        denied = bool(row.get("locked_reason")) or valid and value >= 100
        reset = None
        raw_reset = row.get("resets_at")
        if raw_reset is not None:
            try:
                stamp = datetime.fromisoformat(raw_reset.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError()
                reset = stamp.timestamp()
                if not math.isfinite(reset):
                    raise ValueError()
            except (AttributeError, TypeError, ValueError, OverflowError):
                invalid = True
        invalid = invalid or not valid
        buckets.append({"id": name, "percent": value if valid else None,
                        "source": "live" if fresh and valid else "unavailable", "resets_at": reset,
                        "raw_status": "quota-exceeded" if denied else "ok"})

    for name in ("five_hour", "seven_day"):
        add({"five_hour": "rolling-300m", "seven_day": "weekly"}[name], limits.get(name))
    # Preserve every additional returned utilization window conservatively,
    # including model-specific restrictions, instead of guessing applicability.
    for name, row in limits.items():
        if name not in ("five_hour", "seven_day", "extra_usage", "spend") and isinstance(row, dict) and "utilization" in row:
            add("native-" + accounts.digest(name)[:16], row)
    if fresh:
        for row in limits["model_scoped"]:
            if not isinstance(row, dict) or not isinstance(row.get("display_name"), str):
                invalid = True
                continue
            add("model-" + accounts.digest(row["display_name"])[:16], row)
    if "limits" in limits:
        rows = limits["limits"]
        if not isinstance(rows, list):
            invalid = True
        else:
            for row in rows:
                if (not isinstance(row, dict) or type(row.get("is_active")) is not bool
                        or not isinstance(row.get("kind"), str) or not isinstance(row.get("group"), str)):
                    invalid = True
                    continue
                if row["is_active"]:
                    key = json.dumps([row["kind"], row["group"], row.get("scope")], sort_keys=True)
                    add("limit-" + accounts.digest(key)[:16], {**row, "utilization": row.get("percent")})
    denied = any(row["raw_status"] == "quota-exceeded" for row in buckets)
    extra = limits.get("extra_usage")
    status = "denied" if denied else "ok" if fresh and not invalid else "unknown"
    if status == "unknown":
        # Advisory policy also consumes these buckets. Do not leave a numeric
        # cached/partial percentage looking like ordinary usable headroom.
        buckets = [{**row, "percent": None, "source": "unknown-native"} for row in buckets]
    return {"status": status,
            "buckets": buckets, "quota_source": "native-control",
            "usage_credits_enabled": extra.get("is_enabled") if isinstance(extra, dict) and type(extra.get("is_enabled")) is bool else None}


def probe(binding, identity, *, transport=Control):
    """No user prompt, auth extraction or custom startup hooks in quota discovery.

    Safe mode retains native login and administrator policy, while disabling
    user/project customizations. No native process is started without the two
    native identity proofs available. Unsupported control returns None so the
    caller can retain its explicitly labelled legacy estimate.
    """
    if not identity.get("session_account_ref"):
        return None
    rpc = None
    try:
        with tempfile.TemporaryDirectory(prefix="rightsize-claude-probe-") as cwd:
            rpc = transport(["claude", "--safe-mode", "-p", "--input-format", "stream-json",
                             "--output-format", "stream-json", "--verbose", "--tools", "",
                             "--permission-mode", "manual", "--permission-prompts", "none",
                             "--no-session-persistence"],
                            env=binding.environment(), cwd=cwd)
            try:
                initialized = rpc.request("initialize", {"hooks": None})
                if not account_matches(initialized.get("account"), identity):
                    return {"status": "account-changed", "buckets": []}
                return quota(rpc.request("get_usage", {"skip_behaviors": True}))
            finally:
                rpc.close()
    except (OSError, ProtocolError):
        return None


def terminal_kind(message):
    if message.get("terminal_reason") in ("aborted_streaming", "aborted_tools"):
        return "cancelled"
    if message.get("api_error_status") in (401, 403):
        return "auth_failed"
    if message.get("api_error_status") == 429:
        return "quota_failed"
    if message.get("is_error") is False and message.get("subtype") == "success":
        return "completed"
    if message.get("is_error") is True:
        return "tool_failed" if message.get("permission_denials") else "runtime_failed"
    raise ProtocolError("native result lacks a terminal outcome")


def metrics_from(message):
    rows = message.get("modelUsage")
    if not isinstance(rows, dict):
        raise ProtocolError("native model usage is unavailable")
    fields = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
              "cacheReadInputTokens": "cached_input_tokens", "cacheCreationInputTokens": "cache_write_input_tokens"}
    result = {field: 0 for field in fields.values()}
    for row in rows.values():
        if not isinstance(row, dict):
            raise ProtocolError("native model usage is malformed")
        for source, target in fields.items():
            value = row.get(source)
            if type(value) is not int or value < 0:
                raise ProtocolError("native token count is invalid")
            result[target] += value
    # Claude's four categories are disjoint; its input excludes cache reads/writes.
    result["total_tokens"] = sum(result.values())
    denials = message.get("permission_denials", [])
    if not isinstance(denials, list):
        raise ProtocolError("native permission result is malformed")
    result["tool_failures"] = len(denials)
    return result


def write_outcome(path, record):
    """Persist bounded terminal metadata before settling its ledger event."""
    payload = {**record, "sha256": evidence_hash(record)}
    raw = json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    if len(raw) > 65536:
        raise ProtocolError("native outcome metadata exceeds budget")
    fd, name = tempfile.mkstemp(prefix=".native-outcome-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Claude:
    def __init__(self, ledger, config, prompt, worktree, *, transport=Control, sandbox="read-only"):
        if sandbox not in ("read-only", "workspace-write"):
            raise LedgerError("unsupported managed permissions")
        self.ledger, self.config, self.prompt = ledger, config, prompt
        self.worktree = Path(worktree).resolve()
        self.transport, self.sandbox = transport, sandbox
        self.rpc = None
        self.metrics, self.output_items = {}, set()
        self.session_id = str(uuid.uuid4())
        self.receipt = None
        self.outcome_path = self.worktree.parent / "native-outcome.json"

    def launch(self, attempt):
        self.attempt = attempt
        self.message_id = str(uuid.UUID(attempt["launch_key"]))
        binding = accounts.select("claude", self.config)
        observed = attempt["observation"]
        native = accounts.claude_identity(binding)
        if (binding.public() != attempt["account"] or native.get("status") != "ok"
                or not native.get("quota_account_ref")
                or any(native.get(key) != observed.get(key) for key in ("quota_account_ref", "session_account_ref"))):
            raise NoLaunch("native subscription account changed")
        pick = attempt["pick"]
        mode = "plan" if self.sandbox == "read-only" else "acceptEdits"
        argv = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--replay-user-messages", "--session-id", self.session_id, "--model", pick["model"],
                "--permission-mode", mode, "--permission-prompts", "none"]
        if pick["effort"]:
            argv += ["--effort", pick["effort"]]
        if self.sandbox == "read-only":
            argv += ["--tools", "Read,Glob,Grep", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        try:
            self.rpc = self.transport(argv, env=binding.environment(), cwd=self.worktree)
            setup = self.rpc.request("initialize", {"hooks": None})
            if (not account_matches(setup.get("account"), native)
                    or setup.get("current_permission_mode") != mode or setup.get("fast_mode_state") != "off"):
                raise NoLaunch("native account, permissions or billing mode differs")
            applied = self.rpc.request("get_settings").get("applied")
            if (not isinstance(applied, dict) or applied.get("model") != pick["model"]
                    or applied.get("effort") != pick["effort"] or applied.get("advisor") is not None
                    or applied.get("ultracode") is not False):
                raise NoLaunch("effective native model/effort differs from admission")
            reading = quota(self.rpc.request("get_usage", {"skip_behaviors": True}))
            if reading["status"] != "ok" or reading.get("usage_credits_enabled") is not False:
                raise NoLaunch("included quota without usage-credit fallback is not confirmed")
            if (accounts.claude_identity(binding) != native
                    or accounts.select("claude", self.config).public() != attempt["account"]):
                raise NoLaunch("native account changed during setup")
            held = sum(units(row["points"]) for row in self.ledger.read() if row["state"] in ACTIVE
                       and row["pick"]["provider"] == "claude"
                       and row["quota_ref"] in (attempt["quota_ref"], "claude:*")) + units(attempt.get("external_points", 0))
            reserve = max(float(self.config.get("reserves", {}).get("claude", 10)), attempt.get("reserve_points", 0))
            if min(units(100 - b["percent"], capacity=True) for b in reading["buckets"]) - units(reserve) < held:
                raise NoLaunch("native headroom changed before inference")
            self.receipt = {"provider": "claude", "model": applied["model"], "effort": applied["effort"],
                            "account_ref": binding.account_ref, "fingerprint": binding.fingerprint,
                            "session_id": self.session_id, "worktree": str(self.worktree), "pid": self.rpc.proc.pid}
            self.ledger.event(attempt["attempt_id"], attempt["launch_key"] + ":prepared", "prepared", receipt=self.receipt)
        except (OSError, ProtocolError, KeyError, TypeError, ValueError) as exc:
            raise NoLaunch("native setup failed before task submission") from exc
        # Crossing this boundary may launch inference. Any missing acknowledgement
        # is uncertain and must never be converted into a confirmed no-launch.
        if self.ledger.read(attempt["attempt_id"])["state"] == "cancelling":
            raise NoLaunch("cancelled before task submission")
        self.rpc.send({"type": "user", "uuid": self.message_id, "session_id": self.session_id,
                       "parent_tool_use_id": None, "client_composed": True,
                       "message": {"role": "user", "content": self.prompt}})
        initialized = acknowledged = False
        pending = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            message = self.rpc.notification(0.2)
            if not message:
                continue
            if message.get("session_id") != self.session_id:
                continue
            if message.get("type") == "system" and message.get("subtype") == "init":
                if (message.get("model") != pick["model"] or message.get("permissionMode") != mode
                        or message.get("apiKeySource") != "none"
                        or Path(message.get("cwd", "")).resolve() != self.worktree):
                    raise ProtocolError("native session differs from verified setup")
                initialized = True
            elif message.get("type") == "user" and message.get("uuid") == self.message_id:
                acknowledged = True
            elif message.get("user_message_uuid") == self.message_id:
                acknowledged = True
                pending.append(message)
            else:
                pending.append(message)
            if initialized and acknowledged:
                self.rpc.notifications.extendleft(reversed(pending))
                self.receipt = {**self.receipt, "dispatch_id": self.message_id}
                return self.receipt
        raise ProtocolError("native task acknowledgement unavailable")

    def poll(self, timeout=0.2):
        message = self.rpc.notification(timeout)
        if not message or message.get("session_id") != self.session_id or message.get("parent_tool_use_id"):
            return []
        if message.get("type") == "assistant":
            value = message.get("message")
            if not isinstance(value, dict) or value.get("model") != self.receipt["model"]:
                raise ProtocolError("native response model differs from admission")
            identifier = message.get("uuid")
            if not isinstance(identifier, str):
                raise ProtocolError("native message identifier missing")
            if identifier in self.output_items:
                return []
            self.output_items.add(identifier)
            content = value.get("content")
            if not isinstance(content, list):
                raise ProtocolError("native message content malformed")
            text = "".join(item["text"] for item in content if isinstance(item, dict)
                           and item.get("type") == "text" and isinstance(item.get("text"), str))
            return [{"kind": "producing", "text": text}] if text else []
        if message.get("type") != "result":
            return []
        echoed = message.get("user_message_uuids", [message.get("user_message_uuid")])
        if (echoed != [self.message_id] or message.get("queued_turn_count", 0) != 0
                or message.get("resume_reason")):
            raise ProtocolError("native outcome is not bound to the single submitted task")
        kind = terminal_kind(message)
        self.metrics = metrics_from(message)
        evidence = evidence_hash({k: message.get(k) for k in ("session_id", "uuid", "user_message_uuid",
                                 "terminal_reason", "subtype", "is_error", "api_error_status")})
        write_outcome(self.outcome_path, {"schema_version": 1, "attempt_id": self.attempt["attempt_id"],
                      "launch_key": self.attempt["launch_key"], "receipt": self.receipt,
                      "kind": kind, "evidence": evidence, "metrics": self.metrics})
        return [{"kind": kind, "evidence": evidence}]

    def cancel(self):
        # An interrupt acknowledgement alone is not cancellation completion.
        self.rpc.request("interrupt", {"cancel_queued": True})

    def close(self):
        if self.rpc:
            self.rpc.close()


def reconcile(ledger, attempt_id, config):
    """Recover durable native terminal evidence without resuming or inferring."""
    attempt = ledger.read(attempt_id)
    if not attempt or attempt["state"] not in ACTIVE:
        return {"status": "existing", "attempt": attempt}
    receipt = attempt.get("receipt") or attempt.get("prepared_receipt") or {}
    path = ledger.path.parent / "executions" / attempt_id / "native-outcome.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ProtocolError("outcome must be a regular file")
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ProtocolError("outcome exceeds metadata budget")
        record = json.loads(raw)
        digest = record.pop("sha256")
        expected = {**receipt, "dispatch_id": str(uuid.UUID(attempt["launch_key"]))}
        if (digest != evidence_hash(record) or type(record["schema_version"]) is not int or record["schema_version"] != 1
                or record["attempt_id"] != attempt_id or record["launch_key"] != attempt["launch_key"]
                or record["receipt"] != expected or record["kind"] not in
                ("completed", "cancelled", "quota_failed", "auth_failed", "tool_failed", "runtime_failed")):
            raise ProtocolError("outcome does not match owned launch")
        if not attempt.get("receipt"):
            ledger.event(attempt_id, attempt["launch_key"] + ":recovered-receipt", "started", receipt=expected)
        ledger.event(attempt_id, attempt["launch_key"] + ":recovered-metrics", "metrics", metrics=record["metrics"])
        result = ledger.event(attempt_id, attempt["launch_key"] + ":recovered-outcome", record["kind"], evidence=record["evidence"])
        return {"status": result["attempt"]["state"], "attempt": result["attempt"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        current = ledger.read(attempt_id)
        if current["state"] != "reconciling":
            current = ledger.event(attempt_id, attempt["launch_key"] + ":unresolved-recovery", "reconciling",
                                   evidence="native-terminal-evidence-unavailable")["attempt"]
        return {"status": "reconciling", "reason": "native terminal evidence unavailable; no relaunch", "attempt": current}
