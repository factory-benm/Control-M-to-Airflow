#!/usr/bin/env python3
"""Compatibility harness for the Control-M-defined payments workflow.

This is NOT Control-M and never presents itself as Control-M. It is a
deterministic compatibility execution of the workflow that the Control-M
definitions declare. Every run it produces is stamped
``mode: compatibility-harness``.

What it does:

* derives the canonical task graph from the Control-M definitions, using the
  orchestrator repository's own parser so the runtime cannot drift from the
  scheduler definition
* executes tasks in topological order, enforcing dependencies
* passes scheduler variables through to each task
* captures stdout, stderr, exit code, start time, and end time per attempt
* applies the retry limits declared in the definitions
* tolerates exactly one deterministic expected failure, and only when the
  active scenario declares that fault
* stops on any unexpected failure
* writes normalized task and run manifests

What it deliberately does not do: implement Control-M scheduling, calendars,
resource pools, SLA management, workload policies, or agent orchestration. It
covers only what this workflow's twelve tasks observably require.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator.graph import TaskGraph

RUNTIME_MODE = "compatibility-harness"
MANIFEST_VERSION = "1.0.0"
WORKFLOW = "PAYOPS_CROSS_BORDER_RECONCILIATION"
EXPECTED_FAULT_EXIT = 3

CANONICAL_COUNTS = (
    "received",
    "rejected",
    "duplicatesRemoved",
    "accepted",
    "cutoffAdjusted",
    "posted",
    "matched",
    "broken",
)

RUN_SUBDIRS = ("input", "stages", "logs", "ledger", "output")

JsonObject = dict[str, Any]


class HarnessError(Exception):
    """A harness-level failure, distinct from a task failure."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(131072), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> JsonObject:
    document: JsonObject = json.loads(Path(path).read_text(encoding="utf-8"))
    return document


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def import_orchestrator_graph(repos_root: Path) -> ModuleType:
    """Load the orchestrator's graph parser from the estate itself."""
    src = repos_root / "payments-orchestrator" / "src"
    if not (src / "orchestrator" / "graph.py").is_file():
        raise HarnessError(
            f"cannot find the orchestrator graph parser under {src}. Check --repos-root."
        )
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    import orchestrator.graph as graph_module

    return graph_module


def read_jsonl(path: Path) -> list[JsonObject]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def ledger_rows(run_dir: Path) -> tuple[int, int]:
    """Return (total rows, distinct payment_id) from the ledger, or (0, 0)."""
    database = run_dir / "ledger" / "ledger.sqlite3"
    if not database.is_file():
        return (0, 0)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        cursor = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT payment_id) FROM ledger_entry WHERE state = 'posted'"
        )
        total, distinct = cursor.fetchone()
        return (int(total), int(distinct))
    except sqlite3.Error:
        return (0, 0)
    finally:
        connection.close()


