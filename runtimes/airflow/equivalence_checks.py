#!/usr/bin/env python3
"""Assertions behind scripts/test-airflow-equivalence.sh (design section 6).

Each subcommand prints one line per problem and a summary, and exits 0 only
when it found no problem. Expected values come from the fixtures, the Control-M
definitions, or the design decisions, never from the DAG or the runner. Retry
and try counts are read from Airflow's metadata database through
airflow_state.py, not from what the tasks wrote.

Standard library only. Run with the environment from scripts/lib/airflow.sh.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]
Fetch = Callable[[str], JsonObject]

DAG_ID = "PAYOPS_CROSS_BORDER_RECONCILIATION"
RETRIED_TASK = "post_pending_ledger"
MIN_RETRY_GAP_SECONDS = 60.0
# The pin decided in docs/migration-design.md, Decision 13.
PINNED_AIRFLOW = "3.3.2"
PINNED_PYTHON = "3.12"
PINNED_CONSTRAINTS_URL = (
    "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.12.txt"
)
PID_PATTERN = re.compile(r"pid=(\d+)")


def load_json(path: Path) -> JsonObject:
    document: JsonObject = json.loads(path.read_text(encoding="utf-8"))
    return document


def airflow_home() -> Path:
    return Path(os.environ["PAYOPS_AIRFLOW_HOME"])


def fetch_dag_run(dag_run_id: str) -> JsonObject:
    """One DAG run with every task instance try, straight from Airflow."""
    home = airflow_home()
    script = Path(__file__).resolve().parent / "airflow_state.py"
    command = [str(home / "venv" / "bin" / "python"), str(script), "run", DAG_ID, dag_run_id]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    info = json.loads(completed.stdout) if completed.stdout.strip() else None
    if not isinstance(info, dict):
        raise SystemExit(f"Airflow has no DAG run {dag_run_id}: {completed.stderr[-500:]}")
    return info


def task_order(kit_root: Path) -> list[str]:
    sys.path.insert(0, str(kit_root / "repos" / "payments-orchestrator" / "src"))
    from orchestrator.graph import canonical_graph

    controlm = kit_root / "repos" / "payments-orchestrator" / "controlm"
    order: list[str] = canonical_graph(controlm).topological_order()
    return order


def owed_scenarios(kit_root: Path) -> list[str]:
    """Every scenario with an oracle; the set owed by the scenario checks."""
    scenarios = kit_root / "fixtures" / "scenarios"
    return sorted(
        path.name for path in scenarios.iterdir() if (path / "expected" / "manifest.json").is_file()
    )


def seconds_between(earlier: str | None, later: str | None) -> float:
    if not earlier or not later:
        return -1.0
    return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds()


# -- labels ---------------------------------------------------------------


def labels_problems(run_dir: Path, fetch: Fetch) -> list[str]:
    runtime = load_json(run_dir / "runtime.json")
    manifest = load_json(run_dir / "run-manifest.json")
    problems: list[str] = []
    if runtime.get("mode") != "airflow" or runtime.get("isControlM") is not False:
        problems.append(
            f"runtime.json mode={runtime.get('mode')} isControlM={runtime.get('isControlM')}"
        )
    if manifest["runtime"].get("mode") != "airflow" or manifest["runtime"].get("isControlM"):
        problems.append(f"run-manifest.json runtime={manifest['runtime']}")
    dag_runs = manifest.get("airflow", {}).get("dagRuns") or []
    if not dag_runs:
        problems.append("run-manifest.json lists no Airflow DAG runs")
    for recorded in dag_runs:
        actual = fetch(recorded["dagRunId"])
        if actual["runType"] != "manual" or actual["state"] != "success":
            problems.append(
                f"DAG run {recorded['dagRunId']}: run_type={actual['runType']} "
                f"state={actual['state']} in Airflow"
            )
    return problems


# -- retry (section 6 check 5) --------------------------------------------


def exit_sequence_problems(manifest: JsonObject, order: list[str]) -> list[str]:
    problems: list[str] = []
    sequence = [(e["task"], e["attempt"], e["exitCode"]) for e in manifest["exitSequence"]]
    retried = [(attempt, code) for task, attempt, code in sequence if task == RETRIED_TASK]
    if retried != [(1, 3), (2, 0)]:
        problems.append(
            f"{RETRIED_TASK} attempts (attempt, exit) were {retried}, want [(1, 3), (2, 0)]"
        )
    for task in order:
        if task == RETRIED_TASK:
            continue
        mine = [(attempt, code) for name, attempt, code in sequence if name == task]
        if mine != [(1, 0)]:
            problems.append(f"{task} attempts (attempt, exit) were {mine}, want [(1, 0)]")
    return problems


def ledger_problems(run_dir: Path, expected: JsonObject) -> list[str]:
    declared = expected["partialWrite"]
    committed, retried = len(declared["committedOnFirstAttempt"]), len(declared["writtenOnRetry"])
    metrics = load_json(run_dir / "stages" / "ledger-posting.json").get("metrics", {})
    problems: list[str] = []
    if (metrics.get("alreadyPosted"), metrics.get("inserted")) != (committed, retried):
        problems.append(
            f"ledger-posting.json alreadyPosted={metrics.get('alreadyPosted')} "
            f"inserted={metrics.get('inserted')}, want {committed} and {retried}"
        )
    database = run_dir / "ledger" / "ledger.sqlite3"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows, distinct = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT payment_id) FROM ledger_entry"
        ).fetchone()
    finally:
        connection.close()
    if rows != committed + retried or distinct != rows:
        problems.append(f"ledger has {rows} rows and {distinct} distinct payment_id")
    return problems


def tries_problems(info: JsonObject, order: list[str], retried: str | None) -> list[str]:
    """Airflow's try counts: 2 for the retried task (if any), 1 for every other."""
    problems: list[str] = []
    for task in order:
        instance = info["tasks"].get(task)
        want = 2 if task == retried else 1
        if instance is None:
            problems.append(f"{info['dagRunId']}: Airflow has no task instance {task}")
            continue
        tries = len(instance["tries"])
        if instance["tryNumber"] != want or tries != want:
            problems.append(
                f"{info['dagRunId']} {task}: Airflow try_number={instance['tryNumber']} "
                f"with {tries} recorded tries, want {want}"
            )
    return problems


