"""Derive the canonical task graph from the Control-M definitions.

Three artifacts describe the same workflow:

* ``controlm/payments_reconciliation.json`` -- Automation API jobs as code.
  This is the authority.
* ``controlm/payments_reconciliation.xml`` -- a representative legacy export,
  retained as a converter input fixture.
* ``controlm/task-commands.json`` -- the task to repository mapping used by the
  scheduler wrapper.

Anything that executes the workflow reads the graph from here so the runtime and
the scheduler definition cannot drift apart silently.

Standard library only.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from pathlib import Path

CONDITION_PREFIX = "PAYOPS-"
CONDITION_SUFFIX = "-OK"
WORKFLOW_NAME = "PAYOPS_CROSS_BORDER_RECONCILIATION"

# Keys inside a Control-M folder that are not jobs.
_NON_JOB_FOLDER_KEYS = {
    "Type",
    "ControlmServer",
    "OrderMethod",
    "Comment",
    "Application",
    "SubApplication",
    "RunAs",
    "Host",
    "Variables",
    "AdjustEvents",
    "CreatedBy",
}

_FLOW_TYPE = "Flow"


class GraphError(Exception):
    """Raised when a definition cannot be parsed into a usable graph."""


class TaskGraph:
    """A canonical, comparable view of the workflow."""

    def __init__(self, source: str, tasks: list[str], edges: set[tuple[str, str]],
                 retries: dict[str, int] | None = None) -> None:
        self.source = source
        self.tasks = list(tasks)
        self.edges = set(edges)
        self.retries = dict(retries or {})

    def __repr__(self) -> str:
        return f"TaskGraph(source={self.source!r}, tasks={len(self.tasks)}, edges={len(self.edges)})"

    @property
    def task_set(self) -> set[str]:
        return set(self.tasks)

    def predecessors(self, task: str) -> list[str]:
        return sorted(source for source, target in self.edges if target == task)

    def successors(self, task: str) -> list[str]:
        return sorted(target for source, target in self.edges if source == task)

    def topological_order(self) -> list[str]:
        """Kahn's algorithm with deterministic tie-breaking by task name."""
        remaining = {task: set(self.predecessors(task)) for task in self.tasks}
        order: list[str] = []
        while remaining:
            ready = sorted(task for task, deps in remaining.items() if not deps)
            if not ready:
                raise GraphError(
                    f"{self.source}: dependency cycle among {sorted(remaining)}"
                )
            for task in ready:
                order.append(task)
                del remaining[task]
            for deps in remaining.values():
                deps.difference_update(ready)
        return order

    def differences(self, other: "TaskGraph") -> list[str]:
        """Human-readable differences against another graph. Empty means agreement."""
        problems: list[str] = []

        only_self = self.task_set - other.task_set
        only_other = other.task_set - self.task_set
        if only_self:
            problems.append(f"tasks only in {self.source}: {sorted(only_self)}")
        if only_other:
            problems.append(f"tasks only in {other.source}: {sorted(only_other)}")

        edges_only_self = self.edges - other.edges
        edges_only_other = other.edges - self.edges
        if edges_only_self:
            problems.append(f"edges only in {self.source}: {sorted(edges_only_self)}")
        if edges_only_other:
            problems.append(f"edges only in {other.source}: {sorted(edges_only_other)}")

        shared = self.task_set & other.task_set
        for task in sorted(shared):
            mine = self.retries.get(task)
            theirs = other.retries.get(task)
            if mine is not None and theirs is not None and mine != theirs:
                problems.append(
                    f"retry limit for {task}: {self.source}={mine} {other.source}={theirs}"
                )
        return problems


def condition_for(task: str) -> str:
    return f"{CONDITION_PREFIX}{task.upper()}{CONDITION_SUFFIX}"


def _events(job: dict, key: str) -> list[str]:
    block = job.get(key)
    if not isinstance(block, dict):
        return []
    return [
        event["Event"]
        for event in block.get("Events", [])
        if isinstance(event, dict) and "Event" in event
    ]


