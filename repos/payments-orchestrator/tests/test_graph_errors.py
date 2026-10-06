"""Graph behavior and the parse errors raised for malformed Control-M definitions."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from orchestrator.graph import (
    WORKFLOW_NAME,
    GraphError,
    TaskGraph,
    agreement_problems,
    all_graphs,
    canonical_graph,
    controlm_dir,
    graph_from_json,
    graph_from_task_commands,
    graph_from_xml,
    load_task_commands,
    repository_for_task,
)

CONTROLM = Path(__file__).resolve().parents[1] / "controlm"


def _job(adds: list[str], waits: list[str] | None = None, reruns: Any = None) -> dict[str, Any]:
    job: dict[str, Any] = {
        "Type": "Job:Command",
        "eventsToAdd": {"Type": "AddEvents", "Events": [{"Event": e} for e in adds]},
    }
    if waits is not None:
        job["eventsToWaitFor"] = {"Type": "WaitForEvents", "Events": [{"Event": e} for e in waits]}
    if reruns is not None:
        job["RerunLimit"] = reruns
    return job


def _folder(**members: Any) -> dict[str, Any]:
    return {WORKFLOW_NAME: {"Type": "Folder", "ControlmServer": "CTM", **members}}


class _TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, name: str, document: Any) -> Path:
        return self.write(name, json.dumps(document))


class TestTaskGraph(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = TaskGraph(
            "test", ["a", "b", "c"], {("a", "b"), ("a", "c"), ("b", "c")}, {"b": 2}
        )

    def test_repr_summarizes_size(self) -> None:
        self.assertEqual(repr(self.graph), "TaskGraph(source='test', tasks=3, edges=3)")

    def test_predecessors_and_successors_are_sorted(self) -> None:
        self.assertEqual(self.graph.predecessors("c"), ["a", "b"])
        self.assertEqual(self.graph.successors("a"), ["b", "c"])
        self.assertEqual(self.graph.successors("c"), [])

    def test_successors_on_the_canonical_workflow(self) -> None:
        graph = canonical_graph(CONTROLM)
        self.assertEqual(graph.successors("post_pending_ledger"), ["reconcile_nostro_ledger"])
        self.assertEqual(graph.successors("archive_and_notify"), [])

    def test_retries_default_to_empty(self) -> None:
        self.assertEqual(TaskGraph("s", ["a"], set()).retries, {})

    def test_topological_order_breaks_ties_by_name(self) -> None:
        graph = TaskGraph("s", ["z", "y", "x"], {("z", "x")})
        self.assertEqual(graph.topological_order(), ["y", "z", "x"])

    def test_cycle_is_rejected(self) -> None:
        graph = TaskGraph("cyclic", ["a", "b", "c"], {("a", "b"), ("b", "a")})
        with self.assertRaisesRegex(GraphError, r"cyclic: dependency cycle among \['a', 'b'\]"):
            graph.topological_order()

    def test_identical_graphs_have_no_differences(self) -> None:
        other = TaskGraph("other", ["c", "b", "a"], set(self.graph.edges), {"b": 2})
        self.assertEqual(self.graph.differences(other), [])

    def test_differences_report_tasks_edges_and_retries(self) -> None:
        other = TaskGraph("other", ["a", "b", "d"], {("a", "b"), ("b", "d")}, {"b": 1, "a": 0})
        self.assertEqual(
            self.graph.differences(other),
            [
                "tasks only in test: ['c']",
                "tasks only in other: ['d']",
                "edges only in test: [('a', 'c'), ('b', 'c')]",
                "edges only in other: [('b', 'd')]",
                "retry limit for b: test=2 other=1",
            ],
        )

    def test_retry_difference_needs_both_sides_declared(self) -> None:
        other = TaskGraph("other", ["a", "b", "c"], set(self.graph.edges), {})
        self.assertEqual(self.graph.differences(other), [])


class TestGraphFromJson(_TempDirTest):
    def test_minimal_valid_folder(self) -> None:
        path = self.write_json(
            "ok.json",
            _folder(
                first=_job(["A-OK"]),
                second=_job([], ["A-OK"], {"Times": "3"}),
                third=_job([], None, "not-a-dict"),
                flow={"Type": "Flow", "Sequence": ["first", "second"]},
                Variables={"Type": "Job:Ignored"},
                stray="not a job",
                other={"Type": "Resource"},
            ),
        )
        graph = graph_from_json(path)
        self.assertEqual(graph.source, "json:ok.json")
        self.assertEqual(graph.tasks, ["first", "second", "third"])
        self.assertEqual(graph.edges, {("first", "second")})
        self.assertEqual(graph.retries, {"first": 0, "second": 3, "third": 0})

    def test_events_block_must_be_an_object_of_event_entries(self) -> None:
        job = _job([])
        job["eventsToAdd"] = {"Events": ["bare", {"NotEvent": 1}, {"Event": "A-OK"}]}
        path = self.write_json(
            "events.json",
            _folder(a=job, b=_job([], ["A-OK"]), c={"Type": "Job:X", "eventsToAdd": []}),
        )
        self.assertEqual(graph_from_json(path).edges, {("a", "b")})

    def test_missing_folder(self) -> None:
        path = self.write_json("missing.json", {"Defaults": {}})
        with self.assertRaisesRegex(GraphError, f"folder {WORKFLOW_NAME} not found"):
            graph_from_json(path)

    def test_folder_must_be_an_object(self) -> None:
        path = self.write_json("list.json", {WORKFLOW_NAME: []})
        with self.assertRaisesRegex(GraphError, "not found"):
            graph_from_json(path)

    def test_wrong_folder_type(self) -> None:
        path = self.write_json("type.json", {WORKFLOW_NAME: {"Type": "SimpleFolder"}})
        with self.assertRaisesRegex(GraphError, f"{WORKFLOW_NAME} is not a Folder"):
            graph_from_json(path)

    def test_no_jobs(self) -> None:
        path = self.write_json("empty.json", _folder(flow={"Type": "Flow", "Sequence": []}))
        with self.assertRaisesRegex(GraphError, f"no jobs found in {WORKFLOW_NAME}"):
            graph_from_json(path)

    def test_condition_added_twice(self) -> None:
        path = self.write_json("dup.json", _folder(a=_job(["X-OK"]), b=_job(["X-OK"])))
        with self.assertRaisesRegex(GraphError, "condition X-OK is added by both a and b"):
            graph_from_json(path)

    def test_wait_for_unknown_condition(self) -> None:
        path = self.write_json("orphan.json", _folder(a=_job([], ["NOBODY-OK"])))
        with self.assertRaisesRegex(GraphError, "a waits for NOBODY-OK which no job adds"):
            graph_from_json(path)

    def test_flow_references_unknown_job(self) -> None:
        path = self.write_json(
            "flow.json", _folder(a=_job([]), flow={"Type": "Flow", "Sequence": ["a", "ghost"]})
        )
        with self.assertRaisesRegex(GraphError, r"Flow references unknown jobs \['ghost'\]"):
            graph_from_json(path)

    def test_flow_without_matching_condition(self) -> None:
        path = self.write_json(
            "flow.json",
            _folder(a=_job([]), b=_job([]), flow={"Type": "Flow", "Sequence": ["a", "b"]}),
        )
        with self.assertRaisesRegex(
            GraphError, "Flow declares a -> b but no event condition creates that dependency"
        ):
            graph_from_json(path)

    def test_malformed_json_is_not_wrapped(self) -> None:
        path = self.write("broken.json", "{not json")
        with self.assertRaises(json.JSONDecodeError):
            graph_from_json(path)


class TestGraphFromXml(_TempDirTest):
    def test_minimal_valid_export(self) -> None:
        path = self.write(
            "ok.xml",
            "<DEFTABLE><FOLDER>"
            '<JOB JOBNAME="b" MAXRERUN=""><INCOND NAME="A-OK"/></JOB>'
            '<JOB JOBNAME="a" MAXRERUN="2"><OUTCOND NAME="A-OK" SIGN="+"/>'
            '<OUTCOND NAME="A-OK" SIGN="-"/><OUTCOND SIGN="+"/></JOB>'
            "</FOLDER></DEFTABLE>",
        )
        graph = graph_from_xml(path)
        self.assertEqual(graph.source, "xml:ok.xml")
        self.assertEqual(graph.tasks, ["a", "b"])
        self.assertEqual(graph.edges, {("a", "b")})
        self.assertEqual(graph.retries, {"a": 2, "b": 0})

    def test_no_jobs(self) -> None:
        path = self.write("empty.xml", "<DEFTABLE><FOLDER/></DEFTABLE>")
        with self.assertRaisesRegex(GraphError, "no JOB elements found"):
            graph_from_xml(path)

    def test_job_without_name(self) -> None:
        path = self.write("noname.xml", '<DEFTABLE><JOB JOBNAME="a"/><JOB/></DEFTABLE>')
        with self.assertRaisesRegex(GraphError, "a JOB element has no JOBNAME"):
            graph_from_xml(path)

    def test_incondition_nobody_produces(self) -> None:
        path = self.write(
            "orphan.xml", '<DEFTABLE><JOB JOBNAME="a"><INCOND NAME="X-OK"/></JOB></DEFTABLE>'
        )
        with self.assertRaisesRegex(GraphError, "a requires X-OK which no job produces"):
            graph_from_xml(path)

    def test_unnamed_incondition_is_rejected(self) -> None:
        path = self.write("unnamed.xml", '<DEFTABLE><JOB JOBNAME="a"><INCOND/></JOB></DEFTABLE>')
        with self.assertRaisesRegex(GraphError, "a requires None which no job produces"):
            graph_from_xml(path)


class TestTaskCommands(_TempDirTest):
    def test_no_tasks_declared(self) -> None:
        for name, document in (
            ("missing.json", {}),
            ("empty.json", {"tasks": []}),
            ("object.json", {"tasks": {"task": "a"}}),
        ):
            with self.subTest(name=name):
                path = self.write_json(name, document)
                with self.assertRaisesRegex(GraphError, "no tasks declared"):
                    load_task_commands(path)

    def test_graph_from_task_commands_defaults(self) -> None:
        path = self.write_json(
            "tc.json",
            {"tasks": [{"task": "b", "dependsOn": ["a"], "retryLimit": 1}, {"task": "a"}]},
        )
        graph = graph_from_task_commands(path)
        self.assertEqual(graph.source, "task-commands:tc.json")
        self.assertEqual(graph.tasks, ["a", "b"])
        self.assertEqual(graph.edges, {("a", "b")})
        self.assertEqual(graph.retries, {"a": 0, "b": 1})

    def test_repository_for_known_task(self) -> None:
        self.assertEqual(
            repository_for_task(CONTROLM / "task-commands.json", "post_pending_ledger"),
            "ledger-reconciliation",
        )


class TestDefinitionDirectory(_TempDirTest):
    def test_controlm_dir_defaults_to_this_repository(self) -> None:
        self.assertEqual(controlm_dir(), CONTROLM)
        self.assertEqual(controlm_dir(self.dir), self.dir / "controlm")

    def test_default_directory_loads_the_canonical_graph(self) -> None:
        graphs = all_graphs()
        self.assertEqual([g.source.split(":")[0] for g in graphs], ["json", "xml", "task-commands"])
        self.assertEqual(canonical_graph().edges, graphs[0].edges)
        self.assertEqual(agreement_problems(), [])

    def test_disagreeing_mapping_is_reported(self) -> None:
        for name in ("payments_reconciliation.json", "payments_reconciliation.xml"):
            shutil.copy(CONTROLM / name, self.dir / name)
        mapping = json.loads((CONTROLM / "task-commands.json").read_text(encoding="utf-8"))
        for entry in mapping["tasks"]:
            if entry["task"] == "post_pending_ledger":
                entry["retryLimit"] = 5
        self.write_json("task-commands.json", mapping)

        self.assertEqual(
            agreement_problems(self.dir),
            [
                "retry limit for post_pending_ledger: "
                "json:payments_reconciliation.json=2 task-commands:task-commands.json=5"
            ],
        )


if __name__ == "__main__":
    unittest.main()
