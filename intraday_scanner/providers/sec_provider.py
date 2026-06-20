"""SEC/filings provider abstractions and offline-safe defaults."""

from __future__ import annotations

import urllib.parse
import urllib.request
from collections.abc import Sequence
from xml.etree import ElementTree

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.providers.base import FilingItem, SECProvider


class NullSECProvider(SECProvider):
    """Offline provider used when no filings feed is configured."""

    def validate_credentials(self) -> None:
        return None

    def get_filings(self, symbols: Sequence[str], since: str | None = None) -> list[FilingItem]:
        return []


class SECRSSProvider(SECProvider):
    endpoint = "https://www.sec.gov/cgi-bin/browse-edgar"

    def __init__(self, config: ScannerConfig):
        self.timeout = config.request_timeout_seconds

    def validate_credentials(self) -> None:
        return None

    def get_filings(self, symbols: Sequence[str], since: str | None = None) -> list[FilingItem]:
        rows: list[FilingItem] = []
        for symbol in symbols:
            rows.extend(self._request_symbol(symbol))
        return rows

    def _request_symbol(self, symbol: str) -> list[FilingItem]:
        params = {
            "action": "getcompany",
            "CIK": symbol,
            "owner": "exclude",
            "count": "10",
            "output": "atom",
        }
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Dawnstrike research scanner contact@example.com"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
            root = ElementTree.fromstring(raw)
        except (OSError, ElementTree.ParseError) as exc:
            raise DataProviderError(f"SEC RSS request failed for {symbol}: {exc}") from exc
        return [_filing_from_entry(symbol, entry) for entry in _entries(root)]


def build_sec_provider(config: ScannerConfig) -> SECProvider:
    return SECRSSProvider(config)


def filing_has_dilution_risk(item: FilingItem) -> bool:
    text = f"{item.filing_type} {item.headline}".lower()
    risk_terms = ("s-1", "s-3", "424b", "atm", "offering", "warrant")
    return any(term in text for term in risk_terms)


def _entries(root: ElementTree.Element) -> list[ElementTree.Element]:
    return [element for element in root.iter() if element.tag.endswith("entry")]


def _text(entry: ElementTree.Element, suffix: str) -> str:
    for child in entry.iter():
        if child.tag.endswith(suffix) and child.text:
            return child.text.strip()
    return ""


def _filing_from_entry(symbol: str, entry: ElementTree.Element) -> FilingItem:
    title = _text(entry, "title")
    category = ""
    for child in entry.iter():
        if child.tag.endswith("category"):
            category = child.attrib.get("term", "")
            break
    url = ""
    for child in entry.iter():
        if child.tag.endswith("link"):
            url = child.attrib.get("href", "")
            break
    return FilingItem(
        ticker=symbol.upper(),
        filing_type=category or title.split(" - ", 1)[0],
        filed_at=_text(entry, "updated"),
        source="sec_rss",
        url=url,
        headline=title,
    )
