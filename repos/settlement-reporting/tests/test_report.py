"""Isolated unit checks for the count equation helper (CONTRACT section 5)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from settlement_reporting.common import BusinessRuleError
from settlement_reporting.report import COUNT_EQUATIONS, assert_count_equation


class TestCountEquation(unittest.TestCase):
    def test_happy_path_passes(self) -> None:
        assert_count_equation(
            {
                "received": 6,
                "rejected": 0,
                "duplicatesRemoved": 0,
                "accepted": 6,
                "cutoffAdjusted": 0,
                "posted": 6,
                "matched": 6,
                "broken": 0,
            }
        )

    def test_reconciliation_breaks_passes(self) -> None:
        assert_count_equation(
            {
                "received": 11,
                "rejected": 2,
                "duplicatesRemoved": 0,
                "accepted": 9,
                "cutoffAdjusted": 0,
                "posted": 9,
                "matched": 5,
                "broken": 4,
            }
        )

    def test_duplicate_retry_passes(self) -> None:
        assert_count_equation(
            {
                "received": 8,
                "rejected": 0,
                "duplicatesRemoved": 2,
                "accepted": 6,
                "cutoffAdjusted": 0,
                "posted": 6,
                "matched": 6,
                "broken": 0,
            }
        )

    def test_mismatched_accepted_posted_fails(self) -> None:
        with self.assertRaises(BusinessRuleError) as ctx:
            assert_count_equation(
                {
                    "received": 6,
                    "rejected": 0,
                    "duplicatesRemoved": 0,
                    "accepted": 6,
                    "cutoffAdjusted": 0,
                    "posted": 5,
                    "matched": 5,
                    "broken": 0,
                }
            )
        self.assertEqual(ctx.exception.code, "COUNT_EQUATION_VIOLATION")
        self.assertIn("accepted=6 posted=5", str(ctx.exception))

    def test_mismatched_received_fails(self) -> None:
        with self.assertRaises(BusinessRuleError) as ctx:
            assert_count_equation(
                {
                    "received": 7,
                    "rejected": 0,
                    "duplicatesRemoved": 0,
                    "accepted": 6,
                    "cutoffAdjusted": 0,
                    "posted": 6,
                    "matched": 6,
                    "broken": 0,
                }
            )
        self.assertEqual(ctx.exception.code, "COUNT_EQUATION_VIOLATION")
        self.assertIn("received=7", str(ctx.exception))

    def test_mismatched_breaks_fails(self) -> None:
        with self.assertRaises(BusinessRuleError):
            assert_count_equation(
                {
                    "received": 6,
                    "rejected": 0,
                    "duplicatesRemoved": 0,
                    "accepted": 6,
                    "cutoffAdjusted": 0,
                    "posted": 6,
                    "matched": 4,
                    "broken": 1,
                }
            )

    def test_three_equations_present(self) -> None:
        self.assertEqual(len(COUNT_EQUATIONS), 3)


if __name__ == "__main__":
    unittest.main()
