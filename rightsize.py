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
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from native_rpc import read_rate_limits
import accounts

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
REPO_CONFIG = ".rightsize.json"
REGISTRY = ROOT / "registry.json"
STATE = HOME / ".local/state/rightsize/state.json"

OPENCODE_AUTH = HOME / ".local/share/opencode/auth.json"
OPENCODE_USAGE = "https://opencode.ai/zen/go/v1/usage"
OPENCODE_GO_MODELS = "https://opencode.ai/zen/go/v1/models"
OPENCODE_ZEN_MODELS = "https://opencode.ai/zen/v1/models"
MODELS_DEV = "https://models.opencode.ai/api.json"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_CREDITS = "https://openrouter.ai/api/v1/credits"
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
# Historical rollout/catalogue discovery only. Live quota selection is owned by
# accounts.select; rollout location does not establish account provenance.
ORCA_CODEX_ACCOUNTS = HOME / "Library/Application Support/orca/codex-accounts"
ORCA_SUPPORT = HOME / "Library/Application Support/orca"
# How old a remembered reading may be before the window counts as unknown.
STALE_READING = 6 * 3600
OPENCODE_DB = HOME / ".local/share/opencode/opencode.db"
# Providers whose worker is launched by the opencode CLI, so its sessions land
# in opencode's own database and can be checked after the fact.
OPENCODE_LAUNCHED = ("opencode", "opencode_zen", "openrouter")
# Windows a bucket id implies, in seconds. Codex spells its own in the id.
BUCKET_WINDOWS = {"rolling": 5 * 3600, "weekly": 7 * 86400, "monthly": 30 * 86400,
                  "credit": None, "key-credit": None, "account-credit": None,
                  "free-requests-day": 86400}


def codex_homes() -> list[Path]:
    """Every directory Codex might have written a rollout to."""
    candidates = [os.environ.get("CODEX_HOME"), os.environ.get("ORCA_CODEX_HOME"), HOME / ".codex"]
    try:
        candidates += sorted(ORCA_CODEX_ACCOUNTS.glob("*/home"))
    except OSError:
        pass
    roots, seen = [], set()
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        key = str(path)
        if key not in seen:
            seen.add(key)
            roots.append(path)
    return roots


CODEX_MODELS = HOME / ".codex/models_cache.json"
CLAUDE_PROJECTS = HOME / ".claude/projects"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"

CLAUDE_MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5-1"]


# --------------------------------------------------------------------------
# plumbing


def now() -> float:
    return time.time()


def merge(base, overlay):
    """Deep-merge for config: dicts merge key by key, everything else replaces.

    A list replaces rather than appends on purpose: a repo pinning a band ladder
    means "these candidates", not "these as well as whatever was there".
    """
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for key, value in overlay.items():
            out[key] = merge(out.get(key), value) if key in out else value
        return out
    return overlay


def repo_config(start: Path | None = None) -> Path | None:
    """The nearest .rightsize.json at or above the working directory.

    A project has rules of its own, and they belong with the project rather than
    in one user's home. money.financial is the case that forced this: it
    requires every unsupervised worker to get its own worktree, and nothing in
    a user-level config could say so.

    Note what this is: a file in a repository that can change the commands
    rightsize prints. Treat it like a Makefile, and read it before running a
    repo you did not write.
    """
    here = (start or Path.cwd()).resolve()
    for directory in [here, *here.parents]:
        candidate = directory / REPO_CONFIG
        if candidate.is_file():
            return candidate
    return None


def load_config() -> tuple[dict | None, Path | None]:
    base = load_json(CONFIG)
    if base is None:
        return None, None
    found = repo_config()
    if not found:
        return base, None
    overlay = load_json(found)
    if not isinstance(overlay, dict):
        return base, None
    return merge(base, overlay), found


def load_json(path: Path, default=None):
    try:
        value = json.loads(path.read_text())
        if path == STATE and path.name == "state.v2.json" and (
                not isinstance(value, dict) or value.get("_rightsize_writer_generation") != 2):
            raise ValueError("invalid migrated state")
        return value
    except (OSError, ValueError):
        if path == STATE and path.name == "state.v2.json":
            from managed_ledger import LedgerError
            raise LedgerError("migrated state unavailable; preserve evidence and recover migration") from None
        return default


