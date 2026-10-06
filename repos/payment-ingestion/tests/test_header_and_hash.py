"""Unit tests for payment-ingestion pure functions (standard library only)."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make src importable when run via scripts/test.sh.
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from payment_ingestion.tasks import EXPECTED_HEADER, row_to_base_record  # noqa: E402
from payment_ingestion.common import sha256_file, write_jsonl, read_jsonl  # noqa: E402


class TestExpectedHeader(unittest.TestCase):
    def test_header_matches_contract(self):
        self.assertEqual(
            EXPECTED_HEADER,
            [
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
            ],
        )

    def test_header_has_ten_fields(self):
        self.assertEqual(len(EXPECTED_HEADER), 10)


class TestSha256File(unittest.TestCase):
    def test_known_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.txt"
            p.write_bytes(b"hello")
            expected = hashlib.sha256(b"hello").hexdigest()
            self.assertEqual(sha256_file(p), expected)

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.txt"
            p.write_bytes(b"")
            self.assertEqual(sha256_file(p), hashlib.sha256(b"").hexdigest())


class TestRowToBaseRecord(unittest.TestCase):
    def test_fields_sorted_alphabetically(self):
        row = [
            "PAY-2026-0001", "CHANNEL-SG", "2026-03-16", "2026-03-16T01:15:00Z",
            "DEBTOR-0001", "CREDITOR-0101", "SGD", "12500.00",
            "LEDG-SG-0001", "SG",
        ]
        rec = row_to_base_record(row, 1, "rid", "happy-path", "abc123")
        keys = list(rec.keys())
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(rec["line_number"], 1)
        self.assertEqual(rec["payment_id"], "PAY-2026-0001")
        self.assertEqual(rec["source_file_sha256"], "abc123")
        self.assertEqual(rec["amount"], "12500.00")

    def test_line_number_increments(self):
        row = ["PAY-2026-0002"] + ["x"] * 8 + ["SG"]
        rec = row_to_base_record(row, 7, "rid", "s", "h")
        self.assertEqual(rec["line_number"], 7)


class TestJsonlDeterminism(unittest.TestCase):
    def test_sorted_by_payment_id_then_line(self):
        records = [
            {"payment_id": "PAY-2026-0002", "line_number": 1, "v": "b"},
            {"payment_id": "PAY-2026-0001", "line_number": 2, "v": "a2"},
            {"payment_id": "PAY-2026-0001", "line_number": 1, "v": "a1"},
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "out.jsonl"
            write_jsonl(p, records)
            lines = p.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            first = json.loads(lines[0])
            second = json.loads(lines[1])
            third = json.loads(lines[2])
            self.assertEqual(first["payment_id"], "PAY-2026-0001")
            self.assertEqual(first["line_number"], 1)
            self.assertEqual(second["payment_id"], "PAY-2026-0001")
            self.assertEqual(second["line_number"], 2)
            self.assertEqual(third["payment_id"], "PAY-2026-0002")
            # keys sorted within each line
            for line in lines:
                obj = json.loads(line)
                self.assertEqual(list(obj.keys()), sorted(obj.keys()))

    def test_round_trip(self):
        records = [{"payment_id": "P1", "line_number": 1, "x": "1"}]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "out.jsonl"
            write_jsonl(p, records)
            self.assertEqual(read_jsonl(p), records)


if __name__ == "__main__":
    unittest.main()
