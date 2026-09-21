"""Installed-protocol-shaped fake process; no native login or model calls."""

import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
from types import SimpleNamespace
from unittest.mock import patch

import accounts
import rightsize as r
import native_runs
from managed_ledger import Ledger
from native_codex import Codex, run
from native_transport import JsonProcess, ProtocolError


FAKE = r'''
import json, os, sys, time
mode = os.environ.get("FAKE_MODE", "complete")
thread = "11111111-1111-4111-8111-111111111111"
turn = "22222222-2222-4222-8222-222222222222"
def send(value):
    print(json.dumps(value), flush=True)
def event(method, **values):
    send({"method": method, "params": {"threadId": thread, "turnId": turn, **values}})
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    params = request.get("params", {})
    if method == "initialized":
        continue
    if method == "initialize":
        if mode == "silent-setup":
            time.sleep(30)
        result = {}
    elif method == "account/rateLimits/read":
        result = {"ordinaryUsageAllowed": True, "accountId": "fixture-account",
                  "rateLimits": {"primary": {"usedPercent": 0}}}
        if mode == "quota-before-start":
            result["rateLimits"]["primary"]["usedPercent"] = 100
    elif method == "account/read":
        result = {"account": {"type": "chatgpt"}}
        if mode == "api-billing":
            result["account"]["type"] = "apiKey"
    elif method == "thread/read":
        result = {"thread": {"id": thread, "model": "test-model", "reasoningEffort": "low", "turns": [
            {"id": turn, "status": "completed", "items": [
                {"type": "userMessage", "id": os.environ.get("CLIENT_MESSAGE_ID", "")}]}]}}
    elif method == "thread/start":
        result = {"thread": {"id": thread}, "model": params["model"], "cwd": params["cwd"],
                  "reasoningEffort": params["config"]["model_reasoning_effort"], "modelProvider": "openai",
                  "approvalPolicy": "on-request", "sandbox": {"type": "readOnly" if params["sandbox"]=="read-only" else "workspaceWrite"}}
        if mode == "wrong-model":
            result["model"] = "different-model"
        if mode == "wrong-sandbox":
            result["sandbox"]["type"] = "dangerFullAccess"
    elif method == "turn/start":
        if mode == "disconnect-launch":
            sys.exit(0)
        send({"id": request["id"], "result": {"turn": {"id": turn}}})
        if mode == "malformed":
            print("not json secret-sentinel", flush=True)
            continue
        if mode == "silent-turn":
            continue
        event("item/agentMessage/delta", delta="hello")
        event("item/completed", item={"type": "agentMessage", "id": "message-one", "text": "returned shell text is data"})
        event("item/completed", item={"type": "agentMessage", "id": "message-one", "text": "duplicate"})
        event("thread/tokenUsage/updated", tokenUsage={"total": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}})
        if mode == "heartbeat":
            time.sleep(11)
        final = {"id": turn, "status": "completed"}
        if mode == "quota":
            final = {"id": turn, "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded", "message": "secret-sentinel"}}
        if mode == "wrong-turn":
            final["id"] = "33333333-3333-4333-8333-333333333333"
        event("turn/completed", turn=final)
        continue
    elif method == "turn/interrupt":
        send({"id": request["id"], "result": {}})
        event("turn/completed", turn={"id": turn, "status": "interrupted"})
        continue
    else:
        sys.exit(2)
    send({"id": request["id"], "result": result})
'''


class FastTransport(JsonProcess):
    def request(self, method, params, *, timeout=0.3):
        return super().request(method, params, timeout=min(timeout, 0.3))


class CodexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for patcher in (patch.dict(os.environ, {}, clear=True),
                        patch.object(accounts.Path, "home", return_value=self.root),
                        patch.object(r, "STATE", self.root / "legacy.json")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ledger = Ledger(self.root / "managed.sqlite3")
        self.spec = self.root / "task.md"
        self.spec.write_text("bounded task")
        self.account = accounts.select("codex").public()
        self.attempt = self.ledger.admit(request_key="one",
            task={"task_id": "task", "spec_hash": r.hashlib.sha256(self.spec.read_bytes()).hexdigest(),
                  "context_hash": "none", "judgment_source": "test", "floor": 1},
            pick={"provider": "codex", "model": "test-model", "effort": "low", "band": 1},
            observation={"status": "ok", "account": self.account, "observed_at": time.time(),
                         "quota_account_ref": accounts.digest("codex:fixture-account"),
                         "buckets": [{"id": "weekly", "percent": 0, "source": "live"}]},
            points=1, slot_limit=1, binding_now=lambda: self.account)["attempt"]
        self.processes = []

    def adapter(self, mode):
        def transport(argv, *, env, cwd):
            process = FastTransport([sys.executable, "-u", "-c", FAKE], env={**env, "FAKE_MODE": mode}, cwd=cwd)
            self.processes.append(process)
            return process
        return Codex(self.ledger, {}, "bounded task", self.root, transport=transport)

    def execute(self, mode, **kwargs):
        result = run(self.ledger, self.attempt["attempt_id"], self.adapter(mode), **kwargs)
        for process in self.processes:
            self.assertIsNotNone(process.proc.returncode)
            self.assertTrue(process.proc.stdout.closed)
            self.assertTrue(process.proc.stdin.closed)
        return result

    def test_exact_receipt_output_usage_and_completion_not_acceptance(self):
        output = []
        result = self.execute("complete", output=output.append)
        self.assertEqual(result["status"], "completed")
        attempt = result["attempt"]
        self.assertEqual(attempt["receipt"]["session_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(attempt["receipt"]["dispatch_id"], "22222222-2222-4222-8222-222222222222")
        self.assertEqual(attempt["receipt"]["model"], "test-model")
        self.assertEqual(attempt["metrics"]["total_tokens"], 15)
        self.assertEqual(output, ["returned shell text is data"])
        self.assertIn("first_output_at", attempt)
        self.assertNotIn("review_evidence", attempt)

    def test_setup_failure_is_confirmed_no_launch(self):
        for mode in ("wrong-model",):
            result = self.execute(mode)
            self.assertEqual(result["status"], "launch_failed")
            self.assertNotIn("receipt", result["attempt"])

    def test_native_permission_widening_is_rejected_before_turn(self):
        self.assertEqual(self.execute("wrong-sandbox")["status"], "launch_failed")

    def test_prelaunch_quota_denial_cannot_start_turn(self):
        self.assertEqual(self.execute("quota-before-start")["status"], "launch_failed")

    def test_subscription_admission_cannot_switch_to_api_billing(self):
        self.assertEqual(self.execute("api-billing")["status"], "launch_failed")

    def test_silent_setup_has_real_deadline(self):
        start = time.monotonic()
        self.assertEqual(self.execute("silent-setup")["status"], "launch_failed")
        self.assertLess(time.monotonic() - start, 2)

    def test_turn_request_disconnect_keeps_prepared_session_and_hold(self):
        result = self.execute("disconnect-launch")
        self.assertEqual(result["status"], "reconciling")
        self.assertIn("prepared_receipt", result["attempt"])
        self.assertNotIn("receipt", result["attempt"])

    def test_malformed_event_retains_hold_and_redacts_error(self):
        result = self.execute("malformed")
        self.assertEqual(result["status"], "reconciling")
        self.assertNotIn("secret-sentinel", json.dumps(result))

    def test_silent_turn_timeout_waits_for_native_cancel_confirmation(self):
        result = self.execute("silent-turn", timeout=1)
        self.assertEqual(result["status"], "cancelled")

    def test_lease_renews_while_waiting_for_native_completion(self):
        with patch.object(self.ledger, "renew", wraps=self.ledger.renew) as renew:
            result = self.execute("heartbeat", timeout=20)
        self.assertEqual(result["status"], "completed")
        self.assertGreaterEqual(renew.call_count, 1)

    def test_separate_cancel_command_is_consumed_by_running_owner(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.execute, "silent-turn", timeout=10)
            deadline = time.monotonic() + 3
            while self.ledger.read(self.attempt["attempt_id"])["state"] != "started":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(r.main(["managed", "cancel", "--attempt", self.attempt["attempt_id"]]), 0)
            self.assertEqual(future.result(timeout=3)["status"], "cancelled")

    def test_quota_failure_is_typed_without_raw_error(self):
        result = self.execute("quota")
        self.assertEqual(result["status"], "quota_failed")
        self.assertNotIn("secret-sentinel", json.dumps(result))

    def test_wrong_turn_completion_is_unresolved(self):
        self.assertEqual(self.execute("wrong-turn")["status"], "reconciling")

    def test_reconnect_binds_uncertain_launch_by_client_message_id_without_relaunch(self):
        import native_codex
        self.assertEqual(self.execute("disconnect-launch")["status"], "reconciling")
        def transport(argv, **kwargs):
            kwargs["env"] = {**kwargs["env"], "CLIENT_MESSAGE_ID": self.attempt["launch_key"]}
            return FastTransport([sys.executable, "-u", "-c", FAKE], **kwargs)
        result = native_codex.reconcile(self.ledger, self.attempt["attempt_id"], {}, transport=transport)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempt"]["receipt"]["dispatch_id"], "22222222-2222-4222-8222-222222222222")

    def test_reconnect_without_exact_marker_keeps_hold(self):
        import native_codex
        self.assertEqual(self.execute("disconnect-launch")["status"], "reconciling")
        def transport(argv, **kwargs):
            return FastTransport([sys.executable, "-u", "-c", FAKE], **kwargs)
        result = native_codex.reconcile(self.ledger, self.attempt["attempt_id"], {}, transport=transport)
        self.assertEqual(result["status"], "reconciling")

    def test_owned_worktree_and_output_artifact_end_to_end(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "--allow-empty", "-qm", "initial"], check=True)
        def factory(ledger, config, prompt, cwd, **options):
            def transport(argv, **kwargs):
                return FastTransport([sys.executable, "-u", "-c", FAKE], **kwargs)
            return Codex(ledger, config, prompt, cwd, transport=transport, **options)
        args = SimpleNamespace(attempt=self.attempt["attempt_id"], spec=str(self.spec), repo=str(repo),
                               sandbox="read-only", timeout=5)
        with patch.object(native_runs.native_codex, "Codex", side_effect=factory):
            result = native_runs.execute(args, {}, r)
        self.assertEqual(result["status"], "completed")
        self.assertNotEqual(Path(result["worktree"]), repo)
        self.assertEqual(Path(result["output_artifact"]).read_text(), "returned shell text is data\n")
        self.assertEqual(Path(result["output_artifact"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True), "")

    def test_explicit_native_usage_denial_requires_explicit_recovery(self):
        response = {"accountId": "fixture-account", "ordinaryUsageAllowed": False,
                    "rateLimits": {"primary": {"usedPercent": 0, "windowDurationMins": 300}}}
        with patch.object(r, "codex_rate_limits", return_value=response):
            denied = r.probe_codex({})
            self.assertEqual(denied["status"], "denied")
            r.eligibility({}, {"codex": denied})
            response["ordinaryUsageAllowed"] = None
            self.assertFalse(r.eligibility({}, {"codex": r.probe_codex({})})["codex"]["eligible"])
            response["ordinaryUsageAllowed"] = True
            self.assertTrue(r.eligibility({}, {"codex": r.probe_codex({})})["codex"]["eligible"])


if __name__ == "__main__":
    unittest.main()
