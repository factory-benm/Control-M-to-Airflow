"""Tests for the payment-notifications CLI contract (CONTRACT sections 3.1 and 3.2)."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from notify_support import KIT_ROOT, payops_env, seed_report

from payment_notifications import cli, common

TASK = "archive_and_notify"


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name).resolve() / "baseline-happy-path"
        self.run_dir.mkdir()

    def invoke(self, argv: list[str], **env: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with payops_env(**env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def invoke_json(self, argv: list[str], **env: str) -> tuple[int, dict[str, Any], str]:
        code, out, err = self.invoke(argv, **env)
        doc, end = json.JSONDecoder().raw_decode(out)
        self.assertEqual(out[end:], "\n", f"stdout must hold exactly one JSON object: {out!r}")
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

    def assert_failure(self, doc: dict[str, Any], exit_code: int, error_code: str) -> None:
        self.assertEqual(doc["status"], "failed")
        self.assertEqual(doc["exitCode"], exit_code)
        self.assertEqual(doc["error"]["code"], error_code)
        self.assertEqual(doc["repository"], "payment-notifications")
        self.assertEqual(doc["counts"], {})
        self.assertEqual(doc["artifacts"], [])
        self.assertEqual(doc["metrics"], {})


class TestCliSuccess(CliCase):
    def test_success_emits_merged_envelope(self) -> None:
        seed_report(self.run_dir, {"broken": 0, "matched": 6})
        code, doc, err = self.invoke_json(self.base_argv())
        self.assertEqual(code, 0)
        self.assertEqual(doc["status"], "success")
        self.assertEqual(doc["exitCode"], 0)
        self.assertEqual(doc["task"], TASK)
        self.assertEqual(doc["repository"], "payment-notifications")
        self.assertEqual(doc["runId"], "baseline-happy-path")
        self.assertEqual(doc["scenario"], "happy-path")
        self.assertEqual(doc["attempt"], 1)
        self.assertEqual(doc["startedAt"], "2026-03-16T09:30:00Z")
        self.assertEqual(doc["completedAt"], "2026-03-16T09:30:00Z")
        self.assertEqual(doc["counts"], {"broken": 0, "matched": 6})
        self.assertEqual(doc["metrics"], {"notificationStatus": "completed"})
        self.assertEqual(
            [a["path"] for a in doc["artifacts"]],
            ["output/notification.json", "stages/notify.json"],
        )
        self.assertNotIn("error", doc)
        self.assertIn("notify :: notification written status=completed", err)

    def test_overrides_from_arguments(self) -> None:
        seed_report(self.run_dir, {"broken": 3})
        argv = [
            *self.base_argv(),
            "--run-id",
            "rid-9",
            "--now",
            "2026-03-16T12:00:00Z",
            "--attempt",
            "3",
        ]
        code, doc, _ = self.invoke_json(
            argv, PAYOPS_FIXED_CLOCK="2026-01-01T00:00:00Z", PAYOPS_ATTEMPT="2"
        )
        self.assertEqual(code, 0)
        self.assertEqual(doc["runId"], "rid-9")
        self.assertEqual(doc["attempt"], 3)
        self.assertEqual(doc["startedAt"], "2026-03-16T12:00:00Z")
        notif = json.loads(
            (self.run_dir / "output" / "notification.json").read_text(encoding="utf-8")
        )
        self.assertEqual(notif["runId"], "rid-9")
        self.assertEqual(notif["status"], "completed_with_breaks")

    def test_clock_attempt_and_kit_root_from_environment(self) -> None:
        seed_report(self.run_dir, {"broken": 0})
        argv = ["--run-dir", str(self.run_dir), "--scenario", "happy-path", "--task", TASK]
        code, doc, _ = self.invoke_json(
            argv,
            PAYOPS_KIT_ROOT=str(KIT_ROOT),
            PAYOPS_FIXED_CLOCK="2026-03-16T23:00:00Z",
            PAYOPS_ATTEMPT="2",
        )
        self.assertEqual(code, 0)
        self.assertEqual(doc["attempt"], 2)
        self.assertEqual(doc["completedAt"], "2026-03-16T23:00:00Z")

    def test_repo_root_override_drives_kit_search(self) -> None:
        seed_report(self.run_dir, {"broken": 0})
        argv = ["--run-dir", str(self.run_dir), "--scenario", "happy-path", "--task", TASK]
        nested = KIT_ROOT / "repos" / "payment-notifications" / "src"
        code, doc, _ = self.invoke_json(argv, PAYOPS_REPO_ROOT=str(nested))
        self.assertEqual(code, 0)
        self.assertEqual(doc["status"], "success")

    def test_help_writes_usage_to_stderr_only(self) -> None:
        code, out, err = self.invoke(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, cli.USAGE)


class TestCliFailures(CliCase):
    def test_missing_required_arguments_is_usage_error(self) -> None:
        code, doc, _ = self.invoke_json(["--task", TASK])
        self.assertEqual(code, 2)
        self.assert_failure(doc, 2, "USAGE")
        self.assertEqual(doc["task"], TASK)
        self.assertEqual(doc["scenario"], "")
        self.assertEqual(doc["runId"], "")
        self.assertEqual(doc["startedAt"], "")
        self.assertEqual(doc["attempt"], 1)
        self.assertEqual(
            doc["error"]["message"], "missing required argument(s): --run-dir, --scenario, --task"
        )

    def test_run_dir_must_exist(self) -> None:
        argv = self.base_argv()
        argv[1] = str(self.run_dir / "absent")
        code, doc, _ = self.invoke_json(argv)
        self.assertEqual(code, 2)
        self.assert_failure(doc, 2, "USAGE")
        self.assertTrue(doc["error"]["message"].startswith("run-dir is not a directory: "))

    def test_task_not_owned_is_usage_error(self) -> None:
        argv = self.base_argv()
        argv[5] = "classify_breaks"
        code, doc, _ = self.invoke_json(argv)
        self.assertEqual(code, 2)
        self.assert_failure(doc, 2, "USAGE")
        self.assertEqual(
            doc["error"]["message"], "task not owned by payment-notifications: classify_breaks"
        )

    def test_owned_task_without_implementation_is_usage_error(self) -> None:
        argv = self.base_argv()
        argv[5] = "future_task"
        with mock.patch.object(common, "OWNED_TASKS", {TASK, "future_task"}):
            code, doc, _ = self.invoke_json(argv)
        self.assertEqual(code, 2)
        self.assert_failure(doc, 2, "USAGE")
        self.assertEqual(doc["error"]["message"], "task not owned: future_task")
        self.assertEqual(doc["runId"], "baseline-happy-path")

    def test_kit_root_without_fixtures_is_usage_error(self) -> None:
        argv = self.base_argv()
        argv[-1] = str(self.run_dir)
        code, doc, _ = self.invoke_json(argv)
        self.assertEqual(code, 2)
        self.assert_failure(doc, 2, "USAGE")
        self.assertIn("kit root has no fixtures/ directory", doc["error"]["message"])

    def test_unknown_scenario_is_usage_error(self) -> None:
        code, doc, _ = self.invoke_json(self.base_argv(scenario="ghost"))
        self.assertEqual(code, 2)
        self.assert_failure(doc, 2, "USAGE")
        self.assertEqual(doc["error"]["message"], "scenario not found: ghost")
        self.assertEqual(doc["runId"], "")

    def test_missing_report_is_missing_upstream(self) -> None:
        code, doc, _ = self.invoke_json(self.base_argv())
        self.assertEqual(code, 5)
        self.assert_failure(doc, 5, "MISSING_UPSTREAM_ARTIFACT")
        self.assertEqual(doc["runId"], "baseline-happy-path")
        self.assertEqual(doc["startedAt"], "2026-03-16T09:30:00Z")
        self.assertFalse((self.run_dir / "output" / "notification.json").exists())

    def test_business_rule_error_uses_its_code(self) -> None:
        error = common.BusinessRuleError("NOTIFY_RULE", "rule violated")
        with mock.patch("payment_notifications.notify.run", side_effect=error):
            code, doc, _ = self.invoke_json(self.base_argv())
        self.assertEqual(code, 4)
        self.assert_failure(doc, 4, "NOTIFY_RULE")
        self.assertEqual(doc["error"]["message"], "rule violated")

    def test_malformed_report_is_internal_error_without_details(self) -> None:
        (self.run_dir / "output").mkdir()
        (self.run_dir / "output" / "settlement-report.json").write_text("{", encoding="utf-8")
        code, doc, _ = self.invoke_json(self.base_argv())
        self.assertEqual(code, 1)
        self.assert_failure(doc, 1, "INTERNAL")
        self.assertEqual(doc["error"]["message"], "unexpected error")


if __name__ == "__main__":
    unittest.main()
