#!/usr/bin/env python3
"""Run a payments scenario through the local Airflow scheduler and record it.

The Airflow counterpart of runtimes/control-m/harness/harness.py. Airflow
executes the tasks; this program only prepares the run directory, triggers the
DAG runs, waits for them, and writes the same runtime.json and run-manifest.json
the harness writes, so runtimes/control-m/harness/verify_run.py grades both the
same way (design Decision 11). It reuses the harness module's helpers by import
and never edits it.

  * One DAG run per pass. A declared rerun with no injected fault runs the
    whole graph a second time as a new DAG run with conf pass=2, the same rule
    as the harness (Decision 12).
  * The DAG is paused on creation (Decision 4). It is unpaused only while this
    program's runs execute, and its paused state is restored afterwards, also
    on failure, Ctrl-C, or SIGTERM (Decision 18).
  * The exit sequence, counts, and per-pass ledger rows come from the
    per-attempt records the tasks append to logs/airflow-attempts.jsonl. They
    are cross-checked against Airflow's own task instance tries.

Run it through scripts/airflow-run.sh, which provides the Airflow environment.

Usage:
  runner.py run <scenario> [--run-id ID]
  runner.py trigger [--conf JSON] [--dag-run-id ID]
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

from harness import (
    CANONICAL_COUNTS,
    MANIFEST_VERSION,
    WORKFLOW,
    Harness,
    HarnessError,
    JsonObject,
    ledger_rows,
    read_jsonl,
    utc_now,
    write_json,
)

RUNTIME_MODE = "airflow"
DAG_FILE = "runtimes/airflow/dags/payops_cross_border_reconciliation.py"
ATTEMPTS_FILE = "airflow-attempts.jsonl"
DEFAULT_TIMEOUT_SECONDS = 1800.0
PARSE_TIMEOUT_SECONDS = 120.0
PARSE_POLL_SECONDS = 2.0
EXIT_UNAVAILABLE = 78


class RunnerError(Exception):
    """A runner-level failure, distinct from a task failure."""


class AirflowClient:
    """The Airflow CLI for writes, airflow_state.py for read-only queries."""

    def __init__(self, airflow_home: Path, kit_root: Path, dag_id: str) -> None:
        self.airflow = airflow_home / "venv" / "bin" / "airflow"
        self.python = airflow_home / "venv" / "bin" / "python"
        self.state_script = kit_root / "runtimes" / "airflow" / "airflow_state.py"
        self.lock_path = airflow_home / "run" / "runner.lock"
        self.dag_id = dag_id
        if not self.airflow.is_file():
            raise RunnerError(f"Airflow is not installed at {self.airflow}")

    def _call(self, command: list[str]) -> str:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()[-5:]
            raise RunnerError(f"{' '.join(command[1:4])} failed: " + " | ".join(detail))
        return completed.stdout

    def _state(self, *arguments: str) -> Any:
        command = [str(self.python), str(self.state_script), *arguments]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            tail = completed.stderr.strip().splitlines()[-5:]
            raise RunnerError(f"airflow_state {arguments[0]} failed: {tail}") from error

    def version(self) -> str:
        """The installed apache-airflow version, from the venv's package metadata."""
        venv = self.airflow.parent.parent
        found = sorted(venv.glob("lib/python*/site-packages/apache_airflow-*.dist-info"))
        if not found:
            return "unknown"
        return found[0].name.removeprefix("apache_airflow-").removesuffix(".dist-info")

    def wait_until_current(self, timeout: float = PARSE_TIMEOUT_SECONDS) -> None:
        """Wait until Airflow has parsed the DAG file as it is on disk now.

        Otherwise a run triggered right after an edit runs the previous version.
        """
        deadline = time.monotonic() + timeout
        while True:
            report = self._state("dag-ready", self.dag_id)
            if report.get("importErrors"):
                files = sorted({error["filename"] for error in report["importErrors"]})
                raise RunnerError(f"Airflow reports import errors in {files}")
            if report.get("registered") and report.get("parsedAfterLastEdit"):
                return
            if time.monotonic() >= deadline:
                raise RunnerError(
                    f"DAG {self.dag_id} was not parsed within {timeout:.0f}s; "
                    "is the dag-processor running?"
                )
            time.sleep(PARSE_POLL_SECONDS)

    def is_paused(self) -> bool:
        paused = self._state("paused", self.dag_id)
        if paused is None:
            raise RunnerError(f"DAG {self.dag_id} is not registered; is Airflow running?")
        return bool(paused)

    def set_paused(self, paused: bool) -> None:
        self._call([str(self.airflow), "dags", "pause" if paused else "unpause", self.dag_id])

    def trigger(self, dag_run_id: str, conf: JsonObject) -> None:
        self._call(
            [
                str(self.airflow),
                "dags",
                "trigger",
                self.dag_id,
                "--run-id",
                dag_run_id,
                "--conf",
                json.dumps(conf, sort_keys=True),
            ]
        )

    def wait(self, dag_run_id: str, timeout: float) -> JsonObject:
        info = self._state("wait", self.dag_id, dag_run_id, "--timeout", str(timeout))
        if not isinstance(info, dict):
            raise RunnerError(f"DAG run {dag_run_id} not found")
        return info


