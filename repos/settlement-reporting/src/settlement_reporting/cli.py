"""CLI entry point for settlement-reporting."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import common, report
from .common import JsonDict, TaskContext

USAGE = (
    "Usage: ./scripts/run-task.sh --run-dir <abs> --scenario <name> "
    "--task <task> [--kit-root <abs>] [--run-id <id>] "
    "[--now <iso8601-utc>] [--attempt <n>]\n"
)


class _TaskFailureError(Exception):
    def __init__(self, exit_code: int, err_code: str, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.err_code = err_code
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run-task.sh", add_help=False)
    p.add_argument("--help", action="store_true")
    p.add_argument("--run-dir", required=False)
    p.add_argument("--scenario", required=False)
    p.add_argument("--task", required=False)
    p.add_argument("--kit-root", required=False)
    p.add_argument("--run-id", required=False)
    p.add_argument("--now", required=False)
    p.add_argument("--attempt", required=False)
    return p


def _repo_root() -> Path:
    env = os.environ.get("PAYOPS_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


def _new_envelope(args: argparse.Namespace) -> JsonDict:
    return {
        "repository": common.REPO_NAME,
        "task": args.task or "",
        "runId": "",
        "scenario": args.scenario or "",
        "attempt": 1,
        "startedAt": "",
        "completedAt": "",
    }


def _emit_failure(envelope: JsonDict, failure: _TaskFailureError) -> int:
    envelope.update(
        {
            "status": "failed",
            "exitCode": failure.exit_code,
            "counts": {},
            "artifacts": [],
            "metrics": {},
            "error": {"code": failure.err_code, "message": failure.message},
        }
    )
    common.emit_result(envelope)
    return failure.exit_code


def _validated_run_dir(args: argparse.Namespace) -> Path:
    if not args.run_dir or not args.scenario or not args.task:
        raise _TaskFailureError(
            2, "USAGE", "missing required argument(s): --run-dir, --scenario, --task"
        )
    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        raise _TaskFailureError(2, "USAGE", f"run-dir is not a directory: {run_dir}")
    if args.task not in common.OWNED_TASKS:
        raise _TaskFailureError(2, "USAGE", f"task not owned by {common.REPO_NAME}: {args.task}")
    return run_dir


def _load_kit(args: argparse.Namespace, repo_root: Path) -> tuple[Path, JsonDict]:
    try:
        kit_head = common.resolve_kit_root(args.kit_root, repo_root)
        scenario_cfg = common.load_scenario(kit_head, args.scenario)
    except common.UsageError as e:
        raise _TaskFailureError(2, "USAGE", str(e)) from e
    return kit_head, scenario_cfg


def _build_context(args: argparse.Namespace, run_dir: Path, envelope: JsonDict) -> TaskContext:
    repo_root = _repo_root()
    kit_head, scenario_cfg = _load_kit(args, repo_root)
    # Clock, attempt, and run-id errors are deliberately not mapped to an exit
    # code; they propagate exactly as they always have.
    now = common.resolve_now(args.now, scenario_cfg)
    attempt = common.resolve_attempt(args.attempt)
    run_id = common.resolve_run_id(args.run_id, run_dir)
    envelope.update(
        {
            "runId": run_id,
            "scenario": args.scenario,
            "attempt": attempt,
            "startedAt": now,
            "completedAt": now,
        }
    )
    return {
        "run_dir": run_dir,
        "kit_root": kit_head,
        "repo_root": repo_root,
        "scenario": args.scenario,
        "scenario_cfg": scenario_cfg,
        "run_id": run_id,
        "now": now,
        "attempt": attempt,
    }


def _run_task(task: str, ctx: TaskContext) -> tuple[JsonDict, int]:
    if task != "produce_settlement_report":
        raise _TaskFailureError(2, "USAGE", f"task not owned: {task}")
    try:
        return report.run(ctx)
    except common.MissingUpstreamError as e:
        raise _TaskFailureError(5, "MISSING_UPSTREAM_ARTIFACT", str(e)) from e
    except common.BusinessRuleError as e:
        raise _TaskFailureError(4, e.code, str(e)) from e
    except Exception as e:
        raise _TaskFailureError(1, "INTERNAL", "unexpected error") from e


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.help:
        sys.stderr.write(USAGE)
        return 0

    envelope = _new_envelope(args)
    try:
        run_dir = _validated_run_dir(args)
        ctx = _build_context(args, run_dir, envelope)
        task_result, exit_code = _run_task(args.task, ctx)
    except _TaskFailureError as failure:
        return _emit_failure(envelope, failure)

    envelope.update(task_result)
    envelope["exitCode"] = exit_code
    common.emit_result(envelope)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
