"""Account isolation and binding, entirely offline with temporary homes."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import accounts
import rightsize as r


class AccountTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.auth = self.home / ".codex/auth.json"
        self.auth.parent.mkdir()
        self.auth.write_text('"synthetic-secret-sentinel"')
        for patcher in (patch.dict(os.environ, {}, clear=True),
                        patch.object(accounts.Path, "home", return_value=self.home),
                        patch.object(r, "STATE", self.home / "state.json"),
                        patch.object(r, "ROOT", self.home)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = {"account_bindings": {"work": {"runtime": "codex", "home": str(self.auth.parent)}},
                       "accounts": {"codex": "work"}}

    def test_multiple_homes_require_selection_not_latest(self):
        second = self.home / "Library/Application Support/orca/codex-accounts/second/home/auth.json"
        second.parent.mkdir(parents=True)
        second.write_text('"other-key"')
        self.assertEqual(accounts.select("codex").status, "ambiguous")
        self.assertEqual(accounts.select("codex", self.config).home, self.auth.parent)
        with patch.dict(os.environ, {"CODEX_HOME": str(second.parent)}):
            self.assertEqual(accounts.select("codex").home, second.parent)
            self.assertEqual(accounts.select("codex", self.config).home, self.auth.parent)

    def test_conflicting_homes_and_api_auth_are_ambiguous(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.auth.parent),
                                     "ORCA_CODEX_HOME": str(self.home / "different")}):
            self.assertEqual(accounts.select("codex").status, "ambiguous")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "api-sentinel"}):
            self.assertEqual(accounts.select("codex", self.config).status, "ambiguous")

    def test_alias_cannot_escape_denial_identity_and_invalid_ref_does_not_fallback(self):
        first = accounts.select("codex", self.config)
        self.config["account_bindings"]["alias"] = self.config["account_bindings"]["work"]
        self.config["accounts"]["codex"] = "alias"
        self.assertEqual(first.account_ref, accounts.select("codex", self.config).account_ref)
        self.config["accounts"]["codex"] = "missing"
        with patch.object(r, "read_rate_limits", side_effect=AssertionError("wrong account")):
            self.assertEqual(r.probe_codex(self.config)["status"], "invalid-binding")

    def test_runtime_diagnostic_does_not_echo_secrets(self):
        import subprocess
        with patch.object(accounts.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "secret-sentinel codex-cli 1.2.3", "secret-error")):
            self.assertEqual(accounts.runtime_version("codex"), "1.2.3")
        with patch.object(accounts.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(accounts.runtime_version("codex"), "unavailable")

    def test_auth_rotation_invalidates_fingerprint_not_account_ref(self):
        before = accounts.select("codex", self.config)
        self.auth.write_text('"rotated-synthetic-key"')
        after = accounts.select("codex", self.config)
        self.assertEqual(before.account_ref, after.account_ref)
        self.assertNotEqual(before.fingerprint, after.fingerprint)
        self.assertNotIn("rotated", json.dumps(after.public()))
        self.assertNotIn(str(self.home), repr(after))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "one"}):
            first = accounts.select("codex").fingerprint
        with patch.dict(os.environ, {"OPENAI_API_KEY": "two"}):
            self.assertNotEqual(first, accounts.select("codex").fingerprint)

    def test_probe_pins_home_no_auth_mutation_and_no_rollout_fallback(self):
        before = self.auth.read_bytes()
        result = {"rateLimits": {"primary": {"usedPercent": 10, "windowDurationMins": 300}}}
        with patch.object(r, "read_rate_limits", return_value=result) as rpc:
            probe = r.probe_codex(self.config)
        self.assertEqual(rpc.call_args.kwargs["env"]["CODEX_HOME"], str(self.auth.parent))
        self.assertEqual(probe["account"], accounts.select("codex", self.config).public())
        self.assertEqual(self.auth.read_bytes(), before)
        with patch.object(r, "read_rate_limits", return_value=None), \
                patch.object(r, "newest_codex_rollout", side_effect=AssertionError("unattributable")):
            self.assertEqual(r.probe_codex(self.config)["status"], "unknown")

    def test_auth_switch_during_probe_discards_reading(self):
        def probe(*args, **kwargs):
            self.auth.write_text('"changed"')
            return {"rateLimits": {"primary": {"usedPercent": 0}}}
        with patch.object(r, "read_rate_limits", side_effect=probe):
            self.assertEqual(r.probe_codex(self.config)["status"], "account-changed")

    def test_cache_misses_after_key_change(self):
        with patch.object(r, "probe_all", return_value={}) as probe:
            r.probes_cached(self.config)
            r.probes_cached(self.config)
            self.assertEqual(probe.call_count, 1)
            self.auth.write_text('"new-key"')
            r.probes_cached(self.config)
            self.assertEqual(probe.call_count, 2)

    def test_shell_binds_and_stale_decision_cannot_launch(self):
        config = {**self.config, "launchers": {"shell": {"codex": "codex exec --model {model} {spec}"},
                                              "orca": {"codex": "orca worker-start"}}}
        decision = {"pick": {"provider": "codex", "model": "test", "effort": "low"},
                    "band": 1, "agent": "codex", "account": accounts.select("codex", config).public()}
        command = r.launch_command(decision, config, "shell", None, "task")
        self.assertIn("CODEX_HOME=", command)
        self.assertTrue(r.launch_command(decision, config, "orca", None, "task").startswith("#"))
        self.auth.write_text('"switched"')
        self.assertIn("account changed", r.launch_command(decision, config, "shell", None, "task"))

    def test_denials_remain_with_original_account(self):
        def probe(ref, status="unknown", buckets=None):
            return {"codex": {"status": status, "account": {"account_ref": ref}, "buckets": buckets or []}}
        denied = r.eligibility({}, probe("first", "denied"))
        self.assertIn("quota denied", denied["codex"]["blocked"])
        healthy = [{"id": "weekly", "percent": 0, "source": "live", "resets_at": None}]
        r.eligibility({}, probe("second", "ok", healthy))
        again = r.eligibility({}, probe("first"))
        self.assertIn("quota denied", again["codex"]["blocked"])
        for status in ("ambiguous", "invalid-binding", "reauth-required", "account-changed"):
            self.assertFalse(r.eligibility({}, probe("second", status))["codex"]["eligible"])


if __name__ == "__main__":
    unittest.main()
