"""Alpaca mover discovery adapter.

Alpaca's market-data snapshots endpoint is useful for a supplied universe, but
this repo does not assume access to a market-wide movers endpoint. Live scans
therefore require a local universe file or explicit symbols.
"""

from __future__ import annotations

from pathlib import Path

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.providers.csv_movers_provider import _read_symbols
from intraday_scanner.providers.movers_base import MoverDiscoveryResult


class AlpacaMoversProvider:
    name = "alpaca_movers"

    def discover(
        self,
        config: ScannerConfig,
        *,
        universe_file: str | Path | None = None,
        snapshot_file: str | Path | None = None,
    ) -> MoverDiscoveryResult:
        del config, snapshot_file
        if universe_file is None:
            raise DataProviderError(
                "Alpaca cannot discover market-wide movers without a supplied universe file. "
                "Pass --symbols, --symbols-file, or --universe-file."
            )
        symbols = _read_symbols(universe_file)
        return MoverDiscoveryResult(
            symbols=symbols,
            source=str(universe_file),
            detail=f"loaded {len(symbols)} Alpaca universe symbol(s) from {universe_file}",
        )

