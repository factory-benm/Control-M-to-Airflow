"""Tests for the payment-validation CLI exit codes and stdout contract."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payment_validation import cli
from payment_validation.common import BusinessRuleError

KIT_ROOT = Path(__file__).resolve().parents[3]
NOW = "2026-03-16T09:30:00Z"
PAYOPS_ENV = ("PAYOPS_KIT_ROOT", "PAYOPS_FIXED_CLOCK", "PAYOPS_ATTEMPT")

GOOD_ROW = {
    "payment_id": "PAY-2026-0001",
    "source_system": "CHANNEL-SG",
    "value_date": "2026-03-16",
    "booking_timestamp": "2026-03-16T01:15:00Z",
    "debtor_account_token": "DEBTOR-0001",
    "creditor_account_token": "CREDITOR-0101",
    "currency": "SGD",
    "amount": "12500.00",
    "ledger_reference": "LEDG-SG-0001",
    "country_code": "SG",
    "line_number": 1,
}


class TestMain(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / "run-xyz"
        self.run_dir.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in PAYOPS_ENV}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _argv(self, task: str = "validate_payment_schema", **extra: str) -> list[str]:
        argv = [
            "--run-dir",
            str(self.run_dir),
            "--scenario",
            "happy-path",
            "--task",
            task,
            "--kit-root",
            str(KIT_ROOT),
        ]
        for key, value in extra.items():
            argv += ["--" + key.replace("_", "-"), value]
        return argv

    def _main(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with self.assertLogs("payment-validation", level="INFO"), redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def _single_json(self, stdout: str) -> dict[str, Any]:
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 1, stdout)
        doc: dict[str, Any] = json.loads(lines[0])
        return doc

    def _write_normalized(self, text: str) -> None:
        stages = self.run_dir / "stages"
        stages.mkdir()
        (stages / "normalized-payments.jsonl").write_text(text, encoding="utf-8")

    def test_success_emits_single_result(self) -> None:
        self._write_normalized(json.dumps(GOOD_ROW) + "\n")
        code, stdout = self._main(self._argv(now=NOW, run_id="run-7"))
        self.assertEqual(code, 0)
        result = self._single_json(stdout)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["runId"], "run-7")
        self.assertEqual(result["startedAt"], NOW)
        self.assertEqual(result["completedAt"], NOW)
        self.assertEqual(result["counts"], {"received": 1, "rejected": 0})
        self.assertEqual(result["attempt"], 1)

    def test_defaults_from_scenario_run_dir_and_env(self) -> None:
        self._write_normalized(json.dumps(GOOD_ROW) + "\n")
        with mock.patch.dict(os.environ, {"PAYOPS_ATTEMPT": "3"}):
            code, stdout = self._main(self._argv())
        self.assertEqual(code, 0)
        result = self._single_json(stdout)
        self.assertEqual(result["runId"], "run-xyz")
        self.assertEqual(result["startedAt"], "2026-03-16T09:30:00Z")
        self.assertEqual(result["attempt"], 3)

    def test_env_clock_and_kit_root(self) -> None:
        self._write_normalized(json.dumps(GOOD_ROW) + "\n")
        argv = self._argv()[:-2]
        env = {"PAYOPS_KIT_ROOT": str(KIT_ROOT), "PAYOPS_FIXED_CLOCK": "2026-03-17T00:00:00Z"}
        with mock.patch.dict(os.environ, env):
            code, stdout = self._main(argv)
        self.assertEqual(code, 0)
        self.assertEqual(self._single_json(stdout)["startedAt"], "2026-03-17T00:00:00Z")

    def test_deduplicate_task_dispatched(self) -> None:
        stages = self.run_dir / "stages"
        stages.mkdir()
        rows = [dict(GOOD_ROW), dict(GOOD_ROW, line_number=2)]
        (stages / "validated-payments.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        code, stdout = self._main(self._argv("deduplicate_payments"))
        self.assertEqual(code, 0)
        result = self._single_json(stdout)
        self.assertEqual(result["counts"], {"accepted": 1, "duplicatesRemoved": 1})

    def test_help_prints_usage_and_succeeds(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), cli.USAGE)

    def _assert_error(
        self, argv: list[str], exit_code: int, err_code: str, message_part: str
    ) -> dict[str, Any]:
        code, stdout = self._main(argv)
        self.assertEqual(code, exit_code)
        result = self._single_json(stdout)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exitCode"], exit_code)
        self.assertEqual(result["error"]["code"], err_code)
        self.assertIn(message_part, result["error"]["message"])
        self.assertEqual(result["repository"], "payment-validation")
        self.assertEqual((result["counts"], result["artifacts"]), ({}, []))
        return result

    def test_missing_required_args_is_usage(self) -> None:
        result = self._assert_error(["--task", "validate_payment_schema"], 2, "USAGE", "required")
        self.assertEqual(result["task"], "validate_payment_schema")
        self.assertIsNone(result["scenario"])
        self.assertIsNone(result["startedAt"])

    def test_unknown_task_is_usage(self) -> None:
        result = self._assert_error(
            self._argv("classify_breaks", run_id="r9"), 2, "USAGE", "unknown task"
        )
        self.assertEqual(result["task"], "classify_breaks")
        self.assertEqual(result["scenario"], "happy-path")
        self.assertEqual(result["runId"], "r9")

    def test_unknown_argument_is_usage(self) -> None:
        self._assert_error([*self._argv(), "--bogus", "1"], 2, "USAGE", "unknown argument")

    def test_bad_kit_root_is_usage(self) -> None:
        argv = [*self._argv()[:-1], str(self.run_dir)]
        self._assert_error(argv, 2, "USAGE", "--kit-root")

    def test_unknown_scenario_is_missing_upstream(self) -> None:
        argv = self._argv()
        argv[3] = "no-such-scenario"
        self._assert_error(argv, 5, "MISSING_UPSTREAM", "scenario not found")

    def test_missing_stage_input_is_missing_upstream(self) -> None:
        result = self._assert_error(self._argv(), 5, "MISSING_UPSTREAM", "normalized-payments")
        self.assertEqual(result["startedAt"], "2026-03-16T09:30:00Z")

    def test_corrupt_input_is_internal(self) -> None:
        self._write_normalized("{not json\n")
        self._assert_error(self._argv(), 1, "INTERNAL", "")

    def test_business_rule_violation(self) -> None:
        def violating(**_kwargs: Any) -> dict[str, Any]:
            raise BusinessRuleError("rule broken")

        tasks = {"validate_payment_schema": violating}
        with mock.patch.object(cli, "_load_tasks", return_value=tasks):
            self._assert_error(self._argv(), 4, "BUSINESS_RULE", "rule broken")


if __name__ == "__main__":
    unittest.main()
