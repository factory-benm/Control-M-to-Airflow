"""Unit tests for payment-notifications shared helpers (standard library only)."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from notify_support import KIT_ROOT, REPO_ROOT, payops_env

from payment_notifications import common


class FileCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()


class TestJsonAndHashHelpers(FileCase):
    def test_sha256_file_matches_hashlib(self) -> None:
        path = self.tmp / "blob.bin"
        payload = b"notify" * 20000
        path.write_bytes(payload)
        self.assertEqual(common.sha256_file(path), hashlib.sha256(payload).hexdigest())

    def test_write_json_creates_parents_sorted_and_indented(self) -> None:
        path = self.tmp / "nested" / "dir" / "out.json"
        common.write_json(path, {"b": [1, 2], "a": "x"})
        self.assertEqual(
            path.read_text(encoding="utf-8"), '{\n  "a": "x",\n  "b": [\n    1,\n    2\n  ]\n}\n'
        )
        self.assertEqual(common.read_json(path), {"a": "x", "b": [1, 2]})

    def test_read_json_optional_returns_none_when_absent(self) -> None:
        self.assertIsNone(common.read_json_optional(self.tmp / "absent.json"))

    def test_read_json_optional_returns_document_when_present(self) -> None:
        path = self.tmp / "doc.json"
        path.write_text('{"counts": {"broken": 1}}', encoding="utf-8")
        self.assertEqual(common.read_json_optional(path), {"counts": {"broken": 1}})

    def test_read_json_optional_raises_on_malformed_file(self) -> None:
        path = self.tmp / "doc.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            common.read_json_optional(path)

    def test_artifacts_for_skips_missing_and_sorts_by_path(self) -> None:
        (self.tmp / "stages").mkdir()
        (self.tmp / "output").mkdir()
        (self.tmp / "stages" / "notify.json").write_bytes(b"s")
        (self.tmp / "output" / "notification.json").write_bytes(b"o")
        items = common.artifacts_for(
            self.tmp, ["stages/notify.json", "output/missing.json", "output/notification.json"]
        )
        self.assertEqual(
            items,
            [
                {"path": "output/notification.json", "sha256": hashlib.sha256(b"o").hexdigest()},
                {"path": "stages/notify.json", "sha256": hashlib.sha256(b"s").hexdigest()},
            ],
        )

    def test_emit_result_writes_one_indented_sorted_object(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            common.emit_result({"status": "success", "exitCode": 0})
        self.assertEqual(out.getvalue(), '{\n  "exitCode": 0,\n  "status": "success"\n}\n')

    def test_log_uses_notify_prefix_on_stderr(self) -> None:
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            common.log("hello")
        self.assertEqual(err.getvalue(), "notify :: hello\n")
        self.assertEqual(out.getvalue(), "")

    def test_business_rule_error_carries_code(self) -> None:
        exc = common.BusinessRuleError("SOME_RULE", "rule broken")
        self.assertEqual(exc.code, "SOME_RULE")
        self.assertEqual(str(exc), "rule broken")


class TestResolveKitRoot(FileCase):
    def test_explicit_argument(self) -> None:
        with payops_env():
            self.assertEqual(common.resolve_kit_root(str(KIT_ROOT), self.tmp), KIT_ROOT)

    def test_explicit_argument_without_fixtures_is_usage_error(self) -> None:
        with payops_env(), self.assertRaisesRegex(common.UsageError, "no fixtures/ directory"):
            common.resolve_kit_root(str(self.tmp), REPO_ROOT)

    def test_env_used_when_valid(self) -> None:
        (self.tmp / "fixtures").mkdir()
        with payops_env(PAYOPS_KIT_ROOT=str(self.tmp)):
            self.assertEqual(common.resolve_kit_root(None, REPO_ROOT), self.tmp)

    def test_invalid_env_falls_back_to_repository_search(self) -> None:
        with payops_env(PAYOPS_KIT_ROOT=str(self.tmp)):
            self.assertEqual(common.resolve_kit_root(None, REPO_ROOT), KIT_ROOT)

    def test_search_without_fixtures_is_usage_error(self) -> None:
        start = self.tmp / "a" / "b"
        start.mkdir(parents=True)
        if any((p / "fixtures").is_dir() for p in [start, *start.parents]):
            self.skipTest("a parent of the temp directory contains fixtures/")
        with payops_env(), self.assertRaisesRegex(common.UsageError, "could not locate"):
            common.resolve_kit_root(None, start)


class TestScenarioAndRunSettings(FileCase):
    def test_load_scenario_reads_fixture(self) -> None:
        cfg = common.load_scenario(KIT_ROOT, "reconciliation-breaks")
        self.assertIn("fixedUtc", cfg["clock"])

    def test_load_scenario_unknown_is_usage_error(self) -> None:
        with self.assertRaisesRegex(common.UsageError, "scenario not found: ghost"):
            common.load_scenario(KIT_ROOT, "ghost")

    def test_resolve_now_precedence(self) -> None:
        cfg = {"clock": {"fixedUtc": "2026-03-16T09:30:00Z"}}
        with payops_env(PAYOPS_FIXED_CLOCK="2026-03-17T00:00:00Z"):
            self.assertEqual(
                common.resolve_now("2026-03-18T00:00:00Z", cfg), "2026-03-18T00:00:00Z"
            )
            self.assertEqual(common.resolve_now(None, cfg), "2026-03-17T00:00:00Z")
        with payops_env():
            self.assertEqual(common.resolve_now(None, cfg), "2026-03-16T09:30:00Z")

    def test_resolve_now_without_scenario_clock_raises(self) -> None:
        with payops_env(), self.assertRaises(KeyError):
            common.resolve_now(None, {})

    def test_resolve_attempt_precedence(self) -> None:
        with payops_env(PAYOPS_ATTEMPT="4"):
            self.assertEqual(common.resolve_attempt("2"), 2)
            self.assertEqual(common.resolve_attempt(None), 4)
        with payops_env(PAYOPS_ATTEMPT=""):
            self.assertEqual(common.resolve_attempt(None), 1)
        with payops_env():
            self.assertEqual(common.resolve_attempt(None), 1)

    def test_resolve_attempt_rejects_non_integer(self) -> None:
        with payops_env(), self.assertRaises(ValueError):
            common.resolve_attempt("second")

    def test_resolve_run_id_defaults_to_run_dir_name(self) -> None:
        run_dir = self.tmp / "baseline-happy-path"
        self.assertEqual(common.resolve_run_id(None, run_dir), "baseline-happy-path")
        self.assertEqual(common.resolve_run_id("", run_dir), "baseline-happy-path")
        self.assertEqual(common.resolve_run_id("rid-1", run_dir), "rid-1")


if __name__ == "__main__":
    unittest.main()
