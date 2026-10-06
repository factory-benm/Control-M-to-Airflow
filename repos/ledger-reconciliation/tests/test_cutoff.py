"""Isolated unit checks for the business-day cutoff rule (CONTRACT 6.4)."""

import unittest
from datetime import date

from ledger_reconciliation.cutoff import (
    compute_effective_value_date,
    is_business_day,
    next_business_day,
)


HOLIDAYS = {
    date(2026, 3, 20),  # Hari Raya Puasa (Friday)
}
WEEKEND = {5, 6}  # Saturday, Sunday


class TestBusinessDay(unittest.TestCase):
    def test_weekday_is_business_day(self):
        self.assertTrue(is_business_day(date(2026, 3, 19), HOLIDAYS, WEEKEND))

    def test_saturday_is_not_business_day(self):
        self.assertFalse(is_business_day(date(2026, 3, 21), HOLIDAYS, WEEKEND))

    def test_holiday_is_not_business_day(self):
        self.assertFalse(is_business_day(date(2026, 3, 20), HOLIDAYS, WEEKEND))


class TestNextBusinessDay(unittest.TestCase):
    def test_skip_holiday_then_weekend(self):
        # Thursday 2026-03-19 -> Friday is holiday -> Sat/Sun weekend -> Mon 23
        self.assertEqual(
            next_business_day(date(2026, 3, 19), HOLIDAYS, WEEKEND),
            date(2026, 3, 23),
        )

    def test_from_saturday(self):
        self.assertEqual(
            next_business_day(date(2026, 3, 21), HOLIDAYS, WEEKEND),
            date(2026, 3, 23),
        )


class TestCutoffBoundary(unittest.TestCase):
    """Booking exactly at the cutoff rolls forward (strict <)."""

    def test_exactly_at_cutoff_rolls_forward(self):
        # 2026-03-19T09:00:00Z = 17:00 Asia/Singapore. Not < 17:00 -> roll.
        eff, rolled = compute_effective_value_date(
            "2026-03-19T09:00:00Z", "Asia/Singapore", "17:00", HOLIDAYS, WEEKEND
        )
        self.assertEqual(eff, date(2026, 3, 23))
        self.assertTrue(rolled)

    def test_one_minute_before_cutoff_stays(self):
        # 2026-03-19T08:59:00Z = 16:59 SGT. < 17:00 -> same day.
        eff, rolled = compute_effective_value_date(
            "2026-03-19T08:59:00Z", "Asia/Singapore", "17:00", HOLIDAYS, WEEKEND
        )
        self.assertEqual(eff, date(2026, 3, 19))
        self.assertFalse(rolled)


if __name__ == "__main__":
    unittest.main()
