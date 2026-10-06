-- SQLite ledger schema for the cross-border payments reconciliation ledger.
-- This DDL is executed verbatim by ledger_reconciliation.ledger.create_ledger.
-- payment_id is the PRIMARY KEY: the structural single-posting guarantee.
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
