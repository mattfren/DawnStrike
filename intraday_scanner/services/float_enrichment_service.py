"""Attach a current share count to premarket candidates before scoring.

``formula._float_rotation_score`` awards up to 14 of the 100 available score
points, but returns a hard 0.0 when ``float_shares`` is missing.  Every live
candidate was missing it, so every candidate silently forfeited those points and
none could reach the A/B setup grade the alert gate requires.

This service resolves the missing input automatically from SEC XBRL - a source
the project already reaches - and must run *before* ``ScanService`` reads the
snapshot, or the enrichment cannot influence the grade it exists to inform.

It never invents a number.  A ticker with no current SEC share count keeps
``unknown_float`` and simply does not earn the float-rotation points.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from intraday_scanner.models import SNAPSHOT_COLUMNS, utc_now_iso
from intraday_scanner.providers.sec_float_provider import (
    collect_share_counts,
    enrich_rows_with_float,
)
from intraday_scanner.providers.web_source_base import get_source
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

SNAPSHOT_NAME = "premarket_snapshot_with_float.csv"


def enrich_rows_with_sec_float(
    rows: list[dict[str, Any]],
    *,
    source_config: Any,
    store: SQLiteScanStore | None = None,
    out_dir: Path | str | None = None,
    as_of: Any = "",
    rehearsal_mode: bool = False,
    snapshot_fallback: str = "",
) -> dict[str, Any]:
    """Resolve ``float_shares`` for ``rows`` and rewrite the scan snapshot."""

    base_summary: dict[str, Any] = {
        "generated_at": utc_now_iso(),
        "requested_count": len(rows),
        "resolved_count": 0,
        "status": "SKIPPED",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    result: dict[str, Any] = {"rows": list(rows), "summary": base_summary}
    if snapshot_fallback:
        result["snapshot_path"] = snapshot_fallback

    if not rows:
        base_summary["status"] = "NO_CANDIDATES"
        return result
    if rehearsal_mode:
        # Fixture runs must not reach the network; the snapshot is reused as-is.
        base_summary["status"] = "REHEARSAL_SKIPPED"
        return result

    source = get_source(source_config, "sec_edgar")
    if source is None or not getattr(source, "enabled", False):
        base_summary["status"] = "DISABLED"
        base_summary["reason"] = "sec_edgar source is not enabled"
        return result

    tickers = [str(row.get("ticker") or "").upper() for row in rows if row.get("ticker")]
    output_dir = Path(out_dir) if out_dir is not None else None
    # ``as_of`` arrives as either an ISO string or a datetime depending on the
    # caller; the provider only needs the calendar day.
    as_of_day = str(getattr(as_of, "isoformat", lambda: as_of)() or "")[:10] or None
    counts = collect_share_counts(
        source=source,
        config=source_config,
        tickers=tickers,
        out_dir=output_dir or Path("."),
        store=store,
        persist=store is not None,
        as_of=as_of_day,
    )
    enriched = enrich_rows_with_float(rows, counts)

    summary = {
        **base_summary,
        "status": str(counts.get("status") or "unknown").upper(),
        "resolved_count": int(counts.get("resolved_count") or 0),
        "stale_count": len(counts.get("stale_tickers") or []),
        "unavailable_count": len(counts.get("unavailable_tickers") or []),
        "resolved_tickers": list(counts.get("resolved_tickers") or []),
        "stale_tickers": list(counts.get("stale_tickers") or []),
        "unavailable_tickers": list(counts.get("unavailable_tickers") or []),
        "source_kind": counts.get("source_kind"),
        "proxy_note": counts.get("proxy_note"),
        "cutoff": counts.get("cutoff"),
    }
    result["rows"] = enriched
    result["summary"] = summary

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        snapshot = output_dir / SNAPSHOT_NAME
        with snapshot.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(enriched)
        (output_dir / "float_enrichment_summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result["snapshot_path"] = str(snapshot)
    return result


__all__ = ["SNAPSHOT_NAME", "enrich_rows_with_sec_float"]
