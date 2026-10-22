import unittest

from src.ci_status import check_succeeded, required_checks_succeeded


class CiStatusBaselineTests(unittest.TestCase):
    def test_success_is_success(self):
        self.assertTrue(check_succeeded("success"))

    def test_individual_non_success_is_rejected(self):
        for status in ("failure", "skipped", "pending", "neutral", "unknown", None):
            with self.subTest(status=status):
                self.assertFalse(check_succeeded(status))

    def test_all_required_succeeded(self):
        self.assertTrue(required_checks_succeeded({"unit": "success", "lint": "success"}, ["unit", "lint"]))

    def test_missing_check_is_rejected(self):
        self.assertFalse(required_checks_succeeded({}, ["unit"]))

    def test_empty_required_list_is_rejected(self):
        self.assertFalse(required_checks_succeeded({"unit": "success"}, []))
