"""CLI contract tests for payment-ingestion: exit codes and one JSON object on stdout."""

import contextlib
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

from payment_ingestion import cli

KIT_ROOT = Path(__file__).resolve().parents[3]
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("PAYOPS_")}


def run_main(argv: list[str] | None, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with (
        mock.patch.dict(os.environ, env if env is not None else CLEAN_ENV, clear=True),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def single_json(test: unittest.TestCase, stdout: str) -> dict[str, Any]:
    test.assertTrue(stdout.endswith("\n"))
    test.assertEqual(stdout.count("\n"), 1, "stdout must hold exactly one JSON line")
    doc = json.loads(stdout)
    test.assertIsInstance(doc, dict)
    return dict(doc)


class TestCliSuccess(unittest.TestCase):
    def test_watch_success_emits_one_result(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            argv = [
                "--run-dir",
                d,
                "--scenario",
                "happy-path",
                "--task",
                "watch_inbound_files",
                "--kit-root",
                str(KIT_ROOT),
                "--run-id",
                "rid-7",
            ]
            code, out, err = run_main(argv)
        self.assertEqual(code, 0)
        doc = single_json(self, out)
        self.assertEqual(doc["task"], "watch_inbound_files")
        self.assertEqual(doc["repository"], "payment-ingestion")
        self.assertEqual(doc["runId"], "rid-7")
        self.assertEqual(doc["status"], "success")
        self.assertEqual(doc["attempt"], 1)
        self.assertEqual(doc["startedAt"], "2026-03-16T09:30:00Z")
        self.assertEqual(doc["completedAt"], "2026-03-16T09:30:00Z")
        self.assertIn("detected", err)

    def test_defaults_from_environment_and_run_dir_name(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env = dict(CLEAN_ENV)
            env.update(
                {
                    "PAYOPS_KIT_ROOT": str(KIT_ROOT),
                    "PAYOPS_FIXED_CLOCK": "2026-04-01T00:00:00Z",
                    "PAYOPS_ATTEMPT": "3",
                }
            )
            argv = ["--run-dir", d, "--scenario", "happy-path", "--task", "verify_file_integrity"]
            code, out, _ = run_main(argv, env)
        self.assertEqual(code, 0)
        doc = single_json(self, out)
        self.assertEqual(doc["runId"], Path(d).resolve().name)
        self.assertEqual(doc["attempt"], 3)
        self.assertEqual(doc["startedAt"], "2026-04-01T00:00:00Z")

    def test_help_prints_usage_and_exits_zero(self) -> None:
        code, out, _ = run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("usage: run-task.sh"))
        self.assertIn("extract_payment_batch", out)

    def test_main_reads_sys_argv_when_argv_is_none(self) -> None:
        with mock.patch.object(sys, "argv", ["cli", "-h"]):
            code, out, _ = run_main(None)
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)


class TestCliErrors(unittest.TestCase):
    def assert_error(
        self, argv: list[str], exit_code: int, error_code: str, stderr_prefix: str
    ) -> dict[str, Any]:
        code, out, err = run_main(argv)
        self.assertEqual(code, exit_code)
        doc = single_json(self, out)
        self.assertEqual(doc["status"], "failed")
        self.assertEqual(doc["exitCode"], exit_code)
        self.assertEqual(doc["error"]["code"], error_code)
        self.assertEqual(doc["repository"], "payment-ingestion")
        self.assertEqual(doc["counts"], {})
        self.assertEqual(doc["artifacts"], [])
        self.assertTrue(err.startswith(stderr_prefix), err)
        return doc

    def test_missing_required_arguments(self) -> None:
        doc = self.assert_error(["--task", "watch_inbound_files"], 2, "USAGE", "usage error:")
        self.assertEqual(doc["task"], "watch_inbound_files")
        self.assertIsNone(doc["scenario"])
        self.assertIsNone(doc["startedAt"])

    def test_unknown_argument(self) -> None:
        doc = self.assert_error(["--bogus", "x"], 2, "USAGE", "usage error:")
        self.assertEqual(doc["error"]["message"], "unknown argument: --bogus")

    def test_unknown_task(self) -> None:
        argv = ["--run-dir", "/tmp/x", "--scenario", "s", "--task", "classify_breaks"]
        doc = self.assert_error(argv, 2, "USAGE", "usage error:")
        self.assertIn("unknown task", doc["error"]["message"])
        self.assertEqual(doc["scenario"], "s")

    def test_missing_upstream_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            argv = [
                "--run-dir",
                d,
                "--scenario",
                "no-such-scenario",
                "--task",
                "watch_inbound_files",
                "--kit-root",
                str(KIT_ROOT),
                "--run-id",
                "rid",
            ]
            doc = self.assert_error(argv, 5, "MISSING_UPSTREAM", "missing upstream:")
        self.assertEqual(doc["runId"], "rid")

    def test_business_rule_bad_header(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rd = Path(d)
            (rd / "stages").mkdir()
            (rd / "input").mkdir()
            (rd / "input" / "payments.csv").write_text("wrong,header\n", encoding="utf-8")
            (rd / "stages" / "file-integrity.json").write_text(
                json.dumps({"files": [{"path": "input/payments.csv", "sha256": "x"}]}),
                encoding="utf-8",
            )
            argv = [
                "--run-dir",
                d,
                "--scenario",
                "happy-path",
                "--task",
                "extract_payment_batch",
                "--kit-root",
                str(KIT_ROOT),
            ]
            doc = self.assert_error(argv, 4, "BUSINESS_RULE", "business rule:")
        self.assertEqual(doc["startedAt"], "2026-03-16T09:30:00Z")

    def test_unexpected_exception_is_internal(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            argv = [
                "--run-dir",
                d,
                "--scenario",
                "happy-path",
                "--task",
                "watch_inbound_files",
                "--kit-root",
                str(KIT_ROOT),
            ]
            with mock.patch(
                "payment_ingestion.tasks.watch_inbound_files", side_effect=RuntimeError("boom")
            ):
                doc = self.assert_error(argv, 1, "INTERNAL", "internal error: boom")
        self.assertEqual(doc["error"]["message"], "boom")


if __name__ == "__main__":
    unittest.main()
