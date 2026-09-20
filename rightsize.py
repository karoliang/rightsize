#!/usr/bin/env python3
"""rightsize: pick the subagent provider and model for a task.

Quota is arithmetic and lives in code. What kind of work a task is, is a
judgment and goes to Jev. The model is never asked which provider to use.

Commands:
  rightsize probe [--json]        live headroom for every provider
  rightsize refresh               pull the model catalogues into registry.json
  rightsize route --task "..."    decide provider + model for one task
  rightsize route --spec FILE     same, reading the spec from a file
  rightsize models [--band N]     what the current registry offers
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
REGISTRY = ROOT / "registry.json"
STATE = HOME / ".local/state/rightsize/state.json"

OPENCODE_AUTH = HOME / ".local/share/opencode/auth.json"
OPENCODE_USAGE = "https://opencode.ai/zen/go/v1/usage"
OPENCODE_GO_MODELS = "https://opencode.ai/zen/go/v1/models"
OPENCODE_ZEN_MODELS = "https://opencode.ai/zen/v1/models"
MODELS_DEV = "https://models.opencode.ai/api.json"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
CODEX_SESSIONS = HOME / ".codex/sessions"
CODEX_MODELS = HOME / ".codex/models_cache.json"
CLAUDE_PROJECTS = HOME / ".claude/projects"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"

CLAUDE_MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5-1"]


# --------------------------------------------------------------------------
# plumbing


def now() -> float:
    return time.time()


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def get(url: str, token: str | None = None, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "rightsize/1"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def post(url: str, token: str, body: dict, timeout: int = 60):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "rightsize/1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def secret(name: str) -> str | None:
    """Environment first, then Infisical, then the OpenCode credential file.

    Infisical is only consulted when this repo is linked (.infisical.json) and
    the CLI is present, so a machine without it still works.
    """
    value = os.environ.get(name)
    if value:
        return value
    if (ROOT / ".infisical.json").exists():
        try:
            out = subprocess.run(
                ["infisical", "secrets", "get", name, "--plain", "--silent"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=25,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if name == "OPENCODE_API_KEY":
        auth = load_json(OPENCODE_AUTH, {}) or {}
        return (auth.get("opencode-go") or {}).get("key")
    if name == "OPENCODE_ZEN_API_KEY":
        auth = load_json(OPENCODE_AUTH, {}) or {}
        return (auth.get("opencode") or {}).get("key")
    return None


def iso_to_epoch(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def human_reset(epoch: float | None) -> str:
    if not epoch:
        return "unknown"
    delta = int(epoch - now())
    if delta <= 0:
        return "now"
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# --------------------------------------------------------------------------
# probes
#
# A probe returns {name, buckets, status}. A bucket is
# {id, percent, resets_at, source}. percent is percent USED, so headroom is
# 100 - percent. percent None means the number is genuinely unknown, which is
# different from zero and the policy treats it differently.


def probe_opencode() -> dict:
    key = secret("OPENCODE_API_KEY")
    if not key:
        return {"name": "opencode", "status": "no-credential", "buckets": []}
    try:
        data = get(OPENCODE_USAGE, key)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"name": "opencode", "status": f"error: {exc}", "buckets": []}
    buckets = []
    for bucket_id, value in (data.get("usage") or {}).items():
        ok = value.get("status") == "ok"
        buckets.append(
            {
                "id": bucket_id,
                "percent": value.get("percent") if ok else None,
                "resets_at": iso_to_epoch(value.get("resetsAt")),
                "source": "live",
                "raw_status": value.get("status"),
            }
        )
    return {"name": "opencode", "status": "ok" if buckets else "empty", "buckets": buckets}


def newest_codex_rollout() -> Path | None:
    if not CODEX_SESSIONS.exists():
        return None
    newest, newest_mtime = None, 0.0
    cutoff = now() - 30 * 86400
    for path in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    return newest


def probe_codex() -> dict:
    path = newest_codex_rollout()
    if not path:
        return {"name": "codex", "status": "no-session-data", "buckets": []}
    found = None
    try:
        with path.open() as handle:
            for line in handle:
                if '"rate_limits"' not in line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                limits = find_key(payload, "rate_limits")
                if limits:
                    found = limits
    except OSError as exc:
        return {"name": "codex", "status": f"error: {exc}", "buckets": []}
    if not found:
        return {"name": "codex", "status": "no-rate-limits", "buckets": []}
    buckets = []
    for slot in ("primary", "secondary"):
        value = found.get(slot)
        if not value:
            continue
        minutes = value.get("window_minutes") or 0
        buckets.append(
            {
                "id": f"{slot}-{minutes}m",
                "percent": value.get("used_percent"),
                "resets_at": value.get("resets_at"),
                # Written only when Codex runs, so usage can only be higher.
                "source": "stale-lower-bound",
                "observed_at": path.stat().st_mtime,
            }
        )
    return {"name": "codex", "status": "ok" if buckets else "empty", "buckets": buckets}


def find_key(node, key):
    """Depth-first search for a key in nested dicts and lists."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            hit = find_key(value, key)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for value in node:
            hit = find_key(value, key)
            if hit is not None:
                return hit
    return None


