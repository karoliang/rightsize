"""Frozen-clock replay, baseline provenance and no-side-effect shadow proof."""

import builtins
from contextlib import ExitStack, redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import replay
import rightsize as r


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.document = replay.read(Path(__file__).parent / "fixtures/replay.json")

    def test_fixed_baseline_and_candidate_comparison(self):
        result = replay.run(self.document, r, compare=True)
        self.assertEqual(result["summary"]["cases"], 15)
        self.assertEqual(result["summary"]["known_denied_recommendations"], 0)
        self.assertEqual(result["summary"]["floor_violations"], 0)
        self.assertEqual(result["summary"]["selection_changes"], 4)
        rows = {row["id"]: row for row in result["cases"]}
        self.assertEqual(rows["uncertain-managed-attempt"]["baseline"]["pick"]["provider"], "opencode")
        self.assertEqual(rows["uncertain-managed-attempt"]["candidate"]["pick"]["provider"], "codex")
        for name in ("all-unavailable", "missing-native-quota-identity"):
            self.assertEqual(rows[name]["baseline"]["status"], "recommend")
            self.assertEqual(rows[name]["candidate"]["status"], "wait")
        migrated = rows["migrated-legacy-record-without-receipt"]["coverage"]
        self.assertEqual((migrated["legacy_holds"], migrated["attempts"], migrated["unresolved"]), (1, 1, 1))
        self.assertEqual(migrated["accepted"], 0)

    def test_shadow_is_repeatable_without_io_or_live_clock(self):
        document = {k: v for k, v in self.document.items() if k != "baseline"}
        before = copy.deepcopy(document)
        with ExitStack() as stack:
            for owner, name in [(builtins, "open"), (Path, "open"), (r.os, "open"),
                                (r.subprocess, "Popen"), (r.urllib.request, "urlopen"),
                                (r, "load_config"), (r, "load_json"), (r, "save_json"),
                                (r, "state_lock"), (r, "secret"), (r, "probe_all"), (r, "judge"),
                                (r, "reserve"), (r, "now"), (r.accounts, "select"),
                                (replay.managed_router.time, "time")]:
                stack.enter_context(patch.object(owner, name, side_effect=AssertionError("side effect forbidden")))
            first = replay.run(document, r)
            second = replay.run(document, r)
        self.assertEqual(first, second)
        self.assertEqual(document, before)
        self.assertTrue(all(row["launches"] == row["reservations"] == 0 for row in first["cases"]))

    def test_cli_reads_only_explicit_snapshot_without_local_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(self.document))
            output = io.StringIO()
            with patch.object(r, "load_config", side_effect=AssertionError("no config read")), \
                    patch.object(r, "probe_all", side_effect=AssertionError("no probe")), redirect_stdout(output):
                self.assertEqual(r.main(["replay", "--snapshot", str(path)]), 0)
            self.assertEqual(json.loads(output.getvalue())["mode"], "replay")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_baseline_hash_revision_case_coverage_and_input_changes_rejected(self):
        mutations = [lambda d: d["baseline"].update(revision="HEAD"),
                     lambda d: d["baseline"].update(source_sha256="0" * 64),
                     lambda d: d["cases"][0].update(context_sha256="0" * 64),
                     lambda d: d["baseline"]["records"].pop("healthy"),
                     lambda d: d["cases"].append(d["cases"][0]),
                     lambda d: d["baseline"]["records"]["healthy"]["verdict"].update(secret="sentinel")]
        for mutate in mutations:
            document = copy.deepcopy(self.document)
            mutate(document)
            with self.subTest(mutation=mutate), self.assertRaises(replay.ReplayError):
                replay.run(document, r, compare=True)

    def test_unknown_outcomes_stay_in_denominator(self):
        row = next(c for c in self.document["cases"] if c["id"] == "unreviewed-and-unattributed-outcomes")
        coverage = replay.shadow(row, r)["coverage"]
        self.assertEqual(coverage["attempts"], 2)
        self.assertEqual(coverage["accepted"], 0)
        self.assertEqual(coverage["unresolved"], 2)
        self.assertEqual(coverage["completed_unreviewed"], 1)
        self.assertEqual(coverage["accepted_per_attempt"], 0)
        self.assertIsNone(replay.shadow(self.document["cases"][0], r)["coverage"]["accepted_per_attempt"])

    def test_exact_receipt_and_review_both_required(self):
        case = copy.deepcopy(self.document["cases"][0])
        account = case["probes"]["codex"]["account"]
        pick = {"provider": "codex", "model": "fixture", "effort": "low"}
        attempt = {"state": "accepted", "pick": pick, "account": account,
                   "spec_hash": case["task_sha256"], "context_hash": case["context_sha256"],
                   "receipt": {**pick, **account, "session_id": "native-session", "dispatch_id": "native-turn"},
                   "review_evidence": "sha256:" + "a" * 64}
        case["attempts"] = [attempt]
        self.assertEqual(replay.coverage(case)["accepted"], 1)
        attempt["receipt"]["model"] = "wrong-model"
        self.assertEqual(replay.coverage(case)["accepted"], 0)
        attempt["receipt"]["model"] = "fixture"
        attempt.pop("review_evidence")
        self.assertEqual(replay.coverage(case)["accepted"], 0)

    def test_credentials_and_context_content_not_opened_or_echoed(self):
        sentinel = "SYNTHETIC_SECRET_DO_NOT_ECHO"
        case = copy.deepcopy(self.document["cases"][0])
        case["config"]["api_key"] = sentinel
        document = {"schema_version": 1, "cases": [case]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(document))
            output = io.StringIO()
            with redirect_stdout(output):
                code = r.main(["shadow", "--snapshot", str(path)])
            self.assertEqual(code, 2)
            self.assertNotIn(sentinel, output.getvalue())
        case["config"].pop("api_key")
        case["context_sha256"] = "file:///unapproved/context"
        with self.assertRaises(replay.ReplayError):
            replay.run(document, r)

    def test_duplicate_nonfinite_and_oversized_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            for text in ('{"schema_version":1,"schema_version":1}', '{"at":NaN}', 'x' * (4 * 1024 * 1024 + 1)):
                path.write_text(text)
                with self.assertRaises(replay.ReplayError):
                    replay.read(path)

    def test_safety_report_detects_a_denied_recommendation_and_floor_drop(self):
        case = copy.deepcopy(self.document["cases"][0])
        case["floor"] = 3
        case["probes"]["opencode"]["status"] = "denied"
        decision = {"pick": {"provider": "opencode", "model": "fixture", "effort": "low"},
                    "blocked": None, "confirm_first": False, "band": 1,
                    "account": case["probes"]["opencode"]["account"]}
        with patch.object(replay.managed_router, "decision_at_floor", return_value=decision):
            result = replay.shadow(case, r)
        self.assertTrue(result["known_denied_recommendation"])
        self.assertTrue(result["floor_violation"])


if __name__ == "__main__":
    unittest.main()
