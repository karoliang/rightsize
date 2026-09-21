#!/usr/bin/env python3
"""Self-check for the routing policy. Run: python3 test_rightsize.py

No framework. Every case here is a synthetic quota state plus a judgment, so
the policy can be checked without spending a token or touching a provider.
"""

import json
import os
import sys
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


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


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
    assert '--spec "$(cat -- task.md)"' in line, line
    # The model has to be bound at launch. `--agent opencode` takes no model
    # flag and OPENCODE_MODEL is ignored, so a worker started without this
    # silently runs whatever ~/.config/opencode/opencode.json names.
    assert "opencode -m opencode-go/deepseek-v4.1-flash" in line, line
    assert '--terminal "$HANDLE"' in line, line
    assert "--worktree name:" in line, line
    shell = ar.launch_command(decision, CONFIG, "shell", None)
    assert shell.startswith("opencode run -m opencode-go/deepseek-v4.1-flash"), shell
    inline = ar.launch_command(decision, CONFIG, "orca", None,
                               "add pagination to the invoices list endpoint")
    assert "'add pagination to the invoices list endpoint'" in inline, inline
    assert "<task>" not in inline, inline
    unknown = ar.launch_command(decision, CONFIG, "nope", None)
    assert "unknown launcher" in unknown and "orca" in unknown and "shell" in unknown, unknown

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
    ar.claude_tokens = lambda seconds, projects=None: scanned.append(seconds) or 0
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

    # A repo may pin rules of its own. Dicts merge; a list replaces, because a
    # repo pinning a ladder means "these", not "these as well".
    merged = ar.merge({"reserves": {"opencode": 15, "codex": 10},
                       "bands": {"1": ["opencode:a", "codex:b"]},
                       "thresholds": {"spec_complete_min": 0.35}},
                      {"reserves": {"opencode": 40},
                       "bands": {"1": ["opencode:only-this"]}})
    assert merged["reserves"] == {"opencode": 40, "codex": 10}, merged["reserves"]
    assert merged["bands"]["1"] == ["opencode:only-this"], merged["bands"]
    assert merged["thresholds"]["spec_complete_min"] == 0.35, merged["thresholds"]

    # The overlay is found at or above the working directory.
    import os
    nest = ar.STATE.parent / "repo" / "packages" / "web"
    nest.mkdir(parents=True, exist_ok=True)
    (ar.STATE.parent / "repo" / ".rightsize.json").write_text('{"reserves": {"opencode": 40}}')
    cwd = os.getcwd()
    try:
        os.chdir(nest)
        found = ar.repo_config()
        assert found and found.name == ".rightsize.json", found
    finally:
        os.chdir(cwd)

    # Every opencode worker launched through Orca must carry its model, or the
    # band is decorative: six real workers once ran the config default and it
    # was indistinguishable from the pick being applied.
    for provider, template in CONFIG["launchers"]["orca"].items():
        if provider in ("codex", "claude") or provider.startswith("_"):
            continue
        assert "opencode -m {model_ref}" in template, (provider, template)

    # The shipped Orca launcher must never reuse the coordinator's checkout: a
    # worker in the main tree shares its branch and cleanup cannot find it.
    for name, templates in CONFIG["launchers"].items():
        if name == "orca-current" or not isinstance(templates, dict):
            continue
        for provider, template in templates.items():
            if provider.startswith("_"):
                continue
            assert "--worktree current" not in template, (name, provider, template)

    # `--worktree new-child` requires `--name`, so a template that uses one
    # without the other is unrunnable as printed.
    for name, templates in CONFIG["launchers"].items():
        if not isinstance(templates, dict):
            continue
        for provider, template in templates.items():
            if provider.startswith("_") or "new-child" not in template:
                continue
            assert "--name" in template, (name, provider, template)

    # The name is derived from the task, keeps an issue number, and is unique
    # within a batch.
    assert ar.worktree_name("make the disabled control treatment match the system (#381)") \
        == "disabled-control-treatment-match-381", ar.worktree_name(
            "make the disabled control treatment match the system (#381)")
    taken = set()
    first = ar.worktree_name("add pagination to the invoices endpoint", taken)
    second = ar.worktree_name("add pagination to the invoices endpoint", taken)
    assert first != second, (first, second)

    # The name the decision carries wins, because a batch deduplicates there.
    decision = route_with(judged("implementation"), probes())
    decision["worktree_name"] = "disabled-controls-381"
    rendered = ar.launch_command(decision, CONFIG, "orca", None, "any other text")
    assert "--name disabled-controls-381" in rendered, rendered
    # opencode gets its own worktree by name; codex and claude say new-child.
    assert "--worktree name:disabled-controls-381" in rendered, rendered
    codex_line = CONFIG["launchers"]["orca"]["codex"]
    assert "--worktree new-child" in codex_line and "--name {name}" in codex_line, codex_line

    # With none carried, it is derived from the brief passed in.
    decision.pop("worktree_name")
    derived = ar.launch_command(decision, CONFIG, "orca", None,
                                "fix the disabled control treatment (#381)")
    assert "--name fix-disabled-control-treatment-381" in derived, derived

    # Codex writes where it is told. Orca gives each account its own
    # CODEX_HOME, so reading only ~/.codex believed a 20h-old rollout from a
    # plan that had been replaced while the live one sat unread. Nothing failed
    # visibly: Codex simply stopped being picked.
    import os
    sandbox = ar.STATE.parent / "codex-homes"
    default_home = sandbox / "default"
    account_home = sandbox / "orca" / "codex-accounts" / "acct-1" / "home"
    env_home = sandbox / "env-home"

    def rollout(home, name, age_seconds):
        day = home / "sessions" / "2026" / "09" / "20"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-{name}.jsonl"
        path.write_text('{"rate_limits": {}}\n')
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    old_one = rollout(default_home / ".codex", "old", 20 * HOUR)
    live = rollout(account_home, "live", 60)
    from_env = rollout(env_home, "env", 5 * HOUR)

    saved_home, saved_accounts = ar.HOME, ar.ORCA_CODEX_ACCOUNTS
    saved_env = {k: os.environ.get(k) for k in ("CODEX_HOME", "ORCA_CODEX_HOME")}
    try:
        ar.HOME = default_home
        ar.ORCA_CODEX_ACCOUNTS = sandbox / "orca" / "codex-accounts"
        for key in saved_env:
            os.environ.pop(key, None)

        # The launchd case: no CODEX_HOME in the environment at all, and the
        # account home must still be found, or the timer reads the stale plan.
        assert ar.newest_codex_rollout() == live, ar.newest_codex_rollout()

        # An explicit CODEX_HOME is searched too, and the newest still wins.
        os.environ["CODEX_HOME"] = str(env_home)
        assert ar.newest_codex_rollout() == live, ar.newest_codex_rollout()
        os.utime(from_env, (time.time(), time.time()))
        assert ar.newest_codex_rollout() == from_env, ar.newest_codex_rollout()

        # The same directory named twice is searched once.
        os.environ["ORCA_CODEX_HOME"] = str(env_home)
        assert len(ar.codex_homes()) == len(set(map(str, ar.codex_homes()))), ar.codex_homes()

        # With nothing newer anywhere, the default home is still read.
        for path in (live, from_env):
            os.utime(path, (time.time() - 40 * 86400,) * 2)
        assert ar.newest_codex_rollout() == old_one, ar.newest_codex_rollout()
    finally:
        ar.HOME, ar.ORCA_CODEX_ACCOUNTS = saved_home, saved_accounts
        for key, value in saved_env.items():
            os.environ[key] = value if value is not None else ""
            if value is None:
                os.environ.pop(key, None)

    # A JSON caller builds its own worker-start line, and new-child is refused
    # without a name, so the decision has to carry one.
    named = route_with(judged("implementation"), probes())
    assert named.get("worktree_name"), named.keys()

    # Calibration: percentage points burned in a window, divided by the
    # dispatches made in it, is dispatch_cost in the unit the config uses.
    window_hours = 7 * 24
    now_epoch = time.time()
    sessions = [
        {"primaryModel": "opencode-go/deepseek-v4.1-flash", "lastTimestamp": iso(now_epoch - h * HOUR),
         "totalInputTokens": 1000, "totalCachedInputTokens": 900, "totalOutputTokens": 100}
        for h in (1, 5, 20, 40, 100)
    ]
    # Plus one outside the window and one on another provider: neither counts.
    sessions.append({"primaryModel": "opencode-go/deepseek-v4.1-flash",
                     "lastTimestamp": iso(now_epoch - 300 * HOUR)})
    sessions.append({"primaryModel": "openrouter/something", "lastTimestamp": iso(now_epoch - 2 * HOUR)})
    spent = ar.dispatches_since(sessions, now_epoch - window_hours * HOUR, "opencode-go/")
    assert spent["dispatches"] == 5, spent
    assert spent["input"] == 5000 and spent["output"] == 500, spent
    assert spent["record_from"] < now_epoch - 299 * HOUR, "the record start spans every model"

    # A window is inferred from the bucket, including Codex's explicit minutes.
    assert ar.bucket_window("weekly") == 7 * 86400
    assert ar.bucket_window("primary-10080m") == 10080 * 60
    assert ar.bucket_window("key-credit") is None, "a credit balance has no window to divide"

    # Releasing on the orchestrator's word, not a person's memory. The brief is
    # the key that survives the coordinator naming the worktree something else.
    ar.save_json(ar.STATE, {})
    kept_id = ar.reserve("opencode", 0.5, 1, "still running: add pagination", 1800, "pag-1")
    ar.reserve("codex", 1.2, 2, "Finished: rename getUser across the repo", 1800, "renamed-elsewhere")
    ar.reserve("claude", 6.0, 3, "also finished, matched by worktree", 1800, "wt-done")
    settled = lambda run=None: ([{"dispatch": "ctx_1", "state": "succeeded", "worktree": "wt-done"}], None)
    tasks = lambda run=None: {ar.brief_key("Finished: rename getUser across the repo")}
    original = ar.orca_settled, ar.orca_settled_tasks
    ar.orca_settled, ar.orca_settled_tasks = settled, tasks
    try:
        result = ar.release_settled()
    finally:
        ar.orca_settled, ar.orca_settled_tasks = original
    freed = {r["provider"]: r["matched"] for r in result["released"]}
    # Only the brief releases. A worktree name is one rightsize suggested and
    # any later dispatch may reuse, so matching on it would let an older worker
    # settling in a reused checkout free the hold of the worker running there
    # now. An unmatched hold waits for its expiry instead.
    assert freed == {"codex": "brief"}, result["released"]
    assert kept_id in {k["id"] for k in result["kept"]}, result["kept"]
    assert ar.reservation_load("opencode")[1] == 1, "the running worker keeps its capacity"
    assert ar.reservation_load("claude")[1] == 1, "an unmatched hold waits for its expiry"
    # Reservations made in the same millisecond must still be distinguishable,
    # or releasing one frees every one of them.
    ids = {ar.reserve("opencode", 0.1, 1, f"batch task {i}", 60, f"wt-{i}") for i in range(20)}
    assert len(ids) == 20, "reservation ids collided"
    assert ar.reservation_load("codex")[1] == 0, "the matched brief was released"
    ar.save_json(ar.STATE, {})

    # money.financial, 2026-09-20: opencode's weekly had 8 usable points and
    # reset in 19 hours while codex sat at 0 per cent with a week of room, and a
    # design task was banded onto the emptying bucket at its most expensive
    # rung. Expiring-first is right while a bucket has slack, and wrong when it
    # does not.
    thin = {"opencode": time.time() + 19 * HOUR, "codex": time.time() + 7 * 86400}
    decision = route_with(judged("design"), probes(opencode=77, codex=0, resets=thin))
    assert decision["band"] == 3, decision["band"]
    assert decision["pick"]["provider"] == "codex", decision["pick"]
    assert any("roomiest plan" in note for note in decision["notes"]), decision["notes"]

    # The cheap rung still spends the expiring bucket, which is the whole point
    # of rule 3: it is the expensive rung that must not land there.
    # Cleared first: a snapshot left by an earlier case would now be read as
    # this provider's burn rate, since every bucket is paced against one.
    ar.save_json(ar.STATE, {})
    cheap = route_with(judged("implementation"), probes(opencode=77, codex=0, resets=thin))
    assert cheap["pick"]["provider"] == "opencode", cheap["pick"]

    # A bucket that cannot cover the dispatch at all is passed over rather than
    # merely ranked low.
    starved = probes(opencode=84, codex=99, resets=thin)
    thinned = route_with(judged("implementation"), starved)
    assert any("cannot cover" in note or "below reserve" in note for note in thinned["notes"]), \
        thinned["notes"]

    # Nested windows: every token spent against the weekly is also spent
    # against the monthly, so a weekly that looks cheap to empty can be the
    # thing that exhausts the month. Percent alone cannot see that; pace can.
    month = time.time() + 27 * 86400
    early_month = {"id": "monthly", "percent": 38.0, "resets_at": month}
    pace = ar.bucket_pace(early_month)
    assert pace and pace["projected"] > 300, pace
    # A weekly nearly spent but nearly over is on pace, not over it.
    late_week = {"id": "weekly", "percent": 77.0, "resets_at": time.time() + 19 * HOUR}
    weekly_pace = ar.bucket_pace(late_week)
    assert weekly_pace and weekly_pace["projected"] < 100, weekly_pace
    # Just after a reset, one dispatch must not project to anything.
    assert ar.bucket_pace({"id": "monthly", "percent": 1.0,
                           "resets_at": time.time() + 29.9 * 86400}) is None

    # A provider burning through a window it cannot sustain stops taking cheap
    # work, so what is left is kept for work with nowhere cheaper to go.
    paced = probes()
    paced["opencode"]["buckets"] = [
        {"id": "weekly", "percent": 40.0, "resets_at": time.time() + 19 * HOUR, "source": "live"},
        {"id": "monthly", "percent": 38.0, "resets_at": month, "source": "live"},
    ]
    decision = route_with(judged("implementation"), paced)
    assert decision["pick"]["provider"] != "opencode", decision["pick"]
    assert any("over pace" in note for note in decision["notes"]), decision["notes"]

    # A plan prints commands someone may never run, so its holds expire fast;
    # a hook reserving after a launch has run keeps the long clock.
    ar.save_json(ar.STATE, {})
    original = ar.judge
    ar.judge = lambda spec: judged("implementation")
    try:
        ar.plan(["a task nobody will dispatch"], CONFIG, concurrency=1, hold=True, probes=probes())
    finally:
        ar.judge = original
    held = (ar.load_json(ar.STATE, {}) or {})["reservations"]
    assert held, "the plan should have held something"
    short = float(CONFIG["reservation_ttl_unconfirmed_seconds"])
    assert held[0]["expires"] - held[0]["at"] <= short + 1, held[0]
    assert short < float(CONFIG["reservation_ttl_seconds"]), "unconfirmed must be the shorter clock"
    ar.save_json(ar.STATE, {})

    # Percentage points are not a unit anyone reasons in, and every plan meters
    # something different. Dispatches remaining is the common one.
    room = ar.capacity_in_dispatches(CONFIG, "opencode", 8.0)
    assert "band 1" in room and "band 3" in room, room
    cheap = int(8.0 / ar.dispatch_cost(CONFIG, "opencode", 1))
    assert room.startswith(f"{cheap} band 1"), (room, cheap)
    assert ar.capacity_in_dispatches(CONFIG, "opencode", None) == ""
    assert ar.capacity_in_dispatches(CONFIG, "opencode", -3.0) == ""
    # A provider whose dispatches cost nothing has no such limit to report.
    assert ar.capacity_in_dispatches(CONFIG, "opencode_zen", 50.0) == ""

    # Concurrency. Every mutator loads the whole document, changes a field and
    # writes it back, and a hook fires on every Bash command while a batch
    # reserves in a loop. Twelve concurrent writers once kept one reservation
    # and left the file unparseable, after which every load returned {}.
    import subprocess
    sandbox = Path(tempfile.mkdtemp(prefix="rightsize-race-"))
    (sandbox / ".local/state/rightsize").mkdir(parents=True)
    writer = (
        "import sys; sys.path.insert(0, %r)\n"
        "import rightsize as r\n"
        "r.reserve('opencode', 0.1, 1, 'task ' + sys.argv[1], 600, 'wt-' + sys.argv[1])\n"
        % str(ar.ROOT)
    )
    script = sandbox / "writer.py"
    script.write_text(writer)
    env = {**os.environ, "HOME": str(sandbox)}
    running = [subprocess.Popen([sys.executable, str(script), str(i)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
               for i in range(12)]
    for process in running:
        process.wait()
    written = json.loads((sandbox / ".local/state/rightsize/state.json").read_text())
    assert len(written.get("reservations", [])) == 12, \
        f"{len(written.get('reservations', []))} of 12 concurrent reservations survived"

    adversarial()
    adversarial_two()
    # Codex answers live over its own app-server, which is both fresher than
    # the file it leaves behind and attributable: Orca hardlinks rollouts
    # across account homes, so a rate_limits block found under one account may
    # have been written by another.
    original = ar.codex_rate_limits
    ar.codex_rate_limits = lambda timeout=15.0, binding=None: {
        "accountId": "e81eb3ba-1ed5-420f-8196-abb639352b15",
        "ordinaryUsageAllowed": True,
        "rateLimits": {"planType": "pro",
                       "primary": {"usedPercent": 7, "windowDurationMins": 10080,
                                   "resetsAt": time.time() + 6 * 86400}},
    }
    try:
        probe = ar.probe_codex(CONFIG)
    finally:
        ar.codex_rate_limits = original
    assert probe["buckets"][1]["percent"] == 7, probe
    assert probe["buckets"][0]["source"] == "live", probe
    assert probe["buckets"][1]["account"] == probe["account"]["account_ref"], probe
    assert "age_seconds" not in probe["buckets"][0], "a live reading does not age"

    # With no answer, unattributable rollout data must not become quota evidence.
    ar.codex_rate_limits = lambda timeout=15.0, binding=None: None
    try:
        fallback = ar.probe_codex(CONFIG)
    finally:
        ar.codex_rate_limits = original
    assert fallback["status"] == "unknown", fallback

    print("all checks passed")


def adversarial():
    """Cases a worker was dispatched to find, by attacking the policy.

    Each one failed when it was written. They are kept because every one of
    them was a rule combining with another rule to produce an answer no
    careful engineer would defend.
    """
    # A newly reset bucket should serve band 1 because a sample from the previous window cannot establish its current burn rate.
    ar.save_json(ar.STATE, {"snapshots": {
        "opencode:weekly": {"at": time.time() - 120, "percent": 1.0},
    }})
    state = probes(opencode=2, codex=99, claude_percent=99,
                   resets={"opencode": time.time() + 7 * 86400 - 60})
    decision = route_with(judged("implementation"), state)
    assert decision["band"] == 1, (decision["band"], decision["pick"])

    # A provider with an unknown monthly bucket should remain escalation-only even when its known weekly bucket has room.
    ar.save_json(ar.STATE, {})
    state = probes()
    state["opencode"]["buckets"].append(
        {"id": "monthly", "percent": None, "resets_at": time.time() + 27 * 86400,
         "source": "expired-reading"})
    decision = route_with(judged("implementation"), state)
    assert decision["pick"]["provider"] == "codex", decision["pick"]

    # When every metered plan is over pace, cheap work should wait or stay cheap because buying band 3 on those same plans accelerates exhaustion.
    ar.save_json(ar.STATE, {})
    state = probes(opencode=40, codex=40, claude_percent=99,
                   resets={"opencode": time.time() + 6 * 86400,
                           "codex": time.time() + 6 * 86400})
    decision = route_with(judged("mechanical"), state)
    assert decision["pick"] is None or decision["band"] == 1, \
        (decision["band"], decision["pick"])

    # A batch whose only funded provider starts at its in-flight limit should place work in wave 2 because completion returns those slots.
    ar.save_json(ar.STATE, {})
    state = probes(opencode=99, codex=20, claude_percent=99)
    for _ in range(CONFIG["max_inflight"]["codex"]):
        ar.reserve("codex", ar.dispatch_cost(CONFIG, "codex", 1), 1,
                   "already running", 1800)
    original = ar.judge
    ar.judge = lambda spec: judged("implementation")
    try:
        result = ar.plan(["task after running workers finish"], CONFIG, probes=state)
    finally:
        ar.judge = original
    assert result["tasks"][0]["wave"] == 2, (result["waves"], result["unplaced"])

    # After the first dispatch spends the last on-pace allowance, the second should use Codex because batch commitments count toward pace too.
    ar.save_json(ar.STATE, {})
    state = probes(opencode=49.8, codex=0,
                   resets={"opencode": time.time() + 84 * HOUR,
                           "codex": time.time() + 100 * HOUR})
    original = ar.judge
    ar.judge = lambda spec: judged("implementation")
    try:
        result = ar.plan(["last on-pace dispatch", "next dispatch"], CONFIG, probes=state)
    finally:
        ar.judge = original
    assert result["tasks"][1]["decision"]["pick"]["provider"] == "codex", result["spread"]

    # The second task should have no review leg because the first review spends Codex's last affordable dispatch and no other review provider qualifies.
    ar.save_json(ar.STATE, {})
    state = probes(opencode=20, codex=88.5, claude_percent=99,
                   resets={"opencode": time.time() + 2 * HOUR,
                           "codex": time.time() + 10 * HOUR})
    original = ar.judge
    ar.judge = lambda spec: judged("implementation", second=0.9)
    try:
        result = ar.plan(["first reviewed task", "second reviewed task"], CONFIG, probes=state)
    finally:
        ar.judge = original
    assert result["tasks"][1]["decision"]["review"] is None, \
        (result["tasks"][1]["decision"]["review"], result["quota_after"]["codex"])


def adversarial_two():
    """New rule interactions that still produce indefensible routing decisions."""
    # An unavailable monthly reading should send cheap work to Codex because a readable weekly bucket cannot prove the month has room.
    ar.save_json(ar.STATE, {})
    state = probes()
    state["opencode"]["buckets"].append(
        {"id": "monthly", "percent": None, "resets_at": time.time() + 27 * 86400,
         "source": "unavailable"})
    decision = route_with(judged("implementation"), state)
    assert decision["pick"]["provider"] == "codex", decision["pick"]

    # An accelerating monthly burn should send cheap work to Codex because its 115% projection matters even while the weekly bucket binds and whole-window pace is safe.
    ar.save_json(ar.STATE, {"snapshots": {
        "opencode:monthly": {"at": time.time() - 48 * HOUR, "percent": 30},
    }})
    state = probes(opencode=80, codex=0)
    state["opencode"]["buckets"].append(
        {"id": "monthly", "percent": 40, "resets_at": time.time() + 15 * 86400,
         "source": "live"})
    decision = route_with(judged("implementation"), state)
    assert decision["pick"]["provider"] == "codex", decision["pick"]

    # A measured burn overrun should fall back to funded Claude because relaxing average pacing must not erase a 135% recent-burn projection on another plan.
    ar.save_json(ar.STATE, {"snapshots": {
        "opencode:weekly": {"at": time.time() - 9 * HOUR, "percent": 40},
    }})
    state = probes(opencode=60, codex=99, claude_percent=20,
                   resets={"opencode": time.time() + 33.6 * HOUR})
    decision = route_with(judged("implementation"), state)
    assert decision["pick"]["provider"] == "claude", decision["pick"]

    # The ninth batch task should use Codex because eight commitments raise OpenCode from 1.9% to 5.42% spent in a 5.1%-elapsed week, crossing both the guard and its pace allowance.
    ar.save_json(ar.STATE, {})
    state = probes(opencode=1.9, codex=0,
                   resets={"opencode": time.time() + 0.949 * 7 * 86400,
                           "codex": time.time() + 0.97 * 7 * 86400})
    original = ar.judge
    ar.judge = lambda spec: judged("implementation")
    try:
        result = ar.plan([f"guard crossing task {i}" for i in range(10)], CONFIG,
                         probes=state)
    finally:
        ar.judge = original
    assert result["tasks"][8]["decision"]["pick"]["provider"] == "codex", result["spread"]

    # Two concurrent held routes must not both take OpenCode's single slot because locking only the reservation write leaves the capacity check stale.
    ar.save_json(ar.STATE, {})
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    config = json.loads(json.dumps(CONFIG))
    config["max_inflight"]["opencode"] = 1
    state = probes()
    barrier = Barrier(2)
    original = ar.judge

    def concurrent_judgment(spec):
        barrier.wait(timeout=5)
        return judged("implementation")

    ar.judge = concurrent_judgment
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(pool.map(
                lambda spec: ar.route(spec, config, probes=state, hold=True),
                ["first concurrent dispatch", "second concurrent dispatch"]))
    finally:
        ar.judge = original
    assert sum(d["pick"]["provider"] == "opencode" for d in decisions) <= 1, \
        [d["pick"] for d in decisions]

    # An older settlement on a reused worktree must leave new confirmed holds intact so the next route uses Codex while OpenCode's new worker still owns its room.
    ar.save_json(ar.STATE, {})
    state = probes()
    for i in range(CONFIG["max_inflight"]["opencode"]):
        ar.reserve("opencode", ar.dispatch_cost(CONFIG, "opencode", 1), 1,
                   f"new task {i} still running", 1800,
                   "reused-checkout" if i == 0 else f"active-worker-{i}")
    original_workers, original_tasks = ar.orca_settled, ar.orca_settled_tasks
    ar.orca_settled = lambda run: ([
        {"dispatch": "older-finished-dispatch", "state": "succeeded",
         "worktree": "reused-checkout"}], None)
    ar.orca_settled_tasks = lambda run: {ar.brief_key("older finished task")}
    try:
        ar.release_settled()
    finally:
        ar.orca_settled, ar.orca_settled_tasks = original_workers, original_tasks
    decision = route_with(judged("implementation"), state)
    assert decision["pick"]["provider"] == "codex", decision["pick"]




if __name__ == "__main__":
    main()
