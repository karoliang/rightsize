"""Offline policy comparison from explicit snapshots; no native or state access."""

import copy
import hashlib
import json
import math
from pathlib import Path
import re
import types

import managed_router

BASELINE = "a168a85055a3f1fa527da593fced87cf37c10892"
BASELINE_SOURCE = "7d5fa7483fab5924f119fc35a4f253b634d70a3b623d1fef393da62ce6dd8ff8"
PURE = ("headroom", "bucket_pace", "bucket_window", "denied_buckets", "probe_scope",
        "decide", "band_for", "pick", "parse_candidate", "effort_for", "dispatch_cost",
        "admission_block", "human_reset", "qualified_candidates")


class ReplayError(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError("duplicate snapshot field")
        result[key] = value
    return result


def read(path):
    with Path(path).open("rb") as handle:
        data = handle.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ReplayError("snapshot exceeds 4 MiB")
    try:
        return json.loads(data, object_pairs_hook=object_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ReplayError("nonfinite snapshot number")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReplayError("invalid snapshot JSON") from exc


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def hash_value(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def policy(api, stamp):
    """Bind existing pure policy functions to a private, frozen clock namespace.

    No monkey-patching of the live router and no alternative policy implementation.
    Only constants and the explicitly listed functions are available to calls.
    """
    namespace = {"__builtins__": __builtins__, "now": lambda: stamp,
                 "STALE_READING": api.STALE_READING, "BUCKET_WINDOWS": api.BUCKET_WINDOWS,
                 "EFFORTS": api.EFFORTS, "re": re}
    for name in PURE:
        if hasattr(api, name):
            fn = getattr(api, name)
            namespace[name] = types.FunctionType(fn.__code__, namespace, name, fn.__defaults__, fn.__closure__)
    return types.SimpleNamespace(**namespace)


def validate(case):
    required = {"id", "at", "task_sha256", "context_sha256", "judgment", "floor",
                "config", "probes", "legacy", "attempts"}
    if not isinstance(case, dict) or set(case) != required:
        raise ReplayError("snapshot fields do not match version 1")
    if (not isinstance(case["id"], str) or not re.fullmatch(r"[a-z0-9-]{1,80}", case["id"])
            or not finite(case["at"]) or case["at"] <= 0 or not hash_value(case["task_sha256"])
            or not (case["context_sha256"] == "none" or hash_value(case["context_sha256"]))
            or type(case["floor"]) is not int or case["floor"] not in (1, 2, 3)):
        raise ReplayError("invalid snapshot identity or clock")
    judgment = case["judgment"]
    if (not isinstance(judgment, dict) or set(judgment) !=
            {"tier", "size", "second_opinion", "spec_complete", "destructive"}
            or judgment["tier"] not in ("mechanical", "implementation", "design", "diagnosis", "high_stakes")):
        raise ReplayError("invalid snapshot judgment")
    for name, maximum in (("size", 2), ("second_opinion", 1), ("spec_complete", 1), ("destructive", 1)):
        if not finite(judgment[name]) or not 0 <= judgment[name] <= maximum:
            raise ReplayError("invalid snapshot judgment score")
    config = case["config"]
    allowed = {"bands", "agents", "review_ladder", "thresholds", "reserves", "max_inflight",
               "dispatch_cost", "effort", "expensive_band", "task_profiles", "model_profiles"}
    if not isinstance(config, dict) or set(config) - allowed or not {"bands", "agents", "review_ladder"} <= set(config):
        raise ReplayError("snapshot config must contain policy fields only")
    if not isinstance(case["probes"], dict) or not isinstance(case["legacy"], dict) or not isinstance(case["attempts"], list):
        raise ReplayError("invalid snapshot records")
    # Serialize now to reject nonfinite nested values and unsupported objects.
    encode(case)


def verdict(decision):
    selected = bool(decision["pick"] and not decision["blocked"] and not decision["confirm_first"])
    return {"status": "recommend" if selected else "wait", "band": decision["band"],
            "pick": decision["pick"] if selected else None,
            "account_sha256": digest(decision["account"]) if selected and decision.get("account") else None}


def validate_verdict(value):
    if (not isinstance(value, dict) or set(value) != {"status", "band", "pick", "account_sha256"}
            or value["status"] not in ("recommend", "wait")
            or type(value["band"]) is not int or value["band"] not in (1, 2, 3)
            or value["account_sha256"] is not None and not hash_value(value["account_sha256"])):
        raise ReplayError("invalid recorded verdict")
    pick = value["pick"]
    if value["status"] == "wait":
        if pick is not None or value["account_sha256"] is not None:
            raise ReplayError("wait verdict cannot carry a selection")
    elif (not isinstance(pick, dict) or set(pick) != {"provider", "model", "effort"}
          or not isinstance(pick["provider"], str) or not re.fullmatch(r"[a-z_]{1,32}", pick["provider"])
          or not isinstance(pick["model"], str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", pick["model"])
          or pick["effort"] not in (None, "low", "medium", "high", "xhigh", "max", "ultra")):
        raise ReplayError("invalid recorded selection")


def coverage(case):
    """Missing receipt/review evidence remains in the denominator."""
    result = {"attempts": len(case["attempts"]), "exact_receipts": 0, "accepted": 0,
              "completed_unreviewed": 0, "unresolved": 0,
              "legacy_holds": len(case["legacy"].get("reservations", []))}
    for attempt in case["attempts"]:
        receipt = attempt.get("receipt") or {}
        pick, account = attempt.get("pick") or {}, attempt.get("account") or {}
        exact = bool(all(isinstance(receipt.get(k), str) and receipt[k] for k in
                         ("session_id", "dispatch_id", "provider", "model", "account_ref", "fingerprint"))
                     and all(receipt.get(k) == pick.get(k) for k in ("provider", "model", "effort"))
                     and all(receipt.get(k) == account.get(k) for k in ("account_ref", "fingerprint"))
                     and hash_value(attempt.get("spec_hash"))
                     and (attempt.get("context_hash") == "none" or hash_value(attempt.get("context_hash"))))
        result["exact_receipts"] += int(exact)
        result["accepted"] += int(exact and attempt.get("state") == "accepted"
                                  and isinstance(attempt.get("review_evidence"), str) and bool(attempt["review_evidence"]))
        result["completed_unreviewed"] += int(attempt.get("state") == "completed")
        result["unresolved"] += int(not exact or attempt.get("state") in ("reconciling", "launching", "cancelling"))
    result["accepted_per_attempt"] = result["accepted"] / result["attempts"] if result["attempts"] else None
    return result


def shadow(case, api):
    validate(case)
    frozen = policy(api, case["at"])
    # Work on copies so a policy change cannot mutate caller evidence.
    config, probes, legacy, attempts = copy.deepcopy([case[k] for k in ("config", "probes", "legacy", "attempts")])
    elig = managed_router.eligibility(frozen, config, probes, legacy, attempts, stamp=case["at"])
    floor = max(case["floor"], frozen.band_for(case["judgment"], config)[0])
    decision = managed_router.decision_at_floor(frozen, case["judgment"], config, elig, floor)
    actual = verdict(decision)
    validate_verdict(actual)
    picked = actual["pick"]
    denied = bool(picked and (probes[picked["provider"]].get("status") == "denied"
                             or frozen.denied_buckets(probes[picked["provider"]])
                             or managed_router.external_block(frozen, legacy, picked["provider"],
                                                              probes[picked["provider"]], case["at"])))
    return {"id": case["id"], "snapshot_sha256": digest(case), "task_sha256": case["task_sha256"],
            "context_sha256": case["context_sha256"], "candidate": actual,
            "known_denied_recommendation": denied, "floor_violation": bool(picked and actual["band"] < floor),
            "coverage": coverage(case), "launches": 0, "reservations": 0,
            "scope": "policy recommendation only; not transactional admission or live quality proof"}


def run(document, api, *, compare=False):
    fields = {"schema_version", "cases"} | ({"baseline"} if compare else set())
    if (not isinstance(document, dict) or set(document) != fields
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or not isinstance(document["cases"], list) or not 1 <= len(document["cases"]) <= 1000):
        raise ReplayError("unsupported replay document")
    baseline = document.get("baseline", {})
    if compare and (set(baseline) != {"revision", "source_sha256", "records"}
                    or baseline["revision"] != BASELINE or baseline["source_sha256"] != BASELINE_SOURCE):
        raise ReplayError("fixed baseline provenance mismatch")
    rows, seen = [], set()
    for case in document["cases"]:
        row = shadow(case, api)
        if row["id"] in seen:
            raise ReplayError("duplicate snapshot ID")
        seen.add(row["id"])
        if compare:
            original = baseline["records"].get(row["id"])
            if not original or original.get("snapshot_sha256") != row["snapshot_sha256"]:
                raise ReplayError("baseline does not cover the exact snapshot")
            validate_verdict(original["verdict"])
            row["baseline"] = original["verdict"]
            row["changed"] = row["baseline"] != row["candidate"]
            row["selection_changed"] = any(row["baseline"][key] != row["candidate"][key]
                                            for key in ("status", "band", "pick"))
        rows.append(row)
    if compare and set(baseline["records"]) != seen:
        raise ReplayError("baseline coverage differs from case coverage")
    return {"schema_version": 1, "mode": "replay" if compare else "shadow", "cases": rows,
            "summary": {"cases": len(rows), "recommendations": sum(r["candidate"]["status"] == "recommend" for r in rows),
                        "known_denied_recommendations": sum(r["known_denied_recommendation"] for r in rows),
                        "floor_violations": sum(r["floor_violation"] for r in rows),
                        "changed": sum(r.get("changed", False) for r in rows),
                        "selection_changes": sum(r.get("selection_changed", False) for r in rows)}}


def command(args, api):
    try:
        if args.account:
            raise ReplayError("offline snapshots own account selection")
        result = run(read(args.snapshot), api, compare=args.command == "replay")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        print(json.dumps({"status": "error", "reason": "invalid or unsupported replay snapshot"}))
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1 if result["summary"]["known_denied_recommendations"] or result["summary"]["floor_violations"] else 0
