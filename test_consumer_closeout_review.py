"""Independent peer-review adversarial tests for the closeout contract.

These tests are owned by the independent reviewer under issue #29 (follow-up
to #20). They intentionally bypass happy-path coverage to exercise the
contracts the implementation claims: malformed last-event rollback, duplicate
conflicts, differing cwd, review qualification, post-repair freshness and
real CLI parsing.

All state is temporary and local; no model/provider calls, no network, no
real CLI launches. Same-family reviews must never be reported as
independent different-provider reviews.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import outcome_cli
import outcome_store
import rightsize as r


PROJECT = "/coordinator/project"
TASK = "Fix the focus trap and run regressions."
TASK_SHA = hashlib.sha256(TASK.encode()).hexdigest()
CONFIG_SHA = "b" * 64
POLICY = "d" * 64
SELECTED = {"provider": "minimax", "model": "MiniMax-M3",
            "effort": "medium", "family": "minimax-m3"}
ACTUAL = {"provider": "minimax", "model": "MiniMax-M3",
          "effort": "medium", "family": "minimax-m3"}


def _write_evidence_blob(tmp_dir, eid, blob):
    """Write blob under tmp_dir/evidence/<eid>.bin, return basename.

    The basename is what the manifest's ``evidence_path`` field carries,
    because the closeout CLI resolves ``evidence_path`` relative to the
    directory holding the manifest. Reviewer-owned tests therefore co-locate
    the manifest under ``tmp/evidence/manifest.json`` so the paths match.
    """
    root = Path(tmp_dir) / "evidence"
    root.mkdir(exist_ok=True)
    path = root / f"{eid}.bin"
    path.write_bytes(blob)
    return path.name


class _ReviewerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state.json"
        self.path = self.tmp / "outcomes.sqlite3"
        self.store = outcome_store.OutcomeStore(self.path)
        self.store.decision({
            "decision_id": "dec-1", "project": PROJECT, "task_sha256": TASK_SHA,
            "policy_revision": POLICY, "config_sha256": CONFIG_SHA,
            "selected": dict(SELECTED), "judgment_source": "caller",
            "review_required": True, "quality_floor": 3, "task_tier": "high_stakes",
            "at": 1000.0,
        })
        self.store.launch({
            "dispatch_id": "disp-1", "decision_id": "dec-1", "project": PROJECT,
            "task_sha256": TASK_SHA, "actual": dict(ACTUAL),
            "override_reason": None, "worktree": None,
            "native_session_id": None, "account_status": "unverified",
            "at": 1001.0,
        })

    def closeout(self, dispatch, events, **kwargs):
        """Convenience wrapper: pass evidence_root only if not provided."""
        if "evidence_root" not in kwargs:
            kwargs["evidence_root"] = self.tmp / "evidence"
        return self.store.closeout(dispatch, events, **kwargs)

    def count_events(self, dispatch="disp-1"):
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                "SELECT COUNT(*) FROM events WHERE dispatch_id=?",
                (dispatch,)).fetchone()[0]


class MalformedLastEventRollback(_ReviewerCase):
    """A malformed final event must roll back the entire batch."""

    def test_missing_required_field_in_last_event_rolls_back_batch(self):
        events = [
            {"event_id": "ev-completed", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-completed", b"a")},
            {"event_id": "ev-rework", "kind": "rework",
             "data": {"actor": "alice", "seconds": 5,
                      "evidence_sha256": hashlib.sha256(b"b").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-rework", b"b")},
            {"event_id": "ev-bad", "kind": "review",
             "data": {"actor": "r", "verdict": "accepted",
                      "evidence_sha256": hashlib.sha256(b"c").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-bad", b"c")},
        ]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout("disp-1", events, decision_id="dec-1")
        self.assertEqual(self.count_events(), 0)

    def test_invented_kind_in_last_event_rolls_back_batch(self):
        events = [
            {"event_id": "ev-completed", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-completed", b"a")},
            {"event_id": "ev-rework", "kind": "rework",
             "data": {"actor": "alice", "seconds": 5,
                      "evidence_sha256": hashlib.sha256(b"b").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-rework", b"b")},
            {"event_id": "ev-bad", "kind": "invented_kind", "data": {},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-bad", b"x")},
        ]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout("disp-1", events, decision_id="dec-1")
        self.assertEqual(self.count_events(), 0)

    def test_unqualified_final_reviewer_rolls_back_completion(self):
        events = [
            {"event_id": "ev-completed", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-completed", b"a")},
            {"event_id": "ev-self-review", "kind": "review",
             "data": {"actor": "r", "provider": "minimax", "model": "MiniMax-M3",
                      "family": "minimax-m3", "verdict": "accepted",
                      "evidence_sha256": hashlib.sha256(b"b").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-self-review", b"b")},
            {"event_id": "ev-accept", "kind": "accepted",
             "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"c").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-accept", b"c")},
        ]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout("disp-1", events, decision_id="dec-1")
        self.assertEqual(self.count_events(), 0)


class DuplicateConflict(_ReviewerCase):
    """Duplicate event_ids must fail closed; cross-dispatch reuse must fail."""

    def test_identical_event_id_with_different_content_fails(self):
        events_ok = [
            {"event_id": "ev-c", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-c", b"a")},
        ]
        self.closeout("disp-1", events_ok, decision_id="dec-1")
        events_dup = [
            {"event_id": "ev-c", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"b").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-c-2", b"b")},
        ]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout("disp-1", events_dup, decision_id="dec-1")
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT record_json FROM events WHERE event_id='ev-c'").fetchone()
            self.assertEqual(json.loads(row[0])["data"]["evidence_sha256"],
                             hashlib.sha256(b"a").hexdigest())

    def test_event_id_reused_across_dispatches_must_fail(self):
        self.store.decision({
            "decision_id": "dec-2", "project": PROJECT, "task_sha256": TASK_SHA,
            "policy_revision": POLICY, "config_sha256": CONFIG_SHA,
            "selected": dict(SELECTED), "judgment_source": "caller",
            "review_required": True, "quality_floor": 3, "task_tier": "high_stakes",
            "at": 2000.0,
        })
        self.store.launch({
            "dispatch_id": "disp-2", "decision_id": "dec-2", "project": PROJECT,
            "task_sha256": TASK_SHA, "actual": dict(ACTUAL),
            "override_reason": None, "worktree": None,
            "native_session_id": None, "account_status": "unverified",
            "at": 2001.0,
        })
        self.closeout("disp-1", [
            {"event_id": "shared", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "shared", b"a")},
        ], decision_id="dec-1")
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout("disp-2", [
                {"event_id": "shared", "kind": "completed",
                 "data": {"evidence_sha256": hashlib.sha256(b"a").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "shared-2", b"a")},
            ], decision_id="dec-2")
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT dispatch_id FROM events WHERE event_id='shared'").fetchall()
            self.assertEqual([r[0] for r in rows], ["disp-1"])


class DifferingCwd(_ReviewerCase):
    """Reporting from a different cwd must still bind to the decision project."""

    def test_record_launch_inherits_decision_project_when_cwd_differs(self):
        cwd0 = os.getcwd()
        try:
            os.chdir("/tmp")
            store = outcome_store.OutcomeStore(self.path)
            from unittest.mock import patch
            with patch.object(r, "STATE", self.state), \
                 patch.object(outcome_cli, "store", lambda *_: store):
                r.record_launch(json.loads(r.CONFIG.read_text()),
                                "minimax", "MiniMax-M3", TASK,
                                "disp-cwd-1", None, "medium",
                                "dec-1", None, None)
        finally:
            os.chdir(cwd0)
        launch = self.store.get_launch("disp-cwd-1")
        self.assertEqual(launch["project"], PROJECT)

    def test_explicit_project_mismatch_fails_before_write(self):
        store = outcome_store.OutcomeStore(self.path)
        from unittest.mock import patch
        with self.assertRaises(outcome_store.OutcomeError):
            with patch.object(r, "STATE", self.state), \
                 patch.object(outcome_cli, "store", lambda *_: store):
                r.record_launch(json.loads(r.CONFIG.read_text()),
                                "minimax", "MiniMax-M3", TASK,
                                "disp-mismatch", None, "medium",
                                "dec-1", None, None, project="/other")
        self.assertIsNone(self.store.get_launch("disp-mismatch"))

    def test_explicit_project_matching_decision_succeeds(self):
        store = outcome_store.OutcomeStore(self.path)
        from unittest.mock import patch
        with patch.object(r, "STATE", self.state), \
             patch.object(outcome_cli, "store", lambda *_: store):
            r.record_launch(json.loads(r.CONFIG.read_text()),
                            "minimax", "MiniMax-M3", TASK,
                            "disp-explicit", None, "medium",
                            "dec-1", None, None, project=PROJECT)
        self.assertEqual(self.store.get_launch("disp-explicit")["project"],
                         PROJECT)

    def test_actual_effort_never_defaults_from_selected_effort(self):
        """Omitting actual effort when the decision selected one is a launch
        mismatch, not silent defaulting. ``record_launch`` preserves the
        operator-supplied effort (None if not passed); the launch validator
        rejects the diff against ``decision.selected.effort`` and requires
        ``override_reason`` if the operator knew the difference was
        deliberate. This pins the contract that the actual effort recorded
        is the operator's, never a fabrication from the selection."""
        store = outcome_store.OutcomeStore(self.path)
        from unittest.mock import patch
        with self.assertRaises(outcome_store.OutcomeError):
            with patch.object(r, "STATE", self.state), \
                 patch.object(outcome_cli, "store", lambda *_: store):
                r.record_launch(json.loads(r.CONFIG.read_text()),
                                "minimax", "MiniMax-M3", TASK,
                                "disp-no-effort", None, None,
                                "dec-1", None, None)
        self.assertIsNone(self.store.get_launch("disp-no-effort"))
        # With override_reason the same diff is accepted; actual stays None
        # and is not silently rewritten to the selected "medium".
        with patch.object(r, "STATE", self.state), \
             patch.object(outcome_cli, "store", lambda *_: store):
            r.record_launch(json.loads(r.CONFIG.read_text()),
                            "minimax", "MiniMax-M3", TASK,
                            "disp-no-effort-override", None, None,
                            "dec-1",
                            "operator intentionally left effort unspecified",
                            None)
        stored = self.store.get_launch("disp-no-effort-override")
        self.assertIsNone(stored["actual"]["effort"])
        self.assertEqual(stored["override_reason"],
                         "operator intentionally left effort unspecified")


