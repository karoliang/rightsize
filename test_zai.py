import json
import unittest
from unittest.mock import Mock, patch

import rightsize as r


PAYLOAD = {"result": {"rateLimits": {"zcodePlanApiKeyConfigured": True,
    "zcode": {"status": "ok", "session": {"usedPercent": 20, "windowMinutes": 300, "resetsAt": 2000000},
    "weekly": {"usedPercent": 30, "windowMinutes": 10080, "resetsAt": 9000000},
    "monthly": None, "usageMetadata": {"source": "web", "authProvenance": "orca"}}}}}


class ZaiProbeTests(unittest.TestCase):
    def probe(self, payload=PAYLOAD, **kwargs):
        result = Mock(returncode=0, stdout=json.dumps(payload))
        with patch.object(r.subprocess, "run", return_value=result) as call:
            probe = r.probe_zai(**kwargs)
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

    def test_missing_credential(self):
        payload = json.loads(json.dumps(PAYLOAD)); payload["result"]["rateLimits"]["zcodePlanApiKeyConfigured"] = False
        self.assertEqual(self.probe(payload)["status"], "no-credential")

    def test_invalid_json_and_subprocess_failure_are_safe(self):
        with patch.object(r.subprocess, "run", side_effect=OSError("missing")):
            self.assertTrue(r.probe_zai()["status"].startswith("error:"))
        result = Mock(returncode=0, stdout="not json")
        with patch.object(r.subprocess, "run", return_value=result):
            self.assertTrue(r.probe_zai()["status"].startswith("error:"))

    def test_routes_zai_after_minimax_reserve(self):
        config = json.loads(r.CONFIG.read_text())
        probes = {"minimax": {"status": "ok", "buckets": [{"id": "rolling", "percent": 90, "resets_at": 2000, "source": "live"}]}, "zai": self.probe()}
        choice, _ = r.pick(config["bands"]["1"], r.eligibility(config, probes, record=False), 1)
        self.assertEqual((choice["provider"], choice["model"]), ("zai", "glm-5.3-flash"))
