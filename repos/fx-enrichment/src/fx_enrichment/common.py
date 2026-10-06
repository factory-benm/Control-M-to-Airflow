"""Shared helpers for fx-enrichment (Python standard library only).

This module is intentionally duplicated across the estate repositories. Each
repository stands alone as an independent Git repository, so small helper
duplication is the price of that independence. Do not unify these helpers into
a shared cross-repository package.
"""

import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]
StrPath = str | Path


class UsageError(Exception):
    """Exit code 2: bad or missing arguments."""


class BusinessRuleError(Exception):
    """Exit code 4: a business rule was violated."""


class MissingUpstreamError(Exception):
    """Exit code 5: a required upstream artifact is absent."""


def log(message: object) -> None:
    """Diagnostics to stderr with an ad-hoc bracketed prefix."""
    print("[fx-enrichment] " + str(message), file=sys.stderr)


def sha256_file(path: StrPath) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_dir(path: StrPath) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def write_json(path: StrPath, obj: object) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.write("\n")


def write_jsonl(path: StrPath, records: Iterable[JsonDict]) -> None:
    ordered = sorted(records, key=lambda r: (r.get("payment_id", ""), r.get("line_number", 0)))
    with open(path, "w", encoding="utf-8") as f:
        for r in ordered:
            f.write(json.dumps(r, sort_keys=True, separators=(",", ":")))
            f.write("\n")


def read_jsonl(path: StrPath) -> list[JsonDict]:
    records: list[JsonDict] = []
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if line:
                records.append(json.loads(line))
    return records


def artifact_entry(run_dir: StrPath, rel_path: str) -> dict[str, str]:
    abs_path = Path(run_dir) / rel_path
    return {"path": rel_path, "sha256": sha256_file(abs_path)}


def resolve_kit_root(arg: str | None, env: Mapping[str, str], repo_root: StrPath) -> Path:
    if arg:
        p = Path(arg).resolve()
        if (p / "fixtures").is_dir():
            return p
        raise UsageError("--kit-root does not contain fixtures/")
    env_val = env.get("PAYOPS_KIT_ROOT")
    if env_val:
        p = Path(env_val).resolve()
        if (p / "fixtures").is_dir():
            return p
        raise UsageError("PAYOPS_KIT_ROOT does not contain fixtures/")
    cur = Path(repo_root).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "fixtures").is_dir():
            return parent
    raise UsageError("could not locate kit root containing fixtures/")


def load_scenario(kit_root: StrPath, scenario: str) -> JsonDict:
    path = Path(kit_root) / "fixtures" / "scenarios" / scenario / "scenario.json"
    if not path.is_file():
        raise MissingUpstreamError("scenario not found: " + scenario)
    with open(path, encoding="utf-8") as f:
        doc: JsonDict = json.load(f)
    return doc


def resolve_fixed_clock(now_arg: str | None, env: Mapping[str, str], scenario_doc: JsonDict) -> str:
    if now_arg:
        return now_arg
    env_val = env.get("PAYOPS_FIXED_CLOCK")
    if env_val:
        return env_val
    clock: JsonDict = scenario_doc.get("clock", {})
    fixed: str | None = clock.get("fixedUtc")
    if fixed:
        return fixed
    raise UsageError("no fixed clock available; pass --now or PAYOPS_FIXED_CLOCK")


CLI_FLAGS = (
    "--run-dir",
    "--scenario",
    "--task",
    "--kit-root",
    "--run-id",
    "--now",
    "--attempt",
)


def parse_cli_args(argv: Sequence[str]) -> dict[str, str | None] | None:
    opts: dict[str, str | None] = {k.lstrip("-").replace("-", "_"): None for k in CLI_FLAGS}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h"):
            return None
        if a in CLI_FLAGS:
            if i + 1 >= len(argv):
                raise UsageError("missing value for " + a)
            key = a.lstrip("-").replace("-", "_")
            opts[key] = argv[i + 1]
            i += 2
        else:
            raise UsageError("unknown argument: " + a)
    return opts