def retry_gap_problems(info: JsonObject) -> list[str]:
    tries = info["tasks"][RETRIED_TASK]["tries"]
    if len(tries) < 2:
        return [f"{RETRIED_TASK} has {len(tries)} tries in Airflow"]
    gap = seconds_between(tries[0]["end"], tries[1]["start"])
    print(f"  {RETRIED_TASK}: try 1 ended {tries[0]['end']}, try 2 started {tries[1]['start']}")
    print(f"  retry gap {gap:.1f}s (minimum {MIN_RETRY_GAP_SECONDS:.0f}s)")
    if gap < MIN_RETRY_GAP_SECONDS:
        return [f"retry started {gap:.1f}s after try 1 ended, want at least 60s"]
    return []


def dag_run_ids(run_dir: Path) -> list[str]:
    manifest = load_json(run_dir / "run-manifest.json")
    return [run["dagRunId"] for run in manifest.get("airflow", {}).get("dagRuns") or []]


def retry_problems(kit_root: Path, run_dir: Path, others: list[Path], fetch: Fetch) -> list[str]:
    order = task_order(kit_root)
    manifest = load_json(run_dir / "run-manifest.json")
    expected = load_json(
        kit_root / "fixtures" / "scenarios" / manifest["scenario"] / "expected" / "manifest.json"
    )
    problems = exit_sequence_problems(manifest, order) + ledger_problems(run_dir, expected)
    ids = dag_run_ids(run_dir)
    if len(ids) != 1:
        return [*problems, f"expected one DAG run, found {ids}"]
    info = fetch(ids[0])
    problems += tries_problems(info, order, RETRIED_TASK) + retry_gap_problems(info)
    for other in others:
        other_ids = dag_run_ids(other)
        if not other_ids:
            problems.append(f"{other.name}: no DAG runs recorded")
        for dag_run_id in other_ids:
            problems += tries_problems(fetch(dag_run_id), order, None)
    return problems


