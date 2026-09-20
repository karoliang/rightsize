#!/usr/bin/env python3
"""Self-check for the routing policy. Run: python3 test_rightsize.py

No framework. Every case here is a synthetic quota state plus a judgment, so
the policy can be checked without spending a token or touching a provider.
"""

import json
import time

import rightsize as ar

CONFIG = json.loads((ar.ROOT / "config.json").read_text())
HOUR = 3600


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
    soon = {"openrouter": time.time() + 2 * HOUR, "opencode": time.time() + 40 * HOUR}
    decision = route_with(judged("implementation"), probes(openrouter=5, resets=soon))
    assert decision["pick"]["provider"] == "openrouter", decision["pick"]

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

    print("all checks passed")


if __name__ == "__main__":
    main()
