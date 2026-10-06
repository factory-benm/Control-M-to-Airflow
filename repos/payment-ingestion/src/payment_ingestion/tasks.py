"""Task implementations for payment-ingestion.

Owns the first three tasks of PAYOPS_CROSS_BORDER_RECONCILIATION:
  watch_inbound_files, verify_file_integrity, extract_payment_batch
"""
import csv
import json
import shutil
import time
from pathlib import Path

from .common import (
    BusinessRuleError,
    MissingUpstreamError,
    artifact_entry,
    ensure_dir,
    log,
    read_jsonl,
    sha256_file,
    write_json,
    write_jsonl,
)

REPOSITORY = "payment-ingestion"

EXPECTED_HEADER = [
    "payment_id",
    "source_system",
    "value_date",
    "booking_timestamp",
    "debtor_account_token",
    "creditor_account_token",
    "currency",
    "amount",
    "ledger_reference",
    "country_code",
]


def row_to_base_record(row, line_number, run_id, scenario, source_sha):
    """Build the canonical base record (contract 4.4) from a CSV row list."""
    return {
        "amount": row[7],
        "booking_timestamp": row[3],
        "country_code": row[9],
        "creditor_account_token": row[5],
        "currency": row[6],
        "debtor_account_token": row[4],
        "ledger_reference": row[8],
        "line_number": line_number,
        "payment_id": row[0],
        "run_id": run_id,
        "scenario": scenario,
        "source_file_sha256": source_sha,
        "source_system": row[1],
        "value_date": row[2],
    }


def watch_inbound_files(run_dir, run_id, scenario, kit_root, scenario_doc, now, attempt):
    """Task 1: deterministic wait-for-file simulation against the scenario
    input fixture. Bounded poll; no single sleep exceeds one second."""
    rel_fixture = "fixtures/scenarios/{}/input/payments.csv".format(scenario)
    fixture_csv = Path(kit_root) / rel_fixture
    ensure_dir(Path(run_dir) / "stages")

    poll_interval = 0.1
    max_polls = 20
    detected = False
    byte_size = 0
    polls = 0
    for polls in range(1, max_polls + 1):
        if fixture_csv.is_file():
            detected = True
            byte_size = fixture_csv.stat().st_size
            break
        time.sleep(poll_interval)

    if not detected:
        raise MissingUpstreamError("input file not detected: " + rel_fixture)

    log("watch_inbound_files: detected {}".format(rel_fixture))
    manifest = {
        "detectedFile": rel_fixture,
        "byteSize": byte_size,
        "detectionOutcome": "detected",
        "runId": run_id,
        "scenario": scenario,
    }
    write_json(Path(run_dir) / "stages" / "inbound-manifest.json", manifest)
    artifacts = [artifact_entry(run_dir, "stages/inbound-manifest.json")]
    return {
        "task": "watch_inbound_files",
        "repository": REPOSITORY,
        "runId": run_id,
        "scenario": scenario,
        "attempt": attempt,
        "status": "success",
        "exitCode": 0,
        "counts": {},
        "artifacts": artifacts,
        "metrics": {"polls": polls},
    }


