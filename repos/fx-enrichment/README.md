# fx-enrichment

Deterministic date-keyed FX rate lookup and base-currency amount calculation
for the cross-border payments reconciliation workflow. This repository owns
task 6 of the `PAYOPS_CROSS_BORDER_RECONCILIATION` workflow:

- `enrich_fx_rates` — look up the rate by submitted `value_date` and `currency`,
  compute `base_amount = amount * rate` with `decimal.Decimal`, and emit the
  enriched record set

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
Python standard library only. The FX Operations team owns this service. The
full cross-repository interface contract and invariant documentation live
elsewhere in the estate.