@contextlib.contextmanager
def state_lock(*, transition=False):
    """Hold the state file for a read-modify-write.

    Every mutator here loads the whole document, changes one field and writes
    it back, and several can run at once: a hook on every Bash command, a batch
    reserving in a loop, and each worker terminal's own agent. Without this,
    twelve concurrent writes lost eleven of them and left the file unparseable,
    after which every load returned {} and the next write erased the rest.
    """
    STATE.parent.mkdir(parents=True, exist_ok=True)
    lock = STATE.with_name("state.json.lock" if STATE.name == "state.v2.json" else STATE.name + ".lock")
    handle = open(lock, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if not transition:
            import state_migration
            if state_migration.active_path(STATE) != STATE:
                from managed_ledger import LedgerError
                raise LedgerError("state generation changed; restart this invocation")
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def save_json(path: Path, value) -> None:
    """Write whole or not at all.

    Writing in place leaves the file empty or half-written for as long as the
    write takes, and a reader landing there gets nothing back and then saves
    that nothing over the document. A temporary file in the same directory
    followed by os.replace is atomic, so a reader sees the old file or the new
    one and never a partial one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


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


CREDENTIAL_NAMES = frozenset({"TYPESAFE_API_KEY", "OPENROUTER_API_KEY",
                              "OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY"})


def vault_scope() -> tuple[str, str, str] | None:
    """Read metadata only. Missing scope never means the CLI's default scope."""
    link = load_json(ROOT / ".infisical.json", {})
    if not isinstance(link, dict):
        return None
    fields = tuple(link.get(key) for key in
                   ("workspaceId", "defaultEnvironment", "rightsizePath"))
    if not all(isinstance(value, str) and value.strip() and
               not any(char in value for char in "\r\n\x00") for value in fields):
        return None
    if not fields[2].startswith("/") or ".." in fields[2].split("/"):
        return None
    return fields


def secret(name: str, config: dict | None = None) -> str | None:
    """Resolve one allowlisted value without exporting or evaluating shell text.

    An explicitly linked vault is authoritative after environment variables:
    incomplete scope or failed reads never switch to a different native key.
    """
    if name not in CREDENTIAL_NAMES:
        return None
    provider = {"OPENCODE_API_KEY": "opencode", "OPENCODE_ZEN_API_KEY": "opencode_zen",
                "OPENROUTER_API_KEY": "openrouter"}.get(name)
    binding = accounts.select(provider, config) if provider and config is not None else None
    if binding and binding.status != "unverified":
        return None
    native_selected = binding and binding.source == "configured-native"
    value = os.environ.get(name)
    if value:
        return value
    if not native_selected and (ROOT / ".infisical.json").exists():
        scope = vault_scope()
        if scope is None:
            return None
        project, environment, path = scope
        try:
            out = subprocess.run(
                ["infisical", "secrets", "get", name, "--plain", "--silent",
                 "--projectId", project, "--env", environment, "--path", path,
                 "--include-imports=false", "--expand=false",
                 "--secret-overriding=false"],
                cwd=ROOT, capture_output=True, text=True, timeout=25,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return None
    if name in ("OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY"):
        auth_path = binding.home / "opencode/auth.json" if binding else OPENCODE_AUTH
        auth = load_json(auth_path, {}) or {}
        provider = "opencode-go" if name == "OPENCODE_API_KEY" else "opencode"
        entry = auth.get(provider) if isinstance(auth, dict) else None
        return entry.get("key") if isinstance(entry, dict) else None
    return None


def iso_to_epoch(text: str) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def human_age(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    hours, rem = divmod(int(seconds), 3600)
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}h {rem // 60}m" if hours else f"{rem // 60}m"


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


def probe_opencode(config=None, key=None) -> dict:
    key = secret("OPENCODE_API_KEY", config) if key is None else key
    if not isinstance(key, str) or not key or any(c in key for c in "\r\n\x00"):
        return {"name": "opencode", "status": "no-credential", "buckets": []}
    base = {"name": "opencode", "quota_account_ref": accounts.digest("opencode-go:" + key)}
    try:
        data = get(OPENCODE_USAGE, key)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        status = {401: "reauth-required", 403: "reauth-required", 429: "denied"}.get(
            getattr(exc, "code", None), "error: quota probe unavailable")
        return {**base, "status": status, "buckets": []}
    buckets = []
    for bucket_id, value in (data.get("usage") or {}).items():
        ok = value.get("status") == "ok"
        buckets.append(
            {
                "id": bucket_id,
                "percent": value.get("percent") if ok else None,
                "resets_at": iso_to_epoch(value.get("resetsAt")),
                # A bucket the provider declined to report this minute is not a
                # reading we have lost: the next probe usually has it.
                "source": "live" if ok else "unavailable",
                "raw_status": value.get("status"),
            }
        )
    return {**base, "status": "ok" if buckets else "empty", "buckets": buckets}


def newest_codex_rollout() -> Path | None:
    newest, newest_mtime = None, 0.0
    cutoff = now() - 30 * 86400
    for home in codex_homes():
        sessions = home / "sessions"
        if not sessions.exists():
            continue
        for path in sessions.rglob("rollout-*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = path, mtime
    return newest


def codex_rate_limits(timeout: float = 15.0, binding=None) -> dict | None:
    """Ask Codex itself, rather than reading what it left behind.

    `codex app-server` answers `account/rateLimits/read` with live percentages
    and the account id they belong to. That matters twice over. It needs no
    interactive session, so the number stops ageing between runs; and the
    reading carries its own identity, where a rollout file does not: Orca
    hardlinks session files across account homes, so a `rate_limits` block
    found under one account may have been written by another.
    """
    binding = binding or accounts.select("codex")
    if binding.status != "unverified":
        return {"status": binding.status}
    return read_rate_limits(["codex", "app-server"], timeout=timeout,
                            env=binding.environment())


def probe_codex(config: dict | None = None) -> dict:
    binding = accounts.select("codex", config)
    base = {"name": "codex", "account": binding.public(), "observed_at": now()}
    if binding.status != "unverified":
        return {**base, "status": binding.status, "buckets": []}
    live = codex_rate_limits(binding=binding)
    if accounts.select("codex", config).fingerprint != binding.fingerprint:
        return {**base, "status": "account-changed", "buckets": []}
    if not isinstance(live, dict):
        live = {}
    if isinstance(live.get("accountId"), str) and live["accountId"]:
        base["quota_account_ref"] = accounts.digest("codex:" + live["accountId"])
    permission = live.get("ordinaryUsageAllowed")
    if permission is False:
        return {**base, "status": "denied", "buckets": [
            {"id": "ordinary-usage", "percent": None, "resets_at": None,
             "source": "live", "raw_status": "quota-exceeded"}]}
    if live.get("status"):
        status = live["status"] if live["status"] in ("unknown", "denied", "reauth-required") else "unknown"
        return {**base, "status": status, "buckets": []}
    if isinstance(live.get("rateLimits"), dict) and live["rateLimits"].get("primary"):
        limits = live["rateLimits"]
        buckets = [{"id": "ordinary-usage", "percent": 0 if permission is True else None,
                    "resets_at": None, "source": "live" if permission is True else "unavailable"}]
        for slot in ("primary", "secondary"):
            value = limits.get(slot)
            if not value:
                continue
            if (not isinstance(value, dict) or isinstance(value.get("usedPercent"), bool)
                    or not isinstance(value.get("usedPercent"), (int, float))
                    or not math.isfinite(value["usedPercent"]) or value["usedPercent"] < 0):
                return {**base, "status": "unknown", "buckets": []}
            minutes = value.get("windowDurationMins") or 0
            buckets.append({
                "id": f"{slot}-{minutes}m",
                "percent": value.get("usedPercent"),
                "resets_at": value.get("resetsAt"),
                "source": "live",
                "account": binding.account_ref,
                "plan": limits.get("planType"),
            })
        if buckets:
            return {**base, "status": "ok", "buckets": buckets}
    # Rollouts can be hardlinked between native account homes. Their location
    # does not prove who paid for them, so they are never quota fallback data.
    return {**base, "status": "unknown", "buckets": []}


def codex_buckets(found: dict, observed_at: float, config: dict | None = None) -> list[dict]:
    """Turn one rate_limits block into buckets, with its age taken seriously."""
    age = now() - observed_at
    limit = float(((config or {}).get("staleness_seconds") or {}).get("codex", 21600))
    buckets = []
    for slot in ("primary", "secondary"):
        value = found.get(slot)
        if not value:
            continue
        minutes = value.get("window_minutes") or 0
        percent = value.get("used_percent")
        resets_at = value.get("resets_at")
        # Written only when Codex runs, so usage can only be higher.
        source = "stale-lower-bound"
        if resets_at and resets_at <= now():
            # The window rolled over after this was written. Usage only accrues
            # by running Codex, and running Codex writes a rollout, so a window
            # newer than the newest rollout has nothing spent in it yet.
            window = (minutes or 0) * 60
            while resets_at <= now() and window:
                resets_at += window
            percent, source = 0.0, "post-reset-assumed-zero"
        elif age > limit:
            # An old high reading is the dangerous direction: it blocks a
            # provider that may have reset or been topped up hours ago. Unknown
            # is the honest answer, and unknown already means escalation-only
            # rather than unusable.
            percent, source = None, "expired-reading"
        buckets.append({
            "id": f"{slot}-{minutes}m",
            "percent": percent,
            "resets_at": resets_at,
            "source": source,
            "observed_at": observed_at,
            "age_seconds": round(age),
        })
    return buckets


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


def claude_tokens(window_seconds: int, projects: Path | None = None) -> int:
    """Tokens billed to this account inside the trailing window.

    Claude Code keeps no usage cache, so this is reconstructed from transcript
    token counts. Cache reads are excluded: they are charged at a fraction and
    counting them would badly overstate usage.
    """
    projects = projects or CLAUDE_PROJECTS
    if not projects.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    file_cutoff = now() - window_seconds - 86400
    total = 0
    for path in projects.rglob("*.jsonl"):
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


def probe_claude(config: dict, count_tokens: bool = False) -> dict:
    """Native subscription quota, with a labelled declared-budget fallback.

    The transcript scan is the slowest thing here (seconds, and it grows with
    the size of ~/.claude/projects). With no budget set the percentage is None
    whatever the count says. Prefer native get_usage; scan transcripts only
    when that control is unavailable and a declared budget or count is needed.
    """
    settings = config.get("claude") or {}
    binding = accounts.select("claude", config)
    identity = accounts.claude_identity(binding)
    if identity["status"] != "ok":
        return {"name": "claude", **identity, "buckets": []}
    import native_claude
    native = native_claude.probe(binding, identity)
    if native is not None:
        if (accounts.select("claude", config).fingerprint != binding.fingerprint
                or accounts.claude_identity(binding) != identity):
            return {"name": "claude", "status": "account-changed", "buckets": []}
        return {"name": "claude", **identity, **native}
    buckets = []
    for bucket_id, seconds, budget_key in (
        ("rolling-5h", 5 * 3600, "rolling_token_budget"),
        ("weekly", 7 * 86400, "weekly_token_budget"),
    ):
        budget = settings.get(budget_key)
        used = claude_tokens(seconds, binding.home / "projects") if (budget or count_tokens) else 0
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
    if (accounts.select("claude", config).fingerprint != binding.fingerprint
            or accounts.claude_identity(binding) != identity):
        return {"name": "claude", "status": "account-changed", "buckets": []}
    return {"name": "claude", **identity, "buckets": buckets}


def probe_openrouter(config: dict, key=None) -> dict:
    key = secret("OPENROUTER_API_KEY", config) if key is None else key
    if not key:
        return {"name": "openrouter", "status": "no-credential", "buckets": []}
    try:
        data = (get(OPENROUTER_KEY_URL, key) or {}).get("data") or {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        status = {401: "reauth-required", 403: "reauth-required", 429: "denied"}.get(
            getattr(exc, "code", None), "error: quota probe unavailable")
        return {"name": "openrouter", "status": status, "buckets": []}
    buckets = []
    # Spend. A key can carry its own limit, which is the binding one when set;
    # otherwise the account's credit balance is the ceiling.
    limit = data.get("limit")
    if limit:
        used = data.get("usage") or 0
        buckets.append({
            "id": "key-credit",
            "percent": round(100.0 * used / limit, 1),
            "resets_at": iso_to_epoch(data.get("limit_reset")) if data.get("limit_reset") else None,
            "source": "live",
            "limit_usd": limit,
            "remaining_usd": data.get("limit_remaining"),
        })
    else:
        try:
            credits = (get(OPENROUTER_CREDITS, key) or {}).get("data") or {}
        except (urllib.error.URLError, OSError, ValueError):
            credits = {}
        total = credits.get("total_credits")
        if total:
            spent = credits.get("total_usage") or 0
            buckets.append({
                "id": "account-credit",
                "percent": round(100.0 * spent / total, 1),
                "resets_at": None,
                "source": "live",
                "limit_usd": total,
                "remaining_usd": round(total - spent, 4),
            })

    # Free-model requests. OpenRouter publishes this counter, so read it rather
    # than keeping a local tally: a local count only ever sees the dispatches it
    # was told about, and misses everything else using the same key.
    free = data.get("free_model_daily_requests") or {}
    _, resets_at = free_requests_today()
    if free.get("limit"):
        buckets.append({
            "id": "free-requests-day",
            "percent": round(100.0 * (free.get("used") or 0) / free["limit"], 1),
            "resets_at": resets_at,
            "source": "live",
            "requests_used": free.get("used"),
            "requests_cap": free["limit"],
            "requests_left": free.get("remaining"),
        })
    else:
        cap = (config.get("openrouter") or {}).get("free_requests_per_day") or 1000
        used_today, _ = free_requests_today()
        buckets.append({
            "id": "free-requests-day",
            "percent": round(100.0 * used_today / cap, 1),
            "resets_at": resets_at,
            "source": "local-count",
            "requests_used": used_today,
            "requests_cap": cap,
        })
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
    with state_lock():
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


def probe_all(config: dict, count_tokens: bool = False) -> dict:
    """Every provider at once. They are independent network reads, so serial
    probing just adds their latencies together."""
    jobs = {
        "opencode": lambda: probe_opencode(config),
        "codex": lambda: probe_codex(config),
        "claude": lambda: probe_claude(config, count_tokens),
        "openrouter": lambda: probe_openrouter(config),
    }
    def bound_probe(name, fn):
        if name == "codex":
            return fn()
        binding = accounts.select(name, config)
        base = {"name": name, "account": binding.public(), "observed_at": now()}
        if binding.status != "unverified":
            return {**base, "status": binding.status, "buckets": []}
        variable = {"opencode": "OPENCODE_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(name)
        if (variable and binding.source == "native" and (ROOT / ".infisical.json").exists()):
            key = secret(variable, config)
            scope = vault_scope()
            reference = accounts.digest(json.dumps([name, scope]))[:24]
            base["account"] = {**binding.public(), "source": "scoped-vault",
                               "account_ref": reference,
                               "fingerprint": accounts.digest(key) if key else None}
            base["native_binding"] = binding.public()
            if not key:
                return {**base, "status": "no-credential", "buckets": []}
            result = probe_opencode(config, key) if name == "opencode" else probe_openrouter(config, key)
            return {**result, **base}
        result = fn()
        if accounts.select(name, config).fingerprint != binding.fingerprint:
            return {**base, "status": "account-changed", "buckets": []}
        return {**result, **base}

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(bound_probe, name, fn) for name, fn in jobs.items()}
        probes = {name: future.result() for name, future in futures.items()}
    for name in config.get("free_providers", []):
        probes[name] = probe_free(name)
    return probes


def probes_cached(config: dict, max_age: float | None = None) -> tuple[dict, bool]:
    """Probes from the last reading when it is still young enough.

    Routing happens once per dispatch and a dispatch takes minutes, so a quota
    number a minute old is the same number. Returns (probes, fresh); `fresh`
    is False for a cache hit, which is what stops a cached reading from
    overwriting the burn-rate baseline with a copy of itself.
    """
    if max_age is None:
        max_age = float(config.get("cache_seconds", 60))
    # Vault value rotation is not represented by native auth-file metadata.
    # Until the vault adapter exposes versions, never reuse its quota cache.
    if (ROOT / ".infisical.json").exists():
        max_age = 0
    state = load_json(STATE, {}) or {}
    cached = state.get("probe_cache") or {}
    identity = accounts.cache_identity(config)
    native_claude = identity["claude_native"]
    cached_claude = cached.get("probes", {}).get("claude", {})
    if (native_claude.get("status") != "ok" or
            (cached_claude.get("status") == "ok" and
             cached_claude.get("quota_account_ref") != native_claude.get("quota_account_ref"))):
        max_age = 0
    if (max_age > 0 and cached.get("at") and now() - cached["at"] < max_age
            and cached.get("accounts") == identity):
        return cached["probes"], False
    probes = probe_all(config)
    with state_lock():
        state = load_json(STATE, {}) or {}
        state["probe_cache"] = {"at": now(), "probes": probes, "accounts": identity}
        save_json(STATE, state)
    return probes, True


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
                current[f"{probe_scope(name, probe)}:{bucket['id']}"] = {
                    "at": now(), "percent": bucket["percent"]}
    with state_lock():
        fresh = load_json(STATE, {}) or {}
        fresh["snapshots"] = current
        save_json(STATE, fresh)
    return previous


def bucket_pace(bucket: dict) -> dict | None:
    """Where this bucket lands at its reset if the window keeps its current rate.

    A percentage on its own says nothing about whether it is too much: 77 per
    cent of a week with a day to run is fine, and 38 per cent of a month with
    27 days to run is not. Dividing what has been spent by how much of the
    window has passed says which, and needs no history to do it.

    Nested windows are the reason this matters. Every token spent against the
    weekly is also spent against the monthly, so a weekly bucket that looks
    cheap to empty can be the thing that exhausts the month.
    """
    window = bucket_window(bucket.get("id") or "")
    percent, resets_at = bucket.get("percent"), bucket.get("resets_at")
    if not window or not resets_at or percent is None:
        return None
    elapsed = 1 - (resets_at - now()) / window
    # Just after a reset the ratio is meaningless: a single dispatch divided by
    # a sliver of window projects to anything.
    if elapsed < 0.05 or percent < 2:
        return None
    return {"id": bucket["id"], "elapsed": elapsed, "pace": (percent / 100) / elapsed,
            "projected": percent / elapsed}


def headroom(probe: dict, reserve: float, previous: dict, name: str) -> dict:
    """Binding bucket = the one with least usable headroom.

    Returns usable headroom in percentage points after the reserve, the epoch
    at which the binding bucket resets, and whether this provider is on course
    to exhaust any of its windows before that window resets.
    """
    # A bucket the provider declined this minute is not a bucket we know
    # nothing about: the last good reading is usually minutes old and the
    # window is days long. Remember it rather than choosing between trusting
    # the provider blindly and disabling a workhorse over a hiccup.
    buckets = []
    for bucket in probe["buckets"]:
        if bucket.get("percent") is None and bucket.get("source") == "unavailable":
            remembered = previous.get(f"{name}:{bucket['id']}")
            age = now() - remembered["at"] if remembered else None
            if remembered and age < float(STALE_READING):
                bucket = {**bucket, "percent": remembered["percent"],
                          "source": "remembered", "age_seconds": round(age)}
            else:
                # Nothing to fall back on, so this window really is unknown.
                bucket = {**bucket, "source": "expired-reading"}
        buckets.append(bucket)
    probe = {**probe, "buckets": buckets}

    worst = None
    unknown = False
    for bucket in probe["buckets"]:
        percent = bucket.get("percent")
        if percent is None:
            # A bucket nobody declared a budget for is a measurement not taken,
            # and the reserve covers it. A bucket that should have been readable
            # and was not is a number we have lost, and it could be at 95 per
            # cent: the rest of this provider's headroom cannot be trusted while
            # one of its windows is missing.
            # Only a reading that cannot be refreshed taints the rest. A budget
            # nobody declared is a measurement not taken, and a bucket the
            # provider declined this minute comes back on the next probe;
            # treating either as lost would disable a workhorse over a hiccup.
            if bucket.get("source") == "expired-reading":
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
    # Whole-window pace and a recent burn rate answer different questions, and
    # a bucket can pass one while failing the other: a month at 40 per cent
    # with half its window left is fine on average and on course for 115 per
    # cent if the last two days are the rate that continues. Both run on every
    # bucket, not only the one that binds.
    burn_over = None
    for candidate in probe["buckets"]:
        percent, resets = candidate.get("percent"), candidate.get("resets_at")
        before = previous.get(f"{name}:{candidate['id']}")
        window = bucket_window(candidate.get("id") or "") or 0
        if percent is None or not resets or not before or not window:
            continue
        sample = now() - before["at"]
        climb = percent - before["percent"]
        if sample < max(600.0, 0.05 * window) or climb <= 0:
            continue
        projected = percent + (climb / sample) * max(0.0, resets - now())
        if projected > 100 and (burn_over is None or projected > burn_over["projected"]):
            burn_over = {"id": candidate["id"], "projected": projected,
                         "rate": climb / sample * 86400}
    # Every window, not only the binding one. The binding bucket is about what
    # stops you first; pacing is about what you are on course to run out of.
    paces = [x for x in (bucket_pace(b) for b in probe["buckets"]) if x]
    ahead = [x for x in paces if x["projected"] > 100]
    over_pace = max(ahead, key=lambda x: x["projected"]) if ahead else None
    resets_at = bucket.get("resets_at")
    if burn_over:
        overrun = True
    return {
        "usable": worst["free"],
        "unknown": unknown,
        "resets_at": resets_at,
        "bucket": bucket["id"],
        "overrun": overrun or bool(over_pace),
        "over_pace": over_pace or (
            {"id": burn_over["id"], "pace": burn_over["rate"] / 100,
             "projected": burn_over["projected"]} if burn_over else None),
        "burn_over": burn_over,
        "paces": paces,
    }


def sweep_reservations(state: dict) -> list:
    """Drop reservations whose worker must be finished or dead by now."""
    live = [r for r in state.get("reservations", []) if r.get("_rightsize_migrated") or r["expires"] > now()]
    state["reservations"] = live
    return live


def reservation_load(name: str) -> tuple[float, int]:
    """Points and dispatch count currently in flight on one provider.

    A quota reading says what has been billed, not what is about to be. Fan out
    a hundred workers inside one cache window and every one of them sees the
    same untouched headroom and picks the same provider. A reservation is the
    difference between those two questions.
    """
    state = load_json(STATE, {}) or {}
    live = [r for r in sweep_reservations(state) if r["provider"] == name]
    managed = managed_commitments(name)
    return sum(r["points"] for r in live) + managed[0], len(live) + managed[1]


def managed_commitments(name: str) -> tuple[float, int]:
    from managed_ledger import ACTIVE, Ledger
    live = [attempt for attempt in Ledger(STATE.with_name("managed.sqlite3")).read()
            if attempt["pick"]["provider"] == name and attempt["state"] in ACTIVE]
    return sum(attempt["points"] for attempt in live), len(live)


def reserve(name: str, points: float, band: int, task: str, ttl: float,
            worktree: str | None = None, limit: int | None = None) -> str | None:
    """Take a hold, or refuse it because the last slot went to someone else.

    Eligibility is read outside the lock, so two coordinators can both see one
    free slot and both take it. Counting again inside the lock is the only
    place that can be decided, and the caller routes again when it loses.
    """
    with state_lock():
        state = load_json(STATE, {}) or {}
        sweep_reservations(state)
        # Legacy holds cannot atomically re-evaluate point budgets. While a
        # managed attempt owns this provider, keep legacy pre-reservation out
        # rather than allowing a stale recommendation to overbook its account.
        if managed_commitments(name)[1]:
            return None
        if limit is not None:
            live = sum(1 for r in state["reservations"] if r["provider"] == name)
            if live >= limit:
                return None
        entry = {
        # Unique per reservation, not per millisecond: a batch reserves many in
        # the same tick, and releasing by a shared id would free every one of
        # them while their workers were still running.
        "id": uuid.uuid4().hex[:12],
        "provider": name,
        "points": points,
        "band": band,
        "task": task[:80],
        "worktree": worktree,
        "at": now(),
        "expires": now() + ttl,
    }
        state["reservations"].append(entry)
        save_json(STATE, state)
    return entry["id"]


def release(name: str, reservation_id: str | None = None) -> int:
    """Give the capacity back. Without an id, the oldest on that provider."""
    with state_lock():
        state = load_json(STATE, {}) or {}
        live = sweep_reservations(state)
        mine = [r for r in live if r["provider"] == name]
        if reservation_id:
            mine = [r for r in mine if r["id"] == reservation_id]
        if not mine:
            save_json(STATE, state)
            return 0
        drop = min(mine, key=lambda r: r["at"])
        state["reservations"] = [r for r in live if r["id"] != drop["id"]]
        save_json(STATE, state)
    return 1


SETTLED = {"succeeded", "failed", "stopped", "abandoned", "timed_out", "cancelled"}


def orca_settled(run: str | None = None) -> tuple[list[dict], str | None]:
    """Workers the orchestrator considers finished, with their worktree.

    The orchestrator already knows when a worker is done; it says so in a
    worker_done message and in its own worker list. Asking it is better than
    asking a person to remember, because today's evidence is that nobody
    remembers: seven reservations were holding capacity with every one of their
    workers already settled.
    """
    workers, cursor = [], None
    # The worker list is paginated, and reading only the first page is how a
    # release silently stops finding anything: 249 workers came back as 100.
    for _ in range(20):
        argv = ["orca", "orchestration", "worker-list", "--json"]
        if cursor:
            argv += ["--cursor", cursor]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            payload = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return [], f"could not ask orca: {exc}"
        if not payload.get("ok"):
            return [], f"orca refused: {json.dumps(payload.get('error'))[:120]}"
        body = payload.get("result") or {}
        workers += body.get("workers") or []
        page = body.get("page") or {}
        cursor = page.get("nextCursor")
        if not page.get("hasMore") or not cursor:
            break
    out = []
    for worker in workers:
        if run and worker.get("runId") != run:
            continue
        if worker.get("workerState") not in SETTLED:
            continue
        worktree = ((worker.get("resource") or {}).get("worktreeId") or "").split("::")[-1]
        out.append({"dispatch": worker.get("dispatchId"), "state": worker.get("workerState"),
                    "task": worker.get("taskId"),
                    "worktree": Path(worktree).name if worktree else None})
    return out, None


def brief_key(text: str) -> str:
    """A stable key for one task's text, for matching across tools."""
    return " ".join((text or "").split())[:80].lower()


def orca_runs() -> list[str]:
    """Every Run this machine knows about.

    A task list answers for one Run, and holds are taken by whichever
    coordinator routed the work, so asking only about the bound Run leaves
    another coordinator's finished work holding capacity forever.
    """
    try:
        result = subprocess.run(["orca", "orchestration", "run-list", "--json"],
                                capture_output=True, text=True, timeout=30)
        payload = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if not payload.get("ok"):
        return []
    return [r["id"] for r in ((payload.get("result") or {}).get("runs") or []) if r.get("id")]


def orca_task_index() -> dict:
    """Brief -> what became of it, joined across every Run.

    A task carries the brief and a task id; a worker carries that task id, a
    worktree and a settled state. Joining them gives the one key rightsize
    shares with the orchestrator, which the worktree name is not: rightsize
    only suggests that name and the coordinator may use another.
    """
    by_task = {}
    workers, _ = orca_settled()
    for worker in workers:
        if worker.get("task"):
            by_task[worker["task"]] = worker
    index = {}
    for run_id in orca_runs() or [None]:
        argv = ["orca", "orchestration", "task-list", "--json"]
        if run_id:
            argv += ["--run", run_id]
        try:
            payload = json.loads(subprocess.run(argv, capture_output=True, text=True,
                                                timeout=30).stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        for task in ((payload.get("result") or {}) if payload.get("ok") else {}).get("tasks") or []:
            key = brief_key(task.get("spec") or "")
            if not key:
                continue
            worker = by_task.get(task.get("id")) or {}
            index[key] = {"status": (task.get("status") or "").lower(),
                          "worktree": worker.get("worktree"), "state": worker.get("state")}
    return index


def orca_settled_tasks(run: str | None = None) -> set[str]:
    """Briefs the orchestrator says are finished.

    The worktree name is a weaker key than it looks: rightsize suggests one, and
    the coordinator is free to use another. The brief itself is the thing both
    sides genuinely share.
    """
    done = set()
    for run_id in ([run] if run else orca_runs() or [None]):
        argv = ["orca", "orchestration", "task-list", "--json"]
        if run_id:
            argv += ["--run", run_id]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            payload = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if not payload.get("ok"):
            continue
        for task in (payload.get("result") or {}).get("tasks") or []:
            if (task.get("status") or "").lower() in ("completed", "failed", "cancelled",
                                                      "abandoned"):
                done.add(brief_key(task.get("spec") or ""))
    return done


def release_settled(run: str | None = None) -> dict:
    """Give back the capacity held for workers that have finished.

    New receipts match the dispatch. Legacy holds match the brief. A worktree is a name rightsize
    suggested and any dispatch may reuse, so an older worker settling in a
    reused checkout would release the hold of the new worker running there now.
    A hold whose brief never matches expires on its own instead.
    """
    settled, error = orca_settled(run)
    if error:
        return {"error": error, "released": [], "kept": []}
    finished_briefs = orca_settled_tasks(run)
    finished_dispatches = {w.get("dispatch") for w in settled}
    state = load_json(STATE, {}) or {}
    released, kept = [], []
    for held in list(sweep_reservations(state)):
        if held.get("_rightsize_migrated"):
            kept.append(held)
            continue
        matched = ("dispatch" if held.get("dispatch") in finished_dispatches else None) if held.get("dispatch") else (
            "brief" if brief_key(held.get("task") or "") in finished_briefs else None)
        if matched:
            release(held["provider"], held["id"])
            released.append({**held, "matched": matched})
        else:
            kept.append(held)
    return {"released": released, "kept": kept, "settled": len(settled),
            "briefs": len(finished_briefs), "error": None}


def dispatch_cost(config: dict, provider: str, band: int) -> float:
    """What one dispatch is expected to cost, in percentage points.

    An estimate, and deliberately a coarse one: the exact number is unknowable
    before the worker runs, and being roughly right stops a fan-out from
    overcommitting a plan, which is the whole job.
    """
    costs = config.get("dispatch_cost") or {}
    base = float(costs.get(provider, costs.get("_default", 1.0)))
    return base * band


def exhausted_until(name: str) -> float:
    state = load_json(STATE, {}) or {}
    return (state.get("exhausted") or {}).get(name, 0)


def denied_buckets(probe: dict) -> list[dict]:
    """A positive quota denial is stronger evidence than missing telemetry."""
    return [b for b in probe.get("buckets", [])
            if b.get("raw_status") in ("rate-limited", "exhausted", "quota-exceeded")
            or (b.get("percent") is not None and b["percent"] >= 100)]


def admission_block(info: dict, config: dict, provider: str, band: int | None,
                    reserve_suffix: str = "") -> str | None:
    """Return the quota admission veto, including the reserve floor.

    ``band`` is absent while eligibility is being built because that is a
    provider-level diagnostic.  A candidate check also includes its cost;
    equality is affordable, but a numeric zero still fails the reserve floor
    even for a zero-cost dispatch.
    """
    if info.get("usable") is None:
        return None
    if info["usable"] <= 0:
        return (f"below reserve on {info['bucket']}" + reserve_suffix)
    if band is not None:
        cost = dispatch_cost(config, provider, band)
        if info["usable"] < cost:
            return (f"{info['usable']:.1f} points left cannot cover a band {band}"
                    f" dispatch costing about {cost:.2f}")
    return None


def mark_exhausted(name: str, until: float) -> None:
    with state_lock():
        state = load_json(STATE, {}) or {}
        state.setdefault("exhausted", {})[name] = until
        save_json(STATE, state)


def probe_scope(name, probe):
    account = (probe.get("account") or {}).get("account_ref")
    return f"{name}:{account}" if account else name


def eligibility(config: dict, probes: dict, record: bool = True) -> dict:
    previous = record_snapshot(probes) if record else (load_json(STATE, {}) or {}).get("snapshots", {})
    # A failed refresh must not turn a known denial back into band-3 capacity.
    with state_lock():
        state = load_json(STATE, {}) or {}
        denials = state.get("quota_denials") or {}
        if record:
            for name, probe in probes.items():
                known = denials.setdefault(probe_scope(name, probe), {})
                if probe.get("status") == "denied":
                    known["account"] = {"id": "account", "raw_status": "quota-exceeded"}
                elif probe.get("status") == "ok" and probe.get("buckets") and all(
                        b.get("source") == "live" and b.get("percent") is not None
                        and not denied_buckets({"buckets": [b]}) for b in probe["buckets"]):
                    known.pop("account", None)
                for bucket in probe.get("buckets", []):
                    if denied_buckets({"buckets": [bucket]}):
                        known[bucket["id"]] = bucket
                    elif (bucket.get("source") == "live" and bucket.get("percent") is not None
                          and bucket.get("raw_status", "ok") == "ok"):
                        known.pop(bucket["id"], None)
            state["quota_denials"] = denials
            save_json(STATE, state)
    reserves = config.get("reserves", {})
    out = {}
    for name, probe in probes.items():
        info = headroom(probe, float(reserves.get(name, 10)), previous, probe_scope(name, probe))
        info["provider"] = name
        info["free"] = bool(probe.get("free"))
        reserved, inflight = reservation_load(name)
        info["reserved"], info["inflight"] = reserved, inflight
        if info["usable"] is not None:
            info["usable"] -= reserved
        limits = config.get("max_inflight") or {}
        limit = int(limits.get(name, limits.get("_default", 8)))
        blocked = None
        denied = (denied_buckets(probe) or list(denials.get(probe_scope(name, probe), {}).values())
                  or list(denials.get(name, {}).values()))
        if denied:
            blocked = "quota denied on " + ", ".join(b["id"] for b in denied)
        elif probe["status"].startswith("error") or probe["status"] in (
                "no-credential", "ambiguous", "invalid-binding", "account-changed",
                "reauth-required", "denied"):
            blocked = probe["status"]
        elif exhausted_until(name) > now():
            blocked = f"quota error, retry after {human_reset(exhausted_until(name))}"
        elif inflight >= limit:
            blocked = f"{inflight} dispatches already in flight, limit {limit}"
        elif (reserve_block := admission_block(
                info, config, name, None,
                f" once {reserved:.1f} reserved points are counted" if reserved else "")):
            blocked = reserve_block
        out[name] = {
            **info,
            "account": probe.get("account"),
            "status": probe["status"],
            # A block that a finished wave cannot lift: no credential, a probe
            # error, a quota error. Running out of in-flight slots is not one.
            "hard_blocked": blocked if (blocked and "in flight" not in blocked) else None,
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
            "Could a competent engineer who can read this codebase, but cannot reach the "
            "person who wrote the task, start straight away and know when they are done?"
        ),
        "criteria": {
            "true": (
                "The target and the expected outcome are identifiable from the task and the "
                "code. Unstated implementation details are fine; the engineer is trusted to "
                "choose them."
            ),
            "false": (
                "It points at something only the requester knows: an unnamed target, a result "
                "that was agreed elsewhere, or a decision the engineer is not free to make."
            ),
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


def load_judgment(path: Path, spec: str) -> dict:
    """Validate caller judgment before reading quota or taking any holds."""
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate judgment field")
            result[key] = value
        return result

    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("judgment exceeds 64 KiB")
    try:
        document = json.loads(raw, object_pairs_hook=unique_fields)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid judgment JSON") from exc
    if not isinstance(document, dict) or set(document) != {
            "schema_version", "actor", "task_sha256", "judgment"}:
        raise ValueError("judgment envelope fields do not match version 1")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported judgment schema_version")
    actor = document["actor"]
    if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", actor):
        raise ValueError("judgment actor must be an identifier of 1-80 characters")
    digest = hashlib.sha256(spec.encode("utf-8")).hexdigest()
    if document["task_sha256"] != digest:
        raise ValueError("judgment task_sha256 does not match the routed task")
    judgment = document["judgment"]
    fields = {"tier", "size", "second_opinion", "spec_complete", "destructive"}
    if not isinstance(judgment, dict) or set(judgment) != fields:
        raise ValueError("judgment requires exactly tier and the four scores")
    if not isinstance(judgment["tier"], str) or judgment["tier"] not in QUESTIONS["tier"]["criteria"]:
        raise ValueError("invalid judgment tier")
    for key in fields - {"tier"}:
        value = judgment[key]
        maximum = 2 if key == "size" else 1
        if type(value) not in (int, float) or not 0 <= value <= maximum or not math.isfinite(value):
            raise ValueError(f"judgment {key} must be a finite number from 0 to {maximum}")
    return {**judgment, "tier_confidence": None, "source": f"caller ({actor})",
            "task_sha256": digest, "schema_version": 1}


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


EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Words that say nothing about which task this is.
FILLER = {"the", "a", "an", "to", "in", "on", "of", "for", "and", "or", "with", "that", "this",
          "it", "its", "is", "are", "be", "so", "at", "as", "by", "from", "into", "our", "we",
          "should", "must", "make", "sure", "please", "when", "then", "than", "not"}


def worktree_name(spec: str, taken: set[str] | None = None) -> str:
    """A short worktree name derived from the task.

    `--worktree new-child` requires `--name`, and a name a human can recognise
    on a branch list beats a random one. An issue number, when the brief has
    one, is the single most useful token in it.
    """
    text = spec.lower()
    issue = re.search(r"#(\d{1,6})\b|\bissue\s+(\d{1,6})\b", text)
    number = (issue.group(1) or issue.group(2)) if issue else None
    # A brief that opens with a path names the repository, not the task:
    # "In /Users/karo/Github/rightsize, design the outcome loop" is about the
    # outcome loop.
    text = re.sub(r"\S*/\S+", " ", text)
    words = [w for w in re.findall(r"[a-z0-9]+", text) if w not in FILLER and len(w) > 2]
    name = "-".join(words[:4])[:40].strip("-") or "task"
    if number and number not in name.split("-"):
        name = f"{name}-{number}"
    if taken is not None:
        base, suffix = name, 2
        while name in taken:
            name, suffix = f"{base}-{suffix}", suffix + 1
        taken.add(name)
    return name


def effort_for(config: dict, cand: dict, band: int, judgment: dict, attempt: int = 0) -> tuple[str | None, str | None]:
    """How hard the worker should think, for providers that take the knob.

    The band says which model. Effort is the second dial on the same model, and
    the things that turn it up are not the things that pick the model: a wide
    blast radius and a step that cannot be undone both want more care from
    whatever is already running, and a retry wants more than the attempt that
    just failed. Providers with no effort setting get None and the launcher
    leaves the flag off.
    """
    table = (config.get("effort") or {}).get(cand["provider"]) or {}
    base = cand.get("effort") or table.get(str(band))
    if not base:
        return None, None
    index = EFFORTS.index(base) if base in EFFORTS else 0
    thresholds = config.get("thresholds", {})
    bump, why = attempt, []
    if attempt:
        why.append(f"attempt {attempt + 1}")
    if (judgment.get("size", 0) >= float(thresholds.get("size_escalates_band", 1.5))
            or judgment.get("destructive", 0) >= float(thresholds.get("destructive_min", 0.5))):
        bump += 1
        why.append("wide blast radius or an irreversible step")
    raised = EFFORTS[min(index + bump, len(EFFORTS) - 1)]
    if raised == base:
        return base, None
    return raised, f"effort {base} -> {raised}: " + ", ".join(why)


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


def pick(candidates: list[str], elig: dict, band: int, exclude: set[str] | None = None,
         config: dict | None = None, relax_pace: bool = False) -> tuple[dict | None, list[str]]:
    """Among eligible candidates, spend the bucket that expires first, unless
    this dispatch is too expensive for what that bucket has left.

    Rule 3 is about not wasting capacity that is about to vanish, and the way to
    waste it is to spend it on the most expensive rung. A bucket with a handful
    of points and a reset in the morning should absorb as much cheap work as it
    can; an expensive dispatch belongs on the plan with a week of room, which
    would otherwise sit idle. So the cheap bands still take the soonest reset,
    while an expensive one prefers the most headroom, and a bucket that cannot
    even afford the dispatch is passed over.
    """
    notes = []
    usable = []
    exclude = exclude or set()
    for index, text in enumerate(candidates):
        cand = parse_candidate(text)
        if f"{cand['provider']}:{cand['model']}" in exclude:
            notes.append(f"{text} skipped: it already had a go at this task")
            continue
        info = elig.get(cand["provider"])
        if not info:
            continue
        if not info["eligible"]:
            notes.append(f"{text} skipped: {info['blocked']}")
            continue
        if (info["usable"] is None or info.get("unknown")) and band < 3 and not info.get("free"):
            why = ("headroom unknown" if info["usable"] is None
                   else "one of its windows could not be read, so the rest cannot be trusted")
            notes.append(f"{text} skipped: {why}, escalation only")
            continue
        # Relaxing is for the whole-window average, which is a forecast from a
        # rate nobody has measured. A burn rate taken between two readings is
        # evidence, and staying cheap on a plan that is actually running away
        # is not the lesser of the two evils.
        relaxable = relax_pace and not info.get("burn_over")
        if info["overrun"] and band < 3 and not relaxable:
            pace = info.get("over_pace")
            why = (f"its {pace['id']} window is {pace['pace']:.1f}x over pace and projects to"
                   f" {pace['projected']:.0f}% by reset" if pace
                   else "current burn rate overruns its bucket before reset")
            notes.append(f"{text} skipped: {why}, so its remaining room is kept for work"
                         " that has nowhere cheaper to go")
            continue
        resets = info["resets_at"] or float("inf")
        room = info["usable"]
        if (admission := admission_block(info, config or {}, cand["provider"], band)):
            notes.append(f"{text} skipped: {admission}")
            continue
        expensive = band >= int((config or {}).get("expensive_band", 3))
        if expensive:
            # Most room first: expiring capacity is worth more spent on cheap
            # work, and this rung has somewhere roomier to go.
            order = (-(room if room is not None else 0), resets)
        else:
            order = (resets, 0)
        usable.append((order, index, cand, text, info))
    if not usable:
        return None, notes
    usable.sort(key=lambda row: (row[0], row[1]))
    order, _, cand, text, info = usable[0]
    resets = info["resets_at"] or float("inf")
    if band >= int((config or {}).get("expensive_band", 3)) and info["usable"] is not None:
        notes.append(f"{text} chosen: band {band} is the expensive rung, so it goes to the"
                     f" roomiest plan ({info['usable']:.0f} points) rather than the one expiring"
                     " soonest, which is worth more spent on cheap work")
        return cand, notes
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


def route(spec: str, config: dict, probes: dict | None = None, max_age: float | None = None,
          hold: bool = False, judgment: dict | None = None) -> dict:
    if probes is None:
        probes, fresh = probes_cached(config, max_age)
    else:
        # Injected probes (tests, replay) must not move the burn-rate baseline.
        fresh = False
    elig = eligibility(config, probes, record=fresh)
    judgment = judge(spec) if judgment is None else judgment
    decision = decide(judgment, config, elig)
    decision["worktree_name"] = worktree_name(spec)
    if hold:
        decision["reservation"] = hold_capacity(decision, config, spec)
        if decision["pick"] and decision["reservation"] is None:
            # Somebody else took the slot between deciding and holding it, so
            # this decision was never true. Ask again with that provider full.
            taken = decision["pick"]["provider"]
            elig[taken] = {**elig[taken], "eligible": False,
                           "blocked": "its last slot was taken while this was being decided"}
            decision = decide(judgment, config, elig)
            decision["worktree_name"] = worktree_name(spec)
            decision["reservation"] = hold_capacity(decision, config, spec)
            decision["reasons"].append(f"{taken} lost its last slot to another dispatch"
                                       " between deciding and holding it")
    return decision


def hold_capacity(decision: dict, config: dict, spec: str) -> str | None:
    """Book the estimated cost of this dispatch until it is reported done.

    Only the caller that is about to launch should ask for this, which is why
    it is a flag and not the default: a route run to look at the numbers must
    not eat capacity nobody is going to spend.
    """
    if not decision["pick"] or decision["blocked"]:
        return None
    provider = decision["pick"]["provider"]
    ttl = float(config.get("reservation_ttl_seconds", 1800))
    points = dispatch_cost(config, provider, decision["band"])
    limits = config.get("max_inflight") or {}
    limit = int(limits.get(provider, limits.get("_default", 8)))
    return reserve(provider, points, decision["band"], spec, ttl,
                   decision.get("worktree_name"), limit)


def debit(elig: dict, config: dict, provider: str, band: int) -> float:
    """Spend the estimate in this process, so the next task in a batch sees it."""
    points = dispatch_cost(config, provider, band)
    info = elig.get(provider)
    if not info:
        return points
    info["inflight"] = info.get("inflight", 0) + 1
    info["reserved"] = info.get("reserved", 0.0) + points
    if info["usable"] is not None:
        info["usable"] -= points
    # What a batch commits counts toward pace as much as what has been billed.
    # Recomputed from the buckets rather than adjusted in place, because a
    # bucket too early in its window to have a pace at probe time has no entry
    # to adjust, and that is exactly the bucket a fan-out can fill unnoticed.
    for bucket in info.get("buckets") or []:
        if bucket.get("percent") is None:
            continue
        committed = bucket_pace({**bucket, "percent": bucket["percent"] + info["reserved"]})
        if committed and committed["projected"] > 100 and not info["overrun"]:
            info["overrun"] = True
            info["over_pace"] = committed
    limits = config.get("max_inflight") or {}
    limit = int(limits.get(provider, limits.get("_default", 8)))
    if info["inflight"] >= limit:
        info["blocked"] = f"{info['inflight']} dispatches already in flight, limit {limit}"
    elif (reserve_block := admission_block(
            info, config, provider, None, " once this batch is counted")):
        info["blocked"] = reserve_block
    info["eligible"] = info["blocked"] is None
    return points


def start_wave(elig: dict, config: dict) -> None:
    """Begin a wave: the previous one's workers have finished.

    Their slots come back. The quota they burned does not: `usable` keeps every
    debit, which is why a plan runs out of capacity eventually instead of
    scheduling waves forever.
    """
    for provider, info in elig.items():
        info["inflight"] = 0
        blocked = info.get("hard_blocked")
        if not blocked:
            blocked = admission_block(info, config, provider, None,
                                      " once this plan is counted")
        info["blocked"] = blocked
        info["eligible"] = blocked is None


def plan(specs: list[str], config: dict, concurrency: int = 8, hold: bool = False,
         probes: dict | None = None, max_waves: int = 12,
         names: list[str] | None = None) -> dict:
    """Route a whole fan-out at once.

    Judgments are independent, so they go out in parallel; allocation is not,
    so each task is placed against headroom the earlier ones have already
    spent. That is what stops a hundred workers going to one provider on the
    strength of a single quota reading.

    A hold taken here is a claim on capacity for a dispatch that has not
    happened yet, so it expires on the short clock: a plan that is printed and
    not run must not sit on a provider. A hold taken after a launch has actually
    run keeps the long one.
    """
    if probes is None:
        probes, fresh = probes_cached(config, max_age=0)
    else:
        fresh = False
    elig = eligibility(config, probes, record=fresh)
    ttl = float(config.get("reservation_ttl_unconfirmed_seconds", 300))
    # One judgment per task, in parallel, and only once: waves reschedule the
    # same judgment against different headroom, they do not re-ask the model.
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        judgments = list(pool.map(judge, specs))

    taken: set[str] = set()
    tasks = []
    for i, (spec, judgment) in enumerate(zip(specs, judgments)):
        # A spec read from a file already has a name someone chose.
        source = names[i] if names and i < len(names) else None
        name = worktree_name(source, taken) if source else worktree_name(spec, taken)
        tasks.append({"index": i, "spec": spec, "judgment": judgment, "points": 0.0,
                      "wave": None, "decision": None, "name": name})
    pending = list(tasks)
    wave = 0
    while pending and wave < max_waves:
        wave += 1
        if wave > 1:
            start_wave(elig, config)
        placed_this_wave, still_pending = [], []
        for task in pending:
            decision = decide(task["judgment"], config, elig, fallback=False)
            task["decision"] = decision
            if decision["blocked"]:
                task["wave"] = None
                continue  # a brief nobody can execute is not a capacity problem
            if not decision["pick"]:
                still_pending.append(task)
                continue
            provider = decision["pick"]["provider"]
            task["points"] = debit(elig, config, provider, decision["band"])
            # A review leg is a second dispatch and costs like one.
            if decision.get("review"):
                task["points"] += debit(elig, config, decision["review"]["provider"], 1)
            task["wave"] = wave
            decision["worktree_name"] = task["name"]
            log_decision(decision, task["spec"], dispatched=hold)
            if hold:
                decision["reservation"] = reserve(provider, task["points"], decision["band"],
                                                  task["spec"], ttl, task["name"])
            placed_this_wave.append(task)
        pending = still_pending
        if not placed_this_wave:
            # Placing nothing is not the same as having nothing. Workers that
            # were already running when the batch was planned hold slots a wave
            # boundary gives back, so a batch that starts against a full
            # provider waits for the next wave rather than declaring defeat.
            slots_held = any(info.get("inflight") for info in elig.values())
            if not slots_held:
                break  # no capacity anywhere: more waves would place nothing

    spread, waves = {}, {}
    for task in tasks:
        pick = task["decision"]["pick"] if task["decision"] else None
        if task["wave"] is None or not pick:
            continue
        row = spread.setdefault(pick["provider"], {"dispatches": 0, "points": 0.0, "models": {}})
        row["dispatches"] += 1
        row["points"] = round(row["points"] + task["points"], 2)
        row["models"][pick["model"]] = row["models"].get(pick["model"], 0) + 1
        bucket = waves.setdefault(task["wave"], {"tasks": 0, "providers": {}})
        bucket["tasks"] += 1
        bucket["providers"][pick["provider"]] = bucket["providers"].get(pick["provider"], 0) + 1
    return {
        "tasks": tasks,
        "waves": waves,
        "spread": spread,
        "blocked": [t["index"] for t in tasks if t["decision"] and t["decision"]["blocked"]],
        "unplaced": [t["index"] for t in tasks
                     if t["wave"] is None and t["decision"] and not t["decision"]["blocked"]],
        "quota_after": {name: {"usable": info["usable"], "inflight": info.get("inflight", 0),
                               "eligible": info["eligible"], "blocked": info["blocked"]}
                        for name, info in elig.items()},
    }


def decide(judgment: dict, config: dict, elig: dict, fallback: bool = True,
           floor_band: int = 0, exclude: set[str] | None = None, attempt: int = 0,
           relax_pace: bool = False) -> dict:
    """One decision against one eligibility snapshot.

    `fallback` is the difference between a single dispatch and a wave of them.
    Alone, a task must never be stranded, so a full band drops to a cheaper one
    and then escalates rather than returning nothing. Inside a batch there is a
    next wave, so a task whose band is full waits for one instead of being
    answered with a model that is wrong for it in the other direction: sending
    ordinary implementation work to a band 3 model because the cheap plans are
    busy is the expensive mistake this tool exists to prevent.
    """
    band, reasons = band_for(judgment, config)
    if floor_band and band < floor_band:
        band = min(3, floor_band)
        reasons.append(f"a previous attempt was band {floor_band - 1}, so this starts at band {band}")
    thresholds = config.get("thresholds", {})
    blocked = None
    if judgment["spec_complete"] < float(thresholds.get("spec_complete_min", 0.5)):
        blocked = (
            f"spec is not self-contained (spec_complete {judgment['spec_complete']:.2f}); "
            "tighten the brief before dispatching it to any worker"
        )

    ladders = config["bands"]
    chosen, notes = pick(ladders[str(band)], elig, band, exclude, config, relax_pace=relax_pace)
    used_band = band
    if chosen is None and not fallback:
        reasons.append(f"band {band} is full; holding this task for a later wave "
                       "rather than moving it to a band that suits it worse")
    while fallback and chosen is None and used_band > 1:
        used_band -= 1
        reasons.append(f"nothing eligible in band {used_band + 1}, dropping to band {used_band}")
        chosen, more = pick(ladders[str(used_band)], elig, used_band, exclude, config)
        notes += more
    if fallback and chosen is None and band < 3:
        # Pacing holds a provider's room back for work that has nowhere cheaper
        # to go. This task has nowhere cheaper to go, and escalating it would
        # spend the same strained plan on the priciest rung, which is the
        # opposite of what holding it back was for.
        chosen, more = pick(ladders[str(band)], elig, band, exclude, config, relax_pace=True)
        notes += more
        if chosen is not None:
            reasons.append(f"every band {band} candidate is over pace, but escalating would spend"
                           " the same plans on a dearer model, so this stays where it is")
            used_band = band
    if fallback and chosen is None and band < 3:
        reasons.append("nothing eligible below band 3, escalating instead of failing")
        chosen, more = pick(ladders["3"], elig, 3, exclude, config)
        notes += more
        used_band = 3

    if chosen:
        chosen = dict(chosen)
        chosen["effort"], effort_reason = effort_for(config, chosen, used_band, judgment, attempt)
        if effort_reason:
            reasons.append(effort_reason)

    review = None
    if judgment["second_opinion"] >= float(thresholds.get("second_opinion_min", 0.6)) and chosen:
        others = [c for c in config["review_ladder"] if parse_candidate(c)["provider"] != chosen["provider"]]
        review, review_notes = pick(others, elig, 1, None, config)
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
        "account": elig[chosen["provider"]].get("account") if chosen else None,
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


def rerun(spec: str, because: str, previous: str | None, config: dict,
          probes: dict | None = None) -> dict:
    """Route a task that has already been tried and did not work.

    The first judgment was made from the brief alone. This one gets to see what
    happened, which is usually the more informative state: a worker that could
    not make the tests pass is evidence about the task, not only about the
    model. The previous candidate is taken out of the running and the band
    starts one above where it was, so a retry cannot quietly land on the same
    rung that already failed.
    """
    if probes is None:
        probes, fresh = probes_cached(config, max_age=None)
    else:
        fresh = False
    elig = eligibility(config, probes, record=fresh)
    state = f"{spec}\n\n[Previous attempt]\nmodel: {previous or 'unknown'}\noutcome: {because}"
    judgment = judge(state)
    floor = 0
    if previous:
        for band_id, ladder in config["bands"].items():
            if any(parse_candidate(c)["provider"] + ":" + parse_candidate(c)["model"] == previous
                   for c in ladder):
                floor = int(band_id) + 1
                break
    decision = decide(judgment, config, elig, floor_band=floor,
                      exclude={previous} if previous else None, attempt=1)
    decision["reason_for_rerun"] = because
    decision["previous"] = previous
    decision["worktree_name"] = worktree_name(spec)
    return decision


def launch_fields(decision: dict, config: dict, spec_path: str | None,
                  spec_text: str | None = None) -> dict:
    """The substitutions a launcher template may use.

    `model_ref` is the model id spelled the way the target CLI wants it, which
    is the only provider-specific knowledge in here and lives in config.
    """
    cand = decision["pick"]
    prefix = (config.get("model_prefixes") or {}).get(cand["provider"], "")
    if spec_path:
        quoted = f'"$(cat -- {shlex.quote(spec_path)})"'
    elif spec_text:
        # A batch reads its tasks from lines, not files, so the brief has to go
        # on the command line itself or the printed command is not runnable.
        quoted = shlex.quote(" ".join(spec_text.split()))
    else:
        quoted = '"<task>"'
    return {
        "provider": cand["provider"],
        "agent": decision["agent"] or cand["provider"],
        "name": decision.get("worktree_name") or worktree_name(spec_text or spec_path or "task"),
        "model": cand["model"],
        "model_ref": f"{prefix}{cand['model']}",
        "effort": cand["effort"] or "medium",
        "band": decision["band"],
        "spec": quoted,
        "spec_path": spec_path or "<task file>",
    }


def launch_command(decision: dict, config: dict, launcher: str, spec_path: str | None,
                   spec_text: str | None = None) -> str:
    """Render one launcher template. Adding a launcher is config, not code."""
    if not decision["pick"]:
        return "# no eligible provider"
    templates = (config.get("launchers") or {}).get(launcher)
    if not templates:
        known = ", ".join(sorted(k for k in (config.get("launchers") or {}) if not k.startswith("_")))
        return f"# unknown launcher {launcher!r}; configured: {known or 'none'}"
    fields = launch_fields(decision, config, spec_path, spec_text)
    template = templates.get(fields["provider"]) or templates.get("default")
    if not template:
        return f"# launcher {launcher!r} has no template for provider {fields['provider']}"
    try:
        command = template.format(**fields)
    except KeyError as exc:
        return f"# launcher {launcher!r} template uses unknown field {exc}"
    bound = decision.get("account")
    if bound:
        if bound.get("source") == "scoped-vault":
            return "# vault account requires a managed adapter to bind the same credential at launch"
        binding = accounts.select(fields["provider"], config)
        if (binding.status != "unverified" or binding.public() != bound):
            return "# account changed or unavailable; re-probe before launching"
        if launcher == "shell":
            prefix = "env " + " ".join(f"{key}={shlex.quote(value)}"
                                        for key, value in binding.overrides.items())
            return prefix + " " + command
        # An Orca worker is a separately created terminal. The coordinator's
        # environment is not proof of the worker's selected account.
        return "# launcher account binding is unverified; use shell or a managed adapter"
    return command


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


def capacity_in_dispatches(config: dict, name: str, usable: float | None) -> str:
    """Percentage points are not a unit anyone thinks in. Dispatches are.

    Every plan meters something different (percent of a week, dollars of
    credit, requests a day, tokens against a declared budget), so the numbers
    cannot be compared as they stand. What they all convert into, once a
    dispatch has a measured cost, is how many more dispatches of a given kind
    the plan has left, which is the question being asked anyway.
    """
    if usable is None or usable <= 0:
        return ""
    parts = []
    for band in (1, 3):
        cost = dispatch_cost(config, name, band)
        if cost > 0:
            parts.append(f"{int(usable / cost)} band {band}")
    return ", ".join(parts)


def cmd_probe(args, config):
    identity = accounts.cache_identity(config)
    probes = probe_all(config, count_tokens=True)
    # Warm the cache the router reads, so looking at the numbers and then
    # routing does not probe twice.
    with state_lock():
        state = load_json(STATE, {}) or {}
        state["probe_cache"] = {"at": now(), "probes": probes, "accounts": identity}
        save_json(STATE, state)
    elig = eligibility(config, probes)
    if args.json:
        print(json.dumps({"probes": probes, "eligibility": elig}, indent=2))
        return 0
    for name, info in elig.items():
        mark = "ok " if info["eligible"] else "BLOCKED"
        usable = "unknown" if info["usable"] is None else f"{info['usable']:.0f} pts"
        print(f"{name:<11} {mark:<8} usable {usable:<9} binding {info['bucket'] or '-':<18} resets {human_reset(info['resets_at'])}")
        room = capacity_in_dispatches(config, name, info["usable"])
        if room:
            print(f"    {'':<20} about {room} dispatches left before the reserve")
        for bucket in info["buckets"]:
            percent = "-" if bucket["percent"] is None else f"{bucket['percent']}%"
            pace = bucket_pace(bucket)
            if pace and pace["projected"] > 100:
                bucket = {**bucket, "_pace": pace}
            age = bucket.get("age_seconds")
            stale = f"  observed {human_age(age)} ago" if age and age > 900 else ""
            if bucket.get("tokens_used"):
                extra = f" ({bucket['tokens_used']:,} tokens)"
            elif bucket.get("requests_left") is not None:
                extra = f" ({bucket['requests_left']} of {bucket['requests_cap']} requests left today)"
            elif bucket.get("remaining_usd") is not None:
                extra = f" ({bucket['remaining_usd']} of {bucket['limit_usd']} USD left)"
            else:
                extra = ""
            print(f"    {bucket['id']:<20} used {percent:<7} {bucket['source']}{extra}{stale}")
            if bucket.get("_pace"):
                pace = bucket["_pace"]
                print(f"    {'':<20} {pace['pace']:.1f}x over pace, projects to"
                      f" {pace['projected']:.0f}% by reset with {(1 - pace['elapsed']) * 100:.0f}%"
                      " of the window left")
        if info.get("inflight"):
            print(f"    in flight            {info['inflight']} dispatches holding "
                  f"{info['reserved']:.1f} points")
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
    try:
        judgment = load_judgment(Path(args.judgment), spec) if args.judgment else None
    except (OSError, ValueError) as exc:
        print(f"judgment rejected: {exc}", file=sys.stderr)
        return 2
    decision = route(spec, config, max_age=0 if args.fresh else None, hold=args.reserve,
                     judgment=judgment)
    log_decision(decision, spec, dispatched=args.reserve)
    return print_decision(decision, args, config, spec)


def cmd_rerun(args, config):
    spec = args.task or Path(args.spec).read_text()
    decision = rerun(spec, args.because, args.previous, config)
    log_decision(decision, spec)
    return print_decision(decision, args, config, spec)


def print_decision(args_decision, args, config, spec: str | None = None):
    decision = args_decision
    if args.json:
        print(json.dumps(decision, indent=2))
        return 0
    judgment = decision["judgment"]
    if decision.get("previous"):
        print(f"rerun      {decision['previous']} did not finish it: {decision['reason_for_rerun']}")
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
    if decision.get("reservation"):
        print(f"reserved   {decision['reservation']}, release with: "
              f"rightsize report {cand['provider']} --done")
    for reason in decision["reasons"]:
        print(f"  why      {reason}")
    for note in decision["notes"]:
        print(f"  quota    {note}")
    launcher = "orca" if getattr(args, "orca", False) else args.launcher
    if launcher:
        print()
        print(launch_command(decision, config, launcher, getattr(args, "spec", None), spec))
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


def cmd_report(args, config):
    """Feed a dispatch outcome back, so rule 5 (fallback is code) can fire.

    rightsize decides and steps out, so it never sees the worker fail. The
    caller that does see it reports here, and the next route skips that
    provider until its bucket is known to have reset.
    """
    if args.from_orca:
        result = release_settled(args.run)
        if result["error"]:
            print(result["error"], file=sys.stderr)
            return 1
        for held in result["released"]:
            print(f"{held['provider']}: released {held['points']} points held for"
                  f" {held.get('worktree') or brief_key(held['task'])},"
                  f" settled (matched by {held['matched']})")
        for held in result["kept"]:
            # Say only what was checked. A hold that matched nothing may be a
            # running worker, a dispatch that never happened, or a worker in
            # another coordinator's Run, which this cannot see.
            why = ("no settled task or worktree matches it; it expires on its own"
                   if not held.get("worktree")
                   else f"no finished task or worker matches {held['worktree']} in any Run,"
                        " so it is taken to be still running")
            print(f"{held['provider']}: keeping {held['points']} points for"
                  f" {held.get('worktree') or held['task'][:40]}, {why}")
        if not result["released"] and not result["kept"]:
            print(f"nothing in flight; {result['settled']} settled workers seen")
        return 0
    name = args.provider
    if not name:
        print("give a provider, or --from-orca to release everything that has settled",
              file=sys.stderr)
        return 2
    if name not in config.get("reserves", {}) and name not in config.get("free_providers", []):
        print(f"unknown provider {name!r}", file=sys.stderr)
        return 2
    if args.started:
        if not args.model or not args.task or not args.dispatch:
            print("--started needs --model, --task and --dispatch (a stable launch id)", file=sys.stderr)
            return 2
        receipt = record_launch(config, name, args.model, args.task, args.dispatch,
                                args.worktree, args.effort)
        print(f"{name}: recorded launch {args.dispatch}, reservation {receipt}")
        return 0
    if args.clear:
        with state_lock():
            state = load_json(STATE, {}) or {}
            (state.get("exhausted") or {}).pop(name, None)
            state.pop("probe_cache", None)
            save_json(STATE, state)
        print(f"{name}: cooldown cleared; fresh quota checks still apply")
        return 0
    if args.done:
        freed = release(name, args.id)
        print(f"{name}: released {freed} reservation" if freed
              else f"{name}: nothing in flight to release")
        return 0
    if args.free_request:
        count_free_request()
        used, resets_at = free_requests_today()
        cap = (config.get("openrouter") or {}).get("free_requests_per_day") or 1000
        print(f"{name}: {used}/{cap} free requests today, resets in {human_reset(resets_at)}")
        return 0
    # A quota error: believe the provider's own reset time when the last
    # reading carries one, and fall back to a short cooldown when it does not.
    with state_lock():
        state = load_json(STATE, {}) or {}
        until = now() + args.minutes * 60
        probes = (state.get("probe_cache") or {}).get("probes") or {}
        known = state.setdefault("quota_denials", {}).setdefault(name, {})
        for bucket in denied_buckets(probes.get(name) or {}):
            known[bucket["id"]] = bucket
        resets = [b.get("resets_at") for b in known.values()]
        if resets and all(t and t > now() for t in resets):
            until = max(resets)
        until = max(until, state.get("exhausted", {}).get(name, 0))
        state.setdefault("exhausted", {})[name] = until
        # Reusing the pre-error snapshot would undo the report at expiry.
        state.pop("probe_cache", None)
        save_json(STATE, state)
    print(f"{name}: marked exhausted, skipped until {human_reset(until)} from now")
    return 0


def record_launch(config: dict, provider: str, model: str, task: str,
                  dispatch: str, worktree: str | None = None, effort: str | None = None) -> str:
    """Account for a launch that happened, never make another routing decision."""
    bands = [int(band) for band, ladder in config["bands"].items()
             for text in ladder if parse_candidate(text)["provider"] == provider
             and parse_candidate(text)["model"] == model]
    # An unlisted model has no measured tier; hold the conservative band.
    band = max(bands, default=3)
    with state_lock():
        state = load_json(STATE, {}) or {}
        entries = state.get("decisions") or []
        existing = next((d for d in entries if d.get("dispatch") == dispatch), None)
        if existing:
            return existing["reservation"]
        holds = sweep_reservations(state)
        existing_hold = next((h for h in holds if h.get("dispatch") == dispatch), None)
        if existing_hold:
            return existing_hold["id"]
        # Confirm an explicit pre-launch hold rather than charging it twice.
        held = next((h for h in holds if not h.get("dispatch")
                     and h["provider"] == provider and brief_key(h["task"]) == brief_key(task)
                     and h.get("worktree") == (Path(worktree).name if worktree else None)), None)
        if held is None:
            held = {"id": uuid.uuid4().hex[:12], "provider": provider,
                    "task": task[:80], "at": now()}
            state["reservations"].append(held)
        held.update(points=dispatch_cost(config, provider, band), band=band,
                    worktree=Path(worktree).name if worktree else None,
                    expires=now() + float(config.get("reservation_ttl_seconds", 1800)),
                    dispatch=dispatch, model=model)
        entries.append({"at": now(), "provider": provider, "model": model, "effort": effort,
                        "band": band, "name": Path(worktree).name if worktree else None,
                        "directory": worktree if worktree and Path(worktree).is_absolute() else None,
                        "task": " ".join(task.split())[:120], "dispatched": True,
                        "dispatch": dispatch, "reservation": held["id"], "observed_launch": True})
        state["decisions"] = entries[-200:]
        save_json(STATE, state)
    return held["id"]


def cmd_plan(args, config):
    paths = []
    if args.specs:
        lines = [line.strip() for line in Path(args.specs).read_text().splitlines() if line.strip()]
        # A file of paths is a list of specs, not a list of tasks. Judging the
        # paths themselves produces a routing decision about a filename, which
        # is exactly as useless as it sounds, and it happened in practice.
        as_files = [Path(line).expanduser() for line in lines]
        if as_files and all(f.is_file() for f in as_files):
            paths = as_files
            specs = [f.read_text() for f in paths]
            print(f"# {args.specs} lists files, so each line was read as a spec", file=sys.stderr)
        else:
            specs = lines
    elif args.dir:
        paths = sorted(Path(args.dir).glob(args.glob))
        specs = [f.read_text() for f in paths]
    else:
        specs = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    if not specs:
        print("no tasks given", file=sys.stderr)
        return 2
    result = plan(specs, config, concurrency=args.concurrency, hold=args.reserve,
                  names=[p.stem for p in paths] if paths else None)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"{len(specs)} tasks, judged {args.concurrency} at a time\n")
    for task in result["tasks"]:
        decision = task["decision"]
        pick = decision["pick"] if not decision["blocked"] else None
        where = f"{pick['provider']}:{pick['model']}" + (f" ({pick['effort']})" if pick and pick["effort"] else "") if pick else "-"
        wave = f"w{task['wave']}" if task["wave"] else "-"
        flag = "BLOCKED" if decision["blocked"] else ("CONFIRM" if decision["confirm_first"] else "")
        first = " ".join(task["spec"].split())[:46]
        print(f"{task['index']:>4}  {wave:<3} band {decision['band']}  {where:<46} {flag:<8} {first}")
    if result["waves"]:
        print("\nwaves (each one runs after the previous reports done)")
        for number, bucket in sorted(result["waves"].items()):
            spread = ", ".join(f"{name} x{count}" for name, count in sorted(bucket["providers"].items()))
            print(f"  wave {number}: {bucket['tasks']:>3} tasks  ({spread})")
    print("\nspread")
    for name, row in sorted(result["spread"].items(), key=lambda kv: -kv[1]["dispatches"]):
        models = ", ".join(f"{model} x{count}" for model, count in row["models"].items())
        print(f"  {name:<13} {row['dispatches']:>4} dispatches, {row['points']:.1f} points held  ({models})")
    for name, info in result["quota_after"].items():
        if info["blocked"]:
            print(f"  {name:<13} full: {info['blocked']}")
    if result["blocked"]:
        print(f"\nnot dispatchable ({len(result['blocked'])}): tasks "
              + ", ".join(str(i) for i in result["blocked"])
              + "\n  tighten those briefs; no provider fixes a spec a worker cannot execute alone")
    if result["unplaced"]:
        print(f"\nno capacity for {len(result['unplaced'])} tasks, even across waves: "
              + ", ".join(str(i) for i in result["unplaced"][:20])
              + ("..." if len(result["unplaced"]) > 20 else "")
              + "\n  the plans run out before these are reached. Wait for a bucket to reset,"
              + "\n  add a provider, or raise max_inflight / lower dispatch_cost if the"
              + "\n  estimates are more conservative than reality.")
    if args.launcher:
        for number in sorted(result["waves"]):
            print(f"\n# ---- wave {number} ----")
            for task in result["tasks"]:
                if task["wave"] == number:
                    task["decision"]["worktree_name"] = task["name"]
                    path = str(paths[task["index"]]) if paths else None
                    print(f"# task {task['index']}")
                    print(launch_command(task["decision"], config, args.launcher,
                                         path, task["spec"]))
    return 1 if (result["blocked"] or result["unplaced"]) else 0


def doctor(config: dict) -> list[tuple[str, str]]:
    """Preflight. Every check here exists because something was wrong once.

    Returns (level, message) pairs; level is "ok", "warn" or "error". The one
    that matters most is the ladder check: a model id that a provider has
    retired is invisible until a worker fails on it, because routing happily
    picks a name nobody has confirmed still exists.
    """
    out = []
    registry = load_json(REGISTRY) or {}
    providers = registry.get("providers") or {}
    for runtime in accounts.HOME_VARIABLES:
        out.append(("ok", f"{runtime} runtime version: {accounts.runtime_version(runtime)}"))

    overlay = repo_config()
    out.append(("ok", f"config: {CONFIG}" + (f" overlaid with {overlay}" if overlay else "")))

    candidates = {c for ladder in config["bands"].values() for c in ladder}
    candidates |= set(config.get("review_ladder", []))
    missing, unverifiable = [], set()
    for text in sorted(candidates):
        cand = parse_candidate(text)
        known = (providers.get(cand["provider"]) or {}).get("models") or {}
        if not known:
            unverifiable.add(cand["provider"])
        elif cand["model"] not in known:
            missing.append(text)
    if missing:
        out.append(("error", "ladder models not in the catalogue: " + ", ".join(missing)
                    + " (a retired id fails only when a worker tries it)"))
    else:
        out.append(("ok", f"{len(candidates)} ladder candidates all exist in the catalogue"))
    for name in sorted(unverifiable):
        out.append(("warn", f"{name}: catalogue is empty, so its ladder entries are unverified;"
                            " run rightsize refresh with its key available"))

    fetched = registry.get("fetched_at")
    age = None
    if fetched:
        stamp = iso_to_epoch(fetched)
        age = (now() - stamp) / 86400 if stamp else None
    if age is None:
        out.append(("warn", "no registry yet: run rightsize refresh"))
    elif age > 3:
        out.append(("warn", f"catalogue is {age:.0f} days old: run rightsize refresh"))
    else:
        out.append(("ok", f"catalogue refreshed {age * 24:.0f}h ago"))

    for variable, why in (("TYPESAFE_API_KEY", "the judgment falls back to a keyword heuristic"),
                          ("OPENROUTER_API_KEY", "OpenRouter is unavailable"),
                          ("OPENCODE_API_KEY", "OpenCode quota cannot be read")):
        if os.environ.get(variable):
            out.append(("ok", f"{variable}: environment selected; unverified"))
        elif (ROOT / ".infisical.json").exists():
            if vault_scope():
                out.append(("ok", f"{variable}: scoped vault selected; unverified"))
            else:
                out.append(("warn", f"{variable}: vault binding incomplete; explicit project,"
                                    " environment and rightsizePath required"))
        elif variable == "OPENCODE_API_KEY" and OPENCODE_AUTH.is_file():
            out.append(("ok", f"{variable}: native auth store present; unverified"))
        else:
            out.append(("warn", f"{variable} missing: {why}"))

    if not (config.get("claude") or {}).get("weekly_token_budget"):
        out.append(("warn", "Claude legacy budget is unset; known headroom requires native quota."
                            " Unsupported native control leaves fallback headroom unknown"))

    codex = probe_codex(config)
    binding = codex.get("account") or {}
    if binding:
        out.append(("ok" if codex["status"] == "ok" else "warn",
                    f"codex {binding['source']} account={binding['account_ref']}: {codex['status']}"))
    for bucket in codex["buckets"]:
        if bucket["source"] == "expired-reading":
            out.append(("warn", f"codex usage was last written {human_age(bucket['age_seconds'])}"
                                " ago and is now treated as unknown, so Codex is escalation-only."
                                " Only an interactive Codex session rewrites it: `codex exec` does"
                                " not write a rollout (verified 2026-09-20)"))
            break
        if bucket["source"] == "post-reset-assumed-zero":
            out.append(("ok", "codex window rolled over since its last reading, counted as empty"))
            break

    state = load_json(STATE, {}) or {}
    live = [r for r in (state.get("reservations") or []) if r["expires"] > now()]
    if live:
        oldest = min(r["at"] for r in live)
        out.append(("warn", f"{len(live)} reservations in flight, oldest {human_age(now() - oldest)} ago."
                            " If those workers are finished: rightsize report --from-orca"))
    exhausted = {n: t for n, t in (state.get("exhausted") or {}).items() if t > now()}
    for name, until in exhausted.items():
        out.append(("warn", f"{name} is marked exhausted until {human_reset(until)} from now"))

    for launcher, templates in (config.get("launchers") or {}).items():
        if launcher.startswith("_") or not isinstance(templates, dict):
            continue
        if "default" not in templates:
            out.append(("warn", f"launcher {launcher} has no default template"))
    return out


def orca_sessions(kind: str) -> list[dict]:
    """Sessions Orca has already accounted for, or nothing.

    Orca scans opencode's database and Claude's transcripts and writes what it
    finds beside its own state. This is another application's private file, so
    every failure here is silent and the caller falls back to an estimate.
    """
    path = ORCA_SUPPORT / f"orca-{kind}-usage.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    sessions = data.get("sessions")
    return sessions if isinstance(sessions, list) else []


def bucket_window(bucket_id: str) -> float | None:
    """How long the bucket's window is, in seconds, when that is knowable."""
    match = re.search(r"-(\d+)m$", bucket_id or "")
    if match:
        return int(match.group(1)) * 60
    return BUCKET_WINDOWS.get(bucket_id)


def dispatches_since(sessions: list[dict], since: float, model_prefix: str) -> dict:
    """What has been spent on one provider since a moment, as Orca saw it.

    A session is the closest thing to a dispatch: one worker, one model, its own
    id. Sessions are attributed by when they last did something, because that is
    when the quota moved.
    """
    count, tokens, cached, output, models = 0, 0, 0, 0, {}
    # How far back the record itself goes, across every model: the question is
    # when Orca's history begins, not when this provider was last used.
    stamps = [iso_to_epoch(x.get("lastTimestamp") or "") for x in sessions]
    stamps = [x for x in stamps if x]
    oldest, newest = (min(stamps), max(stamps)) if stamps else (None, None)
    for session in sessions:
        model = session.get("primaryModel") or session.get("model") or ""
        if not model.startswith(model_prefix):
            continue
        last = iso_to_epoch(session.get("lastTimestamp") or "")
        if last is None or last < since:
            continue
        count += 1
        tokens += session.get("totalInputTokens") or 0
        cached += (session.get("totalCachedInputTokens") or session.get("totalCacheReadTokens") or 0)
        output += session.get("totalOutputTokens") or 0
        models[model] = models.get(model, 0) + 1
    return {"dispatches": count, "input": tokens, "cached": cached, "output": output,
            "models": models, "record_from": oldest, "record_to": newest}


def calibrate(config: dict, probes: dict | None = None) -> list[dict]:
    """Measure what a dispatch actually costs, instead of guessing it.

    `dispatch_cost` is the one number in the policy that was invented. It does
    not have to stay invented: a bucket's window has a start, which is its reset
    time minus its length, and Orca already records every session inside that
    window. Percentage points burned divided by dispatches made is the number
    the config is asking for, in the unit the config uses.
    """
    if probes is None:
        probes, _ = probes_cached(config, max_age=None)
    rows = []
    sources = {"opencode": ("opencode", "opencode-go/"), "claude": ("claude", "claude")}
    for name, probe in probes.items():
        kind = sources.get(name)
        if not kind:
            rows.append({"provider": name, "verdict": "no local session record to count against"})
            continue
        sessions = orca_sessions(kind[0])
        if name == "opencode":
            # opencode's own database is the complete record; Orca's scan lags
            # it (355 sessions against 295 when this was written), and counting
            # the short set overstates what each dispatch costs.
            direct = opencode_sessions_since(now() - 31 * 86400)
            if len(direct) > len(sessions):
                sessions = [{"primaryModel": f"opencode-go/{x['model']}",
                             "lastTimestamp": datetime.fromtimestamp(
                                 x["last_at"], timezone.utc).isoformat().replace("+00:00", "Z"),
                             "totalInputTokens": x["tokens_in"],
                             "totalCachedInputTokens": x["cached"],
                             "totalOutputTokens": x["tokens_out"]}
                            for x in direct if (x.get("provider") or "") == "opencode-go"]
        if not sessions:
            rows.append({"provider": name,
                         "verdict": f"no {ORCA_SUPPORT.name}/orca-{kind[0]}-usage.json to read"})
            continue
        # Calibrate against the bucket the policy actually binds on: the one
        # with the least room after its reserve. Picking the highest percentage
        # instead once measured a month's spend against a week's dispatches.
        reserve = float((config.get("reserves") or {}).get(name, 10))
        best = None
        for bucket in probe["buckets"]:
            window = bucket_window(bucket["id"])
            if window is None or bucket.get("resets_at") is None or bucket.get("percent") is None:
                continue
            # A percentage point is not the same size in every window: one
            # dispatch is 2.3 per cent of a five hour allowance and 0.44 per
            # cent of a weekly one. dispatch_cost is a single number, so it has
            # to be measured against the windows that govern a day's spending,
            # not a bucket that turns over while a worker is still running.
            if window < 86400:
                continue
            free = 100.0 - bucket["percent"] - reserve
            if best is None or free < best["free"]:
                best = {**bucket, "window": window, "free": free}
        window_start = (best["resets_at"] - best["window"]) if best else now() - 7 * 86400
        spent = dispatches_since(sessions, window_start, kind[1])
        row = {"provider": name, "bucket": best["id"] if best else None,
               "window_started": window_start, **spent}
        # A record that begins after the window did cannot have counted every
        # dispatch in it, so the cost that falls out is an upper bound.
        row["partial_record"] = bool(spent["record_from"] and spent["record_from"] > window_start)
        percent = best.get("percent") if best else None
        if percent and spent["dispatches"]:
            row["measured_cost"] = round(percent / spent["dispatches"], 2)
            row["configured_cost"] = dispatch_cost(config, name, 1)
            row["verdict"] = "measured"
        elif spent["dispatches"] and name == "claude" and percent is None:
            # Orca keeps only the most recent Claude sessions and counts no
            # cache creation, so its totals are not a budget. rightsize's own
            # scan reads every transcript and is the number to size against.
            measured = claude_tokens(7 * 86400)
            row["tokens_in_window"] = measured
            # A budget has to leave room above the reserve, or declaring one
            # blocks the provider the moment it is set: measured x 1.2 against a
            # 30 point reserve reads as 84 per cent used and -14 points usable.
            reserve = float((config.get("reserves") or {}).get("claude", 30))
            headroom_wanted = 15.0
            row["suggested_budget"] = int(measured / max(0.05, (100 - reserve - headroom_wanted) / 100))
            row["budget_note"] = (f"leaves about {headroom_wanted:.0f} points usable above the"
                                  f" {reserve:.0f} point reserve")
            row["token_source"] = "rightsize transcript scan (Orca's totals omit cache creation)"
            row["verdict"] = "native percentage unavailable; fallback budget suggestion only"
        else:
            row["verdict"] = "no dispatches recorded in the current window"
        rows.append(row)
    return rows


def log_decision(decision: dict, spec: str, dispatched: bool = False) -> None:
    """Remember what was recommended, so it can be checked against what ran.

    A recommendation nobody can verify is a recommendation nobody has to
    follow. For opencode the model is chosen inside the terminal rather than by
    a launch flag, so Orca records the provider and a null model, and "the pick
    was applied" looks exactly like "the pick was ignored and the config default
    ran". This log is the half rightsize can supply.

    `dispatched` separates a route run to read the numbers from one that a
    launch followed. Auditing the two together buries the dispatches that
    really did go missing among the ones nobody ever started.
    """
    if not decision.get("pick"):
        return
    with state_lock():
        state = load_json(STATE, {}) or {}
        entries = state.get("decisions") or []
        # One dispatch, one decision. The hook routes on PreToolUse to advise
        # and again on PostToolUse to reserve, so the same task arrives twice
        # within seconds and would be audited as two dispatches.
        key = brief_key(spec)
        for existing in reversed(entries[-20:]):
            if (existing["at"] > now() - 120 and brief_key(existing.get("task") or "") == key
                    and existing.get("model") == decision["pick"]["model"]
                    and existing.get("judgment_source") == (decision.get("judgment") or {}).get("source")
                    and existing.get("task_sha256") == hashlib.sha256(spec.encode("utf-8")).hexdigest()):
                # The second visit is the one that knows a launch happened.
                if dispatched and not existing.get("dispatched"):
                    existing["dispatched"] = True
                    state["decisions"] = entries
                    save_json(STATE, state)
                return
        entries.append({
            "at": now(),
            "provider": decision["pick"]["provider"],
            "model": decision["pick"]["model"],
            "effort": decision["pick"].get("effort"),
            "band": decision["band"],
            "name": decision.get("worktree_name"),
            "task": " ".join(spec.split())[:120],
            "dispatched": dispatched,
            "judgment_source": (decision.get("judgment") or {}).get("source"),
            "task_sha256": hashlib.sha256(spec.encode("utf-8")).hexdigest(),
        })
        state["decisions"] = entries[-200:]
        save_json(STATE, state)

def opencode_sessions_since(epoch: float) -> list[dict]:
    """What opencode actually ran, from its own database, read-only."""
    if not OPENCODE_DB.exists():
        return []
    query = """
        SELECT s.directory,
               json_extract(m.data, '$.modelID')    AS model,
               json_extract(m.data, '$.providerID') AS provider,
               count(*)                             AS messages,
               max(m.time_created) / 1000.0         AS last_at,
               max(s.tokens_input)                  AS tokens_in,
               max(s.tokens_output)                 AS tokens_out,
               max(s.tokens_cache_read)             AS cached,
               max(s.cost)                          AS cost,
               s.id, s.time_created / 1000.0
        FROM session s JOIN message m ON m.session_id = s.id
        -- Assistant rows only: a user turn carries no model, and a bare column
        -- beside max() takes its value from whichever row that max matched, so
        -- mixing them silently returned a null model for every session.
        WHERE s.time_created > ?
          AND json_extract(m.data, '$.role') = 'assistant'
          AND json_extract(m.data, '$.modelID') IS NOT NULL
        GROUP BY s.id, model
    """
    try:
        with contextlib.closing(sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=5)) as db:
            rows = db.execute(query, (int(epoch * 1000),)).fetchall()
    except sqlite3.Error:
        return []
    return [{"directory": r[0] or "", "model": r[1], "provider": r[2], "messages": r[3],
             "last_at": r[4], "tokens_in": r[5] or 0, "tokens_out": r[6] or 0,
             "cached": r[7] or 0, "cost": r[8] or 0.0,
             "session_id": r[9], "started_at": r[10]} for r in rows if r[1]]


def audit(config: dict, days: float = 7.0) -> dict:
    """Compare what was recommended with what ran."""
    state = load_json(STATE, {}) or {}
    since = now() - days * 86400
    decisions = [d for d in (state.get("decisions") or []) if d["at"] >= since]
    sessions = opencode_sessions_since(since)
    # What the orchestrator thought of each worker, keyed by worktree. A pick
    # that was obeyed and then failed is the case rightsize could not see at
    # all: audit knew which model ran, never whether it got anywhere.
    settled, _ = orca_settled()
    outcomes = {w["worktree"]: w["state"] for w in settled if w.get("worktree")}
    by_dispatch = {w["dispatch"]: w for w in settled if w.get("dispatch")}
    # The brief is the key both sides share; the worktree name is only the one
    # rightsize suggested.
    by_brief = orca_task_index()
    rows, matched_sessions = [], set()
    for decision in decisions:
        if decision["provider"] not in OPENCODE_LAUNCHED:
            # The model cannot be confirmed for these, because only opencode
            # keeps a local record of what it ran. The outcome still can, and
            # for band 3 work that is the half worth knowing.
            matched = (by_dispatch.get(decision.get("dispatch")) if decision.get("dispatch")
                       else by_brief.get(brief_key(decision.get("task") or "")))
            outcome = (matched or {}).get("state")
            if not decision.get("dispatch"):
                outcome = outcome or outcomes.get(decision.get("name") or "")
            if not outcome and (matched or {}).get("status") in ("completed", "failed"):
                outcome = "succeeded" if matched["status"] == "completed" else "failed"
            if outcome in ("failed", "stopped", "abandoned", "timed_out", "cancelled"):
                rows.append({**decision, "verdict": "worker " + outcome, "outcome": outcome,
                             "why": "worker outcome known; model execution not verified"})
            elif outcome == "succeeded":
                rows.append({**decision, "verdict": "finished", "outcome": outcome,
                             "why": f"{decision['provider']} keeps no local record of the model,"
                                    " but the worker succeeded"})
            else:
                rows.append({**decision, "verdict": "not checkable",
                             "why": f"{decision['provider']} keeps no local record of the model,"
                                    " and no worker of that name has settled"})
            continue
        # The worktree rightsize suggested, or the one the coordinator actually
        # used, found by the brief they share.
        name = decision.get("name") or ""
        bound = by_dispatch.get(decision.get("dispatch")) or {}
        actual = bound.get("worktree") or (by_brief.get(brief_key(decision.get("task") or "")) or {}).get("worktree")
        wanted = {n for n in (name, actual) if n}
        found = [x for x in sessions
                 if (x["directory"] == decision.get("directory") if decision.get("directory")
                     else Path(x["directory"]).name in wanted)
                 and decision["at"] - 300 <= x["started_at"] <= decision["at"] + 600]
        if not found:
            if not decision.get("dispatched"):
                rows.append({**decision, "verdict": "unknown dispatch",
                             "why": "no launch receipt or matching session; launch status is unknown"})
            else:
                rows.append({**decision, "verdict": "no session",
                             "why": "a launch/hold was recorded but no session could be matched"})
            continue
        if len(found) != 1:
            rows.append({**decision, "verdict": "ambiguous session",
                         "why": "multiple sessions/models match; cannot attribute this attempt"})
            continue
        ran = found[0]
        matched_sessions.add((ran["session_id"], ran["model"]))
        outcome = bound.get("state") or outcomes.get(actual or name)
        verdict = "mismatch"
        expected_provider = (config.get("model_prefixes", {}).get(decision["provider"]) or "").rstrip("/")
        if ran["model"] == decision["model"] and ran["provider"] == expected_provider:
            verdict = "model verified" if decision.get("observed_launch") else "ran as picked"
        why = None
        if ran["tokens_out"] == 0:
            verdict = "zero output"
            why = "session exists but has no recorded output tokens; completion is unproven"
        if verdict == "ran as picked" and outcome in ("failed", "stopped", "abandoned",
                                                      "timed_out", "cancelled"):
            verdict = "obeyed but " + outcome
        rows.append({**decision, "verdict": verdict, "ran_model": ran["model"],
                     "messages": ran["messages"], "directory": ran["directory"],
                     "session_id": ran["session_id"], "outcome": outcome,
                     "why": why or f"session model {ran['model']} verified"})
    counts = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    # Capacity held by a dispatch that is not running. Either it never
    # launched, or it finished and nobody said so.
    stale = []
    for held in sweep_reservations(state):
        if now() - held["at"] <= 900:
            continue
        worktree = held.get("worktree")
        active = [x for x in sessions
                  if worktree and Path(x["directory"]).name == worktree
                  and now() - x["last_at"] < 900]
        if not active:
            stale.append(held)
    untracked = [{**s, "verdict": "zero output" if s["tokens_out"] == 0 else "untracked session"}
                 for s in sessions if (s["session_id"], s["model"]) not in matched_sessions]
    return {"decisions": rows, "counts": counts, "sessions_seen": len(sessions),
            "untracked_sessions": untracked,
            "held": [{"provider": h["provider"], "points": h["points"], "age": now() - h["at"],
                      "worktree": h.get("worktree"), "task": h["task"]} for h in stale]}


def cmd_audit(args, config):
    result = audit(config, days=args.days)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if not result["decisions"]:
        print(f"no decisions recorded in the last {args.days:g} days."
              " Routing records one per dispatch; this fills as you use it.")
        if not result["untracked_sessions"]:
            return 0
    order = {"mismatch": 0, "no session": 1, "ran as picked": 2, "finished": 3,
             "not checkable": 4, "not dispatched": 5}
    for row in sorted(result["decisions"],
                      key=lambda r: (order.get(r["verdict"], -1), -r["at"])):
        failed = row["verdict"].startswith("obeyed but")
        mark = row["verdict"].upper() if row["verdict"] == "mismatch" or failed else row["verdict"]
        line = f"{mark:<14} {human_age(now() - row['at']):>7} ago  band {row['band']}  {row['name'] or '-'}"
        print(line)
        if row["verdict"] == "mismatch":
            print(f"               picked {row['provider']}:{row['model']}, ran {row['ran_model']}"
                  f" over {row['messages']} messages")
        elif row["verdict"] == "ran as picked":
            print(f"               {row['model']}, {row['messages']} messages")
        elif row["verdict"].startswith("obeyed but"):
            ran = (f"ran {row['ran_model']} over {row['messages']} messages and the worker"
                   if row["messages"] else f"the {row['provider']} worker")
            print(f"               {ran} {row['outcome']}")
            print(f"               rightsize rerun --task {shlex.quote(row['task'])}"
                  f" --previous {row['provider']}:{row['model']}"
                  f" --because \"<what went wrong>\"")
        else:
            print(f"               {row['why']}")
    print()
    print("  ".join(f"{count} {verdict}" for verdict, count in sorted(result["counts"].items())))
    for session in result["untracked_sessions"]:
        print(f"{session['verdict']:<18} {session['directory']} {session['model']}"
              f" session={session['session_id']} output={session['tokens_out']}")
    for held in result["held"]:
        print(f"held           {held['provider']} {held['points']} points for"
              f" {human_age(held['age'])}, nothing running in {held['worktree'] or 'any worktree'}:"
              f" rightsize report {held['provider']} --done")
    if result["counts"].get("mismatch"):
        print("\nA mismatch means the worker ran a different model than the one picked."
              "\nFor opencode that usually means the `opencode -m <model>` line was not run,"
              "\nso the worker fell back to the default in ~/.config/opencode/opencode.json.")
        return 1
    return 0


def cmd_calibrate(args, config):
    rows = calibrate(config)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    changes = {}
    for row in rows:
        print(f"{row['provider']}")
        if "measured_cost" in row:
            print(f"  {row['bucket']} window opened {human_age(now() - row['window_started'])} ago, "
                  f"{row['dispatches']} dispatches since"
                  f"  (a point means a different amount in each window; this is the"
                  f" tightest one lasting a day or more)")
            print(f"  measured {row['measured_cost']} points per dispatch, "
                  f"config says {row['configured_cost']}")
            print(f"  tokens {row['input']:,} in ({row['cached']:,} cached), {row['output']:,} out"
                  + (f", {(row['input'] + row['output']) // row['dispatches']:,} per dispatch"
                     if row["dispatches"] else ""))
            for model, count in sorted(row["models"].items(), key=lambda kv: -kv[1])[:4]:
                print(f"    {count:>4}  {model}")
            if row.get("partial_record"):
                print("  the session record starts inside this window, so dispatches are"
                      " undercounted and this cost is an upper bound")
            if row.get("record_to") and now() - row["record_to"] > 6 * 3600:
                print(f"  last recorded session was {human_age(now() - row['record_to'])} ago;"
                      " anything since is uncounted, which also inflates the cost")
            if abs(row["measured_cost"] - row["configured_cost"]) >= 0.05:
                changes[row["provider"]] = row["measured_cost"]
                print(f"  -> set dispatch_cost.{row['provider']} to {row['measured_cost']}")
        elif "suggested_budget" in row:
            print(f"  {row['dispatches']} sessions recorded; {row['tokens_in_window']:,} tokens"
                  f" over 7 days, counted by {row['token_source']}")
            print(f"  -> set claude.weekly_token_budget to {row['suggested_budget']:,},"
                  f" which {row['budget_note']}")
        else:
            print(f"  {row['verdict']}")
    if not changes:
        print("\nnothing to change: the configured costs match what was measured")
        return 0
    if not args.apply:
        print("\nre-run with --apply to write these into config.json")
        return 0
    current = load_json(CONFIG) or {}
    current.setdefault("dispatch_cost", {}).update(changes)
    CONFIG.write_text(json.dumps(current, indent=2) + "\n")
    print(f"\nwrote {', '.join(f'{k}={v}' for k, v in changes.items())} to {CONFIG}")
    return 0


def cmd_doctor(args, config):
    findings = doctor(config)
    for level, message in findings:
        print(f"{level.upper():<6} {message}")
    return 1 if any(level == "error" for level, _ in findings) else 0


def cmd_context(args, config):
    import context_manifest
    from urllib.parse import urlparse
    def resource_path(value):
        return value if urlparse(value).scheme else str(Path(value).resolve())
    try:
        if args.spec:
            with Path(args.spec).open("rb") as handle:
                data = handle.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise context_manifest.ContextError("task exceeds 1 MiB")
            task = data.decode("utf-8")
        else:
            task = args.task
        optional = []
        for value in args.optional_skill:
            name, separator, reason = value.partition("=")
            if not separator:
                raise context_manifest.ContextError("optional selection must be NAME=REASON")
            optional.append((name, reason))
        result = context_manifest.build(
            task, args.catalog, args.root, skills=args.skill, optional=optional,
            rules=[resource_path(path) for path in args.rule],
            references=[resource_path(path) for path in args.reference],
            max_bytes=args.max_bytes, max_optional=args.max_optional)
    except (OSError, UnicodeError, context_manifest.ContextError) as exc:
        # Filesystem exception messages may expose paths; catalog metadata is
        # untrusted. Only our fixed validation text is returned.
        reason = str(exc) if isinstance(exc, context_manifest.ContextError) else "context input unavailable or not UTF-8"
        print(json.dumps({"error": "invalid-context", "reason": reason}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_managed(args, config):
    import managed_router
    from managed_ledger import Ledger, LedgerError
    try:
        if args.action in ("migrate", "rollback"):
            import state_migration
            operation = state_migration.migrate if args.action == "migrate" else state_migration.rollback
            result = operation(sys.modules[__name__], apply=args.apply)
        elif args.action == "status":
            result = Ledger(managed_router.ledger_path(sys.modules[__name__])).read(args.attempt)
        elif args.action == "run":
            import native_runs
            result = native_runs.execute(args, config, sys.modules[__name__])
        elif args.action == "reconcile":
            import native_runs
            result = native_runs.reconcile(args, config, sys.modules[__name__])
        elif args.action == "review":
            with Path(args.evidence).open("rb") as handle:
                evidence = handle.read(1024 * 1024 + 1)
            if not evidence or len(evidence) > 1024 * 1024:
                raise LedgerError("review evidence must be nonempty and at most 1 MiB")
            digest = hashlib.sha256(evidence).hexdigest()
            ledger = Ledger(managed_router.ledger_path(sys.modules[__name__]))
            result = ledger.event(args.attempt, "review:" + args.attempt + ":" + digest,
                                  "accepted" if args.accept else "rejected", evidence="sha256:" + digest)
            result = {"status": result["attempt"]["state"], "attempt": result["attempt"]}
        elif args.action == "cancel":
            ledger = Ledger(managed_router.ledger_path(sys.modules[__name__]))
            attempt = ledger.read(args.attempt)
            if not attempt:
                raise LedgerError("attempt not found")
            result = ledger.event(args.attempt, attempt["launch_key"] + ":cancel-request", "cancelling")
            result = {"status": result["attempt"]["state"], "attempt": result["attempt"]}
        else:
            result = managed_router.execute(args, config, sys.modules[__name__])
    except (LedgerError, OSError, sqlite3.DatabaseError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, LedgerError) else "managed input or state unavailable/invalid"
        print(json.dumps({"status": "error", "reason": reason}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if not isinstance(result, dict) or result.get("status") != "wait" else 3


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rightsize", description=__doc__)
    parser.add_argument("--account", action="append", default=[], metavar="PROVIDER=REFERENCE",
                        help="select a configured native account binding for this invocation")
    sub = parser.add_subparsers(dest="command", required=True)

    for action in ("replay", "shadow"):
        offline = sub.add_parser(action, help="offline decisions from explicit snapshots; no probes or launches")
        offline.add_argument("--snapshot", required=True, help="version 1 replay/shadow JSON")

    managed_cmd = sub.add_parser("managed", help="opt-in account-aware admission; does not launch by itself")
    managed_sub = managed_cmd.add_subparsers(dest="action", required=True)
    for action in ("migrate", "rollback"):
        transition = managed_sub.add_parser(action, help="preview state transition; --apply performs it")
        transition.add_argument("--apply", action="store_true")
        transition.set_defaults(func=cmd_managed, json=True)
    for action in ("plan", "admit"):
        managed_action = managed_sub.add_parser(action)
        managed_action.add_argument("--spec", required=True)
        managed_action.add_argument("--context-manifest", help="selected context JSON bound to this task")
        managed_action.add_argument("--context-root", action="append", help="explicitly approve a context source root")
        managed_action.add_argument("--judgment", required=True)
        managed_action.add_argument("--task-id", required=True)
        managed_action.add_argument("--floor", type=int, choices=(1, 2, 3), default=1)
        if action == "admit":
            managed_action.add_argument("--request-id", required=True, help="stable idempotency key for this intent")
        managed_action.set_defaults(func=cmd_managed, json=True)
    managed_status = managed_sub.add_parser("status")
    managed_status.add_argument("--attempt")
    managed_status.set_defaults(func=cmd_managed, json=True)
    managed_run = managed_sub.add_parser("run", help="run an admitted task in a new isolated worktree")
    managed_run.add_argument("--attempt", required=True)
    managed_run.add_argument("--spec", required=True)
    managed_run.add_argument("--context-manifest", help="same context manifest used at admission")
    managed_run.add_argument("--context-root", action="append", help="reapprove context source roots before launch")
    managed_run.add_argument("--repo", required=True, help="source Git repository; runs from committed HEAD")
    managed_run.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="read-only")
    managed_run.add_argument("--timeout", type=int, default=300)
    managed_run.set_defaults(func=cmd_managed, json=True)
    managed_cancel = managed_sub.add_parser("cancel", help="request cancellation from the running native owner")
    managed_cancel.add_argument("--attempt", required=True)
    managed_cancel.set_defaults(func=cmd_managed, json=True)
    managed_reconcile = managed_sub.add_parser("reconcile", help="read native outcome without relaunching")
    managed_reconcile.add_argument("--attempt", required=True)
    managed_reconcile.set_defaults(func=cmd_managed, json=True)
    managed_review = managed_sub.add_parser("review", help="record an explicit review backed by evidence")
    managed_review.add_argument("--attempt", required=True)
    managed_review.add_argument("--evidence", required=True, help="local review/test evidence; only its hash is recorded")
    review_outcome = managed_review.add_mutually_exclusive_group(required=True)
    review_outcome.add_argument("--accept", action="store_true")
    review_outcome.add_argument("--reject", action="store_true")
    managed_review.set_defaults(func=cmd_managed, json=True)

    context_cmd = sub.add_parser("context", help="build a bounded context manifest from a host-normalized catalog")
    context_task = context_cmd.add_mutually_exclusive_group(required=True)
    context_task.add_argument("--task")
    context_task.add_argument("--spec")
    context_cmd.add_argument("--catalog", required=True, help="normalized JSON skill catalog")
    context_cmd.add_argument("--root", action="append", required=True, help="approved local root; repeatable")
    context_cmd.add_argument("--skill", action="append", default=[], help="explicitly requested skill name")
    context_cmd.add_argument("--optional-skill", action="append", default=[], metavar="NAME=REASON")
    context_cmd.add_argument("--rule", action="append", default=[], help="mandatory repository instruction file")
    context_cmd.add_argument("--reference", action="append", default=[], help="selected knowledge file, treated as data")
    context_cmd.add_argument("--max-bytes", type=int, default=65536)
    context_cmd.add_argument("--max-optional", type=int, default=3)
    context_cmd.set_defaults(func=cmd_context, json=True)

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
    route_cmd.add_argument("--orca", action="store_true", help="shorthand for --launcher orca")
    route_cmd.add_argument("--launcher", help="also print the launch command for this launcher (see config.json)")
    route_cmd.add_argument("--fresh", action="store_true", help="re-probe instead of using the cached reading")
    route_cmd.add_argument("--judgment", help="versioned task-bound judgment JSON from the active coding agent; skips the judge call")
    route_cmd.add_argument("--reserve", action="store_true",
                           help="hold this dispatch's estimated cost until it is reported done")
    route_cmd.set_defaults(func=cmd_route)

    rerun_cmd = sub.add_parser("rerun", help="route again after an attempt did not work out")
    rerun_group = rerun_cmd.add_mutually_exclusive_group(required=True)
    rerun_group.add_argument("--task", help="the same task description")
    rerun_group.add_argument("--spec", help="the same spec file")
    rerun_cmd.add_argument("--because", required=True,
                           help="what happened: the failure, the review finding, what it got stuck on")
    rerun_cmd.add_argument("--previous", help="provider:model that already tried, so it is not picked again")
    rerun_cmd.add_argument("--json", action="store_true")
    rerun_cmd.add_argument("--orca", action="store_true", help="shorthand for --launcher orca")
    rerun_cmd.add_argument("--launcher", help="also print the launch command")
    rerun_cmd.set_defaults(func=cmd_rerun)

    plan_cmd = sub.add_parser("plan", help="route a whole fan-out at once, spreading it across plans")
    plan_cmd.add_argument("--specs", help="file with one task per line")
    plan_cmd.add_argument("--dir", help="directory of spec files")
    plan_cmd.add_argument("--glob", default="*.md", help="pattern inside --dir (default *.md)")
    plan_cmd.add_argument("--concurrency", type=int, default=8, help="judgments in flight at once")
    plan_cmd.add_argument("--reserve", action="store_true",
                          help="hold each dispatch's estimated cost until it is reported done")
    plan_cmd.add_argument("--launcher", help="also print a launch command per task")
    plan_cmd.add_argument("--json", action="store_true")
    plan_cmd.set_defaults(func=cmd_plan)

    audit_cmd = sub.add_parser("audit", help="did workers run the model that was picked for them")
    audit_cmd.add_argument("--days", type=float, default=7.0)
    audit_cmd.add_argument("--json", action="store_true")
    audit_cmd.set_defaults(func=cmd_audit)

    calibrate_cmd = sub.add_parser(
        "calibrate", help="measure what a dispatch actually costs, from recorded sessions")
    calibrate_cmd.add_argument("--apply", action="store_true",
                               help="write the measured costs into config.json")
    calibrate_cmd.add_argument("--json", action="store_true")
    calibrate_cmd.set_defaults(func=cmd_calibrate)

    doctor_cmd = sub.add_parser("doctor", help="preflight: config, catalogue, credentials, state")
    doctor_cmd.set_defaults(func=cmd_doctor)

    models = sub.add_parser("models", help="what the current registry offers")
    models.add_argument("--limit", type=int, default=12)
    models.set_defaults(func=cmd_models)

    report = sub.add_parser("report", help="report a dispatch outcome back to the router")
    report.add_argument("provider", nargs="?", help="the provider the worker ran on")
    report.add_argument("--from-orca", action="store_true",
                        help="ask Orca which workers have settled and release their capacity")
    report.add_argument("--run", help="limit --from-orca to one Run id")
    report.add_argument("--started", action="store_true", help="record the actual successful launch without routing again")
    report.add_argument("--model", help="model actually launched")
    report.add_argument("--effort", help="effort actually launched")
    report.add_argument("--task", help="launched task brief")
    report.add_argument("--dispatch", help="orchestrator dispatch id or stable tool-call id")
    report.add_argument("--worktree", help="actual worktree path or name")
    report.add_argument("--quota-error", action="store_true", default=True,
                        help="the worker failed on quota (default)")
    report.add_argument("--free-request", action="store_true",
                        help="count one OpenRouter free-tier request instead")
    report.add_argument("--clear", action="store_true", help="clear an exhausted mark early")
    report.add_argument("--done", action="store_true",
                        help="a dispatch finished: release the capacity it was holding")
    report.add_argument("--id", help="which reservation to release (default: the oldest)")
    report.add_argument("--minutes", type=int, default=60,
                        help="cooldown when the provider publishes no reset time")
    report.set_defaults(func=cmd_report)

    deals_cmd = sub.add_parser("deals", help="free models, price drops and catalogue changes")
    deals_cmd.add_argument("--limit", type=int, default=12)
    deals_cmd.set_defaults(func=cmd_deals)

    args = parser.parse_args(argv)
    if args.command in ("replay", "shadow"):
        import replay
        return replay.command(args, sys.modules[__name__])
    config, overlay = load_config()
    if config is None:
        print(f"config missing: {CONFIG}", file=sys.stderr)
        return 2
    for selection in args.account:
        provider, separator, reference = selection.partition("=")
        if not separator or provider not in accounts.RUNTIMES or not reference:
            parser.error("--account must be PROVIDER=REFERENCE for a supported provider")
        config.setdefault("accounts", {})[provider] = reference
    if overlay and not getattr(args, "json", False):
        print(f"# config: {CONFIG} + {overlay}", file=sys.stderr)
    global STATE
    previous_state = STATE
    try:
        if not (args.command == "managed" and args.action in ("migrate", "rollback")):
            import state_migration
            STATE = state_migration.active_path(STATE)
        return args.func(args, config)
    except (OSError, ValueError) as exc:
        from managed_ledger import LedgerError
        if not isinstance(exc, LedgerError):
            raise
        print(json.dumps({"status": "error", "reason": str(exc)}), file=sys.stderr)
        return 2
    finally:
        STATE = previous_state


if __name__ == "__main__":
    raise SystemExit(main())
