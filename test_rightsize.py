#!/usr/bin/env python3
"""Self-check for the routing policy. Run: python3 test_rightsize.py

No framework. Every case here is a synthetic quota state plus a judgment, so
the policy can be checked without spending a token or touching a provider.
"""

import json
import tempfile
import time
from pathlib import Path

import rightsize as ar

CONFIG = json.loads((ar.ROOT / "config.json").read_text())
HOUR = 3600

# Never touch the real state file: it carries this machine's quota snapshots
# and exhausted marks, which would make these results depend on when they ran.
ar.STATE = Path(tempfile.mkdtemp(prefix="rightsize-test-")) / "state.json"


def probes(opencode=20, codex=20, openrouter=None, claude_percent=None, resets=None):
    """Build a probe set. Percentages are percent USED."""
    resets = resets or {}
    out = {
        "opencode": {
            "name": "opencode",
            "status": "ok",
            "buckets": [
                {"id": "weekly", "percent": opencode, "resets_at": resets.get("opencode", time.time() + 20 * HOUR), "source": "live"}
            ],
        },
        "codex": {
            "name": "codex",
            "status": "ok",
            "buckets": [
                {"id": "primary-10080m", "percent": codex, "resets_at": resets.get("codex", time.time() + 100 * HOUR), "source": "stale-lower-bound"}
            ],
        },
        "claude": {
            "name": "claude",
            "status": "ok",
            "buckets": [{"id": "weekly", "percent": claude_percent, "resets_at": None, "source": "no-budget-set"}],
        },
        "openrouter": {"name": "openrouter", "status": "no-credential", "buckets": []},
    }
    if openrouter is not None:
        out["openrouter"] = {
            "name": "openrouter",
            "status": "ok",
            "buckets": [
                {"id": "free-requests-day", "percent": openrouter, "resets_at": resets.get("openrouter", time.time() + 6 * HOUR), "source": "local-count"}
            ],
        }
    return out


def judged(tier, size=0.5, second=0.1, complete=0.9, destructive=0.05):
    return {
        "tier": tier,
        "tier_confidence": 0.9,
        "size": size,
        "second_opinion": second,
        "spec_complete": complete,
        "destructive": destructive,
        "source": "test",
    }


def route_with(judgment, state, monkey={}):
    original = ar.judge
    ar.judge = lambda spec: judgment
    try:
        return ar.route("test spec", CONFIG, probes=state)
    finally:
        ar.judge = original


