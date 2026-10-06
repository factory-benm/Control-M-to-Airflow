"""The scenario runner's bookkeeping, without a running Airflow.

Standard library only: PYTHONPATH=runtimes/airflow:runtimes/control-m/harness.
The end-to-end manifest is graded by verify_run.py in the oracle check.
"""

from __future__ import annotations

import os
import signal
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import runner
from harness import Harness

KIT_ROOT = Path(__file__).resolve().parents[3]
ORDER = [
    "watch_inbound_files",
    "verify_file_integrity",
    "extract_payment_batch",
    "validate_payment_schema",
    "deduplicate_payments",
    "enrich_fx_rates",
    "apply_business_day_cutoff",
    "post_pending_ledger",
    "reconcile_nostro_ledger",
    "classify_breaks",
    "produce_settlement_report",
    "archive_and_notify",
]


def harness_for(scenario: str) -> Harness:
    return Harness(
        kit_root=KIT_ROOT,
        repos_root=KIT_ROOT / "repos",
        run_root=KIT_ROOT / "workspace" / "runtime" / "runs",
        scenario_id=scenario,
        run_id="unit-runner",
    )


def attempt(task: str, pass_number: int, number: int, exit_code: int) -> dict[str, Any]:
    return {
        "task": task,
        "pass": pass_number,
        "attempt": number,
        "exitCode": exit_code,
        "counts": {"posted": 4} if exit_code == 0 else {"posted": 99},
    }


def dag_run(pass_number: int, state: str, tries: dict[str, int]) -> dict[str, Any]:
    return {
        "pass": pass_number,
        "dagRunId": f"r__pass{pass_number}",
        "state": state,
        "runType": "manual",
        "tasks": {task: {"tries": [{}] * count} for task, count in tries.items()},
    }


class FakeClient:
    def __init__(self, lock_path: Path, paused: bool) -> None:
        self.lock_path = lock_path
        self.paused = paused
        self.calls: list[bool] = []

    def is_paused(self) -> bool:
        return self.paused

    def set_paused(self, paused: bool) -> None:
        self.calls.append(paused)
        self.paused = paused


@contextmanager
def fake_client(paused: bool) -> Iterator[FakeClient]:
    with tempfile.TemporaryDirectory() as temp:
        yield FakeClient(Path(temp) / "run" / "runner.lock", paused)


@contextmanager
def installed_client() -> Iterator[tuple[Path, runner.AirflowClient]]:
    """An AirflowClient over an empty stand-in for the Airflow venv."""
    with tempfile.TemporaryDirectory() as temp:
        home = Path(temp)
        (home / "venv" / "bin").mkdir(parents=True)
        (home / "venv" / "bin" / "airflow").write_text("", encoding="utf-8")
        yield home, runner.AirflowClient(home, KIT_ROOT, "DAG")


class TestPasses(unittest.TestCase):
    def test_only_a_declared_rerun_without_a_fault_runs_twice(self) -> None:
        expected = {
            "business-cutoff": 1,
            "duplicate-retry": 2,
            "happy-path": 1,
            "partial-ledger-write": 1,
            "reconciliation-breaks": 1,
        }
        for scenario, passes in expected.items():
            with self.subTest(scenario=scenario):
                self.assertEqual(runner.wanted_passes(harness_for(scenario)), passes)

    def test_runtime_metadata_says_airflow_not_control_m(self) -> None:
        metadata = runner.runtime_metadata(harness_for("happy-path"), "3.3.2")
        self.assertEqual(metadata["mode"], "airflow")
        self.assertIs(metadata["isControlM"], False)
        self.assertEqual(metadata["airflowVersion"], "3.3.2")
        self.assertEqual(metadata["graph"], {"tasks": 12, "edges": 11})
        self.assertIn("not BMC Control-M", metadata["disclosure"])

    def test_records_fill_exit_sequence_passes_and_successful_counts(self) -> None:
        harness = harness_for("partial-ledger-write")
        records = [
            attempt("post_pending_ledger", 1, 1, 3),
            attempt("post_pending_ledger", 1, 2, 0),
            attempt("watch_inbound_files", 2, 1, 0),
        ]
        runner.apply_records(harness, records)
        self.assertEqual(
            harness.exit_sequence,
            [
                {"task": "post_pending_ledger", "attempt": 1, "exitCode": 3},
                {"task": "post_pending_ledger", "attempt": 2, "exitCode": 0},
                {"task": "watch_inbound_files", "attempt": 1, "exitCode": 0},
            ],
        )
        self.assertEqual([item["pass"] for item in harness.passes], [1, 2])
        self.assertEqual(len(harness.passes[0]["tasks"]), 2)
        self.assertEqual(harness.counts["posted"], 4)


