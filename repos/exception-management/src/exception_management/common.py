"""Shared helpers for exception-management.

Standard library only; no cross-repo shared package.
This repository uses the stdlib `logging` module with a plain format.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterable

REPO_NAME = "exception-management"
OWNED_TASKS = {"classify_breaks"}

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="EXC|%(levelname)s|%(message)s",
)
LOG = logging.getLogger("exception-mgmt")


class UsageError(Exception):
    pass


class MissingUpstreamError(Exception):
    pass


class BusinessRuleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def log(message: str) -> None:
    LOG.info(message)


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
    path.write_text(json.dumps(obj, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise MissingUpstreamError(f"upstream artifact not found: {path}")
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            records.append(json.loads(line))
    return records


def sort_key(record: dict) -> tuple:
    return (record.get("payment_id", ""), record.get("line_number", 0))


def write_jsonl(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records, key=sort_key)
    text = "".join(
        json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n"
        for rec in ordered
    )
    path.write_text(text, encoding="utf-8")


def artifacts_for(run_dir: Path, rel_paths: Iterable[str]) -> list[dict]:
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
    raise UsageError("could not locate kit root (fixtures/)")


def load_scenario(kit_root: Path, scenario: str) -> dict:
    s = kit_root / "fixtures" / "scenarios" / scenario / "scenario.json"
    if not s.exists():
        raise UsageError(f"scenario not found: {scenario}")
    return read_json(s)


def resolve_now(arg_now: str | None, scenario_cfg: dict) -> str:
    if arg_now:
        return arg_now
    env = os.environ.get("PAYOPS_FIXED_CLOCK")
    if env:
        return env
    return scenario_cfg["clock"]["fixedUtc"]


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


def emit_result(envelope: dict) -> None:
    sys.stdout.write(json.dumps(envelope, sort_keys=True, indent=2) + "\n")
