"""Isolated unit checks for break classification (CONTRACT 6.7)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exception_management import common
from exception_management.classify import CLASSIFICATION, classify, run


class TestClassification(unittest.TestCase):
    def test_missing_ledger_reference(self) -> None:
        c = classify("MISSING_LEDGER_REFERENCE")
        self.assertEqual(c["severity"], "high")
        self.assertEqual(c["owner_team"], "Nostro Operations")

    def test_currency_mismatch(self) -> None:
        c = classify("CURRENCY_MISMATCH")
        self.assertEqual(c["severity"], "high")
        self.assertEqual(c["owner_team"], "FX Operations")

    def test_amount_mismatch(self) -> None:
        c = classify("AMOUNT_MISMATCH")
        self.assertEqual(c["severity"], "medium")
        self.assertEqual(c["owner_team"], "Reconciliation Operations")

    def test_unmatched_payment(self) -> None:
        c = classify("UNMATCHED_PAYMENT")
        self.assertEqual(c["severity"], "medium")
        self.assertEqual(c["owner_team"], "Payments Investigations")

    def test_all_reason_codes_covered(self) -> None:
        self.assertEqual(
            set(CLASSIFICATION.keys()),
            {
                "MISSING_LEDGER_REFERENCE",
                "CURRENCY_MISMATCH",
                "AMOUNT_MISMATCH",
                "UNMATCHED_PAYMENT",
            },
        )

    def test_unknown_reason_is_business_rule(self) -> None:
        with self.assertRaises(common.BusinessRuleError) as ctx:
            classify("DUPLICATE_LEDGER")
        self.assertEqual(ctx.exception.code, "UNKNOWN_BREAK_REASON")
        self.assertIn("reason_code=DUPLICATE_LEDGER", str(ctx.exception))


def make_ctx(run_dir: Path) -> common.TaskContext:
    return {
        "run_dir": run_dir,
        "kit_root": run_dir,
        "repo_root": run_dir,
        "scenario": "reconciliation-breaks",
        "scenario_cfg": {},
        "run_id": "rid-9",
        "now": "2026-03-16T09:30:00Z",
        "attempt": 1,
    }


def write_breaks(run_dir: Path, breaks: list[dict[str, Any]]) -> None:
    common.write_jsonl(run_dir / "stages" / "breaks.jsonl", breaks)


class TestRun(unittest.TestCase):
    def test_classifies_and_aggregates_breaks(self) -> None:
        breaks = [
            {"payment_id": "PAY-2026-0045", "line_number": 5, "reason_code": "CURRENCY_MISMATCH"},
            {"payment_id": "PAY-2026-0043", "line_number": 3, "reason_code": "AMOUNT_MISMATCH"},
            {"payment_id": "PAY-2026-0044", "line_number": 4, "reason_code": "CURRENCY_MISMATCH"},
        ]
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            write_breaks(rd, breaks)
            with self.assertLogs("exception-mgmt", level="INFO") as logs:
                result, exit_code = run(make_ctx(rd))
            classified = common.read_jsonl(rd / "stages" / "classified-breaks.jsonl")
            summary = json.loads((rd / "stages" / "exceptions.json").read_text("utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["counts"], {"broken": 3})
        self.assertEqual(result["metrics"]["bySeverity"], {"high": 2, "medium": 1})
        self.assertEqual(
            result["metrics"]["byOwner"],
            {"FX Operations": 2, "Reconciliation Operations": 1},
        )
        self.assertEqual(
            [a["path"] for a in result["artifacts"]],
            ["stages/classified-breaks.jsonl", "stages/exceptions.json"],
        )
        self.assertEqual(
            [(r["payment_id"], r["severity"], r["owner_team"]) for r in classified],
            [
                ("PAY-2026-0043", "medium", "Reconciliation Operations"),
                ("PAY-2026-0044", "high", "FX Operations"),
                ("PAY-2026-0045", "high", "FX Operations"),
            ],
        )
        self.assertEqual(classified[0]["reason_code"], "AMOUNT_MISMATCH")
        self.assertEqual(
            summary,
            {
                "scenario": "reconciliation-breaks",
                "runId": "rid-9",
                "counts": {"broken": 3},
                "bySeverity": {"high": 2, "medium": 1},
                "byOwner": {"FX Operations": 2, "Reconciliation Operations": 1},
            },
        )
        self.assertIn("INFO:exception-mgmt:classified 3 breaks", logs.output)

    def test_no_breaks_writes_empty_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            write_breaks(rd, [])
            with self.assertLogs("exception-mgmt", level="INFO"):
                result, exit_code = run(make_ctx(rd))
            jsonl_text = (rd / "stages" / "classified-breaks.jsonl").read_text("utf-8")
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["counts"], {"broken": 0})
        self.assertEqual(result["metrics"], {"bySeverity": {}, "byOwner": {}})
        self.assertEqual(jsonl_text, "")

    def test_missing_breaks_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with (
                self.assertLogs("exception-mgmt", level="INFO"),
                self.assertRaises(common.MissingUpstreamError),
            ):
                run(make_ctx(Path(d)))
            self.assertFalse((Path(d) / "stages" / "exceptions.json").exists())


if __name__ == "__main__":
    unittest.main()