def claude_tokens(window_seconds: int) -> int:
    """Tokens billed to this account inside the trailing window.

    Claude Code keeps no usage cache, so this is reconstructed from transcript
    token counts. Cache reads are excluded: they are charged at a fraction and
    counting them would badly overstate usage.
    """
    if not CLAUDE_PROJECTS.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    file_cutoff = now() - window_seconds - 86400
    total = 0
    for path in CLAUDE_PROJECTS.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < file_cutoff:
                continue
            with path.open() as handle:
                for line in handle:
                    if '"usage"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    stamp = entry.get("timestamp")
                    if not stamp:
                        continue
                    when = iso_to_epoch(stamp)
                    if when is None or when < cutoff.timestamp():
                        continue
                    usage = find_key(entry, "usage") or {}
                    total += int(usage.get("input_tokens") or 0)
                    total += int(usage.get("output_tokens") or 0)
                    total += int(usage.get("cache_creation_input_tokens") or 0)
        except OSError:
            continue
    return total


def probe_claude(config: dict) -> dict:
    settings = config.get("claude") or {}
    buckets = []
    for bucket_id, seconds, budget_key in (
        ("rolling-5h", 5 * 3600, "rolling_token_budget"),
        ("weekly", 7 * 86400, "weekly_token_budget"),
    ):
        budget = settings.get(budget_key)
        used = claude_tokens(seconds)
        percent = round(100.0 * used / budget, 1) if budget else None
        buckets.append(
            {
                "id": bucket_id,
                "percent": percent,
                "resets_at": None,
                "source": "computed" if budget else "no-budget-set",
                "tokens_used": used,
            }
        )
    return {"name": "claude", "status": "ok", "buckets": buckets}


def probe_openrouter(config: dict) -> dict:
    key = secret("OPENROUTER_API_KEY")
    if not key:
        return {"name": "openrouter", "status": "no-credential", "buckets": []}
    try:
        data = (get(OPENROUTER_KEY_URL, key) or {}).get("data") or {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"name": "openrouter", "status": f"error: {exc}", "buckets": []}
    buckets = []
    limit = data.get("limit")
    if limit:
        used = data.get("usage") or 0
        buckets.append(
            {
                "id": "credit",
                "percent": round(100.0 * used / limit, 1),
                "resets_at": None,
                "source": "live",
            }
        )
    cap = (config.get("openrouter") or {}).get("free_requests_per_day") or 1000
    used_today, resets_at = free_requests_today()
    buckets.append(
        {
            "id": "free-requests-day",
            "percent": round(100.0 * used_today / cap, 1),
            "resets_at": resets_at,
            "source": "local-count",
            "requests_used": used_today,
            "requests_cap": cap,
        }
    )
    return {"name": "openrouter", "status": "ok", "buckets": buckets}


def free_requests_today() -> tuple[int, float]:
    state = load_json(STATE, {}) or {}
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counter = state.get("openrouter_free", {})
    used = counter.get("count", 0) if counter.get("day") == day else 0
    tomorrow = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    return used, tomorrow.timestamp()


def count_free_request() -> None:
    state = load_json(STATE, {}) or {}
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counter = state.get("openrouter_free", {})
    count = counter.get("count", 0) if counter.get("day") == day else 0
    state["openrouter_free"] = {"day": day, "count": count + 1}
    save_json(STATE, state)


