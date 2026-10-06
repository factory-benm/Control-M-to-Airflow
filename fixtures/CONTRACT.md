# Shared execution contract

This file is the single source of truth for the interfaces shared by the eight
executable repositories, the Control-M definitions, and the compatibility
harness.

All identifiers, accounts, amounts, and system names in this estate are
fictional sample data.

---

## 1. Payment record schema

Input CSV files at `fixtures/scenarios/<scenario>/input/payments.csv` have this
exact header, in this exact order:

```text
payment_id,source_system,value_date,booking_timestamp,debtor_account_token,creditor_account_token,currency,amount,ledger_reference,country_code
```

| Field | Type | Rules |
| --- | --- | --- |
| `payment_id` | string | `PAY-YYYY-NNNN`. May repeat within a file only in duplicate scenarios. |
| `source_system` | string | `CHANNEL-<ISO2>`. |
| `value_date` | date | `YYYY-MM-DD`. The **submitted** value date. |
| `booking_timestamp` | timestamp | ISO 8601 UTC, always suffixed `Z`. |
| `debtor_account_token` | string | `DEBTOR-NNNN`. Never a realistic account number. |
| `creditor_account_token` | string | `CREDITOR-NNNN`. Never a realistic account number. |
| `currency` | string | ISO 4217 alpha-3, uppercase. |
| `amount` | decimal | Positive, exactly two decimal places, no thousands separators. |
| `ledger_reference` | string | `LEDG-<ISO2>-NNNN`. |
| `country_code` | string | ISO 3166-1 alpha-2, uppercase. |

Reference nostro ledger files at
`fixtures/scenarios/<scenario>/reference/ledger.csv` have this exact header:

```text
ledger_reference,payment_id,currency,amount
```

### 1.1 Currency allowlist

Version `2026.03` of the allowlist, used by `payment-validation`:

```text
AED, CNY, EUR, GBP, HKD, INR, JPY, KES, SGD, USD
```

Base currency for FX normalization is `USD`.

### 1.2 Decimal rules

- Never use binary floating point for money. Use `decimal.Decimal`.
- Quantize monetary values to two decimal places with `ROUND_HALF_UP`.
- FX rates carry six decimal places.
- `base_amount = amount * rate`, quantized to two decimal places after
  multiplication, not before.

---

## 2. Canonical task graph

The workflow `PAYOPS_CROSS_BORDER_RECONCILIATION` has exactly twelve tasks.
Task names are canonical and identical across the Control-M JSON, the Control-M
XML export, and the compatibility-harness manifest.

| # | Task | Owning repository | Depends on |
| --- | --- | --- | --- |
| 1 | `watch_inbound_files` | `payment-ingestion` | — |
| 2 | `verify_file_integrity` | `payment-ingestion` | 1 |
| 3 | `extract_payment_batch` | `payment-ingestion` | 2 |
| 4 | `validate_payment_schema` | `payment-validation` | 3 |
| 5 | `deduplicate_payments` | `payment-validation` | 4 |
| 6 | `enrich_fx_rates` | `fx-enrichment` | 5 |
| 7 | `apply_business_day_cutoff` | `ledger-reconciliation` | 6 |
| 8 | `post_pending_ledger` | `ledger-reconciliation` | 7 |
| 9 | `reconcile_nostro_ledger` | `ledger-reconciliation` | 8 |
| 10 | `classify_breaks` | `exception-management` | 9 |
| 11 | `produce_settlement_report` | `settlement-reporting` | 10 |
| 12 | `archive_and_notify` | `payment-notifications` | 11 |

The graph is a linear chain. `payments-orchestrator` owns the scheduler
definitions and wrappers and contributes no task of its own.

Control-M output condition names use the form `PAYOPS-<TASK_NAME_UPPER>-OK`,
for example `PAYOPS-VERIFY_FILE_INTEGRITY-OK`.

---

## 3. Repository CLI contract

Every executable service repository exposes exactly these two entry points.

```bash
./scripts/run-task.sh --run-dir <abs-path> --scenario <name> --task <task-name> \
  [--kit-root <abs-path>] [--run-id <id>] [--now <iso8601-utc>] [--attempt <n>]
./scripts/test.sh
```

- `--run-dir` is the absolute run directory (see section 4). Required.
- `--scenario` is one of the five scenario ids. Required.
- `--task` is one of the twelve canonical task names owned by that repository.
  Required.
- `--kit-root` locates `fixtures/`. Defaults to the `PAYOPS_KIT_ROOT`
  environment variable, then to a repository-relative search.