def _raise_on_sigterm(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


class PauseControl(Protocol):
    lock_path: Path

    def is_paused(self) -> bool: ...

    def set_paused(self, paused: bool) -> None: ...


@contextlib.contextmanager
def dag_unpaused(client: PauseControl) -> Iterator[None]:
    """Unpause the DAG for the duration of the block, then restore its state.

    A lock serializes runners, so one runner cannot re-pause the DAG while
    another runner's DAG run is still executing.
    """
    client.lock_path.parent.mkdir(parents=True, exist_ok=True)
    previous_handler = signal.signal(signal.SIGTERM, _raise_on_sigterm)
    with open(client.lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        was_paused = client.is_paused()
        try:
            if was_paused:
                client.set_paused(False)
            yield
        finally:
            if was_paused:
                client.set_paused(True)
            signal.signal(signal.SIGTERM, previous_handler)


def dag_run_id_for(run_id: str, pass_number: int) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{run_id}__pass{pass_number}__{stamp}"


def wanted_passes(harness: Harness) -> int:
    # Same rule as harness.py: a declared rerun with no injected fault runs the
    # whole graph again; a rerun with a fault is a task-level retry.
    return 2 if harness.rerun and harness.rerun.get("required") and not harness.fault else 1


def runtime_metadata(harness: Harness, airflow_version: str) -> JsonObject:
    return {
        "mode": RUNTIME_MODE,
        "modeLabel": "Apache Airflow execution of the migrated workflow",
        "isControlM": False,
        "disclosure": (
            f"This run was produced by Apache Airflow {airflow_version} running the DAG "
            f"{WORKFLOW} on a local scheduler with the LocalExecutor. It is not BMC "
            "Control-M and not the compatibility harness. Each task runs the same "
            "repository command declared in the Control-M definitions."
        ),
        "definitionSource": "repos/payments-orchestrator/controlm/payments_reconciliation.json",
        "dagFile": DAG_FILE,
        "airflowVersion": airflow_version,
        "graph": {"tasks": len(harness.graph.tasks), "edges": len(harness.graph.edges)},
        "workflow": WORKFLOW,
        "scenario": harness.scenario_id,
        "runId": harness.run_id,
        "fixedClock": harness.fixed_clock,
        "host": {"platform": sys.platform, "architecture": os.uname().machine},
        "selectedAt": utc_now(),
    }


def group_passes(records: list[JsonObject]) -> list[JsonObject]:
    numbers = sorted({int(record["pass"]) for record in records})
    return [
        {"pass": number, "tasks": [record for record in records if record["pass"] == number]}
        for number in numbers
    ]


def apply_records(harness: Harness, records: list[JsonObject]) -> None:
    """Fill the harness's observations from the tasks' per-attempt records."""
    harness.exit_sequence = [
        {"task": record["task"], "attempt": record["attempt"], "exitCode": record["exitCode"]}
        for record in records
    ]
    harness.passes = group_passes(records)
    for record in records:
        if record["exitCode"] == 0:
            harness.counts.update(record["counts"])


def tries_problems(records: list[JsonObject], dag_runs: list[JsonObject]) -> list[str]:
    """Airflow's own try counts must match the attempts the tasks recorded."""
    problems: list[str] = []
    for dag_run in dag_runs:
        pass_number = dag_run["pass"]
        for task, instance in sorted(dag_run["tasks"].items()):
            recorded = sum(
                1 for record in records if record["pass"] == pass_number and record["task"] == task
            )
            tries = len(instance["tries"])
            if tries != recorded:
                problems.append(
                    f"pass {pass_number} {task}: Airflow ran {tries} tries, "
                    f"tasks recorded {recorded} attempts"
                )
    return problems


def run_status(harness: Harness, dag_runs: list[JsonObject], problems: list[str]) -> str | None:
    """None when the run succeeded, otherwise why it did not."""
    failed = [run for run in dag_runs if run["state"] != "success"]
    if failed:
        return f"DAG run {failed[0]['dagRunId']} ended {failed[0]['state']}"
    if len(dag_runs) != wanted_passes(harness):
        return f"expected {wanted_passes(harness)} DAG runs, completed {len(dag_runs)}"
    if problems:
        return "; ".join(problems)
    return None


def build_manifest(
    harness: Harness,
    records: list[JsonObject],
    dag_runs: list[JsonObject],
    hashes: tuple[dict[str, str], dict[str, str]],
    started: str,
) -> JsonObject:
    apply_records(harness, records)
    failure = run_status(harness, dag_runs, tries_problems(records, dag_runs))
    before, after = hashes
    manifest: JsonObject = {
        "manifestVersion": MANIFEST_VERSION,
        "workflow": WORKFLOW,
        "runId": harness.run_id,
        "scenario": harness.scenario_id,
        "scenarioVersion": harness.scenario.get("scenarioVersion"),
        "runtime": {"mode": RUNTIME_MODE, "isControlM": False, "fixedClock": harness.fixed_clock},
        "status": "failed" if failure else "success",
        "counts": {key: harness.counts[key] for key in CANONICAL_COUNTS if key in harness.counts},
        "passes": harness.passes,
        "exitSequence": harness.exit_sequence,
        "ledgerRowsAfterPass": harness.ledger_after_pass,
        "inputHashes": before,
        "outputHashes": harness.output_hashes(),
        "invariants": harness.observed_invariants(before, after),
        "provenance": "observed-run",
        "startedAt": started,
        "completedAt": utc_now(),
        "airflow": {
            "dagId": WORKFLOW,
            "dagRuns": [
                {key: run[key] for key in ("pass", "dagRunId", "state", "runType", "tasks")}
                for run in dag_runs
            ],
        },
    }
    if failure:
        manifest["failure"] = failure
    return manifest


def execute_passes(client: AirflowClient, harness: Harness, timeout: float) -> list[JsonObject]:
    dag_runs: list[JsonObject] = []
    client.wait_until_current()
    with dag_unpaused(client):
        for pass_number in range(1, wanted_passes(harness) + 1):
            dag_run_id = dag_run_id_for(harness.run_id, pass_number)
            print(f"airflow-run: pass {pass_number}, DAG run {dag_run_id}", file=sys.stderr)
            client.trigger(
                dag_run_id,
                {
                    "scenario": harness.scenario_id,
                    "run_id": harness.run_id,
                    "kit_root": str(harness.kit_root),
                    "fixed_clock": harness.fixed_clock,
                    "pass": pass_number,
                },
            )
            info = client.wait(dag_run_id, timeout)
            info["pass"] = pass_number
            dag_runs.append(info)
            harness.ledger_after_pass.append(ledger_rows(harness.run_dir)[0])
            print(f"airflow-run: DAG run {dag_run_id} ended {info['state']}", file=sys.stderr)
            if info["state"] != "success":
                break
    return dag_runs


def run_scenario(client: AirflowClient, harness: Harness, timeout: float) -> JsonObject:
    started = utc_now()
    harness.prepare_run_dir(fresh=True)
    write_json(harness.run_dir / "runtime.json", runtime_metadata(harness, client.version()))
    before = harness.source_hashes()
    dag_runs = execute_passes(client, harness, timeout)
    records = read_jsonl(harness.run_dir / "logs" / ATTEMPTS_FILE)
    manifest = build_manifest(
        harness, records, dag_runs, (before, harness.source_hashes()), started
    )
    write_json(harness.run_dir / "run-manifest.json", manifest)
    return manifest


def kit_root_from_here() -> Path:
    return Path(__file__).resolve().parents[2]


def airflow_home() -> Path:
    value = os.environ.get("PAYOPS_AIRFLOW_HOME")
    if not value or not os.environ.get("AIRFLOW_HOME"):
        raise RunnerError("the Airflow environment is not set; use scripts/airflow-run.sh")
    return Path(value)


def command_run(arguments: argparse.Namespace, client: AirflowClient) -> int:
    kit_root = kit_root_from_here()
    run_id = arguments.run_id or f"{arguments.scenario}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    harness = Harness(
        kit_root=kit_root,
        repos_root=kit_root / "repos",
        run_root=kit_root / "workspace" / "runtime" / "runs",
        scenario_id=arguments.scenario,
        run_id=run_id,
    )
    manifest = run_scenario(client, harness, arguments.timeout)
    failed = [item["id"] for item in manifest["invariants"] if item["status"] != "pass"]
    print(
        f"airflow-run: scenario={manifest['scenario']} status={manifest['status']} "
        f"mode={RUNTIME_MODE} counts={manifest['counts']}",
        file=sys.stderr,
    )
    if manifest.get("failure"):
        print(f"airflow-run: {manifest['failure']}", file=sys.stderr)
    if failed:
        print(f"airflow-run: failing invariants {failed}", file=sys.stderr)
    print(f"airflow-run: run directory {harness.run_dir}", file=sys.stderr)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest["status"] == "success" and not failed else 1


def command_trigger(arguments: argparse.Namespace, client: AirflowClient) -> int:
    conf: JsonObject = json.loads(arguments.conf) if arguments.conf else {}
    if not isinstance(conf, dict):
        raise RunnerError("--conf must be a JSON object")
    dag_run_id = arguments.dag_run_id or dag_run_id_for(str(conf.get("run_id") or "manual"), 1)
    client.wait_until_current()
    with dag_unpaused(client):
        client.trigger(dag_run_id, conf)
        info = client.wait(dag_run_id, arguments.timeout)
    print(f"airflow-run: DAG run {dag_run_id} ended {info['state']}", file=sys.stderr)
    print(json.dumps(info, indent=2, sort_keys=True))
    return 0 if info["state"] == "success" else 1


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one scenario and write run-manifest.json")
    run.add_argument("scenario")
    run.add_argument("--run-id")
    trigger = commands.add_parser("trigger", help="trigger one DAG run with the given conf")
    trigger.add_argument("--conf", help='JSON object, for example {"scenario": "happy-path"}')
    trigger.add_argument("--dag-run-id")
    for command in (run, trigger):
        command.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        home = airflow_home()
        client = AirflowClient(home, kit_root_from_here(), WORKFLOW)
    except RunnerError as error:
        print(f"airflow-run: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        if arguments.command == "run":
            return command_run(arguments, client)
        return command_trigger(arguments, client)
    except (HarnessError, RunnerError) as error:
        print(f"airflow-run: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