def probe_free(name: str) -> dict:
    """A provider with no meter at all.

    OpenCode Zen's `-free` models are served to the OpenCode client without
    touching the Go subscription's percent buckets, so they cost no quota. They
    are noticeably slower than the paid cheap band, which is why the ladder
    keeps them last: they are overflow capacity, not the default.
    """
    return {"name": name, "status": "ok", "buckets": [], "free": True}


def probe_all(config: dict) -> dict:
    probes = {
        "opencode": probe_opencode(),
        "codex": probe_codex(),
        "claude": probe_claude(config),
        "openrouter": probe_openrouter(config),
    }
    for name in config.get("free_providers", []):
        probes[name] = probe_free(name)
    return probes


# --------------------------------------------------------------------------
# eligibility: pure arithmetic over probe output


def record_snapshot(probes: dict) -> dict:
    """Keep the previous reading so a burn rate can be computed without history."""
    state = load_json(STATE, {}) or {}
    previous = state.get("snapshots", {})
    current = {}
    for name, probe in probes.items():
        for bucket in probe["buckets"]:
            if bucket.get("percent") is not None:
                current[f"{name}:{bucket['id']}"] = {"at": now(), "percent": bucket["percent"]}
    state["snapshots"] = current
    save_json(STATE, state)
    return previous


def headroom(probe: dict, reserve: float, previous: dict, name: str) -> dict:
    """Binding bucket = the one with least usable headroom.

    Returns usable headroom in percentage points after the reserve, the epoch
    at which the binding bucket resets, and whether the current burn rate
    projects past 100 percent before that reset.
    """
    worst = None
    unknown = False
    for bucket in probe["buckets"]:
        percent = bucket.get("percent")
        if percent is None:
            unknown = True
            continue
        free = 100.0 - percent - reserve
        if worst is None or free < worst["free"]:
            worst = {"free": free, "bucket": bucket}
    if worst is None:
        return {
            "usable": None,
            "unknown": True,
            "resets_at": None,
            "bucket": None,
            "overrun": False,
        }
    bucket = worst["bucket"]
    overrun = False
    key = f"{name}:{bucket['id']}"
    before = previous.get(key)
    resets_at = bucket.get("resets_at")
    if before and resets_at:
        elapsed = now() - before["at"]
        climb = bucket["percent"] - before["percent"]
        if elapsed > 60 and climb > 0:
            rate = climb / elapsed
            projected = bucket["percent"] + rate * max(0.0, resets_at - now())
            overrun = projected > 100.0
    return {
        "usable": worst["free"],
        "unknown": unknown,
        "resets_at": resets_at,
        "bucket": bucket["id"],
        "overrun": overrun,
    }


def exhausted_until(name: str) -> float:
    state = load_json(STATE, {}) or {}
    return (state.get("exhausted") or {}).get(name, 0)


def mark_exhausted(name: str, until: float) -> None:
    state = load_json(STATE, {}) or {}
    state.setdefault("exhausted", {})[name] = until
    save_json(STATE, state)


def eligibility(config: dict, probes: dict) -> dict:
    previous = record_snapshot(probes)
    reserves = config.get("reserves", {})
    out = {}
    for name, probe in probes.items():
        info = headroom(probe, float(reserves.get(name, 10)), previous, name)
        info["free"] = bool(probe.get("free"))
        blocked = None
        if probe["status"].startswith("error") or probe["status"] == "no-credential":
            blocked = probe["status"]
        elif exhausted_until(name) > now():
            blocked = f"quota error, retry after {human_reset(exhausted_until(name))}"
        elif info["usable"] is not None and info["usable"] <= 0:
            blocked = f"below reserve on {info['bucket']}"
        out[name] = {
            **info,
            "status": probe["status"],
            "blocked": blocked,
            "eligible": blocked is None,
            "buckets": probe["buckets"],
        }
    return out


# --------------------------------------------------------------------------
# the judgment: Jev when a key exists, a stated heuristic when it does not

