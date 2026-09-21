"""Task requirements must survive quota pressure and model effort differences."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rightsize as r
import replay


class TaskFitTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(r.CONFIG.read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(r, 'STATE', Path(self.temp.name) / 'state.json')
        p.start()
        self.addCleanup(p.stop)

    def judgment(self, tier='implementation', **changes):
        return {'tier':tier, 'size':0.5, 'second_opinion':0,
                'spec_complete':1, 'destructive':0, **changes}

    def eligibility(self, providers=('opencode', 'codex', 'claude', 'minimax')):
        return {p:{'eligible':True, 'blocked':None, 'usable':80, 'unknown':False,
                   'overrun':False, 'resets_at':r.now()+3600,
                   'bucket':'rolling', 'inflight':0, 'reserved':0,
                   'buckets':[]} for p in providers}

    def test_no_downgrade_for_high_quality_or_retry(self):
        c = copy.deepcopy(self.config)
        c['bands']['3'] = ['claude:claude-opus-5']
        e = self.eligibility(('opencode',))
        for tier in ('design', 'diagnosis', 'high_stakes'):
            with self.subTest(tier=tier):
                d = r.decide(self.judgment(tier), c, e)
                self.assertIsNone(d['pick'])
                self.assertEqual(d['quality_floor'], 3)
                self.assertEqual(d['band'], 3)
        d = r.decide(self.judgment(), c, e, floor_band=3, attempt=1)
        self.assertIsNone(d['pick'])
        self.assertEqual(d['quality_floor'], 3)

    def test_wrong_capability_and_unprofiled_models_cannot_win(self):
        c=copy.deepcopy(self.config)
        c['bands']['3']=['opencode:deepseek-v4.1-flash','opencode:unknown',
                         'claude:claude-opus-5']
        # Even relabelling a cheap model as band 3 does not grant task skills.
        c['model_profiles']['opencode:deepseek-v4.1-flash']['max_band']=3
        d=r.decide(self.judgment('high_stakes'),c,self.eligibility())
        self.assertEqual(d['pick']['provider'],'claude')
        self.assertTrue(any('missing task capabilities' in n for n in d['notes']))
        self.assertTrue(any('no model capability profile' in n for n in d['notes']))

    def test_explicit_unsupported_effort_disqualifies_candidate(self):
        c=copy.deepcopy(self.config)
        c['bands']['1']=['codex:gpt-5.6-luna:ultra']
        d=r.decide(self.judgment('mechanical'),c,self.eligibility(),fallback=False)
        self.assertIsNone(d['pick'])
        self.assertTrue(any('unsupported model effort' in n for n in d['notes']))

    def test_effort_is_task_specific_and_bumps_within_model_levels(self):
        c=copy.deepcopy(self.config)
        cand={'provider':'codex','model':'gpt-6-astra','effort':None}
        self.assertEqual(r.effort_for(c,cand,3,self.judgment('mechanical'))[0],'low')
        self.assertEqual(r.effort_for(c,cand,3,self.judgment('design'))[0],'high')
        c['model_profiles']['codex:gpt-6-astra']['efforts']=['low','high']
        effort,_=r.effort_for(c,cand,3,self.judgment('mechanical'),attempt=1)
        self.assertEqual(effort,'high')
        self.assertEqual(r.effort_for(c,cand,3,self.judgment('design'),attempt=8)[0],'high')
        self.assertEqual(r.effort_for(c,{'provider':'minimax','model':'MiniMax-M3'},2,
                                    self.judgment(),attempt=3)[0],None)

    def test_high_stakes_review_has_same_quality_floor(self):
        d=r.decide(self.judgment('high_stakes',second_opinion=1),self.config,
                   self.eligibility())
        self.assertIsNotNone(d['review'])
        self.assertEqual(d['review_band'],3)
        review=d['review']; profile=self.config['model_profiles'][f"{review['provider']}:{review['model']}"]
        self.assertIn('high_stakes',profile['capabilities'])
        self.assertNotEqual(review['provider'],d['pick']['provider'])

    def test_same_model_on_different_subscription_is_not_independent_review(self):
        c=copy.deepcopy(self.config)
        c['bands']['2']=['minimax:MiniMax-M3','opencode:minimax-m3']
        d=r.decide(self.judgment(size=2,second_opinion=1),c,
                   self.eligibility(('minimax','opencode')))
        self.assertIsNotNone(d['pick'])
        self.assertIsNone(d['review'])
        self.assertTrue(any('review remains outstanding' in n for n in d['notes']))

    def test_no_qualified_capacity_in_planner_stays_unplaced(self):
        c=copy.deepcopy(self.config)
        c['bands']['3']=['claude:claude-opus-5']
        probe={'opencode':{'name':'opencode','status':'ok','buckets':[
            {'id':'rolling','percent':0,'resets_at':r.now()+18000,'source':'live'}]}}
        with patch.object(r,'judge',return_value=self.judgment('high_stakes')):
            plan=r.plan(['sensitive task'],c,probes=probe)
        self.assertIsNone(plan['tasks'][0]['decision']['pick'])
        self.assertIsNone(plan['tasks'][0]['wave'])

    def test_planner_charges_review_at_task_quality_band(self):
        probes={p:{'name':p,'status':'ok','buckets':[
            {'id':'rolling','percent':0,'resets_at':r.now()+18000,'source':'live'}]}
            for p in ('codex','claude')}
        with patch.object(r,'judge',return_value=self.judgment('high_stakes',second_opinion=1)):
            plan=r.plan(['review sensitive change'],self.config,probes=probes)
        task=plan['tasks'][0]
        decision=task['decision']
        self.assertIsNotNone(decision['review'])
        expected=sum(r.dispatch_cost(self.config,p,3) for p in ('codex','claude'))
        self.assertAlmostEqual(task['points'],expected)

    def test_bad_task_effort_or_missing_requirements_refuses_candidate(self):
        c=copy.deepcopy(self.config)
        c['bands']['1']=['codex:gpt-5.6-luna']
        c['model_profiles']['codex:gpt-5.6-luna']['effort_by_task']['mechanical']='ultra'
        d=r.decide(self.judgment('mechanical'),c,self.eligibility(),fallback=False)
        self.assertIsNone(d['pick'])
        del c['task_profiles']['mechanical']
        d=r.decide(self.judgment('mechanical'),c,self.eligibility(),fallback=False)
        self.assertIsNone(d['pick'])

    def test_frozen_policy_uses_same_task_filters(self):
        judgment=self.judgment('design')
        e=self.eligibility()
        live=r.decide(judgment,self.config,e)
        frozen=replay.policy(r,r.now()).decide(judgment,self.config,e)
        self.assertEqual(live['pick'],frozen['pick'])
        self.assertEqual(live['quality_floor'],frozen['quality_floor'])


if __name__=='__main__':unittest.main()
