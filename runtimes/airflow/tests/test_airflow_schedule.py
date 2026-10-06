"""Schedule (docs/migration-design.md section 6, check 2).

Needs the Airflow venv: scripts/test-airflow-equivalence.sh schedule. The
expected run times are built here straight from calendars.json and the JSON
When block, without the DAG's helpers, and compared with what the loaded DAG's
timetable would schedule across 2026.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pendulum
from airflow.dag_processing.dagbag import DagBag
from airflow.timetables.base import TimeRestriction

KIT_ROOT = Path(__file__).resolve().parents[3]
CONTROLM = KIT_ROOT / "repos" / "payments-orchestrator" / "controlm"
FIXTURE_CALENDAR = KIT_ROOT / "fixtures" / "calendars" / "sg-business-calendar-2026.json"
DAG_ID = "PAYOPS_CROSS_BORDER_RECONCILIATION"
SINGAPORE = ZoneInfo("Asia/Singapore")
DAY_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def expected_run_times() -> list[datetime]:
    definition = json.loads((CONTROLM / "payments_reconciliation.json").read_text("utf-8"))
    when = definition["Defaults"]["Job"]["When"]
    (calendar_name,) = when["RuleBasedCalendars"]["Included"]
    calendars = json.loads((CONTROLM / "calendars.json").read_text("utf-8"))["calendars"]
    (calendar,) = [entry for entry in calendars if entry["name"] == calendar_name]
    hour, minute = int(when["FromTime"][:2]), int(when["FromTime"][2:])
    times = []
    day = date(2026, 1, 1)
    while day.year == 2026:
        name = DAY_NAMES[day.weekday()]
        if (
            name in when["WeekDays"]
            and name in calendar["weekDays"]
            and day.isoformat() not in calendar["excludedDates"]
        ):
            times.append(datetime(day.year, day.month, day.day, hour, minute, tzinfo=SINGAPORE))
        day += timedelta(days=1)
    return times


def scheduled_run_times() -> list[datetime]:
    """Walk the DAG's timetable the way the scheduler does, from 2026-01-01."""
    dag = DagBag(dag_folder=str(KIT_ROOT / "runtimes" / "airflow" / "dags")).dags[DAG_ID]
    restriction = TimeRestriction(
        earliest=pendulum.datetime(2026, 1, 1, tz="Asia/Singapore"),
        latest=None,
        catchup=True,
    )
    times: list[datetime] = []
    last = None
    while len(times) < 400:
        info = dag.timetable.next_dagrun_info(
            last_automated_data_interval=last, restriction=restriction
        )
        if info is None:
            break
        times.append(info.run_after)
        last = info.data_interval
    return times


class TestSchedule(unittest.TestCase):
    expected: list[datetime]
    scheduled: list[datetime]

    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = expected_run_times()
        cls.scheduled = scheduled_run_times()

    def test_expected_list_has_254_business_days(self) -> None:
        self.assertEqual(len(self.expected), 254)

    def test_timetable_yields_exactly_the_expected_run_times(self) -> None:
        self.assertEqual(len(self.scheduled), 254)
        self.assertEqual(
            [moment.timestamp() for moment in self.scheduled],
            [moment.timestamp() for moment in self.expected],
        )

    def test_every_run_is_at_20_00_singapore_which_is_12_00_utc(self) -> None:
        for moment in self.scheduled:
            local = moment.astimezone(SINGAPORE)
            utc = moment.astimezone(ZoneInfo("UTC"))
            self.assertEqual((local.hour, local.minute), (20, 0), moment)
            self.assertEqual((utc.hour, utc.minute), (12, 0), moment)

    def test_spot_checks(self) -> None:
        local_dates = [moment.astimezone(SINGAPORE).date() for moment in self.scheduled]
        self.assertEqual(local_dates[0], date(2026, 1, 2))
        self.assertEqual(local_dates[-1], date(2026, 12, 31))
        self.assertNotIn(date(2026, 3, 20), local_dates)
        self.assertIn(date(2026, 3, 19), local_dates)
        self.assertFalse([day for day in local_dates if day.year != 2026])

    def test_scheduler_and_fixture_calendars_list_the_same_holidays(self) -> None:
        calendars = json.loads((CONTROLM / "calendars.json").read_text("utf-8"))["calendars"]
        (calendar,) = [entry for entry in calendars if entry["name"] == "PAYOPS_SG_BUSINESS_2026"]
        fixture = json.loads(FIXTURE_CALENDAR.read_text("utf-8"))
        self.assertEqual(
            sorted(calendar["excludedDates"]),
            sorted(holiday["date"] for holiday in fixture["holidays"]),
        )


if __name__ == "__main__":
    unittest.main()
