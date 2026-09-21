"""Focused regressions for task_judgment.heuristic.

Offline, no credentials, no model calls. Each case names the requirement it
exists for; the order matches the brief in issue #26. Run: python3
test_task_judgment.py
"""
import unittest

import task_judgment as t


class DefaultsTests(unittest.TestCase):
    """Same keys and default scales as rightsize.heuristic."""

    def test_keys_match_original(self):
        result = t.heuristic("rename a counter; preserve behavior")
        self.assertEqual(
            set(result),
            {"tier", "tier_confidence", "size", "second_opinion",
             "spec_complete", "destructive"},
        )

    def test_default_scales_match_original(self):
        # Plain implementation task, no risk words, no irreversible actions.
        result = t.heuristic("add a rate limit to the signup endpoint")
        self.assertEqual(result["tier"], "implementation")
        self.assertIsNone(result["tier_confidence"])
        self.assertEqual(result["size"], 0.6)
        self.assertEqual(result["second_opinion"], 0.2)
        self.assertEqual(result["spec_complete"], 0.6)
        self.assertEqual(result["destructive"], 0.1)

    def test_existing_high_stakes_case_still_high_stakes(self):
        # Mirrors the assert in test_rightsize.py:142 so the wiring change
        # does not silently break the offline route.
        result = t.heuristic("add a database migration dropping the old column")
        self.assertEqual(result["tier"], "high_stakes")
        # "dropping the old column" is not a literal "drop <object>" so the
        # destructive flag stays at the prose-only floor.
        self.assertEqual(result["destructive"], 0.1)
        self.assertEqual(result["second_opinion"], 0.7)


class MetadataStripTests(unittest.TestCase):
    """Repository paths, URLs, links and filenames must not trip tier detection."""

    def test_money_in_repo_path_is_not_high_stakes(self):
        # '\bmoney' in the OLD regex matched the bare word 'money' in
        # 'money.financial' and forced the whole spec to high_stakes.
        result = t.heuristic("Fix README typo in /repo/money.financial")
        self.assertEqual(result["tier"], "mechanical")
        self.assertEqual(result["destructive"], 0.1)
        self.assertEqual(result["second_opinion"], 0.2)

    def test_spaces_lease_repo_path_stays_mechanical(self):
        result = t.heuristic("Fix README typo in /repo/spaces.lease")
        self.assertEqual(result["tier"], "mechanical")
        self.assertEqual(result["destructive"], 0.1)

    def test_auth_in_url_is_not_high_stakes(self):
        result = t.heuristic(
            "Investigate the failing request at https://api.example.com/auth/login")
        self.assertEqual(result["tier"], "implementation")
        self.assertEqual(result["destructive"], 0.1)

    def test_auth_in_visible_markdown_label_preserves_risk(self):
        result = t.heuristic(
            "Read [the auth guide](https://example.com/auth) and summarise it")
        self.assertEqual(result["tier"], "high_stakes")
        self.assertEqual(result["destructive"], 0.1)

    def test_auth_in_windows_path_is_not_high_stakes(self):
        result = t.heuristic(
            "Update the config at C:\\Users\\admin\\auth\\settings.json")
        self.assertEqual(result["tier"], "implementation")
        self.assertEqual(result["destructive"], 0.1)

    def test_windows_path_with_spaces_still_strips(self):
        # Spaces inside the path must not leave the inner 'auth' exposed.
        result = t.heuristic(
            "Move the config to C:\\Program Files\\auth\\settings.json")
        self.assertEqual(result["tier"], "implementation")
        self.assertEqual(result["destructive"], 0.1)

    def test_far_unix_path_strips(self):
        result = t.heuristic(
            "In /Users/karo/Github/money.financial/auth-emails, "
            "investigate why the prompt renders twice.")
        # 'design' is not in the spec; the path should leave only prose.
        self.assertEqual(result["tier"], "implementation")

    def test_filename_alone_is_stripped(self):
        result = t.heuristic("Fix typo in parser.py")
        self.assertEqual(result["tier"], "mechanical")
        self.assertEqual(result["destructive"], 0.1)


class ProsePreservationTests(unittest.TestCase):
    """Real risk words in prose must still classify correctly."""

    def test_payment_in_prose_is_high_stakes(self):
        result = t.heuristic("Fix the payment calculation rounding bug")
        self.assertEqual(result["tier"], "high_stakes")
        # No irreversible action word in the spec.
        self.assertEqual(result["destructive"], 0.1)

    def test_payment_alongside_filename_keeps_prose(self):
        # 'payment calculation' is the operation; 'money.financial' is a
        # nearby filename. The filename must not erase the prose word.
        result = t.heuristic("Fix the payment calculation in money.financial")
        self.assertEqual(result["tier"], "high_stakes")
        self.assertEqual(result["destructive"], 0.1)

    def test_fix_auth_token_validation_stays_high_stakes_not_destructive(self):
        result = t.heuristic("fix auth token validation")
        self.assertEqual(result["tier"], "high_stakes")
        # High-stakes alone must not auto-set destructive.
        self.assertEqual(result["destructive"], 0.1)

    def test_change_money_transfer_calculation_stays_high_stakes_not_destructive(self):
        result = t.heuristic("change money transfer calculation")
        self.assertEqual(result["tier"], "high_stakes")
        self.assertEqual(result["destructive"], 0.1)

    def test_add_database_migration_stays_high_stakes_not_destructive(self):
        result = t.heuristic("add database migration")
        self.assertEqual(result["tier"], "high_stakes")
        self.assertEqual(result["destructive"], 0.1)