def verify_file_integrity(run_dir, run_id, scenario, kit_root, scenario_doc, now, attempt):
    """Task 2: SHA-256 the payments and reference ledger CSVs, copy them into
    the run directory as immutable inputs, and re-verify the copies."""
    src_payments = Path(kit_root) / "fixtures" / "scenarios" / scenario / "input" / "payments.csv"
    src_ledger = Path(kit_root) / "fixtures" / "scenarios" / scenario / "reference" / "ledger.csv"
    if not src_payments.is_file():
        raise MissingUpstreamError(
            "missing source fixture: fixtures/scenarios/{}/input/payments.csv".format(scenario)
        )
    if not src_ledger.is_file():
        raise MissingUpstreamError(
            "missing source fixture: fixtures/scenarios/{}/reference/ledger.csv".format(scenario)
        )

    ensure_dir(Path(run_dir) / "input")
    ensure_dir(Path(run_dir) / "stages")
    dst_payments = Path(run_dir) / "input" / "payments.csv"
    dst_ledger = Path(run_dir) / "input" / "ledger.csv"
    shutil.copyfile(src_payments, dst_payments)
    shutil.copyfile(src_ledger, dst_ledger)

    pay_hash = sha256_file(dst_payments)
    pay_size = dst_payments.stat().st_size
    led_hash = sha256_file(dst_ledger)
    led_size = dst_ledger.stat().st_size

    if pay_hash != sha256_file(src_payments):
        raise BusinessRuleError("payments.csv copy integrity check failed")
    if led_hash != sha256_file(src_ledger):
        raise BusinessRuleError("ledger.csv copy integrity check failed")

    log("verify_file_integrity: payments={} ledger={}".format(pay_hash[:12], led_hash[:12]))
    files = [
        {
            "path": "input/ledger.csv",
            "sha256": led_hash,
            "byteSize": led_size,
            "source": "fixtures/scenarios/{}/reference/ledger.csv".format(scenario),
        },
        {
            "path": "input/payments.csv",
            "sha256": pay_hash,
            "byteSize": pay_size,
            "source": "fixtures/scenarios/{}/input/payments.csv".format(scenario),
        },
    ]
    manifest = {"files": files, "runId": run_id, "scenario": scenario}
    write_json(Path(run_dir) / "stages" / "file-integrity.json", manifest)
    artifacts = [
        artifact_entry(run_dir, "input/ledger.csv"),
        artifact_entry(run_dir, "input/payments.csv"),
        artifact_entry(run_dir, "stages/file-integrity.json"),
    ]
    return {
        "task": "verify_file_integrity",
        "repository": REPOSITORY,
        "runId": run_id,
        "scenario": scenario,
        "attempt": attempt,
        "status": "success",
        "exitCode": 0,
        "counts": {},
        "artifacts": artifacts,
        "metrics": {},
    }


def extract_payment_batch(run_dir, run_id, scenario, kit_root, scenario_doc, now, attempt):
    """Task 3: parse input/payments.csv, validate the header against contract
    section 1, and emit normalized base records as JSON Lines."""
    integrity_path = Path(run_dir) / "stages" / "file-integrity.json"
    if not integrity_path.is_file():
        raise MissingUpstreamError("missing upstream artifact: stages/file-integrity.json")
    integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    source_sha = None
    for entry in integrity["files"]:
        if entry["path"] == "input/payments.csv":
            source_sha = entry["sha256"]
            break
    if source_sha is None:
        raise MissingUpstreamError("payments hash absent from file-integrity.json")

    payments_path = Path(run_dir) / "input" / "payments.csv"
    if not payments_path.is_file():
        raise MissingUpstreamError("missing upstream artifact: input/payments.csv")

    ensure_dir(Path(run_dir) / "stages")
    records = []
    with open(payments_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        if header != EXPECTED_HEADER:
            raise BusinessRuleError("payments.csv header does not match contract section 1")
        line_number = 0
        for row in reader:
            if not row:
                continue
            line_number += 1
            records.append(
                row_to_base_record(row, line_number, run_id, scenario, source_sha)
            )

    write_jsonl(Path(run_dir) / "stages" / "normalized-payments.jsonl", records)
    meta = {
        "counts": {"received": len(records)},
        "header": list(EXPECTED_HEADER),
        "runId": run_id,
        "scenario": scenario,
        "sourceFile": "input/payments.csv",
        "sourceFileSha256": source_sha,
    }
    write_json(Path(run_dir) / "stages" / "extract.json", meta)
    log("extract_payment_batch: received={} rows".format(len(records)))
    artifacts = [
        artifact_entry(run_dir, "stages/extract.json"),
        artifact_entry(run_dir, "stages/normalized-payments.jsonl"),
    ]
    return {
        "task": "extract_payment_batch",
        "repository": REPOSITORY,
        "runId": run_id,
        "scenario": scenario,
        "attempt": attempt,
        "status": "success",
        "exitCode": 0,
        "counts": {"received": len(records)},
        "artifacts": artifacts,
        "metrics": {},
    }
