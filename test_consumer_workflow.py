"""Coordinator closeout workflow: bounded manifest, file evidence, atomic idempotency."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import outcome_cli
import outcome_store
import rightsize as r


PROJECT = "/coordinator/project"
TASK_SHA = "a" * 64
CONFIG_SHA = "b" * 64
POLICY = "d" * 64
EVIDENCE = "c" * 64
SELECTED = {"provider": "minimax", "model": "MiniMax-M3",
            "effort": "medium", "family": "minimax-m3"}
ACTUAL = {"provider": "minimax", "model": "MiniMax-M3",
          "effort": "medium", "family": "minimax-m3"}
REVIEWER_MODEL = "gpt-5.6-luna"
REVIEWER_KEY = f"codex:{REVIEWER_MODEL}"
REVIEWER_FAMILY = "gpt-5.6-luna"
CONFIG = {"model_profiles": {REVIEWER_KEY: {
    "family": REVIEWER_FAMILY, "max_band": 3, "capabilities": ["code", "review"],
    "efforts": ["low", "medium", "high", "xhigh", "max"],
    "effort_by_task": {}, "evidence": "test fixture"}},
    "task_profiles": {"high_stakes": {"minimum_band": 3,
                                     "requires": ["code", "review"]}}}


_HASH_KINDS = frozenset({"completed", "failed", "cancelled",
                         "review", "rework", "accepted"})


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.evidence_root = self.tmp / "evidence"
        self.evidence_root.mkdir()
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
            "native_session_id": None, "account_status": "unverified", "at": 1001.0,
        })

    def write_blob(self, name, blob):
        path = self.evidence_root / name
        path.write_bytes(blob)
        return name

    def event(self, kind, data, event_id=None, evidence_path=None):
        eid = event_id or f"ev-{kind}-{id(self)}-{data.get('actor', '')}"
        spec = {"event_id": eid, "kind": kind, "data": data}
        if evidence_path is not None:
            spec["evidence_path"] = evidence_path
        elif kind in _HASH_KINDS:
            blob = data.get("evidence_sha256")
            if isinstance(blob, str):
                (self.evidence_root / f"{eid}.bin").write_text(blob)
                spec["evidence_path"] = f"{eid}.bin"
        return spec

    def closeout(self, events, **overrides):
        kwargs = {"decision_id": "dec-1", "project": PROJECT,
                  "evidence_root": self.evidence_root}
        if "review_validator" not in overrides:
            kwargs["review_validator"] = outcome_cli.reviewer_validator(CONFIG, r)
        kwargs.update(overrides)
        return self.store.closeout("disp-1", events, **kwargs)

    def manifest(self, **overrides):
        events = overrides.pop("events", None)
        body = {
            "schema_version": 1, "dispatch": "disp-1",
            "decision_id": "dec-1", "project": PROJECT,
        }
        body.update(overrides)
        if events is not None:
            body["events"] = events
        return body


class CloseoutHappyPath(_StoreCase):
    def test_completion_then_review_then_acceptance_in_one_batch(self):
        evidence = hashlib.sha256(b"completion artifact").hexdigest()
        review_evidence = hashlib.sha256(b"review notes").hexdigest()
        accept_evidence = hashlib.sha256(b"accept notes").hexdigest()
        rework_evidence = hashlib.sha256(b"rework notes").hexdigest()
        manifest = self.manifest(events=[
            self.event("completed", {"evidence_sha256": evidence},
                       "ev-completed",
                       evidence_path=self.write_blob("completion.bin", b"completion artifact")),
            self.event("rework", {"actor": "alice", "seconds": 12.0,
                                  "evidence_sha256": rework_evidence},
                       "ev-rework",
                       evidence_path=self.write_blob("rework.bin", b"rework notes")),
            self.event("review", {"actor": "reviewer", "provider": "codex",
                                  "model": "gpt-5.6-luna", "family": "gpt-5.6-luna",
                                  "verdict": "accepted",
                                  "evidence_sha256": review_evidence},
                       "ev-review",
                       evidence_path=self.write_blob("review.bin", b"review notes")),
            self.event("usage", {"source": "native", "input_tokens": 100,
                                 "output_tokens": 50}, "ev-usage"),
            self.event("accepted", {"actor": "coordinator",
                                    "evidence_sha256": accept_evidence},
                       "ev-accept",
                       evidence_path=self.write_blob("accept.bin", b"accept notes")),
        ])
        result = self.closeout(manifest["events"])
        self.assertEqual([r["event_id"] for r in result["applied"]],
                         ["ev-completed", "ev-rework", "ev-review",
                          "ev-usage", "ev-accept"])
        audit = self.store.audit()
        attempt = next(a for a in audit["attempts"] if a["launch"]["dispatch_id"] == "disp-1")
        self.assertTrue(attempt["accepted"])
        self.assertEqual(attempt["closeout_gaps"], [])

    def test_full_audit_closeout_gaps_reported(self):
        audit = self.store.audit()
        attempt = next(a for a in audit["attempts"] if a["launch"]["dispatch_id"] == "disp-1")
        self.assertIn("missing_completion", attempt["closeout_gaps"])
        self.assertIn("missing_acceptance", attempt["closeout_gaps"])
        self.assertIn("missing_review", attempt["closeout_gaps"])
        self.assertIn("missing_usage", attempt["closeout_gaps"])
        self.assertIn("missing_rework_evidence", attempt["closeout_gaps"])
        self.assertEqual(audit["metrics"]["closeout_gaps_total"], len(attempt["closeout_gaps"]))


class CloseoutIdempotency(_StoreCase):
    def test_identical_retry_writes_no_duplicates(self):
        evidence = hashlib.sha256(b"artifact").hexdigest()
        events = [
            self.event("completed", {"evidence_sha256": evidence}, "ev-completed",
                       evidence_path=self.write_blob("completion.bin", b"artifact")),
            self.event("review", {"actor": "r", "provider": "codex",
                                  "model": "gpt-5.6-luna", "family": "gpt-5.6-luna",
                                  "verdict": "accepted",
                                  "evidence_sha256": evidence}, "ev-review",
                       evidence_path=self.write_blob("review.bin", b"artifact")),
            self.event("accepted", {"actor": "c",
                                    "evidence_sha256": evidence}, "ev-accept",
                       evidence_path=self.write_blob("accept.bin", b"artifact")),
        ]
        first = self.closeout(events)
        second = self.closeout(events)
        self.assertEqual([r["event_id"] for r in first["applied"]],
                         [r["event_id"] for r in second["applied"]])
        with closing_sqlite(self.path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM events WHERE dispatch_id='disp-1'").fetchone()[0], 3)

    def test_conflicting_retry_refused_without_overwrite(self):
        evidence = hashlib.sha256(b"artifact").hexdigest()
        self.closeout([
            self.event("completed", {"evidence_sha256": evidence}, "ev-completed",
                       evidence_path=self.write_blob("completion.bin", b"artifact")),
            self.event("review", {"actor": "r", "provider": "codex",
                                  "model": "gpt-5.6-luna", "family": "gpt-5.6-luna",
                                  "verdict": "accepted",
                                  "evidence_sha256": evidence}, "ev-review",
                       evidence_path=self.write_blob("review.bin", b"artifact")),
            self.event("accepted", {"actor": "c",
                                    "evidence_sha256": evidence}, "ev-accept",
                       evidence_path=self.write_blob("accept.bin", b"artifact")),
        ])
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout([
                self.event("completed", {"evidence_sha256": evidence}, "ev-completed",
                           evidence_path=self.write_blob("completion.bin", b"artifact")),
                self.event("review", {"actor": "different", "provider": "codex",
                                      "model": "gpt-5.6-luna", "family": "gpt-5.6-luna",
                                      "verdict": "accepted",
                                      "evidence_sha256": evidence}, "ev-review",
                           evidence_path=self.write_blob("review.bin", b"artifact")),
                self.event("accepted", {"actor": "c",
                                        "evidence_sha256": evidence}, "ev-accept",
                           evidence_path=self.write_blob("accept.bin", b"artifact")),
            ])
        with closing_sqlite(self.path) as db:
            row = db.execute(
                "SELECT record_json FROM events WHERE event_id='ev-review'").fetchone()
            self.assertEqual(json.loads(row[0])["data"]["actor"], "r")


class CloseoutAtomicRollback(_StoreCase):
    def test_invalid_final_review_rolls_back_completion_and_review(self):
        evidence = hashlib.sha256(b"artifact").hexdigest()
        events = [
            self.event("completed", {"evidence_sha256": evidence}, "ev-completed",
                       evidence_path=self.write_blob("completion.bin", b"artifact")),
            self.event("review", {"actor": "reviewer", "provider": "minimax",
                                  "model": "MiniMax-M3", "family": "minimax-m3",
                                  "verdict": "accepted",
                                  "evidence_sha256": evidence}, "ev-self-review",
                       evidence_path=self.write_blob("self_review.bin", b"artifact")),
            self.event("accepted", {"actor": "coordinator",
                                    "evidence_sha256": evidence}, "ev-accept",
                       evidence_path=self.write_blob("accept.bin", b"artifact")),
        ]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(events)
        with closing_sqlite(self.path) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM events WHERE dispatch_id='disp-1'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_unqualified_reviewer_blocks_whole_batch(self):
        evidence = hashlib.sha256(b"artifact").hexdigest()
        events = [
            self.event("completed", {"evidence_sha256": evidence}, "ev-completed",
                       evidence_path=self.write_blob("completion.bin", b"artifact")),
            self.event("rework", {"actor": "alice", "seconds": 5.0,
                                  "evidence_sha256": evidence}, "ev-rework",
                       evidence_path=self.write_blob("rework.bin", b"artifact")),
            self.event("review", {"actor": "reviewer", "provider": "codex",
                                  "model": "haiku", "family": "haiku",
                                  "verdict": "accepted",
                                  "evidence_sha256": evidence}, "ev-bad-review",
                       evidence_path=self.write_blob("bad_review.bin", b"artifact")),
            self.event("accepted", {"actor": "coordinator",
                                    "evidence_sha256": evidence}, "ev-accept",
                       evidence_path=self.write_blob("accept.bin", b"artifact")),
        ]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(events)
        with closing_sqlite(self.path) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM events WHERE dispatch_id='disp-1'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_unrelated_existing_event_preserved(self):
        evidence = hashlib.sha256(b"artifact").hexdigest()
        self.store.event("disp-1", "ev-pre", "completed",
                         {"evidence_sha256": evidence}, at=1100.0)
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout([
                self.event("review", {"actor": "r", "provider": "minimax",
                                      "model": "MiniMax-M3", "family": "minimax-m3",
                                      "verdict": "accepted",
                                      "evidence_sha256": evidence}, "ev-self-review",
                       evidence_path=self.write_blob("self_review.bin", b"artifact")),
            ])
        with closing_sqlite(self.path) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM events WHERE dispatch_id='disp-1'").fetchone()[0]
        self.assertEqual(count, 1)


class CloseoutFileEvidence(_StoreCase):
    def test_evidence_path_sha256_verified_against_file(self):
        blob = b"verified completion artifact"
        path = self.write_blob("completion.bin", blob)
        evidence = hashlib.sha256(blob).hexdigest()
        events = [self.event("completed", {"evidence_sha256": evidence}, "ev-completed",
                             evidence_path=path)]
        self.closeout(events)

    def test_evidence_path_sha_mismatch_refused(self):
        path = self.write_blob("x.bin", b"actual content")
        events = [self.event("completed",
                             {"evidence_sha256": hashlib.sha256(b"different").hexdigest()},
                             "ev-completed", evidence_path=path)]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(events)
        with closing_sqlite(self.path) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM events WHERE event_id='ev-completed'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_evidence_path_missing_refused(self):
        events = [self.event("completed", {"evidence_sha256": "c" * 64},
                             "ev-completed", evidence_path="nope.bin")]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(events)

    def test_evidence_path_traversal_refused(self):
        events = [self.event("completed", {"evidence_sha256": "c" * 64},
                             "ev-completed", evidence_path="../escape.bin")]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(events)

    def test_evidence_path_symlink_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        target = self.tmp / "real.bin"
        target.write_bytes(b"x")
        link = self.evidence_root / "link.bin"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unsupported here")
        events = [self.event("completed", {"evidence_sha256": hashlib.sha256(b"x").hexdigest()},
                             "ev-completed", evidence_path="link.bin")]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(events)

    def test_evidence_path_directory_refused(self):
        events = [self.event("completed", {"evidence_sha256": "c" * 64},
                             "ev-completed", evidence_path=".")]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(events)


class CloseoutEnvelopeValidation(_StoreCase):
    def test_duplicate_event_id_refused(self):
        evidence = hashlib.sha256(b"x").hexdigest()
        events = [
            self.event("completed", {"evidence_sha256": evidence}, "dup",
                       evidence_path=self.write_blob("dup.bin", b"x")),
            self.event("usage", {"source": "s"}, "dup"),
        ]
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(events)

    def test_decision_id_mismatch_refused(self):
        evidence = hashlib.sha256(b"x").hexdigest()
        events = [self.event("completed", {"evidence_sha256": evidence}, "ev-completed",
                             evidence_path=self.write_blob("completion.bin", b"x"))]
        with self.assertRaises(outcome_store.OutcomeError):
            self.store.closeout("disp-1", events, decision_id="dec-other",
                                evidence_root=self.evidence_root)

    def test_project_mismatch_refused(self):
        evidence = hashlib.sha256(b"x").hexdigest()
        events = [self.event("completed", {"evidence_sha256": evidence}, "ev-completed",
                             evidence_path=self.write_blob("completion.bin", b"x"))]
        with self.assertRaises(outcome_store.OutcomeError):
            self.store.closeout("disp-1", events, decision_id="dec-1",
                                project="/other", evidence_root=self.evidence_root)

    def test_unknown_dispatch_refused(self):
        evidence = hashlib.sha256(b"x").hexdigest()
        events = [self.event("completed", {"evidence_sha256": evidence}, "ev-completed",
                             evidence_path=self.write_blob("completion.bin", b"x"))]
        with self.assertRaises(outcome_store.OutcomeError):
            self.store.closeout("disp-missing", events, decision_id="dec-1",
                                evidence_root=self.evidence_root)

    def test_too_many_events_refused(self):
        evidence = hashlib.sha256(b"x").hexdigest()
        events = [self.event("completed", {"evidence_sha256": evidence},
                             f"ev-{i}", evidence_path=f"file-{i}.bin")
                  for i in range(65)]
        for i, event in enumerate(events):
            if "evidence_path" in event:
                (self.evidence_root / event["evidence_path"]).write_bytes(b"x")
        with self.assertRaises(outcome_store.OutcomeError):
            self.closeout(events, max_events=64)


class CloseoutCoverageAudit(_StoreCase):
    def test_no_usage_no_rework_means_unknown(self):
        evidence = hashlib.sha256(b"x").hexdigest()
        self.closeout([
            self.event("completed", {"evidence_sha256": evidence}, "ev-completed",
                       evidence_path=self.write_blob("completion.bin", b"x")),
            self.event("review", {"actor": "r", "provider": "codex",
                                  "model": "gpt-5.6-luna", "family": "gpt-5.6-luna",
                                  "verdict": "accepted",
                                  "evidence_sha256": evidence}, "ev-review",
                       evidence_path=self.write_blob("review.bin", b"x")),
            self.event("accepted", {"actor": "c",
                                    "evidence_sha256": evidence}, "ev-accept",
                       evidence_path=self.write_blob("accept.bin", b"x")),
        ])
        audit = self.store.audit()
        attempt = next(a for a in audit["attempts"] if a["launch"]["dispatch_id"] == "disp-1")
        self.assertIn("missing_usage", attempt["closeout_gaps"])
        self.assertIn("missing_rework_evidence", attempt["closeout_gaps"])
        self.assertEqual(audit["metrics"]["known_usage_launches"], 0)
        self.assertEqual(audit["metrics"]["unknown_rework_launches"], 1)


class CloseoutCLI(_StoreCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state.json"
        base_config = json.loads(r.CONFIG.read_text())
        base_config.setdefault("task_profiles", {}).update(CONFIG.get("task_profiles", {}))
        base_config.setdefault("model_profiles", {}).update(CONFIG["model_profiles"])
        self._patches = [
            patch.object(r, "STATE", self.state),
            patch.object(outcome_cli, "store", lambda *_: self.store),
            patch.object(r, "load_config", return_value=(base_config, None)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.config = base_config

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = r.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_cli_closeout_full_happy_path(self):
        evidence = hashlib.sha256(b"completion").hexdigest()
        review_evidence = hashlib.sha256(b"review").hexdigest()
        accept_evidence = hashlib.sha256(b"accept").hexdigest()
        rework_evidence = hashlib.sha256(b"rework").hexdigest()
        # Manifest lives next to the evidence dir so evidence_path resolves
        # against the manifest's parent directory.
        manifest_dir = self.tmp / "evidence"
        (manifest_dir / "completion.bin").write_bytes(b"completion")
        (manifest_dir / "rework.bin").write_bytes(b"rework")
        (manifest_dir / "review.bin").write_bytes(b"review")
        (manifest_dir / "accept.bin").write_bytes(b"accept")
        manifest_path = manifest_dir / "manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1, "dispatch": "disp-1",
            "decision_id": "dec-1", "project": PROJECT,
            "events": [
                {"event_id": "ev-completed", "kind": "completed",
                 "data": {"evidence_sha256": evidence},
                 "evidence_path": "completion.bin"},
                {"event_id": "ev-rework", "kind": "rework",
                 "data": {"actor": "alice", "seconds": 5.0,
                          "evidence_sha256": rework_evidence},
                 "evidence_path": "rework.bin"},
                {"event_id": "ev-review", "kind": "review",
                 "data": {"actor": "reviewer", "provider": "codex",
                          "model": "gpt-5.6-luna", "family": "gpt-5.6-luna",
                          "verdict": "accepted",
                          "evidence_sha256": review_evidence},
                 "evidence_path": "review.bin"},
                {"event_id": "ev-usage", "kind": "usage",
                 "data": {"source": "native", "input_tokens": 10,
                          "output_tokens": 5}},
                {"event_id": "ev-accept", "kind": "accepted",
                 "data": {"actor": "coordinator",
                          "evidence_sha256": accept_evidence},
                 "evidence_path": "accept.bin"},
            ],
        }))
        code, text, err = self.invoke(["outcome", "closeout",
                                       "--manifest", str(manifest_path)])
        self.assertEqual(code, 0, err)
        applied = json.loads(text)["applied"]
        self.assertEqual([e["event_id"] for e in applied],
                         ["ev-completed", "ev-rework", "ev-review",
                          "ev-usage", "ev-accept"])
        attempt = next(a for a in self.store.audit()["attempts"]
                       if a["launch"]["dispatch_id"] == "disp-1")
        self.assertTrue(attempt["accepted"])
        self.assertEqual(attempt["closeout_gaps"], [])

    def test_cli_closeout_evidence_path_sha256(self):
        blob = b"verified"
        (self.evidence_root / "completion.bin").write_bytes(blob)
        evidence = hashlib.sha256(blob).hexdigest()
        manifest_path = self.evidence_root / "manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1, "dispatch": "disp-1",
            "decision_id": "dec-1", "project": PROJECT,
            "events": [
                {"event_id": "ev-completed", "kind": "completed",
                 "data": {"evidence_sha256": evidence},
                 "evidence_path": "completion.bin"},
            ],
        }))
        code, _, err = self.invoke(["outcome", "closeout",
                                    "--manifest", str(manifest_path)])
        self.assertEqual(code, 0, err)

    def test_cli_closeout_invalid_review_no_writes(self):
        evidence = hashlib.sha256(b"x").hexdigest()
        manifest_path = self.evidence_root / "manifest.json"
        (self.evidence_root / "completion.bin").write_bytes(b"x")
        (self.evidence_root / "self_review.bin").write_bytes(b"x")
        (self.evidence_root / "accept.bin").write_bytes(b"x")
        manifest_path.write_text(json.dumps({
            "schema_version": 1, "dispatch": "disp-1",
            "decision_id": "dec-1", "project": PROJECT,
            "events": [
                {"event_id": "ev-completed", "kind": "completed",
                 "data": {"evidence_sha256": evidence},
                 "evidence_path": "completion.bin"},
                {"event_id": "ev-review", "kind": "review",
                 "data": {"actor": "self", "provider": "minimax",
                          "model": "MiniMax-M3", "family": "minimax-m3",
                          "verdict": "accepted",
                          "evidence_sha256": evidence},
                 "evidence_path": "self_review.bin"},
                {"event_id": "ev-accept", "kind": "accepted",
                 "data": {"actor": "c", "evidence_sha256": evidence},
                 "evidence_path": "accept.bin"},
            ],
        }))
        code, _, _ = self.invoke(["outcome", "closeout",
                                  "--manifest", str(manifest_path)])
        self.assertEqual(code, 2)
        audit = self.store.audit()
        attempt = next(a for a in audit["attempts"]
                       if a["launch"]["dispatch_id"] == "disp-1")
        self.assertIn("missing_completion", attempt["closeout_gaps"])
        self.assertEqual(audit["metrics"]["accepted_launches"], 0)

    def test_cli_closeout_duplicate_field_rejected(self):
        manifest_path = self.evidence_root / "manifest.json"
        manifest_path.write_text('{"schema_version":1,"dispatch":"disp-1",'
                                 '"schema_version":1,"events":[]}')
        code, _, _ = self.invoke(["outcome", "closeout",
                                  "--manifest", str(manifest_path)])
        self.assertEqual(code, 2)

    def test_cli_closeout_oversize_manifest_rejected(self):
        manifest_path = self.evidence_root / "manifest.json"
        manifest_path.write_text(" " * (outcome_cli.CLOSEOUT_MANIFEST_MAX_BYTES + 1))
        code, _, _ = self.invoke(["outcome", "closeout",
                                  "--manifest", str(manifest_path)])
        self.assertEqual(code, 2)


class ProjectPlumbing(_StoreCase):
    def setUp(self):
        super().setUp()
        self.state = Path(self._tmp.name) / "state.json"
        self.config = json.loads(r.CONFIG.read_text())
        self.judgment = dict(tier="high_stakes", size=.4, second_opinion=.1,
                             spec_complete=1, destructive=0, source="caller (test)")
        self.elig = {p: dict(eligible=True, blocked=None, usable=80, unknown=False,
                            overrun=False, resets_at=r.now() + 3600, bucket="weekly",
                            inflight=0, reserved=0, buckets=[])
                     for p in ("codex", "claude", "opencode", "minimax")}
        self._patches = [
            patch.object(r, "STATE", self.state),
            patch.object(outcome_cli, "store", lambda *_: self.store),
            patch.object(r, "probes_cached",
                         return_value=({name: {"name": name, "status": "ok",
                                               "buckets": [], "account": None}
                                        for name in self.elig}, False)),
            patch.object(r, "eligibility", return_value=dict(self.elig)),
            patch.object(r, "judge", return_value=dict(self.judgment)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = r.main(args)
        return code, out.getvalue(), err.getvalue()

    def route_decision(self, project=None):
        args = ["route", "--task", "Finish a focused task", "--json"]
        if project is not None:
            args += ["--project", project]
        code, text, _ = self.invoke(args)
        self.assertEqual(code, 0, text)
        return json.loads(text)

    def test_route_records_project_from_cwd_when_unset(self):
        d = self.route_decision()
        decision = self.store.get_decision(d["decision_id"])
        self.assertEqual(decision["project"], str(Path.cwd().resolve()))

    def test_route_records_explicit_project(self):
        d = self.route_decision(project="/coordinator/project")
        decision = self.store.get_decision(d["decision_id"])
        self.assertEqual(decision["project"], "/coordinator/project")

    def test_route_blank_project_rejected(self):
        code, _, _ = self.invoke(["route", "--task", "Finish a focused task",
                                  "--project", "", "--json"])
        self.assertEqual(code, 2)

    def test_report_uses_decision_project_when_cwd_differs(self):
        d = self.route_decision(project="/coordinator/project")
        child = Path(self._tmp.name) / "child"
        child.mkdir()
        cwd = os.getcwd()
        try:
            os.chdir(child)
            args = ["report", d["pick"]["provider"], "--started",
                    "--model", d["pick"]["model"], "--task", "Finish a focused task",
                    "--dispatch", "child-disp-1", "--decision-id", d["decision_id"]]
            if d["pick"].get("effort"):
                args += ["--effort", d["pick"]["effort"]]
            code, text, err = self.invoke(args)
            self.assertEqual(code, 0, err)
        finally:
            os.chdir(cwd)
        launch = self.store.get_launch("child-disp-1")
        self.assertEqual(launch["project"], "/coordinator/project")

    def test_report_explicit_project_mismatch_fails_before_writes(self):
        d = self.route_decision(project="/coordinator/project")
        before = self.store.audit()["metrics"]["total_launches"]
        code, _, _ = self.invoke([
            "report", d["pick"]["provider"], "--started",
            "--model", d["pick"]["model"], "--task", "Finish a focused task",
            "--dispatch", "mismatch-disp", "--decision-id", d["decision_id"],
            "--project", "/different/project",
        ])
        self.assertEqual(code, 2)
        after = self.store.audit()["metrics"]["total_launches"]
        self.assertEqual(before, after)
        self.assertIsNone(self.store.get_launch("mismatch-disp"))

    def test_report_without_decision_uses_cwd_for_legacy_callers(self):
        d = self.route_decision(project="/coordinator/project")
        code, _, _ = self.invoke([
            "report", d["pick"]["provider"], "--started",
            "--model", d["pick"]["model"], "--task", "Finish a focused task",
            "--dispatch", "legacy-disp",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(self.store.get_launch("legacy-disp")["project"],
                         str(Path.cwd().resolve()))

    def test_record_launch_helper_keeps_existing_kwargs_compatible(self):
        # Distinct dispatch id to avoid polluting the live store with a launch
        # carrying the synthetic task text "Finish a focused task".
        receipt = r.record_launch(self.config, "minimax", "MiniMax-M3",
                                  "Finish a focused task", "compat-kwarg-disp")
        self.assertIsNotNone(receipt)
        self.assertEqual(self.store.get_launch("compat-kwarg-disp")["actual"]["model"],
                         "MiniMax-M3")


def closing_sqlite(path):
    from contextlib import closing
    import sqlite3
    return closing(sqlite3.connect(path))


if __name__ == "__main__":
    unittest.main()
