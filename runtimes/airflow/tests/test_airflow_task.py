"""One task try: run values, retry classification, evidence, and the failure mail.

Needs the Airflow venv: scripts/test-airflow-equivalence.sh structure. The
end-to-end behaviour is checked against the real scheduler by the retry,
rerun, and no-retry checks; these tests pin the rules those runs rely on.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import pendulum
from airflow.sdk.exceptions import AirflowException, AirflowFailException
from test_airflow_dag import KIT_ROOT, load_dag_module

DEFAULTS = {"scenario": "happy-path", "run_id": "", "kit_root": "", "fixed_clock": "", "pass": 1}
LEDGER_FAULT = {"task": "post_pending_ledger", "attempt": 1, "exitCode": 3}


def dag_run(utc: str) -> SimpleNamespace:
    return SimpleNamespace(logical_date=pendulum.parse(utc), run_after=pendulum.parse(utc))


def record(task: str, attempt: int, exit_code: int, parsed: bool = True) -> dict[str, Any]:
    return {"task": task, "attempt": attempt, "exitCode": exit_code, "resultParsed": parsed}


class TestResolveRun(unittest.TestCase):
    module: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dag_module()

    def resolve(self, **params: Any) -> Any:
        return self.module.resolve_run(
            {**DEFAULTS, **params},
            dag_run("2026-03-16T12:00:00Z"),
            self.module.WORKFLOW.variables,
        )

    def test_scheduled_run_defaults(self) -> None:
        run = self.resolve()
        self.assertEqual(run.kit_root, KIT_ROOT)
        self.assertEqual(run.scenario, "happy-path")
        self.assertEqual(run.run_id, "scheduled-2026-03-16")
        self.assertEqual(run.fixed_clock, "2026-03-16T09:30:00Z")
        self.assertEqual(run.pass_number, 1)
        self.assertIsNone(run.fault)
        self.assertEqual(run.run_dir, KIT_ROOT / "workspace" / "runtime" / "runs" / run.run_id)

    def test_order_date_is_the_singapore_date(self) -> None:
        late = self.module.order_date(dag_run("2026-03-16T16:30:00Z"))
        self.assertEqual(late, "2026-03-17")

    def test_scenario_supplies_clock_and_fault(self) -> None:
        run = self.resolve(scenario="partial-ledger-write", run_id="af-1", **{"pass": 2})
        self.assertEqual(run.fixed_clock, "2026-03-16T10:00:00Z")
        self.assertEqual(run.fault["task"], "post_pending_ledger")
        self.assertEqual(run.run_id, "af-1")
        self.assertEqual(run.pass_number, 2)

    def test_explicit_clock_and_kit_root_win(self) -> None:
        run = self.resolve(fixed_clock="2026-01-02T00:00:00Z", kit_root="/opt/kit")
        self.assertEqual(run.fixed_clock, "2026-01-02T00:00:00Z")
        self.assertEqual(run.kit_root, Path("/opt/kit"))

    def test_unknown_scenario_has_no_fault_and_the_default_clock(self) -> None:
        run = self.resolve(scenario="no-such-scenario")
        self.assertIsNone(run.fault)
        self.assertEqual(run.fixed_clock, "2026-03-16T09:30:00Z")

    def test_unsafe_run_ids_are_refused(self) -> None:
        for run_id in ("../escape", "a/b", ".hidden", "with space", "trailing\n"):
            with self.subTest(run_id=run_id), self.assertRaises(AirflowFailException):
                self.resolve(run_id=run_id)

    def test_unsafe_scenarios_are_refused(self) -> None:
        for scenario in ("../../escape", "a/b", ".hidden", "with space", "trailing\n"):
            with self.subTest(scenario=scenario), self.assertRaises(AirflowFailException):
                self.resolve(scenario=scenario)


class TestClassify(unittest.TestCase):
    module: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dag_module()

    def test_success_needs_exit_0_and_a_parsed_result(self) -> None:
        self.assertEqual(self.module.classify(record("classify_breaks", 1, 0), None), "success")
        unparsed = record("classify_breaks", 1, 0, parsed=False)
        self.assertEqual(self.module.classify(unparsed, None), "fail")

    def test_only_the_declared_fault_retries(self) -> None:
        classify = self.module.classify
        self.assertEqual(classify(record("post_pending_ledger", 1, 3), LEDGER_FAULT), "retry")
        cases = [
            (record("post_pending_ledger", 2, 3), LEDGER_FAULT),
            (record("post_pending_ledger", 1, 3), None),
            (record("enrich_fx_rates", 1, 3), LEDGER_FAULT),
            (record("post_pending_ledger", 1, 1), LEDGER_FAULT),
            (record("post_pending_ledger", 1, 4), LEDGER_FAULT),
            (record("post_pending_ledger", 1, 5), LEDGER_FAULT),
            (record("post_pending_ledger", 1, 2), LEDGER_FAULT),
            (record("post_pending_ledger", 1, 99), LEDGER_FAULT),
            (record("watch_inbound_files", 1, 2), None),
            (record("post_pending_ledger", 1, 0, parsed=False), LEDGER_FAULT),
        ]
        for case, fault in cases:
            with self.subTest(case=case, fault=fault):
                self.assertEqual(classify(case, fault), "fail")


class TestRunPayopsTask(unittest.TestCase):
    module: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dag_module()

    def run_task(self, try_number: int, exit_code: int, scenario: str) -> mock.MagicMock:
        context = {
            "ti": SimpleNamespace(try_number=try_number),
            "params": {**DEFAULTS, "scenario": scenario, "run_id": "unit-run"},
            "dag_run": dag_run("2026-03-16T12:00:00Z"),
        }
        result = {**record("post_pending_ledger", try_number, exit_code), "counts": {}}
        result["logs"] = {"stderr": "logs/x.stderr"}
        attempt = mock.MagicMock(return_value=result)
        with (
            mock.patch.object(self.module, "get_current_context", return_value=context),
            mock.patch.object(self.module, "run_attempt", attempt),
        ):
            self.module.run_payops_task("post_pending_ledger")
        return attempt

    def test_try_number_is_the_attempt(self) -> None:
        attempt = self.run_task(2, 0, "partial-ledger-write")
        argv = attempt.call_args.args[2]
        self.assertEqual(argv[argv.index("--attempt") + 1], "2")
        self.assertEqual(attempt.call_args.args[4], 2)

    def test_declared_fault_raises_a_retryable_error(self) -> None:
        with self.assertRaises(AirflowException) as raised:
            self.run_task(1, 3, "partial-ledger-write")
        self.assertNotIsInstance(raised.exception, AirflowFailException)

    def test_other_failures_raise_fail_without_retry(self) -> None:
        for scenario, exit_code in (("happy-path", 3), ("partial-ledger-write", 4)):
            with self.subTest(exit_code=exit_code), self.assertRaises(AirflowFailException):
                self.run_task(1, exit_code, scenario)

    def test_unexpected_errors_do_not_retry(self) -> None:
        context = {
            "ti": SimpleNamespace(try_number=1),
            "params": DEFAULTS,
            "dag_run": dag_run("2026-03-16T12:00:00Z"),
        }
        broken = mock.MagicMock(side_effect=OSError("disk full"))
        with (
            mock.patch.object(self.module, "get_current_context", return_value=context),
            mock.patch.object(self.module, "run_attempt", broken),
            self.assertRaises(AirflowFailException),
        ):
            self.module.run_payops_task("classify_breaks")


class TestRunAttempt(unittest.TestCase):
    module: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dag_module()

    def test_attempt_writes_logs_and_one_evidence_line(self) -> None:
        output = {"status": "ok", "counts": {"posted": 3}, "artifacts": ["ledger/x.db"]}
        script = (
            f"import sys; print({json.dumps(json.dumps(output))}); print('diag', file=sys.stderr)"
        )
        with tempfile.TemporaryDirectory() as temp:
            run = self.module.RunValues(
                kit_root=Path(temp),
                scenario="happy-path",
                run_id="unit-run",
                fixed_clock="2026-03-16T09:30:00Z",
                pass_number=2,
                fault=None,
            )
            argv = [sys.executable, "-c", script]
            result = self.module.run_attempt("post_pending_ledger", "repo", argv, run, 1)
            logs = run.run_dir / "logs"
            self.assertEqual(
                sorted(p.name for p in run.run_dir.iterdir()), sorted(self.module.RUN_SUBDIRS)
            )
            self.assertEqual(
                json.loads((logs / "post_pending_ledger.pass2.attempt1.stdout").read_text()), output
            )
            self.assertEqual(
                (logs / "post_pending_ledger.pass2.attempt1.stderr").read_text(), "diag\n"
            )
            (line,) = (logs / "airflow-attempts.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(line), result)
        self.assertEqual(result["counts"], {"posted": 3})
        self.assertEqual((result["pass"], result["attempt"], result["exitCode"]), (2, 1, 0))
        self.assertTrue(result["resultParsed"])

    def test_unparseable_output_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = self.module.RunValues(Path(temp), "happy-path", "r", "c", 1, None)
            argv = [sys.executable, "-c", "print('not json'); raise SystemExit(4)"]
            result = self.module.run_attempt("classify_breaks", "repo", argv, run, 1)
        self.assertFalse(result["resultParsed"])
        self.assertEqual((result["exitCode"], result["status"]), (4, "failed"))


class TestFailureNotification(unittest.TestCase):
    module: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dag_module()

    def test_message_is_the_control_m_mail_text(self) -> None:
        params = {**DEFAULTS, "scenario": "duplicate-retry", "run_id": "af-dup"}
        message = self.module.failure_message(
            "classify_breaks", params, dag_run("2026-03-16T12:00:00Z")
        )
        self.assertEqual(message, "classify_breaks failed for run af-dup scenario duplicate-retry")

    def test_scheduled_run_message_uses_the_default_run_id(self) -> None:
        message = self.module.failure_message(
            "watch_inbound_files", DEFAULTS, dag_run("2026-03-16T12:00:00Z")
        )
        self.assertIn("run scheduled-2026-03-16 scenario happy-path", message)

    def test_callback_logs_and_sends_nothing(self) -> None:
        context = {
            "ti": SimpleNamespace(task_id="enrich_fx_rates"),
            "params": {**DEFAULTS, "run_id": "af-x"},
            "dag_run": dag_run("2026-03-16T12:00:00Z"),
        }
        with (
            mock.patch("smtplib.SMTP") as smtp,
            self.assertLogs("airflow.task", level="ERROR") as logs,
        ):
            self.module.notify_failure(context)
        smtp.assert_not_called()
        (line,) = logs.output
        self.assertIn("logged only, not sent: to=payments-operations@payops.invalid", line)
        self.assertIn("enrich_fx_rates failed for run af-x scenario happy-path", line)


if __name__ == "__main__":
    unittest.main()
