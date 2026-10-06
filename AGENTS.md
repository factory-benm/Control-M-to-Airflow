# AGENTS.md

Guidance for coding agents working in this repository.

## What this is

A fictional Control-M batch estate used for a Control-M to Airflow migration
workshop. `PAYOPS_CROSS_BORDER_RECONCILIATION` is an overnight cross-border
payments reconciliation batch: 12 Control-M jobs in one folder, chained by
conditions, on a Singapore business calendar. Each job calls a wrapper that
hands the work to the service repository that owns it.

```
watch_inbound_files -> verify_file_integrity -> extract_payment_batch
  -> validate_payment_schema -> deduplicate_payments -> enrich_fx_rates
  -> apply_business_day_cutoff -> post_pending_ledger
  -> reconcile_nostro_ledger -> classify_breaks
  -> produce_settlement_report -> archive_and_notify
```

## Layout

| Path | Contents |
| --- | --- |
| `repos/payments-orchestrator/controlm/` | Control-M definitions: `payments_reconciliation.json` (Automation API), `payments_reconciliation.xml` (legacy export), `task-commands.json` (task to repository mapping), `calendars.json`. |
| `repos/payments-orchestrator/src/orchestrator/graph.py` | Parses the three definition files and checks they agree. |
| `repos/<service>/` | Seven Python services, one per business step. Each has `scripts/run-task.sh`, `scripts/test.sh`, `src/`, `tests/`. |
| `fixtures/` | Five scenarios (`input/`, `reference/`, `expected/manifest.json`), calendar, FX rates, and `CONTRACT.md`, the shared execution contract. |
| `runtimes/control-m/harness/` | `harness.py` runs a scenario as the definitions declare; `verify_run.py` checks a run against its fixture oracle. |
| `runtimes/control-m/workbench/` | Optional BMC Control-M Workbench pin. |
| `scripts/` | `check.sh` (runs all checks), prerequisite check, validation, scenario runner, compatibility test, Workbench up/down. `scripts/lib/common.sh` holds shared helpers and exit codes. |
| `workspace/` | Runtime output (git-ignored). Runs land in `workspace/runtime/runs/<run-id>/`. |

## Setup

Needs Git, Bash, and Python 3.11 or newer with the `sqlite3` module. No Python
packages are installed or required. Docker is only needed for the optional
Control-M Workbench; everything below runs without it.

```sh
./scripts/check-prereqs.sh
```

## Build and check

There is no build or packaging step: the services are standard-library Python
run in place via `PYTHONPATH=src`. The single command that gates every change:

```sh
./scripts/check.sh
```

It runs, in order, the prerequisite check, Control-M validation, every
`repos/*/scripts/test.sh`, and the compatibility test, then prints a pass/fail
summary with timings. It stops early only if prerequisites are missing; it
exits 0 when everything passes and 1 otherwise. Run it before declaring any
change done. It takes well under a minute and writes only under `workspace/`.

## Run individual steps

```sh
./scripts/check-prereqs.sh                  # required tools present
./scripts/controlm-validate.sh              # the three definition files agree
./scripts/controlm-run.sh happy-path        # run one scenario
./scripts/test-controlm-compatibility.sh    # all scenarios vs. oracles + determinism
repos/<service>/scripts/test.sh             # one repository's unit tests
```

Scenarios: `happy-path`, `duplicate-retry`, `business-cutoff`,
`partial-ledger-write`, `reconciliation-breaks`.

Tests use the standard library `unittest`, not pytest. To run one test file
directly: `cd repos/<service> && PYTHONPATH=src python3 -m unittest tests/test_<name>.py -v`.

Without Workbench, scripts use the compatibility harness and label runs
`compatibility-harness`. It is not Control-M, and structural validation is not
`ctm build`; say so when reporting results.

## Exit codes

Task exit codes (from `fixtures/CONTRACT.md` section 3.2):

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Unexpected internal error |
| 2 | Usage error |
| 3 | Injected deterministic fault; the only retryable failure, and only when the scenario declares it |
| 4 | Business rule violation detected by the task |
| 5 | Missing required upstream artifact |

Scripts in `scripts/` use 0 ok, 1 failed, 2 usage error, 78 required runtime
unavailable.

## Safety rules

- Never edit fixtures, reference ledgers, or `expected/manifest.json` to make a
  test pass. Fix the code, or report the disagreement.
- Keep the three Control-M definition files (`payments_reconciliation.json`,
  `payments_reconciliation.xml`, `task-commands.json`) in agreement on tasks,
  dependencies, and retry limits. `./scripts/controlm-validate.sh` checks this.
- Services stay standard-library only (each repo has a `STDLIB_ONLY` marker).
  Do not add third-party dependencies.
- Tasks make no network calls. A task writes exactly one JSON object to stdout;
  diagnostics go to stderr.
- Money uses `decimal.Decimal` with `ROUND_HALF_UP`, never floats (see
  `fixtures/CONTRACT.md` section 1.2).
- Never commit secrets or anything under `workspace/`.
- Never push. Leave pushing to the user.
