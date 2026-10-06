"""Tests for the fx-enrichment CLI contract (CONTRACT sections 3.1 and 3.2)."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fx_support import KIT_ROOT, dedup_record, payops_env, write_dedup_stage

from fx_enrichment.cli import USAGE, main

TASK = "enrich_fx_rates"


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name).resolve() / "baseline-happy-path"
        self.run_dir.mkdir()

    def invoke(self, argv: list[str], **env: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with payops_env(**env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def invoke_json(self, argv: list[str], **env: str) -> tuple[int, dict[str, Any], str]:
        code, out, err = self.invoke(argv, **env)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, f"stdout must hold one JSON object: {out!r}")
        doc = json.loads(lines[0])
        self.assertIsInstance(doc, dict)
        return code, doc, err

    def base_argv(self, scenario: str = "happy-path") -> list[str]:
        return [
            "--run-dir",
            str(self.run_dir),
            "--scenario",
            scenario,
            "--task",
            TASK,
            "--kit-root",
            str(KIT_ROOT),
        ]

    def seed_dedup(self, currency: str = "SGD", value_date: str = "2026-03-16") -> None:
        write_dedup_stage(
            self.run_dir, [dedup_record("PAY-2026-0001", 2, value_date, currency, "12500.00")]
        )

    def assert_failure(self, doc: dict[str, Any], exit_code: int, error_code: str) -> None:
        self.assertEqual(doc["status"], "failed")
        self.assertEqual(doc["exitCode"], exit_code)
        self.assertEqual(doc["error"]["code"], error_code)
        self.assertEqual(doc["repository"], "fx-enrichment")
        self.assertEqual(doc["attempt"], 1)
        self.assertEqual(doc["counts"], {})
        self.assertEqual(doc["artifacts"], [])
        self.assertEqual(doc["metrics"], {})


class TestCliSuccess(CliCase):
    def test_success_emits_single_result_object(self) -> None:
        self.seed_dedup()
        code, doc, err = self.invoke_json(self.base_argv())
        self.assertEqual(code, 0)
        self.assertEqual(doc["status"], "success")
        self.assertEqual(doc["exitCode"], 0)
        self.assertEqual(doc["task"], TASK)
        self.assertEqual(doc["scenario"], "happy-path")
        self.assertEqual(doc["runId"], "baseline-happy-path")
        self.assertEqual(doc["attempt"], 1)
        self.assertEqual(doc["counts"], {"accepted": 1})
        self.assertEqual(doc["startedAt"], "2026-03-16T09:30:00Z")
        self.assertEqual(doc["completedAt"], "2026-03-16T09:30:00Z")
        self.assertNotIn("error", doc)
        self.assertIn("[fx-enrichment] enriched 1 rows, table 2026.03.1", err)
        self.assertTrue((self.run_dir / "stages" / "enriched-payments.jsonl").is_file())

    def test_explicit_run_id_and_now_override_defaults(self) -> None:
        self.seed_dedup()
        argv = [*self.base_argv(), "--run-id", "rid-7", "--now", "2026-03-16T12:00:00Z"]
        code, doc, _ = self.invoke_json(argv, PAYOPS_FIXED_CLOCK="2026-01-01T00:00:00Z")
        self.assertEqual(code, 0)
        self.assertEqual(doc["runId"], "rid-7")
        self.assertEqual(doc["startedAt"], "2026-03-16T12:00:00Z")
        fx_meta = json.loads((self.run_dir / "stages" / "fx.json").read_text(encoding="utf-8"))
        self.assertEqual(fx_meta["runId"], "rid-7")

    def test_clock_and_attempt_from_environment(self) -> None:
        self.seed_dedup()
        code, doc, _ = self.invoke_json(
            self.base_argv(), PAYOPS_FIXED_CLOCK="2026-03-16T23:00:00Z", PAYOPS_ATTEMPT="2"
        )
        self.assertEqual(code, 0)
        self.assertEqual(doc["completedAt"], "2026-03-16T23:00:00Z")
        self.assertEqual(doc["attempt"], 2)

    def test_kit_root_from_environment(self) -> None:
        self.seed_dedup()
        argv = ["--run-dir", str(self.run_dir), "--scenario", "happy-path", "--task", TASK]
        code, doc, _ = self.invoke_json(argv, PAYOPS_KIT_ROOT=str(KIT_ROOT))
        self.assertEqual(code, 0)
        self.assertEqual(doc["status"], "success")

    def test_kit_root_found_by_repository_search(self) -> None:
        self.seed_dedup()
        argv = ["--run-dir", str(self.run_dir), "--scenario", "happy-path", "--task", TASK]
        code, doc, _ = self.invoke_json(argv)
        self.assertEqual(code, 0)
        self.assertEqual(doc["counts"], {"accepted": 1})

    def test_help_prints_usage_and_exits_zero(self) -> None:
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                code, out, err = self.invoke([flag])
                self.assertEqual(code, 0)
                self.assertEqual(out, USAGE)
                self.assertEqual(err, "")


class TestCliFailures(CliCase):
    def test_missing_required_arguments_is_usage_error(self) -> None:
        code, doc, err = self.invoke_json(["--task", TASK, "--scenario", "happy-path"])
        self.assertEqual(code, 2)
        self.assert_failure(doc, 2, "USAGE")
        self.assertEqual(doc["task"], TASK)
        self.assertEqual(doc["scenario"], "happy-path")
        self.assertIsNone(doc["runId"])
        self.assertIsNone(doc["startedAt"])
        self.assertIsNone(doc["completedAt"])
        self.assertEqual(doc["error"]["message"], "--run-dir, --scenario, and --task are required")
        self.assertIn("[fx-enrichment] usage error: ", err)

    def test_unknown_task_is_usage_error(self) -> None:
        argv = ["--run-dir", str(self.run_dir), "--scenario", "happy-path", "--task", "nope"]
        code, doc, _ = self.invoke_json([*argv, "--run-id", "rid-1"])
        self.assertEqual(code, 2)
        self.assert_failure(doc, 2, "USAGE")
        self.assertEqual(doc["error"]["message"], "unknown task for fx-enrichment: nope")
        self.assertEqual(doc["task"], "nope")
        self.assertEqual(doc["runId"], "rid-1")

    def test_unknown_flag_and_missing_value_are_usage_errors(self) -> None:
        for argv in (["--bogus"], [*self.base_argv(), "--now"]):
            with self.subTest(argv=argv):
                code, doc, _ = self.invoke_json(argv)
                self.assertEqual(code, 2)
                self.assert_failure(doc, 2, "USAGE")

    def test_kit_root_without_fixtures_is_usage_error(self) -> None:
        argv = self.base_argv()
        argv[-1] = str(self.run_dir)
        code, doc, _ = self.invoke_json(argv)
        self.assertEqual(code, 2)
        self.assert_failure(doc, 2, "USAGE")
        self.assertEqual(doc["error"]["message"], "--kit-root does not contain fixtures/")

    def test_unknown_scenario_is_missing_upstream(self) -> None:
        code, doc, err = self.invoke_json(self.base_argv(scenario="no-such-scenario"))
        self.assertEqual(code, 5)
        self.assert_failure(doc, 5, "MISSING_UPSTREAM")
        self.assertIsNone(doc["startedAt"])
        self.assertIn("missing upstream: scenario not found: no-such-scenario", err)

    def test_missing_dedup_stage_is_missing_upstream_with_clock(self) -> None:
        code, doc, _ = self.invoke_json(self.base_argv())
        self.assertEqual(code, 5)
        self.assert_failure(doc, 5, "MISSING_UPSTREAM")
        self.assertEqual(doc["startedAt"], "2026-03-16T09:30:00Z")
        self.assertEqual(doc["completedAt"], "2026-03-16T09:30:00Z")
        self.assertIn("stages/deduplicated-payments.jsonl", doc["error"]["message"])

    def test_missing_rate_is_business_rule(self) -> None:
        self.seed_dedup(value_date="2026-12-31")
        code, doc, err = self.invoke_json(self.base_argv())
        self.assertEqual(code, 4)
        self.assert_failure(doc, 4, "BUSINESS_RULE")
        self.assertEqual(doc["error"]["message"], "missing FX rate for 2026-12-31 SGD")
        self.assertIn("[fx-enrichment] business rule: ", err)

    def test_malformed_upstream_is_internal_error(self) -> None:
        (self.run_dir / "stages").mkdir()
        (self.run_dir / "stages" / "deduplicated-payments.jsonl").write_text(
            "{broken\n", encoding="utf-8"
        )
        code, doc, err = self.invoke_json(self.base_argv())
        self.assertEqual(code, 1)
        self.assert_failure(doc, 1, "INTERNAL")
        self.assertEqual(doc["startedAt"], "2026-03-16T09:30:00Z")
        self.assertIn("[fx-enrichment] internal error: ", err)

    def test_non_numeric_attempt_env_is_internal_error_before_clock(self) -> None:
        self.seed_dedup()
        code, doc, _ = self.invoke_json(self.base_argv(), PAYOPS_ATTEMPT="first")
        self.assertEqual(code, 1)
        self.assert_failure(doc, 1, "INTERNAL")
        self.assertIsNone(doc["startedAt"])
        self.assertFalse((self.run_dir / "stages" / "fx.json").exists())


if __name__ == "__main__":
    unittest.main()
