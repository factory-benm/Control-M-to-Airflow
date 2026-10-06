"""Unit tests for payment_ingestion.common helpers (standard library only)."""

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from payment_ingestion.common import (
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
    write_json,
)

KIT_ROOT = Path(__file__).resolve().parents[3]


class TestLog(unittest.TestCase):
    def test_writes_message_to_stderr_only(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            log("hello diagnostics")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "hello diagnostics\n")


class TestFileHelpers(unittest.TestCase):
    def test_ensure_dir_creates_nested_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "a" / "b" / "c"
            ensure_dir(target)
            ensure_dir(str(target))
            self.assertTrue(target.is_dir())

    def test_write_json_is_sorted_indented_and_newline_terminated(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "out.json"
            write_json(p, {"b": 1, "a": {"d": 2, "c": 3}})
            self.assertEqual(
                p.read_text(encoding="utf-8"),
                '{\n  "a": {\n    "c": 3,\n    "d": 2\n  },\n  "b": 1\n}\n',
            )

    def test_read_jsonl_skips_blank_and_whitespace_lines(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "in.jsonl"
            p.write_text('{"a":1}\n\n   \n  {"b":2}  \n', encoding="utf-8")
            self.assertEqual(read_jsonl(p), [{"a": 1}, {"b": 2}])

    def test_artifact_entry_hashes_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "stages").mkdir()
            (Path(d) / "stages" / "x.json").write_bytes(b"{}\n")
            entry = artifact_entry(d, "stages/x.json")
            self.assertEqual(
                entry,
                {"path": "stages/x.json", "sha256": hashlib.sha256(b"{}\n").hexdigest()},
            )


class TestResolveKitRoot(unittest.TestCase):
    def test_explicit_arg_with_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "fixtures").mkdir()
            got = resolve_kit_root(d, {"PAYOPS_KIT_ROOT": "/nonexistent"}, "/")
            self.assertEqual(got, Path(d).resolve())

    def test_explicit_arg_without_fixtures_is_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as d, self.assertRaises(UsageError) as ctx:
            resolve_kit_root(d, {}, d)
        self.assertIn("--kit-root", str(ctx.exception))

    def test_env_with_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "fixtures").mkdir()
            got = resolve_kit_root(None, {"PAYOPS_KIT_ROOT": d}, "/")
            self.assertEqual(got, Path(d).resolve())

    def test_env_without_fixtures_is_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as d, self.assertRaises(UsageError) as ctx:
            resolve_kit_root(None, {"PAYOPS_KIT_ROOT": d}, d)
        self.assertIn("PAYOPS_KIT_ROOT", str(ctx.exception))

    def test_walks_up_from_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "fixtures").mkdir()
            nested = Path(d) / "repos" / "payment-ingestion"
            nested.mkdir(parents=True)
            self.assertEqual(resolve_kit_root("", {}, nested), Path(d).resolve())

    def test_no_fixtures_anywhere_is_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            if any((p / "fixtures").is_dir() for p in [Path(d).resolve(), *Path(d).parents]):
                self.skipTest("an ancestor of the temp directory contains fixtures/")
            with self.assertRaises(UsageError):
                resolve_kit_root(None, {}, d)


class TestLoadScenario(unittest.TestCase):
    def test_loads_real_scenario(self) -> None:
        doc = load_scenario(KIT_ROOT, "happy-path")
        self.assertEqual(doc["clock"]["fixedUtc"], "2026-03-16T09:30:00Z")

    def test_missing_scenario_is_missing_upstream(self) -> None:
        with self.assertRaises(MissingUpstreamError) as ctx:
            load_scenario(KIT_ROOT, "no-such-scenario")
        self.assertIn("no-such-scenario", str(ctx.exception))


SCENARIO_WITH_CLOCK = {"clock": {"fixedUtc": "2026-03-16T09:30:00Z"}}


class TestResolveFixedClock(unittest.TestCase):
    def test_arg_wins(self) -> None:
        got = resolve_fixed_clock("2026-01-01T00:00:00Z", {"PAYOPS_FIXED_CLOCK": "x"}, {})
        self.assertEqual(got, "2026-01-01T00:00:00Z")

    def test_env_before_scenario(self) -> None:
        got = resolve_fixed_clock(None, {"PAYOPS_FIXED_CLOCK": "env-clock"}, SCENARIO_WITH_CLOCK)
        self.assertEqual(got, "env-clock")

    def test_scenario_fallback(self) -> None:
        self.assertEqual(resolve_fixed_clock(None, {}, SCENARIO_WITH_CLOCK), "2026-03-16T09:30:00Z")

    def test_no_clock_is_usage_error(self) -> None:
        with self.assertRaises(UsageError):
            resolve_fixed_clock(None, {}, {"clock": {}})
        with self.assertRaises(UsageError):
            resolve_fixed_clock("", {"PAYOPS_FIXED_CLOCK": ""}, {})


class TestParseCliArgs(unittest.TestCase):
    def test_all_flags_parsed_and_unset_default_to_none(self) -> None:
        opts = parse_cli_args(["--run-dir", "/r", "--task", "t", "--attempt", "2"])
        self.assertEqual(
            opts,
            {
                "run_dir": "/r",
                "scenario": None,
                "task": "t",
                "kit_root": None,
                "run_id": None,
                "now": None,
                "attempt": "2",
            },
        )

    def test_help_returns_none(self) -> None:
        self.assertIsNone(parse_cli_args(["--task", "t", "--help"]))
        self.assertIsNone(parse_cli_args(["-h"]))

    def test_missing_value_is_usage_error(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            parse_cli_args(["--run-dir"])
        self.assertEqual(str(ctx.exception), "missing value for --run-dir")

    def test_unknown_argument_is_usage_error(self) -> None:
        with self.assertRaises(UsageError) as ctx:
            parse_cli_args(["--bogus", "1"])
        self.assertEqual(str(ctx.exception), "unknown argument: --bogus")


class TestWriteJsonRoundTrip(unittest.TestCase):
    def test_write_json_output_parses_back(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "o.json"
            obj = {"files": [{"path": "input/payments.csv", "byteSize": 10}]}
            write_json(str(p), obj)
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), obj)


if __name__ == "__main__":
    unittest.main()
