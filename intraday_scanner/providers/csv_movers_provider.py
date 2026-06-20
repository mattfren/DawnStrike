"""CSV-backed mover discovery for sample and local universe files."""

from __future__ import annotations

import csv
from pathlib import Path

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import validate_required_columns
from intraday_scanner.providers.movers_base import MoverDiscoveryResult


class CsvMoversProvider:
    name = "csv_movers"

    def discover(
        self,
        config: ScannerConfig,
        *,
        universe_file: str | Path | None = None,
        snapshot_file: str | Path | None = None,
    ) -> MoverDiscoveryResult:
        del config
        source = Path(universe_file or snapshot_file or "")
        if not str(source):
            raise DataProviderError("CSV mover discovery requires --universe-file or --snapshot.")
        symbols = _read_symbols(source)
        return MoverDiscoveryResult(
            symbols=symbols,
            source=str(source),
            detail=f"loaded {len(symbols)} symbol(s) from {source}",
        )


def _read_symbols(path: str | Path) -> list[str]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise DataProviderError(f"Universe file does not exist: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise DataProviderError(f"Universe file is empty: {csv_path}")
        validate_required_columns(set(reader.fieldnames), ["ticker"], str(csv_path))
        seen: set[str] = set()
        symbols: list[str] = []
        for row in reader:
            symbol = str(row.get("ticker", "")).strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    if not symbols:
        raise DataProviderError(f"Universe file has no ticker rows: {csv_path}")
    return symbols

