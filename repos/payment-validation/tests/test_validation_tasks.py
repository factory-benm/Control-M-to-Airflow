"""Tests for the payment-validation task functions against a temporary run directory."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payment_validation.common import MissingUpstreamError, read_jsonl
from payment_validation.tasks import (
    REPOSITORY,
    deduplicate_payments,
    validate_payment_schema,
)

KIT_ROOT = Path(__file__).resolve().parents[3]
SCENARIO_DOC = {"currencyAllowlist": "fixtures/fx/currency-allowlist.json"}


def _record(line: int, payment_id: str, **overrides: str) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "payment_id": payment_id,
        "source_system": "CHANNEL-SG",
        "value_date": "2026-03-16",
        "booking_timestamp": "2026-03-16T01:15:00Z",
        "debtor_account_token": "DEBTOR-0001",
        "creditor_account_token": "CREDITOR-0101",
        "currency": "SGD",
        "amount": "12500.00",
        "ledger_reference": "LEDG-SG-0001",
        "country_code": "SG",
        "line_number": line,
    }
    rec.update(overrides)
    return rec


def _write_stage(run_dir: Path, name: str, records: list[dict[str, Any]]) -> None:
    stages = run_dir / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r) + "\n" for r in records)
    (stages / name).write_text(text, encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestValidatePaymentSchema(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, scenario_doc: dict[str, Any] | None = None) -> dict[str, Any]:
        return validate_payment_schema(
            run_dir=str(self.run_dir),
            run_id="run-001",
            scenario="reconciliation-breaks",
            kit_root=KIT_ROOT,
            scenario_doc=SCENARIO_DOC if scenario_doc is None else scenario_doc,
            now="2026-03-16T09:30:00Z",
            attempt=1,
        )

    def test_splits_valid_and_rejected_rows(self) -> None:
        _write_stage(
            self.run_dir,
            "normalized-payments.jsonl",
            [
                _record(1, "PAY-2026-0001"),
                _record(2, "PAY-2026-0002", currency="XYZ"),
                _record(3, "PAY-2026-0003", amount="0.00"),
                _record(4, "PAY-2026-0004", currency="USD", amount="48000.00"),
            ],
        )
        result = self._run()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(result["repository"], REPOSITORY)
        self.assertEqual(result["task"], "validate_payment_schema")
        self.assertEqual(result["counts"], {"received": 4, "rejected": 2})

        stages = self.run_dir / "stages"
        validated = read_jsonl(stages / "validated-payments.jsonl")
        self.assertEqual([r["payment_id"] for r in validated], ["PAY-2026-0001", "PAY-2026-0004"])
        rejections = read_jsonl(stages / "rejections.jsonl")
        self.assertEqual(
            [(r["payment_id"], r["reason_code"]) for r in rejections],
            [
                ("PAY-2026-0002", "CURRENCY_NOT_ALLOWED"),
                ("PAY-2026-0003", "NON_POSITIVE_AMOUNT"),
            ],
        )
        self.assertEqual(rejections[0]["reason_detail"], "currency XYZ not in allowlist")

        meta = json.loads((stages / "validate.json").read_text(encoding="utf-8"))
        self.assertEqual(
            meta,
            {
                "counts": {"received": 4, "rejected": 2},
                "currencyAllowlistVersion": "2026.03",
                "runId": "run-001",
                "scenario": "reconciliation-breaks",
            },
        )

    def test_artifacts_sorted_with_hashes(self) -> None:
        _write_stage(self.run_dir, "normalized-payments.jsonl", [_record(1, "PAY-2026-0001")])
        result = self._run()
        paths = [a["path"] for a in result["artifacts"]]
        self.assertEqual(
            paths,
            [
                "stages/rejections.jsonl",
                "stages/validate.json",
                "stages/validated-payments.jsonl",
            ],
        )
        for artifact in result["artifacts"]:
            self.assertEqual(artifact["sha256"], _sha(self.run_dir / artifact["path"]))

    def test_empty_input_produces_empty_outputs(self) -> None:
        _write_stage(self.run_dir, "normalized-payments.jsonl", [])
        result = self._run()
        self.assertEqual(result["counts"], {"received": 0, "rejected": 0})
        self.assertEqual((self.run_dir / "stages" / "rejections.jsonl").read_text(), "")

    def test_missing_normalized_payments(self) -> None:
        with self.assertRaisesRegex(MissingUpstreamError, "normalized-payments.jsonl"):
            self._run()

    def test_missing_allowlist(self) -> None:
        _write_stage(self.run_dir, "normalized-payments.jsonl", [_record(1, "PAY-2026-0001")])
        with self.assertRaisesRegex(MissingUpstreamError, "missing currency allowlist"):
            self._run({"currencyAllowlist": "fixtures/fx/does-not-exist.json"})

    def test_default_allowlist_path(self) -> None:
        _write_stage(self.run_dir, "normalized-payments.jsonl", [_record(1, "PAY-2026-0001")])
        result = self._run({})
        self.assertEqual(result["counts"], {"received": 1, "rejected": 0})


class TestDeduplicatePayments(unittest.TestCase):
    def test_removes_later_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_stage(
                run_dir,
                "validated-payments.jsonl",
                [
                    _record(1, "PAY-2026-0001"),
                    _record(2, "PAY-2026-0002"),
                    _record(5, "PAY-2026-0001"),
                    _record(6, "PAY-2026-0002"),
                    _record(3, "PAY-2026-0003"),
                ],
            )
            result = deduplicate_payments(
                run_dir=run_dir,
                run_id="run-002",
                scenario="duplicate-retry",
                kit_root=KIT_ROOT,
                scenario_doc={},
                now="2026-03-16T09:30:00Z",
                attempt=2,
            )

            self.assertEqual(result["counts"], {"accepted": 3, "duplicatesRemoved": 2})
            self.assertEqual(result["attempt"], 2)
            self.assertEqual(result["task"], "deduplicate_payments")
            stages = run_dir / "stages"
            accepted = read_jsonl(stages / "deduplicated-payments.jsonl")
            self.assertEqual([r["line_number"] for r in accepted], [1, 2, 3])
            duplicates = read_jsonl(stages / "duplicates.jsonl")
            self.assertEqual(
                [(r["line_number"], r["duplicate_of_line"]) for r in duplicates],
                [(5, 1), (6, 2)],
            )
            meta = json.loads((stages / "deduplicate.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["counts"], {"accepted": 3, "duplicatesRemoved": 2})
            self.assertEqual(
                [a["path"] for a in result["artifacts"]],
                [
                    "stages/deduplicate.json",
                    "stages/deduplicated-payments.jsonl",
                    "stages/duplicates.jsonl",
                ],
            )

    def test_missing_validated_payments(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(MissingUpstreamError, "validated-payments.jsonl"),
        ):
            deduplicate_payments(
                run_dir=tmp,
                run_id="r",
                scenario="happy-path",
                kit_root=KIT_ROOT,
                scenario_doc={},
                now="2026-03-16T09:30:00Z",
                attempt=1,
            )


if __name__ == "__main__":
    unittest.main()
