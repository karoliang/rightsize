"""Orca-backed Z.AI Coding Plan quota regressions."""
import json
import unittest
from unittest.mock import Mock, patch

import rightsize as r


PAYLOAD = {"result": {"rateLimits": {"zcodePlanApiKeyConfigured": True,
    "zcode": {"status": "ok", "session": {"usedPercent": 20, "windowMinutes": 300, "resetsAt": 2000000},
    "weekly": {"usedPercent": 30, "windowMinutes": 10080, "resetsAt": 9000000},
    "monthly": None, "usageMetadata": {"source": "web", "authProvenance": "orca"}}}}}


class ZaiCodingPlanOrcaProbeTests(unittest.TestCase):
    def probe(self, payload=PAYLOAD):
        result = Mock(returncode=0, stdout=json.dumps(payload))
        with patch.object(r.subprocess, "run", return_value=result) as call:
            probe = r.probe_zai_coding_plan()
        self.assertEqual(call.call_args.args[0], ["orca", "account", "list", "--json"])
        return probe

    def test_parses_live_orca_windows_without_credentials(self):
        probe = self.probe()
        self.assertEqual(probe["status"], "ok")
        self.assertEqual(probe["buckets"], [{"id": "rolling", "percent": 20, "resets_at": 2000.0, "source": "live"}, {"id": "weekly", "percent": 30, "resets_at": 9000.0, "source": "live"}])
        self.assertNotIn("key", json.dumps(probe).lower())

    def test_denied_at_full_bucket(self):
        payload = json.loads(json.dumps(PAYLOAD)); payload["result"]["rateLimits"]["zcode"]["weekly"]["usedPercent"] = 100
        self.assertEqual(self.probe(payload)["status"], "denied")

    def test_no_credential_from_orca(self):
        payload = json.loads(json.dumps(PAYLOAD)); payload["result"]["rateLimits"]["zcodePlanApiKeyConfigured"] = False
        self.assertEqual(self.probe(payload)["status"], "no-credential")

    def test_invalid_orca_json_is_an_error(self):
        result = Mock(returncode=0, stdout="not json")
        with patch.object(r.subprocess, "run", return_value=result):
            probe = r.probe_zai_coding_plan()
        self.assertEqual(probe["status"], "error: invalid Orca quota response")

    def test_subprocess_error_falls_back_to_api_key_path(self):
        with patch.object(r.subprocess, "run", side_effect=OSError("missing")):
            probe = r.probe_zai_coding_plan(key="subscription-key")
        self.assertEqual(probe["status"], "ok")
        self.assertTrue(probe["unmetered"])
        self.assertEqual(probe["buckets"], [])
        self.assertNotIn("subscription-key", json.dumps(probe))

    def test_subprocess_error_without_api_key_is_no_credential(self):
        with patch.object(r.subprocess, "run", side_effect=OSError("missing")), \
                patch.object(r, "secret", return_value=None):
            self.assertEqual(r.probe_zai_coding_plan()["status"], "no-credential")

    def test_routes_coding_plan_after_minimax_reserve(self):
        config = json.loads(r.CONFIG.read_text())
        probes = {"minimax": {"status": "ok", "buckets": [{"id": "rolling", "percent": 90, "resets_at": 2000, "source": "live"}]}, "zai_coding_plan": self.probe()}
        choice, _ = r.pick(config["bands"]["1"], r.eligibility(config, probes, record=False), 1)
        self.assertEqual((choice["provider"], choice["model"]), ("zai_coding_plan", "glm-5.3-flash"))
