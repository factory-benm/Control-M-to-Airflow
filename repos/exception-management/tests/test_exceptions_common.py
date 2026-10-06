"""Unit tests for exception_management.common helpers (standard library only)."""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exception_management import common

KIT_ROOT = Path(__file__).resolve().parents[3]
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("PAYOPS_")}


class TestLogging(unittest.TestCase):
    def test_log_uses_named_logger_at_info(self) -> None:
        with self.assertLogs("exception-mgmt", level="INFO") as cm:
            common.log("classified 3 breaks")
        self.assertEqual(cm.output, ["INFO:exception-mgmt:classified 3 breaks"])


class TestBusinessRuleError(unittest.TestCase):
    def test_carries_code_and_message(self) -> None:
        err = common.BusinessRuleError("UNKNOWN_BREAK_REASON", "no classification")
        self.assertEqual(err.code, "UNKNOWN_BREAK_REASON")
        self.assertEqual(str(err), "no classification")


class TestJsonIo(unittest.TestCase):
    def test_sha256_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.bin"
            p.write_bytes(b"breaks")
            self.assertEqual(common.sha256_file(p), hashlib.sha256(b"breaks").hexdigest())

    def test_write_json_creates_parent_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "stages" / "exceptions.json"
            common.write_json(p, {"z": 1, "a": [1, 2]})
            self.assertEqual(
                p.read_text(encoding="utf-8"), '{\n  "a": [\n    1,\n    2\n  ],\n  "z": 1\n}\n'
            )
            self.assertEqual(common.read_json(p), {"z": 1, "a": [1, 2]})

    def test_read_jsonl_skips_empty_lines(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "breaks.jsonl"
            p.write_text('{"payment_id":"P2"}\n\n{"payment_id":"P1"}\n', encoding="utf-8")
            self.assertEqual(common.read_jsonl(p), [{"payment_id": "P2"}, {"payment_id": "P1"}])

    def test_read_jsonl_missing_file_is_missing_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as d, self.assertRaises(common.MissingUpstreamError):
            common.read_jsonl(Path(d) / "absent.jsonl")

    def test_write_jsonl_sorts_records_and_keys(self) -> None:
        records: list[dict[str, Any]] = [
            {"payment_id": "P2", "line_number": 1},
            {"line_number": 9, "payment_id": "P1"},
            {"payment_id": "P1", "line_number": 3},
            {"note": "no id"},
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nested" / "out.jsonl"
            common.write_jsonl(p, records)
            self.assertEqual(
                p.read_text(encoding="utf-8"),
                '{"note":"no id"}\n'
                '{"line_number":3,"payment_id":"P1"}\n'
                '{"line_number":9,"payment_id":"P1"}\n'
                '{"line_number":1,"payment_id":"P2"}\n',
            )

    def test_sort_key_defaults(self) -> None:
        self.assertEqual(common.sort_key({}), ("", 0))
        self.assertEqual(common.sort_key({"payment_id": "P", "line_number": 4}), ("P", 4))

    def test_artifacts_for_skips_missing_and_sorts(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            (rd / "stages").mkdir()
            (rd / "stages" / "z.json").write_bytes(b"z")
            (rd / "stages" / "a.jsonl").write_bytes(b"a")
            got = common.artifacts_for(
                rd, ["stages/z.json", "stages/missing.json", "stages/a.jsonl"]
            )
        self.assertEqual(
            got,
            [
                {"path": "stages/a.jsonl", "sha256": hashlib.sha256(b"a").hexdigest()},
                {"path": "stages/z.json", "sha256": hashlib.sha256(b"z").hexdigest()},
            ],
        )

    def test_emit_result_writes_one_indented_json_object(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            common.emit_result({"b": 1, "a": "x"})
        self.assertEqual(out.getvalue(), '{\n  "a": "x",\n  "b": 1\n}\n')
        self.assertEqual(json.loads(out.getvalue()), {"a": "x", "b": 1})


class TestResolveKitRoot(unittest.TestCase):
    def test_explicit_arg(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "fixtures").mkdir()
            self.assertEqual(common.resolve_kit_root(d, Path("/")), Path(d).resolve())

    def test_explicit_arg_without_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as d, self.assertRaises(common.UsageError) as ctx:
            common.resolve_kit_root(d, KIT_ROOT)
        self.assertIn("kit root has no fixtures/", str(ctx.exception))

    def test_env_used_when_it_has_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "fixtures").mkdir()
            env = dict(CLEAN_ENV, PAYOPS_KIT_ROOT=d)
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(common.resolve_kit_root(None, KIT_ROOT), Path(d).resolve())

    def test_env_without_fixtures_falls_back_to_walking_up(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env = dict(CLEAN_ENV, PAYOPS_KIT_ROOT=d)
            nested = KIT_ROOT / "repos" / "exception-management"
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(common.resolve_kit_root(None, nested), KIT_ROOT)

    def test_no_fixtures_anywhere(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            if any((p / "fixtures").is_dir() for p in [root, *root.parents]):
                self.skipTest("an ancestor of the temp directory contains fixtures/")
            with (
                mock.patch.dict(os.environ, CLEAN_ENV, clear=True),
                self.assertRaises(common.UsageError),
            ):
                common.resolve_kit_root(None, root)


class TestScenarioAndResolvers(unittest.TestCase):
    def test_load_scenario(self) -> None:
        cfg = common.load_scenario(KIT_ROOT, "reconciliation-breaks")
        self.assertIn("fixedUtc", cfg["clock"])

    def test_load_missing_scenario_is_usage_error(self) -> None:
        with self.assertRaises(common.UsageError) as ctx:
            common.load_scenario(KIT_ROOT, "nope")
        self.assertEqual(str(ctx.exception), "scenario not found: nope")

    def test_resolve_now_precedence(self) -> None:
        cfg = {"clock": {"fixedUtc": "2026-03-16T09:30:00Z"}}
        env = dict(CLEAN_ENV, PAYOPS_FIXED_CLOCK="2026-05-05T00:00:00Z")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(common.resolve_now("arg-clock", cfg), "arg-clock")
            self.assertEqual(common.resolve_now(None, cfg), "2026-05-05T00:00:00Z")
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=True):
            self.assertEqual(common.resolve_now(None, cfg), "2026-03-16T09:30:00Z")

    def test_resolve_attempt_precedence(self) -> None:
        with mock.patch.dict(os.environ, dict(CLEAN_ENV, PAYOPS_ATTEMPT="4"), clear=True):
            self.assertEqual(common.resolve_attempt("2"), 2)
            self.assertEqual(common.resolve_attempt(None), 4)
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=True):
            self.assertEqual(common.resolve_attempt(None), 1)

    def test_resolve_run_id(self) -> None:
        self.assertEqual(common.resolve_run_id("rid-1", Path("/runs/abc")), "rid-1")
        self.assertEqual(common.resolve_run_id(None, Path("/runs/abc")), "abc")


if __name__ == "__main__":
    unittest.main()
