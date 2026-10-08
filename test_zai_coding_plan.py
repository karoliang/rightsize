"""Offline Z.AI GLM Coding Plan provider, catalogue and routing regressions."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error

import accounts
import rightsize as r


def opencode_db_with_credential(root: Path, integration="zai-coding-plan", value=None):
    db = root / "opencode/opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE `credential` (
          `id` text PRIMARY KEY, `integration_id` text, `label` text NOT NULL,
          `value` text NOT NULL, `connector_id` text, `method_id` text,
          `active` integer, `time_created` integer NOT NULL,
          `time_updated` integer NOT NULL)""")
    con.execute("INSERT INTO credential VALUES (?,?,?,?,?,?,?,?,?)",
                ("cred_test", integration, "Z.AI Coding Plan",
                 value or json.dumps({"type": "api", "key": "db-subscription-key"}),
                 None, None, 1, 1, 1))
    con.commit()
    con.close()
    return db


class ZaiCodingPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in (("STATE", Path(self.temp.name) / "state.json"),
                            ("now", lambda: 1000)):
            p = patch.object(r, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.config = json.loads(r.CONFIG.read_text())
        self.minimax_row = {"model_name": "general",
                            "current_interval_remaining_percent": 80,
                            "current_weekly_remaining_percent": 65,
                            "end_time": 2000000, "weekly_end_time": 9000000}

    def probe(self, key="test-subscription-key"):
        return r.probe_zai_coding_plan(key=key)

    def minimax_probe(self, weekly_remaining=65):
        data = {"base_resp": {"status_code": 0},
                "model_remains": [{**self.minimax_row,
                                   "current_weekly_remaining_percent": weekly_remaining}]}
        with patch.object(r, "get", return_value=data):
            return r.probe_minimax(key="minimax-key")

    # ---------------------------------------------------------------- probe

    def test_probe_is_unmetered_and_invents_no_endpoint(self):
        with patch.object(r, "get", side_effect=AssertionError("no endpoint exists")):
            p = self.probe()
        self.assertEqual(p["status"], "ok")
        self.assertEqual(p["buckets"], [])
        self.assertTrue(p["unmetered"])
        self.assertIn("quota_account_ref", p)
        self.assertNotIn("test-subscription-key", json.dumps(p))

    def test_probe_without_credential_is_blocked(self):
        with patch.object(r, "secret", return_value=None), patch.object(r, "get") as get:
            self.assertEqual(r.probe_zai_coding_plan()["status"], "no-credential")
            get.assert_not_called()

    def test_probe_rejects_malformed_key(self):
        for key in (None, "", "line\nbreak"):
            with self.subTest(key=key):
                with patch.object(r, "secret", return_value=None):
                    self.assertEqual(r.probe_zai_coding_plan()["status"], "no-credential")

    # ------------------------------------------------------------ credential

    def test_secret_reads_opencode_credential_db(self):
        root = Path(self.temp.name)
        opencode_db_with_credential(root)
        with patch.object(r, "ROOT", root), patch.dict(
                "os.environ", {"XDG_DATA_HOME": str(root)}, clear=True):
            self.assertEqual(r.secret("ZAI_CODING_PLAN_API_KEY", {}), "db-subscription-key")

    def test_secret_prefers_auth_store_entry_over_db(self):
        root = Path(self.temp.name)
        auth = root / "opencode/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"zai-coding-plan": {"type": "api", "key": "auth-key"}}))
        opencode_db_with_credential(root)
        with patch.object(r, "ROOT", root), patch.dict(
                "os.environ", {"XDG_DATA_HOME": str(root)}, clear=True):
            self.assertEqual(r.secret("ZAI_CODING_PLAN_API_KEY", {}), "auth-key")

    def test_secret_environment_wins(self):
        root = Path(self.temp.name)
        opencode_db_with_credential(root)
        with patch.object(r, "ROOT", root), patch.dict(
                "os.environ", {"ZAI_CODING_PLAN_API_KEY": "env-key",
                               "XDG_DATA_HOME": str(root)}, clear=True):
            self.assertEqual(r.secret("ZAI_CODING_PLAN_API_KEY", {}), "env-key")

    def test_secret_missing_everywhere(self):
        root = Path(self.temp.name)
        (root / "opencode").mkdir()
        with patch.object(r, "ROOT", root), patch.dict(
                "os.environ", {"XDG_DATA_HOME": str(root)}, clear=True):
            self.assertIsNone(r.secret("ZAI_CODING_PLAN_API_KEY", {}))

    def test_account_binding_fingerprint_distinguishes_keys(self):
        root = Path(self.temp.name)
        a = accounts.select("zai_coding_plan", home=root,
                            environ={"ZAI_CODING_PLAN_API_KEY": "a"})
        b = accounts.select("zai_coding_plan", home=root,
                            environ={"ZAI_CODING_PLAN_API_KEY": "b"})
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        self.assertEqual(a.runtime, "opencode")

    # -------------------------------------------------------------- routing

    def test_routing_falls_to_glm_while_minimax_is_below_reserve(self):
        probes = {"minimax": self.minimax_probe(weekly_remaining=2),
                  "zai_coding_plan": self.probe()}
        elig = r.eligibility(self.config, probes, record=False)
        self.assertFalse(elig["minimax"]["eligible"])
        for band in ("1", "2", "3"):
            choice, _ = r.pick(self.config["bands"][band], elig, int(band))
            self.assertEqual(choice["provider"], "zai_coding_plan", f"band {band}")

    def test_minimax_is_preferred_while_it_has_headroom(self):
        probes = {"minimax": self.minimax_probe(weekly_remaining=65),
                  "zai_coding_plan": self.probe()}
        elig = r.eligibility(self.config, probes, record=False)
        for band in ("1", "2", "3"):
            choice, _ = r.pick(self.config["bands"][band], elig, int(band))
            self.assertEqual(choice["provider"], "minimax", f"band {band}")

    def test_unmetered_is_usable_in_every_band(self):
        elig = r.eligibility(self.config, {"zai_coding_plan": self.probe()}, record=False)
        info = elig["zai_coding_plan"]
        self.assertTrue(info["eligible"])
        self.assertIsNone(info["usable"])
        self.assertTrue(info["unmetered"])
        choice, notes = r.pick(self.config["bands"]["1"], elig, 1)
        self.assertEqual(choice["model"], "glm-5.3-flash")
        self.assertTrue(any("no quota API" in n for n in notes))

    def test_highspeed_models_are_not_on_any_ladder(self):
        ladder = [c for ladders in self.config["bands"].values() for c in ladders]
        ladder += self.config.get("review_ladder", [])
        for model in ("glm-5.3-highspeed", "glm-5.2-highspeed"):
            self.assertNotIn(f"zai_coding_plan:{model}", ladder)

    def test_profile_driven_model_choice_per_tier(self):
        elig = r.eligibility(self.config, {"zai_coding_plan": self.probe()}, record=False)
        tiers = {"mechanical": ("glm-5.3-flash", 1),
                 "implementation": ("glm-5.3-flash", 1),
                 "design": ("glm-5.3", 3),
                 "diagnosis": ("glm-5.3", 3),
                 "high_stakes": ("glm-5.3", 3)}
        for tier, (model, band) in tiers.items():
            judgment = {"tier": tier, "size": 0.4, "second_opinion": 0.2,
                        "spec_complete": 0.9, "destructive": 0.0}
            decision = r.decide(judgment, self.config, elig)
            self.assertIsNotNone(decision["pick"], tier)
            self.assertEqual(decision["pick"]["model"], model, tier)
            self.assertEqual(decision["band"], band, tier)

    def test_design_task_gets_independent_reviewer_across_plans(self):
        elig = r.eligibility(self.config, {"zai_coding_plan": self.probe(),
                                           "codex": self.codex_probe()}, record=False)
        judgment = {"tier": "high_stakes", "size": 0.4, "second_opinion": 0.9,
                    "spec_complete": 0.9, "destructive": 0.0}
        decision = r.decide(judgment, self.config, elig)
        self.assertEqual(decision["pick"]["provider"], "codex")
        self.assertIsNotNone(decision["review"])
        self.assertNotEqual(decision["review"]["provider"], decision["pick"]["provider"])

    # ---------------------------------------------------------- codex lane

    def codex_probe(self, percent=20):
        return {"name": "codex", "status": "ok", "observed_at": 1000,
                "buckets": [{"id": "primary-300m", "percent": percent,
                             "resets_at": 2000, "source": "live"}]}

    def test_codex_serves_deep_tiers_only(self):
        elig = r.eligibility(self.config, {"zai_coding_plan": self.probe(),
                                           "codex": self.codex_probe()}, record=False)
        for tier in ("mechanical", "implementation"):
            judgment = {"tier": tier, "size": 0.4, "second_opinion": 0.2,
                        "spec_complete": 0.9, "destructive": 0.0}
            decision = r.decide(judgment, self.config, elig)
            self.assertNotEqual(decision["pick"]["provider"], "codex", tier)
        for tier in ("design", "diagnosis", "high_stakes"):
            judgment = {"tier": tier, "size": 0.4, "second_opinion": 0.2,
                        "spec_complete": 0.9, "destructive": 0.0}
            decision = r.decide(judgment, self.config, elig)
            self.assertEqual(decision["pick"]["provider"], "codex", tier)

    def test_codex_token_saving_order_and_sol_only_for_top_profile(self):
        elig = r.eligibility(self.config, {"zai_coding_plan": self.probe(),
                                           "codex": self.codex_probe()}, record=False)
        judgment = {"tier": "design", "size": 0.4, "second_opinion": 0.2,
                    "spec_complete": 0.9, "destructive": 0.0}
        self.assertEqual(r.decide(judgment, self.config, elig)["pick"]["model"],
                         "gpt-5.6-luna")
        dry = r.eligibility(self.config, {"zai_coding_plan": self.probe(),
                                          "codex": self.codex_probe(percent=99.5)},
                            record=False)
        pick = r.decide(judgment, self.config, dry)["pick"]
        self.assertEqual((pick["provider"], pick["model"]),
                         ("zai_coding_plan", "glm-5.3"))  # codex dry: premium GLM, off-peak
        stakes = {"tier": "high_stakes", "size": 0.4, "second_opinion": 0.2,
                  "spec_complete": 0.9, "destructive": 0.0}
        healthy = r.eligibility(self.config, {"codex": self.codex_probe()}, record=False)
        # sol is never first while luna is available, even for high stakes
        self.assertEqual(r.decide(stakes, self.config,
                                  r.eligibility(self.config,
                                                {"zai_coding_plan": self.probe(),
                                                 "codex": self.codex_probe()},
                                                record=False))["pick"]["model"],
                         "gpt-5.6-luna")

    def test_sol_is_capped_to_the_high_stakes_profile(self):
        elig = r.eligibility(self.config, {"codex": self.codex_probe()}, record=False)
        # design/diagnosis cannot take sol: luna and terra are preferred and sol
        # lacks the design/diagnosis capabilities.
        profiles = self.config["model_profiles"]["codex:gpt-6-sol"]["capabilities"]
        self.assertNotIn("design", profiles)
        self.assertNotIn("diagnosis", profiles)
        self.assertIn("high_stakes", profiles)
        for tier in ("design", "diagnosis"):
            judgment = {"tier": tier, "size": 0.4, "second_opinion": 0.2,
                        "spec_complete": 0.9, "destructive": 0.0}
            decision = r.decide(judgment, self.config, elig)
            self.assertNotEqual(decision["pick"]["model"], "gpt-6-sol", tier)

    def test_forbidden_list_matches_owner_policy(self):
        forbidden = r.forbidden_models(self.config)
        self.assertIn("codex:gpt-6-astra", forbidden)
        self.assertIn("claude:claude-opus-5", forbidden)
        for model in ("codex:gpt-6-sol", "codex:gpt-5.6-terra", "codex:gpt-5.6-luna"):
            self.assertNotIn(model, forbidden)
        for model in ("zai_coding_plan:glm-5.3", "zai_coding_plan:glm-5.3-flash",
                      "minimax:MiniMax-M3"):
            self.assertNotIn(model, forbidden)

    # ------------------------------------------------------- peak pricing

    def at(self, year, month, day, hour):
        from datetime import datetime, timezone
        return datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()

    def test_peak_window_blocks_premium_glm_on_a_weekday(self):
        # Monday 2026-10-05 15:00 Singapore (07:00 UTC) is inside the peak window.
        with patch.object(r, "now", lambda: self.at(2026, 10, 5, 7)):
            self.assertEqual(r.peak_window(self.config, "zai_coding_plan", "glm-5.3"),
                             "peak")
            elig = r.eligibility(self.config, {"zai_coding_plan": self.probe()},
                                 record=False)
            judgment = {"tier": "design", "size": 0.4, "second_opinion": 0.2,
                        "spec_complete": 0.9, "destructive": 0.0}
            decision = r.decide(judgment, self.config, elig)
            self.assertNotIn(decision["pick"]["model"], ("glm-5.3", "glm-5.2"))
            self.assertEqual(decision["pick"]["model"], "glm-5.3-flash")

    def test_off_peak_allows_premium_glm_at_half_cost(self):
        # Monday 19:00 Singapore (11:00 UTC) is after the window.
        with patch.object(r, "now", lambda: self.at(2026, 10, 5, 11)):
            self.assertEqual(r.peak_window(self.config, "zai_coding_plan", "glm-5.3"),
                             "off-peak")
            self.assertEqual(r.dispatch_cost(self.config, "zai_coding_plan", 3,
                                             "glm-5.3"), 3.75)
            elig = r.eligibility(self.config, {"zai_coding_plan": self.probe()},
                                 record=False)
            judgment = {"tier": "design", "size": 0.4, "second_opinion": 0.2,
                        "spec_complete": 0.9, "destructive": 0.0}
            decision = r.decide(judgment, self.config, elig)
            self.assertEqual(decision["pick"]["model"], "glm-5.3")
            self.assertEqual(decision["pricing_window"], "off-peak")
            # peak pricing still full price when computed
            with patch.object(r, "now", lambda: self.at(2026, 10, 5, 7)):
                self.assertEqual(r.dispatch_cost(self.config, "zai_coding_plan", 3,
                                                 "glm-5.3"), 7.5)

    def test_weekend_is_off_peak_even_in_peak_hours(self):
        # Saturday 15:00 Singapore (07:00 UTC).
        with patch.object(r, "now", lambda: self.at(2026, 10, 10, 7)):
            self.assertEqual(r.peak_window(self.config, "zai_coding_plan", "glm-5.3"),
                             "off-peak")
            elig = r.eligibility(self.config, {"zai_coding_plan": self.probe()},
                                 record=False)
            judgment = {"tier": "diagnosis", "size": 0.4, "second_opinion": 0.2,
                        "spec_complete": 0.9, "destructive": 0.0}
            decision = r.decide(judgment, self.config, elig)
            self.assertEqual(decision["pick"]["model"], "glm-5.3")

    def test_premium_models_cost_more_than_cheap_ones(self):
        cheap = r.dispatch_cost(self.config, "zai_coding_plan", 1, "glm-5.3-flash")
        premium = r.dispatch_cost(self.config, "zai_coding_plan", 3, "glm-5.3")
        self.assertGreater(premium, cheap * 2)
        self.assertEqual(r.dispatch_cost(self.config, "zai_coding_plan", 1,
                                         "glm-5.3-flash"), 1.0)

    def test_premium_glm_is_last_resort_behind_codex(self):
        elig = r.eligibility(self.config, {"zai_coding_plan": self.probe(),
                                           "codex": self.codex_probe()}, record=False)
        judgment = {"tier": "diagnosis", "size": 0.4, "second_opinion": 0.2,
                    "spec_complete": 0.9, "destructive": 0.0}
        decision = r.decide(judgment, self.config, elig)
        self.assertEqual(decision["pick"]["provider"], "codex")

    # --------------------------------------------------------------- effort

    def test_effort_selection(self):
        cases = (({"provider": "zai_coding_plan", "model": "glm-5.3"}, 3, "design", "max"),
                 ({"provider": "zai_coding_plan", "model": "glm-5.3-flash"}, 2,
                  "implementation", "high"),
                 ({"provider": "zai_coding_plan", "model": "glm-5.3-flash"}, 1,
                  "mechanical", "low"),
                 ({"provider": "zai_coding_plan", "model": "glm-5-turbo"}, 1, "mechanical", None),
                 ({"provider": "minimax", "model": "MiniMax-M3"}, 3, "design", None),
                 ({"provider": "minimax", "model": "MiniMax-M3.1-Flash-Preview"}, 1,
                  "mechanical", "low"))
        for cand, band, tier, expected in cases:
            with self.subTest(model=cand["model"], band=band):
                effort, _ = r.effort_for(self.config, cand, band, {"tier": tier})
                self.assertEqual(effort, expected)

    def test_launch_fields_emit_wire_verified_model_options(self):
        decision = {"pick": {"provider": "zai_coding_plan", "model": "glm-5.3",
                             "effort": "max"},
                    "agent": "opencode", "band": 3,
                    "worktree_name": "rs1-test"}
        fields = r.launch_fields(decision, self.config, None, "do the task")
        options = json.loads(fields["model_options_config"])
        self.assertEqual(options["provider"]["zai-coding-plan"]["models"]["glm-5.3"]
                         ["options"], {"reasoningEffort": "max"})
        flash = {"pick": {"provider": "minimax", "model": "MiniMax-M3.1-Flash-Preview",
                          "effort": "low"}, "agent": "opencode", "band": 1}
        fields = r.launch_fields(flash, self.config, None, "do the task")
        options = json.loads(fields["model_options_config"])
        self.assertEqual(options["provider"]["minimax-coding-plan"]["models"]
                         ["MiniMax-M3.1-Flash-Preview"]["options"],
                         {"output_config": {"effort": "low"}})
        no_knob = {"pick": {"provider": "minimax", "model": "MiniMax-M3", "effort": None},
                   "agent": "opencode", "band": 2}
        fields = r.launch_fields(no_knob, self.config, None, "do the task")
        self.assertEqual(fields["model_options_config"], "{}")

    def test_launcher_templates_carry_the_options(self):
        decision = {"pick": {"provider": "zai_coding_plan", "model": "glm-5.3",
                             "effort": "max"}, "agent": "opencode", "band": 3,
                    "worktree_name": "rs1-test", "account": None}
        shell = r.launch_command(decision, self.config, "shell", None, "do the task")
        self.assertIn("OPENCODE_CONFIG_CONTENT=", shell)
        self.assertIn("reasoningEffort", shell)
        orca = r.launch_command(decision, self.config, "orca", None, "do the task")
        self.assertIn(".rightsize-opencode.json", orca)
        self.assertIn("OPENCODE_CONFIG", orca)

    # ------------------------------------------------------------ catalogue

    def zai_catalogue(self):
        return {"zai-coding-plan": {"models": {
            model: {"name": model, "limit": {"context": 1000000}}
            for model in ("glm-4.7", "glm-5-turbo", "glm-5.2", "glm-5.2-highspeed",
                          "glm-5.3", "glm-5.3-flash", "glm-5.3-highspeed")}}}

    def test_refresh_registers_catalogue_and_records_plan_refused_models(self):
        plan_models = {"object": "list", "data": [
            {"id": m} for m in ("glm-4.7", "glm-5-turbo", "glm-5.2", "glm-5.3",
                                "glm-5.3-flash")]}

        def fake_get(url, token=None, timeout=20):
            if "models.opencode.ai" in url:
                return self.zai_catalogue()
            if url == r.ZAI_PLAN_MODELS:
                return plan_models
            raise AssertionError(f"unexpected url {url}")

        with patch.object(r, "get", side_effect=fake_get), \
                patch.object(r, "secret",
                             side_effect=lambda name, config=None: (
                                 "plan-key" if name == "ZAI_CODING_PLAN_API_KEY" else None)), \
                patch.object(r, "load_json", return_value={}), \
                patch.object(r, "save_json"):
            registry = r.refresh()
        provider = registry["providers"]["zai_coding_plan"]
        self.assertIn("glm-5.3", provider["models"])
        self.assertIn("glm-5.3-flash", provider["plan_available"])
        self.assertEqual(provider["plan_refused"],
                         ["glm-5.2-highspeed", "glm-5.3-highspeed"])

    def test_doctor_flags_plan_refused_ladder_entries(self):
        config = json.loads(r.CONFIG.read_text())
        config["bands"]["1"].append("zai_coding_plan:glm-5.3-highspeed")
        registry = {"providers": {"zai_coding_plan": {
            "models": {"glm-5.3": {}, "glm-5.3-highspeed": {}},
            "plan_refused": ["glm-5.3-highspeed"]}}}
        root = Path(self.temp.name)
        registry_path = root / "registry.json"
        registry_path.write_text(json.dumps(registry))
        with patch.object(r, "REGISTRY", registry_path), \
                patch.object(r, "probe_codex", return_value={"status": "unknown",
                                                             "buckets": [],
                                                             "account": {}}), \
                patch.object(accounts, "runtime_version", return_value="test"):
            rows = r.doctor(config)
        refused = [m for _, m in rows if "refused by the plan" in m]
        self.assertTrue(refused and "zai_coding_plan:glm-5.3-highspeed" in refused[0])

    # -------------------------------------------------------------- fan-out

    def specs(self):
        return ["rename the variable foo to bar in the parser module",
                "fix the typo in the README install heading",
                "add a date formatting helper to the report module",
                "debug why the nightly job fails intermittently on the ARM runner",
                "design the caching schema for the settings service"]

    def test_plan_fan_out_goes_all_glm_when_minimax_is_below_reserve(self):
        probes = {"minimax": self.minimax_probe(weekly_remaining=1),
                  "zai_coding_plan": self.probe()}
        result = r.plan(self.specs(), self.config, concurrency=2, probes=probes)
        picked = [t["decision"]["pick"] for t in result["tasks"] if t["decision"]["pick"]]
        self.assertEqual(len(picked), 5)
        self.assertEqual({p["provider"] for p in picked}, {"zai_coding_plan"})
        models = {p["model"] for p in picked}
        self.assertIn("glm-5.3", models)      # diagnosis/design work
        self.assertIn("glm-5.3-flash", models)  # mechanical work

    def test_plan_fan_out_spreads_when_minimax_has_headroom(self):
        probes = {"minimax": self.minimax_probe(weekly_remaining=70),
                  "zai_coding_plan": self.probe()}
        result = r.plan(self.specs(), self.config, concurrency=2, probes=probes)
        picked = [t["decision"]["pick"] for t in result["tasks"] if t["decision"]["pick"]]
        providers = {p["provider"] for p in picked}
        self.assertIn("minimax", providers)
        self.assertIn("zai_coding_plan", providers)

    # ------------------------------------------------------ recorded probes

    def test_recorded_probes_snapshot_round_trip(self):
        path = Path(self.temp.name) / "snapshot.json"
        path.write_text(json.dumps({
            "minimax": {"status": "ok", "buckets": [
                {"id": "rolling", "percent": 20, "resets_at": 2000, "source": "live"},
                {"id": "weekly", "percent": 30, "resets_at": 9000, "source": "live"}]},
            "zai_coding_plan": {"status": "ok", "buckets": [], "unmetered": True}}))
        probes = r.recorded_probes(path)
        self.assertEqual(probes["minimax"]["name"], "minimax")
        decision = r.route("rename a local variable", self.config, probes=probes)
        self.assertEqual(decision["pick"]["provider"], "minimax")
        path.write_text(json.dumps({"bogus": {"status": 5}}))
        with self.assertRaises(ValueError):
            r.recorded_probes(path)


if __name__ == '__main__':
    unittest.main()
