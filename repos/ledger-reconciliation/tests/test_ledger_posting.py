"""The post_pending_ledger task: idempotent posting and deterministic partial writes."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ledger_test_support import make_context, quietly, seed_run_dir

from ledger_reconciliation import common, cutoff, ledger


def _ledger_ids(run_dir: Path) -> list[str]:
    conn = sqlite3.connect(str(run_dir / "ledger" / "ledger.sqlite3"))
    try:
        return [row[0] for row in conn.execute("SELECT payment_id FROM ledger_entry ORDER BY 1")]
    finally:
        conn.close()


class _PostingTest(unittest.TestCase):
    scenario = "happy-path"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name) / "posting-run"
        seed_run_dir(self.run_dir, self.scenario)
        quietly(cutoff.run, make_context(self.run_dir, self.scenario))
        self.payment_ids = sorted(
            r["payment_id"]
            for r in common.read_jsonl(self.run_dir / "stages" / "cutoff-payments.jsonl")
        )


class TestPostPendingLedger(_PostingTest):
    def test_first_run_posts_every_payment(self) -> None:
        result, code = quietly(ledger.run, make_context(self.run_dir, self.scenario))
        total = len(self.payment_ids)

        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["counts"], {"posted": total})
        self.assertEqual(result["metrics"], {"inserted": total, "alreadyPosted": 0})
        self.assertEqual(
            [a["path"] for a in result["artifacts"]],
            ["ledger/ledger.sqlite3", "stages/ledger-posting.json"],
        )
        self.assertEqual(_ledger_ids(self.run_dir), self.payment_ids)
        summary = common.read_json(self.run_dir / "stages" / "ledger-posting.json")
        self.assertEqual(summary["attempt"], 1)
        self.assertEqual(summary["runId"], "posting-run")
        self.assertNotIn("fault", summary)

    def test_rerun_is_idempotent(self) -> None:
        quietly(ledger.run, make_context(self.run_dir, self.scenario))
        result, code = quietly(ledger.run, make_context(self.run_dir, self.scenario))
        total = len(self.payment_ids)

        self.assertEqual(code, 0)
        self.assertEqual(result["counts"], {"posted": total})
        self.assertEqual(result["metrics"], {"inserted": 0, "alreadyPosted": total})
        self.assertEqual(_ledger_ids(self.run_dir), self.payment_ids)

    def test_rows_carry_logical_clock_and_posted_state(self) -> None:
        ctx = make_context(self.run_dir, self.scenario)
        quietly(ledger.run, ctx)
        conn = sqlite3.connect(str(self.run_dir / "ledger" / "ledger.sqlite3"))
        try:
            states = conn.execute("SELECT DISTINCT state, posted_at_logical FROM ledger_entry")
            self.assertEqual(states.fetchall(), [("posted", ctx["now"])])
        finally:
            conn.close()

    def test_missing_cutoff_stage_is_missing_upstream(self) -> None:
        (self.run_dir / "stages" / "cutoff-payments.jsonl").unlink()
        with self.assertRaisesRegex(common.MissingUpstreamError, "cutoff-payments.jsonl"):
            quietly(ledger.run, make_context(self.run_dir, self.scenario))

    def test_missing_schema_is_usage_error(self) -> None:
        empty_repo = self.run_dir / "no-schema"
        with self.assertRaisesRegex(common.UsageError, "ledger schema not found"):
            quietly(ledger.run, make_context(self.run_dir, self.scenario, repo_root=empty_repo))

    def test_insert_rows_with_nothing_to_write_does_not_open_the_ledger(self) -> None:
        missing = self.run_dir / "ledger" / "never.sqlite3"
        self.assertEqual(ledger._insert_rows(missing, [], "now"), 0)
        self.assertFalse(missing.exists())


class TestPartialLedgerWrite(_PostingTest):
    scenario = "partial-ledger-write"

    def test_first_attempt_commits_after_writes_then_faults(self) -> None:
        result, code = quietly(ledger.run, make_context(self.run_dir, self.scenario, attempt=1))

        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["counts"], {"posted": 4})
        self.assertEqual(result["metrics"], {"inserted": 4, "alreadyPosted": 0})
        self.assertEqual(
            result["error"],
            {
                "code": "INJECTED_LEDGER_FAULT",
                "message": "deterministic fault after 4 writes on attempt 1",
            },
        )
        self.assertEqual(_ledger_ids(self.run_dir), self.payment_ids[:4])
        summary = common.read_json(self.run_dir / "stages" / "ledger-posting.json")
        self.assertEqual(
            summary["fault"],
            {"reasonCode": "INJECTED_LEDGER_FAULT", "afterWrites": 4, "attempt": 1},
        )

    def test_retry_writes_only_the_missing_payments(self) -> None:
        quietly(ledger.run, make_context(self.run_dir, self.scenario, attempt=1))
        result, code = quietly(ledger.run, make_context(self.run_dir, self.scenario, attempt=2))

        self.assertEqual(code, 0)
        self.assertEqual(result["counts"], {"posted": 7})
        self.assertEqual(result["metrics"], {"inserted": 3, "alreadyPosted": 4})
        self.assertEqual(_ledger_ids(self.run_dir), self.payment_ids)
        summary = common.read_json(self.run_dir / "stages" / "ledger-posting.json")
        self.assertEqual(summary["attempt"], 2)

    def test_repeating_the_faulting_attempt_does_not_double_post(self) -> None:
        quietly(ledger.run, make_context(self.run_dir, self.scenario, attempt=1))
        result, code = quietly(ledger.run, make_context(self.run_dir, self.scenario, attempt=1))

        self.assertEqual(code, 3)
        self.assertEqual(result["counts"], {"posted": 4})
        self.assertEqual(result["metrics"], {"inserted": 0, "alreadyPosted": 4})
        self.assertEqual(_ledger_ids(self.run_dir), self.payment_ids[:4])


class TestFaultSelection(unittest.TestCase):
    def test_fault_only_applies_to_post_pending_ledger(self) -> None:
        fault = {"task": "post_pending_ledger", "attempt": 1, "afterWrites": 2}
        self.assertEqual(ledger._fault_for_scenario({"faultInjection": fault}), fault)
        self.assertIsNone(ledger._fault_for_scenario({}))
        self.assertIsNone(ledger._fault_for_scenario({"faultInjection": {}}))
        self.assertIsNone(
            ledger._fault_for_scenario({"faultInjection": {"task": "classify_breaks"}})
        )


if __name__ == "__main__":
    unittest.main()