# -- whole-batch rerun (section 6 check 6) --------------------------------


def rerun_problems(kit_root: Path, run_dir: Path, fetch: Fetch) -> list[str]:
    order = task_order(kit_root)
    manifest = load_json(run_dir / "run-manifest.json")
    expected = load_json(
        kit_root / "fixtures" / "scenarios" / manifest["scenario"] / "expected" / "manifest.json"
    )["rerun"]
    problems: list[str] = []
    ids = dag_run_ids(run_dir)
    if len(ids) != 2 or len(set(ids)) != 2:
        problems.append(f"expected two distinct DAG runs, found {ids}")
    sequence = [(e["task"], e["attempt"], e["exitCode"]) for e in manifest["exitSequence"]]
    first = [(task, 1, 0) for task in order]
    if sequence != first + first:
        problems.append(
            f"exit sequence is not two identical passes of {len(order)} tasks: {sequence}"
        )
    final = expected["expectedPostedAfterRerun"]
    want = [final - expected["expectedNewLedgerRowsOnRerun"], final]
    if manifest["ledgerRowsAfterPass"] != want:
        problems.append(f"ledgerRowsAfterPass={manifest['ledgerRowsAfterPass']}, want {want}")
    for dag_run_id in ids:
        problems += tries_problems(fetch(dag_run_id), order, None)
    print(f"  DAG runs {ids}")
    print(f"  {len(sequence)} exit entries; ledgerRowsAfterPass={manifest['ledgerRowsAfterPass']}")
    return problems


# -- no retry on other failures (section 6 check 7) -----------------------


def failure_mail(kit_root: Path) -> JsonObject:
    definition = load_json(
        kit_root / "repos" / "payments-orchestrator" / "controlm" / "payments_reconciliation.json"
    )
    actions = definition["Defaults"]["Job"]["ActionIfFailure"].values()
    mail: JsonObject = next(a for a in actions if isinstance(a, dict) and a.get("Type") == "Mail")
    return mail


def task_log(dag_run_id: str, task: str) -> str:
    folder = airflow_home() / "logs" / f"dag_id={DAG_ID}" / f"run_id={dag_run_id}"
    path = folder / f"task_id={task}" / "attempt=1.log"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def failed_run_problems(info: JsonObject, order: list[str], failed_task: str) -> list[str]:
    problems: list[str] = []
    if info["state"] != "failed":
        problems.append(f"DAG run state {info['state']}, want failed")
    position = order.index(failed_task)
    for index, task in enumerate(order):
        instance = info["tasks"].get(task) or {"state": None, "tryNumber": 0, "tries": []}
        want = (
            "success" if index < position else "failed" if index == position else "upstream_failed"
        )
        if instance["state"] != want:
            problems.append(f"{task}: state {instance['state']}, want {want}")
    instance = info["tasks"].get(failed_task) or {"tryNumber": 0, "tries": []}
    if instance["tryNumber"] != 1 or len(instance["tries"]) != 1:
        problems.append(
            f"{failed_task}: try_number={instance['tryNumber']} with "
            f"{len(instance['tries'])} tries, want exactly one try"
        )
    return problems


def callback_problems(
    log_text: str, mail: JsonObject, task: str, run_id: str, scenario: str
) -> list[str]:
    message = (
        str(mail["Message"])
        .replace("%%JOBNAME", task)
        .replace("%%RUN_ID", run_id)
        .replace("%%SCENARIO", scenario)
    )
    for line in log_text.splitlines():
        if "not sent" in line and f"to={mail['To']}" in line and message in line:
            print(f"  callback logged: to={mail['To']} message={message!r}")
            return []
    return [f"no failure callback line for {task} naming {mail['To']} and {message!r}"]


