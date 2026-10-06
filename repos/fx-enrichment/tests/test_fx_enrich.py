"""Tests for the enrich_fx_rates task against run directories (CONTRACT 6.3)."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fx_support import KIT_ROOT, dedup_record, write_dedup_stage

from fx_enrichment.common import BusinessRuleError, MissingUpstreamError
from fx_enrichment.tasks import REPOSITORY, enrich_fx_rates

SCENARIO_DOC = {"fxTable": "fixtures/fx/fx-rates.json"}


class TestEnrichFxRates(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name) / "run-test"
        self.run_dir.mkdir()

    def _enrich(
        self, scenario_doc: dict[str, Any], kit_root: Path = KIT_ROOT, attempt: int = 1
    ) -> dict[str, Any]:
        with contextlib.redirect_stderr(io.StringIO()):
            return enrich_fx_rates(
                run_dir=str(self.run_dir),
                run_id="run-test",
                scenario="happy-path",
                kit_root=kit_root,
                scenario_doc=scenario_doc,
                now="2026-03-16T09:30:00Z",
                attempt=attempt,
            )

    def _read_enriched(self) -> list[dict[str, Any]]:
        text = (self.run_dir / "stages" / "enriched-payments.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_enriches_rows_with_rate_and_base_amount(self) -> None:
        write_dedup_stage(
            self.run_dir,
            [
                dedup_record("PAY-2026-0003", 4, "2026-03-16", "HKD", "96000.00"),
                dedup_record("PAY-2026-0001", 2, "2026-03-16", "SGD", "12500.00"),
                dedup_record("PAY-2026-0002", 3, "2026-03-17", "USD", "48000.00"),
            ],
        )
        result = self._enrich(SCENARIO_DOC, attempt=2)

        rows = self._read_enriched()
        self.assertEqual(
            [r["payment_id"] for r in rows], ["PAY-2026-0001", "PAY-2026-0002", "PAY-2026-0003"]
        )
        sgd = rows[0]
        self.assertEqual(sgd["base_amount"], "9312.50")
        self.assertEqual(sgd["base_currency"], "USD")
        self.assertEqual(sgd["fx_rate"], "0.745000")
        self.assertEqual(sgd["fx_rate_date"], "2026-03-16")
        self.assertEqual(sgd["fx_table_version"], "2026.03.1")
        self.assertEqual(sgd["source_file_sha256"], "0" * 64)
        self.assertEqual(rows[1]["base_amount"], "48000.00")
        self.assertEqual(rows[1]["fx_rate_date"], "2026-03-17")
        self.assertEqual(rows[2]["base_amount"], "12288.00")

        meta = json.loads((self.run_dir / "stages" / "fx.json").read_text(encoding="utf-8"))
        self.assertEqual(
            meta,
            {
                "baseCurrency": "USD",
                "counts": {"accepted": 3},
                "fxTableVersion": "2026.03.1",
                "runId": "run-test",
                "scenario": "happy-path",
            },
        )

        self.assertEqual(result["task"], "enrich_fx_rates")
        self.assertEqual(result["repository"], REPOSITORY)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(result["counts"], {"accepted": 3})
        self.assertEqual(result["metrics"], {})
        paths = [a["path"] for a in result["artifacts"]]
        self.assertEqual(paths, ["stages/enriched-payments.jsonl", "stages/fx.json"])
        for artifact in result["artifacts"]:
            data = (self.run_dir / artifact["path"]).read_bytes()
            self.assertEqual(artifact["sha256"], hashlib.sha256(data).hexdigest())

    def test_output_is_deterministic_across_reruns(self) -> None:
        write_dedup_stage(
            self.run_dir, [dedup_record("PAY-2026-0001", 2, "2026-03-16", "SGD", "12500.00")]
        )
        first = self._enrich(SCENARIO_DOC)
        second = self._enrich(SCENARIO_DOC)
        self.assertEqual(first, second)

    def test_empty_batch_writes_empty_stage(self) -> None:
        write_dedup_stage(self.run_dir, [])
        result = self._enrich(SCENARIO_DOC)
        self.assertEqual(result["counts"], {"accepted": 0})
        self.assertEqual(self._read_enriched(), [])

    def test_default_fx_table_used_when_scenario_omits_it(self) -> None:
        write_dedup_stage(
            self.run_dir, [dedup_record("PAY-2026-0005", 6, "2026-03-16", "JPY", "3800000.00")]
        )
        self._enrich({})
        self.assertEqual(self._read_enriched()[0]["base_amount"], "25460.00")

    def test_scenario_fx_table_path_is_relative_to_kit_root(self) -> None:
        kit = Path(self._tmp.name) / "kit"
        (kit / "fixtures" / "fx").mkdir(parents=True)
        table = {
            "baseCurrency": "USD",
            "fxTableVersion": "test.1",
            "rates": {"2026-03-16": {"EUR": "1.333333"}},
        }
        (kit / "fixtures" / "fx" / "alt.json").write_text(json.dumps(table), encoding="utf-8")
        write_dedup_stage(self.run_dir, [dedup_record("PAY-9", 2, "2026-03-16", "EUR", "10.01")])
        self._enrich({"fxTable": "fixtures/fx/alt.json"}, kit_root=kit)
        row = self._read_enriched()[0]
        # 10.01 * 1.333333 = 13.34666333 -> 13.35
        self.assertEqual(row["base_amount"], "13.35")
        self.assertEqual(row["fx_table_version"], "test.1")

    def test_missing_dedup_stage_is_missing_upstream(self) -> None:
        with self.assertRaisesRegex(MissingUpstreamError, "deduplicated-payments.jsonl"):
            self._enrich(SCENARIO_DOC)
        self.assertFalse((self.run_dir / "stages").exists())

    def test_missing_fx_table_is_missing_upstream(self) -> None:
        write_dedup_stage(self.run_dir, [])
        with self.assertRaisesRegex(MissingUpstreamError, "missing FX table: fixtures/fx/none"):
            self._enrich({"fxTable": "fixtures/fx/none.json"})

    def test_missing_rate_is_business_rule_and_writes_nothing(self) -> None:
        write_dedup_stage(
            self.run_dir,
            [
                dedup_record("PAY-1", 2, "2026-03-16", "SGD", "1.00"),
                dedup_record("PAY-2", 3, "2026-04-01", "SGD", "1.00"),
            ],
        )
        with self.assertRaisesRegex(BusinessRuleError, "missing FX rate for 2026-04-01 SGD"):
            self._enrich(SCENARIO_DOC)
        self.assertFalse((self.run_dir / "stages" / "enriched-payments.jsonl").exists())
        self.assertFalse((self.run_dir / "stages" / "fx.json").exists())


if __name__ == "__main__":
    unittest.main()
