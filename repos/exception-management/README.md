# exception-management

Owns the `classify_breaks` task in the `PAYOPS_CROSS_BORDER_RECONCILIATION`
workflow.

## Running a task

```bash
./scripts/run-task.sh --run-dir <abs-path> --scenario <name> --task classify_breaks \
  [--kit-root <abs-path>] [--run-id <id>] [--now <iso8601-utc>] [--attempt <n>]
```

Emits one JSON object on stdout; diagnostics to stderr.

## Dependencies

Python standard library only.

## Layout

- `src/exception_management/` — task implementation
- `tests/` — isolated unit checks
- `scripts/run-task.sh` — task entry point
- `scripts/test.sh` — unit tests
