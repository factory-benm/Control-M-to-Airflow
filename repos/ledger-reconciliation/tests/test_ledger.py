"""Isolated unit check for ON CONFLICT idempotency of a single insert."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ledger_reconciliation.ledger import create_ledger, _insert_rows


class TestIdempotentInsert(unittest.TestCase):
    def test_duplicate_insert_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "schema").mkdir()
            (repo_root / "schema" / "ledger.sql").write_text(
                "CREATE TABLE IF NOT EXISTS ledger_entry ("
                "payment_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                "scenario TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL, "
                "base_amount TEXT NOT NULL, ledger_reference TEXT NOT NULL, "
                "effective_value_date TEXT NOT NULL, state TEXT NOT NULL, "
                "source_file_sha256 TEXT NOT NULL, posted_at_logical TEXT NOT NULL);",
                encoding="utf-8",
            )
            db = repo_root / "ledger.sqlite3"
            create_ledger(db, repo_root)
            row = {
                "payment_id": "PAY-2026-0001",
                "run_id": "test",
                "scenario": "happy-path",
                "currency": "SGD",
                "amount": "12500.00",
                "base_amount": "9312.50",
                "ledger_reference": "LEDG-SG-0001",
                "effective_value_date": "2026-03-16",
                "source_file_sha256": "abc",
            }
            n1 = _insert_rows(db, [row], "2026-03-16T09:30:00Z")
            n2 = _insert_rows(db, [row], "2026-03-16T09:30:00Z")
            self.assertEqual(n1, 1)
            self.assertEqual(n2, 0)
            conn = sqlite3.connect(str(db))
            count = conn.execute("SELECT COUNT(*) FROM ledger_entry").fetchone()[0]
            conn.close()
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
