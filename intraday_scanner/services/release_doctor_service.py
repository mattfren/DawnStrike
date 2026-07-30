"""Read-only release doctors used by the Phase 7 proof matrix."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def probability_doctor(db_path: str | Path) -> dict[str, Any]:
    tables = _table_names(db_path)
    count = _count(db_path, "alpha_outcome_labels") if "alpha_outcome_labels" in tables else 0
    status = "Calibrated" if count >= 100 else "Uncalibrated"
    return {
        "status": status,
        "evidence_state": "complete" if status == "Calibrated" else "not_eligible",
        "sample_count": count,
        "threshold": 100,
        "db_path": str(db_path),
        "next_action": "Collect sourced forward outcomes; do not display calibrated probability."
        if status == "Uncalibrated"
        else "Run independent calibration and holdout checks.",
    }


def scheduler_doctor(root: str | Path) -> dict[str, Any]:
    base = Path(root)
    required = {
        "daily_runner": base / "scripts" / "run_daily_finalize.ps1",
        "task_registration": base / "scripts" / "register_daily_finalize_task.ps1",
        "restore_previous": base / "scripts" / "restore_previous_publish_task.ps1",
    }
    present: dict[str, bool] = {name: path.is_file() for name, path in required.items()}
    return {
        "status": "LOCAL_VERIFIED" if all(present.values()) else "FAILED",
        "required_files": present,
        "scheduled_task": "must be checked on approved checkout",
        "next_action": "Register exactly one replacement task after merge."
        if all(present.values())
        else "Restore missing scheduler artifacts.",
    }


def dashboard_doctor(db_path: str | Path, root: str | Path) -> dict[str, Any]:
    base = Path(root)
    readiness_path = base / "build" / "public" / "readiness.json"
    verifier_path = base / "build" / "public" / "data" / "performance.json"
    tables = _table_names(db_path)
    result: dict[str, Any] = {
        "status": "IN_PROGRESS"
        if readiness_path.is_file() and verifier_path.is_file()
        else "NOT_STARTED",
        "readiness_path": str(readiness_path),
        "snapshot_path": str(verifier_path),
        "canonical_tables": {
            name: name in tables
            for name in (
                "portfolio_performance_rows",
                "portfolio_daily_performance",
                "public_snapshot_manifests",
            )
        },
    }
    if readiness_path.is_file():
        try:
            result["readiness"] = json.loads(readiness_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result["status"] = "FAILED"
            result["readiness"] = {"status": "unreadable"}
        else:
            readiness = result["readiness"]
            result["status"] = (
                "LOCAL_VERIFIED" if readiness.get("status") == "ready" else "IN_PROGRESS"
            )
    return result


def _table_names(db_path: str | Path) -> set[str]:
    path = Path(db_path)
    if not path.exists():
        return set()
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def _count(db_path: str | Path, table: str) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
