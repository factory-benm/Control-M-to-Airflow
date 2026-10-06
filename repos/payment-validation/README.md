# payment-validation

Schema and business-field validation plus duplicate detection for the
cross-border payments reconciliation workflow. This repository owns tasks 4
and 5 of the `PAYOPS_CROSS_BORDER_RECONCILIATION` workflow:

- `validate_payment_schema` — required-field, format, currency, and amount checks
- `deduplicate_payments` — first-occurrence wins by `payment_id`

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
Python standard library only. The Payments Platform team owns this service.
The full cross-repository interface contract and invariant documentation live
elsewhere in the estate.
