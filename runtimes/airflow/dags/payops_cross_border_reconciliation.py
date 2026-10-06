"""PAYOPS_CROSS_BORDER_RECONCILIATION on Apache Airflow 3.

Migrated from the Control-M folder of the same name, as decided in
`docs/migration-design.md`. The DAG restates nothing about the workflow. It
reads the Control-M definitions in `repos/payments-orchestrator/controlm/` when
the file is parsed:

* Tasks, edges, retry limits, and retry delays come from the JSON conditions
  and `task-commands.json`, through `orchestrator.graph`. The DAG refuses to
  load when the JSON, XML, and task-commands disagree (Decision 9).
* Each task's command line is the JSON `Command` (Decision 8).
* The schedule is the JSON `When` block and the calendar it names in
  `calendars.json`, at 20:00 Asia/Singapore (Decisions 2 and 3).
* The failure notification is the JSON `ActionIfFailure` mail. It is logged
  and never sent (Decision 10).

Each task runs the existing wrapper `run-task.sh` as a subprocess, with
`--attempt` set to Airflow's try number (Decision 7). Only an exit 3 that the
scenario declares for that task and attempt is retried. Every other failure
fails at once (Decision 6).

Run parameters (Decision 15): `scenario` (default from the JSON variables),
`run_id` (default `scheduled-<order date>`), `kit_root` (default this
repository), `fixed_clock` (default the scenario's `clock.fixedUtc`), and
`pass` (1, or 2 for the second run of a whole-batch rerun, Decision 12).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pendulum
from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, get_current_context
from airflow.sdk.exceptions import AirflowException, AirflowFailException
from airflow.timetables.events import EventsTimetable

KIT_ROOT = Path(__file__).resolve().parents[3]
ORCHESTRATOR_SRC = KIT_ROOT / "repos" / "payments-orchestrator" / "src"
CONTROLM_DIR = KIT_ROOT / "repos" / "payments-orchestrator" / "controlm"
TIMEZONE = "Asia/Singapore"
DECLARED_FAULT_EXIT = 3
RUN_SUBDIRS = ("input", "stages", "logs", "ledger", "output")
ATTEMPTS_FILE = "airflow-attempts.jsonl"
WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
VARIABLE = re.compile(r"%%([A-Z][A-Z0-9_]*)")
COMMAND_VARIABLES = frozenset(
    {"ORCHESTRATOR_HOME", "RUN_DIR", "SCENARIO", "RUN_ID", "KIT_ROOT", "FIXED_CLOCK", "ATTEMPT"}
)
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

JsonObject = dict[str, Any]

log = logging.getLogger("airflow.task")


def load_graph_module() -> ModuleType:
    """Import the orchestrator's own definition parser (standard library only)."""
    if str(ORCHESTRATOR_SRC) not in sys.path:
        sys.path.insert(0, str(ORCHESTRATOR_SRC))
    return importlib.import_module("orchestrator.graph")


@dataclass(frozen=True)
class Workflow:
    """Everything the DAG takes from the Control-M definitions."""

    dag_id: str
    description: str
    tags: list[str]
    order: list[str]
    edges: list[tuple[str, str]]
    commands: dict[str, str]
    descriptions: dict[str, str]
    task_commands: dict[str, JsonObject]
    variables: dict[str, str]
    failure_mail: JsonObject
    when: JsonObject
    calendars: JsonObject


