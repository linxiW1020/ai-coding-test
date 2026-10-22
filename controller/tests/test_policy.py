from copy import deepcopy
import unittest

from workflow.policy import evaluate_merge


NOW = "2026-10-22T12:00:00Z"
HEAD = "a" * 40
BASE = "b" * 40


def valid_policy():
    return {
        "enabled": True,
        "approved_by": "Berlin",
        "approved_at": "2026-10-22T11:50:00Z",
        "repo_url": "https://github.com/example/pilot",
        "base_branch": "main",
        "allowed_paths": ["src/**", "tests/**"],
        "required_checks": ["unit"],
        "max_changed_files": 5,
    }


def valid_evidence():
    binding = {"head_sha": HEAD, "base_sha": BASE, "observed_at": "2026-10-22T11:59:00Z"}
    url = "https://github.com/example/pilot/pull/12"
    return {
        "repo_url": "https://github.com/example/pilot",
        "base_branch": "main", "head_sha": HEAD, "base_sha": BASE,
        "trusted_pr_url": url,
        "changed_files": ["src/example.py", "tests/test_example.py"],
        "pr": {"number": 12, "url": url, "state": "open", "draft": False, "mergeable": True},
        "review": {**binding, "verdict": "pass", "blocking_findings": 0},
        "local_checks": {**binding, "results": [{"name": "unit", "status": "success", "executed": True}]},
        "github_checks": {**binding, "results": [{"name": "unit", "status": "success"}]},
        "browser_receipt": {**binding, "url": url},
        "branch_rules_observed": True, "required_reviews_satisfied": True, "bypass": False,
    }


