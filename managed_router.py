"""Managed routing orchestration. Native launch adapters plug into launch_once."""

import hashlib
import json
import math
from pathlib import Path
import time

import accounts
from managed_ledger import ACTIVE, Ledger, LedgerError, identity, wait


class NoLaunch(Exception):
    """Adapter has positive proof that no native launch occurred."""


def launch_once(ledger, attempt_id, launcher):
    """Persist the launch claim before calling an adapter; uncertainty holds capacity."""
    attempt = ledger.read(attempt_id)
    if not attempt:
        raise LedgerError("attempt not found")
    if attempt["state"] != "admitted":
        return {"status": "existing", "attempt": attempt}
    claim = ledger.event(attempt_id, attempt["launch_key"] + ":claim", "launching")
    if not claim["applied"]:
        return {"status": "existing", "attempt": claim["attempt"]}
    try:
        receipt = launcher(claim["attempt"])
    except NoLaunch:
        result = ledger.event(attempt_id, attempt["launch_key"] + ":no-launch", "launch_failed",
                              evidence="adapter-confirmed-no-launch")
    except Exception:
        result = ledger.event(attempt_id, attempt["launch_key"] + ":unknown", "reconciling",
                              evidence="adapter-launch-result-unknown")
    else:
        # Malformed receipts are uncertain too: a process may already exist.
        try:
            result = ledger.event(attempt_id, attempt["launch_key"] + ":receipt", "started", receipt=receipt)
        except (LedgerError, TypeError, ValueError):
            result = ledger.event(attempt_id, attempt["launch_key"] + ":unknown", "reconciling",
                                  evidence="adapter-receipt-unavailable")
    return {"status": result["attempt"]["state"], "attempt": result["attempt"]}


def ledger_path(api):
    return api.STATE.with_name("managed.sqlite3")


def legacy_state(api):
    """Admission must never reinterpret corrupt legacy commitments as empty."""
    if not api.STATE.exists():
        return {}
    try:
        state = json.loads(api.STATE.read_text())
        if not isinstance(state, dict) or not isinstance(state.get("reservations", []), list):
            raise ValueError()
        for hold in state.get("reservations", []):
            if (not isinstance(hold, dict) or not isinstance(hold.get("provider"), str)
                    or any(isinstance(hold.get(key), bool) or not isinstance(hold.get(key), (int, float))
                           or not math.isfinite(hold[key]) or hold[key] < 0 for key in ("points", "expires"))):
                raise ValueError()
        for key in ("quota_denials", "exhausted", "snapshots"):
            if not isinstance(state.get(key, {}), dict):
                raise ValueError()
        return state
    except (ValueError, OSError) as exc:
        raise LedgerError("legacy state is corrupt or unavailable; reconcile before admission") from exc


def external_load(state, provider, stamp):
    holds = [hold for hold in state.get("reservations", [])
             if hold["provider"] == provider and hold["expires"] > stamp]
    return sum(hold["points"] for hold in holds), len(holds)


def external_block(api, state, provider, probe, stamp):
    denials = state.get("quota_denials", {})
    if denials.get(provider) or denials.get(api.probe_scope(provider, probe)):
        return "legacy quota denial requires reconciliation"
    if state.get("exhausted", {}).get(provider, 0) > stamp:
        return "legacy quota cooldown active"
    return None


def eligibility(api, config, probes, state, attempts, *, stamp=None):
    """Read-only policy inputs. The ledger rechecks commitments at admission."""
    stamp = time.time() if stamp is None else stamp
    result = {}
    for provider, probe in probes.items():
        info = api.headroom(probe, float(config.get("reserves", {}).get(provider, 10)),
                            state.get("snapshots", {}), api.probe_scope(provider, probe))
        account = probe.get("account") or {}
        held = [a for a in attempts if a["state"] in ACTIVE and
                a["account"]["account_ref"] == account.get("account_ref")]
        points, slots = external_load(state, provider, stamp)
        points += sum(a["points"] for a in held)
        slots += len(held)
        if info["usable"] is not None:
            info["usable"] -= points
        blocked = external_block(api, state, provider, probe, stamp)
        if api.denied_buckets(probe) or probe.get("status") == "denied":
            blocked = "quota denied"
        elif probe.get("status") != "ok" or not account:
            blocked = "fresh account-bound quota unavailable"
        elif account.get("source") == "scoped-vault":
            blocked = "vault launch binding requires managed credential adapter"
        elif provider == "codex" and not probe.get("quota_account_ref"):
            blocked = "native quota account identity unavailable"
        elif info["usable"] is None or info.get("unknown"):
            blocked = "managed admission requires known quota"
        limit = int(config.get("max_inflight", {}).get(provider, config.get("max_inflight", {}).get("_default", 8)))
        if slots >= limit:
            blocked = "account slot limit reached"
        result[provider] = {**info, "account": account, "provider": provider,
                            "free": False, "inflight": slots, "reserved": points,
                            "eligible": not blocked, "blocked": blocked, "hard_blocked": blocked,
                            "buckets": probe.get("buckets", [])}
    return result


