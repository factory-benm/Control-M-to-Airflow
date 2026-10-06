"""Task implementations for fx-enrichment.

Owns task 6 of PAYOPS_CROSS_BORDER_RECONCILIATION:
  enrich_fx_rates
"""
import json
from decimal import ROUND_HALF_UP, Decimal
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

REPOSITORY = "fx-enrichment"

TWO_PLACES = Decimal("0.01")


def compute_base_amount(amount_str, rate_str):
    """base_amount = amount * rate, quantized to 2dp with ROUND_HALF_UP AFTER
    multiplication (contract 1.2). Returns a Decimal."""
    amount = Decimal(amount_str)
    rate = Decimal(rate_str)
    return (amount * rate).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def lookup_rate(rates_table, value_date, currency):
    """Return the rate string for (value_date, currency) or None."""
    date_table = rates_table.get(value_date)
    if date_table is None:
        return None
    return date_table.get(currency)


def enrich_fx_rates(run_dir, run_id, scenario, kit_root, scenario_doc, now, attempt):
    dedup_path = Path(run_dir) / "stages" / "deduplicated-payments.jsonl"
    if not dedup_path.is_file():
        raise MissingUpstreamError(
            "missing upstream artifact: stages/deduplicated-payments.jsonl"
        )

    fx_rel = scenario_doc.get("fxTable", "fixtures/fx/fx-rates.json")
    fx_path = Path(kit_root) / fx_rel
    if not fx_path.is_file():
        raise MissingUpstreamError("missing FX table: " + fx_rel)
    fx_doc = json.loads(fx_path.read_text(encoding="utf-8"))
    fx_table_version = fx_doc["fxTableVersion"]
    base_currency = fx_doc["baseCurrency"]
    rates = fx_doc["rates"]

    records = read_jsonl(dedup_path)
    enriched = []
    for r in records:
        value_date = r["value_date"]
        currency = r["currency"]
        rate_str = lookup_rate(rates, value_date, currency)
        if rate_str is None:
            raise BusinessRuleError(
                "missing FX rate for {} {}".format(value_date, currency)
            )
        base_amount = compute_base_amount(r["amount"], rate_str)
        e = dict(r)
        e["base_amount"] = str(base_amount)
        e["base_currency"] = base_currency
        e["fx_rate"] = rate_str
        e["fx_rate_date"] = value_date
        e["fx_table_version"] = fx_table_version
        enriched.append(e)

    ensure_dir(Path(run_dir) / "stages")
    write_jsonl(Path(run_dir) / "stages" / "enriched-payments.jsonl", enriched)
    meta = {
        "baseCurrency": base_currency,
        "counts": {"accepted": len(enriched)},
        "fxTableVersion": fx_table_version,
        "runId": run_id,
        "scenario": scenario,
    }
    write_json(Path(run_dir) / "stages" / "fx.json", meta)
    log("enriched {} rows, table {}".format(len(enriched), fx_table_version))
    artifacts = [
        artifact_entry(run_dir, "stages/enriched-payments.jsonl"),
        artifact_entry(run_dir, "stages/fx.json"),
    ]
    return {
        "task": "enrich_fx_rates",
        "repository": REPOSITORY,
        "runId": run_id,
        "scenario": scenario,
        "attempt": attempt,
        "status": "success",
        "exitCode": 0,
        "counts": {"accepted": len(enriched)},
        "artifacts": artifacts,
        "metrics": {},
    }