def graph_from_json(path: Path) -> TaskGraph:
    """Build the graph from Automation API jobs-as-code.

    Edges come from event conditions: a task that waits for a condition depends
    on the task that adds it. The declared ``Flow`` sequence is cross-checked
    against those edges.
    """
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    folder = document.get(WORKFLOW_NAME)
    if not isinstance(folder, dict):
        raise GraphError(f"{path}: folder {WORKFLOW_NAME} not found")
    if folder.get("Type") != "Folder":
        raise GraphError(f"{path}: {WORKFLOW_NAME} is not a Folder")

    jobs: dict[str, dict] = {}
    flow_sequences: list[list[str]] = []
    for name, value in folder.items():
        if name in _NON_JOB_FOLDER_KEYS or not isinstance(value, dict):
            continue
        if value.get("Type") == _FLOW_TYPE:
            flow_sequences.append(list(value.get("Sequence", [])))
            continue
        if str(value.get("Type", "")).startswith("Job:"):
            jobs[name] = value

    if not jobs:
        raise GraphError(f"{path}: no jobs found in {WORKFLOW_NAME}")

    produced_by: dict[str, str] = {}
    for name, job in jobs.items():
        for event in _events(job, "eventsToAdd"):
            if event in produced_by:
                raise GraphError(
                    f"{path}: condition {event} is added by both "
                    f"{produced_by[event]} and {name}"
                )
            produced_by[event] = name

    edges: set[tuple[str, str]] = set()
    for name, job in jobs.items():
        for event in _events(job, "eventsToWaitFor"):
            producer = produced_by.get(event)
            if producer is None:
                raise GraphError(
                    f"{path}: {name} waits for {event} which no job adds"
                )
            edges.add((producer, name))

    retries: dict[str, int] = {}
    for name, job in jobs.items():
        limit = job.get("RerunLimit", {})
        retries[name] = int(limit.get("Times", 0)) if isinstance(limit, dict) else 0

    graph = TaskGraph(f"json:{Path(path).name}", sorted(jobs), edges, retries)

    for sequence in flow_sequences:
        missing = [task for task in sequence if task not in jobs]
        if missing:
            raise GraphError(f"{path}: Flow references unknown jobs {missing}")
        for earlier, later in zip(sequence, sequence[1:]):
            if (earlier, later) not in edges:
                raise GraphError(
                    f"{path}: Flow declares {earlier} -> {later} but no event "
                    "condition creates that dependency"
                )

    return graph


def graph_from_xml(path: Path) -> TaskGraph:
    """Build the graph from a legacy Control-M XML export.

    A job produces the conditions in its ``OUTCOND`` elements with ``SIGN="+"``
    and depends on the conditions in its ``INCOND`` elements.
    """
    root = ElementTree.parse(Path(path)).getroot()
    jobs = root.findall(".//JOB")
    if not jobs:
        raise GraphError(f"{path}: no JOB elements found")

    produced_by: dict[str, str] = {}
    for job in jobs:
        name = job.get("JOBNAME")
        if not name:
            raise GraphError(f"{path}: a JOB element has no JOBNAME")
        for outcond in job.findall("OUTCOND"):
            if outcond.get("SIGN") == "+" and outcond.get("NAME"):
                produced_by[outcond.get("NAME")] = name

    edges: set[tuple[str, str]] = set()
    retries: dict[str, int] = {}
    names: list[str] = []
    for job in jobs:
        name = job.get("JOBNAME")
        names.append(name)
        retries[name] = int(job.get("MAXRERUN", "0") or 0)
        for incond in job.findall("INCOND"):
            condition = incond.get("NAME")
            producer = produced_by.get(condition)
            if producer is None:
                raise GraphError(
                    f"{path}: {name} requires {condition} which no job produces"
                )
            edges.add((producer, name))

    return TaskGraph(f"xml:{Path(path).name}", sorted(names), edges, retries)


def load_task_commands(path: Path) -> list[dict]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = document.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise GraphError(f"{path}: no tasks declared")
    return tasks


def graph_from_task_commands(path: Path) -> TaskGraph:
    tasks = load_task_commands(path)
    names = [task["task"] for task in tasks]
    edges = {
        (dependency, task["task"])
        for task in tasks
        for dependency in task.get("dependsOn", [])
    }
    retries = {task["task"]: int(task.get("retryLimit", 0)) for task in tasks}
    return TaskGraph(f"task-commands:{Path(path).name}", sorted(names), edges, retries)


def repository_for_task(path: Path, task: str) -> str:
    for entry in load_task_commands(path):
        if entry["task"] == task:
            return entry["repository"]
    raise GraphError(f"{path}: task {task} is not defined")


def controlm_dir(repo_root: Path | None = None) -> Path:
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    return root / "controlm"


def canonical_graph(controlm_directory: Path | None = None) -> TaskGraph:
    directory = Path(controlm_directory) if controlm_directory else controlm_dir()
    return graph_from_json(directory / "payments_reconciliation.json")


def all_graphs(controlm_directory: Path | None = None) -> list[TaskGraph]:
    directory = Path(controlm_directory) if controlm_directory else controlm_dir()
    return [
        graph_from_json(directory / "payments_reconciliation.json"),
        graph_from_xml(directory / "payments_reconciliation.xml"),
        graph_from_task_commands(directory / "task-commands.json"),
    ]


def agreement_problems(controlm_directory: Path | None = None) -> list[str]:
    """Every difference between the three definition artifacts."""
    graphs = all_graphs(controlm_directory)
    problems: list[str] = []
    reference = graphs[0]
    for other in graphs[1:]:
        problems.extend(reference.differences(other))
    return problems
