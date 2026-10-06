"""Isolated unit checks for reconciliation break evaluation (CONTRACT 6.6)."""

import unittest

from ledger_reconciliation.reconcile import _evaluate


def _posted(currency="SGD", amount="12500.00", payment_id="PAY-2026-0001"):
    return {"payment_id": payment_id, "currency": currency, "amount": amount}


class TestBreakEvaluation(unittest.TestCase):
    def test_matched(self):
        ref = {"payment_id": "PAY-2026-0001", "currency": "SGD", "amount": "12500.00"}
        outcome, _ = _evaluate(_posted(), ref)
        self.assertEqual(outcome, "matched")

    def test_missing_ledger_reference(self):
        outcome, detail = _evaluate(_posted(), None)
        self.assertEqual(outcome, "MISSING_LEDGER_REFERENCE")
        self.assertIsNone(detail["expected_payment_id"])
        self.assertEqual(detail["actual_currency"], "SGD")

    def test_unmatched_payment(self):
        ref = {"payment_id": "PAY-2026-0099", "currency": "SGD", "amount": "12500.00"}
        outcome, detail = _evaluate(_posted(), ref)
        self.assertEqual(outcome, "UNMATCHED_PAYMENT")
        self.assertEqual(detail["expected_payment_id"], "PAY-2026-0099")

    def test_currency_mismatch(self):
        ref = {"payment_id": "PAY-2026-0001", "currency": "USD", "amount": "12500.00"}
        outcome, detail = _evaluate(_posted(), ref)
        self.assertEqual(outcome, "CURRENCY_MISMATCH")
        self.assertEqual(detail["expected_currency"], "USD")
        self.assertEqual(detail["actual_currency"], "SGD")

    def test_amount_mismatch(self):
        ref = {"payment_id": "PAY-2026-0001", "currency": "SGD", "amount": "78500.00"}
        outcome, detail = _evaluate(_posted(amount="78000.00"), ref)
        self.assertEqual(outcome, "AMOUNT_MISMATCH")
        self.assertEqual(detail["expected_amount"], "78500.00")
        self.assertEqual(detail["actual_amount"], "78000.00")


if __name__ == "__main__":
    unittest.main()
