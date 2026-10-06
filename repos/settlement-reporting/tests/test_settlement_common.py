"""Unit tests for settlement-reporting shared helpers (standard library only)."""

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from settlement_reporting import common

KIT_ROOT = Path(__file__).resolve().parents[3]
PAYOPS_ENV = ("PAYOPS_KIT_ROOT", "PAYOPS_FIXED_CLOCK", "PAYOPS_ATTEMPT", "PAYOPS_REPO_ROOT")


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in PAYOPS_ENV}


class TestJsonIo(unittest.TestCase):
    def test_write_json_creates_parents_and_sorts_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "out.json"
            common.write_json(path, {"z": 1, "a": [1, 2]})
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.endswith("\n"))
            self.assertLess(text.index('"a"'), text.index('"z"'))
            self.assertEqual(common.read_json(path), {"a": [1, 2], "z": 1})

    def test_read_jsonl_skips_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.jsonl"
            path.write_text('{"payment_id": "A"}\n\n{"payment_id": "B"}\n', encoding="utf-8")
            self.assertEqual(common.read_jsonl(path), [{"payment_id": "A"}, {"payment_id": "B"}])

    def test_read_jsonl_missing_is_missing_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.jsonl"
            with self.assertRaisesRegex(common.MissingUpstreamError, "absent.jsonl"):
                common.read_jsonl(missing)

    def test_read_jsonl_optional_missing_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(common.read_jsonl_optional(Path(tmp) / "absent.jsonl"), [])

    def test_read_jsonl_optional_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.jsonl"
            path.write_text('{"n": 1}\n', encoding="utf-8")
            self.assertEqual(common.read_jsonl_optional(path), [{"n": 1}])


class TestHashing(unittest.TestCase):
    def test_sha256_matches_hashlib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob"
            data = b"settlement" * 10000
            path.write_bytes(data)
            self.assertEqual(common.sha256_file(path), hashlib.sha256(data).hexdigest())

    def test_artifacts_for_skips_missing_and_sorts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "output").mkdir()
            (run_dir / "output" / "b.json").write_bytes(b"b")
            (run_dir / "output" / "a.json").write_bytes(b"a")
            items = common.artifacts_for(
                run_dir, ["output/b.json", "output/missing.json", "output/a.json"]
            )
            self.assertEqual(
                items,
                [
                    {"path": "output/a.json", "sha256": hashlib.sha256(b"a").hexdigest()},
                    {"path": "output/b.json", "sha256": hashlib.sha256(b"b").hexdigest()},
                ],
            )


class TestResolveKitRoot(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, _clean_env(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_explicit_arg(self) -> None:
        self.assertEqual(common.resolve_kit_root(str(KIT_ROOT), Path("/")), KIT_ROOT)

    def test_explicit_arg_without_fixtures(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(common.UsageError, "no fixtures/ directory"),
        ):
            common.resolve_kit_root(tmp, KIT_ROOT)

    def test_env_with_fixtures(self) -> None:
        with mock.patch.dict(os.environ, {"PAYOPS_KIT_ROOT": str(KIT_ROOT)}):
            self.assertEqual(common.resolve_kit_root(None, Path("/")), KIT_ROOT)

    def test_invalid_env_falls_back_to_walking_up(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"PAYOPS_KIT_ROOT": tmp}),
        ):
            repo_root = KIT_ROOT / "repos" / "settlement-reporting"
            self.assertEqual(common.resolve_kit_root(None, repo_root), KIT_ROOT)

    def test_not_found(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(common.UsageError, "could not locate kit root"),
        ):
            common.resolve_kit_root(None, Path(tmp).resolve())


class TestScenarioAndResolvers(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, _clean_env(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_scenario(self) -> None:
        cfg = common.load_scenario(KIT_ROOT, "reconciliation-breaks")
        self.assertEqual(cfg["id"], "reconciliation-breaks")

    def test_load_missing_scenario_is_usage(self) -> None:
        with self.assertRaisesRegex(common.UsageError, "scenario not found: ghost"):
            common.load_scenario(KIT_ROOT, "ghost")

    def test_resolve_now_precedence(self) -> None:
        cfg = {"clock": {"fixedUtc": "2026-03-16T09:30:00Z"}}
        self.assertEqual(common.resolve_now(None, cfg), "2026-03-16T09:30:00Z")
        with mock.patch.dict(os.environ, {"PAYOPS_FIXED_CLOCK": "2026-04-01T00:00:00Z"}):
            self.assertEqual(common.resolve_now(None, cfg), "2026-04-01T00:00:00Z")
            self.assertEqual(
                common.resolve_now("2026-05-01T00:00:00Z", cfg), "2026-05-01T00:00:00Z"
            )

    def test_resolve_attempt_precedence(self) -> None:
        self.assertEqual(common.resolve_attempt(None), 1)
        with mock.patch.dict(os.environ, {"PAYOPS_ATTEMPT": "4"}):
            self.assertEqual(common.resolve_attempt(None), 4)
            self.assertEqual(common.resolve_attempt("2"), 2)

    def test_resolve_run_id(self) -> None:
        self.assertEqual(common.resolve_run_id("abc", Path("/tmp/run-1")), "abc")
        self.assertEqual(common.resolve_run_id(None, Path("/tmp/run-1")), "run-1")


class TestOutput(unittest.TestCase):
    def test_emit_result_writes_one_json_document(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            common.emit_result({"b": 2, "a": 1})
        self.assertEqual(json.loads(out.getvalue()), {"a": 1, "b": 2})
        self.assertTrue(out.getvalue().endswith("}\n"))

    def test_log_goes_to_stderr_with_prefix(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            common.log("hello")
        self.assertEqual(err.getvalue(), "settlement>> hello\n")

    def test_business_rule_error_carries_code(self) -> None:
        exc = common.BusinessRuleError("SOME_RULE", "broken")
        self.assertEqual(exc.code, "SOME_RULE")
        self.assertEqual(str(exc), "broken")


if __name__ == "__main__":
    unittest.main()