QUESTIONS = {
    "tier": {
        "type": "choice",
        "instructions": (
            "A coding task is about to be handed to an autonomous agent working alone in a "
            "git worktree. Which kind of work is it?"
        ),
        "criteria": {
            "mechanical": (
                "The correct result is already determined by the request: a rename, a typo, a "
                "format change, a single obvious substitution."
            ),
            "implementation": (
                "An ordinary feature or fix with a clear target, where correctness can be "
                "checked by running something afterwards."
            ),
            "design": (
                "An architecture, interface or data-model decision has to be made that the "
                "request does not already imply."
            ),
            "diagnosis": (
                "Something is wrong and there is no reliable way to reproduce it yet, so the "
                "work starts by forming and eliminating hypotheses."
            ),
            "high_stakes": (
                "It touches authentication, payments, money figures, database migrations, "
                "secrets, or output a reader or customer sees directly."
            ),
        },
    },
    "size": {
        "type": "score",
        "instructions": "How much of the codebase does carrying this out have to touch?",
        "criteria": [
            "One file, a few lines.",
            "A handful of files inside one package or feature.",
            "Across packages or repositories, or a new subsystem that does not exist yet.",
        ],
    },
    "second_opinion": {
        "type": "noul",
        "instructions": (
            "Would a second, different model reviewing the finished result likely catch "
            "something the model that wrote it would miss?"
        ),
        "criteria": {
            "true": "The work has judgment calls, subtle correctness, or security consequences.",
            "false": "The result is obviously right or obviously wrong on sight.",
        },
    },
    "spec_complete": {
        "type": "noul",
        "instructions": (
            "Could an agent with no access to the conversation that produced this task finish "
            "it from the text alone, without asking a question?"
        ),
        "criteria": {
            "true": "Every file, name, expected behaviour and acceptance check needed is stated.",
            "false": "It relies on context, a decision, or a name that is not written down here.",
        },
    },
    "destructive": {
        "type": "noul",
        "instructions": (
            "Does carrying this out involve a step that cannot be undone by editing code "
            "afterwards?"
        ),
        "criteria": {
            "true": "Dropping or overwriting data, force-pushing, deploying, publishing a figure.",
            "false": "Only source changes, which can be reverted.",
        },
    },
}

HEURISTIC = [
    (r"\bmigrat|\bdrop table|\bauth|\bpassword|\bsecret|\bpayment|\bstripe|\bmoney|\bdeploy", "high_stakes"),
    (r"\bdesign|\barchitect|\bplan\b|\bschema\b|\brefactor across|\bapi shape", "design"),
    (r"\bdebug|\bflaky|\bintermittent|\bcannot reproduce|\bno repro|\bwhy does", "diagnosis"),
    (r"\brename\b|\btypo\b|\bformat\b|\bcomment\b|\blint\b", "mechanical"),
]


def judge(spec: str) -> dict:
    key = secret("TYPESAFE_API_KEY")
    if key:
        body = {"state": spec, "model": "jev-latest", "questions": QUESTIONS}
        try:
            data = post(TYPESAFE_URL, key, body)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {**heuristic(spec), "source": f"heuristic (jev unavailable: {exc})"}
        answers = data.get("answers") or {}
        return {
            "tier": answers["tier"]["choice"],
            "tier_confidence": answers["tier"].get("confidence"),
            "size": answers["size"]["score"],
            "second_opinion": answers["second_opinion"]["noul"],
            "spec_complete": answers["spec_complete"]["noul"],
            "destructive": answers["destructive"]["noul"],
            "source": f"jev ({data.get('model')})",
            "usage": data.get("usage"),
        }
    return {**heuristic(spec), "source": "heuristic (no TYPESAFE_API_KEY)"}


def heuristic(spec: str) -> dict:
    """Deliberately crude stand-in. It states that it is a stand-in so a bad
    route is never mistaken for a model's judgment."""
    text = spec.lower()
    tier = "implementation"
    for pattern, label in HEURISTIC:
        if re.search(pattern, text):
            tier = label
            break
    size = 2.0 if re.search(r"\bacross\b|\ball \b|\bevery \b|\brepo-wide\b", text) else 0.6
    return {
        "tier": tier,
        "tier_confidence": None,
        "size": size,
        "second_opinion": 0.7 if tier in ("high_stakes", "design", "diagnosis") else 0.2,
        "spec_complete": 0.6,
        "destructive": 0.7 if tier == "high_stakes" else 0.1,
    }


