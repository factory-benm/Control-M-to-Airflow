"""Read-only queries against the local Airflow metadata database.

Run with the Airflow venv's Python and the environment from
scripts/lib/airflow.sh. It never writes to the database: pausing, unpausing,
and triggering go through the Airflow CLI.

Usage:
  airflow_state.py dag-ready <dag_id>
  airflow_state.py paused <dag_id>
  airflow_state.py run <dag_id> <dag_run_id>
  airflow_state.py wait <dag_id> <dag_run_id> [--timeout SECONDS]

``dag-ready`` exits 0 once the DAG is registered, the DAG processor has parsed
the file since it last changed, and there are no import errors.

``run`` and ``wait`` print the DAG run with every task instance and every try,
taken from Airflow's own task instance and task instance history tables. That is
the evidence for retry counts and retry timing; it does not depend on anything
the tasks wrote themselves.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from airflow.models.dag import DagModel
from airflow.models.dagrun import DagRun
from airflow.models.errors import ParseImportError
from airflow.models.taskinstance import TaskInstance
from airflow.models.taskinstancehistory import TaskInstanceHistory
from airflow.utils.session import create_session
from sqlalchemy.exc import OperationalError

TERMINAL_STATES = {"success", "failed"}
POLL_SECONDS = 2.0
PARSE_MARGIN_SECONDS = 2.0

JsonObject = dict[str, Any]
Result = TypeVar("Result")


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def with_retry(query: Callable[[], Result], attempts: int = 5) -> Result:
    # The scheduler writes to the same SQLite file; a read can briefly see
    # "database is locked".
    for attempt in range(1, attempts + 1):
        try:
            return query()
        except OperationalError:
            if attempt == attempts:
                raise
            time.sleep(0.5 * attempt)
    raise AssertionError("unreachable")


def parsed_after_last_edit(dag: DagModel | None) -> bool:
    """True once the DAG processor has parsed the file as it is on disk now."""
    if dag is None or dag.last_parsed_time is None or not os.path.isfile(dag.fileloc):
        return False
    # The margin covers a parse that started just before the edit and finished after it.
    edited = os.path.getmtime(dag.fileloc) + PARSE_MARGIN_SECONDS
    return bool(dag.last_parsed_time.timestamp() >= edited)


def dag_ready(dag_id: str) -> tuple[bool, JsonObject]:
    with create_session() as session:
        dag = session.get(DagModel, dag_id)
        errors = [
            {"filename": error.filename, "stacktrace": error.stacktrace}
            for error in session.query(ParseImportError).all()
        ]
        report: JsonObject = {
            "dagId": dag_id,
            "registered": dag is not None and not dag.is_stale,
            "paused": None if dag is None else bool(dag.is_paused),
            "parsedAfterLastEdit": parsed_after_last_edit(dag),
            "lastParsed": iso(dag.last_parsed_time) if dag else None,
            "importErrors": errors,
        }
    ready = bool(report["registered"]) and bool(report["parsedAfterLastEdit"]) and not errors
    return ready, report


def is_paused(dag_id: str) -> bool | None:
    with create_session() as session:
        dag = session.get(DagModel, dag_id)
        return None if dag is None else bool(dag.is_paused)


def task_tries(session: Any, dag_id: str, run_id: str) -> dict[str, JsonObject]:
    tasks: dict[str, JsonObject] = {}
    current = (
        session.query(TaskInstance)
        .filter(TaskInstance.dag_id == dag_id, TaskInstance.run_id == run_id)
        .all()
    )
    history = (
        session.query(TaskInstanceHistory)
        .filter(TaskInstanceHistory.dag_id == dag_id, TaskInstanceHistory.run_id == run_id)
        .all()
    )
    for instance in current:
        tasks[instance.task_id] = {
            "state": instance.state,
            "tryNumber": instance.try_number,
            "tries": [],
        }
    for row in [*history, *current]:
        entry = tasks.setdefault(row.task_id, {"state": None, "tryNumber": 0, "tries": []})
        if row.try_number == 0:
            continue
        entry["tries"].append(
            {
                "tryNumber": row.try_number,
                "state": row.state,
                "start": iso(row.start_date),
                "end": iso(row.end_date),
            }
        )
    for entry in tasks.values():
        entry["tries"].sort(key=lambda item: item["tryNumber"])
    return tasks


def run_info(dag_id: str, run_id: str) -> JsonObject | None:
    with create_session() as session:
        run = session.query(DagRun).filter(DagRun.dag_id == dag_id, DagRun.run_id == run_id).first()
        if run is None:
            return None
        return {
            "dagId": dag_id,
            "dagRunId": run_id,
            "state": run.state,
            "runType": run.run_type,
            "conf": run.conf,
            "start": iso(run.start_date),
            "end": iso(run.end_date),
            "tasks": task_tries(session, dag_id, run_id),
        }


def wait_for_run(dag_id: str, run_id: str, timeout: float) -> JsonObject | None:
    deadline = time.monotonic() + timeout
    while True:
        info = with_retry(lambda: run_info(dag_id, run_id))
        if info is not None and info["state"] in TERMINAL_STATES:
            return info
        if time.monotonic() >= deadline:
            return info
        time.sleep(POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    ready = commands.add_parser("dag-ready")
    ready.add_argument("dag_id")
    paused = commands.add_parser("paused")
    paused.add_argument("dag_id")
    for name in ("run", "wait"):
        command = commands.add_parser(name)
        command.add_argument("dag_id")
        command.add_argument("dag_run_id")
        command.add_argument("--timeout", type=float, default=1800.0)
    arguments = parser.parse_args()

    if arguments.command == "dag-ready":
        ok, report = with_retry(lambda: dag_ready(arguments.dag_id))
        print(json.dumps(report, indent=2))
        return 0 if ok else 1
    if arguments.command == "paused":
        state = with_retry(lambda: is_paused(arguments.dag_id))
        print(json.dumps(state))
        return 0 if state is not None else 1
    if arguments.command == "run":
        info = with_retry(lambda: run_info(arguments.dag_id, arguments.dag_run_id))
    else:
        info = wait_for_run(arguments.dag_id, arguments.dag_run_id, arguments.timeout)
    print(json.dumps(info, indent=2, sort_keys=True))
    if info is None:
        return 1
    return 0 if arguments.command == "run" or info["state"] in TERMINAL_STATES else 1


if __name__ == "__main__":
    sys.exit(main())
