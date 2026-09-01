"""Read-only latest-run adapter and deterministic public projection writer."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.opportunity_store import (
    CURRENT_STORAGE_SCHEMA_VERSION,
    LEGACY_OPPORTUNITY_DATABASE_SCHEMA_VERSION,
    OPPORTUNITY_DATABASE_SCHEMA_VERSION,
    PREVIOUS_STORAGE_SCHEMA_VERSION,
    OpportunityPersistenceIntegrityError,
    OpportunityStore,
)
from intraday_scanner.storage.read_only import connect_read_only

from .opportunity_projection import (
    OpportunityProjection,
    OpportunityProjectionReason,
    build_opportunity_projection,
    disabled_projection,
    unavailable_projection,
)

MARKET_TIMEZONE = ZoneInfo("America/New_York")

OPPORTUNITY_PROJECTION_FLAG = "DAWNSTRIKE_OPPORTUNITY_PROJECTION_ENABLED"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SUPPORTED_SCHEMA_VERSIONS = frozenset(
    {
        LEGACY_OPPORTUNITY_DATABASE_SCHEMA_VERSION,
        OPPORTUNITY_DATABASE_SCHEMA_VERSION,
        PREVIOUS_STORAGE_SCHEMA_VERSION,
        CURRENT_STORAGE_SCHEMA_VERSION,
    }
)


def opportunity_projection_enabled(value: str | None = None) -> bool:
    raw = os.environ.get(OPPORTUNITY_PROJECTION_FLAG) if value is None else value
    return isinstance(raw, str) and raw.strip().lower() in _TRUE_VALUES


def load_latest_opportunity_projection(
    db_path: str | Path,
    *,
    enabled: bool | None = None,
    expected_market_date: str | None = None,
) -> OpportunityProjection:
    """Load the latest verified run without opening the database when disabled.

    The opportunity store is append-only and intentionally contains historical
    runs. A public build must therefore bind its read to the requested market
    date before selecting a row; otherwise a database-wide ``ORDER BY`` can
    silently expose a prior session as today's opportunity.
    """

    is_enabled = opportunity_projection_enabled() if enabled is None else enabled
    if not is_enabled:
        return disabled_projection()
    path = Path(db_path)
    if not path.is_file():
        return unavailable_projection(OpportunityProjectionReason.DATABASE_MISSING)
    normalized_market_date: str | None = None
    if expected_market_date is not None:
        normalized_market_date = _normalize_market_date(expected_market_date)
        if normalized_market_date is None:
            return unavailable_projection(OpportunityProjectionReason.READ_FAILED)

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_read_only(path, row_factory=sqlite3.Row)
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            return unavailable_projection(OpportunityProjectionReason.READ_FAILED)
        try:
            schema_row = connection.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return unavailable_projection(OpportunityProjectionReason.SCHEMA_UNSUPPORTED)
        schema_version = int(schema_row[0]) if schema_row is not None else 0
        if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
            return unavailable_projection(OpportunityProjectionReason.SCHEMA_UNSUPPORTED)
        if normalized_market_date is None:
            latest = connection.execute(
                """
                SELECT run_id
                FROM opportunity_pipeline_runs
                ORDER BY decision_at DESC, first_recorded_at DESC, run_id DESC
                LIMIT 1
                """
            ).fetchone()
        else:
            rows = connection.execute(
                """
                SELECT run_id, decision_at
                FROM opportunity_pipeline_runs
                ORDER BY decision_at DESC, first_recorded_at DESC, run_id DESC
                """
            ).fetchall()
            latest = next(
                (
                    row
                    for row in rows
                    if _market_date_for_timestamp(row[1]) == normalized_market_date
                ),
                None,
            )
        if latest is None:
            return unavailable_projection(OpportunityProjectionReason.NO_PERSISTED_RUN)
        run_id = str(latest[0])
    except sqlite3.DatabaseError:
        return unavailable_projection(OpportunityProjectionReason.DATABASE_INVALID)
    except (OSError, StorageError, ValueError):
        return unavailable_projection(OpportunityProjectionReason.READ_FAILED)
    finally:
        if connection is not None:
            connection.close()

    try:
        result = OpportunityStore(path, read_only=True).load_run(run_id)
        if result is None:
            return unavailable_projection(OpportunityProjectionReason.REPLAY_FAILED)
        if (
            normalized_market_date is not None
            and _market_date_for_timestamp(result.decision_at) != normalized_market_date
        ):
            return unavailable_projection(OpportunityProjectionReason.REPLAY_FAILED)
        return build_opportunity_projection(result)
    except OpportunityPersistenceIntegrityError:
        return unavailable_projection(OpportunityProjectionReason.REPLAY_FAILED)
    except (OSError, StorageError, sqlite3.DatabaseError, TypeError, ValueError):
        return unavailable_projection(OpportunityProjectionReason.REPLAY_FAILED)


def write_public_opportunity_projection(
    data_dir: str | Path,
    projection: OpportunityProjection,
    *,
    expected_market_date: str | None = None,
) -> dict[str, object]:
    """Write canonical bounded JSON plus its SHA manifest to an explicit staging path."""

    destination = Path(data_dir)
    destination.mkdir(parents=True, exist_ok=True)
    payload_path = destination / "opportunity-projection.json"
    manifest_path = destination / "opportunity-projection.json.manifest.json"
    payload = projection.to_json().encode("utf-8")
    normalized_market_date = (
        _normalize_market_date(expected_market_date)
        if expected_market_date is not None
        else _projection_market_date(projection)
    )
    if expected_market_date is not None and normalized_market_date is None:
        raise ValueError("opportunity projection market date is invalid")
    if projection.state.value in {"QUALIFYING", "NO_QUALIFYING"}:
        if (
            normalized_market_date is None
            or _projection_market_date(projection) != normalized_market_date
        ):
            raise ValueError("opportunity projection is not bound to the expected market date")
    payload_value = json.loads(payload)
    payload_path.write_bytes(payload)
    manifest = {
        "schema_version": "dawnstrike.opportunity_projection_manifest.v1",
        "payload_sha256": projection.content_hash(),
        "byte_count": len(payload),
        "state": projection.state.value,
        "row_count": len(projection.rows),
        "max_rows": 5,
        "market_date": normalized_market_date,
        "source_run_id": payload_value.get("source_run_id"),
        "as_of": payload_value.get("as_of"),
        "research_only": True,
        "order_execution_enabled": False,
    }
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        encoding="utf-8",
    )
    return manifest


def _normalize_market_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == value else None


def _projection_market_date(projection: OpportunityProjection) -> str | None:
    if projection.as_of is None:
        return None
    return projection.as_of.astimezone(MARKET_TIMEZONE).date().isoformat()


def _market_date_for_timestamp(value: object) -> str | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(MARKET_TIMEZONE).date().isoformat()


__all__ = [
    "OPPORTUNITY_PROJECTION_FLAG",
    "load_latest_opportunity_projection",
    "opportunity_projection_enabled",
    "write_public_opportunity_projection",
]
