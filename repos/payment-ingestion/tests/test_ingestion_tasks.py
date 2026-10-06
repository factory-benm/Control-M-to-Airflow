"""Task-level tests for payment-ingestion against temporary run directories."""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payment_ingestion.common import BusinessRuleError, MissingUpstreamError, read_jsonl
from payment_ingestion.tasks import (
    EXPECTED_HEADER,
    extract_payment_batch,
    verify_file_integrity,
    watch_inbound_files,
)

KIT_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PAYMENTS = KIT_ROOT / "fixtures" / "scenarios" / "happy-path" / "input" / "payments.csv"
HAPPY_LEDGER = KIT_ROOT / "fixtures" / "scenarios" / "happy-path" / "reference" / "ledger.csv"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _common_kwargs(run_dir: Path, kit_root: Path, scenario: str = "happy-path") -> dict[str, Any]:
    return {
        "run_dir": str(run_dir),
        "run_id": "run-001",
        "scenario": scenario,
        "kit_root": kit_root,
        "scenario_doc": {},
        "now": "2026-03-16T09:30:00Z",
        "attempt": 1,
    }


class TestWatchInboundFiles(unittest.TestCase):
    def test_detects_existing_file_on_first_poll(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            result = watch_inbound_files(**_common_kwargs(Path(run_dir), KIT_ROOT))
            manifest_path = Path(run_dir) / "stages" / "inbound-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(result["metrics"], {"polls": 1})
        self.assertEqual(result["artifacts"][0]["path"], "stages/inbound-manifest.json")
        self.assertEqual(
            manifest,
            {
                "detectedFile": "fixtures/scenarios/happy-path/input/payments.csv",
                "byteSize": HAPPY_PAYMENTS.stat().st_size,
                "detectionOutcome": "detected",
                "runId": "run-001",
                "scenario": "happy-path",
            },
        )

    def test_counts_polls_until_file_appears(self) -> None:
        with tempfile.TemporaryDirectory() as kit, tempfile.TemporaryDirectory() as run_dir:
            target = Path(kit) / "fixtures" / "scenarios" / "late" / "input" / "payments.csv"
            target.parent.mkdir(parents=True)
            sleeps: list[float] = []

            def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)
                if len(sleeps) == 2:
                    target.write_text("x\n", encoding="utf-8")

            with mock.patch("payment_ingestion.tasks.time.sleep", side_effect=fake_sleep):
                result = watch_inbound_files(**_common_kwargs(Path(run_dir), Path(kit), "late"))
        self.assertEqual(result["metrics"], {"polls": 3})
        self.assertEqual(sleeps, [0.1, 0.1])

    def test_missing_file_after_max_polls_raises(self) -> None:
        with tempfile.TemporaryDirectory() as kit, tempfile.TemporaryDirectory() as run_dir:
            sleep = mock.Mock()
            with (
                mock.patch("payment_ingestion.tasks.time.sleep", sleep),
                self.assertRaises(MissingUpstreamError) as ctx,
            ):
                watch_inbound_files(**_common_kwargs(Path(run_dir), Path(kit), "absent"))
        self.assertEqual(sleep.call_count, 20)
        self.assertIn("fixtures/scenarios/absent/input/payments.csv", str(ctx.exception))


class TestVerifyFileIntegrity(unittest.TestCase):
    def test_copies_inputs_and_records_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            result = verify_file_integrity(**_common_kwargs(Path(run_dir), KIT_ROOT))
            rd = Path(run_dir)
            manifest = json.loads((rd / "stages" / "file-integrity.json").read_text("utf-8"))
            copied_payments = (rd / "input" / "payments.csv").read_bytes()
            copied_ledger = (rd / "input" / "ledger.csv").read_bytes()
        self.assertEqual(copied_payments, HAPPY_PAYMENTS.read_bytes())
        self.assertEqual(copied_ledger, HAPPY_LEDGER.read_bytes())
        self.assertEqual(
            manifest["files"],
            [
                {
                    "path": "input/ledger.csv",
                    "sha256": _sha(HAPPY_LEDGER),
                    "byteSize": HAPPY_LEDGER.stat().st_size,
                    "source": "fixtures/scenarios/happy-path/reference/ledger.csv",
                },
                {
                    "path": "input/payments.csv",
                    "sha256": _sha(HAPPY_PAYMENTS),
                    "byteSize": HAPPY_PAYMENTS.stat().st_size,
                    "source": "fixtures/scenarios/happy-path/input/payments.csv",
                },
            ],
        )
        self.assertEqual(
            [a["path"] for a in result["artifacts"]],
            ["input/ledger.csv", "input/payments.csv", "stages/file-integrity.json"],
        )
        self.assertEqual(result["status"], "success")

    def test_missing_payments_fixture(self) -> None:
        with (
            tempfile.TemporaryDirectory() as kit,
            tempfile.TemporaryDirectory() as run_dir,
            self.assertRaises(MissingUpstreamError) as ctx,
        ):
            verify_file_integrity(**_common_kwargs(Path(run_dir), Path(kit), "empty"))
        self.assertIn("input/payments.csv", str(ctx.exception))

    def test_missing_ledger_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as kit, tempfile.TemporaryDirectory() as run_dir:
            src = Path(kit) / "fixtures" / "scenarios" / "noledger" / "input"
            src.mkdir(parents=True)
            shutil.copyfile(HAPPY_PAYMENTS, src / "payments.csv")
            with self.assertRaises(MissingUpstreamError) as ctx:
                verify_file_integrity(**_common_kwargs(Path(run_dir), Path(kit), "noledger"))
        self.assertIn("reference/ledger.csv", str(ctx.exception))

    def _corrupting_copy(self, corrupt_name: str) -> Callable[[Path, Path], Path]:
        real_copy = shutil.copyfile

        def copy(src: Path, dst: Path) -> Path:
            real_copy(src, dst)
            if Path(dst).name == corrupt_name:
                Path(dst).write_bytes(b"tampered\n")
            return dst

        return copy

    def test_payments_copy_mismatch_is_business_rule(self) -> None:
        with (
            tempfile.TemporaryDirectory() as run_dir,
            mock.patch(
                "payment_ingestion.tasks.shutil.copyfile",
                side_effect=self._corrupting_copy("payments.csv"),
            ),
            self.assertRaises(BusinessRuleError) as ctx,
        ):
            verify_file_integrity(**_common_kwargs(Path(run_dir), KIT_ROOT))
        self.assertIn("payments.csv", str(ctx.exception))

    def test_ledger_copy_mismatch_is_business_rule(self) -> None:
        with (
            tempfile.TemporaryDirectory() as run_dir,
            mock.patch(
                "payment_ingestion.tasks.shutil.copyfile",
                side_effect=self._corrupting_copy("ledger.csv"),
            ),
            self.assertRaises(BusinessRuleError) as ctx,
        ):
            verify_file_integrity(**_common_kwargs(Path(run_dir), KIT_ROOT))
        self.assertIn("ledger.csv", str(ctx.exception))


