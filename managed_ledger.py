"""Transactional managed admission and attempt lifecycle. No network or launches."""

from contextlib import contextmanager, closing
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid


VERSION = 1
ACTIVE = ("admitted", "launching", "started", "producing", "cancelling", "reconciling")
TERMINAL = ("launch_failed", "quota_failed", "auth_failed", "tool_failed", "runtime_failed",
            "cancelled", "completed", "accepted", "rejected")


class LedgerError(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def units(value, *, capacity=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise LedgerError("quota points must be finite and nonnegative")
    return int((Decimal(str(value)) * 1000000).to_integral_value(
        rounding=ROUND_FLOOR if capacity else ROUND_CEILING))


def wait(reason, **fields):
    return {"status": "wait", "reason": reason, **fields}


class Ledger:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize the existence check with first schema creation as well as
        # regular writes. An existing empty/corrupt file is never initialized
        # as though commitments had not existed.
        with self.path.with_name(self.path.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with self._transaction() as db:
                    yield db
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def _transaction(self):
        existed = self.path.exists()
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and not existed:
                os.chmod(self.path, 0o600)
                db.execute("CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("INSERT INTO settings VALUES ('admissions', 'enabled')")
                db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, spec_hash TEXT NOT NULL, floor INTEGER NOT NULL)")
                db.execute("""CREATE TABLE attempts (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, intent_hash TEXT NOT NULL,
                    task_id TEXT NOT NULL, account_ref TEXT NOT NULL, provider TEXT NOT NULL,
                    points INTEGER NOT NULL, active INTEGER NOT NULL, state TEXT NOT NULL,
                    at REAL NOT NULL, expires REAL NOT NULL, settled_at REAL, data TEXT NOT NULL)""")
                db.execute("CREATE INDEX commitments ON attempts(account_ref, active)")
                db.execute("CREATE TABLE denials (account_ref TEXT PRIMARY KEY, data TEXT NOT NULL)")
                db.execute("CREATE TABLE events (id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL)")
                db.execute(f"PRAGMA user_version={VERSION}")
            elif version != VERSION:
                raise LedgerError("unsupported managed ledger version; migrate or reconcile before writing")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def read(self, attempt_id=None):
        """Read-only means no creation, schema migration or expired-lease writes."""
        if not self.path.exists():
            return None if attempt_id else []
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != VERSION:
                raise LedgerError("unsupported managed ledger version")
            if attempt_id:
                row = db.execute("SELECT data FROM attempts WHERE id=?", (attempt_id,)).fetchone()
                return json.loads(row[0]) if row else None
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM attempts ORDER BY at, id")]

    @staticmethod
    def save(db, attempt):
        db.execute("UPDATE attempts SET state=?, active=?, expires=?, settled_at=?, data=? WHERE id=?",
                   (attempt["state"], int(attempt["state"] in ACTIVE), attempt["expires"],
                    attempt.get("settled_at"), encode(attempt), attempt["attempt_id"]))

    @staticmethod
    def expire(db, stamp):
        for row in db.execute("SELECT data FROM attempts WHERE active=1 AND expires<=? AND state!='reconciling'", (stamp,)):
            attempt = json.loads(row[0])
            attempt["state"] = "reconciling"
            attempt["reconcile_reason"] = "lease expired; launch/termination unconfirmed"
            Ledger.save(db, attempt)

    def admit(self, *, request_key, task, pick, observation, points, slot_limit, binding_now,
              reserve=10.0, max_age=15.0, lease_seconds=60.0, external_points=0.0,
              external_slots=0, external_block=None, max_attempts=2, clock=time.time):
        """Observation is gathered outside this transaction; binding_now is local metadata only."""
        required_task = {"task_id", "spec_hash", "context_hash", "judgment_source", "floor"}
        if set(task) != required_task or not all(isinstance(task[k], str) and task[k] for k in required_task - {"floor"}):
            raise LedgerError("invalid task identity")
        if type(task["floor"]) is not int or not 1 <= task["floor"] <= 3:
            raise LedgerError("invalid capability floor")
        if not isinstance(request_key, str) or not request_key or len(request_key) > 200:
            raise LedgerError("invalid request idempotency key")
        if (set(pick) != {"provider", "model", "effort", "band"}
                or type(pick["band"]) is not int or not task["floor"] <= pick["band"] <= 3
                or not all(isinstance(pick[k], str) and pick[k] for k in ("provider", "model"))):
            raise LedgerError("pick violates capability floor")
        if type(slot_limit) is not int or slot_limit < 1 or type(max_attempts) is not int or max_attempts < 1:
            raise LedgerError("invalid admission limits")
        if not 0 < max_age <= 300 or not 1 <= lease_seconds <= 3600:
            raise LedgerError("invalid observation age or lease duration")
        cost = units(points)
        external_cost = units(external_points)
        reserve_units = units(reserve)
        intent_hash = identity(task)
        with self.transaction() as db:
            stamp = clock()
            self.expire(db, stamp)
            duplicate = db.execute("SELECT data, intent_hash FROM attempts WHERE request_key=?", (request_key,)).fetchone()
            if duplicate:
                if duplicate["intent_hash"] != intent_hash:
                    raise LedgerError("idempotency key already belongs to a different task intent")
                return {"status": "existing", "attempt": json.loads(duplicate["data"])}
            if db.execute("SELECT value FROM settings WHERE name='admissions'").fetchone()[0] != "enabled":
                return wait("managed admissions disabled")
            prior_task = db.execute("SELECT * FROM tasks WHERE id=?", (task["task_id"],)).fetchone()
            if prior_task and (prior_task["spec_hash"] != task["spec_hash"] or task["floor"] < prior_task["floor"]):
                raise LedgerError("task identity or capability floor changed incompatibly")
            previous = db.execute("SELECT state FROM attempts WHERE task_id=?", (task["task_id"],)).fetchall()
            if any(row[0] in ACTIVE or row[0] in ("completed", "accepted") for row in previous):
                return wait("task already active or awaiting review; reconcile before retry")
            if len(previous) >= max_attempts:
                return wait("task attempt limit reached")
            account = observation.get("account") or {}
            if not account.get("account_ref") or not account.get("fingerprint"):
                return wait("account identity unavailable")
            if account != binding_now():
                return wait("account changed; re-probe", reprobe=True)
            observed = observation.get("observed_at")
            if (isinstance(observed, bool) or not isinstance(observed, (int, float))
                    or not math.isfinite(observed) or not 0 <= stamp - observed <= max_age):
                return wait("observation stale; re-probe", reprobe=True)
            # Native contexts can be separate homes logged into one account.
            # A provider-confirmed quota identity pools them. Without one,
            # retain a provider-wide conservative pool, never path-based credit.
            quota_ref = observation.get("quota_account_ref")
            ref = f"{pick['provider']}:{quota_ref or '*'}"
            buckets = observation.get("buckets") or []
            denied = db.execute("SELECT data FROM denials WHERE account_ref=?", (ref,)).fetchone()
            known = json.loads(denied[0]) if denied else {}
            provenance = {"context_ref": account["account_ref"], "fingerprint": account["fingerprint"]}
            def can_clear(entry):
                return observed > entry.get("denied_at", -1) and (bool(quota_ref) or all(
                    entry.get(key) == value for key, value in provenance.items()))
            if observation.get("status") == "denied":
                known["account"] = {"id": "account", "denied_at": observed, **provenance}
            healthy = []
            for bucket in buckets:
                percent = bucket.get("percent")
                if bucket.get("raw_status") in ("rate-limited", "exhausted", "quota-exceeded") or (
                        isinstance(percent, (int, float)) and percent >= 100):
                    known[bucket["id"]] = {**{k: bucket.get(k) for k in ("id", "resets_at")},
                                           "denied_at": observed, **provenance}
                elif (bucket.get("source") == "live" and bucket.get("raw_status", "ok") == "ok"
                      and isinstance(percent, (int, float)) and not isinstance(percent, bool)
                      and math.isfinite(percent) and 0 <= percent < 100):
                    healthy.append(bucket["id"])
                    if can_clear(known.get(bucket["id"], {})):
                        known.pop(bucket["id"], None)
            if buckets and len(healthy) == len(buckets) and observation.get("status") == "ok":
                if can_clear(known.get("account", {})):
                    known.pop("account", None)
            db.execute("INSERT OR REPLACE INTO denials VALUES (?, ?)", (ref, encode(known)))
            # Unattributed denials cannot be cleared by another selected
            # context. Unknown identity inherits all denials on the provider.
            related = db.execute("SELECT data FROM denials WHERE account_ref LIKE ? AND account_ref!=?",
                                 (pick["provider"] + ":%" if not quota_ref else pick["provider"] + ":*", ref))
            inherited = [bucket for row in related for bucket in json.loads(row[0]).values()]
            if known:
                return wait("quota denied", denied_buckets=list(known.values()))
            if inherited:
                return wait("unattributed quota denial requires reconciliation", denied_buckets=inherited)
            if external_block:
                return wait(external_block)
            if observation.get("status") != "ok" or not buckets:
                return wait("quota telemetry unavailable")
            capacities = []
            for bucket in buckets:
                percent = bucket.get("percent")
                if (isinstance(percent, bool) or not isinstance(percent, (int, float))
                        or not math.isfinite(percent) or not 0 <= percent < 100
                        or bucket.get("source") not in ("live", "computed")
                        or bucket.get("raw_status", "ok") != "ok"):
                    return wait("quota telemetry unavailable")
                capacities.append(max(0, units(100 - percent, capacity=True) - reserve_units))
            # A completion newer than the observation still consumes points in
            # that snapshot. Releasing its slot must not recreate spent quota.
            held = db.execute("""SELECT points, active FROM attempts WHERE provider=? AND
                (?=0 OR account_ref=? OR account_ref=?) AND
                (active=1 OR (settled_at>? AND state!='launch_failed'))""",
                (pick["provider"], int(bool(quota_ref)), ref, pick["provider"] + ":*", observed)).fetchall()
            spent = sum(row["points"] for row in held) + external_cost
            slots = sum(row["active"] for row in held) + external_slots
            if slots >= slot_limit:
                return wait("account slot limit reached", inflight=slots)
            if spent + cost > min(capacities):
                return wait("account point budget committed", committed_points=spent / 1000000)
            attempt_id = uuid.uuid4().hex
            attempt = {**task, "attempt_id": attempt_id, "decision_id": uuid.uuid4().hex,
                       "reservation_id": uuid.uuid4().hex, "launch_key": uuid.uuid4().hex,
                       "request_key": request_key, "pick": pick, "account": account,
                       "quota_ref": ref,
                       "observation": observation, "points": cost / 1000000,
                       "reserve_points": reserve_units / 1000000,
                       "external_points": external_cost / 1000000,
                       "state": "admitted", "at": stamp, "expires": stamp + lease_seconds,
                       "attempt_number": len(previous) + 1}
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?, ?)",
                       (task["task_id"], task["spec_hash"], task["floor"]))
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL, ?)",
                       (attempt_id, request_key, intent_hash, task["task_id"], ref, pick["provider"],
                        cost, "admitted", stamp, attempt["expires"], encode(attempt)))
            return {"status": "admitted", "attempt": attempt}

    def event(self, attempt_id, event_id, kind, *, receipt=None, evidence=None, metrics=None, clock=time.time):
        """Only the trusted adapter supplies receipts/termination evidence; model text cannot."""
        receipt = receipt or {}
        allowed_receipt = {"dispatch_id", "session_id", "provider", "model", "effort", "account_ref",
                           "fingerprint", "worktree", "pid", "sandbox"}
        if set(receipt) - allowed_receipt or not isinstance(event_id, str) or not event_id:
            raise LedgerError("invalid lifecycle event")
        if "sandbox" in receipt and receipt["sandbox"] not in ("read-only", "workspace-write"):
            raise LedgerError("invalid native permission mode")
        if evidence is not None and (not isinstance(evidence, str) or not evidence):
            raise LedgerError("evidence must be a nonempty reference or digest")
        allowed_metrics = {"input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens",
                           "total_tokens", "output_bytes", "tool_failures", "cache_write_input_tokens"}
        if metrics is not None and (not isinstance(metrics, dict) or set(metrics) - allowed_metrics
                                    or any(type(value) is not int or value < 0 for value in metrics.values())):
            raise LedgerError("invalid native metrics")
        payload = {"kind": kind, "receipt": receipt, "evidence": evidence, "metrics": metrics}
        digest = identity(payload)
        with self.transaction() as db:
            stamp = clock()
            row = db.execute("SELECT data FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row:
                raise LedgerError("attempt not found")
            attempt = json.loads(row[0])
            previous = db.execute("SELECT attempt_id, digest FROM events WHERE id=?", (event_id,)).fetchone()
            if previous:
                if tuple(previous) != (attempt_id, digest):
                    raise LedgerError("event id reused with different evidence")
                return {"applied": False, "attempt": attempt}
            state = attempt["state"]
            if kind == "prepared":
                if state != "launching" or not receipt.get("session_id"):
                    raise LedgerError("prepared native session requires a launch claim")
                attempt["prepared_receipt"] = receipt
                kind = state
            elif kind == "metrics":
                if state not in (*ACTIVE, "completed") or metrics is None:
                    raise LedgerError("metrics require an executing or completed attempt")
                attempt["metrics"] = metrics
                kind = state
            elif kind == "launching":
                if state != "admitted" or attempt["expires"] <= stamp:
                    raise LedgerError("launch is not claimable; reconcile instead of retrying")
            elif kind == "started":
                if state not in ("launching", "reconciling", "cancelling") or not receipt.get("dispatch_id"):
                    raise LedgerError("launch receipt required")
                expected = {**{k: attempt["pick"][k] for k in ("provider", "model", "effort")},
                            **{k: attempt["account"][k] for k in ("account_ref", "fingerprint")}}
                attempt["receipt"] = receipt
                if any(receipt.get(key) != value for key, value in expected.items()):
                    kind = "reconciling"
                    attempt["launch_mismatch"] = True
                    attempt["reconcile_reason"] = "observed launch differs from admitted selection"
                elif state == "cancelling":
                    kind = "cancelling"
            elif kind == "producing":
                if state not in ("started", "producing"):
                    raise LedgerError("output requires a bound started attempt")
                attempt.setdefault("first_output_at", stamp)
            elif kind == "cancelling":
                if state not in ACTIVE:
                    raise LedgerError("terminal attempt cannot be cancelled")
                if state == "admitted":
                    kind = "cancelled"
                    attempt["settled_at"] = stamp
                    attempt["outcome_evidence"] = "cancelled-before-native-launch-claim"
            elif kind == "reconciling":
                if state not in ACTIVE:
                    raise LedgerError("terminal attempt cannot become unresolved")
            elif kind in ("accepted", "rejected"):
                if state != "completed" or not evidence:
                    raise LedgerError("acceptance requires completed execution and review evidence")
            elif kind in TERMINAL:
                if state not in ACTIVE or not evidence:
                    raise LedgerError("terminal outcome requires confirmed adapter evidence")
                if kind == "launch_failed" and state not in ("admitted", "launching", "reconciling"):
                    raise LedgerError("a started process is not a failed launch")
                if kind == "completed" and (not attempt.get("receipt") or attempt.get("launch_mismatch")):
                    raise LedgerError("completion requires exact launch identity")
                attempt["settled_at"] = stamp
                attempt["outcome_evidence"] = evidence
            else:
                raise LedgerError("unsupported lifecycle event")
            attempt["state"] = kind
            if kind in ("accepted", "rejected"):
                attempt["review_evidence"] = evidence
            self.save(db, attempt)
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                       (event_id, attempt_id, digest, encode({**payload, "at": stamp})))
            if kind == "quota_failed":
                ref = attempt["quota_ref"]
                db.execute("INSERT OR REPLACE INTO denials VALUES (?, ?)",
                           (ref, encode({"account": {"id": "account", "denied_at": stamp,
                                                      "context_ref": attempt["account"]["account_ref"],
                                                      "fingerprint": attempt["account"]["fingerprint"]}})))
            return {"applied": True, "attempt": attempt}

    def renew(self, attempt_id, launch_key, *, seconds=60, clock=time.time):
        if not 1 <= seconds <= 3600:
            raise LedgerError("invalid renewal duration")
        with self.transaction() as db:
            row = db.execute("SELECT data FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not row:
                raise LedgerError("attempt not found")
            attempt = json.loads(row[0])
            if attempt["launch_key"] != launch_key or attempt["state"] not in ACTIVE:
                raise LedgerError("lease ownership unavailable")
            attempt["expires"] = clock() + seconds
            self.save(db, attempt)
            return attempt

    def reconcile_expired(self, *, clock=time.time):
        with self.transaction() as db:
            self.expire(db, clock())
