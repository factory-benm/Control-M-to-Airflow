"""CLI entry point for settlement-reporting."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import common
from . import report


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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.help:
        sys.stderr.write(
            "Usage: ./scripts/run-task.sh --run-dir <abs> --scenario <name> "
            "--task <task> [--kit-root <abs>] [--run-id <id>] "
            "[--now <iso8601-utc>] [--attempt <n>]\n"
        )
        return 0

    envelope: dict = {
        "repository": common.REPO_NAME,
        "task": args.task or "",
        "runId": "",
        "scenario": args.scenario or "",
        "attempt": 1,
        "startedAt": "",
        "completedAt": "",
    }

    def fail(code, status, err_code, message):
        envelope.update({
            "status": status, "exitCode": code, "counts": {}, "artifacts": [],
            "metrics": {}, "error": {"code": err_code, "message": message},
        })
        common.emit_result(envelope)
        return code

    if not args.run_dir or not args.scenario or not args.task:
        return fail(2, "failed", "USAGE", "missing required argument(s): --run-dir, --scenario, --task")
    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        return fail(2, "failed", "USAGE", f"run-dir is not a directory: {run_dir}")
    if args.task not in common.OWNED_TASKS:
        return fail(2, "failed", "USAGE", f"task not owned by {common.REPO_NAME}: {args.task}")

    repo_root = _repo_root()
    try:
        kit_head = common.resolve_kit_root(args.kit_root, repo_root)
        scenario_cfg = common.load_scenario(kit_head, args.scenario)
    except common.UsageError as e:
        return fail(2, "failed", "USAGE", str(e))

    now = common.resolve_now(args.now, scenario_cfg)
    attempt = common.resolve_attempt(args.attempt)
    run_id = common.resolve_run_id(args.run_id, run_dir)
    envelope.update({"runId": run_id, "scenario": args.scenario, "attempt": attempt,
                     "startedAt": now, "completedAt": now})

    ctx = {"run_dir": run_dir, "kit_root": kit_head, "repo_root": repo_root,
           "scenario": args.scenario, "scenario_cfg": scenario_cfg,
           "run_id": run_id, "now": now, "attempt": attempt}

    try:
        if args.task == "produce_settlement_report":
            task_result, exit_code = report.run(ctx)
        else:
            return fail(2, "failed", "USAGE", f"task not owned: {args.task}")
    except common.MissingUpstreamError as e:
        return fail(5, "failed", "MISSING_UPSTREAM_ARTIFACT", str(e))
    except common.BusinessRuleError as e:
        return fail(4, "failed", e.code, str(e))
    except Exception:
        return fail(1, "failed", "INTERNAL", "unexpected error")

    envelope.update(task_result)
    envelope["exitCode"] = exit_code
    common.emit_result(envelope)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
