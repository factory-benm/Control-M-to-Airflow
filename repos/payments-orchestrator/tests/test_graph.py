"""Unit checks for the Control-M definitions in this repository."""

import unittest
from pathlib import Path

from orchestrator.graph import (
    GraphError,
    agreement_problems,
    condition_for,
    graph_from_json,
    graph_from_task_commands,
    graph_from_xml,
    load_task_commands,
    repository_for_task,
)

CONTROLM = Path(__file__).resolve().parents[1] / "controlm"
EXPECTED_TASK_COUNT = 12


class TestJobsAsCode(unittest.TestCase):
    def test_json_declares_twelve_jobs(self) -> None:
        graph = graph_from_json(CONTROLM / "payments_reconciliation.json")
        self.assertEqual(len(graph.tasks), EXPECTED_TASK_COUNT)

    def test_json_graph_is_acyclic_and_linear(self) -> None:
        graph = graph_from_json(CONTROLM / "payments_reconciliation.json")
        order = graph.topological_order()
        self.assertEqual(len(order), EXPECTED_TASK_COUNT)
        self.assertEqual(order[0], "watch_inbound_files")
        self.assertEqual(order[-1], "archive_and_notify")
        self.assertEqual(len(graph.edges), EXPECTED_TASK_COUNT - 1)

    def test_ledger_posting_is_the_only_job_with_retries(self) -> None:
        graph = graph_from_json(CONTROLM / "payments_reconciliation.json")
        with_retries = {task: n for task, n in graph.retries.items() if n > 0}
        self.assertEqual(with_retries, {"post_pending_ledger": 2})


class TestLegacyExport(unittest.TestCase):
    def test_xml_export_parses(self) -> None:
        graph = graph_from_xml(CONTROLM / "payments_reconciliation.xml")
        self.assertEqual(len(graph.tasks), EXPECTED_TASK_COUNT)


class TestTaskMapping(unittest.TestCase):
    def test_every_task_maps_to_a_repository(self) -> None:
        tasks = load_task_commands(CONTROLM / "task-commands.json")
        self.assertEqual(len(tasks), EXPECTED_TASK_COUNT)
        for entry in tasks:
            self.assertTrue(entry["repository"], entry["task"])

    def test_condition_naming_matches_the_mapping(self) -> None:
        for entry in load_task_commands(CONTROLM / "task-commands.json"):
            self.assertEqual(entry["outputCondition"], condition_for(entry["task"]))

    def test_unknown_task_is_rejected(self) -> None:
        with self.assertRaises(GraphError):
            repository_for_task(CONTROLM / "task-commands.json", "not_a_real_task")

    def test_mapping_orders_are_contiguous(self) -> None:
        orders = sorted(
            entry["order"] for entry in load_task_commands(CONTROLM / "task-commands.json")
        )
        self.assertEqual(orders, list(range(1, EXPECTED_TASK_COUNT + 1)))


class TestDefinitionsAgree(unittest.TestCase):
    def test_json_xml_and_mapping_describe_the_same_workflow(self) -> None:
        problems = agreement_problems(CONTROLM)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_mapping_graph_is_acyclic(self) -> None:
        graph = graph_from_task_commands(CONTROLM / "task-commands.json")
        self.assertEqual(len(graph.topological_order()), EXPECTED_TASK_COUNT)


if __name__ == "__main__":
    unittest.main()
