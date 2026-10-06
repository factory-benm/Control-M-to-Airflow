"""Shared helpers for ledger-reconciliation.

Standard library only. No third-party imports. This module is intentionally
duplicated across the four downstream repositories; each repository is an
independent unit and must not share code at import time.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypedDict

JsonObject = dict[str, Any]

REPO_NAME = "ledger-reconciliation"
OWNED_TASKS = {
    "apply_business_day_cutoff",
    "post_pending_ledger",
    "reconcile_nostro_ledger",
}


class TaskContext(TypedDict):
    """Resolved invocation context handed to every task's ``run``."""

    run_dir: Path
    kit_root: Path
    repo_root: Path
    scenario: str
    scenario_cfg: JsonObject
    run_id: str
    now: str
    attempt: int


class UsageError(Exception):
    """Raised for bad or missing CLI arguments (exit code 2)."""


class MissingUpstreamError(Exception):
    """Raised when a required upstream artifact is absent (exit code 5)."""


class BusinessRuleError(Exception):
    """Raised when a business rule is violated by the task itself (exit 4)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def log(message: str) -> None:
    """Bare stderr diagnostic with a '[ledger-recon]' prefix."""
    print(f"[ledger-recon] {message}", file=sys.stderr)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, sort_keys=True, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def read_jsonl(path: Path) -> list[JsonObject]:
    if not path.exists():
        raise MissingUpstreamError(f"upstream artifact not found: {path}")
    records: list[JsonObject] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            records.append(json.loads(line))
    return records


def sort_key(record: JsonObject) -> tuple[str, int]:
    """Sort by payment_id then line_number (1-based source row index)."""
    return (record.get("payment_id", ""), record.get("line_number", 0))


def write_jsonl(path: Path, records: Iterable[JsonObject]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records, key=sort_key)
    lines = [json.dumps(rec, sort_keys=True, separators=(",", ":")) for rec in ordered]
    text = "".join(line + "\n" for line in lines)
    path.write_text(text, encoding="utf-8")


def artifacts_for(run_dir: Path, rel_paths: Iterable[str]) -> list[dict[str, str]]:
    """Return artifacts sorted by path with a real SHA-256 of each file."""
    items = []
    for rel in rel_paths:
        p = run_dir / rel
        if not p.exists():
            continue
        items.append({"path": rel, "sha256": sha256_file(p)})
    items.sort(key=lambda a: a["path"])
    return items


def resolve_kit_root(arg_kit_root: str | None, repo_root: Path) -> Path:
    if arg_kit_root:
        p = Path(arg_kit_root).resolve()
        if not (p / "fixtures").is_dir():
            raise UsageError(f"kit root has no fixtures/ directory: {p}")
        return p
    env = os.environ.get("PAYOPS_KIT_ROOT")
    if env:
        p = Path(env).resolve()
        if (p / "fixtures").is_dir():
            return p
    for parent in [repo_root, *repo_root.parents]:
        if (parent / "fixtures").is_dir():
            return parent
    raise UsageError(
        "could not locate kit root (fixtures/) via --kit-root, PAYOPS_KIT_ROOT, or upward search"
    )


def load_scenario(kit_root: Path, scenario: str) -> JsonObject:
    s = kit_root / "fixtures" / "scenarios" / scenario / "scenario.json"
    if not s.exists():
        raise UsageError(f"scenario not found: {scenario}")
    scenario_cfg: JsonObject = read_json(s)
    return scenario_cfg


def resolve_now(arg_now: str | None, scenario_cfg: JsonObject) -> str:
    if arg_now:
        return arg_now
    env = os.environ.get("PAYOPS_FIXED_CLOCK")
    if env:
        return env
    fixed_utc: str = scenario_cfg["clock"]["fixedUtc"]
    return fixed_utc


def resolve_attempt(arg_attempt: str | None) -> int:
    if arg_attempt is not None:
        return int(arg_attempt)
    env = os.environ.get("PAYOPS_ATTEMPT")
    if env:
        return int(env)
    return 1


def resolve_run_id(arg_run_id: str | None, run_dir: Path) -> str:
    if arg_run_id:
        return arg_run_id
    return run_dir.name


def emit_result(envelope: JsonObject) -> None:
    sys.stdout.write(json.dumps(envelope, sort_keys=True, indent=2) + "\n")
