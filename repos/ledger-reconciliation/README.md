# ledger-reconciliation

Owns three tasks in the `PAYOPS_CROSS_BORDER_RECONCILIATION` workflow:

- `apply_business_day_cutoff`
- `post_pending_ledger`
- `reconcile_nostro_ledger`

## Running a task

```bash
./scripts/run-task.sh --run-dir <abs-path> --scenario <name> --task <task-name> \
  [--kit-root <abs-path>] [--run-id <id>] [--now <iso8601-utc>] [--attempt <n>]
```

The script emits exactly one JSON object on stdout and writes task artifacts
under the run directory. Diagnostics go to stderr.

## Dependencies

Python standard library only. No third-party packages.

## Layout

- `src/ledger_reconciliation/` — task implementations
- `schema/ledger.sql` — the SQLite ledger DDL, executed verbatim by the code
- `tests/` — isolated unit checks
- `scripts/run-task.sh` — task entry point
- `scripts/test.sh` — unit tests

## Notes

The ledger stores monetary values as TEXT decimal strings. `payment_id` is the
primary key of the ledger, which is the structural single-posting guarantee.
