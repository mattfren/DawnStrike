"""Read-only daily-DAG health and durable heartbeat contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.services.daily_run_service import DAILY_STAGE_ORDER
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

HEARTBEAT_SCHEMA = "dawnstrike.daily_orchestrator_heartbeat.v1"
DEFAULT_HEARTBEAT_TTL_MINUTES = 30


def write_heartbeat(
    *,
    state_root: str | Path,
    market_date: str,
    stage: str,
    run_id: str,
    status: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically update the non-secret daily heartbeat evidence file."""

    timestamp = now or datetime.now(timezone.utc)
    root = Path(state_root) / "heartbeats"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{market_date[:10]}.json"
    payload = {
        "schema_version": HEARTBEAT_SCHEMA,
        "market_date": market_date[:10],
        "stage": stage,
        "run_id": run_id,
        "status": status,
        "observed_at": timestamp.replace(microsecond=0).isoformat(),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    temporary = target.with_suffix(".tmp")
    temporary.write_text(_json(payload), encoding="utf-8")
    temporary.replace(target)
    return {**payload, "path": str(target)}


def daily_orchestration_status(
    store: SQLiteScanStore,
    *,
    market_date: str,
    state_root: str | Path,
    now: datetime | None = None,
    heartbeat_ttl_minutes: int = DEFAULT_HEARTBEAT_TTL_MINUTES,
) -> dict[str, Any]:
    """Report exact missed stages and stale heartbeat state without mutation."""

    if heartbeat_ttl_minutes <= 0:
        raise ValueError("heartbeat_ttl_minutes must be positive")
    current = now or datetime.now(timezone.utc)
    runs = store.load_daily_runs(market_date=market_date, limit=10)
    stages = store.load_daily_run_stages(market_date=market_date, limit=10_000)
    recorded = {str(row.get("stage_name") or "") for row in stages}
    heartbeat = _read_heartbeat(Path(state_root) / "heartbeats" / f"{market_date[:10]}.json")
    stale = _heartbeat_stale(heartbeat, current, heartbeat_ttl_minutes)
    missing = [stage for stage in DAILY_STAGE_ORDER if stage not in recorded]
    failed = [
        row
        for row in stages
        if str(row.get("status") or "") in {"FAILED", "DEGRADED", "TERMINAL_MISSING"}
    ]
    status = "HEALTHY"
    if failed:
        status = "FAILED_STAGE_RECORDED"
    elif stale:
        status = "STALE_HEARTBEAT"
    elif missing:
        status = "MISSED_OR_PENDING_STAGES"
    return {
        "schema_version": "dawnstrike.daily_orchestrator_status.v1",
        "status": status,
        "market_date": market_date[:10],
        "latest_run": runs[0] if runs else None,
        "recorded_stages": sorted(recorded),
        "missing_stages": missing,
        "failed_stages": failed,
        "heartbeat": heartbeat,
        "heartbeat_stale": stale,
        "next_action": _next_action(status),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _read_heartbeat(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _heartbeat_stale(
    heartbeat: dict[str, Any] | None,
    now: datetime,
    ttl_minutes: int,
) -> bool:
    if heartbeat is None:
        return True
    try:
        observed = datetime.fromisoformat(str(heartbeat.get("observed_at") or ""))
    except ValueError:
        return True
    if observed.tzinfo is None:
        return True
    return observed < now - timedelta(minutes=ttl_minutes)


def _next_action(status: str) -> str:
    if status == "FAILED_STAGE_RECORDED":
        return "Inspect the earliest failed stage receipt and repair only its causal failure."
    if status == "STALE_HEARTBEAT":
        return "Inspect the per-market-day lock and scheduled-task result before rerunning."
    if status == "MISSED_OR_PENDING_STAGES":
        return "Verify session timing, then run only the first missing idempotent stage."
    return "Continue scheduled observation; no promotion is implied."


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True)


__all__ = [
    "DEFAULT_HEARTBEAT_TTL_MINUTES",
    "daily_orchestration_status",
    "write_heartbeat",
]
