"""CLI contract tests for exception-management: exit codes and one JSON object on stdout."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exception_management import cli, common

KIT_ROOT = Path(__file__).resolve().parents[3]
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("PAYOPS_")}


@contextlib.contextmanager
def run_dir_with_breaks(breaks: list[dict[str, Any]] | None) -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        if breaks is not None:
            common.write_jsonl(rd / "stages" / "breaks.jsonl", breaks)
        yield rd


def task_argv(run_dir: Path | str, *extra: str) -> list[str]:
    return [
        "--run-dir",
        str(run_dir),
        "--scenario",
        "reconciliation-breaks",
        "--task",
        "classify_breaks",
        "--kit-root",
        str(KIT_ROOT),
        *extra,
    ]


class CliTestCase(unittest.TestCase):
    def run_main(
        self, argv: list[str], env: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, env if env is not None else CLEAN_ENV, clear=True),
            contextlib.redirect_stdout(out),
        ):
            code = cli.main(argv)
        doc = json.loads(out.getvalue())
        self.assertIsInstance(doc, dict)
        self.assertTrue(out.getvalue().endswith("}\n"))
        return code, dict(doc)

    def assert_failure(self, code: int, doc: dict[str, Any], exit_code: int, err: str) -> None:
        self.assertEqual(code, exit_code)
        self.assertEqual(doc["exitCode"], exit_code)
        self.assertEqual(doc["status"], "failed")
        self.assertEqual(doc["error"]["code"], err)
        self.assertEqual(doc["repository"], "exception-management")
        self.assertEqual(doc["counts"], {})
        self.assertEqual(doc["artifacts"], [])
        self.assertEqual(doc["metrics"], {})


class TestCliSuccess(CliTestCase):
    def test_classify_breaks_success_envelope(self) -> None:
        breaks = [{"payment_id": "PAY-2026-0044", "reason_code": "MISSING_LEDGER_REFERENCE"}]
        with run_dir_with_breaks(breaks) as rd, self.assertLogs("exception-mgmt", "INFO"):
            code, doc = self.run_main(task_argv(rd, "--run-id", "rid-1", "--attempt", "2"))
            self.assertTrue((rd / "stages" / "classified-breaks.jsonl").is_file())
        self.assertEqual(code, 0)
        self.assertEqual(doc["exitCode"], 0)
        self.assertEqual(doc["status"], "success")
        self.assertEqual(doc["task"], "classify_breaks")
        self.assertEqual(doc["runId"], "rid-1")
        self.assertEqual(doc["attempt"], 2)
        self.assertEqual(doc["scenario"], "reconciliation-breaks")
        self.assertEqual(doc["counts"], {"broken": 1})
        self.assertEqual(doc["metrics"]["bySeverity"], {"high": 1})
        self.assertEqual(doc["startedAt"], doc["completedAt"])
        self.assertNotIn("error", doc)

    def test_defaults_come_from_environment_and_run_dir(self) -> None:
        env = dict(CLEAN_ENV, PAYOPS_FIXED_CLOCK="2026-07-01T00:00:00Z", PAYOPS_ATTEMPT="3")
        with run_dir_with_breaks([]) as rd, self.assertLogs("exception-mgmt", "INFO"):
            code, doc = self.run_main(task_argv(rd), env)
        self.assertEqual(code, 0)
        self.assertEqual(doc["runId"], rd.resolve().name)
        self.assertEqual(doc["attempt"], 3)
        self.assertEqual(doc["startedAt"], "2026-07-01T00:00:00Z")
        self.assertEqual(doc["counts"], {"broken": 0})

    def test_scenario_clock_used_without_overrides(self) -> None:
        clock = common.load_scenario(KIT_ROOT, "reconciliation-breaks")["clock"]["fixedUtc"]
        with run_dir_with_breaks([]) as rd, self.assertLogs("exception-mgmt", "INFO"):
            _, doc = self.run_main(task_argv(rd, "--now", ""))
        self.assertEqual(doc["startedAt"], clock)

    def test_help_writes_usage_to_stderr_only(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(err.getvalue().startswith("Usage: ./scripts/run-task.sh --run-dir"))


class TestCliUsageErrors(CliTestCase):
    def test_missing_required_arguments(self) -> None:
        with self.assertNoLogs("exception-mgmt"):
            code, doc = self.run_main(["--task", "classify_breaks"])
        self.assert_failure(code, doc, 2, "USAGE")
        self.assertEqual(doc["task"], "classify_breaks")
        self.assertEqual(doc["scenario"], "")
        self.assertEqual(doc["runId"], "")
        self.assertEqual(doc["attempt"], 1)
        self.assertIn("missing required argument", doc["error"]["message"])

    def test_run_dir_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            code, doc = self.run_main(task_argv(Path(d) / "missing"))
        self.assert_failure(code, doc, 2, "USAGE")
        self.assertIn("run-dir is not a directory", doc["error"]["message"])

    def test_task_must_be_owned(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            argv = ["--run-dir", d, "--scenario", "s", "--task", "watch_inbound_files"]
            code, doc = self.run_main(argv)
        self.assert_failure(code, doc, 2, "USAGE")
        self.assertEqual(
            doc["error"]["message"], "task not owned by exception-management: watch_inbound_files"
        )

    def test_kit_root_without_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            argv = ["--run-dir", d, "--scenario", "s", "--task", "classify_breaks"]
            code, doc = self.run_main([*argv, "--kit-root", d])
        self.assert_failure(code, doc, 2, "USAGE")
        self.assertIn("kit root has no fixtures/", doc["error"]["message"])

    def test_unknown_scenario_is_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            argv = ["--run-dir", d, "--scenario", "nope", "--task", "classify_breaks"]
            code, doc = self.run_main([*argv, "--kit-root", str(KIT_ROOT)])
        self.assert_failure(code, doc, 2, "USAGE")
        self.assertEqual(doc["error"]["message"], "scenario not found: nope")
        self.assertEqual(doc["startedAt"], "")

    def test_owned_task_without_dispatcher_is_usage_error(self) -> None:
        with (
            run_dir_with_breaks([]) as rd,
            mock.patch.object(common, "OWNED_TASKS", {"classify_breaks", "future_task"}),
        ):
            argv = ["--run-dir", str(rd), "--scenario", "reconciliation-breaks"]
            code, doc = self.run_main([*argv, "--task", "future_task"])
        self.assert_failure(code, doc, 2, "USAGE")
        self.assertEqual(doc["error"]["message"], "task not owned: future_task")


class TestCliTaskFailures(CliTestCase):
    def test_missing_breaks_is_missing_upstream(self) -> None:
        with run_dir_with_breaks(None) as rd, self.assertLogs("exception-mgmt", "INFO"):
            code, doc = self.run_main(task_argv(rd, "--run-id", "rid-5"))
        self.assert_failure(code, doc, 5, "MISSING_UPSTREAM_ARTIFACT")
        self.assertIn("breaks.jsonl", doc["error"]["message"])
        self.assertEqual(doc["runId"], "rid-5")
        self.assertNotEqual(doc["startedAt"], "")

    def test_unknown_reason_is_business_rule(self) -> None:
        breaks = [{"payment_id": "P1", "reason_code": "MYSTERY"}]
        with run_dir_with_breaks(breaks) as rd, self.assertLogs("exception-mgmt", "INFO"):
            code, doc = self.run_main(task_argv(rd))
            self.assertFalse((rd / "stages" / "exceptions.json").exists())
        self.assert_failure(code, doc, 4, "UNKNOWN_BREAK_REASON")

    def test_unexpected_exception_is_internal_without_detail(self) -> None:
        with (
            run_dir_with_breaks([]) as rd,
            mock.patch(
                "exception_management.classify.run", side_effect=RuntimeError("secret detail")
            ),
        ):
            code, doc = self.run_main(task_argv(rd))
        self.assert_failure(code, doc, 1, "INTERNAL")
        self.assertEqual(doc["error"]["message"], "unexpected error")


class TestRepoRoot(unittest.TestCase):
    def test_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env = dict(CLEAN_ENV, PAYOPS_REPO_ROOT=d)
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(cli._repo_root(), Path(d).resolve())

    def test_default_is_repository_directory(self) -> None:
        with mock.patch.dict(os.environ, CLEAN_ENV, clear=True):
            self.assertEqual(cli._repo_root(), KIT_ROOT / "repos" / "exception-management")


if __name__ == "__main__":
    unittest.main()