def load_workflow(controlm_dir: Path = CONTROLM_DIR) -> Workflow:
    graph_module = load_graph_module()
    problems = graph_module.agreement_problems(controlm_dir)
    if problems:
        raise RuntimeError(
            "the Control-M definition files disagree, refusing to load:\n  " + "\n  ".join(problems)
        )
    graph = graph_module.canonical_graph(controlm_dir)
    document = json.loads((controlm_dir / "payments_reconciliation.json").read_text("utf-8"))
    defaults = document["Defaults"]
    folder = document[graph_module.WORKFLOW_NAME]
    jobs = {
        name: job
        for name, job in folder.items()
        if isinstance(job, dict) and str(job.get("Type", "")).startswith("Job:")
    }
    commands = {name: str(job["Command"]) for name, job in jobs.items()}
    for name, command in commands.items():
        unknown = set(VARIABLE.findall(command)) - COMMAND_VARIABLES
        if unknown:
            raise RuntimeError(f"{name}: Command uses unmapped variables {sorted(unknown)}")
    failure_actions = [
        action
        for action in defaults["Job"]["ActionIfFailure"].values()
        if isinstance(action, dict) and action.get("Type") == "Mail"
    ]
    return Workflow(
        dag_id=graph_module.WORKFLOW_NAME,
        description=str(folder.get("Comment", "")),
        tags=[defaults["Application"], defaults["SubApplication"]],
        order=graph.topological_order(),
        edges=sorted(graph.edges),
        commands=commands,
        descriptions={name: str(job.get("Description", "")) for name, job in jobs.items()},
        task_commands={
            entry["task"]: entry
            for entry in graph_module.load_task_commands(controlm_dir / "task-commands.json")
        },
        variables={
            key: str(value) for item in defaults["Variables"] for key, value in item.items()
        },
        failure_mail=failure_actions[0],
        when=defaults["Job"]["When"],
        calendars=json.loads((controlm_dir / "calendars.json").read_text("utf-8")),
    )


# -- schedule (Decisions 2 and 3) -----------------------------------------


def run_dates(when: JsonObject, calendars: JsonObject) -> list[date]:
    """Days that satisfy the job's week days and an included calendar."""
    week_days = {WEEKDAYS[day] for day in when["WeekDays"]}
    by_name = {calendar["name"]: calendar for calendar in calendars["calendars"]}
    dates: set[date] = set()
    for name in when["RuleBasedCalendars"]["Included"]:
        calendar = by_name[name]
        business_days = week_days & {WEEKDAYS[day] for day in calendar["weekDays"]}
        excluded = set(calendar["excludedDates"])
        day = date(int(calendar["year"]), 1, 1)
        while day.year == int(calendar["year"]):
            if day.weekday() in business_days and day.isoformat() not in excluded:
                dates.add(day)
            day += timedelta(days=1)
    return sorted(dates)


def run_times(when: JsonObject, calendars: JsonObject) -> list[pendulum.DateTime]:
    hour, minute = int(when["FromTime"][:2]), int(when["FromTime"][2:])
    return [
        pendulum.datetime(day.year, day.month, day.day, hour, minute, tz=TIMEZONE)
        for day in run_dates(when, calendars)
    ]


# -- one task try (design section 5.2) ------------------------------------


@dataclass(frozen=True)
class RunValues:
    kit_root: Path
    scenario: str
    run_id: str
    fixed_clock: str
    pass_number: int
    fault: JsonObject | None

    @property
    def run_dir(self) -> Path:
        return self.kit_root / "workspace" / "runtime" / "runs" / self.run_id


def order_date(dag_run: Any) -> str:
    moment = getattr(dag_run, "logical_date", None) or getattr(dag_run, "run_after", None)
    if moment is None:
        moment = datetime.now(UTC)
    return pendulum.instance(moment).in_timezone(TIMEZONE).date().isoformat()


def resolve_run(params: Any, dag_run: Any, variables: dict[str, str]) -> RunValues:
    kit_root = Path(str(params.get("kit_root") or KIT_ROOT))
    scenario = str(params.get("scenario") or variables["SCENARIO"])
    run_id = str(params.get("run_id") or f"scheduled-{order_date(dag_run)}")
    if not RUN_ID_PATTERN.match(run_id):
        raise AirflowFailException(f"run_id {run_id!r} is not a safe directory name")
    scenario_file = kit_root / "fixtures" / "scenarios" / scenario / "scenario.json"
    scenario_doc: JsonObject = (
        json.loads(scenario_file.read_text("utf-8")) if scenario_file.is_file() else {}
    )
    fixed_clock = str(
        params.get("fixed_clock")
        or scenario_doc.get("clock", {}).get("fixedUtc")
        or variables["FIXED_CLOCK"]
    )
    return RunValues(
        kit_root=kit_root,
        scenario=scenario,
        run_id=run_id,
        fixed_clock=fixed_clock,
        pass_number=int(params.get("pass") or 1),
        fault=scenario_doc.get("faultInjection"),
    )


