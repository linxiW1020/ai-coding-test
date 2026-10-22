from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from workflow.review_evidence import review_inputs


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.log = self.root / "check.log"
        self.log.write_bytes(b"Ran 9 tests\nOK\n")
        self.checks = {"results": [{"name": "unit", "status": "success", "executed": True,
                                   "returncode": 0, "stdout": str(self.log), "stderr": str(self.log)}]}

    def test_actual_diff_and_log_contents_are_supplied_independently(self):
        diff = "--- a/src/value.py\n+++ b/src/value.py\n-true\n+false\n"
        result = review_inputs(diff, self.checks, self.root)
        self.assertEqual(result["diff"], diff)
        self.assertEqual(len(result["diff_sha256"]), 64)
        self.assertEqual(result["checks"][0]["stderr"]["tail"], "Ran 9 tests\nOK\n")
        self.assertFalse(result["checks"][0]["stderr"]["truncated"])

    def test_log_tails_are_bounded_and_truncation_is_explicit(self):
        self.log.write_bytes(b"x" * 20000 + b"\nFAILURE\n")
        result = review_inputs("diff", self.checks, self.root)
        tail = result["checks"][0]["stderr"]
        self.assertTrue(tail["truncated"])
        self.assertTrue(tail["tail"].endswith("FAILURE\n"))
        self.assertLessEqual(len(tail["tail"].encode("utf-8")), 8192)

    def test_missing_logs_and_external_paths_block_instead_of_claiming_review_evidence(self):
        for value in (None, "", str(self.root / "missing.log"), str(Path(__file__).resolve())):
            with self.subTest(value=value):
                checks = deepcopy(self.checks)
                checks["results"][0]["stdout"] = value
                with self.assertRaises((ValueError, OSError)):
                    review_inputs("diff", checks, self.root)

    def test_no_missing_failed_or_unexecuted_check_can_be_reviewed_as_success(self):
        for patch in ({"status": "failure"}, {"returncode": True}, {"executed": False}, {"returncode": 1}):
            with self.subTest(patch=patch):
                checks = deepcopy(self.checks)
                checks["results"][0].update(patch)
                with self.assertRaises(ValueError):
                    review_inputs("diff", checks, self.root)
        with self.assertRaises(ValueError):
            review_inputs("diff", {"results": []}, self.root)

    def test_missing_and_oversized_diff_are_rejected_without_truncating_it(self):
        for diff in ("", "  ", "x" * 262145):
            with self.subTest(length=len(diff)):
                with self.assertRaises(ValueError):
                    review_inputs(diff, self.checks, self.root)
