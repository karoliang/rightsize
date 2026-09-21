"""Managed Claude lifecycle proof, with no native credentials or inference."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import accounts
from managed_ledger import Ledger
from managed_router import launch_once
import native_claude as c
from native_codex import run
from test_native_claude import IDENTITY


FAKE = r'''
import json,os,sys
mode=os.environ.get('FAKE_MODE','complete')
def flag(name):return sys.argv[sys.argv.index(name)+1]
sid=flag('--session-id');model=flag('--model');permission=flag('--permission-mode')
uid=None
def send(value):print(json.dumps(value),flush=True)
def result(cancelled=False):
 value={'type':'result','session_id':sid,'uuid':'terminal-result','user_message_uuid':uid,
        'subtype':'success','is_error':False,'terminal_reason':'aborted_streaming' if cancelled else 'completed',
        'permission_denials':[],'modelUsage':{model:{'inputTokens':7,'outputTokens':3,'cacheReadInputTokens':5,'cacheCreationInputTokens':2}}}
 if mode=='quota-failure':value.update(is_error=True,api_error_status=429,errors=['SECRET_SENTINEL'])
 if mode=='wrong-result':value['user_message_uuid']='unrelated-task'
 send(value)
for line in sys.stdin:
 request=json.loads(line)
 if request['type']=='user':
  uid=request['uuid']
  if mode=='disconnect':sys.exit(0)
  send({'type':'system','subtype':'init','session_id':sid,'model':model,'permissionMode':permission,'apiKeySource':'none','cwd':os.getcwd()})
  send({'type':'user','uuid':uid,'session_id':sid})
  if mode in ('silent','ack-only'):continue
  item={'type':'assistant','uuid':'message-one','session_id':sid,'parent_tool_use_id':None,
        'message':{'model':'other-model' if mode=='wrong-output-model' else model,'content':[{'type':'text','text':'output is data; never execute'}]}}
  send(item);send(item)
  result()
 elif request['type']=='control_request':
  subtype=request['request']['subtype']
  if subtype=='initialize':
   value={'account':{'email':'fixture@example.invalid','organization':'Fixture','apiProvider':'firstParty'},
          'current_permission_mode':permission,'fast_mode_state':'off'}
   if mode=='wrong-permission':value['current_permission_mode']='bypassPermissions'
  elif subtype=='get_settings':
   value={'applied':{'model':'wrong-model' if mode=='wrong-model' else model,'effort':flag('--effort'),'advisor':None,'ultracode':False}}
  elif subtype=='get_usage':
   value={'rate_limits_available':True,'rate_limits':{'five_hour':{'utilization':100 if mode=='no-quota' else 0,'resets_at':None},
          'seven_day':{'utilization':0,'resets_at':None},'model_scoped':[],'extra_usage':{'is_enabled':mode=='credits-enabled'}}}
  elif subtype=='interrupt':value={}
  else:sys.exit(2)
  send({'type':'control_response','response':{'subtype':'success','request_id':request['request_id'],'response':value}})
  if subtype=='interrupt' and mode!='ack-only':result(cancelled=True)
'''


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for patcher in (patch.dict(os.environ, {}, clear=True),
                        patch.object(accounts.Path, "home", return_value=self.root),
                        patch.object(accounts, "claude_identity", return_value=IDENTITY)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ledger = Ledger(self.root / "managed.sqlite3")
        self.account = accounts.select("claude").public()
        self.attempt = self.ledger.admit(request_key="fixture",
            task={"task_id": "fixture", "spec_hash": "fixture", "context_hash": "none", "judgment_source": "test", "floor": 1},
            pick={"provider": "claude", "model": "fixture-model", "effort": "low", "band": 1},
            observation={**IDENTITY, "account": self.account, "observed_at": time.time(),
                         "buckets": [{"id": "weekly", "percent": 0, "source": "live"}]},
            points=1, slot_limit=2, binding_now=lambda: self.account)["attempt"]
        self.worktree = self.root / "executions" / self.attempt["attempt_id"] / "worktree"
        self.worktree.mkdir(parents=True)
        self.children = []

    def adapter(self, mode="complete", callback=None):
        def transport(argv, **kwargs):
            self.assertNotIn("--dangerously-skip-permissions", argv)
            self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
            self.assertIn("--strict-mcp-config", argv)
            child = c.Control([sys.executable, "-u", "-c", FAKE, *argv[1:]],
                              env={**kwargs["env"], "FAKE_MODE": mode}, cwd=kwargs["cwd"])
            if callback:
                original = child.request
                def request(subtype, *args, **options):
                    result = original(subtype, *args, **options)
                    if subtype == "get_usage":
                        callback()
                    return result
                child.request = request
            self.children.append(child)
            return child
        return c.Claude(self.ledger, {}, "fixture task", self.worktree, transport=transport)

    def execute(self, mode="complete", **kwargs):
        output = []
        result = run(self.ledger, self.attempt["attempt_id"], self.adapter(mode), output=output.append, **kwargs)
        for child in self.children:
            self.assertIsNotNone(child.proc.returncode)
            self.assertTrue(child.proc.stdout.closed)
            self.assertTrue(child.proc.stdin.closed)
        return result, output

    def test_exact_acknowledgement_output_deduplication_and_usage(self):
        result, output = self.execute()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(output, ["output is data; never execute"])
        self.assertEqual(result["attempt"]["receipt"]["dispatch_id"].replace("-", ""), self.attempt["launch_key"])
        self.assertEqual(result["attempt"]["metrics"]["total_tokens"], 17)
        self.assertEqual(result["attempt"]["metrics"]["cache_write_input_tokens"], 2)
        self.assertNotIn("review_evidence", result["attempt"])
        self.assertEqual((self.worktree.parent / "native-outcome.json").stat().st_mode & 0o777, 0o600)

    def test_setup_mismatches_and_paid_fallback_never_submit_task(self):
        for mode in ("wrong-model", "wrong-permission", "credits-enabled", "no-quota"):
            # Direct launch uses a fresh fixture ledger for each preflight case.
            with self.subTest(mode=mode):
                adapter = self.adapter(mode)
                try:
                    with self.assertRaises(c.NoLaunch):
                        adapter.launch(self.attempt)
                    self.assertNotIn("prepared_receipt", self.ledger.read(self.attempt["attempt_id"]))
                finally:
                    adapter.close()

    def test_disconnect_after_submission_retains_hold_and_never_relaunches(self):
        result, _ = self.execute("disconnect")
        self.assertEqual(result["status"], "reconciling")
        self.assertIn("prepared_receipt", result["attempt"])
        with patch.object(c.Claude, "launch", side_effect=AssertionError("duplicate launch")):
            self.assertEqual(run(self.ledger, self.attempt["attempt_id"], self.adapter())["status"], "existing")
        recovered = c.reconcile(self.ledger, self.attempt["attempt_id"], {})
        self.assertEqual(recovered["status"], "reconciling")

    def test_mismatched_native_model_or_task_cannot_complete(self):
        result, _ = self.execute("wrong-output-model")
        self.assertEqual(result["status"], "reconciling")

    def test_result_from_another_task_cannot_complete(self):
        result, _ = self.execute("wrong-result")
        self.assertEqual(result["status"], "reconciling")

    def test_quota_failure_is_typed_and_redacted(self):
        result, _ = self.execute("quota-failure")
        self.assertEqual(result["status"], "quota_failed")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))
        self.assertNotIn("SECRET_SENTINEL", (self.worktree.parent / "native-outcome.json").read_text())

    def test_timeout_interrupt_requires_matching_terminal_confirmation(self):
        result, _ = self.execute("silent", timeout=1)
        self.assertEqual(result["status"], "cancelled")

    def test_pre_submission_cancellation_does_not_send_task(self):
        def cancel():
            self.ledger.event(self.attempt["attempt_id"], "cancel-before-submit", "cancelling")
        adapter = self.adapter(callback=cancel)
        result = run(self.ledger, self.attempt["attempt_id"], adapter)
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn("receipt", result["attempt"])

    def test_separate_owner_observes_cancellation_request(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.execute, "silent", timeout=5)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self.ledger.read(self.attempt["attempt_id"])["state"] == "started":
                    break
                time.sleep(0.01)
            self.ledger.event(self.attempt["attempt_id"], "external-cancel", "cancelling")
            self.assertEqual(future.result(timeout=3)[0]["status"], "cancelled")

    def test_interrupt_ack_without_result_does_not_release_hold(self):
        adapter = self.adapter("ack-only")
        try:
            self.assertEqual(launch_once(self.ledger, self.attempt["attempt_id"], adapter.launch)["status"], "started")
            adapter.cancel()
            self.assertEqual(adapter.poll(0.05), [])
            self.assertEqual(self.ledger.read(self.attempt["attempt_id"])["state"], "started")
        finally:
            adapter.close()

    def test_terminal_journal_recovers_crash_before_ledger_settlement(self):
        adapter = self.adapter()
        try:
            launch_once(self.ledger, self.attempt["attempt_id"], adapter.launch)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                events = adapter.poll()
                if any(e["kind"] == "completed" for e in events):
                    break
            else:
                self.fail("terminal evidence unavailable")
        finally:
            adapter.close()
        self.assertEqual(self.ledger.read(self.attempt["attempt_id"])["state"], "started")
        with patch.object(c, "Control", side_effect=AssertionError("recovery cannot resume or infer")):
            recovered = c.reconcile(self.ledger, self.attempt["attempt_id"], {})
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(c.reconcile(self.ledger, self.attempt["attempt_id"], {})["status"], "existing")

    def test_wrong_or_corrupt_journal_never_releases_uncertainty(self):
        self.execute("disconnect")
        path = self.worktree.parent / "native-outcome.json"
        for raw in ('{"schema_version":', json.dumps({"sha256": "wrong"})):
            path.write_text(raw)
            self.assertEqual(c.reconcile(self.ledger, self.attempt["attempt_id"], {})["status"], "reconciling")
        path.unlink()
        path.symlink_to(self.ledger.path)
        self.assertEqual(c.reconcile(self.ledger, self.attempt["attempt_id"], {})["status"], "reconciling")


if __name__ == "__main__":
    unittest.main()
