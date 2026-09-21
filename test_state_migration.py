"""Real filesystem exchange and SQLite rollback gates, entirely offline."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import rightsize as r
from managed_ledger import Ledger, LedgerError
from managed_router import external_load
import state_migration as m


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.legacy = self.root / "state.json"
        self.target = self.root / "state.v2.json"
        patcher = patch.object(r, "STATE", self.legacy)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = {"reservations": [{"id": "live", "provider": "codex", "points": 3,
            "expires": time.time() + 3600, "at": time.time(), "task": "legacy task", "band": 1}],
            "quota_denials": {"opencode": {"weekly": {"raw_status": "rate-limited"}}},
            "snapshots": {"opencode": {"weekly": {"at": 1, "percent": 100}}},
            "unrecognized_history": [{"dispatch": "legacy-dispatch", "outcome": "unknown"}]}
        self.raw = json.dumps(self.state, indent=4).encode()
        self.legacy.write_bytes(self.raw)
        self.ledger = Ledger(self.root / "managed.sqlite3")

    def admit(self, key="fixture"):
        account = {"account_ref": "fixture", "fingerprint": "fixture"}
        return self.ledger.admit(request_key=key,
            task={"task_id": key, "spec_hash": "fixture", "context_hash": "none", "judgment_source": "test", "floor": 1},
            pick={"provider": "claude", "model": "fixture", "effort": None, "band": 1},
            observation={"status": "ok", "account": account, "observed_at": time.time(),
                         "buckets": [{"id": "weekly", "percent": 0, "source": "live"}]},
            points=1, slot_limit=8, binding_now=lambda: account)

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*")
                if path.is_file() and not path.name.endswith(".lock")}

    def release_legacy(self):
        # Fixture represents a caller's positive completion report, not TTL.
        with patch.object(r, "STATE", self.target):
            self.assertEqual(r.release("codex", "live"), 1)

    def test_dry_run_has_no_writes(self):
        before = self.files()
        result = m.migrate(r)
        self.assertEqual(result["legacy_holds"], 1)
        self.assertEqual(result["legacy_denial_scopes"], 1)
        self.assertEqual(self.files(), before)
        self.assertTrue((self.root / "state.json.lock").exists())
        self.assertFalse((self.root / "managed.sqlite3").exists())

    def test_migration_preserves_exact_backup_and_all_evidence(self):
        result = m.migrate(r, apply=True)
        self.assertEqual(Path(result["backup"]).read_bytes(), self.raw)
        self.assertEqual(Path(result["backup"]).stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.legacy.is_dir())
        self.assertEqual(m.active_path(self.legacy), self.target)
        state = json.loads(self.target.read_text())
        self.assertEqual(state["quota_denials"], self.state["quota_denials"])
        self.assertEqual(state["unrecognized_history"], self.state["unrecognized_history"])
        self.assertEqual(state["reservations"][0]["_rightsize_identity"], "legacy-unattributed")
        before = self.files()
        self.assertEqual(m.migrate(r, apply=True)["status"], "existing")
        self.assertEqual(self.files(), before)

    def test_old_writer_cannot_replace_fence_or_modify_new_state(self):
        m.migrate(r, apply=True)
        before = self.target.read_bytes()
        with self.assertRaises(OSError):r.save_json(self.legacy, {"reservations": []})
        with self.assertRaises(LedgerError):
            with r.state_lock():pass  # Invocation selected v1 before cutover.
        self.assertEqual(self.target.read_bytes(), before)
        self.assertTrue(self.legacy.is_dir())

    def test_migrated_holds_do_not_expire_or_use_heuristic_reaping(self):
        m.migrate(r, apply=True)
        state = json.loads(self.target.read_text())
        future = time.time() + 10000
        with patch.object(r, "now", return_value=future):
            self.assertEqual(len(r.sweep_reservations(state)), 1)
        self.assertEqual(external_load(state, "codex", future), (3, 1))
        with patch.object(r, "STATE", self.target), patch.object(r, "orca_settled", return_value=([], None)), \
                patch.object(r, "orca_settled_tasks", return_value={r.brief_key("legacy task")}):
            self.assertEqual(len(r.release_settled()["kept"]), 1)

    def test_rollback_disables_admission_before_waiting_for_active_work(self):
        attempt = self.admit()["attempt"]
        m.migrate(r, apply=True)
        result = m.rollback(r, apply=True)
        self.assertEqual((result["status"], result["managed_active"], result["legacy_holds"]), ("wait", 1, 1))
        self.assertEqual(self.admit("new")["reason"], "managed admissions disabled")
        self.assertEqual(self.ledger.read(attempt["attempt_id"])["state"], "admitted")
        self.assertTrue(self.legacy.is_dir())
        self.ledger.event(attempt["attempt_id"], "fixture-cancel", "cancelling")
        self.release_legacy()
        self.assertEqual(m.rollback(r, apply=True)["status"], "rolled-back")
        self.assertEqual(self.legacy.read_bytes(), self.raw)
        self.assertEqual(self.ledger.read(attempt["attempt_id"])["state"], "cancelled")
        self.assertEqual(self.admit("another")["reason"], "managed admissions disabled")
        self.assertEqual(m.rollback(r, apply=True)["status"], "existing")

    def test_crash_before_and_after_exchange_can_resume_migration(self):
        original = m.exchange
        def crash_cutover(left, right):
            if left == self.legacy:
                raise OSError("fixture crash before exchange")
            return original(left, right)
        with patch.object(m, "exchange", side_effect=crash_cutover):
            with self.assertRaises(OSError):m.migrate(r, apply=True)
        self.assertEqual(self.legacy.read_bytes(), self.raw)
        with self.assertRaises(LedgerError):m.active_path(self.legacy)
        def exchange_then_crash(*args):
            original(*args)
            raise OSError("fixture crash after exchange")
        with patch.object(m, "exchange", side_effect=exchange_then_crash):
            with self.assertRaises(OSError):m.migrate(r, apply=True)
        self.assertTrue(self.legacy.is_dir())
        self.assertEqual(m.migrate(r, apply=True)["status"], "migrated")
        self.assertEqual(json.loads(self.target.read_text())["quota_denials"], self.state["quota_denials"])

    def test_crash_after_rollback_exchange_keeps_admissions_disabled(self):
        m.migrate(r, apply=True)
        self.release_legacy()
        original = m.exchange
        def exchange_then_crash(*args):
            original(*args)
            raise OSError("fixture crash")
        with patch.object(m, "exchange", side_effect=exchange_then_crash):
            with self.assertRaises(OSError):m.rollback(r, apply=True)
        self.assertEqual(self.legacy.read_bytes(), self.raw)
        self.assertEqual(self.admit()["reason"], "managed admissions disabled")
        with self.assertRaises(LedgerError):m.active_path(self.legacy)
        self.assertEqual(m.rollback(r, apply=True)["status"], "rolled-back")

    def test_corrupt_input_is_quarantined_without_empty_replacement(self):
        raw = b'{"reservations":'
        self.legacy.write_bytes(raw)
        before = self.files()
        with self.assertRaises(LedgerError):m.migrate(r)
        self.assertEqual(self.files(), before)
        with self.assertRaises(LedgerError):m.migrate(r, apply=True)
        self.assertEqual(self.legacy.read_bytes(), raw)
        self.assertEqual(next((self.root / "quarantine").iterdir()).read_bytes(), raw)
        self.assertFalse((self.root / "managed.sqlite3").exists())
        self.assertFalse((self.root / "state-migration.json").exists())

    def test_bad_backup_cannot_restore_and_admissions_stay_disabled(self):
        result = m.migrate(r, apply=True)
        self.release_legacy()
        Path(result["backup"]).write_bytes(b"{}")
        with self.assertRaises(LedgerError):m.rollback(r, apply=True)
        self.assertTrue(self.legacy.is_dir())
        self.assertEqual(self.admit()["reason"], "managed admissions disabled")

    def test_corrupt_managed_evidence_is_not_reset(self):
        self.ledger.path.write_bytes(b"invalid SQLite evidence")
        with self.assertRaises(LedgerError):m.migrate(r, apply=True)
        self.assertEqual(self.ledger.path.read_bytes(), b"invalid SQLite evidence")
        self.assertEqual(self.legacy.read_bytes(), self.raw)
        self.assertEqual(next((self.root / "quarantine").iterdir()).read_bytes(), b"invalid SQLite evidence")
        self.assertFalse((self.root / "state-migration.json").exists())

    def test_future_or_incomplete_manifest_cannot_select_empty_state(self):
        m.migrate(r, apply=True)
        path = self.root / "state-migration.json"
        record = json.loads(path.read_text())
        for bad in ({**record, "writer_generation": 999}, {"schema_version": 1}):
            path.write_text(json.dumps(bad))
            with self.assertRaises(LedgerError):m.active_path(self.legacy)
            with self.assertRaises(LedgerError):m.migrate(r, apply=True)
        self.assertTrue(self.legacy.is_dir())

    def test_unsupported_filesystem_fails_before_transition_manifest(self):
        with patch.object(m, "exchange", side_effect=LedgerError("unsupported")):
            with self.assertRaises(LedgerError):m.migrate(r, apply=True)
        self.assertEqual(self.legacy.read_bytes(), self.raw)
        self.assertFalse((self.root / "state-migration.json").exists())
        self.assertFalse(self.ledger.path.exists())

    def test_corrupt_migrated_state_never_becomes_empty(self):
        m.migrate(r, apply=True)
        for raw in (b"{", b"[]", b"null", b"{}"):
            self.target.write_bytes(raw)
            with self.assertRaises(LedgerError):m.active_path(self.legacy)
            with patch.object(r, "STATE", self.target):
                with self.assertRaises(LedgerError):r.load_json(self.target, {})

    def test_cli_routes_compatible_commands_through_migrated_state(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(r.main(["managed", "migrate", "--apply"]), 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "migrated")
        self.assertNotIn("legacy task", out.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(r.main(["report", "codex", "--done", "--id", "live"]), 0)
        self.assertEqual(json.loads(self.target.read_text())["reservations"], [])
        with redirect_stdout(io.StringIO()):self.assertEqual(r.main(["managed", "rollback", "--apply"]), 0)
        self.assertEqual(self.legacy.read_bytes(), self.raw)


if __name__ == "__main__":
    unittest.main()