# --------------------------------------------------------------------------
# policy


def band_for(judgment: dict, config: dict) -> tuple[int, list[str]]:
    thresholds = config.get("thresholds", {})
    reasons = []
    tier = judgment["tier"]
    band = {"mechanical": 1, "implementation": 1, "design": 3, "diagnosis": 3, "high_stakes": 3}.get(tier, 1)
    reasons.append(f"tier {tier} starts at band {band}")
    if band < 3 and judgment["size"] >= float(thresholds.get("size_escalates_band", 1.5)):
        band += 1
        reasons.append(f"size {judgment['size']:.2f} raises it to band {band}")
    if band < 3 and judgment["second_opinion"] >= float(thresholds.get("second_opinion_min", 0.6)):
        reasons.append("a second opinion is worth having, review leg added")
    return band, reasons


def parse_candidate(text: str) -> dict:
    parts = text.split(":")
    if len(parts) == 2:
        provider, model, effort = parts[0], parts[1], None
    elif len(parts) == 3 and parts[2] in ("low", "medium", "high", "xhigh", "max", "ultra"):
        provider, model, effort = parts
    else:
        # OpenRouter ids contain a slash and may carry their own ":free" suffix.
        provider, model, effort = parts[0], ":".join(parts[1:]), None
    return {"provider": provider, "model": model, "effort": effort}


def pick(candidates: list[str], elig: dict, band: int) -> tuple[dict | None, list[str]]:
    """Among eligible candidates, spend the bucket that expires first."""
    notes = []
    usable = []
    for index, text in enumerate(candidates):
        cand = parse_candidate(text)
        info = elig.get(cand["provider"])
        if not info:
            continue
        if not info["eligible"]:
            notes.append(f"{text} skipped: {info['blocked']}")
            continue
        if info["usable"] is None and band < 3 and not info.get("free"):
            notes.append(f"{text} skipped: headroom unknown, escalation only")
            continue
        if info["overrun"] and band < 3:
            notes.append(f"{text} skipped: current burn rate overruns its bucket before reset")
            continue
        resets = info["resets_at"] or float("inf")
        usable.append((resets, index, cand, text, info))
    if not usable:
        return None, notes
    usable.sort(key=lambda row: (row[0], row[1]))
    resets, _, cand, text, info = usable[0]
    if resets != float("inf"):
        notes.append(
            f"{text} chosen: its {info['bucket']} bucket resets in {human_reset(resets)} "
            f"with {info['usable']:.0f} points usable, so spend it before it expires"
        )
    elif info.get("free"):
        notes.append(f"{text} chosen: costs no quota at all, so nothing metered is spent")
    else:
        notes.append(f"{text} chosen: first eligible candidate in band {band}")
    return cand, notes


def route(spec: str, config: dict, probes: dict | None = None) -> dict:
    probes = probes if probes is not None else probe_all(config)
    elig = eligibility(config, probes)
    judgment = judge(spec)
    band, reasons = band_for(judgment, config)
    thresholds = config.get("thresholds", {})

    blocked = None
    if judgment["spec_complete"] < float(thresholds.get("spec_complete_min", 0.5)):
        blocked = (
            f"spec is not self-contained (spec_complete {judgment['spec_complete']:.2f}); "
            "tighten the brief before dispatching it to any worker"
        )

    ladders = config["bands"]
    chosen, notes = pick(ladders[str(band)], elig, band)
    used_band = band
    while chosen is None and used_band > 1:
        used_band -= 1
        reasons.append(f"nothing eligible in band {used_band + 1}, dropping to band {used_band}")
        chosen, more = pick(ladders[str(used_band)], elig, used_band)
        notes += more
    if chosen is None and band < 3:
        reasons.append("nothing eligible below band 3, escalating instead of failing")
        chosen, more = pick(ladders["3"], elig, 3)
        notes += more
        used_band = 3

    review = None
    if judgment["second_opinion"] >= float(thresholds.get("second_opinion_min", 0.6)) and chosen:
        others = [c for c in config["review_ladder"] if parse_candidate(c)["provider"] != chosen["provider"]]
        review, review_notes = pick(others, elig, 1)
        notes += [f"review: {n}" for n in review_notes]

    confirm = judgment["destructive"] >= float(thresholds.get("destructive_min", 0.5))
    if confirm:
        reasons.append(
            f"destructive {judgment['destructive']:.2f}: confirm with a human before the "
            "irreversible step, do not dispatch it unattended"
        )

    return {
        "blocked": blocked,
        "band": used_band,
        "judgment": judgment,
        "pick": chosen,
        "agent": config["agents"].get(chosen["provider"]) if chosen else None,
        "review": review,
        "confirm_first": confirm,
        "reasons": reasons,
        "notes": notes,
        "quota": {
            name: {
                "usable": info["usable"],
                "bucket": info["bucket"],
                "resets_in": human_reset(info["resets_at"]),
                "eligible": info["eligible"],
                "blocked": info["blocked"],
            }
            for name, info in elig.items()
        },
    }


