"""Offline transactional admission, crash recovery and lifecycle proof."""

import multiprocessing
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from managed_ledger import Ledger, LedgerError


ACCOUNT = {"account_ref": "synthetic-native-account", "fingerprint": "synthetic-version"}


def request(index=0, *, stamp=None, percent=80, **changes):
    stamp = time.time() if stamp is None else stamp
    value = dict(request_key=f"request-{index}",
                 task={"task_id": f"task-{index}", "spec_hash": f"hash-{index}", "context_hash": "none",
                       "judgment_source": "caller (test)", "floor": 2},
                 pick={"provider": "codex", "model": "test-model", "effort": "medium", "band": 2},
                 observation={"status": "ok", "account": ACCOUNT, "observed_at": stamp,
                              "buckets": [{"id": "weekly", "percent": percent, "source": "live"}]},
                 points=3, slot_limit=4, binding_now=lambda: ACCOUNT)
    value.update(changes)
    return value


def concurrent_admit(path, index, gate, output, points, slots):
    gate.wait()
    try:
        output.put(Ledger(path).admit(**request(index, points=points, slot_limit=slots))["status"])
    except Exception as exc:
        output.put(type(exc).__name__)


def crash_transaction(path, ready):
    with Ledger(path).transaction() as db:
        db.execute("UPDATE attempts SET active=0")
        ready.send(True)
        time.sleep(30)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "managed.sqlite3"
        self.ledger = Ledger(self.path)

    def receipt(self, attempt, **changes):
        return {"dispatch_id": "native-dispatch", "session_id": "native-session",
                **{k: attempt["pick"][k] for k in ("provider", "model", "effort")},
                **attempt["account"], **changes}

    def start(self, attempt):
        aid = attempt["attempt_id"]
        self.ledger.event(aid, aid + "-launch", "launching")
        return self.ledger.event(aid, aid + "-receipt", "started", receipt=self.receipt(attempt))["attempt"]

    def test_read_does_not_create_ledger(self):
        self.assertEqual(self.ledger.read(), [])
        self.assertFalse(self.path.exists())

    def test_points_and_slots_are_atomic_under_process_contention(self):
        context = multiprocessing.get_context("fork")
        for points, slots, expected in ((3, 20, 3), (1, 2, 2)):
            with self.subTest(points=points, slots=slots):
                path = self.path.with_name(f"concurrency-{points}.sqlite3")
                # Exercise first initialization under the same contention as
                # established-ledger admission; it must not lose a writer.
                if points == 1:
                    with Ledger(path).transaction():
                        pass
                gate, output = context.Event(), context.Queue()
                processes = [context.Process(target=concurrent_admit,
                                             args=(path, n, gate, output, points, slots)) for n in range(12)]
                for process in processes:
                    process.start()
                gate.set()
                for process in processes:
                    process.join(10)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)
                results = [output.get(timeout=2) for _ in processes]
                self.assertEqual(results.count("admitted"), expected, results)
                self.assertEqual(results.count("wait"), 12 - expected, results)

    def test_idempotent_request_and_changed_intent_rejected(self):
        values = request()
        first = self.ledger.admit(**values)
        again = self.ledger.admit(**values)
        self.assertEqual(again["status"], "existing")
        self.assertEqual(first["attempt"], again["attempt"])
        values["task"] = {**values["task"], "spec_hash": "changed"}
        with self.assertRaisesRegex(LedgerError, "idempotency"):
            self.ledger.admit(**values)

    def test_expiry_retains_commitment_and_prevents_blind_retry(self):
        stamp = time.time()
        first = self.ledger.admit(**request(stamp=stamp, points=10, lease_seconds=1))["attempt"]
        self.ledger.reconcile_expired(clock=lambda: stamp + 2)
        expired = self.ledger.read(first["attempt_id"])
        self.assertEqual(expired["state"], "reconciling")
        again = self.ledger.admit(**request(1, stamp=stamp + 2, clock=lambda: stamp + 2))
        self.assertEqual(again["reason"], "account point budget committed")
        with self.assertRaisesRegex(LedgerError, "not claimable"):
            self.ledger.event(first["attempt_id"], "retry-launch", "launching")

    def test_crashed_writer_rolls_back_hold_release(self):
        first = self.ledger.admit(**request(points=10))["attempt"]
        context = multiprocessing.get_context("fork")
        parent, child = context.Pipe()
        process = context.Process(target=crash_transaction, args=(self.path, child))
        process.start()
        self.assertTrue(parent.poll(5))
        self.assertTrue(parent.recv())
        process.kill()
        process.join(5)
        self.assertEqual(self.ledger.admit(**request(1))["status"], "wait")
        self.assertEqual(self.ledger.read(first["attempt_id"])["state"], "admitted")

    def test_launch_claim_only_once_and_failure_releases_exactly_once(self):
        attempt = self.ledger.admit(**request(points=10))["attempt"]
        aid = attempt["attempt_id"]
        self.assertTrue(self.ledger.event(aid, "claim", "launching")["applied"])
        self.assertFalse(self.ledger.event(aid, "claim", "launching")["applied"])
        with self.assertRaises(LedgerError):
            self.ledger.event(aid, "different-claim", "launching")
        self.ledger.event(aid, "failed", "launch_failed", evidence="confirmed-no-process")
        self.assertFalse(self.ledger.event(aid, "failed", "launch_failed", evidence="confirmed-no-process")["applied"])
        self.assertEqual(self.ledger.admit(**request(1))["status"], "admitted")

    def test_completion_is_not_acceptance_and_retains_stale_snapshot_debit(self):
        stamp = time.time()
        first = self.ledger.admit(**request(stamp=stamp, points=10))["attempt"]
        self.start(first)
        aid = first["attempt_id"]
        self.ledger.event(aid, "out", "producing")
        result = self.ledger.event(aid, "done", "completed", evidence="native-completion")
        self.assertEqual(result["attempt"]["state"], "completed")
        self.assertEqual(self.ledger.admit(**request(1, stamp=stamp))["status"], "wait")
        self.assertEqual(self.ledger.admit(**request(2))["status"], "admitted")
        with self.assertRaises(LedgerError):
            self.ledger.event(aid, "accept-empty", "accepted")
        self.assertEqual(self.ledger.event(aid, "review", "accepted", evidence="test-report-sha256")["attempt"]["state"], "accepted")

    def test_wrong_receipt_stays_reconciling(self):
        first = self.ledger.admit(**request(points=10))["attempt"]
        aid = first["attempt_id"]
        self.ledger.event(aid, "claim", "launching")
        result = self.ledger.event(aid, "receipt", "started", receipt=self.receipt(first, model="wrong-model"))
        self.assertEqual(result["attempt"]["state"], "reconciling")
        self.assertEqual(self.ledger.admit(**request(1))["status"], "wait")
        with self.assertRaises(LedgerError):
            self.ledger.event(aid, "done", "completed", evidence="unverified-completion")

    def test_account_switch_stale_and_future_observation_wait(self):
        for values in (request(binding_now=lambda: {**ACCOUNT, "fingerprint": "switched"}),
                       request(stamp=time.time() - 30), request(stamp=time.time() + 30)):
            self.assertTrue(self.ledger.admit(**values)["reprobe"])
        self.assertEqual(self.ledger.read(), [])

    def test_two_native_homes_cannot_double_spend_one_quota_account(self):
        values = request(points=10)
        values["observation"]["quota_account_ref"] = "provider-account"
        self.ledger.admit(**values)
        second = request(1)
        second["observation"]["account"] = {"account_ref": "another-home", "fingerprint": "another-version"}
        second["binding_now"] = lambda: second["observation"]["account"]
        second["observation"]["quota_account_ref"] = "provider-account"
        self.assertEqual(self.ledger.admit(**second)["status"], "wait")
        second["observation"].pop("quota_account_ref")
        self.assertEqual(self.ledger.admit(**second)["status"], "wait")
        second["observation"]["quota_account_ref"] = "different-provider-account"
        self.assertEqual(self.ledger.admit(**second)["status"], "admitted")

    def test_denial_survives_unknown_and_older_healthy_snapshot(self):
        first = self.ledger.admit(**request())["attempt"]
        self.start(first)
        old_observation = request(1)
        self.ledger.event(first["attempt_id"], "quota", "quota_failed", evidence="native-quota-error")
        self.assertEqual(self.ledger.admit(**old_observation)["reason"], "quota denied")
        unknown = request(2)
        unknown["observation"]["buckets"] = []
        unknown["observation"]["status"] = "unknown"
        self.assertEqual(self.ledger.admit(**unknown)["reason"], "quota denied")
        self.assertEqual(self.ledger.admit(**request(3))["status"], "admitted")

    def test_unattributed_denial_cannot_be_cleared_by_another_context(self):
        first = request()
        first["observation"]["status"] = "denied"
        self.assertEqual(self.ledger.admit(**first)["status"], "wait")
        second = request(1)
        second["observation"]["account"] = {"account_ref": "other-context", "fingerprint": "other-key"}
        second["binding_now"] = lambda: second["observation"]["account"]
        self.assertEqual(self.ledger.admit(**second)["status"], "wait")

    def test_cancellation_requires_confirmation_and_renewal_has_owner(self):
        first = self.ledger.admit(**request(points=10))["attempt"]
        self.start(first)
        aid = first["attempt_id"]
        self.ledger.event(aid, "cancel-request", "cancelling")
        self.assertEqual(self.ledger.admit(**request(1))["status"], "wait")
        with self.assertRaises(LedgerError):
            self.ledger.renew(aid, "wrong-owner")
        self.ledger.renew(aid, first["launch_key"])
        self.ledger.event(aid, "cancel-ack", "cancelled", evidence="native-termination")
        self.assertEqual(self.ledger.admit(**request(2))["status"], "admitted")

    def test_external_commitments_and_floor_are_enforced(self):
        self.assertEqual(self.ledger.admit(**request(external_points=8))["status"], "wait")
        self.assertEqual(self.ledger.admit(**request(external_slots=4))["status"], "wait")
        values = request()
        values["pick"]["band"] = 1
        with self.assertRaisesRegex(LedgerError, "floor"):
            self.ledger.admit(**values)

    def test_invalid_bucket_and_fractional_points_cannot_overcommit(self):
        values = request()
        values["observation"]["buckets"][0]["raw_status"] = "temporarily-unavailable"
        self.assertEqual(self.ledger.admit(**values)["status"], "wait")
        self.assertEqual(self.ledger.admit(**request(1, points=3.333334))["status"], "admitted")
        self.assertEqual(self.ledger.admit(**request(2, points=3.333334))["status"], "admitted")
        self.assertEqual(self.ledger.admit(**request(3, points=3.333334))["status"], "wait")

    def test_corruption_and_future_version_fail_without_recreating(self):
        self.path.write_bytes(b"corrupt sentinel")
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.admit(**request())
        self.assertEqual(self.path.read_bytes(), b"corrupt sentinel")
        self.path.unlink()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(LedgerError, "version"):
            self.ledger.admit(**request())


if __name__ == "__main__":
    unittest.main()
