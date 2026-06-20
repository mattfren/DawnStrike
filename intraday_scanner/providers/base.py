"""Provider abstraction for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import SnapshotRow


class MarketDataProvider(ABC):
    @abstractmethod
    def validate_credentials(self) -> None:
        """Raise a DataProviderError if credentials are missing or invalid."""

    @abstractmethod
    def get_premarket_snapshot(
        self, symbols: Sequence[str] | None, config: ScannerConfig
    ) -> list[SnapshotRow]:
        """Return normalized premarket snapshot rows."""

    @abstractmethod
    def get_minute_bars(
        self, symbols: Sequence[str], start: str, end: str, config: ScannerConfig
    ) -> list[dict[str, Any]]:
        """Return provider-normalized minute bars."""

    @abstractmethod
    def get_previous_close(self, symbols: Sequence[str], config: ScannerConfig) -> dict[str, float]:
        """Return previous close prices keyed by ticker."""


BaseProvider = MarketDataProvider


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    status: str
    checked_at: str
    detail: str = ""


@dataclass(frozen=True)
class NewsItem:
    ticker: str
    headline: str
    published_at: str
    source: str
    url: str = ""
    summary: str = ""


@dataclass(frozen=True)
class FilingItem:
    ticker: str
    filing_type: str
    filed_at: str
    source: str
    url: str = ""
    headline: str = ""


class NewsProvider(ABC):
    @abstractmethod
    def validate_credentials(self) -> None:
        """Raise a DataProviderError if credentials are required and missing."""

    @abstractmethod
    def get_news(self, symbols: Sequence[str], since: str | None = None) -> list[NewsItem]:
        """Return normalized news items for symbols."""


class SECProvider(ABC):
    @abstractmethod
    def validate_credentials(self) -> None:
        """Raise a DataProviderError if credentials are required and missing."""

    @abstractmethod
    def get_filings(self, symbols: Sequence[str], since: str | None = None) -> list[FilingItem]:
        """Return normalized filings/dilution-risk items for symbols."""


class NotificationProvider(ABC):
    channel: str

    @abstractmethod
    def send(self, payload: dict[str, Any]) -> None:
        """Send one notification payload."""