def no_retry_problems(
    kit_root: Path, arguments: argparse.Namespace, fetch: Fetch, read_log: Callable[[str, str], str]
) -> list[str]:
    order = task_order(kit_root)
    info = fetch(arguments.dag_run_id)
    problems = failed_run_problems(info, order, arguments.failed_task)
    problems += callback_problems(
        read_log(arguments.dag_run_id, arguments.failed_task),
        failure_mail(kit_root),
        arguments.failed_task,
        arguments.run_id,
        arguments.scenario,
    )
    instance = info["tasks"].get(arguments.failed_task, {})
    tries = len(instance.get("tries", []))
    print(f"  {arguments.failed_task}: state={instance.get('state')} tries={tries}")
    return problems


# -- listeners (Decision 17) ----------------------------------------------


def airflow_pids(home: Path) -> set[int]:
    """Process group ids of the components started by scripts/airflow-up.sh."""
    groups: set[int] = set()
    for pid_file in (home / "run").glob("*.pid"):
        text = pid_file.read_text(encoding="utf-8").strip()
        if text.isdigit():
            groups.add(int(text))
    return groups


def process_details(pid: int) -> tuple[int, str]:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return (-1, "")
    # The process group is the fifth field, after "pid (comm) state ppid".
    return (int(stat.rsplit(")", 1)[1].split()[2]), cmdline.strip())


def sample_listeners(home: Path) -> JsonObject:
    output = subprocess.run(
        ["ss", "-ltnupH", "-p"], capture_output=True, text=True, check=True
    ).stdout
    groups = airflow_pids(home)
    venv = str(home / "venv") + "/"
    sockets: list[JsonObject] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        for pid in {int(match) for match in PID_PATTERN.findall(line)}:
            group, cmdline = process_details(pid)
            is_airflow = group in groups or venv in cmdline
            sockets.append(
                {
                    "proto": fields[0],
                    "local": fields[4],
                    "pid": pid,
                    "airflow": is_airflow,
                    "cmd": cmdline[:120],
                }
            )
    return {
        "at": datetime.now().astimezone().isoformat(),
        "groups": sorted(groups),
        "sockets": sockets,
    }


def listener_problems(snapshots: list[JsonObject], port: int) -> list[str]:
    allowed = f"127.0.0.1:{port}"
    problems: list[str] = []
    seen: set[str] = set()
    for snapshot in snapshots:
        for socket in snapshot["sockets"]:
            if not socket["airflow"]:
                continue
            seen.add(f"{socket['proto']} {socket['local']}")
            if socket["proto"] != "tcp" or socket["local"] != allowed:
                where = f"{socket['proto']} {socket['local']}"
                problems.append(
                    f"{snapshot['at']}: Airflow pid {socket['pid']} listens on {where} "
                    f"({socket['cmd']})"
                )
    if not snapshots:
        problems.append("no listener snapshots were taken")
    elif f"tcp {allowed}" not in seen:
        problems.append(f"the API server listener {allowed} was never attributed to Airflow")
    print(f"  {len(snapshots)} snapshots; Airflow listeners seen: {sorted(seen)}")
    return problems


# -- pin (Decision 13) ----------------------------------------------------


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        requirement = line.split("#", 1)[0].strip()
        if "==" in requirement and not requirement.startswith("-"):
            name, version = requirement.split("==", 1)
            found[canonical_name(name.split("[", 1)[0])] = version.split(";", 1)[0].strip()
    return found


