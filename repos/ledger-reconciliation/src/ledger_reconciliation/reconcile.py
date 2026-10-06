"""reconcile_nostro_ledger task.

Compares posted ledger rows against the reference nostro ledger keyed by
ledger_reference, per CONTRACT.md section 6.6. Break evaluation order is
MISSING_LEDGER_REFERENCE, UNMATCHED_PAYMENT, CURRENCY_MISMATCH, AMOUNT_MISMATCH.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from . import common


def _load_reference(ref_path: Path) -> dict[str, dict[str, str]]:
    if not ref_path.exists():
        raise common.MissingUpstreamError(f"reference ledger not found: {ref_path}")
    ref: dict[str, dict[str, str]] = {}
    with ref_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ref[row["ledger_reference"]] = {
                "payment_id": row["payment_id"],
                "currency": row["currency"],
                "amount": row["amount"],
            }
    return ref


def _load_posted(db_path: Path) -> list[common.JsonObject]:
    if not db_path.exists():
        raise common.MissingUpstreamError(f"ledger not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """SELECT payment_id, run_id, scenario, currency, amount, base_amount,
                      ledger_reference, effective_value_date, source_file_sha256
               FROM ledger_entry WHERE state = 'posted'
               ORDER BY payment_id"""
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _load_line_numbers(cutoff_path: Path) -> dict[str, int]:
    """Recover line_number from the cutoff stage (the ledger does not store it)."""
    records = common.read_jsonl(cutoff_path)
    return {r["payment_id"]: r["line_number"] for r in records}


def _evaluate(
    posted: common.JsonObject, reference: dict[str, str] | None
) -> tuple[str, dict[str, str | None]]:
    """Return (reason_code_or_matched, break_detail)."""
    if reference is None:
        return "MISSING_LEDGER_REFERENCE", {
            "expected_currency": None,
            "actual_currency": posted["currency"],
            "expected_amount": None,
            "actual_amount": posted["amount"],
            "expected_payment_id": None,
        }
    if reference["payment_id"] != posted["payment_id"]:
        return "UNMATCHED_PAYMENT", {
            "expected_currency": reference["currency"],
            "actual_currency": posted["currency"],
            "expected_amount": reference["amount"],
            "actual_amount": posted["amount"],
            "expected_payment_id": reference["payment_id"],
        }
    if reference["currency"] != posted["currency"]:
        return "CURRENCY_MISMATCH", {
            "expected_currency": reference["currency"],
            "actual_currency": posted["currency"],
            "expected_amount": reference["amount"],
            "actual_amount": posted["amount"],
            "expected_payment_id": reference["payment_id"],
        }
    if reference["amount"] != posted["amount"]:
        return "AMOUNT_MISMATCH", {
            "expected_currency": reference["currency"],
            "actual_currency": posted["currency"],
            "expected_amount": reference["amount"],
            "actual_amount": posted["amount"],
            "expected_payment_id": reference["payment_id"],
        }
    return "matched", {}


def run(ctx: common.TaskContext) -> tuple[common.JsonObject, int]:
    run_dir = ctx["run_dir"]
    scenario = ctx["scenario"]
    run_id = ctx["run_id"]

    db_path = run_dir / "ledger" / "ledger.sqlite3"
    ref_path = run_dir / "input" / "ledger.csv"
    cutoff_path = run_dir / "stages" / "cutoff-payments.jsonl"

    common.log(f"reading ledger {db_path}")
    posted_rows = _load_posted(db_path)
    common.log(f"reading reference {ref_path}")
    reference = _load_reference(ref_path)
    line_numbers = _load_line_numbers(cutoff_path)

    matched_records: list[common.JsonObject] = []
    break_records: list[common.JsonObject] = []

    for row in posted_rows:
        payment_id = row["payment_id"]
        ref_row = reference.get(row["ledger_reference"])
        outcome, detail = _evaluate(row, ref_row)
        base: common.JsonObject = {
            "payment_id": payment_id,
            "run_id": row["run_id"],
            "scenario": row["scenario"],
            "source_file_sha256": row["source_file_sha256"],
            "line_number": line_numbers.get(payment_id, 0),
            "ledger_reference": row["ledger_reference"],
            "currency": row["currency"],
            "amount": row["amount"],
            "base_amount": row["base_amount"],
            "effective_value_date": row["effective_value_date"],
        }
        if outcome == "matched":
            base["match_status"] = "matched"
            matched_records.append(base)
        else:
            brk = dict(base)
            brk["match_status"] = "broken"
            brk["reason_code"] = outcome
            brk["expected_currency"] = detail["expected_currency"]
            brk["actual_currency"] = detail["actual_currency"]
            brk["expected_amount"] = detail["expected_amount"]
            brk["actual_amount"] = detail["actual_amount"]
            brk["expected_payment_id"] = detail["expected_payment_id"]
            break_records.append(brk)

    matched_jsonl = run_dir / "stages" / "matched.jsonl"
    breaks_jsonl = run_dir / "stages" / "breaks.jsonl"
    reconciliation_json = run_dir / "stages" / "reconciliation.json"
    common.write_jsonl(matched_jsonl, matched_records)
    common.write_jsonl(breaks_jsonl, break_records)
    common.write_json(
        reconciliation_json,
        {
            "scenario": scenario,
            "runId": run_id,
            "counts": {
                "matched": len(matched_records),
                "broken": len(break_records),
            },
            "metrics": {
                "posted": len(posted_rows),
                "referenceRows": len(reference),
            },
        },
    )
    common.log(f"matched={len(matched_records)} broken={len(break_records)}")

    result: common.JsonObject = {
        "status": "success",
        "counts": {
            "matched": len(matched_records),
            "broken": len(break_records),
        },
        "artifacts": common.artifacts_for(
            run_dir,
            [
                "stages/matched.jsonl",
                "stages/breaks.jsonl",
                "stages/reconciliation.json",
            ],
        ),
        "metrics": {"posted": len(posted_rows), "referenceRows": len(reference)},
    }
    return result, 0
