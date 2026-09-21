"""Managed CLI integration and fake-launcher failure recovery."""

from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import accounts
import rightsize as r
from managed_ledger import Ledger
from managed_router import NoLaunch, launch_once


class ManagedRouterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.spec = self.root / "task.md"
        self.spec.write_text("Implement a bounded parser")
        self.judgment = self.root / "judgment.json"
        self.judgment.write_text(json.dumps({"schema_version": 1, "actor": "active-agent",
            "task_sha256": hashlib.sha256(self.spec.read_bytes()).hexdigest(),
            "judgment": {"tier": "implementation", "size": 0.1, "second_opinion": 0,
                         "spec_complete": 1, "destructive": 0}}))
        state = patch.object(r, "STATE", self.root / "state.json")
        state.start()
        self.addCleanup(state.stop)
        self.ledger = Ledger(self.root / "managed.sqlite3")
        self.account = accounts.select("codex").public()
        self.probes = {"codex": {"name": "codex", "status": "ok", "observed_at": r.now(),
                                "account": self.account, "buckets": [
                                    {"id": "primary-300m", "percent": 10, "source": "live",
                                     "resets_at": r.now() + 300}]}}

    def invoke(self, action="admit", request="request-one", task="task-one"):
        argv = ["managed", action, "--spec", str(self.spec), "--judgment", str(self.judgment), "--task-id", task]
        if action == "admit":
            argv += ["--request-id", request]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(r, "probe_all", return_value=self.probes) as probe, \
                patch.object(r, "judge", side_effect=AssertionError("no judge call")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = r.main(argv)
        return code, json.loads(stdout.getvalue() or stderr.getvalue()), probe.call_count

    def test_plan_has_no_lease_or_state_writes(self):
        before = sorted(path.name for path in self.root.iterdir())
        code, result, count = self.invoke("plan")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "planned")
        self.assertFalse(result["lease_created"])
        self.assertEqual(count, 1)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)

    def test_admit_idempotency_avoids_second_probe(self):
        code, result, count = self.invoke()
        self.assertEqual((code, result["status"], count), (0, "admitted", 1))
        code, duplicate, count = self.invoke()
        self.assertEqual((code, duplicate["status"], count), (0, "existing", 0))
        self.assertEqual(result["attempt"]["attempt_id"], duplicate["attempt"]["attempt_id"])

    def test_stale_observation_reprobes_outside_compatibility_lock(self):
        import copy
        import fcntl
        stale = copy.deepcopy(self.probes)
        stale["codex"]["observed_at"] -= 60
        calls = []
        def probe(config):
            # A separate descriptor cannot acquire this lock if execute still
            # holds it while refreshing. Never block the test on failure.
            with r.STATE.with_name("state.json.lock").open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
            calls.append(True)
            return stale if len(calls) == 1 else self.probes
        with patch.object(r, "probe_all", side_effect=probe), redirect_stdout(io.StringIO()):
            code = r.main(["managed", "admit", "--spec", str(self.spec), "--judgment", str(self.judgment),
                           "--task-id", "task", "--request-id", "request"])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)

    def test_invalid_judgment_and_corrupt_legacy_fail_before_probing(self):
        self.judgment.write_text("{}")
        code, result, count = self.invoke()
        self.assertEqual((code, count), (2, 0))
        self.assertFalse(self.ledger.path.exists())

    def test_legacy_corruption_is_not_an_empty_commitment_set(self):
        r.STATE.write_text("invalid-json")
        code, _, count = self.invoke()
        self.assertEqual((code, count), (2, 0))
        self.assertEqual(r.STATE.read_text(), "invalid-json")

    def test_managed_hold_blocks_legacy_prereserve_until_confirmed_no_launch(self):
        _, result, _ = self.invoke()
        attempt = result["attempt"]
        self.assertIsNone(r.reserve("codex", 1, 1, "legacy", 60))
        self.assertGreater(r.reservation_load("codex")[0], 0)
        self.ledger.event(attempt["attempt_id"], "no-launch", "launch_failed", evidence="not-launched")
        self.assertIsNotNone(r.reserve("codex", 1, 1, "legacy", 60))

    def test_quota_wait_does_not_lower_capability_floor(self):
        document = json.loads(self.judgment.read_text())
        document["judgment"]["tier"] = "high_stakes"
        self.judgment.write_text(json.dumps(document))
        self.probes["codex"]["buckets"][0]["percent"] = 100
        code, result, _ = self.invoke()
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "wait")
        self.assertEqual(result["decision"]["band"], 3)
        self.assertIsNone(result["decision"]["pick"])
        self.assertFalse(self.ledger.path.exists())

    def test_fake_launcher_receipt_is_bound_and_launches_once(self):
        _, result, _ = self.invoke()
        attempt = result["attempt"]
        calls = []
        def launch(row):
            calls.append(row["launch_key"])
            return {"dispatch_id": "fake-dispatch", "session_id": "fake-session",
                    **{key: row["pick"][key] for key in ("provider", "model", "effort")},
                    **{key: row["account"][key] for key in ("account_ref", "fingerprint")}}
        result = launch_once(self.ledger, attempt["attempt_id"], launch)
        self.assertEqual(result["status"], "started")
        launch_once(self.ledger, attempt["attempt_id"], launch)
        self.assertEqual(len(calls), 1)

    def test_fake_failure_vs_unknown_release_behavior(self):
        _, result, _ = self.invoke()
        aid = result["attempt"]["attempt_id"]
        def unknown(_):
            raise TimeoutError("synthetic-secret-sentinel")
        result = launch_once(self.ledger, aid, unknown)
        self.assertEqual(result["status"], "reconciling")
        self.assertNotIn("synthetic-secret-sentinel", self.ledger.path.read_bytes().decode(errors="ignore"))
        launch_once(self.ledger, aid, lambda _: self.fail("must not launch twice"))
        self.assertEqual(r.managed_commitments("codex")[1], 1)
        self.ledger.event(aid, "recovered", "launch_failed", evidence="adapter-confirmed-absent")
        _, next_result, _ = self.invoke(request="two", task="two")
        def absent(_):
            raise NoLaunch()
        failed = launch_once(self.ledger, next_result["attempt"]["attempt_id"], absent)
        self.assertEqual(failed["status"], "launch_failed")
        self.assertEqual(r.managed_commitments("codex")[1], 0)


if __name__ == "__main__":
    unittest.main()