def build_command(template: str, run: RunValues, attempt: int) -> list[str]:
    """The JSON Command with its %% variables replaced, split into arguments."""
    values = {
        "ORCHESTRATOR_HOME": str(run.kit_root / "repos" / "payments-orchestrator"),
        "RUN_DIR": str(run.run_dir),
        "SCENARIO": run.scenario,
        "RUN_ID": run.run_id,
        "KIT_ROOT": str(run.kit_root),
        "FIXED_CLOCK": run.fixed_clock,
        "ATTEMPT": str(attempt),
    }
    # Substitute per token, so a value containing spaces stays one argument.
    return [VARIABLE.sub(lambda match: values[match.group(1)], token) for token in template.split()]


def task_environment(run: RunValues, attempt: int) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PAYOPS_KIT_ROOT": str(run.kit_root),
            "PAYOPS_FIXED_CLOCK": run.fixed_clock,
            "PAYOPS_ATTEMPT": str(attempt),
            "PAYOPS_RUN_ID": run.run_id,
            "PAYOPS_SCENARIO": run.scenario,
        }
    )
    return environment


def parse_result(stdout: str) -> JsonObject | None:
    if not stdout.strip():
        return None
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_attempt(
    task: str, repository: str, argv: list[str], run: RunValues, attempt: int
) -> JsonObject:
    """Run the wrapper once and keep the harness's per-attempt evidence."""
    for name in RUN_SUBDIRS:
        (run.run_dir / name).mkdir(parents=True, exist_ok=True)
    stem = f"{task}.pass{run.pass_number}.attempt{attempt}"
    started, clock = utc_now(), time.monotonic()
    completed = subprocess.run(
        argv, capture_output=True, text=True, env=task_environment(run, attempt), check=False
    )
    finished = utc_now()
    (run.run_dir / "logs" / f"{stem}.stdout").write_text(completed.stdout, encoding="utf-8")
    (run.run_dir / "logs" / f"{stem}.stderr").write_text(completed.stderr, encoding="utf-8")
    result = parse_result(completed.stdout)
    record: JsonObject = {
        "task": task,
        "repository": repository,
        "pass": run.pass_number,
        "attempt": attempt,
        "exitCode": completed.returncode,
        "status": "success" if completed.returncode == 0 else "failed",
        "startedAt": started,
        "completedAt": finished,
        "durationMs": int((time.monotonic() - clock) * 1000),
        "counts": (result or {}).get("counts", {}),
        "metrics": (result or {}).get("metrics", {}),
        "artifacts": (result or {}).get("artifacts", []),
        "logs": {"stdout": f"logs/{stem}.stdout", "stderr": f"logs/{stem}.stderr"},
        "resultParsed": result is not None,
    }
    if result and "error" in result:
        record["error"] = result["error"]
    with open(run.run_dir / "logs" / ATTEMPTS_FILE, "a", encoding="utf-8") as attempts:
        attempts.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def fault_declared(fault: JsonObject | None, task: str, attempt: int) -> bool:
    return bool(fault and fault.get("task") == task and int(fault.get("attempt", 0)) == attempt)


def classify(record: JsonObject, fault: JsonObject | None) -> str:
    """success, retry (only a declared exit 3), or fail (everything else)."""
    if record["exitCode"] == 0:
        return "success" if record["resultParsed"] else "fail"
    if record["exitCode"] == DECLARED_FAULT_EXIT and fault_declared(
        fault, record["task"], record["attempt"]
    ):
        return "retry"
    return "fail"


