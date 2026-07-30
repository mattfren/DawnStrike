"""Build and publish a bounded public performance snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.storage.migrations import run_migrations

MAX_SNAPSHOT_BYTES = 250 * 1024


def write_public_snapshot(
    db_path: str | Path,
    output_path: str | Path,
    *,
    market_date: str | None = None,
    days: int = 30,
    row_limit: int = 250,
) -> dict[str, Any]:
    service = CanonicalPerformanceService(db_path)
    chosen: dict[str, Any] | None = None
    for limit in range(max(1, row_limit), 0, -1):
        payload = service.load_public_data(days=days, row_limit=limit)
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        if len(encoded) <= MAX_SNAPSHOT_BYTES:
            chosen = {"payload": payload, "encoded": encoded}
            break
    if chosen is None:
        raise ValueError(
            f"Public snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes even with no detail rows"
        )

    payload = chosen["payload"]
    encoded = chosen["encoded"]
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    payload_sha256 = hashlib.sha256(encoded).hexdigest()
    input_hash = _input_hash(payload)
    effective_date = market_date or _latest_date(payload)
    status = _snapshot_status(payload)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    manifest = {
        "schema_version": "dawnstrike.public_snapshot_manifest.v1",
        "manifest_id": hashlib.sha256(
            f"{effective_date}:{input_hash}:{payload_sha256}".encode()
        ).hexdigest(),
        "market_date": effective_date,
        "status": status,
        "generated_at": generated_at,
        "input_hash_sha256": input_hash,
        "payload_sha256": payload_sha256,
        "artifact_path": str(path).replace("\\", "/"),
        "row_count": len(payload.get("rows") or []),
        "byte_count": len(encoded),
        "limits": payload.get("limits", {}),
        "research_only": True,
        "live_trading_enabled": False,
    }
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    manifest_encoded = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    manifest_path.write_bytes(manifest_encoded)
    with sqlite3.connect(Path(db_path)) as connection:
        run_migrations(connection)
        connection.execute(
            """
            INSERT INTO public_snapshot_manifests
            (manifest_id, market_date, status, generated_at, input_hash_sha256,
             payload_sha256, artifact_path, row_count, byte_count, failure_reason, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(market_date) DO UPDATE SET
                manifest_id = excluded.manifest_id,
                status = excluded.status,
                generated_at = excluded.generated_at,
                input_hash_sha256 = excluded.input_hash_sha256,
                payload_sha256 = excluded.payload_sha256,
                artifact_path = excluded.artifact_path,
                row_count = excluded.row_count,
                byte_count = excluded.byte_count,
                failure_reason = NULL,
                payload_json = excluded.payload_json
            """,
            (
                manifest["manifest_id"],
                effective_date,
                status,
                generated_at,
                input_hash,
                payload_sha256,
                manifest["artifact_path"],
                manifest["row_count"],
                manifest["byte_count"],
                json.dumps(manifest, sort_keys=True),
            ),
        )
    return {"manifest": manifest, "snapshot_path": str(path), "manifest_path": str(manifest_path)}


def _input_hash(payload: dict[str, Any]) -> str:
    hashes = [str(row.get("input_hash_sha256") or "") for row in payload.get("daily") or []]
    return hashlib.sha256(json.dumps(sorted(hashes), separators=(",", ":")).encode()).hexdigest()


def _latest_date(payload: dict[str, Any]) -> str:
    dates = [str(row.get("market_date") or "") for row in payload.get("daily") or []]
    return max(dates) if dates else "unknown"


def _snapshot_status(payload: dict[str, Any]) -> str:
    daily = list(payload.get("daily") or [])
    if not daily:
        return "no_data"
    if any(str(row.get("status")) == "DEGRADED" for row in daily):
        return "degraded"
    if any(str(row.get("status")) == "PARTIAL" for row in daily):
        return "degraded"
    if all(str(row.get("status")) == "NO_TRADE" for row in daily):
        return "no_trade"
    return "complete"
