"""CLI entry point for exception-management."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import classify, common


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


USAGE = (
    "Usage: ./scripts/run-task.sh --run-dir <abs> --scenario <name> "
    "--task <task> [--kit-root <abs>] [--run-id <id>] "
    "[--now <iso8601-utc>] [--attempt <n>]\n"
)


class _UnownedTaskError(Exception):
    pass


def _initial_envelope(args: argparse.Namespace) -> common.JsonDict:
    return {
        "repository": common.REPO_NAME,
        "task": args.task or "",
        "runId": "",
        "scenario": args.scenario or "",
        "attempt": 1,
        "startedAt": "",
        "completedAt": "",
    }


def _fail(envelope: common.JsonDict, code: int, err_code: str, message: str) -> int:
    envelope.update(
        {
            "status": "failed",
            "exitCode": code,
            "counts": {},
            "artifacts": [],
            "metrics": {},
            "error": {"code": err_code, "message": message},
        }
    )
    common.emit_result(envelope)
    return code


def _argument_problem(args: argparse.Namespace) -> str | None:
    if not args.run_dir or not args.scenario or not args.task:
        return "missing required argument(s): --run-dir, --scenario, --task"
    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        return f"run-dir is not a directory: {run_dir}"
    if args.task not in common.OWNED_TASKS:
        return f"task not owned by {common.REPO_NAME}: {args.task}"
    return None


def _build_context(
    args: argparse.Namespace,
    run_dir: Path,
    kit_root: Path,
    repo_root: Path,
    scenario_cfg: common.JsonDict,
) -> common.TaskContext:
    return {
        "run_dir": run_dir,
        "kit_root": kit_root,
        "repo_root": repo_root,
        "scenario": args.scenario,
        "scenario_cfg": scenario_cfg,
        "run_id": common.resolve_run_id(args.run_id, run_dir),
        "now": common.resolve_now(args.now, scenario_cfg),
        "attempt": common.resolve_attempt(args.attempt),
    }


def _run_task(task: str, ctx: common.TaskContext) -> tuple[common.JsonDict, int]:
    if task == "classify_breaks":
        return classify.run(ctx)
    raise _UnownedTaskError(f"task not owned: {task}")


def _execute(task: str, ctx: common.TaskContext, envelope: common.JsonDict) -> int:
    try:
        task_result, exit_code = _run_task(task, ctx)
    except _UnownedTaskError as e:
        return _fail(envelope, 2, "USAGE", str(e))
    except common.MissingUpstreamError as e:
        return _fail(envelope, 5, "MISSING_UPSTREAM_ARTIFACT", str(e))
    except common.BusinessRuleError as e:
        return _fail(envelope, 4, e.code, str(e))
    except Exception:
        return _fail(envelope, 1, "INTERNAL", "unexpected error")

    envelope.update(task_result)
    envelope["exitCode"] = exit_code
    common.emit_result(envelope)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.help:
        sys.stderr.write(USAGE)
        return 0

    envelope = _initial_envelope(args)
    problem = _argument_problem(args)
    if problem is not None:
        return _fail(envelope, 2, "USAGE", problem)

    run_dir = Path(args.run_dir).resolve()
    repo_root = _repo_root()
    try:
        kit_head = common.resolve_kit_root(args.kit_root, repo_root)
        scenario_cfg = common.load_scenario(kit_head, args.scenario)
    except common.UsageError as e:
        return _fail(envelope, 2, "USAGE", str(e))

    ctx = _build_context(args, run_dir, kit_head, repo_root, scenario_cfg)
    envelope.update(
        {
            "runId": ctx["run_id"],
            "scenario": args.scenario,
            "attempt": ctx["attempt"],
            "startedAt": ctx["now"],
            "completedAt": ctx["now"],
        }
    )
    return _execute(args.task, ctx, envelope)


if __name__ == "__main__":
    sys.exit(main())
