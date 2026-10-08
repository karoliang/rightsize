#!/usr/bin/env python3
"""Forbidden models must be unselectable. Run: python3 test_model_policy.py

karoliang/rightsize#32. Two defects are pinned here and neither is subtle once
written down. A spec that said "Astra must not be selected for routine coding,
building, retries or fallback" routed to Astra, because prose is input to the
judgment and never reaches selection. And `rerun --previous codex:gpt-6-astra`
kept Astra out of the implementer ladder and then handed it the review slot,
because the exclusion covered one role.

The case that matters most is the last one: when policy empties the eligible
set, the router must block and say so. The failure to test for is not "does it
block" but "does it quietly substitute something else", so both directions are
asserted every time.
"""
import copy
import contextlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import rightsize as r

ASTRA = "codex:gpt-6-astra"
SOL = "codex:gpt-6-sol"
LUNA = "codex:gpt-5.6-luna"
OPUS = "claude:claude-opus-5"
GLM_FORBIDDEN = tuple(f"zai_coding_plan:{model}" for model in (
    "glm-5.3", "glm-5.2", "glm-5-turbo", "glm-4.7",
    "glm-5.3-highspeed", "glm-5.2-highspeed"))
GLM_REASON = "owner 2026-10-08: GLM limited to glm-5.3-flash"
NECESSITY = ("the failing transport handshake only reproduces under Astra's ultra"
             " effort level, which no other profiled model exposes")


def probe(percent):
    return {"status": "ok", "buckets": [
        {"id": "weekly", "percent": percent, "resets_at": r.now() + 20 * 3600,
         "source": "live"}]}


class ModelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(r.CONFIG.read_text())
        self.assertIn(ASTRA, r.forbidden_models(self.config),
                      "config.json must ship the policy these tests describe")
        state = patch.object(r, 'STATE', Path(self.temp()) / 'state.json')
        state.start()
        self.addCleanup(state.stop)
        # Premium GLM eligibility depends on the vendor pricing window, so the
        # clock is frozen on a past Saturday (deterministic off-peak).
        from datetime import datetime, timezone
        saturday = datetime(2026, 9, 26, 7, tzinfo=timezone.utc).timestamp()
        clock = patch.object(r, 'now', lambda: saturday)
        clock.start()
        self.addCleanup(clock.stop)

    def temp(self):
        import tempfile
        box = tempfile.TemporaryDirectory()
        self.addCleanup(box.cleanup)
        return box.name

    # -- fixtures ---------------------------------------------------------
    def judgment(self, tier='design', **changes):
        return {'tier': tier, 'size': 0.5, 'second_opinion': 0,
                'spec_complete': 1, 'destructive': 0, **changes}

    def eligibility(self, usable=None, blocked=()):
        """Synthetic headroom. `usable` sets per-provider room in points."""
        usable = usable or {}
        out = {}
        for name in ('opencode', 'codex', 'claude', 'minimax', 'zai_coding_plan'):
            out[name] = {'eligible': name not in blocked,
                         'blocked': 'no-credential' if name in blocked else None,
                         'usable': usable.get(name, 80), 'unknown': False,
                         'overrun': False, 'resets_at': r.now() + 3600,
                         'bucket': 'rolling', 'inflight': 0, 'reserved': 0,
                         'buckets': []}
        return out

    def without_policy(self, config=None):
        """The same config with the policy section gone: the defect, on demand."""
        bare = copy.deepcopy(config or self.config)
        bare.pop("model_policy")
        return bare

    def load_overlay(self, overlay):
        """Load a repo file from a nested worktree over the owner config."""
        root = Path(self.temp())
        owner = root / 'config.json'
        owner.write_text(json.dumps(self.config))
        repo = root / 'repo'
        worktree = repo / 'nested'
        worktree.mkdir(parents=True)
        local = repo / '.rightsize.json'
        local.write_text(json.dumps(overlay))
        with patch.object(r, 'CONFIG', owner), contextlib.chdir(worktree):
            loaded, found = r.load_config()
        self.assertEqual(found, local.resolve())
        return loaded

    def picked(self, decision):
        pick = decision["pick"]
        return f"{pick['provider']}:{pick['model']}" if pick else None

    def reviewer(self, decision):
        review = decision["review"]
        return f"{review['provider']}:{review['model']}" if review else None

    # -- repo policy overlays ---------------------------------------------
    def test_repo_overlay_cannot_clear_or_shrink_owner_policy(self):
        owner_reason = r.forbidden_models(self.config)[ASTRA]
        for overlay in ({'model_policy': {'forbidden': {}}},
                        {'model_policy': {'forbidden': []}},
                        {'model_policy': None},
                        {'model_policy': {'forbidden': {OPUS: 'repo exclusion'}}}):
            with self.subTest(overlay=overlay):
                loaded = self.load_overlay(overlay)
                self.assertEqual(r.forbidden_models(loaded)[ASTRA], owner_reason)
                decision = r.decide(self.judgment(), loaded,
                                    self.eligibility({'codex': 999, 'claude': 90,
                                                      'opencode': 10}))
                self.assertNotEqual(self.picked(decision), ASTRA)

    def test_repo_overlay_can_add_forbidden_models_without_replacing_owner_policy(self):
        owner_reason = r.forbidden_models(self.config)[ASTRA]
        for forbidden in ({OPUS: 'repo exclusion', ASTRA: 'spoofed reason'}, [OPUS]):
            with self.subTest(forbidden=forbidden):
                loaded = self.load_overlay({'model_policy': {'forbidden': forbidden}})
                barred = r.forbidden_models(loaded)
                self.assertEqual(barred[ASTRA], owner_reason)
                self.assertIn(OPUS, barred)
                decision = r.decide(self.judgment(), loaded,
                                    self.eligibility({'codex': 999, 'claude': 90,
                                                      'opencode': 10}))
                self.assertNotIn(self.picked(decision), (ASTRA, OPUS))

    # -- the filter, not the prose ----------------------------------------
    def test_spec_prose_is_not_a_constraint_but_config_policy_is(self):
        """The repro from #32, both halves of it.

        Without the policy entry the router picks Astra first out of band 3 no
        matter what the brief says, which is the defect. With it, the same
        judgment and the same headroom pick the permitted model instead.
        """
        room = {'codex': 999, 'claude': 90, 'opencode': 10, 'minimax': 10}
        first = copy.deepcopy(self.config)
        first['bands']['3'] = [ASTRA, LUNA]
        loose = r.decide(self.judgment(), self.without_policy(first), self.eligibility(room))
        self.assertEqual(self.picked(loose), ASTRA)

        strict = r.decide(self.judgment(), first, self.eligibility(room))
        self.assertEqual(self.picked(strict), LUNA)
        self.assertEqual(strict["band"], 3)
        self.assertEqual(strict["quality_floor"], 3)
        self.assertIn(ASTRA, strict["policy"]["enforced"])
        self.assertTrue(any("forbidden by model policy" in n and ASTRA in n
                            for n in strict["notes"]))

    def test_every_non_flash_glm_is_forbidden_with_owner_reason(self):
        forbidden = r.forbidden_models(self.config)
        for model in GLM_FORBIDDEN:
            with self.subTest(model=model):
                self.assertEqual(self.config["model_policy"]["forbidden"][model],
                                 GLM_REASON)
                self.assertIn(GLM_REASON, forbidden[model])

    def test_forbidden_glm_ids_never_route(self):
        c = copy.deepcopy(self.config)
        for band in ("1", "2", "3"):
            c["bands"][band] = list(GLM_FORBIDDEN) + [
                "zai_coding_plan:glm-5.3-flash"]
        d = r.decide(self.judgment("design"), c, self.eligibility({
            "zai_coding_plan": 999, "codex": 999}))
        self.assertEqual(self.picked(d), "zai_coding_plan:glm-5.3-flash")

    def test_every_tier_and_band_is_covered(self):
        for tier in ('mechanical', 'implementation', 'design', 'diagnosis', 'high_stakes'):
            with self.subTest(tier=tier):
                d = r.decide(self.judgment(tier), self.config,
                             self.eligibility({'codex': 999}))
                self.assertNotEqual(self.picked(d), ASTRA)
                self.assertNotEqual(self.reviewer(d), ASTRA)

    def test_band_fallback_cannot_reach_a_forbidden_model(self):
        """Astra must not arrive as the escalation target either.

        Band 1 and band 2 have no task-qualified candidates, so the climb
        reaches band 3, where the only candidate is forbidden. That has to end
        in a block, not in the forbidden model arriving by a different door.
        """
        c = copy.deepcopy(self.config)
        c['bands']['1'] = []
        c['bands']['2'] = []
        c['bands']['3'] = [ASTRA]
        d = r.decide(self.judgment('implementation'), c,
                     self.eligibility({'codex': 999}, blocked=('opencode', 'claude', 'minimax')))
        self.assertIsNone(d['pick'])
        self.assertIn('model policy forbids', d['blocked'])
        self.assertIn(ASTRA, d['blocked'])
        self.assertEqual(d['quality_floor'], 1, "the floor is reported as it was, not raised")

    # -- the review ladder ------------------------------------------------
    def test_rerun_excludes_the_previous_model_from_review_as_well(self):
        """The follow-up finding: `--previous` reached one ladder, not both."""
        judgment = self.judgment('design', second_opinion=0.7)
        with patch.object(r, 'judge', return_value=judgment):
            decision = r.rerun("re-route the exclusion work", "it picked the forbidden model",
                               ASTRA, self.config,
                               probes={'codex': probe(5), 'claude': probe(40),
                                       'opencode': {'status': 'no-credential', 'buckets': []},
                                       'minimax': {'status': 'no-credential', 'buckets': []},
                                       'zai_coding_plan': {'status': 'ok', 'buckets': [],
                                                           'unmetered': True}})
        self.assertEqual(self.picked(decision), LUNA)
        self.assertNotEqual(self.reviewer(decision), ASTRA)
        self.assertEqual(self.reviewer(decision), 'zai_coding_plan:glm-5.3-flash')
        self.assertNotEqual(self.picked(decision), self.reviewer(decision))
        self.assertTrue(any(n.startswith("review:") and "glm-5.3-flash" in n and "chosen" in n
                            for n in decision["notes"]))
        self.assertIn(ASTRA, decision["policy"]["enforced"])
        self.assertTrue(decision["review_required"])

    def test_review_ladder_refuses_a_forbidden_model_even_with_room(self):
        c = copy.deepcopy(self.config)
        c['bands']['3'] = [ASTRA, LUNA, OPUS]
        d = r.decide(self.judgment('high_stakes'), c,
                     self.eligibility({'claude': 90, 'codex': 10}))
        self.assertEqual(self.picked(d), LUNA)
        self.assertIsNone(self.reviewer(d))
        loose = r.decide(self.judgment('high_stakes'), self.without_policy(c),
                         self.eligibility({'claude': 90, 'codex': 10}))
        # Same shape without the policy: the reviewer slot is exactly the hole.
        self.assertEqual(self.picked(loose), OPUS)
        self.assertEqual(self.reviewer(loose), ASTRA)

    # -- the empty eligible set ------------------------------------------
    def test_all_candidates_excluded_blocks_and_reports(self):
        """Blocks, and does not substitute. Both directions, every time."""
        c = copy.deepcopy(self.config)
        c['bands']['3'] = [ASTRA]
        d = r.decide(self.judgment('design'), c, self.eligibility(blocked=('opencode',)))
        self.assertIsNone(d['pick'], "a forbidden model must never be the pick")
        self.assertIsNotNone(d['blocked'], "an empty eligible set must block, not go quiet")
        self.assertIn(ASTRA, d['blocked'])
        self.assertIn("Nothing was substituted", d['blocked'])
        # And nothing cheaper crept in behind the block.
        self.assertEqual(d['band'], 3)
        self.assertEqual(d['quality_floor'], 3)
        self.assertIsNone(d['review'])
        for level in ('1', '2'):
            for candidate in c['bands'][level]:
                self.assertNotIn(r.candidate_key(candidate), json.dumps(d['pick'] or {}))

    def test_capacity_shortage_is_still_not_a_policy_block(self):
        """A forbidden ladder entry does not turn a permitted model's quota into policy."""
        c = copy.deepcopy(self.config)
        c['bands']['3'] = [ASTRA, LUNA]
        d = r.decide(self.judgment('design'), c, self.eligibility(blocked=('codex',)))
        self.assertIsNone(d['pick'])
        self.assertIsNone(d['blocked'])

    def test_plan_reports_a_policy_block_as_undispatchable_not_as_no_capacity(self):
        c = copy.deepcopy(self.config)
        c['bands']['3'] = [ASTRA]
        judgment = self.judgment('design')
        with patch.object(r, 'judge', return_value=judgment):
            result = r.plan(["design the exclusion contract"], c,
                            probes={'codex': probe(5), 'claude': probe(5),
                                    'opencode': {'status': 'no-credential', 'buckets': []},
                                    'minimax': {'status': 'no-credential', 'buckets': []}})
        self.assertEqual(result['blocked'], [0])
        self.assertEqual(result['unplaced'], [], "a forbidden model is not a capacity problem")
        self.assertIsNone(result['tasks'][0]['decision']['pick'])
        self.assertIn(ASTRA, result['tasks'][0]['decision']['blocked'])

    def test_plan_waits_for_permitted_capacity_instead_of_reporting_policy_block(self):
        c = copy.deepcopy(self.config)
        c['bands']['3'] = [ASTRA, LUNA]
        c['max_inflight']['codex'] = 1
        with patch.object(r, 'judge', return_value=self.judgment('design')):
            result = r.plan(['first design task', 'second design task'], c, max_waves=2,
                            probes={'codex': probe(5),
                                    'claude': {'status': 'no-credential', 'buckets': []},
                                    'opencode': {'status': 'no-credential', 'buckets': []},
                                    'minimax': {'status': 'no-credential', 'buckets': []},
                                    'zai_coding_plan': {'status': 'no-credential',
                                                        'buckets': []}})
        self.assertEqual(result['blocked'], [])
        self.assertEqual(result['unplaced'], [])
        self.assertEqual([task['wave'] for task in result['tasks']], [1, 2])
        self.assertEqual([self.picked(task['decision']) for task in result['tasks']],
                         [LUNA, LUNA])

    def test_route_enforces_the_policy_end_to_end(self):
        judgment = self.judgment('design', second_opinion=0.7)
        with patch.object(r, 'judge', return_value=judgment):
            decision = r.route("design the exclusion contract", self.config,
                               probes={'codex': probe(5), 'claude': probe(40),
                                       'opencode': {'status': 'no-credential', 'buckets': []},
                                       'minimax': {'status': 'no-credential', 'buckets': []},
                                       'zai_coding_plan': {'status': 'ok', 'buckets': [],
                                                           'unmetered': True}})
        self.assertEqual(self.picked(decision), LUNA)
        self.assertNotEqual(self.reviewer(decision), ASTRA)
        self.assertEqual(self.reviewer(decision), 'zai_coding_plan:glm-5.3-flash')
        self.assertNotEqual(self.picked(decision), self.reviewer(decision))

    # -- the exception contract ------------------------------------------
    def test_exception_needs_a_real_model_and_a_specific_necessity(self):
        for necessity in (None, "", "required", "needed.", "it is required",
                          "astra is better here"):
            with self.subTest(necessity=necessity):
                with self.assertRaises(ValueError):
                    r.policy_exception(self.config, ASTRA, necessity)
        with self.assertRaises(ValueError):
            r.policy_exception(self.config, LUNA, NECESSITY)
        granted = r.policy_exception(self.config, ASTRA, NECESSITY, "karoliang")
        self.assertEqual(granted['model'], ASTRA)
        self.assertEqual(granted['necessity'], NECESSITY)
        self.assertTrue(granted['approved'])
        self.assertIn("owner policy", granted['policy_reason'])

    def test_unapproved_exception_is_recorded_escalated_and_blocked(self):
        c = copy.deepcopy(self.config)
        c['bands']['3'] = [ASTRA]
        pending = r.policy_exception(self.config, ASTRA, NECESSITY)
        self.assertFalse(pending['approved'])
        d = r.decide(self.judgment('design'), c,
                     self.eligibility({'codex': 999},
                                      blocked=('opencode', 'claude', 'minimax',
                                               'zai_coding_plan')), exception=pending)
        self.assertEqual(self.picked(d), ASTRA)
        self.assertIsNotNone(d['blocked'], "an unapproved exception must not be dispatchable")
        self.assertEqual(d['policy']['exception']['status'], 'pending')
        self.assertEqual(d['policy']['exception']['necessity'], NECESSITY)
        self.assertTrue(any("escalated" in reason for reason in d['reasons']))

    def test_approved_exception_dispatches_and_records_its_necessity(self):
        c = copy.deepcopy(self.config)
        c['bands']['3'] = [ASTRA]
        granted = r.policy_exception(self.config, ASTRA, NECESSITY, "karoliang")
        d = r.decide(self.judgment('design'), c,
                     self.eligibility({'codex': 999},
                                      blocked=('opencode', 'claude', 'minimax',
                                               'zai_coding_plan')), exception=granted)
        self.assertEqual(self.picked(d), ASTRA)
        self.assertIsNone(d['blocked'])
        self.assertEqual(d['policy']['exception']['status'], 'approved')
        self.assertEqual(d['policy']['exception']['approved_by'], 'karoliang')
        self.assertTrue(any(NECESSITY in reason for reason in d['reasons']))

    def test_an_exception_never_lifts_the_review_role(self):
        """The necessity was recorded for the work, not for the opinion on it."""
        c = copy.deepcopy(self.config)
        c['bands']['3'] = [LUNA, ASTRA, OPUS]
        granted = r.policy_exception(c, ASTRA, NECESSITY, "karoliang")
        d = r.decide(self.judgment('high_stakes'), c,
                     self.eligibility({'claude': 90, 'codex': 10}), exception=granted)
        self.assertEqual(self.picked(d), LUNA)
        self.assertIsNone(self.reviewer(d))
        self.assertEqual(d['policy']['exception']['status'], 'unused')
        self.assertEqual(d['policy']['review_forbidden'], [OPUS])

    def test_the_exception_is_kept_in_the_decision_log(self):
        """An excluded model that ran anyway has to be answerable for later."""
        c = copy.deepcopy(self.config)
        c['bands']['3'] = [ASTRA]
        granted = r.policy_exception(self.config, ASTRA, NECESSITY, "karoliang")
        d = r.decide(self.judgment('design'), c,
                     self.eligibility({'codex': 999},
                                      blocked=('opencode', 'claude', 'minimax',
                                               'zai_coding_plan')), exception=granted)
        r.log_decision(d, "a task that genuinely needs the excluded model",
                       config=self.config)
        entry = (r.load_json(r.STATE, {}) or {})["decisions"][-1]
        self.assertEqual(entry["policy_exception"]["necessity"], NECESSITY)
        self.assertEqual(entry["policy_exception"]["approved_by"], "karoliang")
        self.assertEqual(entry["policy_exception"]["status"], "approved")

    def test_exception_flags_are_refused_without_the_model(self):
        class Args:
            require_model = None
            necessity = NECESSITY
            exception_approved_by = None
        exception, refused = r.exception_from_args(Args(), self.config)
        self.assertIsNone(exception)
        self.assertIn("--require-model", refused)

    # -- the exclusion set itself -----------------------------------------
    def test_a_bare_set_keeps_the_retry_wording(self):
        elig = self.eligibility()
        _, notes = r.pick([OPUS], elig, 3, {OPUS}, self.config)
        self.assertTrue(any("already had a go" in n for n in notes))
        _, reasoned = r.pick([OPUS], elig, 3, {OPUS: "forbidden by model policy: because"},
                             self.config)
        self.assertTrue(any("forbidden by model policy" in n for n in reasoned))

    # -- preflight --------------------------------------------------------
    def test_doctor_counts_permitted_candidates_per_band(self):
        c = copy.deepcopy(self.config)
        c['bands']['3'] = [ASTRA, LUNA]
        messages = [f"{level}: {text}" for level, text in r.doctor(c)]
        self.assertTrue(any("band 3 has one permitted candidate" in m for m in messages), messages)
        c['bands']['3'] = [ASTRA]
        messages = [f"{level}: {text}" for level, text in r.doctor(c)]
        self.assertTrue(any(m.startswith("error") and "no candidate permitted by model policy" in m
                            for m in messages), messages)

    def test_doctor_counts_only_providers_the_last_reading_could_use(self):
        """Only a permitted candidate on a usable provider counts as live."""
        c = copy.deepcopy(self.config)
        band = c['bands']['3']
        self.assertIn(ASTRA, band)
        forbidden = r.forbidden_models(c)
        permitted = [candidate for candidate in band
                     if r.candidate_key(candidate) not in forbidden]
        self.assertTrue(permitted)
        only_live = permitted[0]
        live_provider = r.parse_candidate(only_live)['provider']
        # Keep one permitted entry on the live provider and the entries on
        # other providers. Derive the fixture from the shipped band so a new
        # model on any provider cannot silently change this one-live case.
        c['bands']['3'] = [candidate for candidate in band
                           if r.candidate_key(candidate) in forbidden
                           or candidate == only_live
                           or r.parse_candidate(candidate)['provider'] != live_provider]
        providers = {r.parse_candidate(candidate)['provider']
                     for candidate in c['bands']['3']}
        self.assertGreater(len(providers), 1)
        cached = {provider: {"status": "no-credential"} for provider in providers}
        cached[live_provider] = {"status": "ok"}
        r.save_json(r.STATE, {"probe_cache": {"at": r.now(), "probes": cached}})
        messages = [f"{level}: {text}" for level, text in r.doctor(c)]
        self.assertTrue(any(m.startswith("warn") and only_live in m
                            and "one permitted candidate with a usable provider" in m
                            for m in messages), messages)
        # And with the permitted entry unusable too, that band blocks outright.
        cached[live_provider] = {"status": "no-credential"}
        r.save_json(r.STATE, {"probe_cache": {"at": r.now(), "probes": cached}})
        messages = [f"{level}: {text}" for level, text in r.doctor(c)]
        self.assertTrue(any(m.startswith("error") and "none of them had a usable provider" in m
                            for m in messages), messages)

    def test_doctor_rerun_gap_checks_higher_bands_and_keeps_terminal_warning(self):
        c = copy.deepcopy(self.config)
        c['bands'] = {'1': [OPUS], '2': [SOL], '3': [SOL]}
        c['review_ladder'] = [OPUS, 'opencode:kimi-k2.6']
        cached = {provider: {'status': 'no-credential'}
                  for provider in ('claude', 'codex', 'opencode')}
        cached['claude'] = cached['codex'] = cached['opencode'] = {'status': 'ok'}
        r.save_json(r.STATE, {'probe_cache': {'at': r.now(), 'probes': cached}})

        messages = [text for level, text in r.doctor(c) if level == 'warn']
        self.assertFalse(any(text.startswith('band 1 has one permitted candidate')
                             and 'higher band' in text for text in messages), messages)
        # SOL is the previous candidate everywhere, so band 3 cannot absorb
        # the retry. The former false-green assertion expected this to stay
        # silent merely because band 3 contained SOL.
        self.assertTrue(any(text.startswith('band 2 has one permitted candidate')
                            and 'higher band' in text for text in messages), messages)
        self.assertTrue(any(text.startswith('band 3 has one permitted candidate')
                            and 'higher band' in text for text in messages), messages)

    def test_doctor_reviewer_uses_review_ladder_only_for_band_one(self):
        c = copy.deepcopy(self.config)
        c['bands'] = {'1': [OPUS], '2': [SOL], '3': [SOL]}
        c['review_ladder'] = [OPUS, 'opencode:kimi-k2.6']
        cached = {provider: {'status': 'no-credential'}
                  for provider in ('claude', 'codex', 'opencode')}
        cached['claude'] = cached['codex'] = cached['opencode'] = {'status': 'ok'}
        r.save_json(r.STATE, {'probe_cache': {'at': r.now(), 'probes': cached}})

        messages = [text for level, text in r.doctor(c) if level == 'warn']
        self.assertFalse(any(text.startswith('band 1 has one permitted candidate')
                             and 'reviewer' in text for text in messages), messages)
        self.assertTrue(any(text.startswith('band 2 has one permitted candidate')
                            and 'reviewer' in text for text in messages), messages)
        self.assertTrue(any(text.startswith('band 3 has one permitted candidate')
                            and 'reviewer' in text for text in messages), messages)

    def test_doctor_rerun_gap_warns_for_nonterminal_band_without_usable_higher_band(self):
        c = copy.deepcopy(self.config)
        c['bands'] = {'1': [OPUS], '2': [SOL], '3': [ASTRA]}
        c['review_ladder'] = [OPUS, 'opencode:kimi-k2.6']
        cached = {provider: {'status': 'no-credential'}
                  for provider in ('claude', 'codex', 'opencode')}
        cached['claude'] = cached['codex'] = cached['opencode'] = {'status': 'ok'}
        r.save_json(r.STATE, {'probe_cache': {'at': r.now(), 'probes': cached}})

        messages = [text for level, text in r.doctor(c) if level == 'warn']
        self.assertTrue(any(text.startswith('band 2 has one permitted candidate')
                            and 'higher band' in text for text in messages), messages)

    def test_doctor_reviewer_warns_when_only_reviewer_is_the_implementer(self):
        c = copy.deepcopy(self.config)
        c['bands'] = {'1': ['minimax:MiniMax-M3']}
        c['review_ladder'] = ['minimax:MiniMax-M3']
        cached = {'minimax': {'status': 'ok'}}
        r.save_json(r.STATE, {'probe_cache': {'at': r.now(), 'probes': cached}})

        messages = [text for level, text in r.doctor(c) if level == 'warn']
        self.assertTrue(any(text.startswith('band 1 has one permitted candidate')
                            and 'reviewer' in text for text in messages), messages)

    def test_doctor_reviewer_accepts_one_distinct_reviewer(self):
        c = copy.deepcopy(self.config)
        c['bands'] = {'1': ['minimax:MiniMax-M3']}
        c['review_ladder'] = ['zai_coding_plan:glm-5.3-flash']
        cached = {'minimax': {'status': 'ok'}, 'zai_coding_plan': {'status': 'ok'}}
        r.save_json(r.STATE, {'probe_cache': {'at': r.now(), 'probes': cached}})

        messages = [text for level, text in r.doctor(c) if level == 'warn']
        self.assertFalse(any(text.startswith('band 1 has one permitted candidate')
                             and 'reviewer' in text for text in messages), messages)


if __name__ == '__main__':
    unittest.main(verbosity=2)