def orca_command(decision: dict, spec_path: str | None) -> str:
    cand = decision["pick"]
    if not cand:
        return "# no eligible provider"
    agent = decision["agent"]
    spec = f'--spec "$(cat {spec_path})"' if spec_path else '--spec "<task>"'
    line = f"orca orchestration worker-start {spec} --worktree current --agent {agent} --json"
    prefixes = {"opencode": "opencode-go/", "openrouter": "openrouter/", "opencode_zen": "opencode/"}
    if cand["provider"] in prefixes:
        line += f"\n# then inside that terminal: opencode -m {prefixes[cand['provider']]}{cand['model']}"
    elif cand["effort"]:
        line += f" --model {cand['model']} --effort {cand['effort']}"
    return line


# --------------------------------------------------------------------------
# registry refresh


def deals(previous: dict, registry: dict) -> dict:
    """What changed in the catalogues, and what is free right now.

    A free or newly discounted model is the cheapest way to move work off a
    metered bucket, so the daily job exists mostly to notice these.
    """
    free, drops, added, removed = [], [], [], []
    old_providers = (previous or {}).get("providers", {})
    for name, provider in registry["providers"].items():
        old_models = (old_providers.get(name) or {}).get("models") or {}
        for model_id, model in provider["models"].items():
            price = model.get("input")
            if price == 0 or model.get("free") or model_id.endswith(("-free", ":free")):
                free.append({"provider": name, "model": model_id, "context": model.get("context")})
            if model_id not in old_models:
                added.append({"provider": name, "model": model_id, "input": price})
                continue
            was = old_models[model_id].get("input")
            if was is not None and price is not None and price < was:
                drops.append(
                    {
                        "provider": name,
                        "model": model_id,
                        "from": was,
                        "to": price,
                        "percent": round(100.0 * (was - price) / was, 1) if was else None,
                    }
                )
        for model_id in old_models:
            if model_id not in provider["models"]:
                removed.append({"provider": name, "model": model_id})
    free.sort(key=lambda row: -(row["context"] or 0))
    drops.sort(key=lambda row: -(row["percent"] or 0))
    return {"free": free, "price_drops": drops, "new_models": added, "removed_models": removed}