def env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def pin_problems(kit_root: Path, home: Path) -> list[str]:
    problems: list[str] = []
    env = env_file_values(kit_root / "runtimes" / "airflow" / "airflow-version.env")
    want = {
        "AIRFLOW_VERSION": PINNED_AIRFLOW,
        "AIRFLOW_PYTHON_VERSION": PINNED_PYTHON,
        "AIRFLOW_CONSTRAINTS_URL": PINNED_CONSTRAINTS_URL,
    }
    for key, value in want.items():
        if env.get(key) != value:
            problems.append(f"airflow-version.env {key}={env.get(key)}, want {value}")
    python = home / "venv" / "bin" / "python"
    version = subprocess.run(
        [str(python), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not version.startswith(PINNED_PYTHON + "."):
        problems.append(f"venv Python is {version}, want {PINNED_PYTHON}.x")
    installed = pins(
        subprocess.run(
            [str(python), "-m", "pip", "freeze", "--all"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    )
    constraints = pins(
        (home / f"constraints-{PINNED_AIRFLOW}-{PINNED_PYTHON}.txt").read_text("utf-8")
    )
    if installed.get("apache-airflow") != PINNED_AIRFLOW:
        problems.append(
            f"installed apache-airflow {installed.get('apache-airflow')}, want {PINNED_AIRFLOW}"
        )
    reported = (
        subprocess.run(
            [str(home / "venv" / "bin" / "airflow"), "version"],
            capture_output=True,
            text=True,
            check=False,
        )
        .stdout.strip()
        .splitlines()
    )
    if not reported or reported[-1] != PINNED_AIRFLOW:
        problems.append(f"airflow version reports {reported[-1:]}, want {PINNED_AIRFLOW}")
    constrained = sorted(set(installed) & set(constraints))
    differing = [
        f"{name} {installed[name]} != {constraints[name]}"
        for name in constrained
        if installed[name] != constraints[name]
    ]
    problems += [
        f"installed package differs from the constraints file: {item}" for item in differing
    ]
    unconstrained = sorted(set(installed) - set(constraints))
    print(f"  Python {version}; apache-airflow {installed.get('apache-airflow')}")
    print(
        f"  {len(constrained) - len(differing)}/{len(constrained)} constrained packages match; "
        f"{len(unconstrained)} installed packages not in the constraints file: {unconstrained}"
    )
    return problems


# -- command line -----------------------------------------------------------


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kit-root", type=Path, default=Path(__file__).resolve().parents[2])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("owed-scenarios")
    commands.add_parser("labels").add_argument("--run-dir", type=Path, required=True)
    retry = commands.add_parser("retry")
    retry.add_argument("--run-dir", type=Path, required=True)
    retry.add_argument("--other-run-dir", type=Path, action="append", default=[])
    commands.add_parser("rerun").add_argument("--run-dir", type=Path, required=True)
    no_retry = commands.add_parser("no-retry")
    for name in ("--dag-run-id", "--failed-task", "--run-id", "--scenario"):
        no_retry.add_argument(name, required=True)
    commands.add_parser("listeners-sample").add_argument("--out", type=Path, required=True)
    commands.add_parser("listeners").add_argument("--snapshots", type=Path, required=True)
    commands.add_parser("pin")
    return parser.parse_args(argv)


def run_command(arguments: argparse.Namespace) -> list[str]:
    kit_root = arguments.kit_root.resolve()
    command = arguments.command
    if command == "labels":
        return labels_problems(arguments.run_dir, fetch_dag_run)
    if command == "retry":
        return retry_problems(kit_root, arguments.run_dir, arguments.other_run_dir, fetch_dag_run)
    if command == "rerun":
        return rerun_problems(kit_root, arguments.run_dir, fetch_dag_run)
    if command == "no-retry":
        return no_retry_problems(kit_root, arguments, fetch_dag_run, task_log)
    if command == "listeners":
        lines = arguments.snapshots.read_text(encoding="utf-8").splitlines()
        port = int(os.environ.get("PAYOPS_AIRFLOW_PORT", "8080"))
        return listener_problems([json.loads(line) for line in lines if line.strip()], port)
    return pin_problems(kit_root, airflow_home())


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    if arguments.command == "owed-scenarios":
        print("\n".join(owed_scenarios(arguments.kit_root.resolve())))
        return 0
    if arguments.command == "listeners-sample":
        with open(arguments.out, "a", encoding="utf-8") as snapshots:
            snapshots.write(json.dumps(sample_listeners(airflow_home())) + "\n")
        return 0
    problems = run_command(arguments)
    for problem in problems:
        print(f"  problem: {problem}")
    print(f"  {arguments.command}: {'pass' if not problems else f'{len(problems)} problem(s)'}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
