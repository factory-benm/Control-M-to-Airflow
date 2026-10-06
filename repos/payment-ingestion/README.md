# payment-ingestion

Inbound file detection, integrity verification, and batch extraction for the
cross-border payments reconciliation workflow. This repository owns the first
three tasks of the `PAYOPS_CROSS_BORDER_RECONCILIATION` workflow:

- `watch_inbound_files` — deterministic wait-for-file simulation
- `verify_file_integrity` — SHA-256 hashing and immutable copy of inputs
- `extract_payment_batch` — CSV parsing and normalized JSON Lines output

## Running a task

```bash
./scripts/run-task.sh --run-dir <abs-path> --scenario <name> --task <task-name> \
  [--kit-root <abs-path>] [--run-id <id>] [--now <iso8601-utc>] [--attempt <n>]
```

The script writes exactly one JSON object to stdout and all diagnostics to
stderr. The `--kit-root` argument locates the `fixtures/` directory; it
defaults to `PAYOPS_KIT_ROOT` and then to an upward search from this repository.

## Running the tests

```bash
./scripts/test.sh
```

## Notes

This is a representative service in a brownfield estate. It depends on the
Python standard library only. Ownership of this service sits with the
Payments Platform team. The full cross-repository interface contract, retry
semantics, and behavioral invariants are documented separately.