class DestructiveOperationTests(unittest.TestCase):
    """Each irreversible action listed in the brief crosses the threshold."""

    def test_drop_table_customer(self):
        result = t.heuristic("drop table customer")
        self.assertEqual(result["tier"], "high_stakes")
        self.assertGreaterEqual(result["destructive"], 0.5)

    def test_deploy_production(self):
        result = t.heuristic("deploy production")
        self.assertEqual(result["tier"], "high_stakes")
        self.assertGreaterEqual(result["destructive"], 0.5)

    def test_publish_figures(self):
        result = t.heuristic("publish figures")
        self.assertGreaterEqual(result["destructive"], 0.5)

    def test_force_push_branch(self):
        result = t.heuristic("force-push branch")
        self.assertGreaterEqual(result["destructive"], 0.5)

    def test_force_push_hyphenated(self):
        result = t.heuristic("force push the release branch")
        self.assertGreaterEqual(result["destructive"], 0.5)

    def test_overwrite_production_data(self):
        result = t.heuristic("overwrite production data")
        self.assertGreaterEqual(result["destructive"], 0.5)


class GenuineShellCommandTests(unittest.TestCase):
    """Real shell commands inside code fences must still be detected."""

    def test_drop_inside_bash_code_fence(self):
        spec = (
            "Run the cleanup:\n"
            "```bash\n"
            "drop table users\n"
            "```\n"
        )
        result = t.heuristic(spec)
        # Code fence is not metadata; the operation must still flag destructive.
        self.assertGreaterEqual(result["destructive"], 0.5)

    def test_force_push_inside_shell_code_fence(self):
        spec = (
            "Recovery steps:\n"
            "```sh\n"
            "git push --force origin main\n"
            "```\n"
        )
        result = t.heuristic(spec)
        self.assertGreaterEqual(result["destructive"], 0.5)


class NegationTests(unittest.TestCase):
    """Negated instructions must not demand confirmation."""

    def test_do_not_deploy(self):
        result = t.heuristic("do not deploy")
        # The spec mentions 'deploy', which classifies as high_stakes; that
        # is preserved. The destructive flag must stay at the prose floor.
        self.assertEqual(result["tier"], "high_stakes")
        self.assertEqual(result["destructive"], 0.1)

    def test_never_drop_data(self):
        result = t.heuristic("never drop data")
        # 'never' is a negation; no irreversible pattern fires.
        self.assertEqual(result["destructive"], 0.1)

    def test_no_publishing(self):
        result = t.heuristic("no publishing")
        self.assertEqual(result["destructive"], 0.1)

    def test_dont_overwrite_production(self):
        result = t.heuristic("don't overwrite production")
        self.assertEqual(result["destructive"], 0.1)


class TierPrecedenceAndSizeTests(unittest.TestCase):
    """First-match tier wins; size escalates on the existing keywords."""

    def test_tier_precedence_picks_first_match(self):
        # Both 'design' and 'auth' match; high_stakes (first entry) wins.
        result = t.heuristic("design the auth flow for the new app")
        self.assertEqual(result["tier"], "high_stakes")

    def test_size_escalates_on_repo_wide(self):
        result = t.heuristic("rename the counter everywhere across the codebase")
        self.assertEqual(result["size"], 2.0)

    def test_size_default_for_short_prose(self):
        result = t.heuristic("rename a counter; preserve behavior")
        self.assertEqual(result["size"], 0.6)

    def test_second_opinion_default_for_design(self):
        result = t.heuristic("design the new API shape")
        self.assertEqual(result["tier"], "design")
        self.assertEqual(result["second_opinion"], 0.7)

    def test_second_opinion_low_for_mechanical(self):
        result = t.heuristic("fix typo in the comment")
        self.assertEqual(result["tier"], "mechanical")
        self.assertEqual(result["second_opinion"], 0.2)


class ReturnShapeTests(unittest.TestCase):
    """Defensive: every return value must be a JSON-serialisable dict of
    finite numbers."""

    def test_result_is_json_safe_floats(self):
        import json
        import math
        for spec in (
            "Fix typo",
            "drop table users",
            "design the loop",
            "do not deploy",
            "Fix README typo in /repo/money.financial",
        ):
            with self.subTest(spec=spec):
                result = t.heuristic(spec)
                serialised = json.dumps(result)
                self.assertEqual(json.loads(serialised), result)
                for key, value in result.items():
                    if isinstance(value, (int, float)):
                        self.assertTrue(math.isfinite(value))



from task_judgment import heuristic

class MetadataSafetyRegression(unittest.TestCase):
    def test_windows_path_does_not_swallow_following_action(self):
        self.assertGreaterEqual(heuristic(r"In C:\repo\files, deploy production")["destructive"], .5)

    def test_positive_action_after_unrelated_negative_clause(self):
        self.assertGreaterEqual(heuristic("Do not change docs and deploy production")["destructive"], .5)

    def test_quoted_unix_path_with_spaces_is_metadata(self):
        self.assertEqual(heuristic('Fix typo in "/tmp/my money folder/readme.md"')["tier"], "mechanical")

    def test_markdown_label_retains_task_semantics(self):
        self.assertEqual(heuristic("Fix [payment calculation](docs/reference.md)")["tier"], "high_stakes")

if __name__ == "__main__":
    unittest.main()
