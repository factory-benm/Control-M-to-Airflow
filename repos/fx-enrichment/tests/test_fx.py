"""Unit tests for fx-enrichment pure functions (standard library only)."""

import unittest
from decimal import Decimal
from pathlib import Path

from fx_support import SRC

from fx_enrichment.tasks import compute_base_amount, lookup_rate


class TestComputeBaseAmount(unittest.TestCase):
    def test_basic_multiplication(self) -> None:
        # 12500.00 * 0.745000 = 9312.50
        self.assertEqual(str(compute_base_amount("12500.00", "0.745000")), "9312.50")

    def test_usd_identity(self) -> None:
        self.assertEqual(str(compute_base_amount("48000.00", "1.000000")), "48000.00")

    def test_jpy_large_amount(self) -> None:
        # 3800000.00 * 0.006700 = 25460.00
        self.assertEqual(str(compute_base_amount("3800000.00", "0.006700")), "25460.00")

    def test_round_half_up(self) -> None:
        # 0.005 * 1 = 0.005 -> ROUND_HALF_UP -> 0.01
        self.assertEqual(str(compute_base_amount("0.01", "0.5")), "0.01")
        # 0.015 * 1 = 0.015 -> ROUND_HALF_UP -> 0.02
        self.assertEqual(str(compute_base_amount("0.02", "0.75")), "0.02")

    def test_quantize_after_multiplication(self) -> None:
        # rate has 6 dp; quantize to 2 dp only after the product.
        self.assertEqual(str(compute_base_amount("72500.00", "0.272000")), "19720.00")

    def test_no_binary_float(self) -> None:
        # Confirm the result is a Decimal, not a float.
        result = compute_base_amount("100.00", "0.745000")
        self.assertIsInstance(result, Decimal)

    def test_package_imported_from_repo_src(self) -> None:
        import fx_enrichment

        self.assertEqual(
            Path(fx_enrichment.__file__).resolve(), SRC / "fx_enrichment" / "__init__.py"
        )


class TestLookupRate(unittest.TestCase):
    def setUp(self) -> None:
        self.rates = {
            "2026-03-16": {"SGD": "0.745000", "USD": "1.000000"},
            "2026-03-17": {"SGD": "0.744500"},
        }

    def test_found(self) -> None:
        self.assertEqual(lookup_rate(self.rates, "2026-03-16", "SGD"), "0.745000")

    def test_missing_date(self) -> None:
        self.assertIsNone(lookup_rate(self.rates, "2026-03-99", "SGD"))

    def test_missing_currency(self) -> None:
        self.assertIsNone(lookup_rate(self.rates, "2026-03-17", "USD"))


if __name__ == "__main__":
    unittest.main()
