"""The reconcile_nostro_ledger task against the fixture reference ledgers."""

import json
import tempfile
import unittest
from pathlib import Path

from ledger_test_support import SCENARIOS, make_context, quietly, read_jsonl, seed_run_dir

from ledger_reconciliation import common, cutoff, ledger, reconcile


class _ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name) / "recon-run"

    def prepare(self, scenario: str) -> common.TaskContext:
        seed_run_dir(self.run_dir, scenario)
        ctx = make_context(self.run_dir, scenario)
        quietly(cutoff.run, ctx)
        quietly(ledger.run, ctx)
        return ctx


class TestReconcileRun(_ReconcileTest):
    def test_happy_path_matches_everything(self) -> None:
        ctx = self.prepare("happy-path")
        result, code = quietly(reconcile.run, ctx)

        matched = read_jsonl(self.run_dir / "stages" / "matched.jsonl")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["counts"]["broken"], 0)
        self.assertEqual(result["counts"]["matched"], len(matched))
        self.assertEqual(result["metrics"]["posted"], len(matched))
        self.assertEqual(
            [a["path"] for a in result["artifacts"]],
            ["stages/breaks.jsonl", "stages/matched.jsonl", "stages/reconciliation.json"],
        )
        self.assertEqual((self.run_dir / "stages" / "breaks.jsonl").read_text(), "")
        first = matched[0]
        self.assertEqual(first["match_status"], "matched")
        self.assertEqual(first["line_number"], 1)
        self.assertEqual(first["run_id"], "recon-run")

    def test_reconciliation_breaks_match_the_oracle(self) -> None:
        ctx = self.prepare("reconciliation-breaks")
        result, code = quietly(reconcile.run, ctx)

        expected = json.loads(
            (SCENARIOS / "reconciliation-breaks" / "expected" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        breaks = read_jsonl(self.run_dir / "stages" / "breaks.jsonl")
        matched = read_jsonl(self.run_dir / "stages" / "matched.jsonl")

        self.assertEqual(code, 0)
        self.assertEqual(
            sorted((b["payment_id"], b["reason_code"]) for b in breaks),
            sorted((b["payment_id"], b["reason_code"]) for b in expected["breaks"]),
        )
        self.assertEqual(
            sorted(r["payment_id"] for r in matched), sorted(expected["matchedPaymentIds"])
        )
        self.assertTrue(all(b["match_status"] == "broken" for b in breaks))
        self.assertEqual(
            result["counts"],
            {"matched": expected["counts"]["matched"], "broken": expected["counts"]["broken"]},
        )
        self.assertEqual(result["metrics"]["posted"], expected["counts"]["posted"])
        summary = common.read_json(self.run_dir / "stages" / "reconciliation.json")
        self.assertEqual(summary["counts"], result["counts"])
        self.assertEqual(summary["runId"], "recon-run")

    def test_line_number_defaults_to_zero_when_cutoff_lacks_the_payment(self) -> None:
        ctx = self.prepare("happy-path")
        cutoff_path = self.run_dir / "stages" / "cutoff-payments.jsonl"
        rows = common.read_jsonl(cutoff_path)
        common.write_jsonl(cutoff_path, rows[1:])

        quietly(reconcile.run, ctx)
        matched = {
            r["payment_id"]: r for r in read_jsonl(self.run_dir / "stages" / "matched.jsonl")
        }
        self.assertEqual(matched[rows[0]["payment_id"]]["line_number"], 0)


class TestReconcileMissingInputs(_ReconcileTest):
    def test_missing_ledger(self) -> None:
        seed_run_dir(self.run_dir, "happy-path")
        with self.assertRaisesRegex(common.MissingUpstreamError, "ledger not found"):
            quietly(reconcile.run, make_context(self.run_dir, "happy-path"))

    def test_missing_reference_ledger(self) -> None:
        ctx = self.prepare("happy-path")
        (self.run_dir / "input" / "ledger.csv").unlink()
        with self.assertRaisesRegex(common.MissingUpstreamError, "reference ledger not found"):
            quietly(reconcile.run, ctx)

    def test_missing_cutoff_stage(self) -> None:
        ctx = self.prepare("happy-path")
        (self.run_dir / "stages" / "cutoff-payments.jsonl").unlink()
        with self.assertRaisesRegex(common.MissingUpstreamError, "upstream artifact not found"):
            quietly(reconcile.run, ctx)


if __name__ == "__main__":
    unittest.main()
