"""Shared test setup: puts this repository's src/ on sys.path and offers helpers.

Imported by every test module before the package under test, so the suite runs
the same way from scripts/test.sh, the coverage gate, or a bare unittest call.
"""

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
SRC = TESTS_DIR.parent / "src"
REPO_ROOT = TESTS_DIR.parent
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


def seed_report(run_dir: Path, counts: dict[str, Any], with_archive: bool = True) -> None:
    """Write the upstream outputs produce_settlement_report leaves for this task."""
    output = run_dir / "output"
    output.mkdir(parents=True, exist_ok=True)
    report = {"scenario": "happy-path", "runId": run_dir.name, "counts": counts}
    (output / "settlement-report.json").write_text(json.dumps(report), encoding="utf-8")
    if with_archive:
        (output / "archive-manifest.json").write_text(
            json.dumps({"artifacts": []}), encoding="utf-8"
        )
