# settlement-reporting

Owns the `produce_settlement_report` task in the
`PAYOPS_CROSS_BORDER_RECONCILIATION` workflow.

## Running a task

```bash
./scripts/run-task.sh --run-dir <abs-path> --scenario <name> --task produce_settlement_report \
  [--kit-root <abs-path>] [--run-id <id>] [--now <iso8601-utc>] [--attempt <n>]
```

Emits one JSON object on stdout; diagnostics to stderr.

## Outputs

- `output/settlement-report.json` — machine-readable report with canonical counts
- `output/settlement-report.csv` — human-readable per-payment table, sorted
- `output/archive-manifest.json` — every run artifact with SHA-256 and byte size
- `stages/reporting.json` — stage summary

## Dependencies

Python standard library only.
