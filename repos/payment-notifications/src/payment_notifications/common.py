"""Shared helpers for payment-notifications.

Standard library only. This repository uses bare stderr prints with a
'notify ::' prefix.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

REPO_NAME = "payment-notifications"
OWNED_TASKS = {"archive_and_notify"}


class UsageError(Exception):
    pass


class MissingUpstreamError(Exception):
    pass


class BusinessRuleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def log(message: str) -> None:
    print(f"notify :: {message}", file=sys.stderr)


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


def read_json_optional(path: Path) -> dict | None:
    if not path.exists():
        return None
    return read_json(path)


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
