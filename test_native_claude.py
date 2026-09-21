"""Native Claude control/quota contracts using isolated synthetic subprocesses."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import accounts
import native_claude as c
import rightsize as r


IDENTITY = {"status": "ok", "quota_account_ref": accounts.digest("canonical-fixture"),
            "session_account_ref": accounts.digest(json.dumps(["claude", "fixture@example.invalid", "Fixture"]))}
USAGE = {"rate_limits_available": True, "rate_limits": {
    "five_hour": {"utilization": 20, "resets_at": "2030-01-01T00:00:00Z"},
    "seven_day": {"utilization": 30, "resets_at": "2030-01-02T00:00:00Z"},
    "model_scoped": [], "extra_usage": {"is_enabled": False}}}
FAKE = r'''
import json,sys,os,time
mode=os.environ.get('FAKE_MODE','ok')
for line in sys.stdin:
 request=json.loads(line)
 if request['type']=='control_response':
  assert request['response']['response']['behavior']=='deny'
  print(json.dumps({'type':'system','subtype':'denied-confirmed'}),flush=True)
  continue
 assert request['type']=='control_request', 'no user/model prompt permitted'
 kind=request['request']['subtype']
 if mode=='silent':
  print('{',end='',flush=True);time.sleep(30)
 if mode=='reject':
  print(json.dumps({'type':'control_response','response':{'subtype':'error','request_id':request['request_id'],'error':'SECRET_SENTINEL'}}),flush=True)
  continue
 if kind=='initialize':
  if mode=='permission':
   print(json.dumps({'type':'control_request','request_id':'permission-one','request':{'subtype':'can_use_tool','tool_name':'Bash'}}),flush=True)
  print(json.dumps({'type':'system','subtype':'setup-notification'}),flush=True)
  result={'account':{'email':'fixture@example.invalid','organization':'Fixture' if mode!='wrong-account' else 'Other','apiProvider':'firstParty'}}
 elif kind=='get_usage':
  assert request['request']['skip_behaviors'] is True
  result={'rate_limits_available':True,'rate_limits':{'five_hour':{'utilization':20,'resets_at':None},'seven_day':{'utilization':30,'resets_at':None},'model_scoped':[]}}
 else: result={}
 print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':request['request_id'],'response':result}}),flush=True)
'''


class ClaudeTests(unittest.TestCase):
    def transport(self, mode="ok"):
        children = []
        def factory(argv, **kwargs):
            self.assertNotIn("--bare", argv)
            self.assertIn("--safe-mode", argv)
            self.assertIn("--no-session-persistence", argv)
            child = c.Control([sys.executable, "-u", "-c", FAKE],
                              env={**kwargs["env"], "FAKE_MODE": mode}, cwd=kwargs["cwd"])
            children.append(child)
            return child
        return factory, children

    def test_probe_verifies_account_uses_no_prompt_and_reaps(self):
        with tempfile.TemporaryDirectory() as root:
            binding = accounts.select("claude", home=Path(root), environ={})
            for mode, expected in (("ok", "ok"), ("wrong-account", "account-changed")):
                factory, children = self.transport(mode)
                result = c.probe(binding, IDENTITY, transport=factory)
                self.assertEqual(result["status"], expected)
                self.assertIsNotNone(children[0].proc.returncode)
                self.assertTrue(children[0].proc.stdout.closed)
                self.assertTrue(children[0].proc.stdin.closed)
        with patch.object(c, "Control", side_effect=AssertionError("no process")):
            self.assertIsNone(c.probe(binding, {"status": "ok"}))

    def test_control_deadline_rejection_redaction_and_queued_events(self):
        for mode in ("silent", "reject", "permission"):
            rpc = c.Control([sys.executable, "-u", "-c", FAKE], env={**os.environ, "FAKE_MODE": mode})
            try:
                start = time.monotonic()
                if mode != "permission":
                    with self.assertRaises(c.ProtocolError) as error:
                        rpc.request("initialize", timeout=0.1)
                    self.assertNotIn("SECRET_SENTINEL", str(error.exception))
                    self.assertLess(time.monotonic() - start, 1)
                else:
                    rpc.request("initialize", timeout=1)
                    self.assertEqual(rpc.notification()["subtype"], "setup-notification")
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        message = rpc.notification()
                        if message:
                            self.assertEqual(message["subtype"], "denied-confirmed")
                            break
                    else:
                        self.fail("permission denial was not delivered")
            finally:
                rpc.close()
            self.assertIsNotNone(rpc.proc.returncode)

    def test_live_windows_and_usage_credits_are_distinct(self):
        result = c.quota(USAGE)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([b["percent"] for b in result["buckets"]], [20, 30])
        self.assertFalse(result["usage_credits_enabled"])
        modified = copy.deepcopy(USAGE)
        modified["rate_limits"]["extra_usage"] = {"is_enabled": True, "utilization": 100}
        result = c.quota(modified)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["usage_credits_enabled"])
        self.assertEqual(len(result["buckets"]), 2)

    def test_cached_or_partial_telemetry_cannot_claim_fresh_capacity(self):
        for mutate in (lambda d: d["rate_limits"].pop("model_scoped"),
                       lambda d: d["rate_limits"].pop("five_hour"),
                       lambda d: d["rate_limits"]["seven_day"].update(utilization=None),
                       lambda d: d["rate_limits"]["seven_day"].update(utilization=True),
                       lambda d: d["rate_limits"]["seven_day"].update(utilization=float("nan")),
                       lambda d: d["rate_limits"]["seven_day"].update(resets_at="bad"),
                       lambda d: d["rate_limits"].update(limits=[{"is_active": "yes"}])):
            usage = copy.deepcopy(USAGE)
            mutate(usage)
            result = c.quota(usage)
            self.assertEqual(result["status"], "unknown")
            previous = {"claude:weekly": {"percent": 1, "at": r.now()}}
            self.assertIsNone(r.headroom(result, 10, previous, "claude")["usable"])

    def test_denial_survives_missing_freshness_or_other_windows(self):
        for row in ({"utilization": 100, "resets_at": None},
                    {"utilization": None, "locked_reason": "provider-denied"}):
            usage = copy.deepcopy(USAGE)
            usage["rate_limits"].pop("model_scoped")
            usage["rate_limits"]["seven_day"] = row
            self.assertEqual(c.quota(usage)["status"], "denied")

    def test_model_and_additional_active_windows_cannot_disappear(self):
        usage = copy.deepcopy(USAGE)
        usage["rate_limits"]["model_scoped"] = [{"display_name": "Fixture", "utilization": 100, "resets_at": None}]
        self.assertEqual(c.quota(usage)["status"], "denied")
        usage["rate_limits"]["model_scoped"] = []
        usage["rate_limits"]["limits"] = [{"kind": "weekly", "group": "plan", "scope": None,
                                            "is_active": True, "percent": 100, "resets_at": None}]
        self.assertEqual(c.quota(usage)["status"], "denied")
        usage["rate_limits"]["limits"][0]["is_active"] = False
        self.assertEqual(c.quota(usage)["status"], "ok")

    def test_probe_prefers_native_and_rechecks_identity_before_return(self):
        native = c.quota(USAGE)
        with patch.object(accounts, "claude_identity", return_value=IDENTITY), \
                patch.object(c, "probe", return_value=native), \
                patch.object(r, "claude_tokens", side_effect=AssertionError("no transcript estimate")):
            self.assertEqual(r.probe_claude({})["quota_source"], "native-control")
        with patch.object(accounts, "claude_identity", side_effect=[IDENTITY, {"status": "unknown"}]), \
                patch.object(c, "probe", return_value=native):
            self.assertEqual(r.probe_claude({})["status"], "account-changed")

    def test_unsupported_control_keeps_explicit_estimate_label(self):
        with patch.object(accounts, "claude_identity", return_value=IDENTITY), \
                patch.object(c, "probe", return_value=None), patch.object(r, "claude_tokens", return_value=25):
            result = r.probe_claude({"claude": {"weekly_token_budget": 100}})
        self.assertEqual(result["buckets"][1]["source"], "computed")
        self.assertEqual(result["buckets"][1]["percent"], 25)


if __name__ == "__main__":
    unittest.main()
