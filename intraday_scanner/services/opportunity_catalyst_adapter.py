"""Read-only production adapter for retained local catalyst evidence."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.storage.test_isolation import is_active_database_path
from intraday_scanner.v2.opportunity.catalyst import (
    CatalystEvidence,
    InjectedCatalystAdapter,
)


def load_retained_catalyst_adapter(
    database_path: str | Path,
    *,
    decision_at: datetime,
    symbols: tuple[str, ...],
) -> InjectedCatalystAdapter:
    """Load causal catalyst facts from an explicit non-active retained store."""

    if decision_at.tzinfo is None or decision_at.utcoffset() is None:
        raise ValueError("catalyst decision_at must be timezone-aware")
    if is_active_database_path(database_path):
        raise ValueError("active database is forbidden for retained catalyst evidence")
    resolved = Path(database_path).resolve(strict=False)
    normalized_symbols = tuple(sorted({item.strip().upper() for item in symbols if item.strip()}))
    if not normalized_symbols:
        return InjectedCatalystAdapter({})
    query = """
        SELECT event_id, symbol, source_kind, source_content_hash_sha256,
               published_at, first_seen_at, event_type, payload_json
          FROM catalyst_evidence_events
         WHERE symbol = ?
         ORDER BY symbol ASC, first_seen_at ASC, event_id ASC
    """
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_read_only(resolved, row_factory=sqlite3.Row)
        rows = [
            row
            for symbol in normalized_symbols
            for row in connection.execute(query, (symbol,)).fetchall()
        ]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return InjectedCatalystAdapter({})
        raise
    finally:
        if connection is not None:
            connection.close()

    selected: dict[str, tuple[datetime, str, CatalystEvidence]] = {}
    for row in rows:
        published_at = _parse_catalyst_time(row["published_at"])
        first_seen_at = _parse_catalyst_time(row["first_seen_at"])
        if published_at is None or first_seen_at is None:
            continue
        available_at = max(published_at, first_seen_at)
        if available_at > decision_at:
            continue
        state = str(row["event_type"] or "").strip()
        symbol = str(row["symbol"] or "").strip().upper()
        event_id = str(row["event_id"] or "").strip()
        source_kind = str(row["source_kind"] or "").strip()
        content_hash = str(row["source_content_hash_sha256"] or "").strip().lower()
        if not state or not symbol or not event_id or not source_kind:
            continue
        if len(content_hash) != 64:
            continue
        try:
            int(content_hash, 16)
        except ValueError:
            continue
        payload_text = str(row["payload_json"] or "")
        try:
            payload = json.dumps(
                json.loads(payload_text),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        except (TypeError, ValueError):
            continue
        evidence = CatalystEvidence.from_payload(
            symbol=symbol,
            state=state,
            observed_at=first_seen_at,
            available_at=available_at,
            source_identity=f"retained-catalyst:{source_kind}:{event_id}:{content_hash}",
            payload=payload,
        )
        ordering = (available_at, event_id, evidence)
        if symbol not in selected or ordering[:2] > selected[symbol][:2]:
            selected[symbol] = ordering
    return InjectedCatalystAdapter(
        {symbol: value[2] for symbol, value in sorted(selected.items())}
    )


def _parse_catalyst_time(value: object) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


__all__ = ["load_retained_catalyst_adapter"]
