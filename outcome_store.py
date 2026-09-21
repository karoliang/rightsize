"""Advisory evidence store for rightsize decisions, launches, and events.

Observational only. Never blocks work, owns managed quota, or verifies accounts.
Caller-controlled path; permission 0600 on create. SQLite serialises writes via
BEGIN IMMEDIATE; readers use a read-only URI handle so audits do not create
the database.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat as stat_module
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

_HASH_BEARING_KINDS = frozenset({
    "completed", "failed", "cancelled", "review", "rework", "accepted",
})

_HEX = "0123456789abcdef"

# Maximum size of any single evidence file. Evidence larger than this is almost
# always raw text or a captured log that should have been a digest.
CLOSEOUT_EVIDENCE_MAX_BYTES = 1024 * 1024


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


def _validate_evidence_path(ep, where):
    if not isinstance(ep, str) or not ep.strip():
        raise OutcomeError(f"{where}: evidence_path must be a nonempty string")
    parts = Path(ep).parts
    if Path(ep).is_absolute() or any(part == ".." for part in parts):
        raise OutcomeError(f"{where}: evidence_path must be a relative path without traversal")


def _validate_evidence_file(path, evidence_root, max_bytes, where):
    """Validate a single evidence file: regular leaf, bounded size, no symlink
    anywhere on the unresolved path, computed SHA256.

    Walking the resolved parents erases symlinks before ``lstat`` runs, so a
    symlink in the evidence dir would slip through. Build the candidate path
    component-by-component from ``evidence_root`` itself, ``lstat`` each
    intermediate without resolving, and reject any link under the dir. The
    final open uses ``O_NOFOLLOW`` so a leaf replaced by a FIFO between the
    lstat and the read cannot block, and the same nonblocking ``fd`` does
    the fstat and the bounded read.
    """
    import os as _os
    fpath = Path(evidence_root) / path
    current = Path(evidence_root)
    try:
        current.lstat()
    except OSError as exc:
        raise OutcomeError(f"{where}: evidence root not readable: {exc}") from exc
    for part in Path(path).parts:
        if part in ("", "."):
            continue
        current = current / part
        try:
            st = current.lstat()
        except OSError as exc:
            raise OutcomeError(f"{where}: evidence path component {current!s} not readable: {exc}") from exc
        if stat_module.S_ISLNK(st.st_mode):
            raise OutcomeError(f"{where}: evidence path contains a symlink at {current!s}")
    flags = _os.O_RDONLY | _os.O_NOFOLLOW | _os.O_NONBLOCK
    try:
        fd = _os.open(current, flags)
    except OSError as exc:
        raise OutcomeError(f"{where}: evidence file not readable: {exc}") from exc
    try:
        try:
            st = _os.fstat(fd)
        except OSError as exc:
            raise OutcomeError(f"{where}: evidence file not readable: {exc}") from exc
        mode = st.st_mode
        if stat_module.S_ISLNK(mode):
            raise OutcomeError(f"{where}: evidence file is a symlink")
        if stat_module.S_ISDIR(mode):
            raise OutcomeError(f"{where}: evidence file is a directory")
        if stat_module.S_ISFIFO(mode):
            raise OutcomeError(f"{where}: evidence file is a fifo")
        if stat_module.S_ISCHR(mode) or stat_module.S_ISBLK(mode):
            raise OutcomeError(f"{where}: evidence file is a device")
        if not stat_module.S_ISREG(mode):
            raise OutcomeError(f"{where}: evidence file is not a regular file")
        if st.st_size > max_bytes:
            raise OutcomeError(f"{where}: evidence file exceeds {max_bytes} bytes")
        data = _os.read(fd, max_bytes + 1)
    finally:
        _os.close(fd)
    if len(data) > max_bytes:
        raise OutcomeError(f"{where}: evidence file exceeded {max_bytes} bytes during read")
    return hashlib.sha256(data).hexdigest()


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

    def _event(self, db, dispatch_id, event_id, kind, data, at=None,
               review_validator=None):
        """Single transactional event writer shared by ``event()`` and ``closeout``.

        All the read-then-check-then-write invariants (terminal conflict,
        post-accept immutability, monotonic time, accepted-gate, retry preserving
        the original stamp) live here so both paths exercise the same state
        machine. The caller must hold an open ``_write`` transaction. The
        optional ``review_validator`` callback is invoked with the locked
        ``(db, decision, launch, review_data)`` so reviewer profile/family/
        floor/capability checks run inside the transaction and cannot race a
        hook binding the launch.
        """
        if not isinstance(dispatch_id, str) or not dispatch_id.strip():
            raise OutcomeError("invalid dispatch_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise OutcomeError("invalid event_id")
        if not isinstance(kind, str) or kind not in _EVENT_KINDS:
            raise OutcomeError("invalid event kind")
        if at is not None and not _is_nonneg_number(at):
            raise OutcomeError("event timestamp must be finite nonnegative")
        _validate_event_data(kind, data)
        existing = self._get(db, "events", "event_id", event_id)
        if existing is not None:
            # Global event_id identity: a different dispatch that reuses this
            # event_id would silently leave an audit-trail mismatch.
            if existing["dispatch_id"] != dispatch_id:
                raise OutcomeError(
                    f"event_id {event_id!r} already recorded for dispatch "
                    f"{existing['dispatch_id']!r}"
                )
            if at is not None and at != existing["at"]:
                raise OutcomeError(
                    f"event_id {event_id!r} already recorded with a different timestamp"
                )
            stamp = existing["at"]
        else:
            stamp = time.time() if at is None else at
        record = dict(dispatch_id=dispatch_id, event_id=event_id, kind=kind, data=data, at=stamp)
        if existing is not None:
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
        if kind == "review" and review_validator is not None:
            decision = self._get(db, "decisions", "decision_id", launch["decision_id"])
            review_validator(db, decision, launch, data)
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

    def closeout(self, dispatch_id, events, decision_id=None, project=None,
                 evidence_root=None, max_events=64, max_evidence_bytes=CLOSEOUT_EVIDENCE_MAX_BYTES,
                 review_validator=None):
        """Atomic closeout: validate the whole manifest, then apply in one transaction.

        All-or-nothing: any envelope, evidence, dispatch, decision or project
        failure leaves the store untouched. Idempotent: an event_id that
        already exists for this dispatch with identical content is left alone
        and its original timestamp is preserved; a differing record, a cross-
        dispatch reuse, or any new hash-bearing event that fails the gates
        fails the batch.

        Reviewer profile/family/floor/capability validation is supplied by the
        caller as a ``review_validator`` callback invoked from inside the
        write transaction with the live locked ``(db, decision, launch,
        review_data)``, so a hook binding the launch before this check cannot
        weaken acceptance through a stale local view.
        """
        if not isinstance(dispatch_id, str) or not dispatch_id.strip():
            raise OutcomeError("invalid dispatch_id")
        if not isinstance(events, list):
            raise OutcomeError("events must be a list")
        if not events:
            raise OutcomeError("closeout requires at least one event")
        if len(events) > max_events:
            raise OutcomeError(f"closeout exceeds {max_events} events per batch")
        if isinstance(decision_id, str) and decision_id:
            pass
        elif decision_id is not None:
            raise OutcomeError("decision_id must be a string or omitted")

        # Envelope shape, required fields, unique event_ids within the batch.
        seen_ids = set()
        for idx, event in enumerate(events):
            if not isinstance(event, Mapping):
                raise OutcomeError(f"event {idx}: must be a mapping")
            for key in ("event_id", "kind", "data"):
                if key not in event:
                    raise OutcomeError(f"event {idx}: missing {key}")
            for key in event:
                if key not in ("event_id", "kind", "data", "evidence_path"):
                    raise OutcomeError(f"event {idx}: unknown field {key!r}")
            eid = event["event_id"]
            if not isinstance(eid, str) or not eid.strip():
                raise OutcomeError(f"event {idx}: invalid event_id")
            if eid in seen_ids:
                raise OutcomeError(f"event {idx}: duplicate event_id {eid!r}")
            seen_ids.add(eid)
            kind = event["kind"]
            if not isinstance(kind, str) or kind not in _EVENT_KINDS:
                raise OutcomeError(f"event {idx}: invalid event kind {kind!r}")
            if not isinstance(event["data"], Mapping):
                raise OutcomeError(f"event {idx}: data must be a mapping")
            ep = event.get("evidence_path")
            if kind in _HASH_BEARING_KINDS:
                if ep is None:
                    raise OutcomeError(
                        f"event {idx}: evidence_path required for hash-bearing kind {kind!r}"
                    )
                _validate_evidence_path(ep, f"event {idx}")
            elif ep is not None:
                raise OutcomeError(
                    f"event {idx}: evidence_path not permitted for kind {kind!r}"
                )

        # Evidence file integrity: regular files only, bounded, SHA256 matches.
        # Validated before any transaction so a missing or unsafe file leaves
        # the store untouched and never creates an empty SQLite file when the
        # dispatch does not exist yet.
        for idx, event in enumerate(events):
            ep = event.get("evidence_path")
            if ep is None:
                continue
            if evidence_root is None:
                raise OutcomeError(
                    f"event {idx}: evidence_path set but no evidence root provided"
                )
            digest = _validate_evidence_file(ep, evidence_root, max_evidence_bytes,
                                             f"event {idx}")
            event_sha = (event.get("data") or {}).get("evidence_sha256")
            if event_sha and event_sha != digest:
                raise OutcomeError(
                    f"event {idx}: evidence_sha256 mismatch (file {digest!r} vs data {event_sha!r})"
                )

        # Preflight: the dispatch, its decision and project must already exist
        # under the recorded identity. A non-existent store raises here without
        # ever opening the write transaction.
        with self._read() as db:
            if db is None:
                raise OutcomeError("unknown dispatch_id")
            launch = self._launch(db, dispatch_id)
            if launch is None:
                raise OutcomeError("unknown dispatch_id")
            decision = (self._get(db, "decisions", "decision_id", launch["decision_id"])
                        if launch.get("decision_id") else None)
            if isinstance(decision_id, str) and decision_id:
                if not decision or decision["decision_id"] != decision_id:
                    raise OutcomeError(
                        f"decision_id {decision_id!r} does not match the dispatch's recorded decision"
                    )
            if isinstance(project, str) and project:
                recorded_project = (decision or launch).get("project")
                if recorded_project and recorded_project != project:
                    raise OutcomeError(
                        f"project {project!r} does not match recorded project {recorded_project!r}"
                    )

        # Single transaction: every event goes through the shared _event helper,
        # so post-accept immutability, terminal conflict, monotonic time, retry
        # preserving original stamps and accepted-gate logic run uniformly.
        # Optional decision/project consistency is re-validated inside the lock
        # so a hook that bound the launch after the preflight cannot weaken it.
        applied = []
        with self._write() as db:
            if self._launch(db, dispatch_id) is None:
                raise OutcomeError("unknown dispatch_id")
            live_decision = None
            live_launch = self._launch(db, dispatch_id)
            live_decision_id = live_launch.get("decision_id")
            if live_decision_id:
                live_decision = self._get(db, "decisions", "decision_id", live_decision_id)
            if isinstance(decision_id, str) and decision_id:
                if not live_decision or live_decision["decision_id"] != decision_id:
                    raise OutcomeError(
                        f"decision_id {decision_id!r} does not match the dispatch's recorded decision"
                    )
            if isinstance(project, str) and project:
                recorded_project = (live_decision or live_launch).get("project")
                if recorded_project and recorded_project != project:
                    raise OutcomeError(
                        f"project {project!r} does not match recorded project {recorded_project!r}"
                    )
            for event in events:
                applied.append(self._event(
                    db, dispatch_id, event["event_id"], event["kind"], event["data"],
                    review_validator=review_validator,
                ))
        return {"dispatch_id": dispatch_id, "applied": applied}

    def event(self, dispatch_id, event_id, kind, data, at=None, review_validator=None):
        """Insert one event; idempotent on event_id with identical content."""
        with self._write() as db:
            return self._event(db, dispatch_id, event_id, kind, data, at=at,
                               review_validator=review_validator)

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
                   "known_rework_launches", "unknown_rework_launches", "closeout_gaps_total")}
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
            latest_terminal = terminal[-1]["kind"] if terminal else None
            completed = bool(latest_terminal == "completed")
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
            gaps = []
            if not decision:
                gaps.append("missing_decision_link")
            # A failed/cancelled attempt terminated: neither completion nor
            # acceptance are pending evidence, so the gaps stay empty. A
            # missing terminal reports both gaps because nothing finished.
            if latest_terminal is None:
                gaps.append("missing_completion")
                gaps.append("missing_acceptance")
            elif latest_terminal == "completed" and not accepted:
                gaps.append("missing_acceptance")
            if required and not self._review_valid(launch, es):
                gaps.append("missing_review")
            # Missing usage and rework stay missing, never implicit zero.
            if not usage:
                gaps.append("missing_usage")
            if not rework:
                gaps.append("missing_rework_evidence")
            metrics["closeout_gaps_total"] += len(gaps)
            attempts.append(dict(launch=launch, events=es,
                status="accepted" if accepted else latest_terminal or "started",
                completed=completed, accepted=accepted, review_required=required,
                latest_review=latest_review, latest_usage=latest_usage, rework_events=rework,
                closeout_gaps=gaps))
        if metrics["accepted_launches"]:
            repair["coverage_accepted"] = repair["accepted_with_coordinator_rework"] / metrics["accepted_launches"]
        metrics["coordinator_repair"] = repair
        metrics["usage_categories"] = categories
        durations = [dict(project=k[0], task_sha256=k[1], count=1, seconds=[at-first_launch[k]],
                          median_seconds=at-first_launch[k]) for k, at in accepted_at.items()]
        seconds = [d["median_seconds"] for d in durations]
        metrics["time_to_accepted_task"] = dict(count=len(seconds), median_seconds=statistics.median(seconds) if seconds else None)
        return dict(project=project, decisions=decisions, attempts=attempts, metrics=metrics, time_to_accepted_task=durations)
