"""Outcome store contract: validation, idempotency, gates, audit."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from outcome_store import OutcomeError, OutcomeStore


PROJECT = "/proj"
TASK_SHA = "a" * 64
CONFIG_SHA = "b" * 64
POLICY = "d" * 64
EVIDENCE = "c" * 64
SELECTED = {"provider": "opencode", "model": "deepseek",
            "effort": "medium", "family": "deepseek"}
ACTUAL_A = {"provider": "opencode", "model": "deepseek",
            "effort": "medium", "family": "deepseek"}
ACTUAL_B = {"provider": "codex", "model": "gpt",
            "effort": "high", "family": "gpt"}


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "outcomes.sqlite3"
        self.store = OutcomeStore(self.path)

    def decision(self, **overrides):
        rec = {
            "decision_id": "dec-1", "project": PROJECT, "task_sha256": TASK_SHA,
            "policy_revision": POLICY, "config_sha256": CONFIG_SHA,
            "selected": dict(SELECTED), "judgment_source": "caller",
            "review_required": False, "quality_floor": 1, "task_tier": "implementation", "at": 1000.0,
        }
        rec.update(overrides)
        return rec

    def launch(self, **overrides):
        rec = {
            "dispatch_id": "disp-1", "decision_id": None, "project": PROJECT,
            "task_sha256": TASK_SHA, "actual": dict(ACTUAL_A),
            "override_reason": None, "worktree": None,
            "native_session_id": None, "account_status": "unverified",
            "at": 1001.0,
        }
        rec.update(overrides)
        return rec

    def evidence(self):
        return EVIDENCE


class DecisionTests(_StoreCase):
    def test_happy_path_persists_entire_record(self):
        out = self.store.decision(self.decision())
        self.assertEqual(out["decision_id"], "dec-1")
        self.assertEqual(out["selected"], SELECTED)
        self.assertEqual(out["review_required"], False)
        self.assertEqual(out["at"], 1000.0)

    def test_selected_null_allowed(self):
        out = self.store.decision(self.decision(selected=None))
        self.assertIsNone(out["selected"])

    def test_unknown_fields_rejected(self):
        with self.assertRaises(OutcomeError):
            self.store.decision(self.decision(extra_field="x"))

    def test_invalid_sha256_rejected(self):
        with self.assertRaises(OutcomeError):
            self.store.decision(self.decision(task_sha256="not-hex"))
        with self.assertRaises(OutcomeError):
            self.store.decision(self.decision(task_sha256="a" * 63))
        with self.assertRaises(OutcomeError):
            self.store.decision(self.decision(task_sha256="A" * 64))
        with self.assertRaises(OutcomeError):
            self.store.decision(self.decision(config_sha256="b" * 63))

    def test_review_required_must_be_bool(self):
        with self.assertRaises(OutcomeError):
            self.store.decision(self.decision(review_required=1))
        with self.assertRaises(OutcomeError):
            self.store.decision(self.decision(review_required="yes"))

    def test_at_must_be_finite_number(self):
        for bad in (float("nan"), float("inf"), -float("inf"), "1000", None):
            with self.assertRaises(OutcomeError):
                self.store.decision(self.decision(at=bad))
        with self.assertRaises(OutcomeError):
            self.store.decision(self.decision(at=True))

    def test_idempotent_repeat_returns_same(self):
        a = self.store.decision(self.decision())
        b = self.store.decision(self.decision())
        self.assertEqual(a, b)
        with closing(sqlite3.connect(self.path)) as db:
            count = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_conflicting_idempotency_fails(self):
        self.store.decision(self.decision())
        with self.assertRaises(OutcomeError):
            self.store.decision(self.decision(review_required=True))
        with self.assertRaises(OutcomeError):
            self.store.decision(self.decision(judgment_source="other"))

    def test_permission_0600_on_create(self):
        new = self.path.parent / "perm.sqlite3"
        OutcomeStore(new).decision(self.decision())
        mode = new.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_get_decision_returns_record(self):
        self.store.decision(self.decision(decision_id="dec-x"))
        out = self.store.get_decision("dec-x")
        self.assertEqual(out["decision_id"], "dec-x")
        self.assertIsNone(self.store.get_decision("missing"))

    def test_get_decision_rejects_empty(self):
        with self.assertRaises(OutcomeError):
            self.store.get_decision("")
        with self.assertRaises(OutcomeError):
            self.store.get_decision(None)


class LaunchTests(_StoreCase):
    def test_unlinked_launch_allowed(self):
        out = self.store.launch(self.launch(dispatch_id="u-1"))
        self.assertIsNone(out["decision_id"])

    def test_linked_launch_must_match_project(self):
        self.store.decision(self.decision(decision_id="dec-A"))
        with self.assertRaises(OutcomeError):
            self.store.launch(self.launch(
                dispatch_id="d-1", decision_id="dec-A", project="/other"))

    def test_linked_launch_must_match_hash(self):
        self.store.decision(self.decision(decision_id="dec-A"))
        with self.assertRaises(OutcomeError):
            self.store.launch(self.launch(
                dispatch_id="d-1", decision_id="dec-A", task_sha256="d" * 64))

    def test_linked_launch_unknown_decision_rejected(self):
        with self.assertRaises(OutcomeError):
            self.store.launch(self.launch(
                dispatch_id="d-1", decision_id="missing"))

    def test_selection_override_requires_reason(self):
        self.store.decision(self.decision(
            decision_id="dec-A",
            selected={"provider": "opencode", "model": "deepseek",
                     "effort": "medium", "family": "deepseek"}))
        with self.assertRaises(OutcomeError):
            self.store.launch(self.launch(
                dispatch_id="d-1", decision_id="dec-A",
                actual={"provider": "codex", "model": "gpt",
                        "effort": "high", "family": "gpt"},
                override_reason=None))

    def test_selection_override_with_reason_accepted(self):
        self.store.decision(self.decision(decision_id="dec-A"))
        out = self.store.launch(self.launch(
            dispatch_id="d-1", decision_id="dec-A", actual=ACTUAL_B,
            override_reason="provider outage"))
        self.assertEqual(out["override_reason"], "provider outage")

    def test_family_difference_does_not_require_reason(self):
        self.store.decision(self.decision(
            decision_id="dec-A",
            selected={"provider": "opencode", "model": "deepseek",
                     "effort": "medium", "family": "deepseek"}))
        out = self.store.launch(self.launch(
            dispatch_id="d-1", decision_id="dec-A",
            actual={"provider": "opencode", "model": "deepseek",
                    "effort": "medium", "family": "opencode"}))
        self.assertEqual(out["actual"]["family"], "opencode")

    def test_unknown_fields_rejected(self):
        with self.assertRaises(OutcomeError):
            self.store.launch(self.launch(extra_field="x"))

    def test_account_status_strict(self):
        for bad in ("unknown", "OK", None, ""):
            with self.assertRaises(OutcomeError):
                self.store.launch(self.launch(account_status=bad))

    def test_idempotent_repeat_returns_same(self):
        self.store.launch(self.launch(dispatch_id="d-1"))
        again = self.store.launch(self.launch(dispatch_id="d-1"))
        self.assertEqual(again["dispatch_id"], "d-1")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM launches").fetchone()[0], 1)

    def test_conflicting_idempotency_fails(self):
        self.store.launch(self.launch(dispatch_id="d-1"))
        with self.assertRaises(OutcomeError):
            self.store.launch(self.launch(
                dispatch_id="d-1", worktree="/wt"))

    def test_at_must_be_finite_number(self):
        for bad in (float("nan"), float("inf"), "1000", True):
            with self.assertRaises(OutcomeError):
                self.store.launch(self.launch(dispatch_id="d-1", at=bad))

    def test_get_launch_returns_record(self):
        self.store.launch(self.launch(dispatch_id="d-1"))
        out = self.store.get_launch("d-1")
        self.assertEqual(out["dispatch_id"], "d-1")
        self.assertIsNone(self.store.get_launch("missing"))

    def test_get_launch_rejects_empty(self):
        with self.assertRaises(OutcomeError):
            self.store.get_launch("")


class EventTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.store.decision(self.decision(
            decision_id="dec-1", review_required=True))
        self.store.launch(self.launch(
            dispatch_id="disp-1", decision_id="dec-1"))

    def test_unknown_dispatch_rejected(self):
        with self.assertRaises(OutcomeError):
            self.store.event("missing", "ev-1", "completed",
                             {"evidence_sha256": self.evidence()})

    def test_predates_launch_rejected(self):
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-1", "completed",
                             {"evidence_sha256": self.evidence()},
                             at=900.0)

    def test_terminal_evidence_alone_does_not_accept(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-a", "accepted",
                             {"actor": "alice",
                              "evidence_sha256": self.evidence()})

    def test_accepted_requires_completed(self):
        self.store.event("disp-1", "ev-r", "review",
                         {"actor": "bob", "provider": "codex",
                          "model": "gpt", "family": "gpt",
                          "verdict": "accepted",
                          "evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-a", "accepted",
                             {"actor": "alice",
                              "evidence_sha256": self.evidence()})

    def test_accepted_unlinked_fails(self):
        self.store.launch(self.launch(dispatch_id="disp-u",
                                      decision_id=None))
        self.store.event("disp-u", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-u", "ev-a", "accepted",
                             {"actor": "alice",
                              "evidence_sha256": self.evidence()})

    def test_review_required_no_review_rejected(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-a", "accepted",
                             {"actor": "alice",
                              "evidence_sha256": self.evidence()})

    def test_review_required_rejected_latest_rejected(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-r1", "review",
                         {"actor": "bob", "provider": "codex",
                          "model": "gpt", "family": "gpt",
                          "verdict": "rejected",
                          "evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-a", "accepted",
                             {"actor": "alice",
                              "evidence_sha256": self.evidence()})

    def test_self_provider_review_blocks_accept(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-r", "review",
                         {"actor": "bob", "provider": "opencode",
                          "model": "gpt", "family": "gpt",
                          "verdict": "accepted",
                          "evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-a", "accepted",
                             {"actor": "alice",
                              "evidence_sha256": self.evidence()})

    def test_self_family_review_blocks_accept(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-r", "review",
                         {"actor": "bob", "provider": "codex",
                          "model": "gpt", "family": "deepseek",
                          "verdict": "accepted",
                          "evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-a", "accepted",
                             {"actor": "alice",
                              "evidence_sha256": self.evidence()})

    def test_review_missing_model_rejected(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-r", "review",
                             {"actor": "bob", "provider": "codex",
                              "model": "", "family": "gpt",
                              "verdict": "accepted",
                              "evidence_sha256": self.evidence()})

    def test_highstakes_happy_path(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-r", "review",
                         {"actor": "bob", "provider": "codex",
                          "model": "gpt", "family": "gpt",
                          "verdict": "accepted",
                          "evidence_sha256": self.evidence()})
        out = self.store.event("disp-1", "ev-a", "accepted",
                               {"actor": "alice",
                                "evidence_sha256": self.evidence()})
        self.assertEqual(out["kind"], "accepted")

    def test_low_risk_no_review_accepted(self):
        self.store.decision(self.decision(
            decision_id="dec-2", review_required=False))
        self.store.launch(self.launch(
            dispatch_id="disp-2", decision_id="dec-2"))
        self.store.event("disp-2", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        out = self.store.event("disp-2", "ev-a", "accepted",
                               {"actor": "alice",
                                "evidence_sha256": self.evidence()})
        self.assertEqual(out["kind"], "accepted")

    def test_low_risk_rejected_review_blocks_accept(self):
        self.store.decision(self.decision(
            decision_id="dec-2", review_required=False))
        self.store.launch(self.launch(
            dispatch_id="disp-2", decision_id="dec-2"))
        self.store.event("disp-2", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-2", "ev-r", "review",
                         {"actor": "bob", "provider": "codex",
                          "model": "gpt", "family": "gpt",
                          "verdict": "rejected",
                          "evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-2", "ev-a", "accepted",
                             {"actor": "alice",
                              "evidence_sha256": self.evidence()})

    def test_later_accepted_review_after_rework(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-r1", "review",
                         {"actor": "bob", "provider": "codex",
                          "model": "gpt", "family": "gpt",
                          "verdict": "rejected",
                          "evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-w", "rework",
                         {"actor": "alice", "seconds": 30.0,
                          "evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-r2", "review",
                         {"actor": "carol", "provider": "claude",
                          "model": "opus", "family": "opus",
                          "verdict": "accepted",
                          "evidence_sha256": self.evidence()})
        out = self.store.event("disp-1", "ev-a", "accepted",
                               {"actor": "alice",
                                "evidence_sha256": self.evidence()})
        self.assertEqual(out["kind"], "accepted")

    def test_terminal_kinds_cannot_conflict(self):
        self.store.event("disp-1", "ev-c", "completed", {"evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-f", "failed", {"evidence_sha256": self.evidence()})

    def test_terminal_replay_idempotent(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()}, at=1100.0)
        again = self.store.event("disp-1", "ev-c", "completed",
                                 {"evidence_sha256": self.evidence()},
                                 at=1100.0)
        self.assertEqual(again["kind"], "completed")

    def test_post_accept_blocked_for_rework_usage_review_accepted(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-r", "review",
                         {"actor": "bob", "provider": "codex",
                          "model": "gpt", "family": "gpt",
                          "verdict": "accepted",
                          "evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-a", "accepted",
                         {"actor": "alice",
                          "evidence_sha256": self.evidence()})
        for kind, data in (
            ("rework", {"actor": "x", "seconds": 1.0,
                        "evidence_sha256": self.evidence()}),
            ("usage", {"source": "s"}),
            ("review", {"actor": "y", "provider": "codex", "model": "gpt",
                        "family": "gpt", "verdict": "accepted",
                        "evidence_sha256": self.evidence()}),
        ):
            with self.assertRaises(OutcomeError):
                self.store.event("disp-1", f"ev-{kind}", kind, data)
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-a2", "accepted",
                             {"actor": "alice",
                              "evidence_sha256": self.evidence()})

    def test_terminal_rejected_after_accept(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-r", "review",
                         {"actor": "bob", "provider": "codex",
                          "model": "gpt", "family": "gpt",
                          "verdict": "accepted",
                          "evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-a", "accepted",
                         {"actor": "alice",
                          "evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-c2", "cancelled",
                             {"evidence_sha256": self.evidence()}, at=time.time() + 1)

    def test_event_idempotent_returns_same(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()}, at=1100.0)
        again = self.store.event("disp-1", "ev-c", "completed",
                                 {"evidence_sha256": self.evidence()},
                                 at=1100.0)
        self.assertEqual(again["kind"], "completed")

    def test_event_idempotency_conflict(self):
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-c", "failed",
                             {"evidence_sha256": self.evidence()})

    def test_evidence_sha256_format(self):
        for bad in ("x" * 64, "g" * 64, "c" * 63, "C" * 64, 123, None):
            with self.assertRaises(OutcomeError):
                self.store.event("disp-1", "ev-c", "completed",
                                 {"evidence_sha256": bad})

    def test_review_validation(self):
        base = {"actor": "bob", "provider": "codex", "model": "gpt",
                "family": "gpt", "verdict": "accepted",
                "evidence_sha256": self.evidence()}
        for key in ("actor", "provider", "model", "family"):
            with self.assertRaises(OutcomeError):
                self.store.event("disp-1", "ev-r", "review",
                                 {**base, key: ""})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-r", "review",
                             {**base, "verdict": "approved"})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-r", "review",
                             {**base, "extra": 1})

    def test_rework_validation(self):
        base = {"actor": "alice", "seconds": 5.0,
                "evidence_sha256": self.evidence()}
        for bad in ("abc", -1.0, float("nan"), float("inf"), True):
            with self.assertRaises(OutcomeError):
                self.store.event("disp-1", "ev-w", "rework",
                                 {**base, "seconds": bad})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-w", "rework",
                             {**base, "actor": ""})

    def test_usage_validation(self):
        base = {"source": "native"}
        for bad in (True, -1, -0.5, "1", None):
            with self.assertRaises(OutcomeError):
                self.store.event("disp-1", "ev-u", "usage",
                                 {**base, "input_tokens": bad})
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-u", "usage",
                                 {**base, "extra": 1})
        out = self.store.event("disp-1", "ev-u", "usage",
                               {**base, "input_tokens": 10,
                                "output_tokens": 5})
        self.assertEqual(out["data"]["input_tokens"], 10)
        self.assertNotIn("cache_read_tokens", out["data"])

    def test_unknown_kind_rejected(self):
        with self.assertRaises(OutcomeError):
            self.store.event("disp-1", "ev-x", "settled",
                             {"actor": "a", "evidence_sha256": self.evidence()})

    def test_at_must_be_finite_number(self):
        for bad in (float("nan"), float("inf"), "abc", True):
            with self.assertRaises(OutcomeError):
                self.store.event("disp-1", "ev-c", "completed",
                                 {"evidence_sha256": self.evidence()},
                                 at=bad)


class AuditTests(_StoreCase):
    def test_audit_missing_db_returns_empty(self):
        fresh = self.path.parent / "missing.sqlite3"
        fresh_store = OutcomeStore(fresh)
        self.assertFalse(fresh.exists())
        out = fresh_store.audit()
        self.assertEqual(out["decisions"], [])
        self.assertEqual(out["attempts"], [])
        self.assertEqual(out["metrics"]["total_launches"], 0)
        self.assertFalse(fresh.exists())

    def test_audit_on_existing_store_does_not_create_file(self):
        self.store.decision(self.decision())
        before = self.path.stat().st_mtime_ns
        out = self.store.audit()
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertTrue(self.path.exists())
        self.assertEqual(out["metrics"]["total_launches"], 0)

    def test_metrics_with_explicit_denominators(self):
        self.store.decision(self.decision(
            decision_id="dec-1", review_required=True))
        self.store.launch(self.launch(
            dispatch_id="disp-1", decision_id="dec-1"))
        self.store.launch(self.launch(dispatch_id="disp-u",
                                      decision_id=None))
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-u1", "usage", {"source": "n"})
        self.store.event("disp-1", "ev-w1", "rework",
                         {"actor": "alice", "seconds": 5.0,
                          "evidence_sha256": self.evidence()})
        out = self.store.audit()
        m = out["metrics"]
        self.assertEqual(m["total_launches"], 2)
        self.assertEqual(m["linked_launches"], 1)
        self.assertEqual(m["unlinked_launches"], 1)
        self.assertEqual(m["completed_launches"], 1)
        self.assertEqual(m["accepted_launches"], 0)
        self.assertEqual(m["known_usage_launches"], 0)
        self.assertEqual(m["unknown_usage_launches"], 2)
        self.assertEqual(m["known_rework_launches"], 1)
        self.assertEqual(m["unknown_rework_launches"], 1)
        self.assertEqual(m["missing_required_review_launches"], 1)

    def test_rejected_review_counted(self):
        self.store.decision(self.decision(
            decision_id="dec-1", review_required=True))
        self.store.launch(self.launch(
            dispatch_id="disp-1", decision_id="dec-1"))
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-r", "review",
                         {"actor": "bob", "provider": "codex",
                          "model": "gpt", "family": "gpt",
                          "verdict": "rejected",
                          "evidence_sha256": self.evidence()})
        m = self.store.audit()["metrics"]
        self.assertEqual(m["rejected_review_launches"], 1)
        self.assertEqual(m["missing_required_review_launches"], 1)

    def test_time_to_accepted_includes_retries(self):
        self.store.decision(self.decision(
            decision_id="dec-1", review_required=False))
        self.store.launch(self.launch(
            dispatch_id="disp-1", decision_id="dec-1", at=1000.0))
        self.store.launch(self.launch(
            dispatch_id="disp-2", decision_id="dec-1", at=1500.0))
        self.store.event("disp-1", "ev-c-1", "completed",
                         {"evidence_sha256": self.evidence()}, at=1100.0)
        self.store.event("disp-2", "ev-c-2", "completed",
                         {"evidence_sha256": self.evidence()}, at=1600.0)
        self.store.event("disp-1", "ev-a-1", "accepted",
                         {"actor": "alice",
                          "evidence_sha256": self.evidence()}, at=1200.0)
        self.store.event("disp-2", "ev-a-2", "accepted",
                         {"actor": "alice",
                          "evidence_sha256": self.evidence()}, at=1700.0)
        out = self.store.audit()
        durations = out["time_to_accepted_task"]
        self.assertEqual(len(durations), 1)
        self.assertEqual(durations[0]["count"], 1)
        self.assertEqual(durations[0]["seconds"], [200.0])
        self.assertEqual(durations[0]["median_seconds"], 200.0)

    def test_coordinator_repair_separate(self):
        self.store.decision(self.decision(
            decision_id="dec-1", review_required=False))
        self.store.launch(self.launch(
            dispatch_id="disp-1", decision_id="dec-1"))
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-w1", "rework",
                         {"actor": "alice", "seconds": 10.0,
                          "evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-w2", "rework",
                         {"actor": "coordinator", "seconds": 3.5,
                          "evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-a", "accepted",
                         {"actor": "alice",
                          "evidence_sha256": self.evidence()})
        m = self.store.audit()["metrics"]
        self.assertEqual(m["coordinator_repair"]["events"], 2)
        self.assertEqual(m["coordinator_repair"]["seconds_total"], 13.5)
        self.assertEqual(m["coordinator_repair"]["accepted_with_coordinator_rework"], 1)
        self.assertEqual(m["coordinator_repair"]["coverage_accepted"], 1.0)

    def test_unknown_rework_not_assumed_zero(self):
        self.store.decision(self.decision(
            decision_id="dec-1", review_required=False))
        self.store.launch(self.launch(
            dispatch_id="disp-1", decision_id="dec-1"))
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-a", "accepted",
                         {"actor": "alice",
                          "evidence_sha256": self.evidence()})
        m = self.store.audit()["metrics"]
        self.assertEqual(m["known_rework_launches"], 0)
        self.assertEqual(m["unknown_rework_launches"], 1)
        self.assertEqual(m["coordinator_repair"]["seconds_total"], 0.0)
        self.assertEqual(m["coordinator_repair"]["coverage_accepted"], 0.0)

    def test_usage_latest_not_summed(self):
        self.store.decision(self.decision(
            decision_id="dec-1", review_required=False))
        self.store.launch(self.launch(
            dispatch_id="disp-1", decision_id="dec-1"))
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()}, at=1050.0)
        self.store.event("disp-1", "ev-u1", "usage",
                         {"source": "first", "input_tokens": 100,
                          "output_tokens": 50}, at=1100.0)
        self.store.event("disp-1", "ev-u2", "usage",
                         {"source": "second", "input_tokens": 30,
                          "output_tokens": 10, "cache_read_tokens": 5},
                         at=1200.0)
        self.store.event("disp-1", "ev-a", "accepted",
                         {"actor": "alice",
                          "evidence_sha256": self.evidence()}, at=1300.0)
        out = self.store.audit()
        attempt = next(a for a in out["attempts"]
                       if a["launch"]["dispatch_id"] == "disp-1")
        usage = attempt["latest_usage"]
        self.assertEqual(usage["source"], "second")
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["output_tokens"], 10)
        self.assertEqual(usage["cache_read_tokens"], 5)

    def test_zero_usage_counts_as_known(self):
        self.store.decision(self.decision(
            decision_id="dec-1", review_required=False))
        self.store.launch(self.launch(
            dispatch_id="disp-1", decision_id="dec-1"))
        self.store.event("disp-1", "ev-c", "completed",
                         {"evidence_sha256": self.evidence()})
        self.store.event("disp-1", "ev-u", "usage",
                         {"source": "native", "input_tokens": 0})
        self.store.event("disp-1", "ev-a", "accepted",
                         {"actor": "alice",
                          "evidence_sha256": self.evidence()})
        m = self.store.audit()["metrics"]
        self.assertEqual(m["known_usage_launches"], 1)
        self.assertEqual(m["unknown_usage_launches"], 0)

    def test_project_filter(self):
        self.store.decision(self.decision(
            decision_id="dec-1", project="/A"))
        self.store.decision(self.decision(
            decision_id="dec-2", project="/B"))
        self.store.launch(self.launch(
            dispatch_id="d-1", decision_id="dec-1", project="/A"))
        self.store.launch(self.launch(
            dispatch_id="d-2", decision_id="dec-2", project="/B"))
        out_a = self.store.audit(project="/A")
        self.assertEqual([d["decision_id"] for d in out_a["decisions"]],
                         ["dec-1"])
        self.assertEqual([a["launch"]["dispatch_id"]
                          for a in out_a["attempts"]], ["d-1"])
        out_all = self.store.audit()
        self.assertEqual(len(out_all["decisions"]), 2)
        self.assertEqual(len(out_all["attempts"]), 2)


class ConcurrencyTests(_StoreCase):
    def test_concurrent_writes_do_not_corrupt(self):
        results = []
        errors = []

        def writer(idx):
            try:
                store = OutcomeStore(self.path)
                store.decision(self.decision(decision_id=f"dec-{idx}"))
                store.launch(self.launch(
                    dispatch_id=f"d-{idx}", decision_id=f"dec-{idx}",
                    project=PROJECT))
                results.append(idx)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decisions").fetchone()[0], 8)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM launches").fetchone()[0], 8)

    def test_concurrent_duplicate_decision_idempotent(self):
        results = []
        errors = []

        def writer():
            try:
                store = OutcomeStore(self.path)
                store.decision(self.decision())
                results.append(1)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decisions").fetchone()[0], 1)


class RetentionTests(_StoreCase):
    def test_250_records_persist(self):
        for i in range(250):
            self.store.decision(self.decision(decision_id=f"dec-{i}"))
            self.store.launch(self.launch(
                dispatch_id=f"d-{i}", decision_id=f"dec-{i}"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decisions").fetchone()[0], 250)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM launches").fetchone()[0], 250)


class SchemaTests(_StoreCase):
    def test_schema_mismatch_rejected(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version = 999")
            db.commit()
        with self.assertRaises(OutcomeError):
            OutcomeStore(self.path).audit()


class AcceptanceRegressionTests(_StoreCase):
    def linked(self, required=True):
        self.store.decision(self.decision(review_required=required))
        self.store.launch(self.launch(decision_id="dec-1"))

    def emit(self, kind, data, event_id=None, at=None):
        return self.store.event("disp-1", event_id or kind, kind, data, at=at)

    def review(self, at=1101):
        return self.emit("review", dict(actor="reviewer", provider="codex", model="gpt",
            family="gpt", verdict="accepted", evidence_sha256=EVIDENCE), at=at)

    def test_default_timestamp_replay_is_idempotent(self):
        self.linked(False)
        first = self.emit("completed", {"evidence_sha256": EVIDENCE})
        self.assertEqual(first, self.emit("completed", {"evidence_sha256": EVIDENCE}))

    def test_required_review_invalidated_by_later_repair(self):
        self.linked()
        self.emit("completed", {"evidence_sha256": EVIDENCE}, at=1100)
        self.review()
        self.emit("rework", dict(actor="agent-123", seconds=10, evidence_sha256=EVIDENCE), at=1102)
        with self.assertRaises(OutcomeError):
            self.emit("accepted", dict(actor="coordinator", evidence_sha256=EVIDENCE), at=1103)
        self.assertEqual(self.store.audit()["metrics"]["missing_required_review_launches"], 1)

    def test_same_family_review_is_still_missing_in_metrics(self):
        self.linked()
        self.emit("review", dict(actor="reviewer", provider="other", model="alias",
            family=ACTUAL_A["family"], verdict="accepted", evidence_sha256=EVIDENCE))
        self.assertEqual(self.store.audit()["metrics"]["missing_required_review_launches"], 1)

    def test_failed_first_attempt_included_in_task_latency(self):
        self.linked(False)
        self.emit("failed", {"evidence_sha256": EVIDENCE}, at=1100)
        self.store.decision(self.decision(decision_id="dec-2", at=1200))
        self.store.launch(self.launch(dispatch_id="disp-2", decision_id="dec-2", at=1201))
        self.store.event("disp-2", "completed2", "completed", {"evidence_sha256": EVIDENCE}, at=1300)
        self.store.event("disp-2", "accepted2", "accepted", dict(actor="reviewer", evidence_sha256=EVIDENCE), at=1401)
        report = self.store.audit()
        self.assertEqual(report["metrics"]["time_to_accepted_task"], dict(count=1, median_seconds=400))

    def test_null_effort_and_missing_fields(self):
        self.store.decision(self.decision(selected={**SELECTED, "effort": None}))
        self.store.launch(self.launch(decision_id="dec-1", actual={**ACTUAL_A, "effort": None}))
        for method, record in [(self.store.decision, self.decision()), (self.store.launch, self.launch())]:
            for field in record:
                with self.subTest(field=field), self.assertRaises(OutcomeError):
                    method({k:v for k,v in record.items() if k != field})

    def test_nonnegative_time_and_high_stakes_review_policy(self):
        for record in [self.decision(at=-1), self.decision(task_tier="high_stakes", quality_floor=3),
                       self.decision(quality_floor=True), self.decision(at=10**1000)]:
            with self.assertRaises(OutcomeError):
                self.store.decision(record)

    def test_no_pick_launch_requires_explicit_override(self):
        self.store.decision(self.decision(selected=None))
        with self.assertRaises(OutcomeError):
            self.store.launch(self.launch(decision_id="dec-1"))
        self.store.launch(self.launch(decision_id="dec-1", override_reason="user pilot"))

    def test_source_only_usage_is_unknown_per_category(self):
        self.linked(False)
        self.emit("usage", {"source":"native", "input_tokens":0})
        metrics = self.store.audit()["metrics"]
        self.assertEqual(metrics["usage_categories"]["input_tokens"], dict(total=0, known_launches=1, unknown_launches=0))
        self.assertEqual(metrics["usage_categories"]["output_tokens"]["unknown_launches"], 1)

    def test_exact_binding_after_hook_keeps_original_launch(self):
        self.store.decision(self.decision())
        self.store.launch(self.launch())
        bound = self.store.launch(self.launch(decision_id="dec-1"))
        self.assertEqual(bound["decision_id"], "dec-1")
        self.assertEqual(self.store.launch(self.launch(decision_id="dec-1")), bound)
        with closing(sqlite3.connect(self.path)) as db:
            original = json.loads(db.execute("SELECT record_json FROM launches").fetchone()[0])
        self.assertIsNone(original["decision_id"])
        self.assertEqual(self.store.audit()["metrics"]["linked_launches"], 1)
        with self.assertRaises(OutcomeError):
            self.store.launch(self.launch(decision_id="dec-1", task_sha256="f"*64))

    def test_read_only_constructor_creates_nothing(self):
        nested = self.path.parent / "absent" / "store.db"
        self.assertEqual(OutcomeStore(nested).audit()["attempts"], [])
        self.assertFalse(nested.parent.exists())

if __name__ == "__main__":
    unittest.main()