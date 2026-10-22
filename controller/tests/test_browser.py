import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workflow.browser import ChromeBrowser, chrome_trace
from workflow.policy import evaluate_merge
from test_policy import NOW, valid_evidence, valid_policy


class BrowserEvidenceTests(unittest.TestCase):
    def trace(self, content, *, status="completed"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "stdout.log"
        item = {"id": "tool-1", "type": "mcp_tool_call", "server": "cua_repl", "status": status,
                "result": {"content": [{"type": "text", "text": content}]}}
        path.write_text(json.dumps({"type": "item.completed", "item": item}), encoding="utf-8")
        return {"stdout": str(path)}

    def test_agent_success_alone_is_not_browser_evidence(self):
        receipt = self.trace("I have checked everything")
        self.assertFalse(chrome_trace(receipt, [])['verified'])

    def test_failed_tool_is_not_evidence(self):
        receipt = self.trace("Browser tab: https://github.com/x/y", status="failed")
        self.assertFalse(chrome_trace(receipt, ["https://github.com/x/y"])['verified'])

    def test_expected_sha_must_be_in_returned_browser_state(self):
        receipt = self.trace("AXWebArea https://github.com/x/y/pull/1 " + "a" * 40)
        self.assertFalse(chrome_trace(receipt, ["b" * 40])['verified'])
        self.assertTrue(chrome_trace(receipt, ["a" * 40])['verified'])

    def test_browser_mutation_requires_independent_authorization(self):
        browser = ChromeBrowser({})
        task = {"repo_url": "https://github.com/x/y"}
        with self.assertRaisesRegex(RuntimeError, "authorization_required"):
            browser.ensure_pr(task, "a" * 40, "b" * 40, Path("unused"), None, 1)

    def test_repository_grant_does_not_authorize_another_task(self):
        repo = "https://github.com/x/y"
        browser = ChromeBrowser({"grants": {"create_pr": True, "approved_by": "operator",
                                           "approved_at": NOW, "repo_url": repo,
                                           "task_ids": ["confirmed-task"]}})
        with self.assertRaisesRegex(RuntimeError, "authorization_required"):
            browser.ensure_pr({"id": "other-task", "repo_url": repo}, "a" * 40, "b" * 40,
                              Path("unused"), None, 1)

    def observation_with_proofs(self, replacements=None, *, tool_quotes=None):
        baseline = valid_evidence()
        quotes = {"check": "unit has passed", "required_reviews": "No reviews required",
                  "branch_rules": "Branch protection enabled", "bypass": "Bypass disabled"}
        quotes.update(replacements or {})
        # All green fields are deliberate model claims. Only independently
        # returned CUA text may justify retaining them as merge evidence.
        result = {"status": "passed", "reason": "", "head_sha": baseline["head_sha"],
                  "base_sha": baseline["base_sha"], "merge_sha": "", "pr": deepcopy(baseline["pr"]),
                  "checks": [{"name": "unit", "status": "success", "details_url": baseline["trusted_pr_url"]}],
                  "required_reviews_satisfied": True, "branch_rules_observed": True, "bypass": False,
                  "feedback": [], "failure_kind": "none",
                  "failure_evidence": {"head_sha": "", "base_sha": "", "url": "", "observed_text": ""},
                  "proofs": {"checks": [{"name": "unit", "quote": quotes["check"]}],
                             "required_reviews": quotes["required_reviews"],
                             "branch_rules": quotes["branch_rules"], "bypass": quotes["bypass"], "merged": ""}}
        observed_quotes = list(quotes.values()) if tool_quotes is None else tool_quotes
        text = "\n".join(["Browser tab: " + baseline["trusted_pr_url"], "AXWebArea GitHub Pull request",
                           baseline["head_sha"], baseline["base_sha"], *observed_quotes])
        receipt = {**self.trace(text), "result": result}
        directory = Path(receipt["stdout"]).parent
        browser = ChromeBrowser({"repo_path": str(directory)}, codex_run=lambda *args, **kwargs: deepcopy(receipt))
        task = {"repo_url": baseline["repo_url"], "base_branch": baseline["base_branch"],
                "pr_url": baseline["trusted_pr_url"]}
        with patch("workflow.browser.utc_now", return_value=NOW):
            observed = browser.observe(task, baseline["head_sha"], baseline["base_sha"], directory, None, 10)
        evidence = {**baseline, **observed}
        return observed, evaluate_merge(valid_policy(), evidence, now=NOW)

    def assert_proof_rejected(self, observed, decision, key):
        if key == "check":
            self.assertEqual(observed["github_checks"]["results"][0]["status"], "unknown")
        else:
            field = {"required_reviews": "required_reviews_satisfied", "branch_rules": "branch_rules_observed",
                     "bypass": "bypass"}[key]
            self.assertIsNone(observed[field])
        self.assertFalse(decision["eligible"], decision)
        self.assertTrue(decision["reasons"])

    def test_explicit_positive_observation_proofs_allow_valid_merge_evidence(self):
        observed, decision = self.observation_with_proofs()
        self.assertEqual(observed["github_checks"]["results"][0]["status"], "success")
        self.assertIs(observed["required_reviews_satisfied"], True)
        self.assertIs(observed["branch_rules_observed"], True)
        self.assertIs(observed["bypass"], False)
        self.assertEqual(decision, {"eligible": True, "reasons": []})

    def test_each_negative_observation_clears_model_green_claim_and_blocks_merge(self):
        negative = {"check": "unit has not passed", "required_reviews": "Review required",
                    "branch_rules": "Branch protection disabled", "bypass": "Bypass required"}
        for key, quote in negative.items():
            with self.subTest(key=key, quote=quote):
                observed, decision = self.observation_with_proofs({key: quote})
                self.assert_proof_rejected(observed, decision, key)

    def test_original_combined_negative_browser_text_cannot_become_green_evidence(self):
        negative = {"check": "unit has not passed", "required_reviews": "Review required",
                    "branch_rules": "Branch protection disabled", "bypass": "Bypass required"}
        observed, decision = self.observation_with_proofs(negative)
        for key in negative:
            with self.subTest(key=key):
                self.assert_proof_rejected(observed, decision, key)

    def test_positive_proof_absent_from_tool_text_cannot_authorize_merge(self):
        positive = {"check": "unit has passed", "required_reviews": "No reviews required",
                    "branch_rules": "Branch protection enabled", "bypass": "Bypass disabled"}
        for missing in positive:
            with self.subTest(missing=missing):
                tool_quotes = [quote for key, quote in positive.items() if key != missing]
                observed, decision = self.observation_with_proofs(tool_quotes=tool_quotes)
                self.assert_proof_rejected(observed, decision, missing)

    def test_positive_words_with_conflicting_suffixes_are_not_positive_proofs(self):
        contradictory = {"check": "unit has passed but failed", "required_reviews": "No reviews required, changes requested",
                         "branch_rules": "Branch protection enabled but disabled",
                         "bypass": "Bypass disabled but administrator bypass required"}
        for key, quote in contradictory.items():
            with self.subTest(key=key, quote=quote):
                observed, decision = self.observation_with_proofs({key: quote})
                self.assert_proof_rejected(observed, decision, key)

    def test_positive_substring_cannot_hide_conflict_in_complete_tool_line(self):
        positive = {"check": "unit has passed", "required_reviews": "No reviews required",
                    "branch_rules": "Branch protection enabled", "bypass": "Bypass disabled"}
        contradictory = {"check": "unit has passed but failed", "required_reviews": "No reviews required, changes requested",
                         "branch_rules": "Branch protection enabled but disabled",
                         "bypass": "Bypass disabled but administrator bypass required"}
        for key, complete_line in contradictory.items():
            with self.subTest(key=key, complete_line=complete_line):
                # The model quotes only the supported positive prefix. The
                # actual CUA line retains its contradictory suffix, which must
                # not be erased by substring-based proof matching.
                tool_quotes = [complete_line if field == key else quote for field, quote in positive.items()]
                observed, decision = self.observation_with_proofs(tool_quotes=tool_quotes)
                self.assert_proof_rejected(observed, decision, key)

    def test_unknown_proof_semantics_clear_model_green_claims(self):
        unknown = {"check": "unit finished", "required_reviews": "Review status unavailable",
                   "branch_rules": "Protection information unavailable", "bypass": "Unknown bypass state"}
        for key, quote in unknown.items():
            with self.subTest(key=key, quote=quote):
                observed, decision = self.observation_with_proofs({key: quote})
                self.assert_proof_rejected(observed, decision, key)

    def merged_observation(self, quote, actual_line, *, include_sha=True):
        baseline = valid_evidence()
        merge_sha = "c" * 40
        proofs = {"checks": [], "required_reviews": "", "branch_rules": "", "bypass": ""}
        if quote is not None:
            proofs["merged"] = quote
        result = {"status": "passed", "reason": "", "head_sha": baseline["head_sha"],
                  "base_sha": baseline["base_sha"], "merge_sha": merge_sha,
                  "pr": {**baseline["pr"], "state": "merged", "mergeable": False}, "checks": [],
                  "required_reviews_satisfied": None, "branch_rules_observed": None, "bypass": None,
                  "feedback": [], "failure_kind": "none", "proofs": proofs,
                  "failure_evidence": {"head_sha": "", "base_sha": "", "url": "", "observed_text": ""}}
        text = "\n".join(["Browser tab: " + baseline["trusted_pr_url"], "AXWebArea GitHub Pull request",
                           baseline["head_sha"], baseline["base_sha"], merge_sha if include_sha else "", actual_line])
        receipt = {**self.trace(text), "result": result}
        directory = Path(receipt["stdout"]).parent
        browser = ChromeBrowser({"repo_path": str(directory)}, codex_run=lambda *args, **kwargs: deepcopy(receipt))
        task = {"repo_url": baseline["repo_url"], "base_branch": baseline["base_branch"],
                "pr_url": baseline["trusted_pr_url"]}
        return browser.observe(task, baseline["head_sha"], baseline["base_sha"], directory, None, 10)

    def merged_action(self, quote, actual_line, *, include_sha=True):
        baseline = valid_evidence()
        merge_sha = "c" * 40
        result = {"status": "merged", "merge_sha": merge_sha, "pr_url": baseline["trusted_pr_url"], "reason": ""}
        if quote is not None:
            result["merged_proof"] = quote
        text = "\n".join(["Browser tab: " + baseline["trusted_pr_url"], "AXWebArea GitHub Pull request",
                           merge_sha if include_sha else "", actual_line])
        receipt = {**self.trace(text), "result": result}
        directory = Path(receipt["stdout"]).parent
        config = {"repo_path": str(directory), "merge_policy": valid_policy(),
                  "grants": {"repo_url": baseline["repo_url"], "task_ids": ["confirmed-task"],
                             "approved_by": "Berlin", "approved_at": "2026-10-22T11:50:00Z", "merge": True}}
        browser = ChromeBrowser(config, codex_run=lambda *args, **kwargs: deepcopy(receipt))
        task = {"id": "confirmed-task", "repo_url": baseline["repo_url"],
                "base_branch": baseline["base_branch"], "pr_url": baseline["trusted_pr_url"]}
        # Keep the real deterministic merge gate while freezing its clock.
        with patch("workflow.browser.evaluate_merge", side_effect=lambda policy, evidence: evaluate_merge(policy, evidence, now=NOW)):
            return browser.merge(task, baseline, directory, None, 10)

    def test_merged_observation_rejects_negative_trimmed_or_missing_proof(self):
        for quote, actual_line in (("PR not merged", "PR not merged"),
                                   ("Merged", "Merged but failed to merge"),
                                   (None, "PR merged")):
            with self.subTest(quote=quote, actual_line=actual_line):
                with self.assertRaisesRegex(RuntimeError, "merge_sha_not_observed"):
                    self.merged_observation(quote, actual_line)
        with self.assertRaisesRegex(RuntimeError, "merge_sha_not_observed"):
            self.merged_observation("PR merged", "PR merged", include_sha=False)

    def test_merged_observation_accepts_supported_whole_line_proof_and_actual_sha(self):
        for label in ("Merged", "PR merged", "Pull request merged", "Pull request successfully merged and closed"):
            with self.subTest(label=label):
                observed = self.merged_observation(label, label)
                self.assertEqual(observed["pr"]["state"], "merged")
                self.assertEqual(observed["merge_sha"], "c" * 40)

    def test_merge_action_rejects_negative_trimmed_or_missing_proof(self):
        for quote, actual_line in (("PR not merged", "PR not merged"),
                                   ("Merged", "Merged but failed to merge"),
                                   (None, "PR merged")):
            with self.subTest(quote=quote, actual_line=actual_line):
                with self.assertRaisesRegex(RuntimeError, "merge_result_not_observed"):
                    self.merged_action(quote, actual_line)
        with self.assertRaisesRegex(RuntimeError, "merge_result_not_observed"):
            self.merged_action("PR merged", "PR merged", include_sha=False)

    def test_merge_action_accepts_supported_whole_line_proof_and_actual_sha(self):
        for label in ("Merged", "PR merged", "Pull request merged", "Pull request successfully merged and closed"):
            with self.subTest(label=label):
                result = self.merged_action(label, label)
                self.assertEqual(result["status"], "merged")
                self.assertEqual(result["merge_sha"], "c" * 40)
