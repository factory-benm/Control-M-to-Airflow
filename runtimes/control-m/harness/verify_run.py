#!/usr/bin/env python3
"""Verify an executed run against its fixture oracle.

The harness records what happened. This program decides whether what happened
was correct, by comparing the run directory against
``fixtures/scenarios/<scenario>/expected/manifest.json``. Keeping the two apart
matters: a runtime that graded its own homework would prove nothing.

It also provides a ``--normalize`` mode that emits a normalized view of a run
with volatile fields replaced by stable placeholders. That is what makes two
runs comparable.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

VOLATILE_KEYS = {
    "startedAt",
    "completedAt",
    "generatedAt",
    "capturedAt",
    "importedAt",
    "selectedAt",
    "probedAt",
    "scannedAt",
    "validatedAt",
    "durationMs",
    "elapsedMs",
    "elapsed",
    "durationSeconds",
    "runId",
    "run_id",
    "hostArchitecture",
    "composeCommand",
    "containerId",
    "pid",
    "port",
    # Hashes of run-scoped artifacts are volatile across run identifiers,
    # because those artifacts embed the run identifier by design. Byte-level
    # stability is proven separately by re-running the same scenario with the
    # same run identifier, per section 4.3 of fixtures/CONTRACT.md.
    #
    # Deliberately NOT listed here: source_file_sha256, which hashes an
    # immutable input fixture. That value must stay comparable, because it is
    # what lets an output row be traced back to its input.
    "sha256",
    "archiveSha256",
    "reportSha256",
}
VOLATILE_PLACEHOLDER = "<volatile>"
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

JsonObject = dict[str, Any]


def load_json(path: Path) -> JsonObject:
    document: JsonObject = json.loads(path.read_text(encoding="utf-8"))
    return document


def read_jsonl(path: Path) -> list[JsonObject]:
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def normalize(value: Any, run_dir: Path | None = None, run_id: str | None = None) -> Any:
    """Replace volatile values with placeholders, preserving structure."""
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            if key in VOLATILE_KEYS:
                result[key] = VOLATILE_PLACEHOLDER
            else:
                result[key] = normalize(item, run_dir, run_id)
        return result
    if isinstance(value, list):
        return [normalize(item, run_dir, run_id) for item in value]
    if isinstance(value, str):
        if TIMESTAMP_PATTERN.match(value):
            return VOLATILE_PLACEHOLDER
        if run_id and run_id in value:
            value = value.replace(run_id, VOLATILE_PLACEHOLDER)
        if run_dir and str(run_dir) in value:
            value = value.replace(str(run_dir), "<run-dir>")
        # Absolute paths must never leak into comparable output.
        if value.startswith("/"):
            return "<absolute-path>"
        return value
    return value


class Verifier:
    def __init__(self, kit_root: Path, run_dir: Path) -> None:
        self.kit_root = kit_root.resolve()
        self.run_dir = run_dir.resolve()
        self.manifest = load_json(self.run_dir / "run-manifest.json")
        self.scenario = self.manifest["scenario"]
        self.expected = load_json(
            self.kit_root / "fixtures" / "scenarios" / self.scenario / "expected" / "manifest.json"
        )
        self.failures: list[str] = []
        self.checks: list[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.checks.append(name)
        else:
            self.failures.append(f"{name}: {detail}")

    # -- individual comparisons -------------------------------------------

    def verify_status(self) -> None:
        self.check(
            "run status is success",
            self.manifest["status"] == "success",
            f"status={self.manifest['status']} failure={self.manifest.get('failure', '')}",
        )
        self.check(
            "runtime mode recorded",
            self.manifest["runtime"]["mode"] in {"compatibility-harness", "control-m-workbench"},
            f"mode={self.manifest['runtime'].get('mode')}",
        )
        self.check(
            "scenario version matches the fixture",
            self.manifest.get("scenarioVersion") == self.expected.get("scenarioVersion"),
            f"run={self.manifest.get('scenarioVersion')} "
            f"fixture={self.expected.get('scenarioVersion')}",
        )

    def verify_counts(self) -> None:
        expected = self.expected["counts"]
        observed = self.manifest["counts"]
        for key in sorted(expected):
            self.check(
                f"count {key} == {expected[key]}",
                observed.get(key) == expected[key],
                f"expected {expected[key]}, observed {observed.get(key)}",
            )
        # The equations are asserted independently of the authored numbers.
        if all(
            key in observed
            for key in (
                "received",
                "rejected",
                "duplicatesRemoved",
                "accepted",
                "posted",
                "matched",
                "broken",
            )
        ):
            self.check(
                "received == rejected + duplicatesRemoved + accepted",
                observed["received"]
                == observed["rejected"] + observed["duplicatesRemoved"] + observed["accepted"],
                json.dumps(observed),
            )
            self.check(
                "accepted == posted",
                observed["accepted"] == observed["posted"],
                json.dumps(observed),
            )
            self.check(
                "posted == matched + broken",
                observed["posted"] == observed["matched"] + observed["broken"],
                json.dumps(observed),
            )

    def verify_exit_sequence(self) -> None:
        expected = [
            (item["task"], item["attempt"], item["exitCode"])
            for item in self.expected["expectedExitSequence"]
        ]
        observed = [
            (item["task"], item["attempt"], item["exitCode"])
            for item in self.manifest["exitSequence"]
        ]
        # A declared rerun of the whole graph repeats the sequence; compare the
        # first pass, then confirm the repeat is identical.
        if (
            len(observed) == 2 * len(expected)
            and observed[: len(expected)] == observed[len(expected) :]
        ):
            observed = observed[: len(expected)]
        self.check(
            "exit sequence matches the fixture",
            observed == expected,
            f"expected {expected}, observed {observed}",
        )

    def verify_value_dates(self) -> None:
        expected = self.expected.get("effectiveValueDates") or {}
        if not expected:
            return
        records = read_jsonl(self.run_dir / "stages" / "cutoff-payments.jsonl")
        observed = {row["payment_id"]: row.get("effective_value_date") for row in records}
        mismatched = {
            payment: (expected[payment], observed.get(payment))
            for payment in sorted(expected)
            if observed.get(payment) != expected[payment]
        }
        self.check(
            f"effective value dates match the declared calendar ({len(expected)} payments)",
            not mismatched,
            f"mismatched {mismatched}",
        )

    def verify_fx(self) -> None:
        expected = self.expected.get("fxRatesUsed") or {}
        expected_amounts = self.expected.get("baseAmounts") or {}
        if not expected:
            return
        records = read_jsonl(self.run_dir / "stages" / "enriched-payments.jsonl")
        observed_rate = {row["payment_id"]: str(row.get("fx_rate")) for row in records}
        observed_amount = {row["payment_id"]: str(row.get("base_amount")) for row in records}
        bad_rates = {
            payment: (values["rate"], observed_rate.get(payment))
            for payment, values in sorted(expected.items())
            if observed_rate.get(payment) != values["rate"]
        }
        self.check(
            f"FX rates match the pinned table ({len(expected)} payments)",
            not bad_rates,
            f"mismatched {bad_rates}",
        )
        bad_amounts = {
            payment: (amount, observed_amount.get(payment))
            for payment, amount in sorted(expected_amounts.items())
            if observed_amount.get(payment) != amount
        }
        self.check(
            f"base amounts match the expected decimal results ({len(expected_amounts)} payments)",
            not bad_amounts,
            f"mismatched {bad_amounts}",
        )

    def verify_matching(self) -> None:
        expected_matched = sorted(self.expected.get("matchedPaymentIds") or [])
        observed_matched = sorted(
            row["payment_id"] for row in read_jsonl(self.run_dir / "stages" / "matched.jsonl")
        )
        self.check(
            f"matched payment ids ({len(expected_matched)})",
            observed_matched == expected_matched,
            f"expected {expected_matched}, observed {observed_matched}",
        )

        expected_breaks = self.expected.get("breaks") or []
        observed_breaks = read_jsonl(
            self.run_dir / "stages" / "classified-breaks.jsonl"
        ) or read_jsonl(self.run_dir / "stages" / "breaks.jsonl")
        self.check(
            f"break count ({len(expected_breaks)})",
            len(observed_breaks) == len(expected_breaks),
            f"expected {len(expected_breaks)}, observed {len(observed_breaks)}",
        )
        if expected_breaks:
            expected_pairs = sorted(
                (item["payment_id"], item["reason_code"], item["severity"], item["owner_team"])
                for item in expected_breaks
            )
            observed_pairs = sorted(
                (row["payment_id"], row["reason_code"], row["severity"], row["owner_team"])
                for row in observed_breaks
            )
            self.check(
                "break classification, severity, and owning team all match",
                observed_pairs == expected_pairs,
                f"expected {expected_pairs}, observed {observed_pairs}",
            )

    def verify_rejections_and_duplicates(self) -> None:
        for key, filename, id_field in (
            ("rejections", "rejections.jsonl", "payment_id"),
            ("duplicates", "duplicates.jsonl", "payment_id"),
        ):
            expected = self.expected.get(key) or []
            observed = read_jsonl(self.run_dir / "stages" / filename)
            self.check(
                f"{key} count ({len(expected)})",
                len(observed) == len(expected),
                f"expected {len(expected)}, observed {len(observed)}",
            )
            if expected:
                expected_ids = sorted(item[id_field] for item in expected)
                observed_ids = sorted(row[id_field] for row in observed)
                self.check(
                    f"{key} payment ids match",
                    observed_ids == expected_ids,
                    f"expected {expected_ids}, observed {observed_ids}",
                )

    def verify_artifacts(self) -> None:
        missing = [
            relative
            for relative in self.expected.get("expectedArtifacts") or []
            if not (self.run_dir / relative).exists()
        ]
        self.check(
            f"all {len(self.expected.get('expectedArtifacts') or [])} expected artifacts exist",
            not missing,
            f"missing {missing}",
        )

    def verify_recorded_logs_exist(self) -> None:
        """Every log path the manifest advertises must resolve to a real file.

        Attempt-scoped logs are the only forensic record of a failed attempt, so
        a manifest that points at a file which does not exist, or at one shared
        between attempts, is a defect rather than a cosmetic issue.
        """
        claimed: list[str] = []
        missing: list[str] = []
        for pass_record in self.manifest.get("passes") or []:
            for task in pass_record.get("tasks") or []:
                for stream in ("stdout", "stderr"):
                    relative = task.get("logs", {}).get(stream)
                    if not relative:
                        continue
                    claimed.append(relative)
                    if not (self.run_dir / relative).is_file():
                        missing.append(relative)
        self.check(
            f"all {len(claimed)} recorded log paths exist",
            not missing,
            f"missing {missing}",
        )
        self.check(
            "each attempt has its own log files",
            len(claimed) == len(set(claimed)),
            "the same log path is claimed by more than one attempt, so a failing "
            "attempt's output would be overwritten",
        )

    def verify_partial_write(self) -> None:
        declared = self.expected.get("partialWrite")
        if not declared:
            return
        expected_committed = len(declared["committedOnFirstAttempt"])
        expected_retried = len(declared["writtenOnRetry"])

        # The ledger table intentionally has no attempt column, so the split is
        # proven by the posting task's own stage summary plus the ledger's
        # cardinality and primary-key guarantee.
        summary_path = self.run_dir / "stages" / "ledger-posting.json"
        if not summary_path.is_file():
            self.check("ledger posting summary present", False, f"{summary_path} missing")
            return
        summary = load_json(summary_path)
        metrics = summary.get("metrics", {})

        self.check(
            f"recovery attempt found {expected_committed} payments already posted",
            metrics.get("alreadyPosted") == expected_committed,
            f"expected alreadyPosted={expected_committed}, observed {metrics.get('alreadyPosted')}",
        )
        self.check(
            f"recovery attempt inserted only the {expected_retried} missing payments",
            metrics.get("inserted") == expected_retried,
            f"expected inserted={expected_retried}, observed {metrics.get('inserted')}",
        )
        self.check(
            "recovery ran as a later attempt",
            int(summary.get("attempt", 0)) > 1,
            f"attempt recorded as {summary.get('attempt')}",
        )

        database = self.run_dir / "ledger" / "ledger.sqlite3"
        if not database.is_file():
            self.check("partial write ledger present", False, f"{database} missing")
            return
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            total, distinct = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT payment_id) FROM ledger_entry "
                "WHERE state = 'posted'"
            ).fetchone()
        except sqlite3.Error as error:
            self.check("partial write ledger readable", False, str(error))
            return
        finally:
            connection.close()

        self.check(
            "no payment was posted twice across the failed and recovered attempts",
            total == distinct == expected_committed + expected_retried,
            f"rows={total} distinct={distinct} expected={expected_committed + expected_retried}",
        )

    def verify_rerun(self) -> None:
        declared = self.expected.get("rerun")
        if not declared or not declared.get("required"):
            return
        rows = self.manifest.get("ledgerRowsAfterPass") or []
        self.check(
            f"ledger holds {declared['expectedPostedAfterRerun']} rows after the declared rerun",
            bool(rows) and rows[-1] == declared["expectedPostedAfterRerun"],
            f"expected {declared['expectedPostedAfterRerun']}, observed {rows}",
        )
        if len(rows) >= 2:
            added = rows[-1] - rows[-2]
            self.check(
                f"a full rerun added {declared['expectedNewLedgerRowsOnRerun']} ledger rows",
                added == declared["expectedNewLedgerRowsOnRerun"],
                f"expected {declared['expectedNewLedgerRowsOnRerun']}, observed {added}",
            )

    def verify_invariants(self) -> None:
        declared = {item["id"]: item["expected"] for item in self.expected.get("invariants") or []}
        observed = {
            item["id"]: item["status"] == "pass" for item in self.manifest.get("invariants") or []
        }
        for identifier in sorted(declared):
            if identifier == "INV-DETERMINISTIC-RERUN":
                # Proven by comparing two runs, not observable within one.
                continue
            self.check(
                f"invariant {identifier} holds",
                observed.get(identifier) is True,
                "not evaluated by the runtime"
                if identifier not in observed
                else "evaluated as failing",
            )

    def verify_no_leaked_secrets(self) -> None:
        """Customer-visible artifacts must not carry host paths or user names."""
        suspicious: list[str] = []
        for relative in (
            "output/settlement-report.json",
            "output/archive-manifest.json",
            "output/notification.json",
            "run-manifest.json",
        ):
            candidate = self.run_dir / relative
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if "/Users/" in text or "/home/" in text:
                suspicious.append(relative)
        self.check(
            "no absolute host paths in customer-visible outputs",
            not suspicious,
            f"found host paths in {suspicious}",
        )

    def run(self) -> bool:
        self.verify_status()
        self.verify_counts()
        self.verify_exit_sequence()
        self.verify_value_dates()
        self.verify_fx()
        self.verify_matching()
        self.verify_rejections_and_duplicates()
        self.verify_artifacts()
        self.verify_recorded_logs_exist()
        self.verify_partial_write()
        self.verify_rerun()
        self.verify_invariants()
        self.verify_no_leaked_secrets()
        return not self.failures


def normalized_view(run_dir: Path) -> JsonObject:
    """A comparable projection of a run, with volatile fields neutralized."""
    manifest = load_json(run_dir / "run-manifest.json")
    run_id = manifest.get("runId")
    view: JsonObject = {
        "scenario": manifest["scenario"],
        "counts": manifest["counts"],
        "exitSequence": manifest["exitSequence"],
        "ledgerRowsAfterPass": manifest.get("ledgerRowsAfterPass"),
        # Input hashes are hashes of immutable fixtures, so they are stable and
        # worth comparing. Output hashes are not, because outputs embed the run
        # identifier by design.
        "inputHashes": manifest.get("inputHashes"),
        "invariants": [
            {"id": item["id"], "status": item["status"]}
            for item in manifest.get("invariants") or []
        ],
        "stages": {},
    }
    stages_dir = run_dir / "stages"
    if stages_dir.is_dir():
        for path in sorted(stages_dir.glob("*.jsonl")):
            view["stages"][path.name] = normalize(read_jsonl(path), run_dir, run_id)
    for relative in (
        "output/settlement-report.json",
        "output/archive-manifest.json",
        "output/notification.json",
    ):
        candidate = run_dir / relative
        if candidate.is_file():
            view[relative] = normalize(load_json(candidate), run_dir, run_id)
    normalized: JsonObject = normalize(view, run_dir, run_id)
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--kit-root", required=True, type=Path)
    parser.add_argument(
        "--normalize",
        action="store_true",
        help="Emit the normalized comparable view instead of verifying.",
    )
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args()

    run_dir = arguments.run_dir.resolve()
    if not (run_dir / "run-manifest.json").is_file():
        print(f"verify: no run-manifest.json in {run_dir}", file=sys.stderr)
        return 2

    if arguments.normalize:
        print(json.dumps(normalized_view(run_dir), indent=2, sort_keys=True))
        return 0

    verifier = Verifier(kit_root=arguments.kit_root, run_dir=run_dir)
    ok = verifier.run()

    if not arguments.quiet:
        print(
            f"verify: scenario={verifier.scenario} "
            f"checks={len(verifier.checks)} failures={len(verifier.failures)}"
        )
    for failure in verifier.failures:
        print(f"  FAIL {failure}", file=sys.stderr)
    if ok and not arguments.quiet:
        print(f"  all {len(verifier.checks)} checks passed against the fixture oracle")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
