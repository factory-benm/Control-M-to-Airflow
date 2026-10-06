"""apply_business_day_cutoff task.

Implements the business-day cutoff rule from CONTRACT.md section 6.4 exactly.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import common


def _load_calendar(kit_root: Path, calendar_rel: str) -> common.JsonObject:
    cal_path = kit_root / calendar_rel
    if not cal_path.exists():
        raise common.UsageError(f"calendar not found: {cal_path}")
    calendar: common.JsonObject = common.read_json(cal_path)
    return calendar


def _holiday_dates(calendar: common.JsonObject) -> set[date]:
    holidays: set[date] = set()
    for entry in calendar.get("holidays", []):
        holidays.add(date.fromisoformat(entry["date"]))
    return holidays


def _weekend_days(calendar: common.JsonObject) -> set[int]:
    """Return set of weekday numbers (Mon=0..Sun=6) that are weekend days."""
    names = set(calendar.get("weekend", ["Saturday", "Sunday"]))
    name_to_num = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    return {name_to_num[n.lower()] for n in names if n.lower() in name_to_num}


def is_business_day(d: date, holidays: set[date], weekend: set[int]) -> bool:
    if d.weekday() in weekend:
        return False
    return d not in holidays


def next_business_day(d: date, holidays: set[date], weekend: set[int]) -> date:
    """Next business day strictly after d."""
    cur = d + timedelta(days=1)
    while not is_business_day(cur, holidays, weekend):
        cur = cur + timedelta(days=1)
    return cur


def compute_effective_value_date(
    booking_timestamp: str,
    tz_name: str,
    cutoff_local_time: str,
    holidays: set[date],
    weekend: set[int],
) -> tuple[date, bool]:
    """Return (effective_value_date, rolled_forward).

    rolled_forward is True when the effective date is strictly after the local
    booking date (i.e. the cutoff or non-business-day rule moved the date).
    """
    tz = ZoneInfo(tz_name)
    utc_dt = datetime.fromisoformat(booking_timestamp.replace("Z", "+00:00"))
    local_dt = utc_dt.astimezone(tz)
    local_date = local_dt.date()
    local_time = local_dt.time()

    hour, minute = (int(x) for x in cutoff_local_time.split(":"))
    cutoff_time = time(hour=hour, minute=minute)

    if is_business_day(local_date, holidays, weekend) and local_time < cutoff_time:
        return local_date, False
    eff = next_business_day(local_date, holidays, weekend)
    return eff, True


def run(ctx: common.TaskContext) -> tuple[common.JsonObject, int]:
    run_dir = ctx["run_dir"]
    kit_root = ctx["kit_root"]
    scenario_cfg = ctx["scenario_cfg"]

    enriched_path = run_dir / "stages" / "enriched-payments.jsonl"
    common.log(f"reading {enriched_path}")
    enriched = common.read_jsonl(enriched_path)

    cutoff_cfg = scenario_cfg["cutoff"]
    tz_name = cutoff_cfg["timezone"]
    cutoff_local_time = cutoff_cfg["localTime"]
    calendar = _load_calendar(kit_root, scenario_cfg["calendar"])
    holidays = _holiday_dates(calendar)
    weekend = _weekend_days(calendar)
    calendar_id = calendar["calendarId"]

    out_records: list[common.JsonObject] = []
    cutoff_adjusted = 0
    for rec in enriched:
        eff_date, _rolled = compute_effective_value_date(
            rec["booking_timestamp"],
            tz_name,
            cutoff_local_time,
            holidays,
            weekend,
        )
        eff_str = eff_date.isoformat()
        cutoff_applied = eff_str != rec["value_date"]
        if cutoff_applied:
            cutoff_adjusted += 1
        out = dict(rec)
        out["effective_value_date"] = eff_str
        out["cutoff_applied"] = cutoff_applied
        out["cutoff_local_time"] = cutoff_local_time
        out["cutoff_timezone"] = tz_name
        out["calendar_id"] = calendar_id
        out_records.append(out)

    cutoff_jsonl = run_dir / "stages" / "cutoff-payments.jsonl"
    cutoff_json = run_dir / "stages" / "cutoff.json"
    common.write_jsonl(cutoff_jsonl, out_records)
    common.write_json(
        cutoff_json,
        {
            "scenario": ctx["scenario"],
            "runId": ctx["run_id"],
            "calendarId": calendar_id,
            "cutoffLocalTime": cutoff_local_time,
            "cutoffTimezone": tz_name,
            "counts": {"cutoffAdjusted": cutoff_adjusted},
            "inputRecords": len(out_records),
        },
    )

    common.log(f"cutoffAdjusted={cutoff_adjusted} of {len(out_records)}")

    result: common.JsonObject = {
        "status": "success",
        "counts": {"cutoffAdjusted": cutoff_adjusted},
        "artifacts": common.artifacts_for(
            run_dir,
            [
                "stages/cutoff-payments.jsonl",
                "stages/cutoff.json",
            ],
        ),
        "metrics": {"inputRecords": len(out_records)},
    }
    return result, 0
