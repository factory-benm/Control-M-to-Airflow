"""produce_settlement_report task.

Reads the ledger and downstream stage artifacts, produces machine- and
human-readable settlement reports, an archive manifest of every run artifact,
and asserts the canonical count equation (CONTRACT section 5).
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from . import common
from .common import JsonDict, TaskContext

COUNT_EQUATIONS = [
    "received == rejected + duplicatesRemoved + accepted",
    "accepted == posted",
    "posted == matched + broken",
]


def count_csv_rows(path: Path) -> int:
    """Count data rows (excluding header) in a CSV file."""
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows:
        return 0
    return len(rows) - 1


def count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.rstrip("\n"):
                n += 1
    return n


def count_posted(db_path: Path) -> int:
    if not db_path.exists():
        raise common.MissingUpstreamError(f"ledger not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT COUNT(*) FROM ledger_entry WHERE state = 'posted'").fetchone()
        posted: int = row[0]
        return posted
    finally:
        conn.close()


def assemble_counts(run_dir: Path) -> dict[str, int]:
    """Derive the full canonical count set from ground-truth artifacts."""
    received = count_csv_rows(run_dir / "input" / "payments.csv")
    rejected = count_jsonl(run_dir / "stages" / "rejections.jsonl")
    duplicates_removed = count_jsonl(run_dir / "stages" / "duplicates.jsonl")
    accepted = count_jsonl(run_dir / "stages" / "enriched-payments.jsonl")
    cutoff_adjusted = sum(
        1 for _ in _iter_cutoff_adjusted(run_dir / "stages" / "cutoff-payments.jsonl")
    )
    posted = count_posted(run_dir / "ledger" / "ledger.sqlite3")
    matched = count_jsonl(run_dir / "stages" / "matched.jsonl")
    broken = count_jsonl(run_dir / "stages" / "breaks.jsonl")
    return {
        "received": received,
        "rejected": rejected,
        "duplicatesRemoved": duplicates_removed,
        "accepted": accepted,
        "cutoffAdjusted": cutoff_adjusted,
        "posted": posted,
        "matched": matched,
        "broken": broken,
    }


def _iter_cutoff_adjusted(path: Path) -> Iterator[JsonDict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line:
                continue
            rec: JsonDict = json.loads(line)
            if rec.get("cutoff_applied") is True:
                yield rec


def assert_count_equation(counts: Mapping[str, int]) -> None:
    c = counts
    ok1 = c["received"] == c["rejected"] + c["duplicatesRemoved"] + c["accepted"]
    ok2 = c["accepted"] == c["posted"]
    ok3 = c["posted"] == c["matched"] + c["broken"]
    if not (ok1 and ok2 and ok3):
        raise common.BusinessRuleError(
            "COUNT_EQUATION_VIOLATION",
            f"count equation failed: received={c['received']} "
            f"rejected={c['rejected']} duplicatesRemoved={c['duplicatesRemoved']} "
            f"accepted={c['accepted']} posted={c['posted']} "
            f"matched={c['matched']} broken={c['broken']}",
        )


def build_archive_manifest(run_dir: Path, exclude: set[str]) -> list[JsonDict]:
    """Hash every run artifact except those in `exclude` (relative paths)."""
    items: list[JsonDict] = []
    for sub in ("input", "stages", "ledger", "output"):
        base = run_dir / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(run_dir).as_posix()
            if rel in exclude:
                continue
            items.append(
                {
                    "path": rel,
                    "sha256": common.sha256_file(p),
                    "size": p.stat().st_size,
                }
            )
    items.sort(key=lambda a: a["path"])
    return items


def _load_ledger_rows(db_path: Path) -> list[JsonDict]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT payment_id, ledger_reference, currency, amount, base_amount, "
            "effective_value_date FROM ledger_entry WHERE state='posted' "
            "ORDER BY payment_id"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def run(ctx: TaskContext) -> tuple[JsonDict, int]:
    run_dir: Path = ctx["run_dir"]
    scenario: str = ctx["scenario"]
    run_id: str = ctx["run_id"]
    now: str = ctx["now"]

    common.log("assembling counts")
    counts = assemble_counts(run_dir)
    assert_count_equation(counts)
    common.log(f"counts ok: {counts}")

    matched = common.read_jsonl_optional(run_dir / "stages" / "matched.jsonl")
    classified = common.read_jsonl_optional(run_dir / "stages" / "classified-breaks.jsonl")
    rejections = common.read_jsonl_optional(run_dir / "stages" / "rejections.jsonl")
    duplicates = common.read_jsonl_optional(run_dir / "stages" / "duplicates.jsonl")
    cutoff = common.read_jsonl_optional(run_dir / "stages" / "cutoff-payments.jsonl")
    ledger_rows = _load_ledger_rows(run_dir / "ledger" / "ledger.sqlite3")

    # Per-payment disposition table, sorted by payment_id then line_number.
    disposition: dict[str, JsonDict] = {}
    for r in cutoff:
        disposition[r["payment_id"]] = {
            "payment_id": r["payment_id"],
            "line_number": r.get("line_number", ""),
            "ledger_reference": r.get("ledger_reference", ""),
            "currency": r.get("currency", ""),
            "amount": r.get("amount", ""),
            "base_amount": r.get("base_amount", ""),
            "effective_value_date": r.get("effective_value_date", ""),
            "disposition": "",
            "reason_code": "",
            "severity": "",
            "owner_team": "",
        }
    for m in matched:
        if m["payment_id"] in disposition:
            disposition[m["payment_id"]]["disposition"] = "matched"
    for b in classified:
        if b["payment_id"] in disposition:
            d = disposition[b["payment_id"]]
            d["disposition"] = "broken"
            d["reason_code"] = b.get("reason_code", "")
            d["severity"] = b.get("severity", "")
            d["owner_team"] = b.get("owner_team", "")
    for r in rejections:
        disposition.setdefault(
            r["payment_id"],
            {
                "payment_id": r["payment_id"],
                "line_number": r.get("line_number", ""),
                "ledger_reference": r.get("ledger_reference", ""),
                "currency": r.get("currency", ""),
                "amount": r.get("amount", ""),
                "base_amount": "",
                "effective_value_date": "",
                "disposition": "rejected",
                "reason_code": r.get("reason_code", ""),
                "severity": "",
                "owner_team": "",
            },
        )
        disposition[r["payment_id"]]["disposition"] = "rejected"
        disposition[r["payment_id"]]["reason_code"] = r.get("reason_code", "")
    for d in duplicates:
        disposition.setdefault(
            d["payment_id"],
            {
                "payment_id": d["payment_id"],
                "line_number": d.get("line_number", ""),
                "ledger_reference": d.get("ledger_reference", ""),
                "currency": d.get("currency", ""),
                "amount": d.get("amount", ""),
                "base_amount": "",
                "effective_value_date": "",
                "disposition": "duplicate",
                "reason_code": "",
                "severity": "",
                "owner_team": "",
            },
        )
        disposition[d["payment_id"]]["disposition"] = "duplicate"

    rows = sorted(disposition.values(), key=lambda r: (r["payment_id"], r["line_number"]))

    report_json = run_dir / "output" / "settlement-report.json"
    report_csv = run_dir / "output" / "settlement-report.csv"
    archive_manifest = run_dir / "output" / "archive-manifest.json"
    reporting_stage = run_dir / "stages" / "reporting.json"

    report_obj: dict[str, Any] = {
        "scenario": scenario,
        "runId": run_id,
        "counts": counts,
        "countEquations": COUNT_EQUATIONS,
        "matched": sorted(
            [
                {
                    "payment_id": m["payment_id"],
                    "ledger_reference": m["ledger_reference"],
                    "amount": m["amount"],
                    "currency": m["currency"],
                }
                for m in matched
            ],
            key=lambda x: x["payment_id"],
        ),
        "broken": sorted(
            [
                {
                    "payment_id": b["payment_id"],
                    "reason_code": b["reason_code"],
                    "severity": b.get("severity", ""),
                    "owner_team": b.get("owner_team", ""),
                }
                for b in classified
            ],
            key=lambda x: x["payment_id"],
        ),
        "ledgerRowCount": len(ledger_rows),
        "generatedAt": now,
    }
    common.write_json(report_json, report_obj)

    csv_columns = [
        "payment_id",
        "line_number",
        "ledger_reference",
        "currency",
        "amount",
        "base_amount",
        "effective_value_date",
        "disposition",
        "reason_code",
        "severity",
        "owner_team",
    ]
    report_csv.parent.mkdir(parents=True, exist_ok=True)
    with report_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in csv_columns})

    manifest = build_archive_manifest(run_dir, exclude={"output/archive-manifest.json"})
    common.write_json(
        archive_manifest,
        {
            "scenario": scenario,
            "runId": run_id,
            "generatedAt": now,
            "artifacts": manifest,
        },
    )

    common.write_json(
        reporting_stage,
        {
            "scenario": scenario,
            "runId": run_id,
            "counts": counts,
            "artifacts": [
                "output/settlement-report.json",
                "output/settlement-report.csv",
                "output/archive-manifest.json",
            ],
        },
    )
    common.log("report written")

    result: JsonDict = {
        "status": "success",
        "counts": counts,
        "artifacts": common.artifacts_for(
            run_dir,
            [
                "output/settlement-report.json",
                "output/settlement-report.csv",
                "output/archive-manifest.json",
                "stages/reporting.json",
            ],
        ),
        "metrics": {"ledgerRows": len(ledger_rows)},
    }
    return result, 0
