"""Isolated unit checks for the notification payload shape (CONTRACT 6.8)."""

import json
import tempfile
import unittest
from pathlib import Path

from payment_notifications.notify import run


class TestNotification(unittest.TestCase):
    def _ctx(self, tmp: Path, counts: dict) -> dict:
        run_dir = tmp / "run"
        (run_dir / "output").mkdir(parents=True)
        (run_dir / "stages").mkdir(parents=True)
        report = {
            "scenario": "happy-path", "runId": "test", "counts": counts,
        }
        (run_dir / "output" / "settlement-report.json").write_text(
            json.dumps(report), encoding="utf-8")
        (run_dir / "output" / "archive-manifest.json").write_text(
            json.dumps({"artifacts": []}), encoding="utf-8")
        return {
            "run_dir": run_dir, "scenario": "happy-path", "run_id": "test",
            "now": "2026-03-16T09:30:00Z", "attempt": 1,
        }

    def test_completed_when_no_breaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), {"broken": 0, "matched": 6})
            result, code = run(ctx)
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "success")
            notif = json.loads(
                (ctx["run_dir"] / "output" / "notification.json").read_text())
            self.assertEqual(notif["status"], "completed")
            self.assertEqual(notif["counts"]["broken"], 0)

    def test_completed_with_breaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), {"broken": 4, "matched": 5})
            result, code = run(ctx)
            self.assertEqual(code, 0)
            notif = json.loads(
                (ctx["run_dir"] / "output" / "notification.json").read_text())
            self.assertEqual(notif["status"], "completed_with_breaks")

    def test_generated_at_from_fixed_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(Path(tmp), {"broken": 0, "matched": 6})
            run(ctx)
            notif = json.loads(
                (ctx["run_dir"] / "output" / "notification.json").read_text())
            self.assertEqual(notif["generatedAt"], "2026-03-16T09:30:00Z")


if __name__ == "__main__":
    unittest.main()