def refresh() -> dict:
    previous = load_json(REGISTRY, {}) or {}
    registry = {"fetched_at": datetime.now(timezone.utc).isoformat(), "providers": {}}
    catalogue = {}
    try:
        catalogue = get(MODELS_DEV, timeout=40)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        registry["models_dev_error"] = str(exc)

    def priced(provider_key: str) -> dict:
        out = {}
        for model_id, model in (catalogue.get(provider_key, {}).get("models") or {}).items():
            cost = model.get("cost") or {}
            limit = model.get("limit") or {}
            out[model_id] = {
                "name": model.get("name"),
                "input": cost.get("input"),
                "output": cost.get("output"),
                "context": limit.get("context"),
                "max_output": limit.get("output"),
                "reasoning": model.get("reasoning"),
                "tool_call": model.get("tool_call"),
            }
        return out

    go_key = secret("OPENCODE_API_KEY")
    go_available = []
    if go_key:
        try:
            go_available = [m["id"] for m in get(OPENCODE_GO_MODELS, go_key)["data"]]
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            registry["opencode_error"] = str(exc)
    go_priced = priced("opencode-go")
    registry["providers"]["opencode"] = {
        "billing": "subscription percent buckets",
        "models": {mid: go_priced.get(mid, {}) for mid in go_available} or go_priced,
    }

    zen_key = secret("OPENCODE_ZEN_API_KEY")
    zen_available = []
    if zen_key:
        try:
            zen_available = [m["id"] for m in get(OPENCODE_ZEN_MODELS, zen_key)["data"]]
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            registry["zen_error"] = str(exc)
    zen_priced = priced("opencode")
    registry["providers"]["opencode_zen"] = {
        "billing": "prepaid credit",
        "models": {mid: zen_priced.get(mid, {}) for mid in zen_available},
    }

    codex = load_json(CODEX_MODELS, {}) or {}
    registry["providers"]["codex"] = {
        "billing": "subscription percent buckets",
        "fetched_at": codex.get("fetched_at"),
        "models": {
            m["slug"]: {
                "name": m.get("display_name"),
                "context": m.get("context_window"),
                "max_context": m.get("max_context_window"),
                "efforts": [r["effort"] for r in m.get("supported_reasoning_levels", [])],
                "default_effort": m.get("default_reasoning_level"),
                "hidden": m.get("visibility") == "hide",
            }
            for m in codex.get("models", [])
        },
    }

    registry["providers"]["claude"] = {
        "billing": "subscription, no quota API",
        "models": {mid: {} for mid in CLAUDE_MODELS},
    }

    or_key = secret("OPENROUTER_API_KEY")
    or_models = {}
    if or_key:
        try:
            for model in get(OPENROUTER_MODELS, or_key, timeout=40)["data"]:
                pricing = model.get("pricing") or {}
                is_free = model["id"].endswith(":free")
                or_models[model["id"]] = {
                    "name": model.get("name"),
                    # OpenRouter prices per token; convert to per million.
                    "input": round(float(pricing.get("prompt", 0)) * 1e6, 4),
                    "output": round(float(pricing.get("completion", 0)) * 1e6, 4),
                    "context": model.get("context_length"),
                    "free": is_free,
                }
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            registry["openrouter_error"] = str(exc)
    registry["providers"]["openrouter"] = {
        "billing": "free tier daily request cap, then credit",
        "models": or_models,
    }

    registry["deals"] = deals(previous, registry)
    save_json(REGISTRY, registry)
    return registry


# --------------------------------------------------------------------------
# cli


def cmd_probe(args, config):
    probes = probe_all(config)
    elig = eligibility(config, probes)
    if args.json:
        print(json.dumps({"probes": probes, "eligibility": elig}, indent=2))
        return 0
    for name, info in elig.items():
        mark = "ok " if info["eligible"] else "BLOCKED"
        usable = "unknown" if info["usable"] is None else f"{info['usable']:.0f} pts"
        print(f"{name:<11} {mark:<8} usable {usable:<9} binding {info['bucket'] or '-':<18} resets {human_reset(info['resets_at'])}")
        for bucket in info["buckets"]:
            percent = "-" if bucket["percent"] is None else f"{bucket['percent']}%"
            extra = bucket.get("tokens_used")
            extra = f" ({extra:,} tokens)" if extra else ""
            print(f"    {bucket['id']:<20} used {percent:<7} {bucket['source']}{extra}")
        if info["blocked"]:
            print(f"    -> {info['blocked']}")
    return 0


def cmd_refresh(args, config):
    registry = refresh()
    for name, provider in registry["providers"].items():
        print(f"{name:<15} {len(provider['models'])} models")
    for key in [k for k in registry if k.endswith("_error")]:
        print(f"warning: {key}: {registry[key]}")
    print(f"written to {REGISTRY}")
    return cmd_deals(args, config)


