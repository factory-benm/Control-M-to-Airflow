"""archive_and_notify task (CONTRACT 6.8).

Writes a notification payload file only. Performs no network activity of any
kind: no email, webhook, or message is sent.
"""

from __future__ import annotations

from pathlib import Path

from . import common


def run(ctx: dict) -> tuple[dict, int]:
    run_dir: Path = ctx["run_dir"]
    scenario: str = ctx["scenario"]
    run_id: str = ctx["run_id"]
    now: str = ctx["now"]

    report_path = run_dir / "output" / "settlement-report.json"
    archive_path = run_dir / "output" / "archive-manifest.json"

    report = common.read_json_optional(report_path)
    archive = common.read_json_optional(archive_path)

    if report is None:
        raise common.MissingUpstreamError("settlement report not found: output/settlement-report.json")

    counts = report.get("counts", {})
    broken = counts.get("broken", 0)
    status = "completed_with_breaks" if broken > 0 else "completed"

    notification = {
        "runId": run_id,
        "scenario": scenario,
        "status": status,
        "counts": counts,
        "reportPath": "output/settlement-report.json",
        "evidencePath": "output/archive-manifest.json",
        "reportSha256": common.sha256_file(report_path) if report_path.exists() else None,
        "archiveSha256": common.sha256_file(archive_path) if archive_path.exists() else None,
        "generatedAt": now,
    }

    notification_path = run_dir / "output" / "notification.json"
    notify_stage = run_dir / "stages" / "notify.json"
    common.write_json(notification_path, notification)
    common.write_json(notify_stage, {
        "scenario": scenario,
        "runId": run_id,
        "status": status,
        "notificationPath": "output/notification.json",
    })
    common.log(f"notification written status={status}")

    result = {
        "status": "success",
        "counts": counts,
        "artifacts": common.artifacts_for(run_dir, [
            "output/notification.json",
            "stages/notify.json",
        ]),
        "metrics": {"notificationStatus": status},
    }
    return result, 0
