"""Task implementations for payment-validation.

Owns tasks 4 and 5 of PAYOPS_CROSS_BORDER_RECONCILIATION:
  validate_payment_schema, deduplicate_payments
"""
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .common import (
    BusinessRuleError,
    MissingUpstreamError,
    artifact_entry,
    ensure_dir,
    log,
    read_jsonl,
    write_json,
    write_jsonl,
)

REPOSITORY = "payment-validation"

# Contract section 1 field order.
FIELDS = [
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

_FORMAT_CHECKS = {
    "payment_id": re.compile(r"^PAY-\d{4}-\d{4}$"),
    "source_system": re.compile(r"^CHANNEL-[A-Z]{2}$"),
    "value_date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "booking_timestamp": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"),
    "debtor_account_token": re.compile(r"^DEBTOR-\d{4}$"),
    "creditor_account_token": re.compile(r"^CREDITOR-\d{4}$"),
    "currency": re.compile(r"^[A-Z]{3}$"),
    "amount": re.compile(r"^-?\d+\.\d{2}$"),
    "ledger_reference": re.compile(r"^LEDG-[A-Z]{2}-\d{4}$"),
    "country_code": re.compile(r"^[A-Z]{2}$"),
}


def validate_row(row, allowlist):
    """Apply contract 6.1 rules in order. Returns (reason_code, reason_detail)
    or (None, None) when the row is accepted."""
    # 1. MISSING_REQUIRED_FIELD
    for field in FIELDS:
        val = row.get(field)
        if val is None or val == "":
            return "MISSING_REQUIRED_FIELD", "empty field: " + field
    # 2. MALFORMED_FIELD
    for field in FIELDS:
        if not _FORMAT_CHECKS[field].match(row[field]):
            return "MALFORMED_FIELD", "field {} failed format".format(field)
    # 3. CURRENCY_NOT_ALLOWED
    if row["currency"] not in allowlist:
        return "CURRENCY_NOT_ALLOWED", "currency {} not in allowlist".format(row["currency"])
    # 4. NON_POSITIVE_AMOUNT
    try:
        amount = Decimal(row["amount"])
    except InvalidOperation:
        return "MALFORMED_FIELD", "field amount not a decimal"
    if amount <= 0:
        return "NON_POSITIVE_AMOUNT", "amount {} not positive".format(row["amount"])
    return None, None


def deduplicate(records):
    """Contract 6.2: keep the first occurrence of each payment_id in input
    file (line_number) order. Returns (accepted, duplicates)."""
    in_line_order = sorted(records, key=lambda r: r["line_number"])
    seen = {}
    accepted = []
    duplicates = []
    for r in in_line_order:
        pid = r["payment_id"]
        if pid in seen:
            dup = dict(r)
            dup["duplicate_of_line"] = seen[pid]
            duplicates.append(dup)
        else:
            seen[pid] = r["line_number"]
            accepted.append(r)
    return accepted, duplicates


def validate_payment_schema(run_dir, run_id, scenario, kit_root, scenario_doc, now, attempt):
    norm_path = Path(run_dir) / "stages" / "normalized-payments.jsonl"
    if not norm_path.is_file():
        raise MissingUpstreamError("missing upstream artifact: stages/normalized-payments.jsonl")

    allowlist_rel = scenario_doc.get(
        "currencyAllowlist", "fixtures/fx/currency-allowlist.json"
    )
    allowlist_path = Path(kit_root) / allowlist_rel
    if not allowlist_path.is_file():
        raise MissingUpstreamError("missing currency allowlist: " + allowlist_rel)
    allowlist_doc = json.loads(allowlist_path.read_text(encoding="utf-8"))
    allowlist = set(allowlist_doc["currencies"])
    allowlist_version = allowlist_doc["allowlistVersion"]

    records = read_jsonl(norm_path)
    validated = []
    rejections = []
    for r in records:
        code, detail = validate_row(r, allowlist)
        if code:
            rej = dict(r)
            rej["reason_code"] = code
            rej["reason_detail"] = detail
            rejections.append(rej)
        else:
            validated.append(r)

    ensure_dir(Path(run_dir) / "stages")
    write_jsonl(Path(run_dir) / "stages" / "validated-payments.jsonl", validated)
    write_jsonl(Path(run_dir) / "stages" / "rejections.jsonl", rejections)
    meta = {
        "counts": {"received": len(records), "rejected": len(rejections)},
        "currencyAllowlistVersion": allowlist_version,
        "runId": run_id,
        "scenario": scenario,
    }
    write_json(Path(run_dir) / "stages" / "validate.json", meta)
    log.info(
        "validate_payment_schema: received=%d rejected=%d", len(records), len(rejections)
    )
    artifacts = [
        artifact_entry(run_dir, "stages/rejections.jsonl"),
        artifact_entry(run_dir, "stages/validate.json"),
        artifact_entry(run_dir, "stages/validated-payments.jsonl"),
    ]
    return {
        "task": "validate_payment_schema",
        "repository": REPOSITORY,
        "runId": run_id,
        "scenario": scenario,
        "attempt": attempt,
        "status": "success",
        "exitCode": 0,
        "counts": {"received": len(records), "rejected": len(rejections)},
        "artifacts": artifacts,
        "metrics": {},
    }


def deduplicate_payments(run_dir, run_id, scenario, kit_root, scenario_doc, now, attempt):
    val_path = Path(run_dir) / "stages" / "validated-payments.jsonl"
    if not val_path.is_file():
        raise MissingUpstreamError("missing upstream artifact: stages/validated-payments.jsonl")

    records = read_jsonl(val_path)
    accepted, duplicates = deduplicate(records)

    ensure_dir(Path(run_dir) / "stages")
    write_jsonl(Path(run_dir) / "stages" / "deduplicated-payments.jsonl", accepted)
    write_jsonl(Path(run_dir) / "stages" / "duplicates.jsonl", duplicates)
    meta = {
        "counts": {"accepted": len(accepted), "duplicatesRemoved": len(duplicates)},
        "runId": run_id,
        "scenario": scenario,
    }
    write_json(Path(run_dir) / "stages" / "deduplicate.json", meta)
    log.info(
        "deduplicate_payments: accepted=%d duplicates=%d",
        len(accepted),
        len(duplicates),
    )
    artifacts = [
        artifact_entry(run_dir, "stages/deduplicate.json"),
        artifact_entry(run_dir, "stages/deduplicated-payments.jsonl"),
        artifact_entry(run_dir, "stages/duplicates.jsonl"),
    ]
    return {
        "task": "deduplicate_payments",
        "repository": REPOSITORY,
        "runId": run_id,
        "scenario": scenario,
        "attempt": attempt,
        "status": "success",
        "exitCode": 0,
        "counts": {"accepted": len(accepted), "duplicatesRemoved": len(duplicates)},
        "artifacts": artifacts,
        "metrics": {},
    }
