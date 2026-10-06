"""Unit tests for payment-validation pure functions (standard library only)."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from payment_validation.tasks import validate_row, deduplicate  # noqa: E402

ALLOW = {"AED", "CNY", "EUR", "GBP", "HKD", "INR", "JPY", "KES", "SGD", "USD"}


def _good_row(payment_id="PAY-2026-0001", currency="SGD", amount="100.00"):
    return {
        "payment_id": payment_id,
        "source_system": "CHANNEL-SG",
        "value_date": "2026-03-16",
        "booking_timestamp": "2026-03-16T01:15:00Z",
        "debtor_account_token": "DEBTOR-0001",
        "creditor_account_token": "CREDITOR-0101",
        "currency": currency,
        "amount": amount,
        "ledger_reference": "LEDG-SG-0001",
        "country_code": "SG",
        "line_number": 1,
    }


class TestValidateRow(unittest.TestCase):
    def test_good_row_accepted(self):
        code, _ = validate_row(_good_row(), ALLOW)
        self.assertIsNone(code)

    def test_missing_field(self):
        row = _good_row()
        row["currency"] = ""
        code, detail = validate_row(row, ALLOW)
        self.assertEqual(code, "MISSING_REQUIRED_FIELD")
        self.assertIn("currency", detail)

    def test_malformed_field(self):
        row = _good_row()
        row["payment_id"] = "BAD-ID"
        code, _ = validate_row(row, ALLOW)
        self.assertEqual(code, "MALFORMED_FIELD")

    def test_currency_not_allowed(self):
        row = _good_row(currency="XYZ")
        code, _ = validate_row(row, ALLOW)
        self.assertEqual(code, "CURRENCY_NOT_ALLOWED")

    def test_non_positive_amount(self):
        row = _good_row(amount="0.00")
        code, _ = validate_row(row, ALLOW)
        self.assertEqual(code, "NON_POSITIVE_AMOUNT")

    def test_negative_amount(self):
        row = _good_row(amount="-5.00")
        code, _ = validate_row(row, ALLOW)
        self.assertEqual(code, "NON_POSITIVE_AMOUNT")

    def test_order_currency_before_amount(self):
        # A row with both a bad currency and a zero amount must report currency
        # first, because currency is evaluated before amount.
        row = _good_row(currency="XYZ", amount="0.00")
        code, _ = validate_row(row, ALLOW)
        self.assertEqual(code, "CURRENCY_NOT_ALLOWED")


class TestDeduplicate(unittest.TestCase):
    def test_keeps_first_occurrence(self):
        a = _good_row("PAY-2026-0001")
        a["line_number"] = 1
        b = _good_row("PAY-2026-0001")
        b["line_number"] = 4
        accepted, duplicates = deduplicate([a, b])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["line_number"], 1)
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["duplicate_of_line"], 1)

    def test_unique_all_accepted(self):
        rows = [_good_row("PAY-2026-0001"), _good_row("PAY-2026-0002")]
        rows[0]["line_number"] = 1
        rows[1]["line_number"] = 2
        accepted, duplicates = deduplicate(rows)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len(duplicates), 0)

    def test_input_file_order_not_payment_id_order(self):
        # line 2 has a lower payment_id than line 1; first in file order wins.
        first = _good_row("PAY-2026-0009")
        first["line_number"] = 1
        second = _good_row("PAY-2026-0001")
        second["line_number"] = 2
        accepted, duplicates = deduplicate([first, second])
        self.assertEqual(len(accepted), 2)


if __name__ == "__main__":
    unittest.main()
