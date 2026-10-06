"""Line coverage gate for one service repository, standard library only.

Runs a repository's unittest suite with line tracing on, measures which lines
of ``src/`` executed, prints a per-file report, writes a JSON summary, and
fails when total coverage is below the threshold.

The services are standard-library only and the estate promises that no Python
packages are needed to run its checks, so this replaces coverage.py rather
than depending on it.

Usage:
    python3 scripts/lib/coverage_gate.py --repo repos/fx-enrichment
        [--fail-under 85] [--config pyproject.toml] [--json-out PATH]

Exit codes:
    0   tests passed and coverage meets the threshold
    1   tests failed or coverage is below the threshold
    2   usage error
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tomllib
import types
import unittest
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def bytecode_lines(code: types.CodeType) -> set[int]:
    """Lines that carry bytecode, found by walking every nested code object."""
    lines: set[int] = set()
    stack: list[types.CodeType] = [code]
    while stack:
        current = stack.pop()
        for _start, _end, line in current.co_lines():
            if line is not None and line > 0:
                lines.add(line)
        stack.extend(c for c in current.co_consts if isinstance(c, types.CodeType))
    return lines


class Statement:
    """One source statement and the lines that can report its execution.

    A statement spans several lines when it wraps or carries decorators, and
    the interpreter reports only some of them, so any hit in the span counts.
    Compound statements span their header only; they also count as executed
    when their first body statement ran (``try:`` has no reliable line event).
    """

    def __init__(self, node: ast.stmt) -> None:
        self.line = node.lineno
        start = node.lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([d.lineno for d in node.decorator_list] + [start])
        body = getattr(node, "body", None)
        self.first_child: int | None = None
        if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
            self.first_child = body[0].lineno
            end = max(node.lineno, body[0].lineno - 1)
        else:
            end = node.end_lineno or node.lineno
        self.span = range(start, end + 1)


def statements(path: Path) -> dict[int, Statement]:
    """Executable statements keyed by first line; docstrings and no-op lines excluded."""
    source = path.read_text(encoding="utf-8")
    has_code = bytecode_lines(compile(source, str(path), "exec"))
    found: dict[int, Statement] = {}
    for node in ast.walk(ast.parse(source, str(path))):
        if not isinstance(node, ast.stmt):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        stmt = Statement(node)
        if stmt.first_child is not None or any(line in has_code for line in stmt.span):
            found[stmt.line] = stmt
    return found


def executed(stmts: dict[int, Statement], hits: set[int]) -> set[int]:
    done: set[int] = set()
    # Children before parents, so a compound statement can rely on its body.
    for line in sorted(stmts, reverse=True):
        stmt = stmts[line]
        if any(h in hits for h in stmt.span) or (
            stmt.first_child is not None and stmt.first_child in done
        ):
            done.add(line)
    return done


def format_ranges(lines: Iterable[int]) -> str:
    """Collapse sorted line numbers into "3-5, 9" style ranges."""
    ranges: list[str] = []
    start = prev = None
    for line in sorted(lines):
        if start is None:
            start = prev = line
        elif prev is not None and line == prev + 1:
            prev = line
        else:
            ranges.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = line
    if start is not None:
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(ranges)


class LineRecorder:
    """Records executed lines for files under one source root."""

    def __init__(self, source_root: Path) -> None:
        self.source_root = str(source_root.resolve()) + os.sep
        self.hits: dict[str, set[int]] = {}
        self._tracked: dict[str, str | None] = {}

    def _resolve(self, filename: str) -> str | None:
        if filename not in self._tracked:
            real = os.path.realpath(filename)
            self._tracked[filename] = real if real.startswith(self.source_root) else None
        return self._tracked[filename]

    def start(self) -> None:
        if sys.version_info >= (3, 12):
            # Each line location is reported once, then disabled, so tracing
            # costs almost nothing after the first hit.
            mon = sys.monitoring
            disable = mon.DISABLE

            def on_line(code: types.CodeType, line: int) -> Any:
                real = self._resolve(code.co_filename)
                if real is not None:
                    self.hits.setdefault(real, set()).add(line)
                return disable

            mon.use_tool_id(mon.COVERAGE_ID, "payops-coverage-gate")
            mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, on_line)
            mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
        else:
            sys.settrace(self._global_trace)

    def stop(self) -> None:
        if sys.version_info >= (3, 12):
            mon = sys.monitoring
            mon.set_events(mon.COVERAGE_ID, mon.events.NO_EVENTS)
            mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, None)
            mon.free_tool_id(mon.COVERAGE_ID)
        else:
            sys.settrace(None)

    def _global_trace(
        self, frame: types.FrameType, event: str, arg: object
    ) -> Callable[..., Any] | None:
        # Python 3.11 fallback: only trace frames from tracked files.
        real = self._resolve(frame.f_code.co_filename)
        if real is None:
            return None
        hits = self.hits.setdefault(real, set())

        def local_trace(frame: types.FrameType, event: str, arg: object) -> Any:
            if event == "line":
                hits.add(frame.f_lineno)
            return local_trace

        hits.add(frame.f_lineno)
        return local_trace


def load_threshold(config: Path) -> float | None:
    if not config.is_file():
        return None
    with config.open("rb") as f:
        data = tomllib.load(f)
    value = data.get("tool", {}).get("payops", {}).get("coverage", {}).get("fail_under")
    return float(value) if value is not None else None


def run_tests(repo: Path, recorder: LineRecorder) -> bool:
    src = repo / "src"
    tests = repo / "tests"
    sys.path.insert(0, str(src.resolve()))
    recorder.start()
    try:
        suite = unittest.TestLoader().discover(start_dir=str(tests), top_level_dir=str(tests))
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
    finally:
        recorder.stop()
    return result.wasSuccessful()


def build_report(repo: Path, recorder: LineRecorder) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    total_statements = total_missed = 0
    for path in sorted((repo / "src").rglob("*.py")):
        stmts = statements(path)
        expected = set(stmts)
        missed = expected - executed(stmts, recorder.hits.get(os.path.realpath(path), set()))
        total_statements += len(expected)
        total_missed += len(missed)
        files.append(
            {
                "file": str(path.relative_to(repo)),
                "statements": len(expected),
                "missed": len(missed),
                "missing_lines": format_ranges(missed),
                "percent": percent(len(expected), len(missed)),
            }
        )
    return {
        "repo": repo.name,
        "statements": total_statements,
        "missed": total_missed,
        "percent": percent(total_statements, total_missed),
        "files": files,
    }


def percent(statements: int, missed: int) -> float:
    if statements == 0:
        return 100.0
    return round(100.0 * (statements - missed) / statements, 1)


def print_report(report: dict[str, Any]) -> None:
    width = max([len(f["file"]) for f in report["files"]] + [5])
    print(f"{'File':<{width}}  {'Stmts':>5}  {'Miss':>5}  {'Cover':>6}  Missing")
    for f in report["files"]:
        print(
            f"{f['file']:<{width}}  {f['statements']:>5}  {f['missed']:>5}  "
            f"{f['percent']:>5.1f}%  {f['missing_lines']}"
        )
    print(
        f"{'TOTAL':<{width}}  {report['statements']:>5}  {report['missed']:>5}  "
        f"{report['percent']:>5.1f}%"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, type=Path, help="service repository root")
    parser.add_argument("--fail-under", type=float, help="minimum total line coverage percent")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "pyproject.toml",
        help="TOML file with [tool.payops.coverage] fail_under",
    )
    parser.add_argument("--json-out", type=Path, help="write the report as JSON here")
    args = parser.parse_args(argv)

    repo: Path = args.repo.resolve()
    if not (repo / "src").is_dir() or not (repo / "tests").is_dir():
        print(f"error: {repo} needs src/ and tests/ directories", file=sys.stderr)
        return EXIT_USAGE
    threshold = args.fail_under if args.fail_under is not None else load_threshold(args.config)
    if threshold is None:
        print("error: no threshold; pass --fail-under or set it in pyproject.toml", file=sys.stderr)
        return EXIT_USAGE

    recorder = LineRecorder(repo / "src")
    tests_passed = run_tests(repo, recorder)
    report = build_report(repo, recorder)
    report["fail_under"] = threshold
    report["tests_passed"] = tests_passed
    print_report(report)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    if not tests_passed:
        print("error: tests failed", file=sys.stderr)
        return EXIT_FAILED
    if report["percent"] < threshold:
        print(
            f"error: {repo.name} line coverage {report['percent']:.1f}% "
            f"is below the {threshold:.1f}% minimum",
            file=sys.stderr,
        )
        return EXIT_FAILED
    print(f"{repo.name}: line coverage {report['percent']:.1f}% (minimum {threshold:.1f}%)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
