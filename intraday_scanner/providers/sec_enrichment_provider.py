"""SEC filing enrichment for offering/reverse-split risk flags."""

from __future__ import annotations

from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import SnapshotRow
from intraday_scanner.providers.base import SECProvider
from intraday_scanner.providers.enrichment_base import EnrichmentPatch
from intraday_scanner.providers.sec_provider import filing_has_dilution_risk


class SECEnrichmentProvider:
    name = "sec_enrichment"

    def __init__(self, provider: SECProvider):
        self.provider = provider

    def enrich(
        self,
        snapshots: list[SnapshotRow],
        config: ScannerConfig,
    ) -> list[EnrichmentPatch]:
        del config
        symbols = [snapshot.ticker for snapshot in snapshots]
        filings = self.provider.get_filings(symbols)
        patches: dict[str, dict[str, object]] = {}
        for filing in filings:
            ticker = filing.ticker.upper()
            if filing_has_dilution_risk(filing):
                patches.setdefault(ticker, {})["recent_offering"] = True
                patches[ticker]["catalyst_headline"] = filing.headline
                patches[ticker]["catalyst_url"] = filing.url
        return [
            EnrichmentPatch(ticker=ticker, values=values, source=self.name)
            for ticker, values in patches.items()
        ]

