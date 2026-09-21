"""Managed Go lifecycle using native-schema-shaped fake HTTP responses."""

import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import subprocess
import time
import unittest
from unittest.mock import patch

import accounts
import rightsize as r
import native_runs
from managed_ledger import Ledger
from managed_router import launch_once, current_binding
from native_codex import run
from native_opencode import ResponseError
from native_opencode_execution import OpenCode, reconcile, permissions
from native_transport import ProtocolError


class Backend:
    def __init__(self, mode="complete"):
        self.mode, self.calls, self.children = mode, [], []
        self.session, self.user, self.assistant = None, None, None

    def connect(self, *, env, cwd):
        child = SimpleNamespace(proc=SimpleNamespace(pid=123), closed=False)
        def request(method, path, body=None, **kwargs):
            self.calls.append((method, path))
            if path == "/provider":
                return {"connected": ["opencode-go"], "all": [{"id": "opencode-go",
                    "key": env["RIGHTSIZE_OPENCODE_GO_KEY"], "options": {}, "models": {
                    "fixture": {"id": "fixture", "providerID": "opencode-go", "api": {"id": "fixture",
                    "url": "https://opencode.ai/zen/go/v1", "npm": "@ai-sdk/openai-compatible"}, "options": {}, "headers": {}, "variants": {}}}}]}
            if method == "POST" and path == "/session":
                self.session = {**copy.deepcopy(body), "id": "ses_fixture", "directory": str(cwd)}
                if self.mode == "wrong-permissions":self.session["permission"] = []
                return self.session
            if path.endswith("/prompt_async"):
                self.prompt = copy.deepcopy(body)
                self.user = {"info": {"id": body["messageID"], "sessionID": "ses_fixture", "role": "user",
                    "model": {**body["model"], **({"variant": body["variant"]} if "variant" in body else {})}}, "parts": []}
                self.assistant = {"info": {"id": "msg_answer", "sessionID": "ses_fixture", "role": "assistant",
                    "parentID": body["messageID"], "providerID": "opencode-go", "modelID": "fixture",
                    "time": {"created": 1, "completed": 2}, "finish": "stop",
                    "tokens": {"input": 7, "output": 3, "reasoning": 1, "cache": {"read": 5, "write": 2}, "total": 18}},
                    "parts": [{"id": "prt_answer", "sessionID": "ses_fixture", "messageID": "msg_answer", "type": "text", "text": "output data"}]}
                if self.mode in ("silent", "ack-only"):
                    self.assistant["info"]["time"].pop("completed")
                    self.assistant["info"].pop("finish")
                    self.assistant["parts"] = []
                if self.mode == "wrong-model":self.assistant["info"]["modelID"] = "wrong"
                if self.mode == "wrong-parent":self.assistant["info"]["parentID"] = "msg_other"
                if self.mode == "quota":self.assistant["info"]["error"] = {"name": "APIError", "data": {"statusCode": 429, "message": "SECRET_SENTINEL"}}
                if self.mode == "malformed":self.assistant["info"]["tokens"] = None
                if self.mode == "disconnect":raise ProtocolError("lost prompt acknowledgement")
                return None
            if path.endswith("/abort"):
                if self.mode != "ack-only":
                    self.assistant["info"]["error"] = {"name": "MessageAbortedError", "data": {"message": "cancelled"}}
                return True
            if path == "/session/ses_fixture":return copy.deepcopy(self.session)
            if path == "/session/status":return {"ses_fixture": {"type": "busy"}} if self.mode == "busy" else {}
            if path.endswith("/message"):
                if self.mode == "missing-history":return []
                return copy.deepcopy([self.user, self.assistant])
            if "/message/msg_" in path:
                if self.user:return copy.deepcopy(self.user)
                raise ResponseError(404)
            raise AssertionError((method, path))
        child.request = request
        child.close = lambda: setattr(child, "closed", True)
        self.children.append(child)
        return child


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for patcher in (patch.dict(os.environ, {"OPENCODE_API_KEY": "fixture-key"}, clear=True),
                        patch.object(accounts.Path, "home", return_value=self.root),
                        patch.object(r, "STATE", self.root / "state.json"),
                        patch.object(r, "ROOT", self.root)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ledger = Ledger(self.root / "managed.sqlite3")
        self.account = accounts.select("opencode", {}).public()
        self.quota = {"name": "opencode", "status": "ok", "quota_account_ref": accounts.digest("opencode-go:fixture-key"),
                      "buckets": [{"id": "weekly", "percent": 0, "source": "live"}]}
        patcher = patch.object(r, "probe_opencode", return_value=self.quota)
        self.probe = patcher.start()
        self.addCleanup(patcher.stop)
        self.attempt = self.ledger.admit(request_key="fixture", task={"task_id": "fixture", "spec_hash": hashlib.sha256(b"task").hexdigest(),
            "context_hash": "none", "judgment_source": "test", "floor": 1},
            pick={"provider": "opencode", "model": "fixture", "effort": None, "band": 1},
            observation={**self.quota, "account": self.account, "observed_at": time.time()},
            points=1, slot_limit=2, binding_now=lambda: self.account)["attempt"]
        self.cwd = self.root / "worktree"
        self.cwd.mkdir()

    def adapter(self, backend):
        return OpenCode(self.ledger, {}, "task", self.cwd, transport=backend.connect, api=r)

    def execute(self, mode="complete", **kwargs):
        backend = Backend(mode)
        output = []
        result = run(self.ledger, self.attempt["attempt_id"], self.adapter(backend), output=output.append, **kwargs)
        self.assertTrue(all(child.closed for child in backend.children))
        return result, output, backend

    def test_completion_is_bound_and_distinct_from_acceptance(self):
        result, output, backend = self.execute()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(output, ["output data"])
        self.assertEqual(result["attempt"]["receipt"]["dispatch_id"], "msg_" + self.attempt["launch_key"])
        self.assertEqual(result["attempt"]["metrics"]["total_tokens"], 18)
        self.assertNotIn("review_evidence", result["attempt"])
        self.probe.assert_called_once_with({}, "fixture-key")
        self.assertEqual(sum(path.endswith("/prompt_async") for _, path in backend.calls), 1)

    def test_cli_execution_uses_isolated_worktree_and_owned_artifact(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "--allow-empty", "-qm", "fixture"], check=True)
        spec = self.root / "task.md"
        spec.write_text("task")
        backend = Backend()
        def create(ledger, config, prompt, cwd, **kwargs):
            return OpenCode(ledger, config, prompt, cwd, transport=backend.connect, api=r, **kwargs)
        with patch.object(native_runs.native_opencode_execution, "OpenCode", side_effect=create):
            result = native_runs.execute(SimpleNamespace(attempt=self.attempt["attempt_id"], spec=spec,
                repo=repo, sandbox="read-only", timeout=3), {}, r)
        self.assertEqual(result["status"], "completed")
        self.assertNotEqual(Path(result["worktree"]), repo)
        self.assertEqual(Path(result["output_artifact"]).read_text().strip(), "output data")
        self.assertEqual(Path(result["output_artifact"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(backend.prompt["parts"], [{"type": "text", "text": "task"}])

    def test_preflight_denial_or_permission_mismatch_submits_nothing(self):
        for mode in ("quota", "wrong-permissions"):
            with self.subTest(mode=mode):
                backend = Backend(mode)
                adapter = self.adapter(backend)
                if mode == "quota":self.quota["buckets"][0]["percent"] = 100
                try:
                    from managed_router import NoLaunch
                    with self.assertRaises(NoLaunch):adapter.launch(self.attempt)
                    self.assertFalse(any(path.endswith("/prompt_async") for _, path in backend.calls))
                finally:
                    adapter.close()
                    self.quota["buckets"][0]["percent"] = 0

    def test_lost_ack_holds_and_history_recovers_without_new_prompt(self):
        result, _, backend = self.execute("disconnect")
        self.assertEqual(result["status"], "reconciling")
        backend.mode = "complete"
        prior = len(backend.calls)
        recovered = reconcile(self.ledger, self.attempt["attempt_id"], {}, transport=backend.connect, api=r)
        self.assertEqual(recovered["status"], "completed")
        self.assertTrue(all(method == "GET" for method, _ in backend.calls[prior:]))
        self.assertEqual(reconcile(self.ledger, self.attempt["attempt_id"], {}, transport=backend.connect, api=r)["status"], "existing")

    def test_mismatched_and_malformed_outcomes_retain_commitments(self):
        # Use separate fixtures, preserving each unknown attempt for the check.
        for mode in ("wrong-model", "wrong-parent", "malformed"):
            with self.subTest(mode=mode):
                case = ExecutionTests()
                case.setUp()
                try:
                    result, _, _ = case.execute(mode)
                    self.assertEqual(result["status"], "reconciling")
                finally:case.doCleanups()

    def test_quota_error_redacted_and_denial_persisted(self):
        result, _, _ = self.execute("quota")
        self.assertEqual(result["status"], "quota_failed")
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))

    def test_timeout_requires_matching_native_abort_result(self):
        result, _, _ = self.execute("silent", timeout=1)
        self.assertEqual(result["status"], "cancelled")

    def test_abort_ack_alone_does_not_settle(self):
        backend = Backend("ack-only")
        adapter = self.adapter(backend)
        try:
            self.assertEqual(launch_once(self.ledger, self.attempt["attempt_id"], adapter.launch)["status"], "started")
            adapter.cancel()
            self.assertEqual(adapter.poll(0), [])
            self.assertEqual(self.ledger.read(self.attempt["attempt_id"])["state"], "started")
        finally:adapter.close()

    def test_poll_deduplicates_text_and_cumulative_usage(self):
        backend = Backend()
        adapter = self.adapter(backend)
        try:
            launch_once(self.ledger, self.attempt["attempt_id"], adapter.launch)
            self.assertEqual(len([e for e in adapter.poll(0) if e["kind"] == "producing"]), 1)
            self.assertEqual(len([e for e in adapter.poll(0) if e["kind"] == "producing"]), 0)
            self.assertEqual(adapter.metrics["total_tokens"], 18)
        finally:adapter.close()

    def test_missing_history_cannot_release_hold(self):
        _, _, backend = self.execute("disconnect")
        backend.mode = "missing-history"
        self.assertEqual(reconcile(self.ledger, self.attempt["attempt_id"], {}, transport=backend.connect, api=r)["status"], "reconciling")

    def test_busy_or_compaction_completion_is_not_task_completion(self):
        backend = Backend("busy")
        adapter = self.adapter(backend)
        try:
            launch_once(self.ledger, self.attempt["attempt_id"], adapter.launch)
            self.assertFalse(any(e["kind"] == "completed" for e in adapter.poll(0)))
            backend.mode = "complete"
            backend.assistant["info"]["summary"] = True
            self.assertFalse(any(e["kind"] == "completed" for e in adapter.poll(0)))
            backend.assistant["info"].pop("summary")
            backend.assistant["parts"].append({"type": "tool", "id": "prt_tool", "sessionID": "ses_fixture",
                "messageID": "msg_answer", "state": {"status": "error"}})
            adapter.poll(0)
            adapter.poll(0)
            self.assertEqual(adapter.metrics["tool_failures"], 1)
        finally:adapter.close()

    def test_vault_admission_binding_is_local_metadata_only(self):
        with patch.dict(os.environ, {}, clear=True):
            binding = accounts.select("opencode", {}).public()
            scope = ("project", "test", "/router")
            (self.root / ".infisical.json").write_text(json.dumps(dict(zip(("workspaceId", "defaultEnvironment", "rightsizePath"), scope))))
            account = {**binding, "source": "scoped-vault", "fingerprint": "fixture-key-hash",
                       "account_ref": accounts.digest(json.dumps(["opencode", scope]))[:24]}
            observation = {"account": account, "native_binding": binding}
            with patch.object(r, "secret", side_effect=AssertionError("network under lock")):
                self.assertEqual(current_binding(r, "opencode", {}, observation), account)
                (self.root / ".infisical.json").write_text('{"workspaceId":"other","defaultEnvironment":"test","rightsizePath":"/router"}')
                self.assertNotEqual(current_binding(r, "opencode", {}, observation), account)


if __name__ == "__main__":
    unittest.main()
