# payment-notifications

Owns the `archive_and_notify` task in the `PAYOPS_CROSS_BORDER_RECONCILIATION`
workflow.

## Running a task

```bash
./scripts/run-task.sh --run-dir <abs-path> --scenario <name> --task archive_and_notify \
  [--kit-root <abs-path>] [--run-id <id>] [--now <iso8601-utc>] [--attempt <n>]
```

Emits one JSON object on stdout; diagnostics to stderr.

## Outputs

- `output/notification.json` — notification payload (run id, scenario, status,
  canonical counts, report path, evidence path, generated-at timestamp)
- `stages/notify.json` — stage summary

This task writes a payload file only. It performs no network activity of any
kind: no email, webhook, or message is sent.

## Dependencies

Python standard library only.
