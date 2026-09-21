"""Trusted corpus checks and equivalent, reference-free pilot preparation."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pilot_tasks as pilot


class PilotTasksTests(unittest.TestCase):
    def test_references_pass_and_each_initial_fixture_fails(self):
        self.assertEqual(pilot.self_check(), {
            "tasks": 20, "checks": 74, "corpus_sha256": pilot.CORPUS_SHA256,
            "live_attempts": 0})

    def test_changed_corpus_is_rejected_before_execution(self):
        with patch.object(Path, "read_bytes", return_value=b'{"tasks":[]}'), \
                patch.object(pilot, "check_source", side_effect=AssertionError("must not execute")):
            with self.assertRaisesRegex(ValueError, "reviewed digest"):
                pilot.self_check()

    def test_paired_snapshots_identical_private_and_do_not_expose_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "trial"
            manifest = pilot.prepare(root)
            self.assertEqual(json.loads((root / "manifest.json").read_text()), manifest)
            self.assertFalse(manifest["live_ready"])
            self.assertIsNone(manifest["usage_ceilings"])
            self.assertEqual(manifest["max_attempts_per_variant_task"], 2)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            for index, pair in enumerate(manifest["pairs"]):
                with self.subTest(task=pair["id"]):
                    self.assertEqual(pair["order"], ["baseline", "candidate"]
                                     if index % 2 == 0 else ["candidate", "baseline"])
                    self.assertEqual(pair["tier"], pilot.TIERS[(index // 4 + index % 4) % 4])
                    baseline = root / pair["id"] / "baseline"
                    candidate = root / pair["id"] / "candidate"
                    self.assertEqual(sorted(p.name for p in candidate.iterdir()),
                                     ["TASK.md", "solution.py"])
                    for name in ("TASK.md", "solution.py"):
                        raw = (candidate / name).read_bytes()
                        self.assertEqual(raw, (baseline / name).read_bytes())
                        self.assertEqual((candidate / name).stat().st_mode & 0o777, 0o600)
                    self.assertEqual(pilot.digest((candidate / "TASK.md").read_bytes()), pair["task_sha256"])
                    self.assertEqual(pair["judgment"]["task_sha256"], pair["task_sha256"])
            for tier in pilot.TIERS:
                baseline_first = sum(pair["order"][0] == "baseline"
                                     for pair in manifest["pairs"] if pair["tier"] == tier)
                self.assertIn(baseline_first, (2, 3))
            # Editing one arm cannot change the other or the pinned corpus.
            pair = manifest["pairs"][0]
            changed = root / pair["id"] / "candidate" / "solution.py"
            changed.write_text("changed")
            self.assertNotEqual(changed.read_bytes(),
                                (root / pair["id"] / "baseline" / "solution.py").read_bytes())
            self.assertEqual(pilot.self_check()["checks"], 74)

    def test_prepare_never_overwrites_existing_directory_or_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "existing.txt"
            marker.write_text("keep")
            with self.assertRaises(FileExistsError):
                pilot.prepare(root)
            link = root / "alias"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(FileExistsError):
                pilot.prepare(link)
            self.assertEqual(marker.read_text(), "keep")
            self.assertFalse((root / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