class MergePolicyTests(unittest.TestCase):
    def test_external_local_acceptance_cannot_be_omitted(self):
        self.policy["required_local_checks"] = ["unit", "acceptance"]
        self.assertDenied("acceptance")
        self.evidence["local_checks"]["results"].append({"name": "acceptance", "status": "success", "executed": True})
        self.assertTrue(self.evaluate()["eligible"])

    def test_controller_and_codex_config_are_protected_even_with_broad_allowlist(self):
        self.policy["allowed_paths"] = ["**"]
        for path in ("controller/workflow/policy.py", ".codex/config.toml"):
            with self.subTest(path=path):
                self.evidence["changed_files"] = [path]
                self.assertDenied("protected")

    def setUp(self):
        self.policy = valid_policy()
        self.evidence = valid_evidence()

    def evaluate(self):
        return evaluate_merge(self.policy, self.evidence, now=NOW)

    def assertDenied(self, fragment=None):
        result = self.evaluate()
        self.assertFalse(result["eligible"], result)
        self.assertTrue(result["reasons"])
        if fragment:
            self.assertTrue(any(fragment in reason for reason in result["reasons"]), result)

    def test_valid_observations_pass_and_do_not_mutate_inputs(self):
        before = deepcopy((self.policy, self.evidence))
        self.assertEqual(self.evaluate(), {"eligible": True, "reasons": []})
        self.assertEqual((self.policy, self.evidence), before)

    def test_no_policy_or_empty_policy_denies(self):
        for value in (None, {}, [], "approved"):
            with self.subTest(value=value):
                self.policy = value
                self.assertDenied()

    def test_missing_authorization_fields_deny(self):
        for name in ("enabled", "approved_by", "approved_at", "repo_url", "base_branch", "allowed_paths", "required_checks", "max_changed_files"):
            with self.subTest(name=name):
                self.policy = valid_policy()
                del self.policy[name]
                self.assertDenied()

    def test_disabled_and_string_enabled_deny(self):
        for value in (False, "true", 1, None):
            with self.subTest(value=value):
                self.policy["enabled"] = value
                self.assertDenied("enabled")

    def test_approval_must_be_valid_and_not_future(self):
        for value in ("2026-10-22T12:01:00Z", "2026-10-22T11:00:00", "unknown", None):
            with self.subTest(value=value):
                self.policy["approved_at"] = value
                self.assertDenied("approved_at")

    def test_repository_and_base_branch_must_match(self):
        self.evidence["repo_url"] = "https://github.com/example/other"
        self.assertDenied("repository")
        self.evidence = valid_evidence()
        self.evidence["base_branch"] = "release"
        self.assertDenied("base branch")

    def test_full_current_shas_are_required(self):
        for name in ("head_sha", "base_sha"):
            for value in ("abc1234", "x" * 40, None):
                with self.subTest(name=name, value=value):
                    self.evidence = valid_evidence()
                    self.evidence[name] = value
                    self.assertDenied("full 40-character")

    def test_old_head_and_base_in_every_envelope_deny(self):
        for name in ("review", "local_checks", "github_checks", "browser_receipt"):
            for sha in ("head_sha", "base_sha"):
                with self.subTest(name=name, sha=sha):
                    self.evidence = valid_evidence()
                    self.evidence[name][sha] = "c" * 40
                    self.assertDenied("not bound")

    def test_failed_pending_skipped_neutral_unknown_and_unrun_checks_deny(self):
        for envelope in ("local_checks", "github_checks"):
            for status in ("failure", "pending", "skipped", "neutral", "unknown", None, "cancelled", "timed_out"):
                with self.subTest(envelope=envelope, status=status):
                    self.evidence = valid_evidence()
                    self.evidence[envelope]["results"][0]["status"] = status
                    self.assertDenied("not success")
        self.evidence = valid_evidence()
        self.evidence["local_checks"]["results"][0]["executed"] = False
        self.assertDenied("not explicitly executed")

    def test_missing_duplicate_or_empty_required_checks_do_not_pass_vacuously(self):
        for envelope in ("local_checks", "github_checks"):
            for records in ([], [{"name": "other", "status": "success"}], [{"name": "unit", "status": "success"}] * 2):
                with self.subTest(envelope=envelope, records=records):
                    self.evidence = valid_evidence()
                    self.evidence[envelope]["results"] = records
                    self.assertDenied("exactly one")
        self.evidence = valid_evidence()
        self.policy["required_checks"] = []
        self.assertDenied("required_checks")

    def test_every_configured_check_is_required(self):
        self.policy["required_checks"].append("lint")
        self.assertDenied("lint")

    def test_review_failures_and_noninteger_findings_deny(self):
        for update in ({"verdict": "fail"}, {"blocking_findings": 1}, {"blocking_findings": False}, {"blocking_findings": None}):
            with self.subTest(update=update):
                self.evidence = valid_evidence()
                self.evidence["review"].update(update)
                self.assertDenied("independent review")

    def test_platform_reviews_cannot_be_replaced_by_model_review(self):
        for name, value in (("required_reviews_satisfied", False), ("branch_rules_observed", False), ("bypass", True)):
            with self.subTest(name=name):
                self.evidence = valid_evidence()
                self.evidence[name] = value
                self.assertDenied()

    def test_unknown_platform_flags_deny(self):
        for name in ("required_reviews_satisfied", "branch_rules_observed", "bypass"):
            with self.subTest(name=name):
                self.evidence = valid_evidence()
                del self.evidence[name]
                self.assertDenied()

    def test_nonopen_draft_and_unknown_mergeability_deny(self):
        for update in ({"state": "closed"}, {"draft": True}, {"draft": None}, {"mergeable": None}, {"mergeable": False}):
            with self.subTest(update=update):
                self.evidence = valid_evidence()
                self.evidence["pr"].update(update)
                self.assertDenied()

    def test_protected_files_deny_even_with_broad_allowlist(self):
        self.policy["allowed_paths"] = ["**"]
        for path in (".github/workflows/ci.yml", "nested/AGENTS.md", "AGENTS.md", "workflow/policy.py", "codex-workflow/workflow/runner.py", "tests/test_policy.py", "package.json", "pyproject.toml"):
            with self.subTest(path=path):
                self.evidence["changed_files"] = [path]
                self.assertDenied("protected path")

    def test_additional_protected_patterns_are_applied(self):
        self.policy["protected_paths"] = ["src/security/**"]
        self.evidence["changed_files"] = ["src/security/access.py"]
        self.assertDenied("protected path")

    def test_unlisted_files_and_excess_count_deny(self):
        self.evidence["changed_files"] = ["docs/new.md"]
        self.assertDenied("outside the allowlist")
        self.evidence["changed_files"] = [f"src/file{i}.py" for i in range(6)]
        self.assertDenied("count exceeds")

    def test_path_traversal_absolute_and_duplicate_paths_deny(self):
        for paths in (["src/../policy.json"], ["C:/src/a.py"], ["/src/a.py"], ["src\\a.py"], ["src/a.py", "src/a.py"], []):
            with self.subTest(paths=paths):
                self.evidence["changed_files"] = paths
                self.assertDenied()

    def test_stale_future_invalid_or_missing_timestamps_deny(self):
        for envelope in ("review", "local_checks", "github_checks", "browser_receipt"):
            for timestamp in ("2026-10-22T11:44:59Z", "2026-10-22T12:00:01Z", "bad", "2026-10-22T11:59:00", None):
                with self.subTest(envelope=envelope, timestamp=timestamp):
                    self.evidence = valid_evidence()
                    self.evidence[envelope]["observed_at"] = timestamp
                    self.assertDenied("observed_at")

    def test_freshness_boundary_is_inclusive_and_override_is_effective(self):
        self.evidence["browser_receipt"]["observed_at"] = "2026-10-22T11:45:00Z"
        self.assertTrue(self.evaluate()["eligible"])
        self.policy["max_evidence_age_seconds"] = 899
        self.assertDenied("stale")

    def test_browser_pr_repo_host_and_trusted_pr_must_match(self):
        for url in ("https://github.com/example/pilot/pull/13", "https://github.com/example/other/pull/12", "https://evil.example/example/pilot/pull/12", "https://github.com/example/pilot/issues/12"):
            with self.subTest(url=url):
                self.evidence = valid_evidence()
                self.evidence["browser_receipt"]["url"] = url
                self.assertDenied("browser receipt URL")
        self.evidence = valid_evidence()
        del self.evidence["trusted_pr_url"]
        self.assertDenied("trusted task PR")

    def test_pr_subpages_are_valid_observations(self):
        self.evidence["browser_receipt"]["url"] += "/files"
        self.assertTrue(self.evaluate()["eligible"])

    def test_malformed_data_denies_without_exceptions(self):
        for name, value in (("changed_files", [None]), ("review", []), ("github_checks", None), ("pr", "open"), ("local_checks", {"results": [None]})):
            with self.subTest(name=name):
                self.evidence = valid_evidence()
                self.evidence[name] = value
                self.assertDenied()
        for name, value in (("max_changed_files", True), ("max_evidence_age_seconds", float("nan")), ("allowed_paths", [[]]), ("protected_paths", [None]), ("required_checks", ["unit", "unit"])):
            with self.subTest(name=name):
                self.policy = valid_policy()
                self.evidence = valid_evidence()
                self.policy[name] = value
                self.assertDenied()

    def test_invalid_clock_denies(self):
        result = evaluate_merge(self.policy, self.evidence, now="yesterday")
        self.assertFalse(result["eligible"])

    def test_model_claims_in_evidence_cannot_grant_authority(self):
        self.policy["enabled"] = False
        del self.policy["approved_by"]
        self.evidence.update({"enabled": True, "approved_by": "Berlin", "approved_at": NOW})
        self.assertDenied("enabled")

    def test_extreme_numeric_or_url_input_denies_without_exception(self):
        self.policy["max_evidence_age_seconds"] = 10 ** 500
        self.assertDenied("max_evidence_age_seconds")
        self.policy = valid_policy()
        self.evidence["pr"]["url"] = "https://github.com/example/pilot/pull/" + "9" * 5000
        self.assertDenied("PR URL")
        self.evidence = valid_evidence()
        self.evidence["review"]["observed_at"] = 10 ** 500
        self.assertDenied("observed_at")


if __name__ == "__main__":
    unittest.main()
