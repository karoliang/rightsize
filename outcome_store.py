"""Advisory evidence store for rightsize decisions, launches, and events.

Observational only. Never blocks work, owns managed quota, or verifies accounts.
Caller-controlled path; permission 0600 on create. SQLite serialises writes via
BEGIN IMMEDIATE; readers use a read-only URI handle so audits do not create
the database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import fcntl
import math
import statistics
from contextlib import contextmanager, closing, nullcontext
from pathlib import Path
from typing import Mapping


SCHEMA_VERSION = 1

_DECISION_FIELDS = frozenset({
    "decision_id", "project", "task_sha256", "policy_revision", "config_sha256",
    "selected", "judgment_source", "review_required", "quality_floor", "task_tier", "at",
})
_LAUNCH_FIELDS = frozenset({
    "dispatch_id", "decision_id", "project", "task_sha256", "actual",
    "override_reason", "worktree", "native_session_id", "account_status", "at",
})
_PROVIDER_FIELDS = frozenset({"provider", "model", "effort", "family"})

_EVENT_KINDS = frozenset({
    "completed", "failed", "cancelled", "review", "rework", "usage", "accepted",
})
_TERMINAL_KINDS = frozenset({"completed", "failed", "cancelled"})
_REVIEW_VERDICTS = frozenset({"accepted", "rejected"})

_REVIEW_DATA = frozenset({"actor", "provider", "model", "family", "verdict", "evidence_sha256"})
_REWORK_DATA = frozenset({"actor", "seconds", "evidence_sha256"})
_USAGE_REQUIRED = frozenset({"source"})
_USAGE_OPTIONAL = frozenset({"input_tokens", "output_tokens", "cache_read_tokens"})
_ACCEPT_DATA = frozenset({"actor", "evidence_sha256"})
_TERMINAL_DATA = frozenset({"evidence_sha256"})

_HEX = "0123456789abcdef"


class OutcomeError(ValueError):
    """Raised for validation, conflict, and acceptance-gate failures."""


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _is_sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def _is_finite_number(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    if isinstance(value, float):
        return value == value and abs(value) != float("inf")
    return False


def _is_nonneg_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_nonneg_number(value):
    return _is_finite_number(value) and value >= 0


def _validate_evidence(sha, where):
    if not _is_sha256(sha):
        raise OutcomeError(f"{where}: evidence_sha256 must be 64-char lowercase hex")


def _check_unknown(record, allowed, where):
    missing = allowed - set(record)
    if missing:
        raise OutcomeError(f"{where}: missing fields {sorted(missing)}")
    extra = set(record) - allowed
    if extra:
        raise OutcomeError(f"{where}: unknown fields {sorted(extra)}")


def _require_nonempty_str(record, key, where):
    value = record[key]
    if not isinstance(value, str) or not value.strip():
        raise OutcomeError(f"{where}.{key} must be a nonempty string")
    return value


def _optional_nonempty_str(record, key, where):
    value = record[key]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise OutcomeError(f"{where}.{key} must be a nonempty string or null")
    return value


def _validate_provider_map(value, allowed, where):
    if not isinstance(value, Mapping):
        raise OutcomeError(f"{where}: must be a mapping")
    _check_unknown(value, allowed, where)
    out = {}
    for key in allowed:
        v = value[key]
        if key == "effort" and v is None:
            out[key] = None
            continue
        if not isinstance(v, str) or not v.strip():
            raise OutcomeError(f"{where}.{key} must be a nonempty string")
        out[key] = v
    return out


def _validate_decision(record):
    if not isinstance(record, Mapping):
        raise OutcomeError("decision record must be a mapping")
    _check_unknown(record, _DECISION_FIELDS, "decision")
    decision_id = _require_nonempty_str(record, "decision_id", "decision")
    project = _require_nonempty_str(record, "project", "decision")
    task_sha256 = record["task_sha256"]
    if not _is_sha256(task_sha256):
        raise OutcomeError("decision.task_sha256 must be 64-char lowercase hex")
    policy_revision = record["policy_revision"]
    if not _is_sha256(policy_revision):
        raise OutcomeError("policy_revision must be a source SHA256")
    config_sha256 = record["config_sha256"]
    if not _is_sha256(config_sha256):
        raise OutcomeError("decision.config_sha256 must be 64-char lowercase hex")
    raw_selected = record["selected"]
    if raw_selected is None:
        selected = None
    elif not isinstance(raw_selected, Mapping):
        raise OutcomeError("decision.selected must be a mapping or null")
    else:
        selected = _validate_provider_map(raw_selected, _PROVIDER_FIELDS, "decision.selected")
    judgment_source = _require_nonempty_str(record, "judgment_source", "decision")
    review_required = record["review_required"]
    if not isinstance(review_required, bool):
        raise OutcomeError("decision.review_required must be a bool")
    at = record["at"]
    if not _is_nonneg_number(at):
        raise OutcomeError("decision.at must be a finite number")
    tier, floor = record["task_tier"], record["quality_floor"]
    if tier not in ("mechanical", "implementation", "design", "diagnosis", "high_stakes"):
        raise OutcomeError("invalid task_tier")
    if type(floor) is not int or not 1 <= floor <= 3:
        raise OutcomeError("invalid quality_floor")
    if tier == "high_stakes" and (not review_required or floor != 3):
        raise OutcomeError("high_stakes requires floor3 and independent review")
    return {
        "task_tier": tier, "quality_floor": floor,
        "decision_id": decision_id, "project": project, "task_sha256": task_sha256,
        "policy_revision": policy_revision, "config_sha256": config_sha256,
        "selected": selected, "judgment_source": judgment_source,
        "review_required": review_required, "at": at,
    }


def _validate_launch(record, decisions):
    if not isinstance(record, Mapping):
        raise OutcomeError("launch record must be a mapping")
    _check_unknown(record, _LAUNCH_FIELDS, "launch")
    dispatch_id = _require_nonempty_str(record, "dispatch_id", "launch")
    decision_id = record["decision_id"]
    if decision_id is not None and not isinstance(decision_id, str):
        raise OutcomeError("launch.decision_id must be a string or null")
    project = _require_nonempty_str(record, "project", "launch")
    task_sha256 = record["task_sha256"]
    if not _is_sha256(task_sha256):
        raise OutcomeError("launch.task_sha256 must be 64-char lowercase hex")
    actual = _validate_provider_map(record["actual"], _PROVIDER_FIELDS, "launch.actual")
    override_reason = _optional_nonempty_str(record, "override_reason", "launch")
    worktree = _optional_nonempty_str(record, "worktree", "launch")
    native_session_id = _optional_nonempty_str(record, "native_session_id", "launch")
    account_status = record["account_status"]
    if account_status not in ("unverified", "verified"):
        raise OutcomeError("launch.account_status must be 'unverified' or 'verified'")
    at = record["at"]
    if not _is_nonneg_number(at):
        raise OutcomeError("launch.at must be a finite number")
    if decision_id is not None:
        decision = decisions.get(decision_id)
        if decision is None:
            raise OutcomeError(f"launch references unknown decision_id {decision_id!r}")
        if decision["project"] != project:
            raise OutcomeError("launch.project must match decision.project")
        if decision["task_sha256"] != task_sha256:
            raise OutcomeError("launch.task_sha256 must match decision.task_sha256")
        if at < decision["at"]:
            raise OutcomeError("launch predates decision")
        selected = decision["selected"]
        if selected is None and not override_reason:
            raise OutcomeError("launch without a recommendation requires override_reason")
        if selected is not None:
            diffs = [k for k in ("provider", "model", "effort")
                     if selected[k] != actual[k]]
            if diffs and not override_reason:
                raise OutcomeError(
                    f"launch.actual differs from decision.selected on {diffs} "
                    "but override_reason is empty"
                )
    return {
        "dispatch_id": dispatch_id, "decision_id": decision_id, "project": project,
        "task_sha256": task_sha256, "actual": actual,
        "override_reason": override_reason, "worktree": worktree,
        "native_session_id": native_session_id, "account_status": account_status, "at": at,
    }


def _validate_event_data(kind, data):
    if not isinstance(data, Mapping):
        raise OutcomeError(f"event.data must be a mapping for kind {kind!r}")
    if kind in _TERMINAL_KINDS:
        _check_unknown(data, _TERMINAL_DATA, f"{kind}.data")
        _validate_evidence(data["evidence_sha256"], f"{kind}.data")
        return
    if kind == "review":
        _check_unknown(data, _REVIEW_DATA, "review.data")
        for key in ("actor", "provider", "model", "family"):
            _require_nonempty_str(data, key, "review.data")
        verdict = data["verdict"]
        if verdict not in _REVIEW_VERDICTS:
            raise OutcomeError(
                f"review.data.verdict must be one of {sorted(_REVIEW_VERDICTS)}"
            )
        _validate_evidence(data["evidence_sha256"], "review.data")
        return
    if kind == "rework":
        _check_unknown(data, _REWORK_DATA, "rework.data")
        _require_nonempty_str(data, "actor", "rework.data")
        seconds = data["seconds"]
        if not _is_nonneg_number(seconds):
            raise OutcomeError("rework.data.seconds must be a finite nonnegative number")
        _validate_evidence(data["evidence_sha256"], "rework.data")
        return
    if kind == "usage":
        if not _USAGE_REQUIRED <= set(data) or set(data) - (_USAGE_REQUIRED | _USAGE_OPTIONAL):
            raise OutcomeError("usage fields must be source and optional token categories")
        _require_nonempty_str(data, "source", "usage.data")
        for key in _USAGE_OPTIONAL:
            if key in data and not _is_nonneg_int(data[key]):
                raise OutcomeError(
                    f"usage.data.{key} must be a nonnegative int (bool rejected)"
                )
        return
    if kind == "accepted":
        _check_unknown(data, _ACCEPT_DATA, "accepted.data")
        _require_nonempty_str(data, "actor", "accepted.data")
        _validate_evidence(data["evidence_sha256"], "accepted.data")
        return
    raise OutcomeError(f"unknown event kind {kind!r}")


class OutcomeStore:
    """Transactional immutable evidence. No managed admission or network effects."""

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _read(self):
        lock_path = self.path.with_name(self.path.name + ".lock")
        with (lock_path.open("rb") if lock_path.exists() else nullcontext(None)) as lock:
            if lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
            try:
                if not self.path.exists():
                    yield None
                    return
                with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                        raise OutcomeError("unsupported outcome schema")
                    db.execute("BEGIN")
                    yield db
            finally:
                if lock:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                first = not self.path.exists()
                if first:
                    os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
                with closing(sqlite3.connect(self.path, timeout=15, isolation_level=None)) as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        if first:
                            for table, key in (("decisions", "decision_id"), ("launches", "dispatch_id")):
                                db.execute(f"CREATE TABLE {table} ({key} TEXT PRIMARY KEY, record_json TEXT NOT NULL)")
                            db.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, dispatch_id TEXT NOT NULL, record_json TEXT NOT NULL)")
                            db.execute("CREATE INDEX events_dispatch ON events(dispatch_id)")
                            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                        elif db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                            raise OutcomeError("unsupported outcome schema")
                        yield db
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _get(db, table, key, value):
        if db is None:
            return None
        row = db.execute(f"SELECT record_json FROM {table} WHERE {key}=?", (value,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _all(db, table):
        if db is None:
            return []
        return [json.loads(row[0]) for row in db.execute(f"SELECT record_json FROM {table} ORDER BY rowid")]

    def _insert(self, db, table, key, record):
        existing = self._get(db, table, key, record[key])
        if existing is not None:
            if _encode(existing) != _encode(record):
                raise OutcomeError(f"conflicting {key}")
            return existing
        if table == "events":
            db.execute("INSERT INTO events VALUES (?,?,?)", (record[key], record["dispatch_id"], _encode(record)))
        else:
            db.execute(f"INSERT INTO {table} VALUES (?,?)", (record[key], _encode(record)))
        return record

    @staticmethod
    def _events(db, dispatch):
        if db is None:
            return []
        return [json.loads(row[0]) for row in db.execute(
            "SELECT record_json FROM events WHERE dispatch_id=? ORDER BY rowid", (dispatch,))]

    def _launch(self, db, dispatch):
        launch = self._get(db, "launches", "dispatch_id", dispatch)
        if launch:
            links = [e for e in self._events(db, dispatch) if e["kind"] == "linked"]
            if links:
                launch = {**launch, **links[-1]["data"]}
        return launch

    def decision(self, record):
        clean = _validate_decision(record)
        with self._write() as db:
            return self._insert(db, "decisions", "decision_id", clean)

    def launch(self, record):
        if not isinstance(record, Mapping):
            raise OutcomeError("launch record must be a mapping")
        with self._write() as db:
            ident = record.get("decision_id")
            decision = self._get(db, "decisions", "decision_id", ident) if isinstance(ident, str) else None
            clean = _validate_launch(record, {ident: decision} if decision else {})
            existing = self._launch(db, clean["dispatch_id"])
            if existing:
                if _encode(existing) == _encode(clean):
                    return existing
                # A hook can record the real launch before the coordinator supplies
                # its exact decision ID. Retain the original and append an explicit
                # binding; never infer it from task text or timestamps.
                fields = set(clean) - {"decision_id", "override_reason"}
                if (existing["decision_id"] is None and clean["decision_id"] is not None
                        and all(existing[k] == clean[k] for k in fields)):
                    self._insert(db, "events", "event_id", dict(
                        event_id="link:" + clean["dispatch_id"], dispatch_id=clean["dispatch_id"],
                        kind="linked", data={k: clean[k] for k in ("decision_id", "override_reason")},
                        at=time.time()))
                    return clean
                raise OutcomeError("conflicting dispatch_id")
            return self._insert(db, "launches", "dispatch_id", clean)

    @staticmethod
    def _review_valid(launch, events):
        reviews = [e for e in events if e["kind"] == "review"]
        if not reviews:
            return False
        review = reviews[-1]
        actual, rv = launch["actual"], review["data"]
        repairs = [i for i, e in enumerate(events) if e["kind"] == "rework"]
        return (rv["verdict"] == "accepted" and rv["provider"] != actual["provider"]
                and rv["family"] != actual["family"]
                and (not repairs or events.index(review) > repairs[-1]))

    def event(self, dispatch_id, event_id, kind, data, at=None):
        if not isinstance(dispatch_id, str) or not dispatch_id.strip():
            raise OutcomeError("invalid dispatch_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise OutcomeError("invalid event_id")
        if not isinstance(kind, str) or kind not in _EVENT_KINDS:
            raise OutcomeError("invalid event kind")
        if at is not None and not _is_nonneg_number(at):
            raise OutcomeError("event timestamp must be finite nonnegative")
        _validate_event_data(kind, data)
        with self._write() as db:
            existing = self._get(db, "events", "event_id", event_id)
            stamp = existing["at"] if existing and at is None else time.time() if at is None else at
            record = dict(dispatch_id=dispatch_id, event_id=event_id, kind=kind, data=data, at=stamp)
            if existing:
                return self._insert(db, "events", "event_id", record)
            launch = self._launch(db, dispatch_id)
            if not launch:
                raise OutcomeError("unknown dispatch_id")
            events = self._events(db, dispatch_id)
            if stamp < max([launch["at"]] + [e["at"] for e in events]):
                raise OutcomeError("event timestamp predates launch or previous event")
            if any(e["kind"] == "accepted" for e in events):
                raise OutcomeError("accepted record is immutable")
            terminal = [e for e in events if e["kind"] in _TERMINAL_KINDS]
            if kind in _TERMINAL_KINDS and terminal:
                raise OutcomeError("terminal outcome already recorded; replay its event id")
            if kind == "accepted":
                decision = self._get(db, "decisions", "decision_id", launch["decision_id"])
                if not decision:
                    raise OutcomeError("cannot accept unlinked launch")
                if not terminal or terminal[-1]["kind"] != "completed":
                    raise OutcomeError("acceptance requires completed execution")
                reviews = [e for e in events if e["kind"] == "review"]
                if reviews and reviews[-1]["data"]["verdict"] == "rejected":
                    raise OutcomeError("latest review rejected the result")
                if decision["review_required"] and not self._review_valid(launch, events):
                    raise OutcomeError("fresh independent accepted review required")
            return self._insert(db, "events", "event_id", record)

    def get_decision(self, decision_id):
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise OutcomeError("invalid decision_id")
        with self._read() as db:
            return self._get(db, "decisions", "decision_id", decision_id)

    def get_launch(self, dispatch_id):
        if not isinstance(dispatch_id, str) or not dispatch_id.strip():
            raise OutcomeError("invalid dispatch_id")
        with self._read() as db:
            return self._launch(db, dispatch_id)

    def audit(self, project=None):
        with self._read() as db:
            decisions = [d for d in self._all(db, "decisions") if project is None or d["project"] == project]
            launches = [l for l in self._all(db, "launches") if project is None or l["project"] == project]
            events = self._all(db, "events")
        by_decision = {d["decision_id"]: d for d in decisions}
        by_dispatch = {}
        for event in events:
            by_dispatch.setdefault(event["dispatch_id"], []).append(event)
        metrics = {k: 0 for k in ("total_launches", "linked_launches", "unlinked_launches",
                   "completed_launches", "accepted_launches", "rejected_review_launches",
                   "missing_required_review_launches", "known_usage_launches", "unknown_usage_launches",
                   "known_rework_launches", "unknown_rework_launches")}
        repair = dict(seconds_total=0, events=0, accepted_with_coordinator_rework=0, coverage_accepted=None)
        categories = {k: dict(total=0, known_launches=0, unknown_launches=0) for k in _USAGE_OPTIONAL}
        attempts, first_launch, accepted_at = [], {}, {}
        for launch in launches:
            es = by_dispatch.get(launch["dispatch_id"], [])
            links = [e for e in es if e["kind"] == "linked"]
            if links:
                launch = {**launch, **links[-1]["data"]}
            decision = by_decision.get(launch["decision_id"])
            required = decision["review_required"] if decision else None
            terminal = [e for e in es if e["kind"] in _TERMINAL_KINDS]
            acceptance = [e for e in es if e["kind"] == "accepted"]
            reviews = [e for e in es if e["kind"] == "review"]
            usage = [e for e in es if e["kind"] == "usage"]
            rework = [e for e in es if e["kind"] == "rework"]
            completed = bool(terminal and terminal[-1]["kind"] == "completed")
            accepted = bool(acceptance)
            latest_review = reviews[-1] if reviews else None
            latest_usage = usage[-1]["data"] if usage else None
            metrics["total_launches"] += 1
            metrics["linked_launches" if decision else "unlinked_launches"] += 1
            metrics["completed_launches"] += completed
            metrics["accepted_launches"] += accepted
            metrics["rejected_review_launches"] += bool(latest_review and latest_review["data"]["verdict"] == "rejected")
            metrics["missing_required_review_launches"] += bool(required and not self._review_valid(launch, es))
            known_usage = bool(latest_usage and any(k in latest_usage for k in _USAGE_OPTIONAL))
            metrics["known_usage_launches" if known_usage else "unknown_usage_launches"] += 1
            metrics["known_rework_launches" if rework else "unknown_rework_launches"] += 1
            for key, summary in categories.items():
                if latest_usage and key in latest_usage:
                    summary["known_launches"] += 1
                    summary["total"] += latest_usage[key]
                else:
                    summary["unknown_launches"] += 1
            # All rework events describe coordinator-side repair; actor is attribution,
            # not a magic required literal such as 'coordinator'.
            repair["seconds_total"] += sum(e["data"]["seconds"] for e in rework)
            repair["events"] += len(rework)
            repair["accepted_with_coordinator_rework"] += bool(accepted and rework)
            key = (launch["project"], launch["task_sha256"])
            first_launch[key] = min(first_launch.get(key, launch["at"]), launch["at"])
            if accepted:
                accepted_at[key] = min(accepted_at.get(key, acceptance[0]["at"]), acceptance[0]["at"])
            attempts.append(dict(launch=launch, events=es,
                status="accepted" if accepted else terminal[-1]["kind"] if terminal else "started",
                completed=completed, accepted=accepted, review_required=required,
                latest_review=latest_review, latest_usage=latest_usage, rework_events=rework))
        if metrics["accepted_launches"]:
            repair["coverage_accepted"] = repair["accepted_with_coordinator_rework"] / metrics["accepted_launches"]
        metrics["coordinator_repair"] = repair
        metrics["usage_categories"] = categories
        durations = [dict(project=k[0], task_sha256=k[1], count=1, seconds=[at-first_launch[k]],
                          median_seconds=at-first_launch[k]) for k, at in accepted_at.items()]
        seconds = [d["median_seconds"] for d in durations]
        metrics["time_to_accepted_task"] = dict(count=len(seconds), median_seconds=statistics.median(seconds) if seconds else None)
        return dict(project=project, decisions=decisions, attempts=attempts, metrics=metrics, time_to_accepted_task=durations)
