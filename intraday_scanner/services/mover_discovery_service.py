"""Universe discovery and provider-health count helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import ScanResult, SnapshotRow
from intraday_scanner.providers.alpaca_movers_provider import AlpacaMoversProvider
from intraday_scanner.providers.csv_movers_provider import CsvMoversProvider
from intraday_scanner.services.provider_health_service import record_health_status


@dataclass(frozen=True)
class UniverseSelection:
    symbols: list[str]
    source: str
    detail: str


def resolve_universe(
    *,
    provider_name: str,
    config: ScannerConfig,
    explicit_symbols: Iterable[str] | None = None,
    universe_file: str | Path | None = None,
    snapshot_file: str | Path | None = None,
) -> UniverseSelection:
    symbols = _dedupe(explicit_symbols or [])
    if symbols:
        return UniverseSelection(
            symbols=symbols,
            source="explicit",
            detail=f"{len(symbols)} symbols",
        )
    provider = AlpacaMoversProvider() if provider_name == "alpaca" else CsvMoversProvider()
    result = provider.discover(config, universe_file=universe_file, snapshot_file=snapshot_file)
    return UniverseSelection(symbols=result.symbols, source=result.source, detail=result.detail)


def provider_count_payload(
    *,
    symbols_requested: Iterable[str],
    snapshots: Iterable[SnapshotRow],
    result: ScanResult | None = None,
) -> dict[str, int]:
    snapshot_rows = list(snapshots)
    requested = _dedupe(symbols_requested)
    return {
        "symbols_requested": len(requested),
        "symbols_returned": len({row.ticker for row in snapshot_rows}),
        "symbols_with_premarket_volume": sum(
            1 for row in snapshot_rows if row.premarket_volume > 0
        ),
        "snapshot_row_count": len(snapshot_rows),
        "candidate_count": len(result.ranked_candidates) if result else 0,
        "top_explosive_count": len(result.top_explosive) if result else 0,
        "symbols_passing_filters": len(result.ranked_candidates) if result else 0,
    }


def record_provider_counts(store: Any, provider: str, counts: dict[str, int]) -> None:
    record_health_status(
        store,
        provider=f"{provider}:counts",
        status="ok",
        detail=json.dumps(counts, sort_keys=True),
    )


def require_universe(symbols: list[str], provider_name: str) -> None:
    if not symbols:
        raise DataProviderError(
            f"{provider_name} live scan requires --symbols, --symbols-file, or --universe-file."
        )


def _dedupe(symbols: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    clean: list[str] = []
    for raw in symbols:
        symbol = str(raw).strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            clean.append(symbol)
    return clean