def decision_at_floor(api, judgment, config, elig, floor):
    decision = api.decide(judgment, config, elig, fallback=False, floor_band=floor)
    if not decision["pick"] and not decision["blocked"]:
        # Reuse the existing single-task policy's forecast-only relaxation,
        # without its lower/higher-band fallbacks. Measured burn still vetoes.
        decision = api.decide(judgment, config, elig, fallback=False, floor_band=floor, relax_pace=True)
        if decision["pick"]:
            decision["reasons"].append("kept capability band; only whole-window forecast pacing was relaxed")
    return decision


def execute(args, config, api):
    """Caller judgment validation precedes probes or any durable writes."""
    with Path(args.spec).open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise LedgerError("task exceeds 1 MiB")
    spec = raw.decode("utf-8")
    judgment = api.load_judgment(Path(args.judgment), spec)
    from context_manifest import for_execution
    context_hash, _ = for_execution(args, spec)
    floor = max(api.band_for(judgment, config)[0], args.floor)
    task = {"task_id": args.task_id, "spec_hash": hashlib.sha256(raw).hexdigest(),
            "context_hash": context_hash, "judgment_source": judgment["source"], "floor": floor}
    ledger = Ledger(ledger_path(api))
    state = legacy_state(api)
    attempts = ledger.read()
    if args.action == "admit":
        for attempt in attempts:
            if attempt["request_key"] == args.request_id:
                if any(attempt[key] != value for key, value in task.items()):
                    raise LedgerError("idempotency key already belongs to different task intent")
                return {"status": "existing", "attempt": attempt}
    probes = api.probe_all(config)
    if args.action == "plan":
        elig = eligibility(api, config, probes, state, attempts)
        decision = decision_at_floor(api, judgment, config, elig, floor)
        return {"status": "planned" if decision["pick"] and not decision["blocked"] else "wait",
                "task": task, "decision": decision, "lease_created": False}
    for refresh in range(2):
        result = admit_snapshot(args, config, api, ledger, task, judgment, floor, probes)
        if not result.get("reprobe") or refresh == 1:
            return result
        # The compatibility/ledger locks are released before network work.
        probes = api.probe_all(config)
    return result


def admit_snapshot(args, config, api, ledger, task, judgment, floor, probes):
    # Legacy compatibility lock precedes the SQLite transaction. Native probes
    # were already fetched, and local account metadata is the only callback.
    with api.state_lock():
        state = legacy_state(api)
        attempts = ledger.read()
        elig = eligibility(api, config, probes, state, attempts)
        failures = {}
        for _ in range(len(elig)):
            decision = decision_at_floor(api, judgment, config, elig, floor)
            if decision["blocked"] or not decision["pick"] or decision["confirm_first"]:
                return wait(decision["blocked"] or ("destructive task requires explicit approval" if decision["confirm_first"]
                                                   else "no eligible account at capability floor"),
                            decision=decision, accounts=failures)
            pick = decision["pick"]
            provider = pick["provider"]
            points, slots = external_load(state, provider, time.time())
            result = ledger.admit(
                request_key=args.request_id, task=task, pick={**pick, "band": decision["band"]},
                observation=probes[provider], points=api.dispatch_cost(config, provider, decision["band"]),
                slot_limit=int(config.get("max_inflight", {}).get(provider, config.get("max_inflight", {}).get("_default", 8))),
                binding_now=lambda: accounts.select(provider, config).public(),
                reserve=float(config.get("reserves", {}).get(provider, 10)),
                external_points=points, external_slots=slots,
                external_block=external_block(api, state, provider, probes[provider], time.time()))
            if result["status"] != "wait":
                return result
            if result.get("reprobe"):
                return result
            failures[provider] = result["reason"]
            elig[provider] = {**elig[provider], "eligible": False, "blocked": result["reason"]}
        return wait("all eligible accounts unavailable", accounts=failures)
