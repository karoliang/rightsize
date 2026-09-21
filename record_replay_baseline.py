"""Developer-only synthetic golden capture from the hash-pinned repair baseline.

Run from this repository: python3 record_replay_baseline.py > fixtures/replay.json
Only reads the fixed Git object; does not fetch, install, probe or launch a model.
The replay/shadow commands never import or execute this capture script.
"""

import contextlib
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import types

import replay


def cases():
    at = 1700000000
    config = {"bands": {str(i): [f"{p}:fixture-band-{i}:low" for p in ("opencode", "codex")]
                        for i in (1, 2, 3)}, "agents": {"opencode": "opencode", "codex": "codex"},
              "review_ladder": [], "reserves": {"opencode": 10, "codex": 10},
              "dispatch_cost": {"_default": 2}, "max_inflight": {"_default": 2}}
    probes = {p: {"name": p, "status": "ok", "observed_at": at, "quota_account_ref": replay.digest(p),
                  "account": {"account_ref": replay.digest(p)[:24], "fingerprint": replay.digest(p + ":credential"),
                              "status": "unverified", "source": "native", "provider": p, "runtime": p},
                  "buckets": [{"id": "weekly", "percent": 5, "source": "live", "resets_at": at + 3600}]}
              for p in ("opencode", "codex")}
    base = {"id": "healthy", "at": at, "task_sha256": replay.digest("synthetic task"),
            "context_sha256": "none", "judgment": {"tier": "implementation", "size": 0,
                "second_opinion": 0, "spec_complete": 1, "destructive": 0}, "floor": 1,
            "config": config, "probes": probes, "legacy": {}, "attempts": []}
    rows = [copy.deepcopy(base)]
    def add(name):
        row = copy.deepcopy(base)
        row["id"] = name
        rows.append(row)
        return row
    row = add("denied-weekly")
    row["probes"]["opencode"]["buckets"][0]["raw_status"] = "rate-limited"
    row = add("denied-account-missing-buckets")
    row["probes"]["opencode"].update(status="denied", buckets=[])
    row["judgment"]["tier"] = "high_stakes"
    row = add("missing-telemetry")
    row["probes"]["opencode"]["buckets"][0].update(percent=None, source="unavailable")
    row["judgment"]["tier"] = "diagnosis"
    row = add("sticky-legacy-denial")
    row["legacy"] = {"quota_denials": {"opencode": {"weekly": {"id": "weekly", "raw_status": "rate-limited"}}}}
    row = add("legacy-slots")
    row["legacy"] = {"reservations": [{"provider": "opencode", "points": 2, "expires": at + 60} for _ in range(2)]}
    row = add("legacy-points")
    row["legacy"] = {"reservations": [{"provider": "opencode", "points": 84, "expires": at + 60}]}
    row = add("uncertain-managed-attempt")
    row["attempts"] = [{"state": "reconciling", "points": 85,
                        "pick": {"provider": "opencode", "model": "fixture-band-1", "effort": "low"},
                        "account": probes["opencode"]["account"], "spec_hash": base["task_sha256"],
                        "context_hash": "none"}]
    row = add("all-unavailable")
    for probe in row["probes"].values():
        probe.update(status="reauth-required", buckets=[])
    row = add("destructive")
    row["judgment"].update(tier="high_stakes", destructive=1)
    row = add("account-changed")
    row["probes"]["opencode"].update(status="account-changed", buckets=[])
    row = add("missing-native-quota-identity")
    row["probes"]["opencode"]["buckets"][0]["percent"] = 100
    row["probes"]["codex"].pop("quota_account_ref")
    row = add("selected-context-hash")
    row["context_sha256"] = replay.digest("approved synthetic context")
    row = add("unreviewed-and-unattributed-outcomes")
    row["attempts"] = [{"state": "completed", "pick": {"provider": "codex"}}, {"state": "accepted"}]
    row = add("migrated-legacy-record-without-receipt")
    row["legacy"] = {"reservations": [{"provider": "opencode", "points": 2, "expires": at + 60}]}
    row["attempts"] = [{"state": "reconciling", "points": 2, "migration_source": "legacy-json",
                        "pick": {"provider": "opencode", "model": "fixture-band-1", "effort": "low"},
                        "account": probes["opencode"]["account"]}]
    return rows


def capture():
    source = subprocess.check_output(["git", "show", replay.BASELINE + ":rightsize.py"],
                                     cwd=Path(__file__).resolve().parent, timeout=10)
    if hashlib.sha256(source).hexdigest() != replay.BASELINE_SOURCE:
        raise RuntimeError("baseline source digest mismatch")
    module = types.ModuleType("rightsize_fixed_baseline")
    module.__file__ = str(Path(__file__).resolve().parent / "rightsize.py")
    exec(compile(source, "<fixed baseline>", "exec"), module.__dict__)
    snapshots, records = cases(), {}
    for case in snapshots:
        frozen = replay.policy(module, case["at"])
        # Function globals are a separate dictionary owned by the cloned policy.
        namespace = frozen.decide.__globals__
        state = copy.deepcopy(case["legacy"])
        def forbidden(*args, **kwargs):
            raise AssertionError("baseline capture attempted a side effect")
        namespace.update(STATE="in-memory", load_json=lambda *args: state,
                         state_lock=contextlib.nullcontext, save_json=forbidden,
                         exhausted_until=lambda name: state.get("exhausted", {}).get(name, 0))
        def reservations(name):
            rows = [r for r in state.get("reservations", []) if r["provider"] == name and r["expires"] > case["at"]]
            return sum(r["points"] for r in rows), len(rows)
        namespace["reservation_load"] = reservations
        eligibility = types.FunctionType(module.eligibility.__code__, namespace, "eligibility", module.eligibility.__defaults__)
        elig = eligibility(case["config"], case["probes"], record=False)
        decision = frozen.decide(case["judgment"], case["config"], elig)
        records[case["id"]] = {"snapshot_sha256": replay.digest(case), "verdict": replay.verdict(decision)}
    return {"schema_version": 1, "cases": snapshots,
            "baseline": {"revision": replay.BASELINE, "source_sha256": replay.BASELINE_SOURCE, "records": records}}


if __name__ == "__main__":
    print(json.dumps(capture(), indent=2, sort_keys=True))
