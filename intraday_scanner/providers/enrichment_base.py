"""Provider-neutral snapshot enrichment contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import SnapshotRow


@dataclass(frozen=True)
class EnrichmentPatch:
    ticker: str
    values: dict[str, object]
    source: str


class EnrichmentProvider(Protocol):
    name: str

    def enrich(
        self,
        snapshots: list[SnapshotRow],
        config: ScannerConfig,
    ) -> list[EnrichmentPatch]:
        """Return explicit field patches for known enrichment data."""

