"""Command-line entry point for payment-validation run-task.sh.

Emits exactly one JSON object on stdout. All diagnostics go to stderr.
"""
import json
import os
import sys
from pathlib import Path

from .common import (
    BusinessRuleError,
    MissingUpstreamError,
    UsageError,
    load_scenario,
    log,
    parse_cli_args,
    resolve_fixed_clock,
    resolve_kit_root,
)
from .tasks import REPOSITORY

USAGE = """usage: run-task.sh --run-dir <abs> --scenario <name> --task <task>
                  [--kit-root <abs>] [--run-id <id>] [--now <iso8601-utc>]
                  [--attempt <n>]

Tasks owned by payment-validation:
  validate_payment_schema
  deduplicate_payments
"""

TASKS = {
    "validate_payment_schema": None,
    "deduplicate_payments": None,
}


def _load_tasks():
    from . import tasks

    return {
        "validate_payment_schema": tasks.validate_payment_schema,
        "deduplicate_payments": tasks.deduplicate_payments,
    }


def _extract_meta(argv):
    meta = {"task": None, "scenario": None, "run_id": None}
    j = 0
    while j < len(argv) - 1:
        if argv[j] == "--task":
            meta["task"] = argv[j + 1]
        elif argv[j] == "--scenario":
            meta["scenario"] = argv[j + 1]
        elif argv[j] == "--run-id":
            meta["run_id"] = argv[j + 1]
        j += 1
    return meta


def _emit_error(argv, exc, exit_code, code, started_at):
    meta = _extract_meta(argv)
    result = {
        "task": meta["task"],
        "repository": REPOSITORY,
        "runId": meta["run_id"],
        "scenario": meta["scenario"],
        "attempt": 1,
        "status": "failed",
        "exitCode": exit_code,
        "counts": {},
        "artifacts": [],
        "metrics": {},
        "startedAt": started_at,
        "completedAt": started_at,
        "error": {"code": code, "message": str(exc)},
    }
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    repo_root = Path(__file__).resolve().parents[2]
    started_at = None
    tasks = _load_tasks()
    try:
        opts = parse_cli_args(argv)
        if opts is None:
            sys.stdout.write(USAGE)
            return 0
        if not opts["run_dir"] or not opts["scenario"] or not opts["task"]:
            raise UsageError("--run-dir, --scenario, and --task are required")
        task = opts["task"]
        if task not in tasks:
            raise UsageError("unknown task for {}: {}".format(REPOSITORY, task))
        run_dir = str(Path(opts["run_dir"]).resolve())
        run_id = opts["run_id"] or Path(run_dir).name
        kit_root = resolve_kit_root(opts["kit_root"], os.environ, repo_root)
        scenario_doc = load_scenario(kit_root, opts["scenario"])
        now = resolve_fixed_clock(opts["now"], os.environ, scenario_doc)
        attempt = opts["attempt"] or int(os.environ.get("PAYOPS_ATTEMPT", "1"))
        started_at = now
        result = tasks[task](
            run_dir=run_dir,
            run_id=run_id,
            scenario=opts["scenario"],
            kit_root=kit_root,
            scenario_doc=scenario_doc,
            now=now,
            attempt=attempt,
        )
        result["startedAt"] = now
        result["completedAt"] = now
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 0
    except UsageError as e:
        log.error("usage error: %s", e)
        _emit_error(argv, e, 2, "USAGE", started_at)
        return 2
    except MissingUpstreamError as e:
        log.error("missing upstream: %s", e)
        _emit_error(argv, e, 5, "MISSING_UPSTREAM", started_at)
        return 5
    except BusinessRuleError as e:
        log.error("business rule: %s", e)
        _emit_error(argv, e, 4, "BUSINESS_RULE", started_at)
        return 4
    except Exception as e:  # noqa: BLE001
        log.error("internal error: %s", e)
        _emit_error(argv, e, 1, "INTERNAL", started_at)
        return 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
