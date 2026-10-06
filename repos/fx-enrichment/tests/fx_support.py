"""Shared test setup: puts this repository's src/ on sys.path and offers helpers.

Imported by every test module before the package under test, so the suite runs
the same way from scripts/test.sh, the coverage gate, or a bare unittest call.
"""

import contextlib
import json
import os
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SRC = TESTS_DIR.parent / "src"
KIT_ROOT = TESTS_DIR.parents[2]

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@contextlib.contextmanager
def payops_env(**overrides: str) -> Iterator[None]:
    """Run with every PAYOPS_* variable removed, then the given overrides applied."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PAYOPS_")}
    env.update(overrides)
    with mock.patch.dict(os.environ, env, clear=True):
        yield


def dedup_record(
    payment_id: str, line_number: int, value_date: str, currency: str, amount: str
) -> dict[str, Any]:
    return {
        "amount": amount,
        "currency": currency,
        "line_number": line_number,
        "payment_id": payment_id,
        "run_id": "run-test",
        "scenario": "happy-path",
        "source_file_sha256": "0" * 64,
        "value_date": value_date,
    }


def write_dedup_stage(run_dir: Path, records: Iterable[dict[str, Any]]) -> Path:
    stages = run_dir / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    path = stages / "deduplicated-payments.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path
