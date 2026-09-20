"""Scoped credential resolution using synthetic secrets only."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import rightsize as ar


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, value in (("ROOT", self.root), ("OPENCODE_AUTH", self.root / "auth.json")):
            mock = patch.object(ar, target, value)
            mock.start()
            self.addCleanup(mock.stop)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.link = self.root / ".infisical.json"

    def configure(self, **changes):
        scope = dict(workspaceId="project", defaultEnvironment="test", rightsizePath="/router")
        scope.update(changes)
        self.link.write_text(json.dumps(scope))

    def test_exact_scope_and_no_shell(self):
        self.configure()
        sentinel = "$(do-not-execute); synthetic-key"
        with patch.object(ar.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, sentinel, "")) as run:
            self.assertEqual(ar.secret("TYPESAFE_API_KEY"), sentinel)
        args = run.call_args.args[0]
        self.assertEqual(args, ["infisical", "secrets", "get", "TYPESAFE_API_KEY",
                               "--plain", "--silent", "--projectId", "project",
                               "--env", "test", "--path", "/router",
                               "--include-imports=false", "--expand=false",
                               "--secret-overriding=false"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(os.environ.get("TYPESAFE_API_KEY"), None)

    def test_incomplete_scope_never_guesses_or_falls_back(self):
        ar.OPENCODE_AUTH.write_text(json.dumps({"opencode-go": {"key": "native-sentinel"}}))
        for field in ("workspaceId", "defaultEnvironment", "rightsizePath"):
            with self.subTest(field=field):
                self.configure(**{field: None})
                with patch.object(ar.subprocess, "run") as run:
                    self.assertIsNone(ar.secret("OPENCODE_API_KEY"))
                    run.assert_not_called()

    def test_environment_precedence_and_unknown_name(self):
        self.configure()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "explicit", "OTHER": "unrelated"}):
            with patch.object(ar.subprocess, "run") as run:
                self.assertEqual(ar.secret("OPENROUTER_API_KEY"), "explicit")
                self.assertIsNone(ar.secret("OTHER"))
                run.assert_not_called()

    def test_native_unlinked_unchanged_and_not_mutated(self):
        contents = json.dumps({"opencode-go": {"key": "native-sentinel"}})
        ar.OPENCODE_AUTH.write_text(contents)
        self.assertEqual(ar.secret("OPENCODE_API_KEY"), "native-sentinel")
        self.assertEqual(ar.OPENCODE_AUTH.read_text(), contents)

    def test_missing_cli_and_denied_read_do_not_switch_accounts(self):
        self.configure()
        ar.OPENCODE_AUTH.write_text(json.dumps({"opencode-go": {"key": "other-account"}}))
        with patch.object(ar.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(ar.secret("OPENCODE_API_KEY"))
        with patch.object(ar.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, "", "sensitive-provider-error")):
            self.assertIsNone(ar.secret("OPENCODE_API_KEY"))

    def test_doctor_does_not_resolve_or_display_values(self):
        self.configure()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "synthetic-secret-sentinel"}):
            with patch.object(ar, "secret", side_effect=AssertionError("secret read")), \
                    patch.object(ar, "probe_codex", return_value={"buckets": []}), \
                    patch.object(ar, "STATE", self.root / "state.json"), \
                    patch.object(ar, "REGISTRY", self.root / "registry.json"):
                output = str(ar.doctor({"bands": {}}))
        self.assertNotIn("synthetic-secret-sentinel", output)
        self.assertIn("scoped vault selected; unverified", output)


if __name__ == "__main__":
    unittest.main()
