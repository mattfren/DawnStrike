"""Scanner orchestration service."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import ScanResult
from intraday_scanner.providers.base import MarketDataProvider
from intraday_scanner.providers.enrichment_base import EnrichmentProvider
from intraday_scanner.scoring import score_universe
from intraday_scanner.services.enrichment_service import enrich_snapshots, record_enrichment_health
from intraday_scanner.storage.base import ScanStore

LOGGER = logging.getLogger(__name__)


class ScanService:
    def __init__(
        self,
        provider: MarketDataProvider,
        store: ScanStore | None = None,
        enrichment_providers: list[EnrichmentProvider] | None = None,
    ):
        self.provider = provider
        self.store = store
        self.enrichment_providers = enrichment_providers or []

    def run(
        self,
        config: ScannerConfig,
        symbols: Sequence[str] | None = None,
        *,
        persist: bool = False,
    ) -> ScanResult:
        LOGGER.info("Starting scan using provider=%s", config.provider)
        self.provider.validate_credentials()
        snapshots = self.provider.get_premarket_snapshot(symbols, config)
        LOGGER.info("Loaded %s snapshot row(s)", len(snapshots))
        if self.enrichment_providers:
            snapshots, report = enrich_snapshots(snapshots, config, self.enrichment_providers)
            if self.store is not None:
                record_enrichment_health(self.store, report)
        result = score_universe(
            snapshots,
            config,
            historical_outcomes=self._historical_intelligence_outcomes(),
        )
        if persist:
            if self.store is None:
                raise ValueError("persist=True requires a store")
            self.store.persist_scan_result(result)
            LOGGER.info("Persisted scan run %s", result.run_id)
        return result

    def _historical_intelligence_outcomes(self) -> list[dict[str, object]]:
        if self.store is None or not hasattr(self.store, "load_intelligence_outcomes"):
            return []
        try:
            loaded = self.store.load_intelligence_outcomes(limit=5000)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - history must not block a live scan
            LOGGER.exception("Could not load historical intelligence outcomes")
            return []
        return list(loaded or [])