class TestExtractPaymentBatch(unittest.TestCase):
    def _prepare(self, run_dir: Path, csv_text: str, files: list[dict[str, str]]) -> None:
        (run_dir / "stages").mkdir(parents=True, exist_ok=True)
        (run_dir / "input").mkdir(parents=True, exist_ok=True)
        (run_dir / "input" / "payments.csv").write_text(csv_text, encoding="utf-8")
        (run_dir / "stages" / "file-integrity.json").write_text(
            json.dumps({"files": files}), encoding="utf-8"
        )

    def test_extracts_after_integrity_step(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            verify_file_integrity(**_common_kwargs(rd, KIT_ROOT))
            result = extract_payment_batch(**_common_kwargs(rd, KIT_ROOT))
            records = read_jsonl(rd / "stages" / "normalized-payments.jsonl")
            meta = json.loads((rd / "stages" / "extract.json").read_text(encoding="utf-8"))
        self.assertEqual(result["counts"], {"received": 6})
        self.assertEqual(len(records), 6)
        self.assertEqual(records[0]["payment_id"], "PAY-2026-0001")
        self.assertEqual(records[0]["amount"], "12500.00")
        self.assertEqual(records[0]["source_file_sha256"], _sha(HAPPY_PAYMENTS))
        self.assertEqual(meta["header"], EXPECTED_HEADER)
        self.assertEqual(meta["sourceFileSha256"], _sha(HAPPY_PAYMENTS))
        self.assertEqual(
            [a["path"] for a in result["artifacts"]],
            ["stages/extract.json", "stages/normalized-payments.jsonl"],
        )

    def test_blank_rows_are_skipped_without_consuming_line_numbers(self) -> None:
        header = ",".join(EXPECTED_HEADER)
        row_a = "PAY-B,SYS,2026-03-16,2026-03-16T01:00:00Z,D,C,SGD,1.00,L1,SG"
        row_b = "PAY-A,SYS,2026-03-16,2026-03-16T02:00:00Z,D,C,USD,2.50,L2,SG"
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            self._prepare(
                rd,
                f"{header}\n{row_a}\n\n{row_b}\n",
                [{"path": "input/payments.csv", "sha256": "abc"}],
            )
            result = extract_payment_batch(**_common_kwargs(rd, KIT_ROOT))
            records = read_jsonl(rd / "stages" / "normalized-payments.jsonl")
        self.assertEqual(result["counts"], {"received": 2})
        self.assertEqual(
            [(r["payment_id"], r["line_number"]) for r in records],
            [("PAY-A", 2), ("PAY-B", 1)],
        )

    def test_missing_integrity_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as d, self.assertRaises(MissingUpstreamError) as ctx:
            extract_payment_batch(**_common_kwargs(Path(d), KIT_ROOT))
        self.assertIn("file-integrity.json", str(ctx.exception))

    def test_payments_hash_absent_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._prepare(Path(d), "", [{"path": "input/ledger.csv", "sha256": "x"}])
            with self.assertRaises(MissingUpstreamError) as ctx:
                extract_payment_batch(**_common_kwargs(Path(d), KIT_ROOT))
        self.assertIn("payments hash absent", str(ctx.exception))

    def test_missing_payments_copy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            self._prepare(rd, "", [{"path": "input/payments.csv", "sha256": "x"}])
            (rd / "input" / "payments.csv").unlink()
            with self.assertRaises(MissingUpstreamError) as ctx:
                extract_payment_batch(**_common_kwargs(rd, KIT_ROOT))
        self.assertIn("input/payments.csv", str(ctx.exception))

    def test_wrong_header_is_business_rule(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            self._prepare(
                rd, "payment_id,amount\nP1,1.00\n", [{"path": "input/payments.csv", "sha256": "x"}]
            )
            with self.assertRaises(BusinessRuleError):
                extract_payment_batch(**_common_kwargs(rd, KIT_ROOT))
            self.assertFalse((rd / "stages" / "normalized-payments.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
