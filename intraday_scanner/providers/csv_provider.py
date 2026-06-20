"""Offline CSV market data provider."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError, SnapshotValidationError
from intraday_scanner.models import (
    SNAPSHOT_REQUIRED_COLUMNS,
    SnapshotRow,
    validate_required_columns,
)
from intraday_scanner.providers.base import MarketDataProvider


def read_snapshot_csv(path: str | Path) -> list[SnapshotRow]:
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        raise DataProviderError(f"Snapshot file does not exist: {snapshot_path}")
    with snapshot_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SnapshotValidationError(f"{snapshot_path} is empty or missing a header row")
        validate_required_columns(
            set(reader.fieldnames), SNAPSHOT_REQUIRED_COLUMNS, str(snapshot_path)
        )
        return [SnapshotRow.from_mapping(row, source=str(snapshot_path)) for row in reader]


def read_csv_rows(
    path: str | Path, required_columns: list[str], source: str
) -> list[dict[str, Any]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise DataProviderError(f"{source} file does not exist: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SnapshotValidationError(f"{csv_path} is empty or missing a header row")
        validate_required_columns(set(reader.fieldnames), required_columns, str(csv_path))
        return list(reader)


class CsvSnapshotProvider(MarketDataProvider):
    def __init__(self, snapshot_path: str | Path):
        self.snapshot_path = Path(snapshot_path)

    def validate_credentials(self) -> None:
        if not self.snapshot_path.exists():
            raise DataProviderError(f"Snapshot file does not exist: {self.snapshot_path}")

    def get_premarket_snapshot(
        self, symbols: Sequence[str] | None, config: ScannerConfig
    ) -> list[SnapshotRow]:
        rows = read_snapshot_csv(self.snapshot_path)
        if not symbols:
            return rows
        wanted = {symbol.upper() for symbol in symbols}
        return [row for row in rows if row.ticker in wanted]

    def get_minute_bars(
        self, symbols: Sequence[str], start: str, end: str, config: ScannerConfig
    ) -> list[dict[str, Any]]:
        raise DataProviderError("CsvSnapshotProvider does not have a minute-bar file configured")

    def get_previous_close(self, symbols: Sequence[str], config: ScannerConfig) -> dict[str, float]:
        rows = self.get_premarket_snapshot(symbols, config)
        return {row.ticker: row.previous_close for row in rows}


CSVProvider = CsvSnapshotProvider
