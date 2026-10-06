"""Shared builders for ledger-reconciliation tests.

Fixtures under the kit root are only ever read; everything a test writes goes
into a temporary run directory.
"""

import csv
import hashlib
import io
import json
import shutil
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, TypeVar

from ledger_reconciliation import common

T = TypeVar("T")

REPO_ROOT = Path(__file__).resolve().parents[1]
KIT_ROOT = REPO_ROOT.parents[1]
SCENARIOS = KIT_ROOT / "fixtures" / "scenarios"


def scenario_config(scenario: str) -> common.JsonObject:
    return common.load_scenario(KIT_ROOT, scenario)


def enriched_records(scenario: str, run_id: str) -> list[common.JsonObject]:
    """Records shaped like the enrich_fx_rates output for a fixture scenario.

    Rows the oracle says validation rejects are dropped, as they would be
    upstream, so only accepted payments reach the ledger tasks.
    """
    source = SCENARIOS / scenario / "input" / "payments.csv"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = json.loads(
        (SCENARIOS / scenario / "expected" / "manifest.json").read_text(encoding="utf-8")
    )
    rejected = {item["payment_id"] for item in manifest.get("rejections") or []}
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        {
            "payment_id": row["payment_id"],
            "line_number": index,
            "run_id": run_id,
            "scenario": scenario,
            "source_file_sha256": digest,
            "value_date": row["value_date"],
            "booking_timestamp": row["booking_timestamp"],
            "currency": row["currency"],
            "amount": row["amount"],
            "base_amount": row["amount"],
            "ledger_reference": row["ledger_reference"],
        }
        for index, row in enumerate(rows, start=1)
        if row["payment_id"] not in rejected
    ]


def seed_run_dir(run_dir: Path, scenario: str) -> list[common.JsonObject]:
    """Write the upstream artifacts the three ledger tasks consume."""
    records = enriched_records(scenario, run_dir.name)
    common.write_jsonl(run_dir / "stages" / "enriched-payments.jsonl", records)
    (run_dir / "input").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        SCENARIOS / scenario / "reference" / "ledger.csv", run_dir / "input" / "ledger.csv"
    )
    return records


def make_context(
    run_dir: Path, scenario: str, attempt: int = 1, repo_root: Path = REPO_ROOT
) -> common.TaskContext:
    cfg = scenario_config(scenario)
    return {
        "run_dir": run_dir,
        "kit_root": KIT_ROOT,
        "repo_root": repo_root,
        "scenario": scenario,
        "scenario_cfg": cfg,
        "run_id": run_dir.name,
        "now": cfg["clock"]["fixedUtc"],
        "attempt": attempt,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def quietly(func: Callable[..., T], *args: Any) -> T:
    """Call ``func`` with its stderr diagnostics swallowed."""
    with redirect_stderr(io.StringIO()):
        return func(*args)


def run_quietly(func: Callable[..., T], *args: Any) -> tuple[T, str]:
    """Call ``func`` and return its result with everything it wrote to stdout."""
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        result = func(*args)
    return result, out.getvalue()
