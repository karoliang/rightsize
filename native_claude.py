"""Claude Code's native stream-json control channel, without token extraction."""

from datetime import datetime
import json
import math
import tempfile
import time

import accounts

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
