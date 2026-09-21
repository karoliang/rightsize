"""Offline MiniMax subscription probe, account and routing regressions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error

import accounts
import rightsize as r


class MiniMaxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in (("STATE", Path(self.temp.name) / "state.json"),
                            ("now", lambda: 1000)):
            p = patch.object(r, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.config = json.loads(r.CONFIG.read_text())
        self.row = {"model_name": "general", "current_interval_remaining_percent": 80,
                    "current_weekly_remaining_percent": 65,
                    "end_time": 2000000, "weekly_end_time": 9000000}

    def probe(self, row=None):
        data = {"base_resp": {"status_code": 0},
                "model_remains": [self.row if row is None else row]}
        with patch.object(r, "get", return_value=data):
            return r.probe_minimax(key="test-subscription-key")

    def test_remaining_is_converted_to_used_with_millisecond_reset(self):
        p = self.probe()
        self.assertEqual(p["status"], "ok")
        self.assertEqual([b["percent"] for b in p["buckets"]], [20, 35])
        self.assertEqual([b["resets_at"] for b in p["buckets"]], [2000, 9000])
        self.assertNotIn("test-subscription-key", json.dumps(p))

    def test_bad_missing_expired_and_ambiguous_counts_are_unavailable(self):
        for field, value in (("current_weekly_remaining_percent", None),
                             ("current_interval_remaining_percent", True),
                             ("current_interval_remaining_percent", 101),
                             ("current_interval_remaining_percent", float('nan')),
                             ("end_time", 999000), ("model_name", "video")):
            row = {**self.row, field: value,
                   "current_interval_usage_count": 0, "current_interval_total_count": 100}
            with self.subTest(field=field, value=value):
                self.assertTrue(self.probe(row)["status"].startswith("error"))

    def test_exhausted_status_overrides_percent(self):
        p = self.probe({**self.row, "current_weekly_status": 2})
        self.assertEqual(p["status"], "denied")
        self.assertEqual(p["buckets"][1]["percent"], 100)

    def test_http_errors_are_redacted(self):
        for code, status in ((401, "reauth-required"), (429, "denied"),
                             (500, "error: quota probe unavailable")):
            with patch.object(r, "get", side_effect=urllib.error.HTTPError(
                    'https://example.com', code, 'secret-error', {}, None)):
                self.assertEqual(r.probe_minimax(key="key")["status"], status)

    def test_no_credential_does_not_call_endpoint(self):
        with patch.object(r, "secret", return_value=None), patch.object(r, "get") as get:
            self.assertEqual(r.probe_minimax()["status"], "no-credential")
            get.assert_not_called()

    def test_native_subscription_key_only_and_environment_fingerprint(self):
        root = Path(self.temp.name)
        auth = root / 'opencode/auth.json'
        auth.parent.mkdir()
        auth.write_text(json.dumps({'minimax': {'key': 'payg'},
                                    'minimax-coding-plan': {'key': 'subscription'}}))
        with patch.object(r, 'ROOT', root), patch.dict('os.environ',
                {'XDG_DATA_HOME': str(root)}, clear=True):
            self.assertEqual(r.secret('MINIMAX_API_KEY', {}), 'subscription')
        a = accounts.select('minimax', home=root, environ={'MINIMAX_API_KEY': 'a'})
        b = accounts.select('minimax', home=root, environ={'MINIMAX_API_KEY': 'b'})
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_explicit_vault_does_not_fall_back_to_native(self):
        root = Path(self.temp.name)
        (root / '.infisical.json').write_text('{}')
        with patch.object(r, 'ROOT', root), patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(r.secret('MINIMAX_API_KEY', {}))

    def test_refresh_registers_subscription_catalog(self):
        catalog = {'minimax-coding-plan': {'models': {
            'MiniMax-M3': {'name': 'M3', 'limit': {'context': 1000000}}}}}
        with patch.object(r, 'get', return_value=catalog), \
                patch.object(r, 'secret', return_value=None), \
                patch.object(r, 'load_json', return_value={}), \
                patch.object(r, 'save_json'):
            registry = r.refresh()
        self.assertIn('MiniMax-M3', registry['providers']['minimax']['models'])

    def test_band_two_can_select_direct_plan_and_reserve_blocks_it(self):
        elig = r.eligibility(self.config, {'minimax': self.probe()}, record=False)
        choice = r.pick(self.config['bands']['2'], elig, 2)
        self.assertEqual(choice[0]['provider'], 'minimax')
        self.assertEqual(choice[0]['model'], 'MiniMax-M3')
        denied = self.probe({**self.row, 'current_weekly_remaining_percent': 10})
        elig = r.eligibility(self.config, {'minimax': denied}, record=False)
        self.assertIsNone(r.pick(self.config['bands']['2'], elig, 2)[0])


if __name__ == '__main__':
    unittest.main()