def main():
    # Band selection follows the tier, and size raises it.
    assert ar.band_for(judged("mechanical"), CONFIG)[0] == 1
    assert ar.band_for(judged("implementation"), CONFIG)[0] == 1
    assert ar.band_for(judged("implementation", size=1.8), CONFIG)[0] == 2
    assert ar.band_for(judged("design"), CONFIG)[0] == 3
    assert ar.band_for(judged("diagnosis"), CONFIG)[0] == 3
    assert ar.band_for(judged("high_stakes"), CONFIG)[0] == 3

    # Ordinary coding goes to the cheap OpenCode workhorse.
    decision = route_with(judged("implementation"), probes())
    assert decision["pick"]["provider"] == "opencode", decision["pick"]
    assert decision["pick"]["model"] == "deepseek-v4.1-flash", decision["pick"]
    assert decision["band"] == 1

    # Design work escalates, and Claude stays available there even with no
    # budget set, because band 3 is the one place unknown headroom is allowed.
    decision = route_with(judged("design"), probes())
    assert decision["band"] == 3
    assert decision["pick"]["provider"] in ("codex", "claude", "opencode"), decision["pick"]

    # Below its reserve, OpenCode drops out and band 1 falls to the next
    # eligible candidate rather than failing.
    decision = route_with(judged("implementation"), probes(opencode=90))
    assert decision["pick"]["provider"] != "opencode", decision["pick"]
    assert any("below reserve" in note for note in decision["notes"]), decision["notes"]

    # Spend the bucket that expires first: with both eligible, the provider
    # whose bucket resets sooner is preferred even though it is later in the
    # configured order.
    soon = {"codex": time.time() + 2 * HOUR, "opencode": time.time() + 40 * HOUR}
    decision = route_with(judged("implementation"), probes(codex=5, resets=soon))
    assert decision["pick"]["provider"] == "codex", decision["pick"]
    assert any("resets in" in note for note in decision["notes"]), decision["notes"]

    # An irreversible task is flagged for a human, never silently dispatched.
    decision = route_with(judged("high_stakes", destructive=0.9), probes())
    assert decision["confirm_first"] is True

    # A brief nobody could execute alone is blocked before any provider is
    # considered.
    decision = route_with(judged("implementation", complete=0.2), probes())
    assert decision["blocked"] and "self-contained" in decision["blocked"]

    # A second opinion adds a review leg on a different vendor.
    decision = route_with(judged("implementation", second=0.9), probes())
    assert decision["review"] is not None
    assert decision["review"]["provider"] != decision["pick"]["provider"]

    # Everything metered exhausted: still returns an escalation rather than
    # nothing, so a run is never stranded.
    decision = route_with(judged("implementation"), probes(opencode=99, codex=99))
    assert decision["pick"] is None or decision["pick"]["provider"] == "claude", decision["pick"]

    # The heuristic stand-in labels itself, so a route made without Jev is
    # never mistaken for one made with it.
    fallback = ar.heuristic("add a database migration dropping the old column")
    assert fallback["tier"] == "high_stakes", fallback

    # Candidate parsing keeps OpenRouter's slashes and :free suffix intact.
    assert ar.parse_candidate("codex:gpt-6-astra:high") == {
        "provider": "codex", "model": "gpt-6-astra", "effort": "high"}
    assert ar.parse_candidate("openrouter:deepseek/deepseek-chat-v3.1:free") == {
        "provider": "openrouter", "model": "deepseek/deepseek-chat-v3.1:free", "effort": None}

    # Deals: a price drop and a new free model are both noticed.
    before = {"providers": {"opencode": {"models": {"a": {"input": 1.0}, "gone": {"input": 2.0}}}}}
    after = {"providers": {"opencode": {"models": {"a": {"input": 0.5}, "b:free": {"input": 0}}}}}
    found = ar.deals(before, after)
    assert found["price_drops"][0]["percent"] == 50.0, found
    assert found["free"][0]["model"] == "b:free", found
    assert found["new_models"][0]["model"] == "b:free", found
    assert found["removed_models"][0]["model"] == "gone", found

    # A launcher renders from config, so adding one is config and not code.
    decision = route_with(judged("implementation"), probes())
    line = ar.launch_command(decision, CONFIG, "orca", "task.md")
    assert "--agent opencode" in line and '--spec "$(cat task.md)"' in line, line
    assert "opencode -m opencode-go/deepseek-v4.1-flash" in line, line
    shell = ar.launch_command(decision, CONFIG, "shell", None)
    assert shell.startswith("opencode run -m opencode-go/deepseek-v4.1-flash"), shell
    inline = ar.launch_command(decision, CONFIG, "orca", None,
                               "add pagination to the invoices list endpoint")
    assert "'add pagination to the invoices list endpoint'" in inline, inline
    assert "<task>" not in inline, inline
    unknown = ar.launch_command(decision, CONFIG, "nope", None)
    assert "unknown launcher" in unknown and "orca, shell" in unknown, unknown

    # Rule 5: a reported quota error takes the provider out until its reset,
    # and the next route goes elsewhere rather than failing.
    ar.mark_exhausted("opencode", time.time() + 2 * HOUR)
    try:
        decision = route_with(judged("implementation"), probes())
        assert decision["pick"]["provider"] != "opencode", decision["pick"]
        assert any("quota error" in note for note in decision["notes"]), decision["notes"]
    finally:
        ar.mark_exhausted("opencode", 0)
    decision = route_with(judged("implementation"), probes())
    assert decision["pick"]["provider"] == "opencode", decision["pick"]

    # No budget means no transcript scan: the answer cannot depend on it.
    scanned = []
    original = ar.claude_tokens
    ar.claude_tokens = lambda seconds: scanned.append(seconds) or 0
    try:
        probe = ar.probe_claude({"claude": {}})
        assert scanned == [], "scanned transcripts for a number nothing reads"
        assert all(bucket["percent"] is None for bucket in probe["buckets"]), probe
        ar.probe_claude({"claude": {}}, count_tokens=True)
        assert scanned, "probe asked for the count and did not get it"
    finally:
        ar.claude_tokens = original

    # Reservations: a dispatch in flight is capacity that is already spoken
    # for, even though no quota reading has moved yet.
    ar.save_json(ar.STATE, {})
    elig = ar.eligibility(CONFIG, probes(opencode=76), record=False)
    before = elig["opencode"]["usable"]
    ar.reserve("opencode", 3.0, 1, "a worker that is running", 1800)
    elig = ar.eligibility(CONFIG, probes(opencode=76), record=False)
    assert elig["opencode"]["usable"] == before - 3.0, (before, elig["opencode"])
    assert elig["opencode"]["inflight"] == 1
    assert ar.release("opencode") == 1
    assert ar.eligibility(CONFIG, probes(opencode=76), record=False)["opencode"]["usable"] == before

    # A reservation nobody releases expires, so a worker that dies silently
    # cannot hold a plan hostage.
    ar.reserve("opencode", 3.0, 1, "a worker that died", -1)
    assert ar.eligibility(CONFIG, probes(opencode=76), record=False)["opencode"]["usable"] == before
    ar.save_json(ar.STATE, {})

    # Too many dispatches in flight blocks a provider before its quota does.
    limit = CONFIG["max_inflight"]["opencode"]
    for _ in range(limit):
        ar.reserve("opencode", 0.0, 1, "in flight", 1800)
    elig = ar.eligibility(CONFIG, probes(), record=False)
    assert not elig["opencode"]["eligible"], elig["opencode"]
    assert "in flight" in elig["opencode"]["blocked"], elig["opencode"]
    ar.save_json(ar.STATE, {})

    # Cost estimate scales with the band: a band 3 dispatch is not a band 1 one.
    assert ar.dispatch_cost(CONFIG, "opencode", 3) == 3 * ar.dispatch_cost(CONFIG, "opencode", 1)

    # A fan-out spreads across providers instead of sending everything to the
    # one whose bucket happens to expire first.
    original = ar.judge
    ar.judge = lambda spec: judged("implementation")
    try:
        result = ar.plan([f"task {i}" for i in range(60)], CONFIG, concurrency=4,
                         probes=probes(codex=5))
    finally:
        ar.judge = original
    assert len(result["spread"]) > 1, result["spread"]
    assert max(row["dispatches"] for row in result["spread"].values()) <= \
        max(CONFIG["max_inflight"].values()) * len(result["waves"]), result["spread"]

    # Cheap work is never answered with a band 3 model just because the cheap
    # providers are busy: inside a batch it waits for the next wave instead.
    placed = [t for t in result["tasks"] if t["wave"]]
    assert all(t["decision"]["band"] == 1 for t in placed), \
        [t["decision"]["band"] for t in placed if t["decision"]["band"] != 1]
    assert all(t["decision"]["pick"]["provider"] != "claude" for t in placed)

    # Waves are ordered and every task lands in one of them, or is named as
    # having nowhere to go. Nothing is silently dropped.
    assert sorted(result["waves"]) == list(range(1, len(result["waves"]) + 1)), result["waves"]
    accounted = len(placed) + len(result["blocked"]) + len(result["unplaced"])
    assert accounted == 60, (accounted, len(placed), result["blocked"], result["unplaced"])

    # A single route still escalates rather than stranding one task, which is
    # the opposite call from the batch and deliberately so.
    solo = route_with(judged("implementation"), probes(opencode=99, codex=99))
    assert solo["pick"] is None or solo["band"] == 3, solo

    # Effort is a second dial on the chosen model, for the CLIs that take it.
    decision = route_with(judged("design"), probes(codex=10))
    if decision["pick"]["provider"] in ("codex", "claude"):
        assert decision["pick"]["effort"] in ar.EFFORTS, decision["pick"]
    assert ar.effort_for(CONFIG, {"provider": "opencode", "effort": None}, 1, judged("mechanical")) \
        == (None, None), "opencode has no effort knob and must not be given one"

    # A wide blast radius or an irreversible step raises effort without
    # changing the model, and a retry raises it again.
    base, _ = ar.effort_for(CONFIG, {"provider": "codex", "effort": None}, 3, judged("design"))
    wide, why = ar.effort_for(CONFIG, {"provider": "codex", "effort": None}, 3,
                              judged("design", size=2.0))
    assert ar.EFFORTS.index(wide) > ar.EFFORTS.index(base), (base, wide)
    assert why and "blast radius" in why, why
    retry, _ = ar.effort_for(CONFIG, {"provider": "codex", "effort": None}, 3,
                             judged("design", size=2.0), attempt=1)
    assert ar.EFFORTS.index(retry) >= ar.EFFORTS.index(wide), (wide, retry)
    assert retry == ar.EFFORTS[min(ar.EFFORTS.index(base) + 2, len(ar.EFFORTS) - 1)]

    # A rerun starts above the band that already failed and never picks the
    # model that just failed.
    elig = ar.eligibility(CONFIG, probes(), record=False)
    again = ar.decide(judged("implementation"), CONFIG, elig, floor_band=2,
                      exclude={"opencode:deepseek-v4.1-flash"}, attempt=1)
    assert again["band"] >= 2, again["band"]
    assert again["pick"]["model"] != "deepseek-v4.1-flash", again["pick"]
    # And within the band it already failed in, it is passed over by name.
    same_band = ar.decide(judged("implementation"), CONFIG, elig,
                          exclude={"opencode:deepseek-v4.1-flash"}, attempt=1)
    assert same_band["pick"]["model"] != "deepseek-v4.1-flash", same_band["pick"]
    assert any("already had a go" in note for note in same_band["notes"]), same_band["notes"]

    # Doctor catches a ladder entry the provider has retired. This is the
    # failure that is invisible until a worker tries the name: routing will
    # happily pick a model id nobody has confirmed still exists.
    registry = {"fetched_at": "2026-09-20T00:00:00+00:00",
                "providers": {"opencode": {"models": {"deepseek-v4.1-flash": {}}}}}
    original_registry = ar.REGISTRY
    tmp = ar.STATE.parent / "registry.json"
    tmp.write_text(json.dumps(registry))
    ar.REGISTRY = tmp
    try:
        findings = ar.doctor({**CONFIG, "bands": {"1": ["opencode:deepseek-v4.1-flash",
                                                        "opencode:a-model-that-was-retired"]},
                              "review_ladder": []})
        errors = [message for level, message in findings if level == "error"]
        assert errors and "a-model-that-was-retired" in errors[0], findings
        clean = ar.doctor({**CONFIG, "bands": {"1": ["opencode:deepseek-v4.1-flash"]},
                           "review_ladder": []})
        assert not [m for level, m in clean if level == "error"], clean
    finally:
        ar.REGISTRY = original_registry

    # A file-sourced reading ages. An old high number is the dangerous
    # direction: it blocks a provider that may have reset hours ago.
    rollout = {"primary": {"used_percent": 93.0, "window_minutes": 10080,
                           "resets_at": time.time() + 26 * HOUR}}
    fresh = ar.codex_buckets(rollout, observed_at=time.time() - 60, config=CONFIG)
    assert fresh[0]["percent"] == 93.0 and fresh[0]["source"] == "stale-lower-bound", fresh
    old_reading = ar.codex_buckets(rollout, observed_at=time.time() - 20 * HOUR, config=CONFIG)
    assert old_reading[0]["percent"] is None, old_reading
    assert old_reading[0]["source"] == "expired-reading", old_reading

    # And a window that has already rolled over is empty, not still full:
    # usage only accrues by running Codex, which writes a newer reading.
    rolled = {"primary": {"used_percent": 93.0, "window_minutes": 10080,
                          "resets_at": time.time() - 2 * HOUR}}
    after = ar.codex_buckets(rolled, observed_at=time.time() - 3 * HOUR, config=CONFIG)
    assert after[0]["percent"] == 0.0, after
    assert after[0]["source"] == "post-reset-assumed-zero", after
    assert after[0]["resets_at"] > time.time(), "the next reset must be in the future"

    print("all checks passed")


if __name__ == "__main__":
    main()
