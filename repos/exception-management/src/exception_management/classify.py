"""classify_breaks task (CONTRACT 6.7).

Maps each break to a deterministic severity and owner team.
"""

from __future__ import annotations

from pathlib import Path

from . import common

CLASSIFICATION: dict[str, dict[str, str]] = {
    "MISSING_LEDGER_REFERENCE": {"severity": "high", "owner_team": "Nostro Operations"},
    "CURRENCY_MISMATCH": {"severity": "high", "owner_team": "FX Operations"},
    "AMOUNT_MISMATCH": {"severity": "medium", "owner_team": "Reconciliation Operations"},
    "UNMATCHED_PAYMENT": {"severity": "medium", "owner_team": "Payments Investigations"},
}


def classify(reason_code: str) -> dict[str, str]:
    entry = CLASSIFICATION.get(reason_code)
    if entry is None:
        raise common.BusinessRuleError(
            "UNKNOWN_BREAK_REASON", f"no classification for reason_code={reason_code}"
        )
    return entry


def run(ctx: common.TaskContext) -> tuple[common.JsonDict, int]:
    run_dir: Path = ctx["run_dir"]
    scenario: str = ctx["scenario"]
    run_id: str = ctx["run_id"]

    breaks_path = run_dir / "stages" / "breaks.jsonl"
    common.log(f"reading {breaks_path}")
    breaks = common.read_jsonl(breaks_path)

    classified: list[common.JsonDict] = []
    by_severity: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    for brk in breaks:
        c = classify(brk["reason_code"])
        out = dict(brk)
        out["severity"] = c["severity"]
        out["owner_team"] = c["owner_team"]
        classified.append(out)
        by_severity[c["severity"]] = by_severity.get(c["severity"], 0) + 1
        by_owner[c["owner_team"]] = by_owner.get(c["owner_team"], 0) + 1

    classified_jsonl = run_dir / "stages" / "classified-breaks.jsonl"
    exceptions_json = run_dir / "stages" / "exceptions.json"
    common.write_jsonl(classified_jsonl, classified)
    common.write_json(
        exceptions_json,
        {
            "scenario": scenario,
            "runId": run_id,
            "counts": {"broken": len(classified)},
            "bySeverity": by_severity,
            "byOwner": by_owner,
        },
    )
    common.log(f"classified {len(classified)} breaks")

    result: common.JsonDict = {
        "status": "success",
        "counts": {"broken": len(classified)},
        "artifacts": common.artifacts_for(
            run_dir,
            [
                "stages/classified-breaks.jsonl",
                "stages/exceptions.json",
            ],
        ),
        "metrics": {"bySeverity": by_severity, "byOwner": by_owner},
    }
    return result, 0