class Harness:
    def __init__(
        self,
        kit_root: Path,
        repos_root: Path,
        run_root: Path,
        scenario_id: str,
        run_id: str,
        workbench_note: JsonObject | None = None,
    ) -> None:
        self.kit_root = kit_root.resolve()
        self.repos_root = repos_root.resolve()
        self.run_dir = (run_root / run_id).resolve()
        self.scenario_id = scenario_id
        self.run_id = run_id
        self.workbench_note = workbench_note or {
            "attempted": False,
            "available": False,
            "reason": "not evaluated by the harness; see scripts/controlm-up.sh",
        }

        self.scenario_dir = self.kit_root / "fixtures" / "scenarios" / scenario_id
        if not self.scenario_dir.is_dir():
            raise HarnessError(f"unknown scenario '{scenario_id}' ({self.scenario_dir} missing)")
        self.scenario = load_json(self.scenario_dir / "scenario.json")

        self.graph_module = import_orchestrator_graph(self.repos_root)
        controlm = self.repos_root / "payments-orchestrator" / "controlm"
        problems = self.graph_module.agreement_problems(controlm)
        if problems:
            raise HarnessError(
                "the Control-M definition artifacts disagree, refusing to run:\n  "
                + "\n  ".join(problems)
            )
        self.graph: TaskGraph = self.graph_module.canonical_graph(controlm)
        self.task_commands: dict[str, JsonObject] = {
            entry["task"]: entry
            for entry in self.graph_module.load_task_commands(controlm / "task-commands.json")
        }
        self.wrapper = self.repos_root / "payments-orchestrator" / "scripts" / "run-task.sh"
        if not self.wrapper.is_file():
            raise HarnessError(f"orchestrator wrapper not found at {self.wrapper}")

        self.fixed_clock: str = self.scenario["clock"]["fixedUtc"]
        self.fault: JsonObject | None = self.scenario.get("faultInjection")
        self.rerun: JsonObject | None = self.scenario.get("rerun")

        self.exit_sequence: list[JsonObject] = []
        self.passes: list[JsonObject] = []
        self.counts: dict[str, int] = {}
        self.ledger_after_pass: list[int] = []

    # -- setup ------------------------------------------------------------

    def prepare_run_dir(self, fresh: bool) -> None:
        if fresh and self.run_dir.exists():
            # Bounded and explicit: only ever the run directory we own.
            if self.run_dir.parent.name != "runs":
                raise HarnessError(
                    f"refusing to clear {self.run_dir}: not inside a runs/ directory"
                )
            shutil.rmtree(self.run_dir)
        for name in RUN_SUBDIRS:
            (self.run_dir / name).mkdir(parents=True, exist_ok=True)

    def write_runtime_metadata(self) -> None:
        write_json(
            self.run_dir / "runtime.json",
            {
                "mode": RUNTIME_MODE,
                "modeLabel": "Compatibility execution of the Control-M-defined workflow",
                "isControlM": False,
                "disclosure": (
                    "This run was produced by the deterministic compatibility harness, "
                    "not by BMC Control-M. It executes the same task graph and the same "
                    "repository commands declared in the Control-M definitions."
                ),
                "definitionSource": str(
                    Path("repos/payments-orchestrator/controlm/payments_reconciliation.json")
                ),
                "graph": {"tasks": len(self.graph.tasks), "edges": len(self.graph.edges)},
                "workflow": WORKFLOW,
                "scenario": self.scenario_id,
                "runId": self.run_id,
                "fixedClock": self.fixed_clock,
                "host": {"platform": sys.platform, "architecture": os.uname().machine},
                "workbench": self.workbench_note,
                "selectedAt": utc_now(),
                "harnessLimitations": [
                    "Does not implement Control-M scheduling, calendars as a scheduler service, "
                    "resource pools, SLA management, or agent orchestration.",
                    "Applies only the retry limits and conditions declared in the definitions.",
                    "Executes locally as the invoking user, not through a Control-M agent.",
                ],
            },
        )

    # -- execution --------------------------------------------------------

    def source_hashes(self) -> dict[str, str]:
        return {
            "fixtures/input/payments.csv": sha256_file(
                self.scenario_dir / "input" / "payments.csv"
            ),
            "fixtures/reference/ledger.csv": sha256_file(
                self.scenario_dir / "reference" / "ledger.csv"
            ),
        }

    def run_task(self, task: str, attempt: int, pass_number: int) -> JsonObject:
        # Scope captures by pass as well as attempt. A declared rerun executes
        # the whole graph again with every task back at attempt 1, so attempt
        # alone would let the second pass overwrite the first pass's evidence.
        # Build by concatenation, never with Path.with_suffix: ".attempt1" looks
        # like a suffix and would be replaced.
        stdout_name = f"{task}.pass{pass_number}.attempt{attempt}.stdout"
        stderr_name = f"{task}.pass{pass_number}.attempt{attempt}.stderr"
        command = [
            str(self.wrapper),
            "--task",
            task,
            "--run-dir",
            str(self.run_dir),
            "--scenario",
            self.scenario_id,
            "--run-id",
            self.run_id,
            "--kit-root",
            str(self.kit_root),
            "--now",
            self.fixed_clock,
            "--attempt",
            str(attempt),
        ]
        environment = dict(os.environ)
        environment.update(
            {
                "PAYOPS_KIT_ROOT": str(self.kit_root),
                "PAYOPS_FIXED_CLOCK": self.fixed_clock,
                "PAYOPS_ATTEMPT": str(attempt),
                "PAYOPS_RUN_ID": self.run_id,
                "PAYOPS_SCENARIO": self.scenario_id,
            }
        )

        started = utc_now()
        started_monotonic = datetime.now(UTC)
        completed_process = subprocess.run(
            command, capture_output=True, text=True, env=environment, check=False
        )
        finished = utc_now()
        duration_ms = int((datetime.now(UTC) - started_monotonic).total_seconds() * 1000)

        (self.run_dir / "logs" / stdout_name).write_text(completed_process.stdout, encoding="utf-8")
        (self.run_dir / "logs" / stderr_name).write_text(completed_process.stderr, encoding="utf-8")

        result: JsonObject | None = None
        if completed_process.stdout.strip():
            try:
                result = json.loads(completed_process.stdout)
            except json.JSONDecodeError:
                result = None

        record: JsonObject = {
            "task": task,
            "repository": self.task_commands[task]["repository"],
            "pass": pass_number,
            "attempt": attempt,
            "exitCode": completed_process.returncode,
            "status": "success" if completed_process.returncode == 0 else "failed",
            "startedAt": started,
            "completedAt": finished,
            "durationMs": duration_ms,
            "counts": (result or {}).get("counts", {}),
            "metrics": (result or {}).get("metrics", {}),
            "artifacts": (result or {}).get("artifacts", []),
            "logs": {
                "stdout": f"logs/{stdout_name}",
                "stderr": f"logs/{stderr_name}",
            },
            "resultParsed": result is not None,
        }
        if result is None and completed_process.returncode == 0:
            raise HarnessError(
                f"{task} exited 0 but did not emit a parseable JSON result on stdout. "
                f"See {record['logs']['stdout']}"
            )
        if result and "error" in result:
            record["error"] = result["error"]

        self.exit_sequence.append(
            {"task": task, "attempt": attempt, "exitCode": completed_process.returncode}
        )
        return record

    def fault_is_expected(self, task: str, attempt: int) -> bool:
        return bool(
            self.fault
            and self.fault.get("task") == task
            and int(self.fault.get("attempt", 0)) == attempt
        )

    def execute_pass(self, pass_number: int) -> JsonObject:
        tasks: list[JsonObject] = []
        order = self.graph.topological_order()
        print(f"harness: pass {pass_number} executing {len(order)} tasks", file=sys.stderr)

        for task in order:
            retry_limit = int(self.task_commands[task].get("retryLimit", 0))
            attempt = 1
            while True:
                record = self.run_task(task, attempt, pass_number)
                tasks.append(record)
                if record["exitCode"] == 0:
                    for key, value in record["counts"].items():
                        self.counts[key] = value
                    break

                expected = record["exitCode"] == EXPECTED_FAULT_EXIT and self.fault_is_expected(
                    task, attempt
                )
                if not expected:
                    raise HarnessError(
                        f"{task} failed unexpectedly on attempt {attempt} with exit "
                        f"{record['exitCode']}. The scenario does not declare this "
                        f"failure. Stopping. See {record['logs']['stderr']}"
                    )
                if attempt > retry_limit:
                    raise HarnessError(
                        f"{task} hit its declared retry limit of {retry_limit} without completing"
                    )
                print(
                    f"harness: {task} attempt {attempt} raised the declared "
                    f"deterministic fault, retrying as attempt {attempt + 1}",
                    file=sys.stderr,
                )
                attempt += 1

        total, _distinct = ledger_rows(self.run_dir)
        self.ledger_after_pass.append(total)
        return {"pass": pass_number, "tasks": tasks}

    # -- observations -----------------------------------------------------

    def observed_invariants(
        self, before: dict[str, str], after: dict[str, str]
    ) -> list[JsonObject]:
        results: list[JsonObject] = []

        def record(identifier: str, ok: bool, detail: str) -> None:
            results.append(
                {
                    "id": identifier,
                    "status": "pass" if ok else "fail",
                    "detail": detail,
                    "provenance": "observed-run",
                }
            )

        counts = self.counts
        missing = [key for key in CANONICAL_COUNTS if key not in counts]
        if missing:
            record("INV-COUNTS-RECONCILE", False, f"missing counts: {missing}")
        else:
            first = counts["received"] == (
                counts["rejected"] + counts["duplicatesRemoved"] + counts["accepted"]
            )
            second = counts["accepted"] == counts["posted"]
            third = counts["posted"] == counts["matched"] + counts["broken"]
            record(
                "INV-COUNTS-RECONCILE",
                first and second and third,
                f"received={counts['received']} rejected={counts['rejected']} "
                f"duplicatesRemoved={counts['duplicatesRemoved']} accepted={counts['accepted']} "
                f"posted={counts['posted']} matched={counts['matched']} broken={counts['broken']}",
            )

        record(
            "INV-INPUT-IMMUTABLE",
            before == after,
            "source fixture hashes unchanged"
            if before == after
            else f"source fixture changed during the run: {before} -> {after}",
        )

        total, distinct = ledger_rows(self.run_dir)
        record(
            "INV-SINGLE-POSTING",
            total == distinct and total == counts.get("posted", -1),
            f"ledger rows={total} distinct payment_id={distinct} "
            f"posted count={counts.get('posted')}",
        )

        cutoff_records = read_jsonl(self.run_dir / "stages" / "cutoff-payments.jsonl")
        traceable = bool(cutoff_records) and all(
            row.get("run_id") == self.run_id
            and row.get("scenario") == self.scenario_id
            and row.get("source_file_sha256")
            and row.get("payment_id")
            for row in cutoff_records
        )
        record(
            "INV-TRACEABLE-OUTPUT",
            traceable,
            f"{len(cutoff_records)} enriched records carry run_id, scenario, "
            "source hash, and payment_id",
        )

        matched_ids = {
            row["payment_id"] for row in read_jsonl(self.run_dir / "stages" / "matched.jsonl")
        }
        break_ids = {
            row["payment_id"] for row in read_jsonl(self.run_dir / "stages" / "breaks.jsonl")
        }
        overlap = matched_ids & break_ids
        record(
            "INV-BREAKS-EXCLUDED",
            not overlap and len(matched_ids) == counts.get("matched", -1),
            f"matched={len(matched_ids)} broken={len(break_ids)} overlap={sorted(overlap)}",
        )

        cutoff_summary = self.run_dir / "stages" / "cutoff.json"
        if cutoff_summary.is_file():
            summary = load_json(cutoff_summary)
            declared = summary.get("calendarId") or summary.get("calendar_id")
            timezone_used = summary.get("cutoffTimezone") or summary.get("cutoff_timezone")
            expected_zone = self.scenario["cutoff"]["timezone"]
            record(
                "INV-CUTOFF-CALENDAR",
                bool(declared) and timezone_used == expected_zone,
                f"calendar={declared} timezone={timezone_used} expected={expected_zone}",
            )

        if self.rerun:
            expected_rows = int(self.rerun.get("expectedPostedAfterRerun", -1))
            final_rows = self.ledger_after_pass[-1] if self.ledger_after_pass else -1
            if len(self.ledger_after_pass) >= 2:
                added = self.ledger_after_pass[-1] - self.ledger_after_pass[-2]
                expected_added = int(self.rerun.get("expectedNewLedgerRowsOnRerun", 0))
                record(
                    "INV-RETRY-COMPLETES",
                    final_rows == expected_rows and added == expected_added,
                    f"ledger rows per pass={self.ledger_after_pass} added on rerun={added} "
                    f"expected added={expected_added} expected final={expected_rows}",
                )
            else:
                record(
                    "INV-RETRY-COMPLETES",
                    final_rows == expected_rows,
                    f"ledger rows after recovery={final_rows} expected={expected_rows}",
                )

        return sorted(results, key=lambda item: item["id"])

    def output_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for relative in sorted(
            [
                "output/settlement-report.json",
                "output/settlement-report.csv",
                "output/archive-manifest.json",
                "output/notification.json",
                "stages/normalized-payments.jsonl",
                "stages/validated-payments.jsonl",
                "stages/rejections.jsonl",
                "stages/deduplicated-payments.jsonl",
                "stages/duplicates.jsonl",
                "stages/enriched-payments.jsonl",
                "stages/cutoff-payments.jsonl",
                "stages/matched.jsonl",
                "stages/breaks.jsonl",
                "stages/classified-breaks.jsonl",
            ]
        ):
            candidate = self.run_dir / relative
            if candidate.is_file():
                hashes[relative] = sha256_file(candidate)
        return hashes

    # -- orchestration ----------------------------------------------------

    def run(self, fresh: bool = True) -> JsonObject:
        started = utc_now()
        self.prepare_run_dir(fresh)
        self.write_runtime_metadata()
        before = self.source_hashes()

        status = "success"
        failure: str | None = None
        try:
            self.passes.append(self.execute_pass(1))
            # A declared rerun with no injected fault means the whole graph runs
            # again, which is how duplicate-retry proves ledger cardinality is
            # stable. A rerun WITH a fault is handled by task-level retry.
            if self.rerun and self.rerun.get("required") and not self.fault:
                self.passes.append(self.execute_pass(2))
        except HarnessError as error:
            status = "failed"
            failure = str(error)
            print(f"harness: {error}", file=sys.stderr)

        after = self.source_hashes()
        manifest: JsonObject = {
            "manifestVersion": MANIFEST_VERSION,
            "workflow": WORKFLOW,
            "runId": self.run_id,
            "scenario": self.scenario_id,
            "scenarioVersion": self.scenario.get("scenarioVersion"),
            "runtime": {
                "mode": RUNTIME_MODE,
                "isControlM": False,
                "fixedClock": self.fixed_clock,
            },
            "status": status,
            "counts": {key: self.counts[key] for key in CANONICAL_COUNTS if key in self.counts},
            "passes": self.passes,
            "exitSequence": self.exit_sequence,
            "ledgerRowsAfterPass": self.ledger_after_pass,
            "inputHashes": before,
            "outputHashes": self.output_hashes(),
            "invariants": self.observed_invariants(before, after),
            "provenance": "observed-run",
            "startedAt": started,
            "completedAt": utc_now(),
        }
        if failure:
            manifest["failure"] = failure

        write_json(self.run_dir / "run-manifest.json", manifest)
        return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--kit-root", required=True, type=Path)
    parser.add_argument(
        "--repos-root",
        type=Path,
        help="Directory holding the eight repositories. Defaults to <kit-root>/repos.",
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        help="Where run directories are created. Defaults to <kit-root>/workspace/runtime/runs.",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Do not clear an existing run directory before executing.",
    )
    parser.add_argument("--workbench-note", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()

    kit_root = arguments.kit_root.resolve()
    repos_root = (arguments.repos_root or kit_root / "repos").resolve()
    run_root = (arguments.run_root or kit_root / "workspace" / "runtime" / "runs").resolve()

    workbench_note = None
    if arguments.workbench_note and arguments.workbench_note.is_file():
        workbench_note = load_json(arguments.workbench_note)

    try:
        harness = Harness(
            kit_root=kit_root,
            repos_root=repos_root,
            run_root=run_root,
            scenario_id=arguments.scenario,
            run_id=arguments.run_id,
            workbench_note=workbench_note,
        )
        manifest = harness.run(fresh=not arguments.keep_existing)
    except HarnessError as error:
        print(f"harness: {error}", file=sys.stderr)
        return 1

    failed_invariants = [item["id"] for item in manifest["invariants"] if item["status"] != "pass"]
    print(
        f"harness: scenario={manifest['scenario']} status={manifest['status']} "
        f"mode={RUNTIME_MODE} counts={manifest['counts']}",
        file=sys.stderr,
    )
    if failed_invariants:
        print(f"harness: failing invariants {failed_invariants}", file=sys.stderr)

    print(json.dumps(manifest, indent=2, sort_keys=True))
    if manifest["status"] != "success" or failed_invariants:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
