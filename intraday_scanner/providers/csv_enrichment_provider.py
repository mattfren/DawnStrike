"""CSV enrichment provider for local float/news/risk metadata."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SnapshotRow, parse_bool, validate_required_columns
from intraday_scanner.providers.enrichment_base import EnrichmentPatch

ENRICHMENT_FIELDS = {
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
}

BOOL_FIELDS = {"current_halt", "recent_offering", "reverse_split_90d", "has_news"}
FLOAT_FIELDS = {"float_shares", "market_cap", "short_float_pct", "spread_pct"}


class CsvEnrichmentProvider:
    name = "csv_enrichment"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def enrich(
        self,
        snapshots: list[SnapshotRow],
        config: ScannerConfig,
    ) -> list[EnrichmentPatch]:
        del snapshots, config
        rows = _read_rows(self.path)
        patches = []
        for ticker, row in rows.items():
            values: dict[str, object] = {}
            for field in ENRICHMENT_FIELDS:
                if field not in row or row[field] in {None, ""}:
                    continue
                values[field] = _coerce(field, row[field])
            if values:
                patches.append(EnrichmentPatch(ticker=ticker, values=values, source=str(self.path)))
        return patches


def _read_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise DataProviderError(f"Enrichment file does not exist: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DataProviderError(f"Enrichment file is empty: {path}")
        validate_required_columns(set(reader.fieldnames), ["ticker"], str(path))
        return {str(row["ticker"]).strip().upper(): row for row in reader if row.get("ticker")}


def _coerce(field: str, value: Any) -> object:
    if field in BOOL_FIELDS:
        return parse_bool(value)
    if field in FLOAT_FIELDS:
        return float(value)
    return str(value).strip()