- `--run-id` defaults to the basename of `--run-dir`.
- `--now` supplies the fixed clock as ISO 8601 UTC. Defaults to
  `PAYOPS_FIXED_CLOCK`, then to the scenario's declared fixed clock. Tasks must
  never call an unfrozen wall clock for business logic.
- `--attempt` is the 1-based execution attempt, used for deterministic fault
  injection. Defaults to `PAYOPS_ATTEMPT`, then `1`.

`run-task.sh` writes **one** JSON object to stdout and nothing else. All
diagnostics go to stderr. `test.sh` runs that repository's unit tests only.

### 3.1 Task result JSON

```json
{
  "task": "extract_payment_batch",
  "repository": "payment-ingestion",
  "runId": "baseline-happy-path",
  "scenario": "happy-path",
  "attempt": 1,
  "status": "success",
  "exitCode": 0,
  "counts": { "received": 6 },
  "artifacts": [
    { "path": "stages/normalized-payments.jsonl", "sha256": "..." }
  ],
  "metrics": {},
  "startedAt": "2026-03-16T09:00:00Z",
  "completedAt": "2026-03-16T09:00:00Z"
}
```

- `artifacts[].path` is relative to the run directory. Always sorted by path.
- `counts` keys use the canonical stage-count names in section 5.
- `startedAt` and `completedAt` are volatile and excluded from equivalence
  comparison.
- On failure, `status` is `"failed"`, `exitCode` is non-zero, and a `"error"`
  object with `"code"` and `"message"` is present. Never include a stack trace,
  absolute host path, or user name in `"message"`.

### 3.2 Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Usage error: bad or missing arguments |
| 3 | Injected deterministic fault (an expected failure declared by the scenario) |
| 4 | Business rule violation detected by the task itself |
| 5 | Missing required upstream artifact |
| 1 | Unexpected internal error |

Exit code 3 is the only failure the harness is allowed to retry as an expected
condition, and only when the active scenario declares that fault.

---

## 4. Run directory layout

Runs are immutable once complete. Every run lives at:

```text
workspace/runtime/runs/<run-id>/
├── runtime.json          # which runtime executed this run
├── input/                # copies of immutable source fixtures + their hashes
├── stages/               # per-task normalized outputs
├── logs/                 # per-task stdout/stderr capture
├── ledger/               # SQLite ledger database
├── output/               # customer-visible artifacts
└── run-manifest.json     # normalized whole-run result
```

Nothing outside `workspace/` is ever written by a task.

### 4.1 Stage artifacts

| Task | Writes |
| --- | --- |
| `watch_inbound_files` | `stages/inbound-manifest.json` |
| `verify_file_integrity` | `stages/file-integrity.json`, `input/payments.csv`, `input/ledger.csv` |
| `extract_payment_batch` | `stages/normalized-payments.jsonl`, `stages/extract.json` |
| `validate_payment_schema` | `stages/validated-payments.jsonl`, `stages/rejections.jsonl`, `stages/validate.json` |
| `deduplicate_payments` | `stages/deduplicated-payments.jsonl`, `stages/duplicates.jsonl`, `stages/deduplicate.json` |
| `enrich_fx_rates` | `stages/enriched-payments.jsonl`, `stages/fx.json` |
| `apply_business_day_cutoff` | `stages/cutoff-payments.jsonl`, `stages/cutoff.json` |
| `post_pending_ledger` | `ledger/ledger.sqlite3`, `stages/ledger-posting.json` |
| `reconcile_nostro_ledger` | `stages/matched.jsonl`, `stages/breaks.jsonl`, `stages/reconciliation.json` |
| `classify_breaks` | `stages/classified-breaks.jsonl`, `stages/exceptions.json` |
| `produce_settlement_report` | `output/settlement-report.json`, `output/settlement-report.csv`, `output/archive-manifest.json`, `stages/reporting.json` |
| `archive_and_notify` | `output/notification.json`, `stages/notify.json` |

### 4.2 JSON Lines rules

- One JSON object per line, UTF-8, `\n` terminated, no trailing blank line.
- Object keys sorted alphabetically.
- Records sorted by `payment_id`, then by a task-specific tiebreaker documented
  in that repository's README.
- Every record carries `run_id`, `scenario`, `source_file_sha256`, and
  `payment_id` so any output row traces to its input.

### 4.3 Determinism

Re-running the same scenario with the same fixed clock must produce
byte-identical `stages/*.jsonl` and `output/*` files, except for fields listed
in section 7.

### 4.4 JSON Lines record shapes

Field sets are additive along the chain. Every record in every file carries the
five traceability fields `payment_id`, `run_id`, `scenario`,
`source_file_sha256`, and `line_number`.

`stages/normalized-payments.jsonl` — the base record, one per input data row:

