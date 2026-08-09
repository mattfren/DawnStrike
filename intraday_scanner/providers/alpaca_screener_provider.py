"""Authenticated, read-only Alpaca market-wide candidate discovery.

The provider combines Alpaca's stock screener with its active-asset directory
and IEX snapshots.  It never imports or calls a trading/order endpoint; the
asset directory is used only to reject inactive, OTC, non-tradable, and
obviously non-common instruments before a symbol reaches AlphaOps.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import EVIDENCE_CONFIDENCE_VERSION, utc_now_iso
from intraday_scanner.network_safety import open_allowlisted_url
from intraday_scanner.providers.alpaca_provider import AlpacaProvider

SCREENER_BASE_URL = "https://data.alpaca.markets/v1beta1/screener/stocks"
ASSETS_URL = "https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity"
ALLOWED_EXCHANGES = frozenset({"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"})
NON_COMMON_NAME_TERMS = (
    " warrant",
    " warrants",
    " right",
    " rights",
    " unit",
    " units",
    " preferred",
    " preference",
    " depositary",
    " etf",
    " fund",
    " trust",
    " notes due",
    " proshares",
    " direxion",
    " ultrapro",
    " ultra short",
)
_TICKER = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


class AlpacaScreenerProvider:
    """Return a point-in-time, liquid US-equity research universe."""

    name = "alpaca_screener"

    def __init__(self, config: ScannerConfig):
        self.config = config
        self.market_data = AlpacaProvider(config)

    def collect(
        self,
        *,
        most_active_limit: int = 100,
        mover_limit: int = 50,
        include_losers: bool = False,
        max_symbols: int = 160,
    ) -> dict[str, Any]:
        self.market_data.validate_credentials()
        started_at = utc_now_iso()
        most_active = self.market_data._request_json(
            "/v1beta1/screener/stocks/most-actives",
            {"top": str(max(1, min(most_active_limit, 100))), "by": "volume"},
            self.config,
        )
        movers = self.market_data._request_json(
            "/v1beta1/screener/stocks/movers",
            {"top": str(max(1, min(mover_limit, 50)))},
            self.config,
        )
        assets = self._active_assets()
        asset_by_symbol = {
            str(row.get("symbol") or "").upper(): row
            for row in assets
            if isinstance(row, dict) and row.get("symbol")
        }
        discovery: dict[str, set[str]] = {}
        for row in list(most_active.get("most_actives") or []):
            symbol = _symbol(row)
            if symbol:
                discovery.setdefault(symbol, set()).add("most_active")
        for bucket in ("gainers", "losers") if include_losers else ("gainers",):
            for row in list(movers.get(bucket) or []):
                symbol = _symbol(row)
                if symbol:
                    discovery.setdefault(symbol, set()).add(bucket.rstrip("s"))

        accepted: list[str] = []
        rejected: dict[str, str] = {}
        for symbol in discovery:
            reason = _asset_rejection_reason(symbol, asset_by_symbol.get(symbol))
            if reason:
                rejected[symbol] = reason
            else:
                accepted.append(symbol)
        accepted = accepted[: max(1, max_symbols)]
        snapshots = self.market_data.get_premarket_snapshot(accepted, self.config)
        rows: list[dict[str, Any]] = []
        members: list[dict[str, Any]] = []
        for snapshot in snapshots:
            asset = asset_by_symbol.get(snapshot.ticker, {})
            row = snapshot.to_dict()
            completeness_values = (
                snapshot.previous_close,
                snapshot.premarket_price,
                snapshot.premarket_high,
                snapshot.premarket_low,
                snapshot.premarket_volume,
                snapshot.dollar_volume,
                snapshot.float_shares,
                snapshot.market_cap,
                snapshot.short_float_pct,
            )
            field_completeness_score = round(
                100.0 * sum(value is not None for value in completeness_values)
                / len(completeness_values),
                2,
            )
            row.update(
                {
                    "company": str(asset.get("name") or snapshot.ticker),
                    "source": self.name,
                    "source_url": SCREENER_BASE_URL,
                    "extraction_mode": "authenticated_api",
                    "data_source_kind": "alpaca_api",
                    "source_timestamp": str(
                        movers.get("last_updated")
                        or most_active.get("last_updated")
                        or snapshot.as_of_timestamp
                    ),
                    "extracted_at": utc_now_iso(),
                    "source_confidence": 92.0,
                    "field_completeness_score": field_completeness_score,
                    "source_reliability_prior": 85.0,
                    "reconciliation_status": "single_source",
                    "reconciliation_confidence_score": 0.0,
                    "evidence_confidence_version": EVIDENCE_CONFIDENCE_VERSION,
                    "source_count": 1,
                    "score_consensus": "single_authenticated_source",
                    "preferred_source": self.name,
                    "row_merge_reason": "authenticated_market_discovery",
                    "source_quality_status": "VERIFIED",
                    "halt_status": "UNKNOWN",
                    "sec_risk_status": "UNKNOWN",
                    "corporate_action_status": "UNKNOWN",
                    "discovery_context": ";".join(sorted(discovery[snapshot.ticker])),
                }
            )
            rows.append(row)
            members.append(
                {
                    "ticker": snapshot.ticker,
                    "listing_status": "ACTIVE",
                    "valid_from": started_at[:10],
                    "source_ref": str(asset.get("id") or snapshot.ticker),
                    "eligibility": {
                        "asset_class": asset.get("class"),
                        "exchange": asset.get("exchange"),
                        "tradable": asset.get("tradable"),
                        "marginable": asset.get("marginable"),
                        "shortable": asset.get("shortable"),
                        "fractionable": asset.get("fractionable"),
                        "discovery_context": sorted(discovery[snapshot.ticker]),
                    },
                }
            )
        completed_at = utc_now_iso()
        raw_identity = {
            "most_active_last_updated": most_active.get("last_updated"),
            "movers_last_updated": movers.get("last_updated"),
            "symbols": accepted,
            "members": members,
        }
        return {
            "status": "success" if rows else "no_data",
            "source": self.name,
            "source_type": "alpaca_screener_api",
            "started_at": started_at,
            "completed_at": completed_at,
            "rows_extracted": len(discovery),
            "rows_normalized": len(rows),
            "rows_rejected": len(rejected),
            "rejection_reason_counts": _counts(rejected.values()),
            "rows": rows,
            "last_updated": {
                "most_actives": most_active.get("last_updated"),
                "movers": movers.get("last_updated"),
            },
            "universe_evidence": {
                "provider_id": "alpaca",
                "dataset_id": "stocks-screener-plus-active-assets",
                "dataset_version": str(
                    movers.get("last_updated") or most_active.get("last_updated") or completed_at
                ),
                "retrieved_at": completed_at,
                "members": members,
                "raw_artifact_sha256": hashlib.sha256(
                    json.dumps(raw_identity, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "terms_reference": "https://docs.alpaca.markets/",
                "entitlement_reference": "configured-alpaca-account",
                "accountable_contact": "dawnstrikebot@gmail.com",
            },
            "research_only": True,
            "broker_execution_enabled": False,
        }

    def _active_assets(self) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            ASSETS_URL,
            headers={
                "APCA-API-KEY-ID": self.config.alpaca_api_key_id,
                "APCA-API-SECRET-KEY": self.config.alpaca_api_secret_key,
                "Accept": "application/json",
            },
            method="GET",
        )
        last_error: Exception | None = None
        for attempt in range(1, self.config.request_retries + 1):
            try:
                with open_allowlisted_url(
                    request,
                    timeout=self.config.request_timeout_seconds,
                    allowed_hosts=("paper-api.alpaca.markets",),
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, list):
                    raise DataProviderError("Alpaca active assets returned an invalid payload.")
                return [row for row in payload if isinstance(row, dict)]
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise DataProviderError(
                        f"Alpaca active assets request failed with HTTP {exc.code}."
                    ) from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.config.request_retries:
                time.sleep(min(2 ** (attempt - 1), 8))
        raise DataProviderError(
            "Alpaca active assets request failed after retries."
        ) from last_error


def _symbol(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("symbol") or "").upper().strip()


def _asset_rejection_reason(symbol: str, asset: dict[str, Any] | None) -> str:
    if not _TICKER.fullmatch(symbol):
        return "invalid_symbol"
    if asset is None:
        return "asset_identity_missing"
    if str(asset.get("status") or "").lower() != "active":
        return "inactive"
    if str(asset.get("class") or "").lower() != "us_equity":
        return "not_us_equity"
    if str(asset.get("exchange") or "").upper() not in ALLOWED_EXCHANGES:
        return "unsupported_exchange"
    if asset.get("tradable") is not True:
        return "not_tradable"
    name = " " + str(asset.get("name") or "").lower() + " "
    if any(term in name for term in NON_COMMON_NAME_TERMS):
        return "non_common_security_name"
    return ""


def _counts(values: Any) -> dict[str, int]:
    output: dict[str, int] = {}
    for value in values:
        key = str(value)
        output[key] = output.get(key, 0) + 1
    return dict(sorted(output.items()))


__all__ = ["AlpacaScreenerProvider"]