def cmd_deals(args, config):
    registry = load_json(REGISTRY)
    if not registry or "deals" not in registry:
        print("no deals yet, run: rightsize refresh", file=sys.stderr)
        return 1
    found = registry["deals"]
    limit = getattr(args, "limit", 12)
    print(f"\n# free right now ({len(found['free'])})")
    for row in found["free"][:limit]:
        print(f"  {row['provider']:<13} {row['model']:<40} ctx {row['context'] or '-'}")
    if found["price_drops"]:
        print(f"\n# price drops since last refresh ({len(found['price_drops'])})")
        for row in found["price_drops"][:limit]:
            print(f"  {row['provider']:<13} {row['model']:<40} {row['from']} -> {row['to']} ({row['percent']}% off)")
    if found["new_models"]:
        print(f"\n# new models ({len(found['new_models'])})")
        for row in found["new_models"][:limit]:
            print(f"  {row['provider']:<13} {row['model']:<40} {row['input']}")
    if found["removed_models"]:
        print(f"\n# gone ({len(found['removed_models'])})")
        for row in found["removed_models"][:limit]:
            print(f"  {row['provider']:<13} {row['model']}")
    return 0


def cmd_route(args, config):
    spec = args.task or Path(args.spec).read_text()
    decision = route(spec, config)
    if args.json:
        print(json.dumps(decision, indent=2))
        return 0
    judgment = decision["judgment"]
    print(f"judgment   {judgment['source']}")
    print(
        f"           tier={judgment['tier']} size={judgment['size']:.2f} "
        f"second_opinion={judgment['second_opinion']:.2f} "
        f"spec_complete={judgment['spec_complete']:.2f} "
        f"destructive={judgment['destructive']:.2f}"
    )
    if decision["blocked"]:
        print(f"BLOCKED    {decision['blocked']}")
    cand = decision["pick"]
    if cand:
        effort = f" effort={cand['effort']}" if cand["effort"] else ""
        print(f"dispatch   band {decision['band']} -> agent {decision['agent']}, model {cand['model']}{effort}")
    else:
        print("dispatch   nothing eligible, every provider is blocked")
    if decision["review"]:
        review = decision["review"]
        print(f"review     {review['provider']} {review['model']}")
    if decision["confirm_first"]:
        print("confirm    irreversible step, ask a human first")
    for reason in decision["reasons"]:
        print(f"  why      {reason}")
    for note in decision["notes"]:
        print(f"  quota    {note}")
    if args.orca:
        print()
        print(orca_command(decision, args.spec))
    return 0 if cand and not decision["blocked"] else 1


def cmd_models(args, config):
    registry = load_json(REGISTRY)
    if not registry:
        print("no registry.json yet, run: rightsize refresh", file=sys.stderr)
        return 1
    for name, provider in registry["providers"].items():
        rows = []
        for model_id, model in provider["models"].items():
            price = model.get("input")
            rows.append((price if price is not None else 999, model_id, model))
        rows.sort()
        print(f"# {name} ({provider['billing']}), {len(rows)} models")
        for price, model_id, model in rows[: args.limit]:
            cost = "-" if model.get("input") is None else f"{model['input']}/{model.get('output')}"
            print(f"  {model_id:<34} {cost:<14} ctx {model.get('context') or '-'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rightsize", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="live headroom for every provider")
    probe.add_argument("--json", action="store_true")
    probe.set_defaults(func=cmd_probe)

    refresh_cmd = sub.add_parser("refresh", help="pull model catalogues into registry.json")
    refresh_cmd.add_argument("--limit", type=int, default=12, help="rows per deals section")
    refresh_cmd.set_defaults(func=cmd_refresh)

    route_cmd = sub.add_parser("route", help="decide provider and model for one task")
    group = route_cmd.add_mutually_exclusive_group(required=True)
    group.add_argument("--task", help="task description inline")
    group.add_argument("--spec", help="file holding the task spec")
    route_cmd.add_argument("--json", action="store_true")
    route_cmd.add_argument("--orca", action="store_true", help="also print the worker-start command")
    route_cmd.set_defaults(func=cmd_route)

    models = sub.add_parser("models", help="what the current registry offers")
    models.add_argument("--limit", type=int, default=12)
    models.set_defaults(func=cmd_models)

    deals_cmd = sub.add_parser("deals", help="free models, price drops and catalogue changes")
    deals_cmd.add_argument("--limit", type=int, default=12)
    deals_cmd.set_defaults(func=cmd_deals)

    args = parser.parse_args(argv)
    config = load_json(CONFIG)
    if config is None:
        print(f"config missing: {CONFIG}", file=sys.stderr)
        return 2
    return args.func(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
