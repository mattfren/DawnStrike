"""Apply optional snapshot enrichment without fabricating unknown fields."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import SnapshotRow
from intraday_scanner.providers.enrichment_base import EnrichmentProvider
from intraday_scanner.services.provider_health_service import record_health_status

TRACKED_FIELDS = [
    "float_shares",
    "market_cap",
    "short_float_pct",
    "current_halt",
    "recent_offering",
    "reverse_split_90d",
    "has_news",
    "catalyst_headline",
    "catalyst_url",
    "spread_pct",
]


def enrich_snapshots(
    snapshots: list[SnapshotRow],
    config: ScannerConfig,
    providers: list[EnrichmentProvider],
) -> tuple[list[SnapshotRow], dict[str, Any]]:
    enriched = list(snapshots)
    applied_by_provider: dict[str, int] = {}
    for provider in providers:
        patches = provider.enrich(enriched, config)
        applied_by_provider[provider.name] = len(patches)
        enriched = _apply_patches(enriched, patches)
    report = enrichment_report(enriched, applied_by_provider)
    return enriched, report


def enrichment_report(
    snapshots: list[SnapshotRow],
    applied_by_provider: dict[str, int] | None = None,
) -> dict[str, Any]:
    total = len(snapshots)
    counts = {field: 0 for field in TRACKED_FIELDS}
    for snapshot in snapshots:
        for field in TRACKED_FIELDS:
            value = getattr(snapshot, field)
            if value not in {None, ""}:
                counts[field] += 1
    required_for_quality = ["float_shares", "market_cap", "short_float_pct", "catalyst_url"]
    missing_quality_fields = sum(total - counts[field] for field in required_for_quality)
    max_missing = max(total * len(required_for_quality), 1)
    completeness_pct = round(100 - ((missing_quality_fields / max_missing) * 100), 2)
    return {
        "snapshot_row_count": total,
        "field_counts": counts,
        "completeness_pct": completeness_pct,
        "applied_by_provider": applied_by_provider or {},
    }


def record_enrichment_health(store: Any, report: dict[str, Any]) -> None:
    record_health_status(
        store,
        provider="enrichment",
        status="ok",
        detail=json.dumps(report, sort_keys=True),
    )


def _apply_patches(snapshots: list[SnapshotRow], patches: list[Any]) -> list[SnapshotRow]:
    patch_by_ticker: dict[str, dict[str, object]] = {}
    for patch in patches:
        patch_by_ticker.setdefault(patch.ticker.upper(), {}).update(patch.values)
    output = []
    for snapshot in snapshots:
        values = patch_by_ticker.get(snapshot.ticker.upper())
        output.append(replace(snapshot, **cast(Any, values)) if values else snapshot)
    return output