```json
{
  "amount": "12500.00",
  "booking_timestamp": "2026-03-16T01:15:00Z",
  "country_code": "SG",
  "creditor_account_token": "CREDITOR-0101",
  "currency": "SGD",
  "debtor_account_token": "DEBTOR-0001",
  "ledger_reference": "LEDG-SG-0001",
  "line_number": 1,
  "payment_id": "PAY-2026-0001",
  "run_id": "baseline-happy-path",
  "scenario": "happy-path",
  "source_file_sha256": "9f2c...",
  "source_system": "CHANNEL-SG",
  "value_date": "2026-03-16"
}
```

| File | Fields |
| --- | --- |
| `stages/validated-payments.jsonl` | base record, unchanged |
| `stages/rejections.jsonl` | base record plus `reason_code`, `reason_detail` |
| `stages/deduplicated-payments.jsonl` | base record, unchanged |
| `stages/duplicates.jsonl` | base record plus `duplicate_of_line` |
| `stages/enriched-payments.jsonl` | base record plus `base_amount`, `base_currency`, `fx_rate`, `fx_rate_date`, `fx_table_version` |
| `stages/cutoff-payments.jsonl` | enriched record plus `effective_value_date`, `cutoff_applied`, `cutoff_local_time`, `cutoff_timezone`, `calendar_id` |
| `stages/matched.jsonl` | `payment_id`, `run_id`, `scenario`, `source_file_sha256`, `line_number`, `ledger_reference`, `currency`, `amount`, `base_amount`, `effective_value_date`, `match_status` fixed to `"matched"` |
| `stages/breaks.jsonl` | the matched field set with `match_status` `"broken"`, plus `reason_code`, `expected_currency`, `actual_currency`, `expected_amount`, `actual_amount`, `expected_payment_id` |
| `stages/classified-breaks.jsonl` | break record plus `severity`, `owner_team` |

For break records, `expected_*` describes the value found in the reference
nostro ledger and `actual_*` describes the value posted from the payment batch.
When no reference row exists, the `expected_*` fields are `null`.

`line_number` is the 1-based index of the source data row, header excluded. It
is the tiebreaker for sorting whenever two records share a `payment_id`.

---

## 5. Canonical stage counts

These names are used in task results, `run-manifest.json`, and fixture
expectation manifests. No other spelling is permitted.

| Count | Meaning |
| --- | --- |
| `received` | Data rows read from the input CSV, excluding the header |
| `rejected` | Rows failing schema or business-field validation |
| `duplicatesRemoved` | Rows discarded because their `payment_id` already appeared |
| `accepted` | Rows surviving validation and deduplication |
| `cutoffAdjusted` | Accepted rows whose effective value date moved |
| `posted` | Distinct `payment_id` values present in the ledger as `posted` |
| `matched` | Posted payments matching the reference ledger exactly |
| `broken` | Posted payments with a reconciliation break |

The count equation, asserted by tests and by the harness:

```text
received == rejected + duplicatesRemoved + accepted
accepted == posted
posted   == matched + broken
```

---

## 6. Business rules

### 6.1 Validation

A row is rejected when any of the following holds. The first matching reason
wins, evaluated in this order, and is recorded as `reason_code`:

| Order | `reason_code` | Condition |
| --- | --- | --- |
| 1 | `MISSING_REQUIRED_FIELD` | Any contract field is empty |
| 2 | `MALFORMED_FIELD` | A field fails its format rule |
| 3 | `CURRENCY_NOT_ALLOWED` | Currency is not in the allowlist |
| 4 | `NON_POSITIVE_AMOUNT` | Amount is less than or equal to zero |

### 6.2 Deduplication

Within one batch, the **first** occurrence of a `payment_id` in input file order
is kept. Later occurrences are written to `stages/duplicates.jsonl` with
`duplicate_of_line` recorded. Deduplication is by `payment_id` only.

### 6.3 FX enrichment

- The rate is looked up by the payment's **submitted** `value_date` and
  `currency` from the versioned table at `fixtures/fx/fx-rates.json`.
- A missing rate is a business rule violation, exit code 4. Fixtures never
  trigger it.
- Every enriched row carries `fx_table_version`, `fx_rate`, `fx_rate_date`, and
  `base_amount`.

### 6.4 Business-day cutoff

Declared per scenario. Inputs are the payment's `booking_timestamp` in UTC, the
scenario timezone, the scenario cutoff local time, and the business calendar at
`fixtures/calendars/sg-business-calendar-2026.json`.

```text
local     = booking_timestamp converted to the scenario timezone
localDate = local.date()
if localDate is a business day and local.time() < cutoffLocalTime:
    effective_value_date = localDate
else:
    effective_value_date = next business day strictly after localDate
```

