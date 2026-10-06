"""Unit checks for the shared helpers in ledger_reconciliation.common."""

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ledger_test_support import KIT_ROOT

from ledger_reconciliation import common

CLEAN_ENV = {"PAYOPS_KIT_ROOT": "", "PAYOPS_FIXED_CLOCK": "", "PAYOPS_ATTEMPT": ""}


class _TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()


class TestJsonFiles(_TempDirTest):
    def test_read_jsonl_skips_blank_lines(self) -> None:
        path = self.dir / "in.jsonl"
        path.write_text('{"a": 1}\n\n{"a": 2}\n', encoding="utf-8")
        self.assertEqual(common.read_jsonl(path), [{"a": 1}, {"a": 2}])

    def test_read_jsonl_missing_is_missing_upstream(self) -> None:
        with self.assertRaisesRegex(common.MissingUpstreamError, "upstream artifact not found"):
            common.read_jsonl(self.dir / "absent.jsonl")

    def test_write_jsonl_sorts_by_payment_then_line_and_is_compact(self) -> None:
        path = self.dir / "nested" / "out.jsonl"
        common.write_jsonl(
            path,
            [
                {"payment_id": "P2", "line_number": 1},
                {"payment_id": "P1", "line_number": 9, "z": 1, "a": 0},
                {"payment_id": "P1", "line_number": 3},
            ],
        )
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            '{"line_number":3,"payment_id":"P1"}\n'
            '{"a":0,"line_number":9,"payment_id":"P1","z":1}\n'
            '{"line_number":1,"payment_id":"P2"}\n',
        )

    def test_write_jsonl_with_no_records_writes_empty_file(self) -> None:
        path = self.dir / "empty.jsonl"
        common.write_jsonl(path, [])
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_sort_key_defaults_missing_fields(self) -> None:
        self.assertEqual(common.sort_key({}), ("", 0))

    def test_write_json_is_sorted_indented_and_round_trips(self) -> None:
        path = self.dir / "deep" / "doc.json"
        common.write_json(path, {"b": 1, "a": [1, 2]})
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith('{\n  "a": [\n'))
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(common.read_json(path), {"a": [1, 2], "b": 1})


class TestArtifacts(_TempDirTest):
    def test_sha256_file_matches_hashlib(self) -> None:
        path = self.dir / "blob.bin"
        data = b"x" * 70000
        path.write_bytes(data)
        self.assertEqual(common.sha256_file(path), hashlib.sha256(data).hexdigest())

    def test_artifacts_for_sorts_and_skips_missing(self) -> None:
        (self.dir / "stages").mkdir()
        (self.dir / "stages" / "b.json").write_text("b", encoding="utf-8")
        (self.dir / "stages" / "a.json").write_text("a", encoding="utf-8")
        items = common.artifacts_for(self.dir, ["stages/b.json", "stages/missing", "stages/a.json"])
        self.assertEqual(
            items,
            [
                {"path": "stages/a.json", "sha256": hashlib.sha256(b"a").hexdigest()},
                {"path": "stages/b.json", "sha256": hashlib.sha256(b"b").hexdigest()},
            ],
        )


class TestKitRoot(_TempDirTest):
    def test_explicit_kit_root_wins(self) -> None:
        (self.dir / "fixtures").mkdir()
        self.assertEqual(common.resolve_kit_root(str(self.dir), KIT_ROOT), self.dir)

    def test_explicit_kit_root_without_fixtures_is_usage_error(self) -> None:
        with self.assertRaisesRegex(common.UsageError, "kit root has no fixtures/ directory"):
            common.resolve_kit_root(str(self.dir), KIT_ROOT)

    def test_environment_kit_root(self) -> None:
        (self.dir / "fixtures").mkdir()
        with mock.patch.dict(os.environ, {"PAYOPS_KIT_ROOT": str(self.dir)}):
            self.assertEqual(common.resolve_kit_root(None, KIT_ROOT), self.dir)

    def test_invalid_environment_falls_back_to_upward_search(self) -> None:
        start = self.dir / "repos" / "ledger"
        start.mkdir(parents=True)
        (self.dir / "fixtures").mkdir()
        with mock.patch.dict(os.environ, {"PAYOPS_KIT_ROOT": str(self.dir / "nowhere")}):
            self.assertEqual(common.resolve_kit_root(None, start), self.dir)

    def test_no_kit_root_anywhere_is_usage_error(self) -> None:
        with (
            mock.patch.dict(os.environ, CLEAN_ENV),
            self.assertRaisesRegex(common.UsageError, "could not locate kit root"),
        ):
            common.resolve_kit_root(None, self.dir)


class TestScenarioResolution(unittest.TestCase):
    def test_load_scenario_reads_fixture(self) -> None:
        cfg = common.load_scenario(KIT_ROOT, "partial-ledger-write")
        self.assertEqual(cfg["id"], "partial-ledger-write")
        self.assertEqual(cfg["faultInjection"]["afterWrites"], 4)

    def test_unknown_scenario_is_usage_error(self) -> None:
        with self.assertRaisesRegex(common.UsageError, "scenario not found: nope"):
            common.load_scenario(KIT_ROOT, "nope")

    def test_resolve_now_precedence(self) -> None:
        cfg = {"clock": {"fixedUtc": "2026-01-01T00:00:00Z"}}
        with mock.patch.dict(os.environ, {"PAYOPS_FIXED_CLOCK": "2026-02-02T00:00:00Z"}):
            self.assertEqual(
                common.resolve_now("2026-03-03T00:00:00Z", cfg), "2026-03-03T00:00:00Z"
            )
            self.assertEqual(common.resolve_now(None, cfg), "2026-02-02T00:00:00Z")
        with mock.patch.dict(os.environ, CLEAN_ENV):
            self.assertEqual(common.resolve_now(None, cfg), "2026-01-01T00:00:00Z")

    def test_resolve_attempt_precedence(self) -> None:
        with mock.patch.dict(os.environ, {"PAYOPS_ATTEMPT": "4"}):
            self.assertEqual(common.resolve_attempt("2"), 2)
            self.assertEqual(common.resolve_attempt(None), 4)
        with mock.patch.dict(os.environ, CLEAN_ENV):
            self.assertEqual(common.resolve_attempt(None), 1)

    def test_resolve_run_id_defaults_to_run_dir_name(self) -> None:
        self.assertEqual(common.resolve_run_id("explicit", Path("/tmp/x/run-7")), "explicit")
        self.assertEqual(common.resolve_run_id(None, Path("/tmp/x/run-7")), "run-7")


class TestOutput(unittest.TestCase):
    def test_emit_result_writes_one_sorted_json_object(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            common.emit_result({"b": 2, "a": 1})
        self.assertEqual(json.loads(out.getvalue()), {"a": 1, "b": 2})
        self.assertLess(out.getvalue().index('"a"'), out.getvalue().index('"b"'))

    def test_log_goes_to_stderr_with_prefix(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            common.log("hello")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "[ledger-recon] hello\n")

    def test_business_rule_error_carries_code(self) -> None:
        error = common.BusinessRuleError("RULE_X", "broken rule")
        self.assertEqual(error.code, "RULE_X")
        self.assertEqual(str(error), "broken rule")


if __name__ == "__main__":
    unittest.main()
