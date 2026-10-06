"""post_pending_ledger task.

Creates the SQLite ledger using the exact DDL in schema/ledger.sql and posts
cutoff payments idempotently. Implements deterministic fault injection per
CONTRACT.md section 6.5.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import common

SCHEMA_FILE = "schema/ledger.sql"


def create_ledger(db_path: Path, repo_root: Path) -> None:
    """Create the ledger database if absent, executing the inspectable DDL."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ddl_path = repo_root / SCHEMA_FILE
    if not ddl_path.exists():
        raise common.UsageError(f"ledger schema not found: {ddl_path}")
    ddl = ddl_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


def posted_payment_ids(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT payment_id FROM ledger_entry WHERE state = 'posted'"
        )
        return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def count_posted(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM ledger_entry WHERE state = 'posted'"
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def _insert_rows(db_path: Path, rows: list[dict], posted_at_logical: str) -> int:
    """Insert rows with ON CONFLICT DO NOTHING inside one transaction.

    Returns the number of rows actually inserted (not already present).
    """
    if not rows:
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        inserted = 0
        for rec in rows:
            cur = conn.execute(
                """
                INSERT INTO ledger_entry (
                    payment_id, run_id, scenario, currency, amount, base_amount,
                    ledger_reference, effective_value_date, state,
                    source_file_sha256, posted_at_logical
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'posted', ?, ?)
                ON CONFLICT(payment_id) DO NOTHING
                """,
                (
                    rec["payment_id"],
                    rec["run_id"],
                    rec["scenario"],
                    rec["currency"],
                    rec["amount"],
                    rec["base_amount"],
                    rec["ledger_reference"],
                    rec["effective_value_date"],
                    rec["source_file_sha256"],
                    posted_at_logical,
                ),
            )
            inserted += cur.rowcount
        conn.commit()
        return inserted
    finally:
        conn.close()


def _fault_for_scenario(scenario_cfg: dict) -> dict | None:
    fault = scenario_cfg.get("faultInjection")
    if not fault:
        return None
    if fault.get("task") != "post_pending_ledger":
        return None
    return fault


def run(ctx: dict) -> tuple[dict, int]:
    run_dir: Path = ctx["run_dir"]
    repo_root: Path = ctx["repo_root"]
    scenario_cfg: dict = ctx["scenario_cfg"]
    now: str = ctx["now"]
    attempt: int = ctx["attempt"]

    cutoff_path = run_dir / "stages" / "cutoff-payments.jsonl"
    common.log(f"reading {cutoff_path}")
    payments = common.read_jsonl(cutoff_path)

    db_path = run_dir / "ledger" / "ledger.sqlite3"
    create_ledger(db_path, repo_root)

    # Sorted by payment_id for deterministic write order.
    payments_sorted = sorted(payments, key=lambda r: r["payment_id"])
    already = posted_payment_ids(db_path)

    fault = _fault_for_scenario(scenario_cfg)
    fault_attempt = fault.get("attempt") if fault else None
    after_writes = fault.get("afterWrites") if fault else None

    if fault is not None and attempt == fault_attempt:
        # Deterministic fault: commit exactly the first N payments, then exit 3.
        first_n = [r for r in payments_sorted[:after_writes] if r["payment_id"] not in already]
        inserted = _insert_rows(db_path, first_n, now)
        posted = count_posted(db_path)
        common.log(f"INJECTED FAULT attempt={attempt} committed={posted} (afterWrites={after_writes})")
        posting_json = run_dir / "stages" / "ledger-posting.json"
        common.write_json(posting_json, {
            "scenario": ctx["scenario"],
            "runId": ctx["run_id"],
            "attempt": attempt,
            "fault": {
                "reasonCode": fault.get("reasonCode", "INJECTED_LEDGER_FAULT"),
                "afterWrites": after_writes,
                "attempt": fault_attempt,
            },
            "counts": {"posted": posted},
            "metrics": {"inserted": inserted, "alreadyPosted": len(first_n) - inserted + len(already)},
        })
        result = {
            "status": "failed",
            "counts": {"posted": posted},
            "artifacts": common.artifacts_for(run_dir, [
                "ledger/ledger.sqlite3",
                "stages/ledger-posting.json",
            ]),
            "metrics": {"inserted": inserted, "alreadyPosted": len(already)},
            "error": {
                "code": fault.get("reasonCode", "INJECTED_LEDGER_FAULT"),
                "message": f"deterministic fault after {after_writes} writes on attempt {attempt}",
            },
        }
        return result, 3

    # Normal or retry attempt: write only missing rows.
    to_write = [r for r in payments_sorted if r["payment_id"] not in already]
    inserted = _insert_rows(db_path, to_write, now)
    posted = count_posted(db_path)
    already_posted = len(payments_sorted) - inserted

    posting_json = run_dir / "stages" / "ledger-posting.json"
    common.write_json(posting_json, {
        "scenario": ctx["scenario"],
        "runId": ctx["run_id"],
        "attempt": attempt,
        "counts": {"posted": posted},
        "metrics": {"inserted": inserted, "alreadyPosted": already_posted},
    })
    common.log(f"posted={posted} inserted={inserted} alreadyPosted={already_posted}")

    result = {
        "status": "success",
        "counts": {"posted": posted},
        "artifacts": common.artifacts_for(run_dir, [
            "ledger/ledger.sqlite3",
            "stages/ledger-posting.json",
        ]),
        "metrics": {"inserted": inserted, "alreadyPosted": already_posted},
    }
    return result, 0
