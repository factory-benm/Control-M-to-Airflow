"""Tests for produce_settlement_report and the settlement-reporting CLI.

Each test builds a small, self-consistent run directory: five received
payments, one rejected, one duplicate, three accepted and posted, two matched
and one broken.
"""

import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from settlement_reporting import cli, common, report

KIT_ROOT = Path(__file__).resolve().parents[3]
NOW = "2026-03-16T09:30:00Z"
PAYOPS_ENV = ("PAYOPS_KIT_ROOT", "PAYOPS_FIXED_CLOCK", "PAYOPS_ATTEMPT", "PAYOPS_REPO_ROOT")

EXPECTED_COUNTS = {
    "received": 5,
    "rejected": 1,
    "duplicatesRemoved": 1,
    "accepted": 3,
    "cutoffAdjusted": 1,
    "posted": 3,
    "matched": 2,
    "broken": 1,
}


def _payment(line: int, pid: str, currency: str, amount: str) -> dict[str, Any]:
    return {
        "payment_id": pid,
        "line_number": line,
        "ledger_reference": "LEDG-SG-" + pid[-4:],
        "currency": currency,
        "amount": amount,
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _base_amount(amount: str, rate: str) -> str:
    return str((Decimal(amount) * Decimal(rate)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def build_run_dir(run_dir: Path) -> None:
    accepted = [
        _payment(1, "PAY-2026-0001", "SGD", "12500.00"),
        _payment(2, "PAY-2026-0002", "USD", "48000.00"),
        _payment(3, "PAY-2026-0003", "EUR", "1000.05"),
    ]
    rates = {"SGD": "0.7432", "USD": "1", "EUR": "1.0851"}
    enriched = []
    for p in accepted:
        rec = dict(p, base_amount=_base_amount(p["amount"], rates[p["currency"]]))
        rec["effective_value_date"] = "2026-03-16"
        enriched.append(rec)
    cutoff = [dict(r, cutoff_applied=False) for r in enriched]
    cutoff[1]["cutoff_applied"] = True
    cutoff[1]["effective_value_date"] = "2026-03-17"

    (run_dir / "input").mkdir(parents=True)
    with (run_dir / "input" / "payments.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["payment_id", "currency", "amount"])
        for pid in ("0001", "0002", "0003", "0004", "0001"):
            writer.writerow(["PAY-2026-" + pid, "SGD", "1.00"])

    stages = run_dir / "stages"
    rejected = dict(_payment(4, "PAY-2026-0004", "XYZ", "5.00"))
    rejected["reason_code"] = "CURRENCY_NOT_ALLOWED"
    _write_jsonl(stages / "rejections.jsonl", [rejected])
    duplicate = dict(_payment(5, "PAY-2026-0005", "SGD", "12500.00"), duplicate_of_line=1)
    _write_jsonl(stages / "duplicates.jsonl", [duplicate])
    _write_jsonl(stages / "enriched-payments.jsonl", enriched)
    _write_jsonl(stages / "cutoff-payments.jsonl", cutoff)
    _write_jsonl(stages / "matched.jsonl", enriched[:2])
    _write_jsonl(stages / "breaks.jsonl", [dict(enriched[2], reason_code="AMOUNT_MISMATCH")])
    classified = dict(
        enriched[2], reason_code="AMOUNT_MISMATCH", severity="high", owner_team="nostro-ops"
    )
    _write_jsonl(stages / "classified-breaks.jsonl", [classified])

    (run_dir / "ledger").mkdir()
    conn = sqlite3.connect(str(run_dir / "ledger" / "ledger.sqlite3"))
    try:
        conn.execute(
            "CREATE TABLE ledger_entry (payment_id TEXT, ledger_reference TEXT, "
            "currency TEXT, amount TEXT, base_amount TEXT, effective_value_date TEXT, "
            "state TEXT)"
        )
        rows = [
            (
                r["payment_id"],
                r["ledger_reference"],
                r["currency"],
                r["amount"],
                r["base_amount"],
                r["effective_value_date"],
                "posted",
            )
            for r in cutoff
        ]
        rows.append(("PAY-2026-0099", "LEDG-SG-0099", "SGD", "1.00", "0.74", "", "pending"))
        conn.executemany("INSERT INTO ledger_entry VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


class _RunDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name) / "run-42"
        build_run_dir(self.run_dir)
        env = {k: v for k, v in os.environ.items() if k not in PAYOPS_ENV}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestCounting(_RunDirCase):
    def test_assemble_counts_from_artifacts(self) -> None:
        self.assertEqual(report.assemble_counts(self.run_dir), EXPECTED_COUNTS)

    def test_missing_optional_artifacts_count_zero(self) -> None:
        empty = Path(self._tmp.name) / "empty"
        self.assertEqual(report.count_csv_rows(empty / "payments.csv"), 0)
        self.assertEqual(report.count_jsonl(empty / "x.jsonl"), 0)
        self.assertEqual(sum(1 for _ in report._iter_cutoff_adjusted(empty / "c.jsonl")), 0)

    def test_empty_csv_counts_zero(self) -> None:
        path = Path(self._tmp.name) / "empty.csv"
        path.write_text("", encoding="utf-8")
        self.assertEqual(report.count_csv_rows(path), 0)

    def test_count_posted_ignores_other_states(self) -> None:
        self.assertEqual(report.count_posted(self.run_dir / "ledger" / "ledger.sqlite3"), 3)

    def test_count_posted_missing_ledger(self) -> None:
        with self.assertRaisesRegex(common.MissingUpstreamError, "ledger not found"):
            report.count_posted(Path(self._tmp.name) / "none.sqlite3")

    def test_archive_manifest_respects_exclude(self) -> None:
        excluded = "stages/matched.jsonl"
        items = report.build_archive_manifest(self.run_dir, exclude={excluded})
        paths = [i["path"] for i in items]
        self.assertEqual(paths, sorted(paths))
        self.assertNotIn(excluded, paths)
        self.assertIn("ledger/ledger.sqlite3", paths)
        self.assertIn("input/payments.csv", paths)
        csv_item = next(i for i in items if i["path"] == "input/payments.csv")
        self.assertEqual(csv_item["size"], (self.run_dir / "input" / "payments.csv").stat().st_size)
        self.assertEqual(
            csv_item["sha256"], common.sha256_file(self.run_dir / "input" / "payments.csv")
        )


class TestRun(_RunDirCase):
    def _run(self, run_dir: Path | None = None) -> tuple[dict[str, Any], int]:
        ctx: common.TaskContext = {
            "run_dir": run_dir or self.run_dir,
            "kit_root": KIT_ROOT,
            "repo_root": KIT_ROOT / "repos" / "settlement-reporting",
            "scenario": "reconciliation-breaks",
            "scenario_cfg": {},
            "run_id": "run-42",
            "now": NOW,
            "attempt": 1,
        }
        with redirect_stderr(io.StringIO()):
            return report.run(ctx)

    def test_result_and_artifacts(self) -> None:
        result, exit_code = self._run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["counts"], EXPECTED_COUNTS)
        self.assertEqual(result["metrics"], {"ledgerRows": 3})
        self.assertEqual(
            [a["path"] for a in result["artifacts"]],
            [
                "output/archive-manifest.json",
                "output/settlement-report.csv",
                "output/settlement-report.json",
                "stages/reporting.json",
            ],
        )
        for artifact in result["artifacts"]:
            self.assertEqual(
                artifact["sha256"], common.sha256_file(self.run_dir / artifact["path"])
            )

    def test_settlement_report_json(self) -> None:
        self._run()
        doc = common.read_json(self.run_dir / "output" / "settlement-report.json")
        self.assertEqual(doc["counts"], EXPECTED_COUNTS)
        self.assertEqual(doc["countEquations"], report.COUNT_EQUATIONS)
        self.assertEqual(doc["generatedAt"], NOW)
        self.assertEqual(doc["ledgerRowCount"], 3)
        self.assertEqual(
            [m["payment_id"] for m in doc["matched"]], ["PAY-2026-0001", "PAY-2026-0002"]
        )
        self.assertEqual(
            doc["broken"],
            [
                {
                    "payment_id": "PAY-2026-0003",
                    "reason_code": "AMOUNT_MISMATCH",
                    "severity": "high",
                    "owner_team": "nostro-ops",
                }
            ],
        )
        matched_total = sum(Decimal(m["amount"]) for m in doc["matched"])
        self.assertEqual(matched_total, Decimal("60500.00"))

    def test_settlement_report_csv_dispositions(self) -> None:
        self._run()
        with (self.run_dir / "output" / "settlement-report.csv").open(
            encoding="utf-8", newline=""
        ) as fh:
            rows = {r["payment_id"]: r for r in csv.DictReader(fh)}
        self.assertEqual(
            {pid: r["disposition"] for pid, r in rows.items()},
            {
                "PAY-2026-0001": "matched",
                "PAY-2026-0002": "matched",
                "PAY-2026-0003": "broken",
                "PAY-2026-0004": "rejected",
                "PAY-2026-0005": "duplicate",
            },
        )
        self.assertEqual(rows["PAY-2026-0003"]["owner_team"], "nostro-ops")
        self.assertEqual(rows["PAY-2026-0004"]["reason_code"], "CURRENCY_NOT_ALLOWED")
        self.assertEqual(rows["PAY-2026-0004"]["base_amount"], "")
        self.assertEqual(rows["PAY-2026-0002"]["effective_value_date"], "2026-03-17")
        self.assertEqual(rows["PAY-2026-0001"]["base_amount"], "9290.00")
        self.assertEqual(rows["PAY-2026-0003"]["base_amount"], "1085.15")

    def test_archive_manifest_and_stage_record(self) -> None:
        self._run()
        manifest = common.read_json(self.run_dir / "output" / "archive-manifest.json")
        paths = [a["path"] for a in manifest["artifacts"]]
        self.assertNotIn("output/archive-manifest.json", paths)
        self.assertIn("output/settlement-report.csv", paths)
        self.assertEqual(manifest["runId"], "run-42")
        stage = common.read_json(self.run_dir / "stages" / "reporting.json")
        self.assertEqual(stage["counts"], EXPECTED_COUNTS)
        self.assertEqual(stage["scenario"], "reconciliation-breaks")

    def test_identical_inputs_give_identical_outputs(self) -> None:
        other = Path(self._tmp.name) / "elsewhere" / "run-42"
        build_run_dir(other)
        first, _ = self._run()
        second, _ = self._run(other)
        self.assertEqual(first, second)

    def test_count_violation_raises_before_writing(self) -> None:
        _write_jsonl(self.run_dir / "stages" / "matched.jsonl", [])
        with self.assertRaises(common.BusinessRuleError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.code, "COUNT_EQUATION_VIOLATION")
        self.assertFalse((self.run_dir / "output").exists())


class TestMain(_RunDirCase):
    def _argv(self, **overrides: str) -> list[str]:
        opts = {
            "run-dir": str(self.run_dir),
            "scenario": "reconciliation-breaks",
            "task": "produce_settlement_report",
            "kit-root": str(KIT_ROOT),
            "now": NOW,
        }
        opts.update(overrides)
        argv: list[str] = []
        for key, value in opts.items():
            if value:
                argv += ["--" + key, value]
        return argv

    def _main(self, argv: list[str]) -> tuple[int, dict[str, Any], str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        doc: dict[str, Any] = json.loads(out.getvalue())
        return code, doc, err.getvalue()

    def _assert_failure(self, argv: list[str], code: int, err_code: str) -> dict[str, Any]:
        exit_code, doc, _ = self._main(argv)
        self.assertEqual(exit_code, code)
        self.assertEqual(doc["exitCode"], code)
        self.assertEqual(doc["status"], "failed")
        self.assertEqual(doc["error"]["code"], err_code)
        self.assertEqual((doc["counts"], doc["artifacts"], doc["metrics"]), ({}, [], {}))
        self.assertEqual(doc["repository"], "settlement-reporting")
        return doc

    def test_success_envelope(self) -> None:
        code, doc, stderr = self._main(self._argv(**{"run-id": "explicit", "attempt": "2"}))
        self.assertEqual(code, 0)
        self.assertEqual(doc["exitCode"], 0)
        self.assertEqual(doc["status"], "success")
        self.assertEqual(doc["task"], "produce_settlement_report")
        self.assertEqual(doc["runId"], "explicit")
        self.assertEqual(doc["attempt"], 2)
        self.assertEqual((doc["startedAt"], doc["completedAt"]), (NOW, NOW))
        self.assertEqual(doc["counts"], EXPECTED_COUNTS)
        self.assertNotIn("error", doc)
        self.assertIn("settlement>> report written", stderr)

    def test_defaults_from_env_and_scenario(self) -> None:
        env = {"PAYOPS_KIT_ROOT": str(KIT_ROOT), "PAYOPS_ATTEMPT": "3"}
        with mock.patch.dict(os.environ, env):
            code, doc, _ = self._main(self._argv(**{"kit-root": "", "now": ""}))
        self.assertEqual(code, 0)
        self.assertEqual(doc["runId"], "run-42")
        self.assertEqual(doc["attempt"], 3)
        scenario = common.read_json(
            KIT_ROOT / "fixtures" / "scenarios" / "reconciliation-breaks" / "scenario.json"
        )
        self.assertEqual(doc["startedAt"], scenario["clock"]["fixedUtc"])

    def test_help_writes_usage_to_stderr_only(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), cli.USAGE)

    def test_missing_required_is_usage(self) -> None:
        doc = self._assert_failure(self._argv(scenario=""), 2, "USAGE")
        self.assertIn("missing required", doc["error"]["message"])
        self.assertEqual((doc["runId"], doc["scenario"], doc["startedAt"]), ("", "", ""))

    def test_run_dir_not_a_directory_is_usage(self) -> None:
        missing = str(self.run_dir / "nope")
        doc = self._assert_failure(self._argv(**{"run-dir": missing}), 2, "USAGE")
        self.assertIn("run-dir is not a directory", doc["error"]["message"])

    def test_unowned_task_is_usage(self) -> None:
        doc = self._assert_failure(self._argv(task="classify_breaks"), 2, "USAGE")
        self.assertEqual(doc["task"], "classify_breaks")
        self.assertIn("task not owned", doc["error"]["message"])

    def test_bad_kit_root_is_usage(self) -> None:
        doc = self._assert_failure(self._argv(**{"kit-root": str(self.run_dir)}), 2, "USAGE")
        self.assertIn("no fixtures/", doc["error"]["message"])

    def test_unknown_scenario_is_usage(self) -> None:
        doc = self._assert_failure(self._argv(scenario="ghost"), 2, "USAGE")
        self.assertEqual(doc["error"]["message"], "scenario not found: ghost")
        self.assertEqual(doc["runId"], "")

    def test_missing_ledger_is_missing_upstream(self) -> None:
        (self.run_dir / "ledger" / "ledger.sqlite3").unlink()
        doc = self._assert_failure(self._argv(), 5, "MISSING_UPSTREAM_ARTIFACT")
        self.assertIn("ledger not found", doc["error"]["message"])
        self.assertEqual(doc["runId"], "run-42")
        self.assertEqual(doc["startedAt"], NOW)

    def test_count_violation_is_business_rule(self) -> None:
        _write_jsonl(self.run_dir / "stages" / "breaks.jsonl", [])
        doc = self._assert_failure(self._argv(), 4, "COUNT_EQUATION_VIOLATION")
        self.assertIn("broken=0", doc["error"]["message"])

    def test_unexpected_error_is_internal(self) -> None:
        path = self.run_dir / "stages" / "classified-breaks.jsonl"
        path.write_text("{broken\n", encoding="utf-8")
        doc = self._assert_failure(self._argv(), 1, "INTERNAL")
        self.assertEqual(doc["error"]["message"], "unexpected error")

    def test_unreachable_task_branch_is_usage(self) -> None:
        owned = {"produce_settlement_report", "other_task"}
        with mock.patch.object(common, "OWNED_TASKS", owned):
            doc = self._assert_failure(self._argv(task="other_task"), 2, "USAGE")
        self.assertEqual(doc["error"]["message"], "task not owned: other_task")
        self.assertEqual(doc["runId"], "run-42")

    def test_repo_root_from_env(self) -> None:
        repo_root = KIT_ROOT / "repos" / "settlement-reporting"
        with mock.patch.dict(os.environ, {"PAYOPS_REPO_ROOT": str(repo_root)}):
            self.assertEqual(cli._repo_root(), repo_root.resolve())
        self.assertEqual(cli._repo_root(), repo_root.resolve())


if __name__ == "__main__":
    unittest.main()
