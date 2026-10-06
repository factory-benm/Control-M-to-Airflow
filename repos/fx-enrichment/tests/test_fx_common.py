"""Unit tests for fx-enrichment shared helpers (standard library only)."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fx_support import KIT_ROOT

from fx_enrichment.common import (
    MissingUpstreamError,
    UsageError,
    artifact_entry,
    ensure_dir,
    load_scenario,
    log,
    parse_cli_args,
    read_jsonl,
    resolve_fixed_clock,
    resolve_kit_root,
    sha256_file,
    write_json,
    write_jsonl,
)


class TestFileHelpers(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_sha256_file_matches_hashlib(self) -> None:
        path = self.tmp / "data.bin"
        payload = b"cross-border" * 10000
        path.write_bytes(payload)
        self.assertEqual(sha256_file(path), hashlib.sha256(payload).hexdigest())
        self.assertEqual(sha256_file(str(path)), hashlib.sha256(payload).hexdigest())

    def test_ensure_dir_creates_nested_and_is_idempotent(self) -> None:
        target = self.tmp / "a" / "b" / "c"
        ensure_dir(target)
        ensure_dir(str(target))
        self.assertTrue(target.is_dir())

    def test_write_json_sorted_indented_with_trailing_newline(self) -> None:
        path = self.tmp / "out.json"
        write_json(path, {"b": 1, "a": {"d": 2, "c": 3}})
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps({"a": {"c": 3, "d": 2}, "b": 1}, indent=2) + "\n")

    def test_write_jsonl_sorts_by_payment_id_then_line_number(self) -> None:
        path = self.tmp / "rows.jsonl"
        write_jsonl(
            path,
            [
                {"payment_id": "PAY-2", "line_number": 3, "z": 1},
                {"payment_id": "PAY-1", "line_number": 9},
                {"payment_id": "PAY-1", "line_number": 2},
            ],
        )
        lines = path.read_text(encoding="utf-8").split("\n")
        self.assertEqual(
            lines,
            [
                '{"line_number":2,"payment_id":"PAY-1"}',
                '{"line_number":9,"payment_id":"PAY-1"}',
                '{"line_number":3,"payment_id":"PAY-2","z":1}',
                "",
            ],
        )

    def test_write_jsonl_tolerates_missing_sort_keys(self) -> None:
        path = self.tmp / "rows.jsonl"
        write_jsonl(path, [{"payment_id": "PAY-1"}, {"other": True}])
        self.assertEqual(read_jsonl(path), [{"other": True}, {"payment_id": "PAY-1"}])

    def test_read_jsonl_skips_blank_lines(self) -> None:
        path = self.tmp / "rows.jsonl"
        path.write_text('{"a": 1}\n\n   \n{"b": 2}\n', encoding="utf-8")
        self.assertEqual(read_jsonl(path), [{"a": 1}, {"b": 2}])

    def test_read_jsonl_raises_on_malformed_line(self) -> None:
        path = self.tmp / "rows.jsonl"
        path.write_text('{"a": 1}\nnot-json\n', encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            read_jsonl(path)

    def test_artifact_entry_uses_relative_path_and_file_hash(self) -> None:
        (self.tmp / "stages").mkdir()
        (self.tmp / "stages" / "fx.json").write_bytes(b"{}\n")
        entry = artifact_entry(str(self.tmp), "stages/fx.json")
        self.assertEqual(
            entry, {"path": "stages/fx.json", "sha256": hashlib.sha256(b"{}\n").hexdigest()}
        )

    def test_log_writes_prefixed_line_to_stderr(self) -> None:
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            log(42)
        self.assertEqual(err.getvalue(), "[fx-enrichment] 42\n")
        self.assertEqual(out.getvalue(), "")


class TestResolveKitRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def test_explicit_argument_wins_over_env(self) -> None:
        env = {"PAYOPS_KIT_ROOT": str(self.tmp)}
        self.assertEqual(resolve_kit_root(str(KIT_ROOT), env, self.tmp), KIT_ROOT)

    def test_explicit_argument_without_fixtures_is_usage_error(self) -> None:
        with self.assertRaisesRegex(UsageError, "--kit-root does not contain fixtures/"):
            resolve_kit_root(str(self.tmp), {}, KIT_ROOT)

    def test_env_used_when_no_argument(self) -> None:
        (self.tmp / "fixtures").mkdir()
        env = {"PAYOPS_KIT_ROOT": str(self.tmp)}
        self.assertEqual(resolve_kit_root(None, env, KIT_ROOT), self.tmp)

    def test_env_without_fixtures_is_usage_error(self) -> None:
        env = {"PAYOPS_KIT_ROOT": str(self.tmp)}
        with self.assertRaisesRegex(UsageError, "PAYOPS_KIT_ROOT does not contain fixtures/"):
            resolve_kit_root(None, env, KIT_ROOT)

    def test_empty_values_fall_through_to_repository_search(self) -> None:
        repo_root = KIT_ROOT / "repos" / "fx-enrichment"
        self.assertEqual(resolve_kit_root("", {"PAYOPS_KIT_ROOT": ""}, repo_root), KIT_ROOT)

    def test_search_accepts_the_start_directory_itself(self) -> None:
        (self.tmp / "fixtures").mkdir()
        self.assertEqual(resolve_kit_root(None, {}, str(self.tmp)), self.tmp)

    def test_search_without_fixtures_anywhere_is_usage_error(self) -> None:
        start = self.tmp / "deep" / "repo"
        start.mkdir(parents=True)
        if any((p / "fixtures").is_dir() for p in [start, *start.parents]):
            self.skipTest("a parent of the temp directory contains fixtures/")
        with self.assertRaisesRegex(UsageError, "could not locate kit root"):
            resolve_kit_root(None, {}, start)


class TestLoadScenario(unittest.TestCase):
    def test_loads_fixture_scenario(self) -> None:
        doc = load_scenario(KIT_ROOT, "happy-path")
        self.assertEqual(doc["clock"]["fixedUtc"], "2026-03-16T09:30:00Z")
        self.assertEqual(doc["fxTable"], "fixtures/fx/fx-rates.json")

    def test_unknown_scenario_is_missing_upstream(self) -> None:
        with self.assertRaisesRegex(MissingUpstreamError, "scenario not found: nope"):
            load_scenario(str(KIT_ROOT), "nope")


SCENARIO_WITH_CLOCK: dict[str, Any] = {"clock": {"fixedUtc": "2026-03-16T09:30:00Z"}}


class TestResolveFixedClock(unittest.TestCase):
    def test_argument_wins(self) -> None:
        env = {"PAYOPS_FIXED_CLOCK": "2026-01-01T00:00:00Z"}
        self.assertEqual(
            resolve_fixed_clock("2026-02-02T00:00:00Z", env, SCENARIO_WITH_CLOCK),
            "2026-02-02T00:00:00Z",
        )

    def test_env_beats_scenario(self) -> None:
        env = {"PAYOPS_FIXED_CLOCK": "2026-01-01T00:00:00Z"}
        self.assertEqual(
            resolve_fixed_clock(None, env, SCENARIO_WITH_CLOCK), "2026-01-01T00:00:00Z"
        )

    def test_scenario_clock_is_last_resort(self) -> None:
        self.assertEqual(resolve_fixed_clock(None, {}, SCENARIO_WITH_CLOCK), "2026-03-16T09:30:00Z")

    def test_no_clock_anywhere_is_usage_error(self) -> None:
        docs: list[dict[str, Any]] = [{}, {"clock": {}}, {"clock": {"fixedUtc": ""}}]
        for doc in docs:
            with self.subTest(doc=doc), self.assertRaisesRegex(UsageError, "no fixed clock"):
                resolve_fixed_clock(None, {}, doc)


class TestParseCliArgs(unittest.TestCase):
    def test_all_flags_mapped_to_snake_case_keys(self) -> None:
        argv = [
            "--run-dir",
            "/r",
            "--scenario",
            "happy-path",
            "--task",
            "enrich_fx_rates",
            "--kit-root",
            "/k",
            "--run-id",
            "rid",
            "--now",
            "2026-03-16T09:30:00Z",
            "--attempt",
            "2",
        ]
        self.assertEqual(
            parse_cli_args(argv),
            {
                "run_dir": "/r",
                "scenario": "happy-path",
                "task": "enrich_fx_rates",
                "kit_root": "/k",
                "run_id": "rid",
                "now": "2026-03-16T09:30:00Z",
                "attempt": "2",
            },
        )

    def test_absent_flags_default_to_none(self) -> None:
        opts = parse_cli_args(["--task", "enrich_fx_rates"])
        assert opts is not None
        self.assertEqual(opts["task"], "enrich_fx_rates")
        self.assertIsNone(opts["run_dir"])
        self.assertIsNone(opts["attempt"])

    def test_help_returns_none(self) -> None:
        self.assertIsNone(parse_cli_args(["--task", "x", "--help"]))
        self.assertIsNone(parse_cli_args(["-h"]))

    def test_flag_without_value_is_usage_error(self) -> None:
        with self.assertRaisesRegex(UsageError, "missing value for --scenario"):
            parse_cli_args(["--task", "x", "--scenario"])

    def test_unknown_argument_is_usage_error(self) -> None:
        with self.assertRaisesRegex(UsageError, "unknown argument: --verbose"):
            parse_cli_args(["--verbose"])


if __name__ == "__main__":
    unittest.main()
