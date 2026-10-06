"""Isolated unit checks for break classification (CONTRACT 6.7)."""

import unittest

from exception_management.classify import CLASSIFICATION, classify


class TestClassification(unittest.TestCase):
    def test_missing_ledger_reference(self):
        c = classify("MISSING_LEDGER_REFERENCE")
        self.assertEqual(c["severity"], "high")
        self.assertEqual(c["owner_team"], "Nostro Operations")

    def test_currency_mismatch(self):
        c = classify("CURRENCY_MISMATCH")
        self.assertEqual(c["severity"], "high")
        self.assertEqual(c["owner_team"], "FX Operations")

    def test_amount_mismatch(self):
        c = classify("AMOUNT_MISMATCH")
        self.assertEqual(c["severity"], "medium")
        self.assertEqual(c["owner_team"], "Reconciliation Operations")

    def test_unmatched_payment(self):
        c = classify("UNMATCHED_PAYMENT")
        self.assertEqual(c["severity"], "medium")
        self.assertEqual(c["owner_team"], "Payments Investigations")

    def test_all_reason_codes_covered(self):
        self.assertEqual(
            set(CLASSIFICATION.keys()),
            {"MISSING_LEDGER_REFERENCE", "CURRENCY_MISMATCH",
             "AMOUNT_MISMATCH", "UNMATCHED_PAYMENT"},
        )


if __name__ == "__main__":
    unittest.main()
