"""DAG structure (docs/migration-design.md section 6, check 1).

Needs the Airflow venv: scripts/test-airflow-equivalence.sh structure. The
expected task names and edges are written out here, and are also compared with
each of the three Control-M definition parsers, so a bug shared by the DAG and
a parser cannot hide a wrong graph.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import sys
import unittest
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from types import ModuleType

from airflow.dag_processing.dagbag import DagBag

KIT_ROOT = Path(__file__).resolve().parents[3]
DAGS = KIT_ROOT / "runtimes" / "airflow" / "dags"
DAG_FILE = DAGS / "payops_cross_border_reconciliation.py"
CONTROLM = KIT_ROOT / "repos" / "payments-orchestrator" / "controlm"
DAG_ID = "PAYOPS_CROSS_BORDER_RECONCILIATION"

# The twelve Control-M jobs, in condition order (AGENTS.md, JSON Flow).
TASKS = [
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
EDGES = set(pairwise(TASKS))


@functools.cache
def load_dag_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("payops_dag_under_test", DAG_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses look their module up in sys.modules while the file executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def kahn_order(tasks: list[str], edges: set[tuple[str, str]]) -> list[str]:
    remaining = {task: {a for a, b in edges if b == task} for task in tasks}
    order: list[str] = []
    while remaining:
        ready = sorted(task for task, deps in remaining.items() if not deps)
        if not ready:
            raise AssertionError(f"cycle among {sorted(remaining)}")
        for task in ready:
            order.append(task)
            del remaining[task]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


class TestDagStructure(unittest.TestCase):
    bag: DagBag
    module: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.bag = DagBag(dag_folder=str(DAGS))
        cls.module = load_dag_module()

    def dag_edges(self) -> set[tuple[str, str]]:
        dag = self.bag.dags[DAG_ID]
        return {
            (upstream, task.task_id) for task in dag.tasks for upstream in task.upstream_task_ids
        }

    def test_dag_imports_without_errors(self) -> None:
        self.assertEqual(self.bag.import_errors, {})
        self.assertEqual(sorted(self.bag.dags), [DAG_ID])

    def test_tasks_are_the_twelve_control_m_jobs(self) -> None:
        dag = self.bag.dags[DAG_ID]
        self.assertEqual(len(dag.tasks), 12)
        self.assertEqual(sorted(dag.task_ids), sorted(TASKS))

    def test_edges_are_the_eleven_conditions(self) -> None:
        self.assertEqual(len(EDGES), 11)
        self.assertEqual(self.dag_edges(), EDGES)

    def test_edges_equal_each_definition_parser(self) -> None:
        sys.path.insert(0, str(KIT_ROOT / "repos" / "payments-orchestrator" / "src"))
        from orchestrator.graph import graph_from_json, graph_from_task_commands, graph_from_xml

        parsed = {
            "json": graph_from_json(CONTROLM / "payments_reconciliation.json"),
            "xml": graph_from_xml(CONTROLM / "payments_reconciliation.xml"),
            "task-commands": graph_from_task_commands(CONTROLM / "task-commands.json"),
        }
        for source, graph in parsed.items():
            with self.subTest(source=source):
                self.assertEqual(self.dag_edges(), graph.edges)
                self.assertEqual(sorted(self.bag.dags[DAG_ID].task_ids), sorted(graph.tasks))

    def test_graph_has_no_cycles(self) -> None:
        dag = self.bag.dags[DAG_ID]
        self.assertEqual(kahn_order(list(dag.task_ids), self.dag_edges()), TASKS)

    def test_only_post_pending_ledger_retries(self) -> None:
        dag = self.bag.dags[DAG_ID]
        for task in dag.tasks:
            with self.subTest(task=task.task_id):
                if task.task_id == "post_pending_ledger":
                    self.assertEqual(task.retries, 2)
                    self.assertEqual(task.retry_delay, timedelta(seconds=60))
                else:
                    self.assertEqual(task.retries, 0)
                self.assertFalse(task.retry_exponential_backoff)

    def test_every_task_has_the_failure_callback(self) -> None:
        dag = self.bag.dags[DAG_ID]
        for task in dag.tasks:
            with self.subTest(task=task.task_id):
                callbacks = task.on_failure_callback
                names = [
                    c.__name__ for c in (callbacks if isinstance(callbacks, list) else [callbacks])
                ]
                self.assertEqual(names, ["notify_failure"])

    def test_dag_settings(self) -> None:
        dag = self.bag.dags[DAG_ID]
        self.assertTrue(dag.is_paused_upon_creation)
        self.assertEqual(dag.max_active_runs, 1)
        self.assertFalse(dag.catchup)
        self.assertEqual(set(dag.tags), {"PAYOPS", "CROSS_BORDER_RECONCILIATION"})
        self.assertEqual(str(dag.timezone), "Asia/Singapore")
        self.assertEqual(dag.description, "Cross-border payments reconciliation batch.")

    def test_tasks_run_the_wrapper_through_run_payops_task(self) -> None:
        dag = self.bag.dags[DAG_ID]
        for task in dag.tasks:
            with self.subTest(task=task.task_id):
                self.assertEqual(type(task).__name__, "PythonOperator")
                self.assertEqual(task.python_callable.__name__, "run_payops_task")
                self.assertEqual(task.op_kwargs, {"task_name": task.task_id})


class TestTaskCommands(unittest.TestCase):
    module: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dag_module()

    def run_values(self, kit_root: Path) -> object:
        return self.module.RunValues(
            kit_root=kit_root,
            scenario="happy-path",
            run_id="unit-run",
            fixed_clock="2026-03-16T09:30:00Z",
            pass_number=1,
            fault=None,
        )

    def test_command_is_the_harness_argument_list(self) -> None:
        kit = Path("/opt/kit")
        for task in TASKS:
            with self.subTest(task=task):
                argv = self.module.build_command(
                    self.module.WORKFLOW.commands[task], self.run_values(kit), 2
                )
                expected = [
                    "/opt/kit/repos/payments-orchestrator/scripts/run-task.sh",
                    "--task",
                    task,
                    "--run-dir",
                    "/opt/kit/workspace/runtime/runs/unit-run",
                    "--scenario",
                    "happy-path",
                    "--run-id",
                    "unit-run",
                    "--kit-root",
                    "/opt/kit",
                    "--now",
                    "2026-03-16T09:30:00Z",
                    "--attempt",
                    "2",
                ]
                self.assertEqual(argv, expected)

    def test_command_is_the_json_command_resolved(self) -> None:
        jobs = json.loads((CONTROLM / "payments_reconciliation.json").read_text("utf-8"))[DAG_ID]
        values = {
            "%%ORCHESTRATOR_HOME": "/opt/kit/repos/payments-orchestrator",
            "%%RUN_DIR": "/opt/kit/workspace/runtime/runs/unit-run",
            "%%SCENARIO": "happy-path",
            "%%RUN_ID": "unit-run",
            "%%KIT_ROOT": "/opt/kit",
            "%%FIXED_CLOCK": "2026-03-16T09:30:00Z",
            "%%ATTEMPT": "1",
        }
        for task in TASKS:
            with self.subTest(task=task):
                command = jobs[task]["Command"]
                for name in sorted(values, key=len, reverse=True):
                    command = command.replace(name, values[name])
                argv = self.module.build_command(
                    self.module.WORKFLOW.commands[task], self.run_values(Path("/opt/kit")), 1
                )
                self.assertEqual(argv, command.split())

    def test_paths_with_spaces_stay_one_argument(self) -> None:
        argv = self.module.build_command(
            self.module.WORKFLOW.commands["post_pending_ledger"],
            self.run_values(Path("/opt/my kit")),
            1,
        )
        self.assertEqual(argv[0], "/opt/my kit/repos/payments-orchestrator/scripts/run-task.sh")
        self.assertEqual(argv[argv.index("--kit-root") + 1], "/opt/my kit")
        self.assertEqual(len(argv), 15)


if __name__ == "__main__":
    unittest.main()
