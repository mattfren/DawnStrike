"""Capture read-only scheduled-monitor definition and current runtime receipts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
RUNTIME = Path(r"C:\r\dawnstrike-runtime")
STATE = Path(r"C:\r\dawnstrike-state")
TASK_NAME = "Dawnstrike AlphaOps Monitor 5m"
RECEIPTS = (
    "alpha_monitor_heartbeat-2026-08-17.receipt.json",
    "alpha_monitor_calendar-2026-08-17.receipt.json",
    "alpha_monitor-2026-08-17.receipt.json",
    "trade_watch-2026-08-17.receipt.json",
    "record_stage-intraday_monitor-2026-08-17.receipt.json",
    "scenario_monitor-2026-08-17.receipt.json",
    "record_stage-scenario_intelligence-2026-08-17.receipt.json",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"Export-ScheduledTask -TaskName '{TASK_NAME}'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    task_xml = EVIDENCE / "scheduled-task-definition.xml"
    task_xml.write_text(completed.stdout, encoding="utf-16")
    entries = []
    for name in RECEIPTS:
        path = STATE / "logs" / name
        stat = path.stat()
        entries.append(
            {
                "path": str(path),
                "length": stat.st_size,
                "mtime_utc": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
                "sha256": sha256(path),
                "payload": json.loads(path.read_text(encoding="utf-8-sig")),
            }
        )
    runner = RUNTIME / "scripts/run_alphaops_monitor.ps1"
    payload = {
        "schema_version": "dawnstrike.final_commit_monitor_evidence.v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_name": TASK_NAME,
        "task_export_exit_code": completed.returncode,
        "task_export_stderr": completed.stderr,
        "task_xml_path": task_xml.relative_to(ROOT).as_posix(),
        "task_xml_sha256": sha256(task_xml),
        "runtime_root": str(RUNTIME),
        "state_root": str(STATE),
        "runner_path": str(runner),
        "runner_sha256": sha256(runner),
        "receipts": entries,
    }
    (EVIDENCE / "monitor-runtime-evidence-after.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "task_export_exit_code": completed.returncode,
                "receipt_count": len(entries),
                "all_receipts_exit_zero": all(
                    item["payload"]["exit_code"] == 0 for item in entries
                ),
            },
            sort_keys=True,
        )
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
