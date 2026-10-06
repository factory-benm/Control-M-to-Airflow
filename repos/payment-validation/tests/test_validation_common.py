"""Unit tests for payment-validation shared helpers (standard library only)."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payment_validation.common import (
    MissingUpstreamError,
    UsageError,
    artifact_entry,
    ensure_dir,
    load_scenario,
    parse_cli_args,
    read_jsonl,
    resolve_fixed_clock,
    resolve_kit_root,
    sha256_file,
    write_json,
    write_jsonl,
)

KIT_ROOT = Path(__file__).resolve().parents[3]


class TestParseCliArgs(unittest.TestCase):
    def test_all_flags_parsed(self) -> None:
        opts = parse_cli_args(
            [
                "--run-dir",
                "/tmp/run",
                "--scenario",
                "happy-path",
                "--task",
                "validate_payment_schema",
                "--kit-root",
                "/kit",
                "--run-id",
                "r1",
                "--now",
                "2026-03-16T09:30:00Z",
                "--attempt",
                "2",
            ]
        )
        self.assertEqual(
            opts,
            {
                "run_dir": "/tmp/run",
                "scenario": "happy-path",
                "task": "validate_payment_schema",
                "kit_root": "/kit",
                "run_id": "r1",
                "now": "2026-03-16T09:30:00Z",
                "attempt": "2",
            },
        )

    def test_absent_flags_default_to_none(self) -> None:
        opts = parse_cli_args(["--task", "deduplicate_payments"])
        assert opts is not None
        self.assertEqual(opts["task"], "deduplicate_payments")
        self.assertIsNone(opts["run_dir"])
        self.assertIsNone(opts["attempt"])

    def test_help_returns_none(self) -> None:
        self.assertIsNone(parse_cli_args(["--task", "x", "--help"]))
        self.assertIsNone(parse_cli_args(["-h"]))

    def test_missing_value_is_usage_error(self) -> None:
        with self.assertRaisesRegex(UsageError, "missing value for --scenario"):
            parse_cli_args(["--scenario"])

    def test_unknown_argument_is_usage_error(self) -> None:
        with self.assertRaisesRegex(UsageError, "unknown argument: --verbose"):
            parse_cli_args(["--verbose"])


class TestResolveKitRoot(unittest.TestCase):
    def test_explicit_arg_with_fixtures(self) -> None:
        self.assertEqual(resolve_kit_root(str(KIT_ROOT), {}, "/"), KIT_ROOT)

    def test_explicit_arg_without_fixtures(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(UsageError, "--kit-root"),
        ):
            resolve_kit_root(tmp, {"PAYOPS_KIT_ROOT": str(KIT_ROOT)}, KIT_ROOT)

    def test_arg_takes_precedence_over_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "fixtures").mkdir()
            got = resolve_kit_root(tmp, {"PAYOPS_KIT_ROOT": str(KIT_ROOT)}, KIT_ROOT)
            self.assertEqual(got, Path(tmp).resolve())

    def test_env_with_fixtures(self) -> None:
        self.assertEqual(resolve_kit_root(None, {"PAYOPS_KIT_ROOT": str(KIT_ROOT)}, "/"), KIT_ROOT)

    def test_env_without_fixtures(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(UsageError, "PAYOPS_KIT_ROOT"),
        ):
            resolve_kit_root(None, {"PAYOPS_KIT_ROOT": tmp}, KIT_ROOT)

    def test_walks_up_from_repo_root(self) -> None:
        repo_root = KIT_ROOT / "repos" / "payment-validation"
        self.assertEqual(resolve_kit_root(None, {}, repo_root), KIT_ROOT)

    def test_no_fixtures_anywhere(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(UsageError, "could not locate kit root"),
        ):
            resolve_kit_root(None, {}, tmp)


class TestLoadScenario(unittest.TestCase):
    def test_loads_fixture_scenario(self) -> None:
        doc = load_scenario(KIT_ROOT, "happy-path")
        self.assertEqual(doc["id"], "happy-path")
        self.assertEqual(doc["clock"]["fixedUtc"], "2026-03-16T09:30:00Z")

    def test_missing_scenario_is_missing_upstream(self) -> None:
        with self.assertRaisesRegex(MissingUpstreamError, "scenario not found: nope"):
            load_scenario(KIT_ROOT, "nope")


class TestResolveFixedClock(unittest.TestCase):
    SCENARIO: ClassVar[dict[str, Any]] = {"clock": {"fixedUtc": "2026-03-16T09:30:00Z"}}

    def test_arg_wins(self) -> None:
        env = {"PAYOPS_FIXED_CLOCK": "2026-01-01T00:00:00Z"}
        got = resolve_fixed_clock("2026-02-02T00:00:00Z", env, self.SCENARIO)
        self.assertEqual(got, "2026-02-02T00:00:00Z")

    def test_env_beats_scenario(self) -> None:
        env = {"PAYOPS_FIXED_CLOCK": "2026-01-01T00:00:00Z"}
        self.assertEqual(resolve_fixed_clock(None, env, self.SCENARIO), "2026-01-01T00:00:00Z")

    def test_scenario_fallback(self) -> None:
        self.assertEqual(resolve_fixed_clock(None, {}, self.SCENARIO), "2026-03-16T09:30:00Z")

    def test_no_clock_is_usage_error(self) -> None:
        with self.assertRaises(UsageError):
            resolve_fixed_clock(None, {}, {})
        with self.assertRaises(UsageError):
            resolve_fixed_clock("", {"PAYOPS_FIXED_CLOCK": ""}, {"clock": {}})


class TestFileHelpers(unittest.TestCase):
    def test_sha256_matches_hashlib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob.bin"
            data = b"payops" * 20000
            path.write_bytes(data)
            self.assertEqual(sha256_file(path), hashlib.sha256(data).hexdigest())

    def test_ensure_dir_creates_nested_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a" / "b"
            ensure_dir(target)
            ensure_dir(target)
            self.assertTrue(target.is_dir())

    def test_write_json_is_sorted_and_newline_terminated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meta.json"
            write_json(path, {"b": 1, "a": {"d": 2, "c": 3}})
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.endswith("}\n"))
            self.assertLess(text.index('"a"'), text.index('"b"'))
            self.assertEqual(json.loads(text), {"a": {"c": 3, "d": 2}, "b": 1})

    def test_jsonl_round_trip_sorted_by_payment_then_line(self) -> None:
        records = [
            {"payment_id": "PAY-2026-0002", "line_number": 1},
            {"payment_id": "PAY-2026-0001", "line_number": 5},
            {"payment_id": "PAY-2026-0001", "line_number": 3},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            write_jsonl(path, records)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], '{"line_number":3,"payment_id":"PAY-2026-0001"}')
            self.assertEqual(
                [(r["payment_id"], r["line_number"]) for r in read_jsonl(path)],
                [("PAY-2026-0001", 3), ("PAY-2026-0001", 5), ("PAY-2026-0002", 1)],
            )

    def test_read_jsonl_skips_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.jsonl"
            path.write_text('{"a": 1}\n\n   \n{"a": 2}\n', encoding="utf-8")
            self.assertEqual(read_jsonl(path), [{"a": 1}, {"a": 2}])

    def test_artifact_entry_hashes_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "stages").mkdir()
            (Path(tmp) / "stages" / "x.json").write_bytes(b"{}\n")
            self.assertEqual(
                artifact_entry(tmp, "stages/x.json"),
                {"path": "stages/x.json", "sha256": hashlib.sha256(b"{}\n").hexdigest()},
            )


if __name__ == "__main__":
    unittest.main()
