# Apache Airflow runtime

`PAYOPS_CROSS_BORDER_RECONCILIATION` migrated from Control-M to Apache Airflow
3, as decided in [`docs/migration-design.md`](../../docs/migration-design.md).
It runs beside the Control-M definitions and the compatibility harness, which
stay unchanged. Runs made here are labelled `mode: airflow`. They are not
Control-M runs and not compatibility-harness runs.

The DAG reads everything from the Control-M definitions in
`repos/payments-orchestrator/controlm/`: tasks, conditions, retry limits, the
command line of each job, the schedule, the calendar, and the failure mail.
Each task runs the existing `repos/payments-orchestrator/scripts/run-task.sh`
wrapper, exactly as Control-M would.

## Requirements

- Python 3.12 as `python3.12` (Airflow runs in its own venv), plus the
  estate's own requirements (`./scripts/check-prereqs.sh`).
- Network access to PyPI and `raw.githubusercontent.com` the first time only,
  to install the pin.
- Port 8080 on 127.0.0.1 free, or set `PAYOPS_AIRFLOW_PORT`.

The pin is in [`airflow-version.env`](airflow-version.env): `apache-airflow==3.3.2`
on Python 3.12, installed with the official constraints file for that version.

## Start, run, stop

Run these from the repository root.

```sh
./scripts/airflow-up.sh                  # install the pin (first time), start Airflow
./scripts/airflow-run.sh happy-path      # run one scenario through the scheduler
./scripts/airflow-down.sh                # stop every Airflow process
```

`airflow-up.sh` installs Airflow into `workspace/airflow/venv` the first time
(about a minute), then starts four components, each in its own process group:

| Component | Command | Listens on |
| --- | --- | --- |
| scheduler | `airflow scheduler --skip-serve-logs` | nothing |
| DAG processor | `airflow dag-processor` | nothing |
| API server and UI | `airflow api-server --host 127.0.0.1 --port 8080` | `127.0.0.1:8080` only |
| triggerer | `airflow triggerer --skip-serve-logs` | nothing |

`airflow standalone` is not used because it binds two log servers to all
interfaces (design Decision 17). It prints the UI address and where the
generated admin password is stored; it never prints the password itself:

- UI and REST API: <http://127.0.0.1:8080>
- User `admin`; the password is in
  `workspace/airflow/simple_auth_manager_passwords.json.generated` (git-ignored)

Component logs are in `workspace/airflow/run/<component>.log`. Task logs are in
`workspace/airflow/logs/` and in the UI.

### Run a scenario

```sh
./scripts/airflow-run.sh <scenario> [--run-id <id>]
python3 runtimes/control-m/harness/verify_run.py --run-dir workspace/runtime/runs/<id> --kit-root .
```

Scenarios: `happy-path`, `duplicate-retry`, `business-cutoff`,
`partial-ledger-write`, `reconciliation-breaks`. The runner prepares the run
directory, triggers one DAG run per pass (two for `duplicate-retry`), waits for
the scheduler to finish them, and writes `runtime.json` and `run-manifest.json`
in the harness format, so `verify_run.py` grades it the same way. A
`partial-ledger-write` run takes about 1.5 minutes because of the 60-second
retry delay.

### Trigger the DAG directly

```sh
./scripts/airflow-run.sh --trigger '{"scenario": "happy-path", "run_id": "manual-happy-path"}'
```

This starts one DAG run with exactly that conf, waits for it, and prints every
task's state and tries. Keys you leave out take the DAG's defaults: `scenario`
`happy-path`, `run_id` `scheduled-<order date>`, `kit_root` this repository,
`fixed_clock` from the scenario's `scenario.json`, `pass` 1. Run directories
land in `workspace/runtime/runs/<run_id>/`.

### Paused on creation

The DAG is paused on creation, like the `Manual` order method of the Control-M
folder. `airflow-run.sh` unpauses it only while its own runs execute and pauses
it again afterwards, also after a failure or Ctrl-C. While it is unpaused, a
20:00 Singapore business-day event that falls inside the window also starts its
scheduled run (with the defaults above).

To let the schedule run on its own, unpause it in the UI, or:

```sh
bash -c 'source scripts/lib/common.sh && source scripts/lib/airflow.sh && "$AIRFLOW_BIN" dags unpause "$PAYOPS_DAG_ID"'
```

## Check it

```sh
./scripts/test-airflow-equivalence.sh              # every Airflow check, about 8 minutes
./scripts/test-airflow-equivalence.sh structure schedule   # a subset
./scripts/check.sh                                 # every check in the estate, Airflow included
```

The suite runs the checks in design section 6 (`structure`, `schedule`,
`oracle`, `equivalence`, `retry`, `rerun`, `no-retry`, `determinism`), plus
`listeners` (every Airflow socket is on 127.0.0.1) and `pin` (the installed
packages match the pin and its constraints). It starts Airflow if a check needs
it and stops it again afterwards if it started it. Details land in
`workspace/airflow-equivalence/`. It exits 78 when Airflow is not installed, and
`check.sh` then reports the step as skipped.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `PAYOPS_AIRFLOW_HOME` | `workspace/airflow` | `AIRFLOW_HOME`: venv, metadata DB (SQLite), logs, admin password |
| `PAYOPS_AIRFLOW_PORT` | `8080` | API server port, always on 127.0.0.1 |

Every other Airflow setting is fixed in `scripts/lib/airflow.sh` through
`AIRFLOW__*` environment variables: LocalExecutor, SimpleAuthManager, DAGs
folder `runtimes/airflow/dags`, no example DAGs, DAGs paused at creation.

## Files

| Path | Contents |
| --- | --- |
| `airflow-version.env` | The exact Airflow version, Python version, and constraints URL. |
| `dags/payops_cross_border_reconciliation.py` | The DAG. It refuses to load if the three Control-M definition files disagree. |
| `runner.py` | Runs a scenario through the scheduler and writes the harness-format manifest (standard library). |
| `airflow_state.py` | Read-only queries against the metadata DB: DAG readiness, pause state, DAG runs with every task try. |
| `equivalence_checks.py` | The assertions behind `scripts/test-airflow-equivalence.sh` (standard library). |
| `tests/` | Unit tests. `test_airflow_dag`, `test_airflow_task`, and `test_airflow_schedule` need the Airflow venv; `test_airflow_runner` and `test_equivalence_checks` need only Python. |

Scripts: `scripts/airflow-up.sh`, `scripts/airflow-down.sh`,
`scripts/airflow-run.sh`, `scripts/test-airflow-equivalence.sh`, and the shared
settings in `scripts/lib/airflow.sh`.

## Known limits

- **The calendar ends on 2026-12-31.** The DAG schedules the 254 business days
  in `PAYOPS_SG_BUSINESS_2026` and nothing after it, the same as Control-M with
  a 2026-only calendar. Add a 2027 calendar to `calendars.json` and to the
  folder's `RuleBasedCalendars` before the end of 2026, or the batch stops
  being scheduled on 2027-01-01.
- The 20:00 start time is interpreted as Asia/Singapore time (Decision 2).
  Confirm the Control-M server timezone before any real cutover.
- The failure mail to `payments-operations@payops.invalid` is written to the
  task log and never sent (Decision 10).
- LocalExecutor only: tasks share state through the run directory.