class ReviewQualification(_ReviewerCase):
    """Reviewer validation lives in outcome_cli.reviewer_validator (CLI layer).

    The store-level closeout only enforces the same-family rule via the
    accepted gate; the CLI must reject invented profiles, under-floor
    reviewers and same-family reviewers before any write. These tests pin
    the dedicated rejection reason via stderr rather than merely a nonzero
    exit code, and co-locate the manifest under the evidence directory so
    the file lookups actually reach the bytes the SHA references."""

    def _write_manifest(self, body, name="manifest.json"):
        # Co-locate manifest with the evidence blobs. The closeout CLI
        # resolves evidence_path relative to the manifest's parent
        # directory; a manifest under tmp/ would resolve c.bin to tmp/c.bin
        # and fail the evidence lookup before the reviewer check ever runs,
        # making the test a false positive against the intended rule.
        evidence_dir = self.tmp / "evidence"
        evidence_dir.mkdir(exist_ok=True)
        path = evidence_dir / name
        path.write_text(json.dumps(body))
        return path

    def _run_cli(self, manifest_path):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with patch.object(r, "STATE", self.state), \
                 patch.object(outcome_cli, "store", lambda *_: self.store):
                code = r.main(["outcome", "closeout",
                               "--manifest", str(manifest_path)])
        return code, out.getvalue(), err.getvalue()

    def test_direct_minimax_does_not_qualify_for_opencode_minimax_author(self):
        """Reviewer qualification pins the per-event rejection to a
        dedicated reason. ``minimax:MiniMax-M3`` and ``opencode:minimax-m3``
        resolve to the same configured profile family, so the per-event
        validator surfaces the floor/capability rejection rather than the
        family mismatch one. The same-family acceptance gate is exercised
        separately in
        ``test_same_family_review_blocks_required_acceptance_only``."""
        self.store.decision({
            "decision_id": "dec-minimax-go", "project": PROJECT,
            "task_sha256": TASK_SHA, "policy_revision": POLICY,
            "config_sha256": CONFIG_SHA,
            "selected": {"provider": "opencode", "model": "minimax-m3",
                         "effort": "medium", "family": "minimax-m3"},
            "judgment_source": "caller", "review_required": True,
            "quality_floor": 3, "task_tier": "high_stakes", "at": 1000.0,
        })
        self.store.launch({
            "dispatch_id": "disp-minimax-go", "decision_id": "dec-minimax-go",
            "project": PROJECT, "task_sha256": TASK_SHA,
            "actual": {"provider": "opencode", "model": "minimax-m3",
                       "effort": "medium", "family": "minimax-m3"},
            "override_reason": None, "worktree": None,
            "native_session_id": None, "account_status": "unverified",
            "at": 1001.0,
        })
        body = {
            "schema_version": 1, "dispatch": "disp-minimax-go",
            "decision_id": "dec-minimax-go", "project": PROJECT,
            "events": [
                {"event_id": "ev-c", "kind": "completed",
                 "data": {"evidence_sha256": hashlib.sha256(b"c").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "c", b"c")},
                {"event_id": "ev-r", "kind": "review",
                 "data": {"actor": "r", "provider": "minimax",
                          "model": "MiniMax-M3", "family": "minimax-m3",
                          "verdict": "accepted",
                          "evidence_sha256": hashlib.sha256(b"r").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "r", b"r")},
                {"event_id": "ev-a", "kind": "accepted",
                 "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"a").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "a", b"a")},
            ],
        }
        code, _, err = self._run_cli(self._write_manifest(body))
        self.assertNotEqual(code, 0)
        self.assertIn("quality floor", err)
        self.assertEqual(self.count_events("disp-minimax-go"), 0)

    def test_invented_reviewer_family_rejected_at_parse_time(self):
        """An invented ``family`` string must surface as the dedicated
        profile-mismatch reason in stderr. A passing test that asserted
        only ``code != 0`` would be silent against any other validation
        failure (oversize manifest, missing file, decision id mismatch);
        the dedicated substring keeps the pin honest."""
        body = {
            "schema_version": 1, "dispatch": "disp-1",
            "decision_id": "dec-1", "project": PROJECT,
            "events": [
                {"event_id": "ev-c", "kind": "completed",
                 "data": {"evidence_sha256": hashlib.sha256(b"c").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "c2", b"c")},
                {"event_id": "ev-r", "kind": "review",
                 "data": {"actor": "r", "provider": "codex",
                          "model": "gpt-6-astra",
                          "family": "invented-independent-family",
                          "verdict": "accepted",
                          "evidence_sha256": hashlib.sha256(b"r").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "r2", b"r")},
                {"event_id": "ev-a", "kind": "accepted",
                 "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"a").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "a2", b"a")},
            ],
        }
        code, _, err = self._run_cli(self._write_manifest(body))
        self.assertNotEqual(code, 0)
        self.assertIn("configured model profile", err)
        self.assertEqual(self.count_events(), 0)

    def test_underfloor_reviewer_rejected_via_cli(self):
        """``codex:gpt-5.6-luna`` has max_band=1; the recorded task floor
        is 3 so the reviewer fails the per-event quality-floor check.
        Asserting the dedicated floor/capability substring keeps the
        failure pinned to the reviewer rule, not a generic file lookup."""
        body = {
            "schema_version": 1, "dispatch": "disp-1",
            "decision_id": "dec-1", "project": PROJECT,
            "events": [
                {"event_id": "ev-c", "kind": "completed",
                 "data": {"evidence_sha256": hashlib.sha256(b"c").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "c3", b"c")},
                {"event_id": "ev-r", "kind": "review",
                 "data": {"actor": "r", "provider": "codex",
                          "model": "gpt-5.6-luna",
                          "family": "gpt-5.6-luna",
                          "verdict": "accepted",
                          "evidence_sha256": hashlib.sha256(b"r").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "r3", b"r")},
                {"event_id": "ev-a", "kind": "accepted",
                 "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"a").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "a3", b"a")},
            ],
        }
        code, _, err = self._run_cli(self._write_manifest(body))
        self.assertNotEqual(code, 0)
        self.assertIn("quality floor", err)
        self.assertEqual(self.count_events(), 0)

    def test_same_family_review_stored_when_review_not_required(self):
        """Same-family only blocks required ACCEPTANCE. Optional same-family
        review may be stored if it passes per-event validation
        (configured profile, family match, quality floor, capability gate).
        The store keeps it for traceability; the accepted-gate refuses
        acceptance when ``review_required`` is true. This pins that
        distinction: storage is permissive, acceptance is the gate."""
        self.store.decision({
            "decision_id": "dec-optional", "project": PROJECT,
            "task_sha256": TASK_SHA, "policy_revision": POLICY,
            "config_sha256": CONFIG_SHA,
            "selected": {"provider": "codex", "model": "gpt-6-astra",
                         "effort": "high", "family": "gpt-6-astra"},
            "judgment_source": "caller", "review_required": False,
            "quality_floor": 3, "task_tier": "implementation", "at": 1500.0,
        })
        self.store.launch({
            "dispatch_id": "disp-optional", "decision_id": "dec-optional",
            "project": PROJECT, "task_sha256": TASK_SHA,
            "actual": {"provider": "codex", "model": "gpt-6-astra",
                       "effort": "high", "family": "gpt-6-astra"},
            "override_reason": None, "worktree": None,
            "native_session_id": None, "account_status": "unverified",
            "at": 1501.0,
        })
        events = [
            {"event_id": "ev-c", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"c").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-c-opt", b"c")},
            {"event_id": "ev-r", "kind": "review",
             "data": {"actor": "r", "provider": "codex",
                      "model": "gpt-6-astra", "family": "gpt-6-astra",
                      "verdict": "accepted",
                      "evidence_sha256": hashlib.sha256(b"r").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-r-opt", b"r")},
            {"event_id": "ev-a", "kind": "accepted",
             "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-a-opt", b"a")},
        ]
        # Same-family review is stored because review_required=False;
        # _review_valid accepts it. The store does not gate storage.
        result = self.closeout("disp-optional", events, decision_id="dec-optional")
        self.assertEqual([r["event_id"] for r in result["applied"]],
                         ["ev-c", "ev-r", "ev-a"])

    def test_same_family_review_blocks_required_acceptance_only(self):
        """Inverse pin: when ``review_required=True`` and the only review is
        same-family, the per-event validator stores the review (the CLI's
        profile/family/floor checks all pass because the model is
        configured and at floor) but the accepted-gate refuses the
        accepted event. Nothing here is a CLI-level rejection."""
        events = [
            {"event_id": "ev-c", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"c").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-c-sf", b"c")},
            {"event_id": "ev-r", "kind": "review",
             "data": {"actor": "r", "provider": "minimax",
                      "model": "MiniMax-M3", "family": "minimax-m3",
                      "verdict": "accepted",
                      "evidence_sha256": hashlib.sha256(b"r").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-r-sf", b"r")},
            {"event_id": "ev-a", "kind": "accepted",
             "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-a-sf", b"a")},
        ]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout("disp-1", events, decision_id="dec-1")
        self.assertEqual(self.count_events(), 0)


class PostRepairFreshness(_ReviewerCase):
    """A fresh review after rework is required; the original review must not
    satisfy acceptance once a rework is recorded."""

    def test_review_then_rework_blocks_acceptance(self):
        events = [
            {"event_id": "ev-c", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"c").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-c-pr", b"c")},
            {"event_id": "ev-r", "kind": "review",
             "data": {"actor": "r", "provider": "codex", "model": "gpt-6-astra",
                      "family": "gpt-6-astra", "verdict": "accepted",
                      "evidence_sha256": hashlib.sha256(b"r").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-r-pr", b"r")},
            {"event_id": "ev-rework", "kind": "rework",
             "data": {"actor": "alice", "seconds": 5,
                      "evidence_sha256": hashlib.sha256(b"rw").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-rework-pr", b"rw")},
            {"event_id": "ev-a", "kind": "accepted",
             "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-a-pr", b"a")},
        ]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout("disp-1", events, decision_id="dec-1")
        self.assertEqual(self.count_events(), 0)

    def test_rework_then_fresh_review_allows_acceptance(self):
        events = [
            {"event_id": "ev-c", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"c").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-c-r", b"c")},
            {"event_id": "ev-rework", "kind": "rework",
             "data": {"actor": "alice", "seconds": 5,
                      "evidence_sha256": hashlib.sha256(b"rw").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-rework-r", b"rw")},
            {"event_id": "ev-r", "kind": "review",
             "data": {"actor": "r", "provider": "codex", "model": "gpt-6-astra",
                      "family": "gpt-6-astra", "verdict": "accepted",
                      "evidence_sha256": hashlib.sha256(b"r").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-r-r", b"r")},
            {"event_id": "ev-a", "kind": "accepted",
             "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-a-r", b"a")},
        ]
        result = self.closeout("disp-1", events, decision_id="dec-1")
        self.assertEqual([r["event_id"] for r in result["applied"]],
                         ["ev-c", "ev-rework", "ev-r", "ev-a"])


class PostAcceptImmutability(_ReviewerCase):
    """Post-accept immutability must be enforced by the closeout path."""

    def _accepted(self):
        return [
            {"event_id": "ev-c", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"c").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-c-im", b"c")},
            {"event_id": "ev-r", "kind": "review",
             "data": {"actor": "r", "provider": "codex", "model": "gpt-6-astra",
                      "family": "gpt-6-astra", "verdict": "accepted",
                      "evidence_sha256": hashlib.sha256(b"r").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-r-im", b"r")},
            {"event_id": "ev-a", "kind": "accepted",
             "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-a-im", b"a")},
        ]

    def test_rework_after_accept_rejected(self):
        self.closeout("disp-1", self._accepted(), decision_id="dec-1")
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout("disp-1", [
                {"event_id": "ev-rw", "kind": "rework",
                 "data": {"actor": "alice", "seconds": 1,
                          "evidence_sha256": hashlib.sha256(b"x").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "ev-rw-im", b"x")},
            ], decision_id="dec-1")
        self.assertEqual(self.count_events(), 3)

    def test_usage_after_accept_rejected(self):
        self.closeout("disp-1", self._accepted(), decision_id="dec-1")
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout("disp-1", [
                {"event_id": "ev-u", "kind": "usage",
                 "data": {"source": "native", "input_tokens": 1}},
            ], decision_id="dec-1")
        self.assertEqual(self.count_events(), 3)

    def test_second_accepted_rejected(self):
        self.closeout("disp-1", self._accepted(), decision_id="dec-1")
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout("disp-1", [
                {"event_id": "ev-a2", "kind": "accepted",
                 "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"y").hexdigest()},
                 "evidence_path": _write_evidence_blob(self._tmp.name, "ev-a2-im", b"y")},
            ], decision_id="dec-1")
        self.assertEqual(self.count_events(), 3)


class RealCLI(_ReviewerCase):
    """The CLI must reject malformed inputs and parse valid manifests."""

    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        self._patches = [
            patch.object(r, "STATE", self.state),
            patch.object(outcome_cli, "store", lambda *_: self.store),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = r.main(args)
        return code, out.getvalue(), err.getvalue()

    def write_manifest(self, body):
        # Co-locate with evidence so evidence_path resolves correctly.
        path = self.tmp / "evidence" / "manifest.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(body))
        return path

    def test_schema_version_must_be_integer_one(self):
        for bad in ("foo", 2, 1.5, None, [1]):
            body = {"schema_version": bad, "dispatch": "disp-1",
                    "decision_id": "dec-1", "project": PROJECT, "events": []}
            path = self.write_manifest(body)
            code, _, _ = self.invoke(["outcome", "closeout",
                                      "--manifest", str(path)])
            self.assertEqual(code, 2,
                             f"schema_version={bad!r} should be rejected")

    def test_unknown_manifest_keys_rejected(self):
        body = {"schema_version": 1, "dispatch": "disp-1",
                "decision_id": "dec-1", "project": PROJECT,
                "invented_envelope_key": "foo",
                "events": [{"event_id": "ev", "kind": "completed",
                            "data": {"evidence_sha256": "f" * 64},
                            "evidence_path": "x.bin"}]}
        path = self.write_manifest(body)
        (path.parent / "x.bin").write_bytes(b"x" * 32)
        code, _, _ = self.invoke(["outcome", "closeout",
                                  "--manifest", str(path)])
        self.assertEqual(code, 2,
                         "unknown manifest envelope keys must be rejected")

    def test_oversize_evidence_file_rejected(self):
        blob = b"x" * (outcome_cli.CLOSEOUT_EVIDENCE_MAX_BYTES + 1)
        evidence = hashlib.sha256(blob).hexdigest()
        body = {"schema_version": 1, "dispatch": "disp-1",
                "decision_id": "dec-1", "project": PROJECT,
                "events": [{"event_id": "ev", "kind": "completed",
                            "data": {"evidence_sha256": evidence},
                            "evidence_path": "huge.bin"}]}
        path = self.write_manifest(body)
        (path.parent / "huge.bin").write_bytes(blob)
        code, _, _ = self.invoke(["outcome", "closeout",
                                  "--manifest", str(path)])
        self.assertEqual(code, 2)

    def test_real_evidence_sha_match_succeeds(self):
        blob = b"verified artifact"
        evidence = hashlib.sha256(blob).hexdigest()
        body = {"schema_version": 1, "dispatch": "disp-1",
                "decision_id": "dec-1", "project": PROJECT,
                "events": [{"event_id": "ev", "kind": "completed",
                            "data": {"evidence_sha256": evidence},
                            "evidence_path": "completion.bin"}]}
        path = self.write_manifest(body)
        (path.parent / "completion.bin").write_bytes(blob)
        code, _, err = self.invoke(["outcome", "closeout",
                                    "--manifest", str(path)])
        self.assertEqual(code, 0, err)

    def test_real_evidence_sha_mismatch_rejected(self):
        body = {"schema_version": 1, "dispatch": "disp-1",
                "decision_id": "dec-1", "project": PROJECT,
                "events": [{"event_id": "ev", "kind": "completed",
                            "data": {"evidence_sha256": "f" * 64},
                            "evidence_path": "completion.bin"}]}
        path = self.write_manifest(body)
        (path.parent / "completion.bin").write_bytes(b"actual content")
        code, _, _ = self.invoke(["outcome", "closeout",
                                  "--manifest", str(path)])
        self.assertEqual(code, 2)

    def test_root_flag_does_not_exist(self):
        """Documented contract: ``closeout --manifest FILE only``.

        The steering brief for #29 explicitly states "no --root"; the
        evidence root is fixed to the manifest's directory. The current
        docs (docs/CONSUMER-WORKFLOW.md) describe a ``--root`` flag that
        was never wired up; that is a docs defect, not an implementation
        gap. Asserting that argparse rejects the flag pins the contract
        so a future addition of ``--root`` is a deliberate decision."""
        blob = b"verified artifact under root"
        evidence = hashlib.sha256(blob).hexdigest()
        root = self.tmp / "my-evidence"
        root.mkdir()
        (root / "completion.bin").write_bytes(blob)
        body = {"schema_version": 1, "dispatch": "disp-1",
                "decision_id": "dec-1", "project": PROJECT,
                "events": [{"event_id": "ev", "kind": "completed",
                            "data": {"evidence_sha256": evidence},
                            "evidence_path": "completion.bin"}]}
        path = self.write_manifest(body)
        with self.assertRaises(SystemExit):
            self.invoke(["outcome", "closeout",
                         "--manifest", str(path),
                         "--root", str(root)])


class SymlinkEvidence(_ReviewerCase):
    """A symlink under evidence_path must not bypass the boundary.

    Existing leaf-only coverage proves the ``lstat`` on the final path
    catches a symlink at the leaf; this class adds a parent-directory
    symlink whose target lies outside the evidence root. The store walks
    every path component and lstats each one without resolving, so a
    symlink at any depth is rejected and no bytes are read."""

    def _setup_real_file(self, name, blob=b"x"):
        target = Path(self._tmp.name) / name
        target.write_bytes(blob)
        return target, hashlib.sha256(blob).hexdigest()

    def test_symlink_in_evidence_dir_rejected(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        root = Path(self._tmp.name) / "evidence"
        root.mkdir()
        target = Path(self._tmp.name) / "outside.bin"
        target.write_bytes(b"x")
        link = root / "link.bin"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unsupported here")
        evidence = hashlib.sha256(b"x").hexdigest()
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(
                "disp-1",
                [{"event_id": "ev-c", "kind": "completed",
                  "data": {"evidence_sha256": evidence},
                  "evidence_path": "link.bin"}],
                decision_id="dec-1")
        self.assertEqual(self.count_events(), 0)

    def test_symlink_in_parent_dir_rejected_with_real_bytes(self):
        """``root/link/file`` where ``link`` is a symlink that resolves
        outside the evidence root. The real target file exists with bytes
        the SHA references, so the rejection is unambiguously the
        symlink guard and not a missing-file or hash-mismatch artefact.
        No event is written."""
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        root = Path(self._tmp.name) / "evidence"
        root.mkdir()
        outside = Path(self._tmp.name) / "real.bin"
        outside.write_bytes(b"outside-bytes")
        evidence = hashlib.sha256(b"outside-bytes").hexdigest()
        link_dir = root / "link"
        try:
            link_dir.symlink_to(Path(self._tmp.name))
        except OSError:
            self.skipTest("symlinks unsupported here")
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(
                "disp-1",
                [{"event_id": "ev-c", "kind": "completed",
                  "data": {"evidence_sha256": evidence},
                  "evidence_path": "link/real.bin"}],
                decision_id="dec-1")
        self.assertEqual(self.count_events(), 0)


class AuditCoverage(_ReviewerCase):
    """Audit must report known/unknown splits; missing categories stay unknown."""

    def test_missing_usage_means_unknown(self):
        events = [
            {"event_id": "ev-c", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"c").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-c-au", b"c")},
            {"event_id": "ev-r", "kind": "review",
             "data": {"actor": "r", "provider": "codex", "model": "gpt-6-astra",
                      "family": "gpt-6-astra", "verdict": "accepted",
                      "evidence_sha256": hashlib.sha256(b"r").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-r-au", b"r")},
            {"event_id": "ev-a", "kind": "accepted",
             "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-a-au", b"a")},
        ]
        self.closeout("disp-1", events, decision_id="dec-1")
        audit = self.store.audit()
        attempt = next(a for a in audit["attempts"]
                       if a["launch"]["dispatch_id"] == "disp-1")
        self.assertIn("missing_usage", attempt["closeout_gaps"])
        self.assertIn("missing_rework_evidence", attempt["closeout_gaps"])
        self.assertEqual(audit["metrics"]["known_usage_launches"], 0)
        self.assertEqual(audit["metrics"]["unknown_rework_launches"], 1)

    def test_full_acceptance_has_no_closeout_gaps(self):
        events = [
            {"event_id": "ev-c", "kind": "completed",
             "data": {"evidence_sha256": hashlib.sha256(b"c").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-c-fu", b"c")},
            {"event_id": "ev-rw", "kind": "rework",
             "data": {"actor": "alice", "seconds": 12,
                      "evidence_sha256": hashlib.sha256(b"rw").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-rw-fu", b"rw")},
            {"event_id": "ev-r", "kind": "review",
             "data": {"actor": "r", "provider": "codex", "model": "gpt-6-astra",
                      "family": "gpt-6-astra", "verdict": "accepted",
                      "evidence_sha256": hashlib.sha256(b"r").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-r-fu", b"r")},
            {"event_id": "ev-u", "kind": "usage",
             "data": {"source": "native", "input_tokens": 100}},
            {"event_id": "ev-a", "kind": "accepted",
             "data": {"actor": "c", "evidence_sha256": hashlib.sha256(b"a").hexdigest()},
             "evidence_path": _write_evidence_blob(self._tmp.name, "ev-a-fu", b"a")},
        ]
        self.closeout("disp-1", events, decision_id="dec-1")
        audit = self.store.audit()
        attempt = next(a for a in audit["attempts"]
                       if a["launch"]["dispatch_id"] == "disp-1")
        self.assertEqual(attempt["closeout_gaps"], [])
        self.assertTrue(attempt["accepted"])


if __name__ == "__main__":
    unittest.main()
