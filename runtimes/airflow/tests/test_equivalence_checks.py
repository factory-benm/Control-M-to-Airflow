"""The equivalence assertions report the defects they exist to catch.

Standard library only: PYTHONPATH=runtimes/airflow. Each test feeds a known
good observation and one or more broken ones, so a check that silently passes
everything fails here.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import equivalence_checks as checks

KIT_ROOT = Path(__file__).resolve().parents[3]
ORDER = checks.task_order(KIT_ROOT)
RETRIED = "post_pending_ledger"
MAIL = {
    "To": "ops@payops.invalid",
    "Message": "%%JOBNAME failed for run %%RUN_ID scenario %%SCENARIO",
}


def tries(count: int, gap_seconds: int = 61) -> list[dict[str, Any]]:
    """Airflow-style tries lasting 10 s each, gap_seconds apart."""
    made = []
    start = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
    for number in range(1, count + 1):
        end = start + timedelta(seconds=10)
        made.append({"tryNumber": number, "start": start.isoformat(), "end": end.isoformat()})
        start = end + timedelta(seconds=gap_seconds)
    return made


def dag_run(
    retried: str | None = None, state: str = "success", run_id: str = "r1"
) -> dict[str, Any]:
    tasks = {}
    for task in ORDER:
        count = 2 if task == retried else 1
        tasks[task] = {"state": "success", "tryNumber": count, "tries": tries(count)}
    return {"dagRunId": run_id, "state": state, "runType": "manual", "tasks": tasks}


def write_manifest(run_dir: Path, **fields: Any) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run-manifest.json").write_text(json.dumps(fields), encoding="utf-8")


class TestOwedSet(unittest.TestCase):
    def test_a_scenario_without_an_oracle_stays_owed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            scenarios = Path(temp) / "fixtures" / "scenarios"
            (scenarios / "graded" / "expected").mkdir(parents=True)
            (scenarios / "graded" / "expected" / "manifest.json").write_text("{}", "utf-8")
            (scenarios / "ungraded").mkdir()
            (scenarios / "README.md").write_text("not a scenario", "utf-8")
            self.assertEqual(checks.owed_scenarios(Path(temp)), ["graded", "ungraded"])

    def test_owed_scenarios_are_the_five_fixture_directories(self) -> None:
        self.assertEqual(
            checks.owed_scenarios(KIT_ROOT),
            [
                "business-cutoff",
                "duplicate-retry",
                "happy-path",
                "partial-ledger-write",
                "reconciliation-breaks",
            ],
        )

    def test_task_order_is_the_twelve_jobs(self) -> None:
        self.assertEqual(len(ORDER), 12)
        self.assertEqual((ORDER[0], ORDER[-1]), ("watch_inbound_files", "archive_and_notify"))


class TestRetryChecks(unittest.TestCase):
    def sequence(self, retried: list[tuple[int, int]]) -> dict[str, Any]:
        entries = []
        for task in ORDER:
            pairs = retried if task == RETRIED else [(1, 0)]
            entries += [{"task": task, "attempt": a, "exitCode": c} for a, c in pairs]
        return {"exitSequence": entries}

    def test_exit_sequence(self) -> None:
        self.assertEqual(checks.exit_sequence_problems(self.sequence([(1, 3), (2, 0)]), ORDER), [])
        for broken in ([(1, 0)], [(1, 3), (2, 3), (3, 0)], [(1, 4)]):
            with self.subTest(broken=broken):
                self.assertTrue(checks.exit_sequence_problems(self.sequence(broken), ORDER))
        extra = self.sequence([(1, 3), (2, 0)])
        extra["exitSequence"].append({"task": "classify_breaks", "attempt": 2, "exitCode": 0})
        self.assertTrue(checks.exit_sequence_problems(extra, ORDER))

    def test_airflow_tries(self) -> None:
        self.assertEqual(checks.tries_problems(dag_run(RETRIED), ORDER, RETRIED), [])
        self.assertEqual(checks.tries_problems(dag_run(), ORDER, None), [])
        self.assertTrue(checks.tries_problems(dag_run(), ORDER, RETRIED))
        self.assertTrue(checks.tries_problems(dag_run("classify_breaks"), ORDER, None))
        missing = dag_run()
        del missing["tasks"]["archive_and_notify"]
        self.assertTrue(checks.tries_problems(missing, ORDER, None))

    def test_retry_gap_is_at_least_sixty_seconds(self) -> None:
        info = dag_run(RETRIED)
        self.assertEqual(checks.retry_gap_problems(info), [])
        info["tasks"][RETRIED]["tries"] = tries(2, gap_seconds=30)
        self.assertTrue(checks.retry_gap_problems(info))
        info["tasks"][RETRIED]["tries"] = tries(1)
        self.assertTrue(checks.retry_gap_problems(info))

    def test_ledger_rows_and_posting_metrics(self) -> None:
        expected = {
            "partialWrite": {"committedOnFirstAttempt": [1, 2, 3, 4], "writtenOnRetry": [5, 6, 7]}
        }
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "stages").mkdir()
            (run_dir / "ledger").mkdir()
            metrics = {"metrics": {"alreadyPosted": 4, "inserted": 3}}
            (run_dir / "stages" / "ledger-posting.json").write_text(json.dumps(metrics), "utf-8")
            connection = sqlite3.connect(run_dir / "ledger" / "ledger.sqlite3")
            connection.execute("CREATE TABLE ledger_entry (payment_id TEXT)")
            connection.executemany(
                "INSERT INTO ledger_entry VALUES (?)", [(str(i),) for i in range(7)]
            )
            connection.commit()
            self.assertEqual(checks.ledger_problems(run_dir, expected), [])
            connection.execute("INSERT INTO ledger_entry VALUES ('3')")
            connection.commit()
            connection.close()
            self.assertTrue(checks.ledger_problems(run_dir, expected))


class TestRerunCheck(unittest.TestCase):
    def check(self, sequence: list[dict[str, Any]], ledger: list[int], ids: list[str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            write_manifest(
                run_dir,
                scenario="duplicate-retry",
                exitSequence=sequence,
                ledgerRowsAfterPass=ledger,
                airflow={"dagRuns": [{"dagRunId": i} for i in ids]},
            )
            return checks.rerun_problems(KIT_ROOT, run_dir, lambda i: dag_run(run_id=i))

    def test_two_identical_passes(self) -> None:
        one_pass = [{"task": task, "attempt": 1, "exitCode": 0} for task in ORDER]
        self.assertEqual(self.check(one_pass * 2, [6, 6], ["a", "b"]), [])
        self.assertTrue(self.check(one_pass, [6], ["a"]))
        self.assertTrue(self.check(one_pass * 2, [6, 12], ["a", "b"]))
        self.assertTrue(self.check(one_pass * 2, [6, 6], ["a", "a"]))
        self.assertTrue(self.check(one_pass + one_pass[:-1], [6, 6], ["a", "b"]))


class TestNoRetryCheck(unittest.TestCase):
    def failed_run(self) -> dict[str, Any]:
        info = dag_run(state="failed")
        for task in ORDER[1:]:
            info["tasks"][task] = {"state": "upstream_failed", "tryNumber": 0, "tries": []}
        info["tasks"][ORDER[0]]["state"] = "failed"
        return info

    def test_failed_run_states(self) -> None:
        self.assertEqual(checks.failed_run_problems(self.failed_run(), ORDER, ORDER[0]), [])
        retried = self.failed_run()
        retried["tasks"][ORDER[0]].update(tryNumber=2, tries=tries(2))
        self.assertTrue(checks.failed_run_problems(retried, ORDER, ORDER[0]))
        ran_on = self.failed_run()
        ran_on["tasks"][ORDER[1]]["state"] = "success"
        self.assertTrue(checks.failed_run_problems(ran_on, ORDER, ORDER[0]))
        succeeded = self.failed_run()
        succeeded["state"] = "success"
        self.assertTrue(checks.failed_run_problems(succeeded, ORDER, ORDER[0]))

    def test_callback_line(self) -> None:
        good = (
            "ERROR - Control-M ActionIfFailure mail, logged only, not sent: "
            "to=ops@payops.invalid message='watch failed for run r scenario s'"
        )
        self.assertEqual(checks.callback_problems(good, MAIL, "watch", "r", "s"), [])
        self.assertTrue(checks.callback_problems(good, MAIL, "watch", "other", "s"))
        self.assertTrue(
            checks.callback_problems(good.replace("not sent", "sent"), MAIL, "watch", "r", "s")
        )
        self.assertTrue(checks.callback_problems("", MAIL, "watch", "r", "s"))

    def test_attempt_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            self.assertTrue(checks.attempt_exit_problems(run_dir, RETRIED, 1))
            (run_dir / "logs").mkdir()
            records = [
                {"task": ORDER[0], "attempt": 1, "exitCode": 0},
                {"task": RETRIED, "attempt": 1, "exitCode": 1},
            ]
            attempts = run_dir / "logs" / "airflow-attempts.jsonl"
            attempts.write_text("".join(json.dumps(r) + "\n" for r in records), "utf-8")
            self.assertEqual(checks.attempt_exit_problems(run_dir, RETRIED, 1), [])
            self.assertTrue(checks.attempt_exit_problems(run_dir, RETRIED, 4))
            retried = {"task": RETRIED, "attempt": 2, "exitCode": 1}
            with open(attempts, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(retried) + "\n")
            self.assertTrue(checks.attempt_exit_problems(run_dir, RETRIED, 1))

    def test_whole_check_reads_the_task_log(self) -> None:
        arguments = argparse.Namespace(
            dag_run_id="d", failed_task=ORDER[0], run_id="r", scenario="s", exit_code=None
        )
        mail = checks.failure_mail(KIT_ROOT)
        line = f"not sent: to={mail['To']} message='{ORDER[0]} failed for run r scenario s'"
        problems = checks.no_retry_problems(
            KIT_ROOT, arguments, lambda _: self.failed_run(), lambda _run, _task: line
        )
        self.assertEqual(problems, [])


class TestLabels(unittest.TestCase):
    def check(
        self,
        mode: str,
        run_type: str,
        dag_runs: list[dict[str, str]],
        manifest_runtime: dict[str, Any] | None = None,
    ) -> list[str]:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            runtime = {"mode": mode, "isControlM": False}
            (run_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
            write_manifest(
                run_dir, runtime=manifest_runtime or runtime, airflow={"dagRuns": dag_runs}
            )
            info = {**dag_run(), "runType": run_type}
            return checks.labels_problems(run_dir, lambda _: info)

    def test_mode_and_scheduler_runs(self) -> None:
        self.assertEqual(self.check("airflow", "manual", [{"dagRunId": "x"}]), [])
        self.assertTrue(self.check("compatibility-harness", "manual", [{"dagRunId": "x"}]))
        self.assertTrue(self.check("airflow", "manual", []))
        self.assertTrue(self.check("airflow", "backfill", [{"dagRunId": "x"}]))

    def test_manifest_must_say_not_control_m(self) -> None:
        for runtime in ({"mode": "airflow"}, {"mode": "airflow", "isControlM": True}):
            with self.subTest(runtime=runtime):
                self.assertTrue(self.check("airflow", "manual", [{"dagRunId": "x"}], runtime))


RUN_START = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
WINDOWS = [(RUN_START, RUN_START + timedelta(minutes=1))]


class TestListeners(unittest.TestCase):
    def snapshot(
        self, *sockets: tuple[str, str, bool], at: datetime = RUN_START + timedelta(seconds=30)
    ) -> dict[str, Any]:
        return {
            "at": at.isoformat(),
            "sockets": [
                {"proto": proto, "local": local, "pid": 1, "airflow": airflow, "cmd": "c"}
                for proto, local, airflow in sockets
            ],
        }

    def test_only_loopback_api_port(self) -> None:
        good = self.snapshot(("tcp", "127.0.0.1:8080", True), ("tcp", "0.0.0.0:22", False))
        self.assertEqual(checks.listener_problems([good], 8080, WINDOWS), [])
        for bad in (
            ("tcp", "0.0.0.0:8793", True),
            ("tcp", "[::]:8794", True),
            ("udp", "127.0.0.1:8080", True),
        ):
            with self.subTest(bad=bad):
                snapshot = self.snapshot(("tcp", "127.0.0.1:8080", True), bad)
                self.assertTrue(checks.listener_problems([snapshot], 8080, WINDOWS))
        self.assertTrue(checks.listener_problems([], 8080, WINDOWS))
        self.assertTrue(
            checks.listener_problems([self.snapshot(("tcp", "0.0.0.0:22", False))], 8080, WINDOWS)
        )
        self.assertTrue(checks.listener_problems([good], 9090, WINDOWS))

    def test_a_snapshot_must_fall_inside_a_scenario_run(self) -> None:
        after = self.snapshot(("tcp", "127.0.0.1:8080", True), at=RUN_START + timedelta(hours=1))
        self.assertTrue(checks.listener_problems([after], 8080, WINDOWS))
        good = self.snapshot(("tcp", "127.0.0.1:8080", True))
        self.assertTrue(checks.listener_problems([good], 8080, []))

    def test_windows_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "windows.txt"
            self.assertEqual(checks.read_windows(path), [])
            path.write_text("2026-10-06T10:00:00+00:00 2026-10-06T10:01:00+00:00\n", "utf-8")
            self.assertEqual(checks.read_windows(path), WINDOWS)


class TestPins(unittest.TestCase):
    def test_requirement_lines_are_normalised(self) -> None:
        text = (
            "# comment\nApache_Airflow==3.3.2\n"
            "foo[bar]==1.0 ; python_version<'3.13'\n-e .\nbaz>=2\n"
        )
        self.assertEqual(checks.pins(text), {"apache-airflow": "3.3.2", "foo": "1.0"})


if __name__ == "__main__":
    unittest.main()
