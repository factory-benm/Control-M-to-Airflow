# Control-M to Airflow

A small Control-M batch estate for a hands-on migration workshop with
[Droid](https://docs.factory.ai).

**Attending the workshop? Open [PARTICIPANT_CHECKLIST.md](PARTICIPANT_CHECKLIST.md)
and follow it top to bottom.** The `/migrate` prompt is also in
[BUILD_PROMPT.md](BUILD_PROMPT.md).

> Fictional sample estate. The jobs, hosts, mail addresses, payments, and
> calendars are made up. Nothing here is real scheduler configuration.

## The batch

`PAYOPS_CROSS_BORDER_RECONCILIATION` is an overnight cross-border payments
reconciliation batch: 12 Control-M jobs in one folder, chained by conditions,
scheduled on a Singapore business calendar.

```
watch_inbound_files -> verify_file_integrity -> extract_payment_batch
  -> validate_payment_schema -> deduplicate_payments -> enrich_fx_rates
  -> apply_business_day_cutoff -> post_pending_ledger
  -> reconcile_nostro_ledger -> classify_breaks
  -> produce_settlement_report -> archive_and_notify
```

Each job calls a wrapper that hands the work to the repository that owns it.

## Layout

| Path | What it is |
| --- | --- |
| `repos/payments-orchestrator/controlm/` | Control-M definitions: Automation API JSON, legacy XML export, task-to-repository mapping, calendar copy. |
| `repos/*` | Eight Python services (standard library only), one per business step. |
| `fixtures/` | Five test scenarios with expected results, the business calendar, FX rates, and the shared execution contract. |
| `runtimes/control-m/` | Compatibility harness and result checker, plus the optional BMC Workbench pin. |
| `scripts/` | Prerequisite check, validation, scenario runner, compatibility test, and `check.sh`, which runs them all. |

## Run it

Needs Git and Python 3.11 or newer. No Python packages are required.

```sh
./scripts/check-prereqs.sh
./scripts/controlm-validate.sh
./scripts/controlm-run.sh happy-path
./scripts/test-controlm-compatibility.sh
```

Scenarios: `happy-path`, `duplicate-retry`, `business-cutoff`,
`partial-ledger-write`, `reconciliation-breaks`. Run output goes to
`workspace/runtime/runs/<run-id>/`, which Git ignores.

Without BMC Control-M Workbench credentials, the scripts use the compatibility
harness. It runs the jobs exactly as the Control-M definitions declare them and
labels every run `compatibility-harness`. It is not Control-M.

Each repository has its own tests: `repos/<name>/scripts/test.sh`.

To run everything at once (prerequisites, Control-M validation, every
repository's tests, and the compatibility test):

```sh
./scripts/check.sh
```

## Workshop flow

1. `/readiness-report`, then fix the gaps that matter (`AGENTS.md`, one check command).
2. Write a migration design doc, review it, and make its decisions.
3. `/migrate` to Airflow 3, building what the design doc says.
4. Check the code matches the design and the Control-M definitions.
5. Test the Airflow DAG with all five scenarios.
