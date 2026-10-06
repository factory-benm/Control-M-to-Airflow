"""CLI contract for ledger-reconciliation: exit codes and one JSON object on stdout."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from ledger_test_support import KIT_ROOT, REPO_ROOT, make_context, run_quietly, seed_run_dir

from ledger_reconciliation import cli, common

ENVELOPE_KEYS = {
    "repository",
    "task",
    "runId",
    "scenario",
    "attempt",
    "startedAt",
    "completedAt",
    "status",
    "exitCode",
    "counts",
    "artifacts",
    "metrics",
}


class _CliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name).resolve() / "cli-run"
        self.run_dir.mkdir()

    def invoke(self, *argv: str) -> tuple[int, dict[str, Any]]:
        code, stdout = run_quietly(cli.main, list(argv))
        # json.loads rejects trailing data, so this also proves a single object.
        document = json.loads(stdout)
        self.assertIsInstance(document, dict)
        self.assertEqual(document["exitCode"], code)
        self.assertLessEqual(ENVELOPE_KEYS, set(document))
        return code, document

    def task_args(self, task: str, scenario: str = "happy-path", *extra: str) -> list[str]:
        return [
            "--run-dir",
            str(self.run_dir),
            "--scenario",
            scenario,
            "--task",
            task,
            "--kit-root",
            str(KIT_ROOT),
            *extra,
        ]

    def assert_failure(self, document: dict[str, Any], code: str, message: str) -> None:
        self.assertEqual(document["status"], "failed")
        self.assertEqual(document["error"]["code"], code)
        self.assertIn(message, document["error"]["message"])
        self.assertEqual(document["counts"], {})
        self.assertEqual(document["artifacts"], [])


class TestUsage(_CliTest):
    def test_help_prints_usage_to_stderr_only(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Usage: ./scripts/run-task.sh", err.getvalue())

    def test_missing_arguments(self) -> None:
        code, document = self.invoke("--task", "post_pending_ledger")
        self.assertEqual(code, 2)
        self.assert_failure(document, "USAGE", "missing required argument(s)")
        self.assertEqual(document["task"], "post_pending_ledger")
        self.assertEqual(document["scenario"], "")
        self.assertEqual(document["repository"], "ledger-reconciliation")

    def test_run_dir_must_exist(self) -> None:
        missing = self.run_dir / "absent"
        code, document = self.invoke(
            "--run-dir", str(missing), "--scenario", "happy-path", "--task", "post_pending_ledger"
        )
        self.assertEqual(code, 2)
        self.assert_failure(document, "USAGE", f"run-dir is not a directory: {missing}")

    def test_task_must_be_owned(self) -> None:
        code, document = self.invoke(*self.task_args("classify_breaks"))
        self.assertEqual(code, 2)
        self.assert_failure(document, "USAGE", "task not owned by ledger-reconciliation")

    def test_bad_kit_root(self) -> None:
        args = self.task_args("post_pending_ledger")
        args[args.index("--kit-root") + 1] = str(self.run_dir)
        code, document = self.invoke(*args)
        self.assertEqual(code, 2)
        self.assert_failure(document, "USAGE", "kit root has no fixtures/ directory")

    def test_unknown_scenario(self) -> None:
        code, document = self.invoke(*self.task_args("post_pending_ledger", "nope"))
        self.assertEqual(code, 2)
        self.assert_failure(document, "USAGE", "scenario not found: nope")
        self.assertEqual(document["runId"], "")

    def test_owned_task_without_a_runner(self) -> None:
        owned = common.OWNED_TASKS | {"phantom_task"}
        with mock.patch.object(common, "OWNED_TASKS", owned):
            code, document = self.invoke(*self.task_args("phantom_task"))
        self.assertEqual(code, 2)
        self.assert_failure(document, "USAGE", "task not owned: phantom_task")
        self.assertIsNone(cli._run_task("phantom_task", make_context(self.run_dir, "happy-path")))


class TestTaskOutcomes(_CliTest):
    def test_full_chain_succeeds_and_stamps_the_envelope(self) -> None:
        seed_run_dir(self.run_dir, "happy-path")
        for task in ("apply_business_day_cutoff", "post_pending_ledger", "reconcile_nostro_ledger"):
            with self.subTest(task=task):
                code, document = self.invoke(
                    *self.task_args(task, "happy-path", "--now", "2026-03-16T09:30:00Z")
                )
                self.assertEqual(code, 0)
                self.assertEqual(document["status"], "success")
                self.assertNotIn("error", document)
                self.assertEqual(document["task"], task)
                self.assertEqual(document["runId"], "cli-run")
                self.assertEqual(document["startedAt"], "2026-03-16T09:30:00Z")
                self.assertEqual(document["completedAt"], "2026-03-16T09:30:00Z")
                self.assertTrue(document["artifacts"])
        self.assertEqual(document["counts"]["broken"], 0)

    def test_missing_upstream_artifact_exits_5(self) -> None:
        code, document = self.invoke(*self.task_args("apply_business_day_cutoff"))
        self.assertEqual(code, 5)
        self.assert_failure(document, "MISSING_UPSTREAM_ARTIFACT", "enriched-payments.jsonl")

    def test_injected_fault_exits_3_and_retry_recovers(self) -> None:
        seed_run_dir(self.run_dir, "partial-ledger-write")
        self.invoke(*self.task_args("apply_business_day_cutoff", "partial-ledger-write"))

        code, document = self.invoke(
            *self.task_args("post_pending_ledger", "partial-ledger-write", "--attempt", "1")
        )
        self.assertEqual(code, 3)
        self.assertEqual(document["status"], "failed")
        self.assertEqual(document["error"]["code"], "INJECTED_LEDGER_FAULT")
        self.assertEqual(document["counts"], {"posted": 4})
        self.assertEqual(document["attempt"], 1)

        code, document = self.invoke(
            *self.task_args("post_pending_ledger", "partial-ledger-write", "--attempt", "2")
        )
        self.assertEqual(code, 0)
        self.assertEqual(document["attempt"], 2)
        self.assertEqual(document["metrics"], {"inserted": 3, "alreadyPosted": 4})

    def test_business_rule_violation_exits_4(self) -> None:
        error = common.BusinessRuleError("LEDGER_RULE", "rule violated")
        with mock.patch("ledger_reconciliation.ledger.run", side_effect=error):
            code, document = self.invoke(*self.task_args("post_pending_ledger"))
        self.assertEqual(code, 4)
        self.assert_failure(document, "LEDGER_RULE", "rule violated")

    def test_unexpected_error_exits_1_without_leaking_details(self) -> None:
        records = seed_run_dir(self.run_dir, "happy-path")
        del records[0]["booking_timestamp"]
        common.write_jsonl(self.run_dir / "stages" / "enriched-payments.jsonl", records)

        code, document = self.invoke(*self.task_args("apply_business_day_cutoff"))
        self.assertEqual(code, 1)
        self.assertEqual(document["error"], {"code": "INTERNAL", "message": "unexpected error"})


class TestRepoRoot(unittest.TestCase):
    def test_environment_override(self) -> None:
        with mock.patch.dict(os.environ, {"PAYOPS_REPO_ROOT": "/tmp/somewhere/../repo"}):
            self.assertEqual(cli._repo_root(), Path("/tmp/repo").resolve())

    def test_defaults_to_the_repository_holding_the_package(self) -> None:
        with mock.patch.dict(os.environ, {"PAYOPS_REPO_ROOT": ""}):
            self.assertEqual(cli._repo_root(), REPO_ROOT)


if __name__ == "__main__":
    unittest.main()