class TestRunStatus(unittest.TestCase):
    def test_tries_must_match_recorded_attempts(self) -> None:
        records = [attempt("post_pending_ledger", 1, 1, 3), attempt("post_pending_ledger", 1, 2, 0)]
        self.assertEqual(
            runner.tries_problems(records, [dag_run(1, "success", {"post_pending_ledger": 2})]), []
        )
        problems = runner.tries_problems(
            records, [dag_run(1, "success", {"post_pending_ledger": 3, "classify_breaks": 1})]
        )
        self.assertEqual(len(problems), 2)
        self.assertIn("Airflow ran 3 tries, tasks recorded 2 attempts", problems[1])

    def test_status_reports_failed_runs_missing_passes_and_problems(self) -> None:
        happy, rerun = harness_for("happy-path"), harness_for("duplicate-retry")
        good = dag_run(1, "success", {})
        self.assertIsNone(runner.run_status(happy, [good], []))
        self.assertIsNone(runner.run_status(rerun, [good, dag_run(2, "success", {})], []))
        self.assertEqual(
            runner.run_status(happy, [dag_run(1, "failed", {})], []),
            "DAG run r__pass1 ended failed",
        )
        self.assertEqual(runner.run_status(rerun, [good], []), "expected 2 DAG runs, completed 1")
        self.assertEqual(runner.run_status(happy, [good], ["a", "b"]), "a; b")


class TestDagUnpaused(unittest.TestCase):
    def test_paused_dag_is_unpaused_then_paused_again(self) -> None:
        with fake_client(paused=True) as client:
            with runner.dag_unpaused(client):
                self.assertFalse(client.paused)
            self.assertEqual(client.calls, [False, True])

    def test_pause_is_restored_after_an_error(self) -> None:
        with fake_client(paused=True) as client:
            with self.assertRaises(RuntimeError), runner.dag_unpaused(client):
                raise RuntimeError("boom")
            self.assertTrue(client.paused)

    def test_pause_is_restored_on_sigterm(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        with fake_client(paused=True) as client:
            with self.assertRaises(SystemExit) as stopped, runner.dag_unpaused(client):
                os.kill(os.getpid(), signal.SIGTERM)
            self.assertEqual(stopped.exception.code, 128 + signal.SIGTERM)
            self.assertTrue(client.paused)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_an_unpaused_dag_is_left_alone(self) -> None:
        with fake_client(paused=False) as client:
            with runner.dag_unpaused(client):
                pass
            self.assertEqual(client.calls, [])


class TestClientAndCommandLine(unittest.TestCase):
    def test_missing_installation_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(runner.RunnerError):
            runner.AirflowClient(Path(temp), KIT_ROOT, "DAG")

    def test_version_comes_from_the_installed_distribution(self) -> None:
        with installed_client() as (home, client):
            self.assertEqual(client.version(), "unknown")
            site = home / "venv" / "lib" / "python3.12" / "site-packages"
            (site / "apache_airflow-3.3.2.dist-info").mkdir(parents=True)
            self.assertEqual(client.version(), "3.3.2")

    def test_failed_cli_call_raises_with_the_error_tail(self) -> None:
        with installed_client() as (_, client), self.assertRaises(runner.RunnerError) as raised:
            client._call(["sh", "-c", "echo nope >&2; exit 3"])
        self.assertIn("nope", str(raised.exception))

    def test_runs_wait_for_the_dag_file_on_disk_to_be_parsed(self) -> None:
        stale = {"registered": True, "parsedAfterLastEdit": False, "importErrors": []}
        current = {**stale, "parsedAfterLastEdit": True}
        broken = {**stale, "importErrors": [{"filename": "dag.py", "stacktrace": "x"}]}
        with installed_client() as (_, client), mock.patch("time.sleep"):
            with mock.patch.object(client, "_state", side_effect=[stale, stale, current]) as state:
                client.wait_until_current()
            self.assertEqual(state.call_count, 3)
            with (
                mock.patch.object(client, "_state", side_effect=[broken]),
                self.assertRaisesRegex(runner.RunnerError, "import errors"),
            ):
                client.wait_until_current()
            with (
                mock.patch.object(client, "_state", return_value=stale),
                self.assertRaisesRegex(runner.RunnerError, "not parsed"),
            ):
                client.wait_until_current(timeout=0)

    def test_without_the_airflow_environment_the_runner_is_unavailable(self) -> None:
        with mock.patch.dict(os.environ, {"PAYOPS_AIRFLOW_HOME": "", "AIRFLOW_HOME": ""}):
            self.assertEqual(runner.main(["run", "happy-path"]), runner.EXIT_UNAVAILABLE)

    def test_trigger_conf_must_be_an_object(self) -> None:
        arguments = runner.parse_arguments(["trigger", "--conf", "[1]"])
        with self.assertRaises(runner.RunnerError):
            runner.command_trigger(arguments, mock.MagicMock())

    def test_dag_run_ids_name_the_run_and_pass(self) -> None:
        dag_run_id = runner.dag_run_id_for("af-happy", 2)
        self.assertRegex(dag_run_id, r"^af-happy__pass2__\d{8}T\d{12}Z$")


if __name__ == "__main__":
    unittest.main()
