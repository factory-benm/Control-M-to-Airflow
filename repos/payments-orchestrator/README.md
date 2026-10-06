# payments-orchestrator

Control-M scheduling definitions for the overnight cross-border payments
reconciliation batch (`PAYOPS_CROSS_BORDER_RECONCILIATION`).

Looked after by the Payments Platform team. Ask in the payments-batch channel
before changing job conditions, the batch window, or the calendar.

> Fictional sample estate. These definitions, hosts, and mail destinations are
> not real scheduler configuration.

## What is in here

| Path | What it is |
| --- | --- |
| `controlm/payments_reconciliation.json` | The jobs-as-code definition. This is what we deploy. |
| `controlm/payments_reconciliation.xml` | Older export kept from the previous migration. Still handy for tooling that only reads XML. |
| `controlm/task-commands.json` | Which repository owns each task. The wrapper reads this. |
| `controlm/calendars.json` | Scheduler-side copy of the business calendar dates. |
| `scripts/run-task.sh` | The wrapper every Control-M job command calls. |
| `src/orchestrator/graph.py` | Parses the definitions so tooling does not re-implement it. |

## The batch

Twelve jobs, run in order. Each job adds an output condition and the next job
waits for it.

```
watch_inbound_files -> verify_file_integrity -> extract_payment_batch
  -> validate_payment_schema -> deduplicate_payments -> enrich_fx_rates
  -> apply_business_day_cutoff -> post_pending_ledger
  -> reconcile_nostro_ledger -> classify_breaks
  -> produce_settlement_report -> archive_and_notify
```

Condition names follow `PAYOPS-<TASK_NAME_UPPER>-OK`.

The jobs do not implement any business logic themselves. Each one shells out to
the repository that owns that step.

## Running a task by hand

```bash
./scripts/run-task.sh \
  --task extract_payment_batch \
  --run-dir /path/to/workspace/runtime/runs/my-run \
  --scenario happy-path \
  --kit-root /path/to/kit
```

The wrapper looks up the owning repository and calls its `scripts/run-task.sh`.
It looks for that repository next to this one first, then under `repos/` in the estate root.

## Tests

```bash
./scripts/test.sh
```

Checks that the JSON parses, the XML parses, the mapping is complete, and the
three files describe the same twelve jobs.

## Notes and rough edges

- `post_pending_ledger` is the only job with a rerun limit. It is set to 2.
  Rerunning it is safe, but the details of why are in the
  `ledger-reconciliation` repository, not here.
- The calendar dates exist both in `controlm/calendars.json` and in the batch
  fixtures. Someone should reconcile those one day.
- There is no single command that runs the whole batch end to end from this
  repository. Operations drive it from the scheduler.
- Failure mail goes to `payments-operations@payops.invalid`. Update it in
  `Defaults` if the distribution list changes, and remember the XML export has
  its own copy per job.
