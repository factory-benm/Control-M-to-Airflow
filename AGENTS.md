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
| `runtimes/airflow/` | The batch on Apache Airflow 3: `dags/` (the DAG, built from the Control-M definitions), `runner.py` (runs a scenario through the scheduler, writes the harness-format manifest), `airflow_state.py`, `equivalence_checks.py`, `tests/`, the pin in `airflow-version.env`, and a `README.md` with every command. |
| `docs/migration-design.md` | The migration design. Build only what its DECIDED decisions say; record later changes in its section 9. |
| `scripts/` | `check.sh` (runs all checks), prerequisite check, `lint.sh`, `coverage.sh`, `check-file-sizes.sh`, validation, scenario runner, compatibility test, Workbench up/down, Airflow up/down/run, Airflow equivalence test. `scripts/lib/airflow.sh` holds the Airflow settings. `scripts/lib/common.sh` holds shared helpers and exit codes; `scripts/lib/coverage_gate.py` is the standard-library coverage tool. |
| `pyproject.toml` | Tool config only (ruff, mypy, pytest, vulture, coverage threshold). Not a package. |
| `requirements-dev.txt` | Hash-locked dev tools, compiled from `requirements-dev.in`. |
| `workspace/` | Runtime output (git-ignored). Runs land in `workspace/runtime/runs/<run-id>/`; coverage reports in `workspace/coverage/`. |

## Setup

Needs Git, Bash, and Python 3.11 or newer with the `sqlite3` module. The
services need no Python packages. Docker is only needed for the optional
Control-M Workbench; everything below runs without it.

```sh
./scripts/check-prereqs.sh
```

Development tools (ruff, mypy, vulture, pytest, pre-commit) are optional to run
the estate but required to change it. Install the pinned versions and the
commit hooks once:

```sh
uv venv .venv && uv pip install --python .venv/bin/python --require-hashes -r requirements-dev.txt
# or: python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.txt
.venv/bin/pre-commit install
```

To change a tool version, edit `requirements-dev.in`, then regenerate the lock
with the command at the top of that file.

## Build and check

There is no compile or packaging step: the services are standard-library Python
run in place via `PYTHONPATH=src`, so "build" means installing the dev tools
(above) and passing the gate. The single command that gates every change:

```sh
./scripts/check.sh
```

It runs, in order: prerequisites, file size limits, lint/format/types
(`scripts/lint.sh`), Control-M validation, every `repos/*/scripts/test.sh`,
coverage thresholds (`scripts/coverage.sh`), the compatibility test, and the
Airflow equivalence test (`scripts/test-airflow-equivalence.sh`), then prints a
pass/fail summary with timings. It stops early only if prerequisites are
missing; it exits 0 when everything passes and 1 otherwise. Run it before
declaring any change done. It writes only under `workspace/`. Without Airflow
it takes under a minute. With Airflow installed (`./scripts/airflow-up.sh`) the
Airflow step adds about 8 minutes: it runs every scenario through the real
scheduler, starting and stopping Airflow itself if it is not running. If the
dev tools are not installed and `uv` is absent, the lint step reports
`skipped`; if Airflow is not installed, the Airflow step reports `skipped`. CI
sets `PAYOPS_REQUIRE_DEV_TOOLS=1` so lint never skips there
(`.github/workflows/ci.yml`); CI does not install Airflow.

## Code quality rules

All configured in `pyproject.toml`; fix the code, never loosen the config or
add `# noqa` / `# type: ignore` to get past a check.

- Formatting: `ruff format`, line length 100. `./scripts/lint.sh --fix` applies
  fixes and formatting.
- Types: mypy `strict` over `repos/*/src`, `repos/*/tests`, the harness,
  `scripts/lib`, and the standard-library Airflow files (`runner.py`,
  `equivalence_checks.py` and their tests). Files that import Airflow are not
  type-checked because Airflow is not in the dev venv (see `pyproject.toml`).
  Every function, including tests, is fully annotated.
- Naming (ruff `N`): `snake_case` functions, variables, and modules;
  `CapWords` classes; `UPPER_CASE` module constants; test files
  `tests/test_<area>.py`, classes `Test<Thing>`, methods `test_<behavior>`.
- Complexity: cyclomatic complexity at most 10, at most 12 branches per
  function. Split long functions into named helpers.
- Dead code: vulture must report nothing. Delete unused code; list a name in
  `vulture_whitelist.py` only when something outside Python uses it, with a
  reason.
- Technical debt: no `FIXME`, `XXX`, or `HACK`. A `TODO` must link its issue,
  for example `# TODO: handle FX gaps https://github.com/<org>/<repo>/issues/12`.
- File size: tracked files at most 512 KB; `.py` and `.sh` files at most 800
  lines (`scripts/check-file-sizes.sh`).
- Coverage: every service repository keeps statement coverage at or above
  `[tool.payops.coverage] fail_under` (95%). New code comes with tests. Run
  `./scripts/coverage.sh <repo>` to see missing lines per file.

## Run individual steps

```sh
./scripts/check-prereqs.sh                  # required tools present
./scripts/lint.sh [--fix]                   # ruff, ruff format, mypy, vulture
./scripts/coverage.sh [repo ...]            # unit tests under the coverage gate
./scripts/check-file-sizes.sh               # file size and line limits
./scripts/controlm-validate.sh              # the three definition files agree
./scripts/controlm-run.sh happy-path        # run one scenario
./scripts/test-controlm-compatibility.sh    # all scenarios vs. oracles + determinism
repos/<service>/scripts/test.sh             # one repository's unit tests
./scripts/airflow-up.sh                     # install (first time) and start Airflow on 127.0.0.1
./scripts/airflow-run.sh happy-path         # run one scenario on Airflow
./scripts/test-airflow-equivalence.sh [check ...]   # Airflow vs. oracles and harness
./scripts/airflow-down.sh                   # stop Airflow
```

Scenarios: `happy-path`, `duplicate-retry`, `business-cutoff`,
`partial-ledger-write`, `reconciliation-breaks`.

Tests use the standard library `unittest`. To run one test file directly:
`cd repos/<service> && PYTHONPATH=src python3 -m unittest tests/test_<name>.py -v`.
With the dev tools installed, pytest can also run or collect them from the
root: `.venv/bin/python -m pytest repos/<service>/tests/test_<name>.py`.

Without Workbench, scripts use the compatibility harness and label runs
`compatibility-harness`. It is not Control-M, and structural validation is not
`ctm build`; say so when reporting results. Airflow runs are labelled
`airflow`; they are not Control-M either.

The Airflow unit tests live in `runtimes/airflow/tests/`. Those that import
Airflow run with its venv:
`cd runtimes/airflow/tests && ../../../workspace/airflow/venv/bin/python -m unittest test_airflow_dag -v`.
The others need only Python:
`cd runtimes/airflow/tests && PYTHONPATH=..:../../control-m/harness python3 -m unittest test_airflow_runner -v`.

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
  diagnostics go to stderr. The Airflow failure callback only logs the mail.
- Airflow listens on 127.0.0.1 only. Never start it with `airflow standalone`
  (its log servers bind to all interfaces); use `./scripts/airflow-up.sh`.
- Never print or commit the generated Airflow admin password
  (`workspace/airflow/simple_auth_manager_passwords.json.generated`).
- Money uses `decimal.Decimal` with `ROUND_HALF_UP`, never floats (see
  `fixtures/CONTRACT.md` section 1.2).
- Never commit secrets or anything under `workspace/`.
- Never push. Leave pushing to the user.