def run_payops_task(task_name: str) -> None:
    context = get_current_context()
    attempt = int(context["ti"].try_number)
    try:
        run = resolve_run(context["params"], context["dag_run"], WORKFLOW.variables)
        argv = build_command(WORKFLOW.commands[task_name], run, attempt)
        log.info("running %s", " ".join(argv))
        repository = str(WORKFLOW.task_commands[task_name]["repository"])
        record = run_attempt(task_name, repository, argv, run, attempt)
    except AirflowException:
        raise
    except Exception as error:
        # Airflow would retry a plain exception; only a declared exit 3 may retry.
        raise AirflowFailException(f"{task_name}: {error}") from error
    outcome = classify(record, run.fault)
    summary = (
        f"{task_name} pass {run.pass_number} attempt {attempt} exited {record['exitCode']}; "
        f"stderr in {run.run_dir / record['logs']['stderr']}"
    )
    if outcome == "success":
        log.info("%s counts=%s", summary, record["counts"])
        return
    if outcome == "retry":
        raise AirflowException(f"{summary}: the declared deterministic fault; retrying")
    raise AirflowFailException(f"{summary}: not a declared fault; not retrying")


def failure_message(task: str, params: Any, dag_run: Any) -> str:
    run_id = str(params.get("run_id") or f"scheduled-{order_date(dag_run)}")
    scenario = str(params.get("scenario") or WORKFLOW.variables["SCENARIO"])
    values = {"JOBNAME": task, "RUN_ID": run_id, "SCENARIO": scenario}
    template = str(WORKFLOW.failure_mail["Message"])
    return VARIABLE.sub(lambda match: values.get(match.group(1), match.group(0)), template)


def notify_failure(context: Any) -> None:
    """Log the Control-M failure mail. Nothing is sent (Decision 10)."""
    task = context["ti"].task_id
    message = failure_message(task, context["params"], context["dag_run"])
    log.error(
        "Control-M ActionIfFailure mail, logged only, not sent: to=%s message=%r",
        WORKFLOW.failure_mail["To"],
        message,
    )


def task_doc(task: str) -> str:
    entry = WORKFLOW.task_commands[task]
    critical = "yes" if entry.get("critical") else "no"
    return (
        f"{WORKFLOW.descriptions[task]}\n\n"
        f"Control-M job `{task}`, repository `{entry['repository']}`. "
        f"Control-M CRITICAL: {critical} (recorded only, Decision 14).\n\n"
        f"Command: `{WORKFLOW.commands[task]}`"
    )


WORKFLOW = load_workflow()
RUN_TIMES = run_times(WORKFLOW.when, WORKFLOW.calendars)

dag = DAG(
    dag_id=WORKFLOW.dag_id,
    description=WORKFLOW.description,
    doc_md=__doc__,
    schedule=EventsTimetable(
        event_dates=RUN_TIMES,
        description=(
            f"{WORKFLOW.when['FromTime'][:2]}:{WORKFLOW.when['FromTime'][2:]} {TIMEZONE} on "
            + ", ".join(WORKFLOW.when["RuleBasedCalendars"]["Included"])
            + " business days"
        ),
    ),
    start_date=pendulum.datetime(RUN_TIMES[0].year, 1, 1, tz=TIMEZONE),
    catchup=False,
    is_paused_upon_creation=True,
    max_active_runs=1,
    tags=WORKFLOW.tags,
    params={
        "scenario": WORKFLOW.variables["SCENARIO"],
        "run_id": "",
        "kit_root": "",
        "fixed_clock": "",
        "pass": 1,
    },
)

with dag:
    operators = {
        name: PythonOperator(
            task_id=name,
            python_callable=run_payops_task,
            op_kwargs={"task_name": name},
            retries=int(WORKFLOW.task_commands[name].get("retryLimit", 0)),
            retry_delay=timedelta(
                seconds=int(WORKFLOW.task_commands[name].get("retryDelaySeconds", 0))
            ),
            retry_exponential_backoff=0,
            on_failure_callback=notify_failure,
            doc_md=task_doc(name),
        )
        for name in WORKFLOW.order
    }
    for upstream, downstream in WORKFLOW.edges:
        operators[upstream] >> operators[downstream]
