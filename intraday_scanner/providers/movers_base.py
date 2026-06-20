"""Provider-neutral universe discovery contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from intraday_scanner.config import ScannerConfig


@dataclass(frozen=True)
class MoverDiscoveryResult:
    symbols: list[str]
    source: str
    detail: str


class MoverDiscoveryProvider(Protocol):
    name: str

    def discover(
        self,
        config: ScannerConfig,
        *,
        universe_file: str | Path | None = None,
        snapshot_file: str | Path | None = None,
    ) -> MoverDiscoveryResult:
        """Return symbols to scan, or raise a clear provider error."""

