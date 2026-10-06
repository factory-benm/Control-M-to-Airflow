"""The apply_business_day_cutoff task against the Singapore business calendar."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ledger_test_support import (
    KIT_ROOT,
    SCENARIOS,
    make_context,
    quietly,
    read_jsonl,
    seed_run_dir,
)

from ledger_reconciliation import common, cutoff

SG_CALENDAR = "fixtures/calendars/sg-business-calendar-2026.json"


class TestSingaporeCalendar(unittest.TestCase):
    def setUp(self) -> None:
        calendar = cutoff._load_calendar(KIT_ROOT, SG_CALENDAR)
        self.holidays = cutoff._holiday_dates(calendar)
        self.weekend = cutoff._weekend_days(calendar)

    def test_calendar_declares_weekend_and_holidays(self) -> None:
        self.assertEqual(self.weekend, {5, 6})
        self.assertIn(date(2026, 1, 1), self.holidays)
        self.assertIn(date(2026, 2, 17), self.holidays)

    def test_chinese_new_year_rolls_to_the_next_business_day(self) -> None:
        # Mon 2026-02-16 after cutoff -> Tue 17 and Wed 18 are holidays -> Thu 19.
        eff, rolled = cutoff.compute_effective_value_date(
            "2026-02-16T10:00:00Z", "Asia/Singapore", "17:00", self.holidays, self.weekend
        )
        self.assertEqual(eff, date(2026, 2, 19))
        self.assertTrue(rolled)

    def test_booking_on_a_holiday_before_cutoff_still_rolls(self) -> None:
        eff, rolled = cutoff.compute_effective_value_date(
            "2026-01-01T01:00:00Z", "Asia/Singapore", "17:00", self.holidays, self.weekend
        )
        self.assertEqual(eff, date(2026, 1, 2))
        self.assertTrue(rolled)

    def test_utc_evening_is_next_local_day(self) -> None:
        # 2026-03-18T20:00Z is 04:00 on Thu 19 March in Singapore, before cutoff.
        eff, rolled = cutoff.compute_effective_value_date(
            "2026-03-18T20:00:00Z", "Asia/Singapore", "17:00", self.holidays, self.weekend
        )
        self.assertEqual(eff, date(2026, 3, 19))
        self.assertFalse(rolled)

    def test_missing_calendar_is_usage_error(self) -> None:
        with self.assertRaisesRegex(common.UsageError, "calendar not found"):
            cutoff._load_calendar(KIT_ROOT, "fixtures/calendars/none.json")


class TestWeekendDays(unittest.TestCase):
    def test_default_weekend_is_saturday_and_sunday(self) -> None:
        self.assertEqual(cutoff._weekend_days({}), {5, 6})

    def test_names_are_case_insensitive_and_unknown_names_ignored(self) -> None:
        self.assertEqual(
            cutoff._weekend_days({"weekend": ["FRIDAY", "saturday", "Caturday"]}), {4, 5}
        )

    def test_no_holidays_key_means_no_holidays(self) -> None:
        self.assertEqual(cutoff._holiday_dates({}), set())


class TestCutoffRun(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name) / "cutoff-run"

    def test_business_cutoff_scenario_matches_the_oracle(self) -> None:
        seed_run_dir(self.run_dir, "business-cutoff")
        result, code = quietly(cutoff.run, make_context(self.run_dir, "business-cutoff"))

        expected_manifest = json.loads(
            (SCENARIOS / "business-cutoff" / "expected" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        rows = read_jsonl(self.run_dir / "stages" / "cutoff-payments.jsonl")
        observed = {row["payment_id"]: row["effective_value_date"] for row in rows}

        self.assertEqual(code, 0)
        self.assertEqual(observed, expected_manifest["effectiveValueDates"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["counts"], {"cutoffAdjusted": 3})
        self.assertEqual(result["metrics"], {"inputRecords": 6})
        self.assertEqual(
            [a["path"] for a in result["artifacts"]],
            ["stages/cutoff-payments.jsonl", "stages/cutoff.json"],
        )
        late = next(row for row in rows if row["payment_id"] == "PAY-2026-0024")
        self.assertTrue(late["cutoff_applied"])
        self.assertEqual(late["calendar_id"], "PAYOPS-SG-2026")
        self.assertEqual(late["cutoff_timezone"], "Asia/Singapore")
        self.assertEqual(late["cutoff_local_time"], "17:00")

        summary = common.read_json(self.run_dir / "stages" / "cutoff.json")
        self.assertEqual(
            summary,
            {
                "scenario": "business-cutoff",
                "runId": "cutoff-run",
                "calendarId": "PAYOPS-SG-2026",
                "cutoffLocalTime": "17:00",
                "cutoffTimezone": "Asia/Singapore",
                "counts": {"cutoffAdjusted": 3},
                "inputRecords": 6,
            },
        )

    def test_cutoff_applied_compares_against_declared_value_date(self) -> None:
        records = seed_run_dir(self.run_dir, "business-cutoff")
        records[0]["value_date"] = "2026-03-18"
        common.write_jsonl(self.run_dir / "stages" / "enriched-payments.jsonl", records)

        result, _ = quietly(cutoff.run, make_context(self.run_dir, "business-cutoff"))
        self.assertEqual(result["counts"], {"cutoffAdjusted": 4})

    def test_missing_enriched_payments_is_missing_upstream(self) -> None:
        self.run_dir.mkdir()
        with self.assertRaises(common.MissingUpstreamError):
            quietly(cutoff.run, make_context(self.run_dir, "business-cutoff"))


if __name__ == "__main__":
    unittest.main()