A business day is a weekday that is not listed as a holiday in the calendar.
`cutoff_applied` is `true` when `effective_value_date != value_date`.
`cutoffAdjusted` counts rows where `cutoff_applied` is `true`.

Timezone conversion uses `zoneinfo`. The unfrozen system clock is never
consulted.

### 6.5 Ledger posting

SQLite schema, created if absent:

```sql
CREATE TABLE IF NOT EXISTS ledger_entry (
  payment_id            TEXT    PRIMARY KEY,
  run_id                TEXT    NOT NULL,
  scenario              TEXT    NOT NULL,
  currency              TEXT    NOT NULL,
  amount                TEXT    NOT NULL,
  base_amount           TEXT    NOT NULL,
  ledger_reference      TEXT    NOT NULL,
  effective_value_date  TEXT    NOT NULL,
  state                 TEXT    NOT NULL CHECK (state IN ('pending','posted')),
  source_file_sha256    TEXT    NOT NULL,
  posted_at_logical     TEXT    NOT NULL
);
```

- `payment_id` is the primary key. This is the structural guarantee that a
  payment is posted at most once.
- Inserts use `INSERT ... ON CONFLICT(payment_id) DO NOTHING` inside a single
  transaction per attempt.
- Posting is idempotent. Re-running the task against an existing ledger adds no
  row and reports `alreadyPosted` in `metrics`.
- Monetary values are stored as TEXT decimal strings, never as REAL.
- `posted_at_logical` is derived from the fixed clock, never the wall clock.

**Deterministic fault injection.** When the active scenario declares a fault for
`post_pending_ledger` with `afterWrites: N` on `attempt: A`, then on attempt `A`
the task commits exactly the first `N` payments in sorted order, then exits with
code 3 without writing the remaining rows. On any later attempt it completes the
missing rows only. Recovery therefore proves both progress and idempotency.

### 6.6 Reconciliation

Each posted payment is compared to the reference ledger keyed by
`ledger_reference`:

| Result | Condition |
| --- | --- |
| matched | Reference exists and `payment_id`, `currency`, and `amount` all agree |
| break `MISSING_LEDGER_REFERENCE` | No reference row for that `ledger_reference` |
| break `AMOUNT_MISMATCH` | Reference exists, currency agrees, amount differs |
| break `CURRENCY_MISMATCH` | Reference exists, currency differs |
| break `UNMATCHED_PAYMENT` | Reference exists but `payment_id` differs |

Break evaluation order is `MISSING_LEDGER_REFERENCE`, `UNMATCHED_PAYMENT`,
`CURRENCY_MISMATCH`, `AMOUNT_MISMATCH`. Broken payments are excluded from
`matched`.

### 6.7 Break classification

`exception-management` maps each break to a deterministic severity and owner:

| `reason_code` | `severity` | `owner_team` |
| --- | --- | --- |
| `MISSING_LEDGER_REFERENCE` | `high` | `Nostro Operations` |
| `CURRENCY_MISMATCH` | `high` | `FX Operations` |
| `AMOUNT_MISMATCH` | `medium` | `Reconciliation Operations` |
| `UNMATCHED_PAYMENT` | `medium` | `Payments Investigations` |

### 6.8 Notification

`payment-notifications` writes a payload only. It never sends a message, email,
webhook, or any network request.

---

## 7. Volatile fields

Excluded from equivalence comparison and from content hashes used for
comparison. They are still recorded in evidence for auditability.

- `startedAt`, `completedAt`, `generatedAt`, `capturedAt`, `importedAt`
- `durationMs`, any elapsed or duration measurement
- `runtime.hostArchitecture`, `runtime.composeCommand`, container ids
- absolute filesystem paths
- `runId` when comparing two runs of the same scenario across runtimes
- Airflow `dag_run_id`, `logical_date`, task instance ids
- process ids, ports, and temporary directory names

Normalization replaces each volatile value with a stable placeholder before
comparison rather than deleting the key, so structural drift is still detected.

---

## 8. Fixed clocks and scenario configuration

`fixtures/scenarios/<scenario>/scenario.json` is authoritative for execution
configuration. `fixtures/scenarios/<scenario>/expected/manifest.json` is
authoritative for expected results. A test asserts the overlapping fields agree.

Tasks receive the fixed clock through `--now` and the `PAYOPS_FIXED_CLOCK`
environment variable, and the attempt number through `--attempt` and
`PAYOPS_ATTEMPT`.

---

## 9. Logging

- tasks write human-readable diagnostics to stderr
- the harness captures stdout and stderr per task under `logs/`

Secrets, tokens, credentials, and account identifiers must never appear in any
log.
