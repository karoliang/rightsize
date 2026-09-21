"""Additive legacy-state cutover, durable backup and guarded rollback."""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys
import tempfile
import time
import uuid

from managed_ledger import ACTIVE, Ledger, LedgerError


GENERATION = 2
LIMIT = 16 * 1024 * 1024


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise LedgerError("migration input must be a regular file")
        data = handle.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise LedgerError("migration input exceeds backup budget")
    return data


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".migration-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exchange(left, right):
    """Atomic file/directory exchange prevents a missing-file writer window."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        fn = library.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        result = fn(os.fsencode(left), os.fsencode(right), 2)  # RENAME_SWAP
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        fn = library.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        result = fn(-100, os.fsencode(left), -100, os.fsencode(right), 2)  # RENAME_EXCHANGE
    else:
        raise LedgerError("atomic state exchange unsupported on this platform")
    if result:
        raise LedgerError("atomic state exchange failed; migration remains recoverable")
    sync(left.parent)
    if left.parent != right.parent:
        sync(right.parent)


def check_exchange(root):
    # Verify the actual filesystem, not merely the libc symbol, before a
    # transition manifest can block ordinary commands on an unsupported host.
    with tempfile.TemporaryDirectory(prefix=".exchange-check-", dir=root) as temporary:
        directory = Path(temporary)
        file, fence = directory / "file", directory / "fence"
        file.write_bytes(b"exchange-check")
        fence.mkdir()
        exchange(file, fence)
        if not file.is_dir() or fence.read_bytes() != b"exchange-check":
            raise LedgerError("filesystem did not preserve atomic exchange inputs")


def manifest(root):
    path = root / "state-migration.json"
    if not path.exists():
        return None
    try:
        value = json.loads(read(path))
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["writer_generation"] != GENERATION
                or value["phase"] not in ("prepared", "active", "restoring", "rolled-back")
                or not isinstance(value["id"], str) or len(value["id"]) != 32
                or any(c not in "0123456789abcdef" for c in value["id"])
                or type(value["source_exists"]) is not bool
                or any(not isinstance(value[key], str) or len(value[key]) != 64
                       or any(c not in "0123456789abcdef" for c in value[key])
                       for key in ("source_sha256", "candidate_sha256"))):
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise LedgerError("migration manifest invalid; restore from reviewed backup") from exc


def active_path(path):
    record = manifest(path.parent)
    if not record or record["phase"] == "rolled-back":
        return path.parent / "state.json" if path.name == "state.v2.json" else path
    if record["phase"] != "active":
        raise LedgerError("state transition incomplete; rerun managed migrate or rollback")
    fence = path.parent / "state.json"
    if not fence.is_dir() or fence.is_symlink():
        raise LedgerError("legacy writer fence missing; stop incompatible writers")
    target = path.parent / "state.v2.json"
    try:
        value = json.loads(read(target))
        if value.get("_rightsize_writer_generation") != GENERATION:
            raise ValueError()
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        raise LedgerError("migrated state invalid; do not reset commitments") from exc
    return target


def load_legacy(api, path):
    original = api.STATE
    try:
        api.STATE = path
        from managed_router import legacy_state
        return legacy_state(api)
    finally:
        api.STATE = original


def held(state):
    return [row for row in state.get("reservations", [])
            if row.get("_rightsize_migrated") or row["expires"] > time.time()]


def inspect(api):
    root = api.STATE.parent
    record = manifest(root)
    if record and record["phase"] in ("prepared", "restoring"):
        return {"status": "recovery-required", "phase": record["phase"], "migration_id": record["id"]}
    path = active_path(api.STATE)
    state = load_legacy(api, path)
    attempts = Ledger(root / "managed.sqlite3").read()
    return {"status": "planned", "writer_generation": GENERATION,
            "phase": record["phase"] if record else "legacy",
            "legacy_holds": len(held(state)), "legacy_denial_scopes": sum(bool(value) for value in state.get("quota_denials", {}).values()),
            "managed_active": sum(row["state"] in ACTIVE for row in attempts),
            "migration_id": record["id"] if record else None, "writes": False}


def migrate(api, *, apply=False):
    if not apply:
        with api.state_lock(transition=True):
            return inspect(api)
    root = api.STATE.parent
    legacy, target = root / "state.json", root / "state.v2.json"
    with api.state_lock(transition=True):
        record = manifest(root)
        if record and record["phase"] == "active":
            active_path(legacy)
            return {**inspect(api), "status": "existing"}
        if record and record["phase"] == "restoring":
            raise LedgerError("rollback in progress; resume rollback first")
        ledger = Ledger(root / "managed.sqlite3")
        if not record or record["phase"] == "rolled-back":
            exists = legacy.exists()
            raw = read(legacy) if exists else b"{}\n"
            try:
                state = load_legacy(api, legacy)
                encoded(state)
                ids = [row.get("id") for row in held(state)]
                if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
                    raise ValueError("ambiguous legacy reservation identity")
            except (LedgerError, ValueError):
                digest = hashlib.sha256(raw).hexdigest()
                write(root / "quarantine" / (digest + ".json"), raw)
                raise LedgerError("corrupt legacy evidence copied to quarantine; original retained") from None
        try:
            ledger.read()  # Validate existing SQLite before migration artifacts.
        except (LedgerError, sqlite3.DatabaseError):
            raw_ledger = read(ledger.path)
            digest = hashlib.sha256(raw_ledger).hexdigest()
            write(root / "quarantine" / (digest + ".sqlite3"), raw_ledger)
            raise LedgerError("managed evidence invalid; quarantined copy retained, original unchanged") from None
        if not record or record["phase"] == "rolled-back":
            check_exchange(root)
        if not ledger.path.exists():
            # Commit first schema creation separately. A later filesystem fault
            # must not leave an existing SQLite file with a rolled-back schema.
            with ledger.transaction():
                pass
        with ledger.transaction() as db:
            if not record or record["phase"] == "rolled-back":
                identifier = uuid.uuid4().hex
                backup = root / "migrations" / identifier
                write(backup / "state.json", raw)
                if target.exists():
                    write(backup / "previous-state.v2.json", read(target))
                migrated = json.loads(json.dumps(state))
                for row in held(migrated):
                    row["_rightsize_migrated"] = True
                    row["_rightsize_identity"] = "legacy-unattributed"
                migrated["_rightsize_writer_generation"] = GENERATION
                write(backup / "candidate.json", encoded(migrated))
                record = {"schema_version": 1, "writer_generation": GENERATION, "id": identifier,
                          "phase": "prepared", "source_exists": exists,
                          "source_sha256": hashlib.sha256(raw).hexdigest(),
                          "candidate_sha256": hashlib.sha256(encoded(migrated)).hexdigest()}
                write(root / "state-migration.json", encoded(record))
            backup = root / "migrations" / record["id"]
            raw = read(backup / "state.json")
            candidate = read(backup / "candidate.json")
            if (hashlib.sha256(raw).hexdigest() != record["source_sha256"]
                    or hashlib.sha256(candidate).hexdigest() != record["candidate_sha256"]):
                raise LedgerError("migration backup digest mismatch")
            write(target, candidate)
            guard = backup / "writer-fence"
            if not legacy.is_dir():
                if not legacy.exists() and not record["source_exists"]:
                    write(legacy, raw)
                if hashlib.sha256(read(legacy)).hexdigest() != record["source_sha256"]:
                    raise LedgerError("legacy state changed during interrupted migration")
                if not guard.exists():
                    guard.mkdir(mode=0o700)
                    write(guard / "README", b"Use the current Rightsize CLI; state moved to state.v2.json.\n")
                if not guard.is_dir() or guard.is_symlink():
                    raise LedgerError("migration fence conflicts with preserved evidence")
                exchange(legacy, guard)
            db.execute("UPDATE settings SET value='enabled' WHERE name='admissions'")
        record["phase"] = "active"
        write(root / "state-migration.json", encoded(record))
    return {"status": "migrated", "migration_id": record["id"], "writer_generation": GENERATION,
            "backup": str(backup / "state.json"), "legacy_holds": len(held(json.loads(candidate)))}


def rollback(api, *, apply=False):
    if not apply:
        with api.state_lock(transition=True):
            return inspect(api)
    root = api.STATE.parent
    legacy, target = root / "state.json", root / "state.v2.json"
    with api.state_lock(transition=True):
        record = manifest(root)
        if not record or record["phase"] == "rolled-back":
            return {"status": "existing", "phase": "legacy"}
        if record["phase"] == "prepared":
            raise LedgerError("finish interrupted migration before rollback")
        ledger = Ledger(root / "managed.sqlite3")
        with ledger.transaction() as db:
            db.execute("UPDATE settings SET value='disabled' WHERE name='admissions'")
        with ledger.transaction() as db:
            active = db.execute("SELECT count(*) FROM attempts WHERE active=1").fetchone()[0]
            state = load_legacy(api, target)
            holds = held(state)
            if active or holds:
                return {"status": "wait", "reason": "admissions disabled; reconcile active work before restoring",
                        "managed_active": active, "legacy_holds": len(holds)}
            backup = root / "migrations" / record["id"]
            raw = read(backup / "state.json")
            if hashlib.sha256(raw).hexdigest() != record["source_sha256"]:
                raise LedgerError("rollback backup digest mismatch")
            if record["phase"] != "restoring":
                write(backup / "settled-state.v2.json", read(target))
                record["phase"] = "restoring"
                write(root / "state-migration.json", encoded(record))
            if legacy.is_dir():
                restore = backup / "restore-state.json"
                if not restore.exists():
                    write(restore, raw)
                if restore.is_dir() or hashlib.sha256(read(restore)).hexdigest() != record["source_sha256"]:
                    raise LedgerError("rollback staging evidence differs")
                exchange(legacy, restore)
            elif hashlib.sha256(read(legacy)).hexdigest() != record["source_sha256"]:
                raise LedgerError("legacy state changed during interrupted rollback")
            record["phase"] = "rolled-back"
            write(root / "state-migration.json", encoded(record))
    return {"status": "rolled-back", "migration_id": record["id"], "admissions": "disabled",
            "restored_sha256": record["source_sha256"], "managed_history_retained": True}
