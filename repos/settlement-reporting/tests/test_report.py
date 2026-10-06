"""Isolated unit checks for the count equation helper (CONTRACT section 5)."""

import unittest

from settlement_reporting.report import COUNT_EQUATIONS, assert_count_equation


class TestCountEquation(unittest.TestCase):
    def test_happy_path_passes(self):
        assert_count_equation({
            "received": 6, "rejected": 0, "duplicatesRemoved": 0,
            "accepted": 6, "cutoffAdjusted": 0, "posted": 6,
            "matched": 6, "broken": 0,
        })

    def test_reconciliation_breaks_passes(self):
        assert_count_equation({
            "received": 11, "rejected": 2, "duplicatesRemoved": 0,
            "accepted": 9, "cutoffAdjusted": 0, "posted": 9,
            "matched": 5, "broken": 4,
        })

    def test_duplicate_retry_passes(self):
        assert_count_equation({
            "received": 8, "rejected": 0, "duplicatesRemoved": 2,
            "accepted": 6, "cutoffAdjusted": 0, "posted": 6,
            "matched": 6, "broken": 0,
        })

    def test_mismatched_accepted_posted_fails(self):
        with self.assertRaises(Exception):
            assert_count_equation({
                "received": 6, "rejected": 0, "duplicatesRemoved": 0,
                "accepted": 6, "cutoffAdjusted": 0, "posted": 5,
                "matched": 5, "broken": 0,
            })

    def test_mismatched_received_fails(self):
        with self.assertRaises(Exception):
            assert_count_equation({
                "received": 7, "rejected": 0, "duplicatesRemoved": 0,
                "accepted": 6, "cutoffAdjusted": 0, "posted": 6,
                "matched": 6, "broken": 0,
            })

    def test_three_equations_present(self):
        self.assertEqual(len(COUNT_EQUATIONS), 3)


if __name__ == "__main__":
    unittest.main()
