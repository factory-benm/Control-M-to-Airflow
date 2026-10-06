"""Isolated unit checks for the notification payload shape (CONTRACT 6.8)."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from notify_support import KIT_ROOT, REPO_ROOT, seed_report

from payment_notifications.common import MissingUpstreamError, TaskContext
from payment_notifications.notify import run


class TestNotification(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name) / "run"
        self.run_dir.mkdir()

    def _ctx(self, counts: dict[str, Any] | None, with_archive: bool = True) -> TaskContext:
        if counts is not None:
            seed_report(self.run_dir, counts, with_archive=with_archive)
        return {
            "run_dir": self.run_dir,
            "kit_root": KIT_ROOT,
            "repo_root": REPO_ROOT,
            "scenario": "happy-path",
            "scenario_cfg": {},
            "run_id": "test",
            "now": "2026-03-16T09:30:00Z",
            "attempt": 1,
        }

    def _run(self, ctx: TaskContext) -> tuple[dict[str, Any], int]:
        with contextlib.redirect_stderr(io.StringIO()):
            return run(ctx)

    def _notification(self) -> dict[str, Any]:
        doc: dict[str, Any] = json.loads(
            (self.run_dir / "output" / "notification.json").read_text(encoding="utf-8")
        )
        return doc

    def test_completed_when_no_breaks(self) -> None:
        result, code = self._run(self._ctx({"broken": 0, "matched": 6}))
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "success")
        notif = self._notification()
        self.assertEqual(notif["status"], "completed")
        self.assertEqual(notif["counts"]["broken"], 0)

    def test_completed_with_breaks(self) -> None:
        _result, code = self._run(self._ctx({"broken": 4, "matched": 5}))
        self.assertEqual(code, 0)
        self.assertEqual(self._notification()["status"], "completed_with_breaks")

    def test_missing_broken_count_means_completed(self) -> None:
        self._run(self._ctx({"matched": 6}))
        self.assertEqual(self._notification()["status"], "completed")

    def test_generated_at_from_fixed_clock(self) -> None:
        self._run(self._ctx({"broken": 0, "matched": 6}))
        self.assertEqual(self._notification()["generatedAt"], "2026-03-16T09:30:00Z")

    def test_payload_references_and_hashes_upstream_files(self) -> None:
        self._run(self._ctx({"broken": 1, "matched": 5}))
        notif = self._notification()
        output = self.run_dir / "output"
        report_bytes = (output / "settlement-report.json").read_bytes()
        archive_bytes = (output / "archive-manifest.json").read_bytes()
        self.assertEqual(notif["runId"], "test")
        self.assertEqual(notif["scenario"], "happy-path")
        self.assertEqual(notif["reportPath"], "output/settlement-report.json")
        self.assertEqual(notif["evidencePath"], "output/archive-manifest.json")
        self.assertEqual(notif["reportSha256"], hashlib.sha256(report_bytes).hexdigest())
        self.assertEqual(notif["archiveSha256"], hashlib.sha256(archive_bytes).hexdigest())

    def test_missing_archive_manifest_is_tolerated(self) -> None:
        _result, code = self._run(self._ctx({"broken": 0}, with_archive=False))
        self.assertEqual(code, 0)
        self.assertIsNone(self._notification()["archiveSha256"])

    def test_malformed_archive_manifest_fails_the_task(self) -> None:
        ctx = self._ctx({"broken": 0})
        (self.run_dir / "output" / "archive-manifest.json").write_text("{", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self._run(ctx)
        self.assertFalse((self.run_dir / "output" / "notification.json").exists())

    def test_missing_report_is_missing_upstream(self) -> None:
        with self.assertRaisesRegex(MissingUpstreamError, "settlement report not found"):
            self._run(self._ctx(None))
        self.assertFalse((self.run_dir / "stages").exists())

    def test_stage_file_and_result_artifacts(self) -> None:
        counts = {"broken": 2, "matched": 4}
        result, _code = self._run(self._ctx(counts))
        stage = json.loads((self.run_dir / "stages" / "notify.json").read_text(encoding="utf-8"))
        self.assertEqual(
            stage,
            {
                "notificationPath": "output/notification.json",
                "runId": "test",
                "scenario": "happy-path",
                "status": "completed_with_breaks",
            },
        )
        self.assertEqual(result["counts"], counts)
        self.assertEqual(result["metrics"], {"notificationStatus": "completed_with_breaks"})
        self.assertEqual(
            [a["path"] for a in result["artifacts"]],
            ["output/notification.json", "stages/notify.json"],
        )
        for artifact in result["artifacts"]:
            data = (self.run_dir / artifact["path"]).read_bytes()
            self.assertEqual(artifact["sha256"], hashlib.sha256(data).hexdigest())

    def test_logs_status_to_stderr(self) -> None:
        err = io.StringIO()
        ctx = self._ctx({"broken": 0})
        with contextlib.redirect_stderr(err):
            run(ctx)
        self.assertEqual(err.getvalue(), "notify :: notification written status=completed\n")


if __name__ == "__main__":
    unittest.main()
