"""Point-in-time premarket plan-input enrichment for AlphaOps.

Candidate discovery pages do not publish premarket high/low.  This service
resolves those facts from observed one-minute extended-hours bars before the
scanner constructs a plan.  Missing or stale observations remain explicitly
ineligible; they are never converted to zero or synthetic executable levels.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SNAPSHOT_COLUMNS
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.yahoo_chart_provider import (
    YAHOO_SOURCE_NAME,
    bars_from_yahoo_chart_payload,
    chart_result,
    fetch_yahoo_chart,
    yahoo_chart_url,
)

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")
FetchChart = Callable[[str, ScannerConfig], dict[str, Any]]
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
ONE_MINUTE_SECONDS = 60
MAX_YAHOO_FALLBACK_RATIO = 0.25


@dataclass(frozen=True)
class PremarketObservation:
    ticker: str
    status: str
    premarket_high: float | None = None
    premarket_low: float | None = None
    previous_close: float | None = None
    latest_price: float | None = None
    premarket_volume: int | None = None
    observed_at: str = ""
    bar_completed_at: str = ""
    is_complete: bool = False
    bar_count: int = 0
    age_seconds: int | None = None
    source: str = YAHOO_SOURCE_NAME
    source_url: str = ""
    failure_reason: str = ""

    @property
    def is_usable(self) -> bool:
        return (
            self.status == "verified"
            and self.is_complete
            and bool(self.bar_completed_at)
            and self.premarket_high is not None
            and self.premarket_low is not None
            and self.premarket_high > self.premarket_low > 0
        )


def enrich_premarket_rows(
    rows: list[dict[str, Any]],
    *,
    config: ScannerConfig,
    requested_at: datetime | None = None,
    fetcher: FetchChart = fetch_yahoo_chart,
    source: str = "yahoo",
    alpaca_provider: AlpacaProvider | None = None,
    allow_yahoo_fallback: bool = False,
    rehearsal_mode: bool = False,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return rows plus auditable, point-in-time enrichment observations."""

    at = _as_utc(requested_at or datetime.now(UTC))
    copied = [dict(row) for row in rows]
    selected = _select_candidates(copied, config)
    selected_tickers = [str(row.get("ticker") or "").upper() for row in selected]
    observations: dict[str, PremarketObservation] = {}
    fallback_count = 0
    fallback_candidate_count = 0
    fallback_status = "not_applicable"
    fallback_status_by_ticker: dict[str, str] = {}
    normalized_source = source.strip().lower()
    if normalized_source not in {"alpaca", "yahoo"}:
        raise DataProviderError("Premarket enrichment source must be alpaca or yahoo.")
    source_name = (
        f"alpaca_market_data_{config.alpaca_data_feed.lower()}"
        if normalized_source == "alpaca"
        else YAHOO_SOURCE_NAME
    )

    if rehearsal_mode:
        fallback_status = "rehearsal_not_applicable"
        fallback_status_by_ticker = {
            ticker: "rehearsal_not_applicable" for ticker in selected_tickers
        }
    elif config.premarket_enrichment_enabled and selected_tickers:
        if normalized_source == "alpaca":
            observations = _observe_alpaca_tickers(
                selected_tickers,
                config=config,
                requested_at=at,
                provider=alpaca_provider,
            )
            fallback_tickers = [
                ticker
                for ticker in selected_tickers
                if observations[ticker].status
                in {"missing_premarket_bars", "stale_observation"}
            ]
            fallback_candidate_count = len(fallback_tickers)
            fallback_candidate_ratio = _ratio(
                fallback_candidate_count,
                len(selected_tickers),
            )
            fallback_status_by_ticker = {
                ticker: (
                    "candidate"
                    if ticker in fallback_tickers
                    else ("not_needed" if observations[ticker].is_usable else "ineligible")
                )
                for ticker in selected_tickers
            }
            if not allow_yahoo_fallback:
                fallback_status = "disabled"
                for ticker in fallback_tickers:
                    fallback_status_by_ticker[ticker] = "disabled"
            elif not fallback_tickers:
                fallback_status = "not_needed"
            elif fallback_candidate_ratio > MAX_YAHOO_FALLBACK_RATIO:
                # Sparse free-feed coverage is a symbol-level data-quality event,
                # not a systemic provider outage.  Keep Yahoo out of the ranking
                # path when the ceiling is exceeded, and let the cycle finish with
                # only the independently verified Alpaca rows (or an honest
                # DATA_INELIGIBLE/no-trade result when none remain).
                fallback_status = "ceiling_exceeded_not_applied"
                for ticker in fallback_tickers:
                    fallback_status_by_ticker[ticker] = fallback_status
            else:
                fallback_observations = _observe_yahoo_tickers(
                    fallback_tickers,
                    config=config,
                    requested_at=at,
                    fetcher=fetcher,
                )
                for ticker, fallback in fallback_observations.items():
                    if fallback.is_usable:
                        observations[ticker] = fallback
                        fallback_count += 1
                        fallback_status_by_ticker[ticker] = "applied"
                    else:
                        fallback_status_by_ticker[ticker] = "attempted_unusable"
                if fallback_count == len(fallback_tickers):
                    fallback_status = "applied"
                elif fallback_count:
                    fallback_status = "partial"
                else:
                    fallback_status = "attempted_unusable"
        else:
            observations = _observe_yahoo_tickers(
                selected_tickers,
                config=config,
                requested_at=at,
                fetcher=fetcher,
            )
            fallback_status_by_ticker = {
                ticker: "not_applicable" for ticker in selected_tickers
            }

    enriched_rows = [
        _apply_observation(
            row,
            observations.get(str(row.get("ticker") or "").upper()),
            enabled=config.premarket_enrichment_enabled,
            selected=str(row.get("ticker") or "").upper() in set(selected_tickers),
            primary_source=source_name,
            fallback_status=fallback_status_by_ticker.get(
                str(row.get("ticker") or "").upper(),
                "not_applicable",
            ),
        )
        for row in copied
    ]
    if rehearsal_mode:
        ranking_rows = enriched_rows
    else:
        ranking_eligible_tickers = {
            ticker for ticker, observation in observations.items() if observation.is_usable
        }
        ranking_rows = [
            row
            for row in enriched_rows
            if str(row.get("ticker") or "").upper() in ranking_eligible_tickers
        ]
    status_counts = Counter(observation.status for observation in observations.values())
    summary = {
        "schema_version": "alphaops.premarket_enrichment.v1",
        "status": (
            "rehearsal_only"
            if rehearsal_mode
            else _summary_status(
                enabled=config.premarket_enrichment_enabled,
                selected_count=len(selected_tickers),
                verified_count=sum(1 for item in observations.values() if item.is_usable),
            )
        ),
        "requested_at": at.isoformat(),
        "source": source_name,
        "secondary_source": (
            YAHOO_SOURCE_NAME
            if normalized_source == "alpaca" and allow_yahoo_fallback
            else None
        ),
        "secondary_fallback_count": fallback_count,
        "secondary_fallback_ratio": _ratio(fallback_count, len(selected_tickers)),
        "secondary_fallback_candidate_count": fallback_candidate_count,
        "secondary_fallback_candidate_ratio": _ratio(
            fallback_candidate_count,
            len(selected_tickers),
        ),
        "secondary_fallback_ceiling_ratio": MAX_YAHOO_FALLBACK_RATIO,
        "secondary_fallback_status": fallback_status,
        "verified_by_source": dict(
            sorted(
                Counter(
                    item.source for item in observations.values() if item.is_usable
                ).items()
            )
        ),
        "input_count": len(rows),
        "selected_count": len(selected_tickers),
        "verified_count": sum(1 for item in observations.values() if item.is_usable),
        "failed_count": sum(1 for item in observations.values() if not item.is_usable),
        "ranking_eligible_count": len(ranking_rows),
        "ranking_excluded_count": len(enriched_rows) - len(ranking_rows),
        "ranking_policy": (
            "fixture_rehearsal_only_non_learning"
            if rehearsal_mode
            else "verified_selected_premarket_observations_only"
        ),
        "rehearsal_mode": rehearsal_mode,
        "status_counts": dict(sorted(status_counts.items())),
        "max_candidates": config.premarket_enrichment_max_candidates,
        "max_age_seconds": config.premarket_enrichment_max_age_seconds,
        "research_only": True,
        "broker_execution": "disabled",
    }
    result = {
        "input_rows": copied,
        "rows": enriched_rows,
        "ranking_rows": ranking_rows,
        "summary": summary,
        "observations": [asdict(observations[ticker]) for ticker in sorted(observations)],
    }
    if out_dir is not None:
        result["paths"] = _write_artifacts(Path(out_dir), result)
    return result


def observation_from_chart_payload(
    ticker: str,
    payload: dict[str, Any],
    *,
    requested_at: datetime,
    max_age_seconds: int,
) -> PremarketObservation:
    """Extract one real premarket range without using future bars."""

    result = chart_result(payload)
    source_url = yahoo_chart_url(ticker)
    if not result:
        return _failed_observation(ticker, "chart result missing", source_url=source_url)
    meta = result.get("meta") or {}
    meta = meta if isinstance(meta, dict) else {}
    session_start, session_end = _premarket_session_bounds(meta, requested_at)
    requested_epoch = int(_as_utc(requested_at).timestamp())
    eligible = [
        row
        for row in bars_from_yahoo_chart_payload(ticker, payload)
        if _is_eligible_premarket_bar(row, session_start, session_end, requested_epoch)
    ]
    if not eligible:
        return _failed_observation(
            ticker,
            "no observed premarket bars at or before requested_at",
            source_url=source_url,
            status="missing_premarket_bars",
        )
    highs = [_float(row.get("high")) for row in eligible]
    lows = [_float(row.get("low")) for row in eligible]
    usable_highs = [value for value in highs if value is not None and value > 0]
    usable_lows = [value for value in lows if value is not None and value > 0]
    if not usable_highs or not usable_lows:
        return _failed_observation(
            ticker,
            "premarket bars lacked high/low facts",
            source_url=source_url,
            status="missing_range_values",
        )
    observed_epoch = max(int(row["timestamp"]) for row in eligible)
    completed_epoch = observed_epoch + ONE_MINUTE_SECONDS
    completed_at = datetime.fromtimestamp(completed_epoch, UTC).isoformat()
    age_seconds = max(0, requested_epoch - completed_epoch)
    if age_seconds > max_age_seconds:
        return PremarketObservation(
            ticker=ticker,
            status="stale_observation",
            observed_at=datetime.fromtimestamp(observed_epoch, UTC).isoformat(),
            bar_completed_at=completed_at,
            is_complete=True,
            bar_count=len(eligible),
            age_seconds=age_seconds,
            source_url=source_url,
            failure_reason=f"latest premarket bar is {age_seconds}s old",
        )
    high = max(usable_highs)
    low = min(usable_lows)
    if high <= low:
        return PremarketObservation(
            ticker=ticker,
            status="insufficient_range",
            premarket_high=round(high, 6),
            premarket_low=round(low, 6),
            previous_close=_previous_close(meta),
            observed_at=datetime.fromtimestamp(observed_epoch, UTC).isoformat(),
            bar_completed_at=completed_at,
            is_complete=True,
            bar_count=len(eligible),
            age_seconds=age_seconds,
            source_url=source_url,
            failure_reason="observed premarket high did not exceed low",
        )
    return PremarketObservation(
        ticker=ticker,
        status="verified",
        premarket_high=round(high, 6),
        premarket_low=round(low, 6),
        previous_close=_previous_close(meta),
        observed_at=datetime.fromtimestamp(observed_epoch, UTC).isoformat(),
        bar_completed_at=completed_at,
        is_complete=True,
        bar_count=len(eligible),
        age_seconds=age_seconds,
        source_url=source_url,
    )


def observation_from_alpaca_bars(
    ticker: str,
    bars: list[dict[str, Any]],
    *,
    previous_close: float | None,
    requested_at: datetime,
    max_age_seconds: int,
    feed: str,
) -> PremarketObservation:
    """Build one point-in-time premarket observation from Alpaca IEX bars."""

    source_name = f"alpaca_market_data_{feed.lower()}"
    requested_epoch = int(_as_utc(requested_at).timestamp())
    session_start, session_end = _premarket_session_bounds({}, requested_at)
    eligible: list[tuple[int, dict[str, Any]]] = []
    for row in bars:
        observed_epoch = _bar_epoch(row.get("timestamp"))
        if (
            observed_epoch is not None
            and session_start <= observed_epoch < session_end
            and observed_epoch + ONE_MINUTE_SECONDS <= requested_epoch
        ):
            eligible.append((observed_epoch, row))
    if not eligible:
        return _failed_observation(
            ticker,
            "no completed Alpaca premarket bars at or before requested_at",
            source=source_name,
            source_url=ALPACA_BARS_URL,
            status="missing_premarket_bars",
        )
    usable_highs = [
        value
        for _, row in eligible
        if (value := _float(row.get("high"))) is not None and value > 0
    ]
    usable_lows = [
        value
        for _, row in eligible
        if (value := _float(row.get("low"))) is not None and value > 0
    ]
    if not usable_highs or not usable_lows:
        return _failed_observation(
            ticker,
            "Alpaca premarket bars lacked high/low facts",
            source=source_name,
            source_url=ALPACA_BARS_URL,
            status="missing_range_values",
        )
    observed_epoch, latest_bar = max(eligible, key=lambda item: item[0])
    completed_epoch = observed_epoch + ONE_MINUTE_SECONDS
    completed_at = datetime.fromtimestamp(completed_epoch, UTC).isoformat()
    age_seconds = max(0, requested_epoch - completed_epoch)
    if age_seconds > max_age_seconds:
        return PremarketObservation(
            ticker=ticker,
            status="stale_observation",
            observed_at=datetime.fromtimestamp(observed_epoch, UTC).isoformat(),
            bar_completed_at=completed_at,
            is_complete=True,
            bar_count=len(eligible),
            age_seconds=age_seconds,
            source=source_name,
            source_url=ALPACA_BARS_URL,
            failure_reason=f"latest Alpaca premarket bar is {age_seconds}s old",
        )
    high = max(usable_highs)
    low = min(usable_lows)
    latest_price = _float(latest_bar.get("close"))
    volume = sum(max(0, _int(row.get("volume")) or 0) for _, row in eligible)
    status = "verified" if high > low else "insufficient_range"
    return PremarketObservation(
        ticker=ticker,
        status=status,
        premarket_high=round(high, 6),
        premarket_low=round(low, 6),
        previous_close=(
            round(previous_close, 6)
            if previous_close is not None and previous_close > 0
            else None
        ),
        latest_price=(
            round(latest_price, 6)
            if latest_price is not None and latest_price > 0
            else None
        ),
        premarket_volume=volume,
        observed_at=datetime.fromtimestamp(observed_epoch, UTC).isoformat(),
        bar_completed_at=completed_at,
        is_complete=True,
        bar_count=len(eligible),
        age_seconds=age_seconds,
        source=source_name,
        source_url=ALPACA_BARS_URL,
        failure_reason=("" if status == "verified" else "observed high did not exceed low"),
    )


def _observe_alpaca_tickers(
    tickers: list[str],
    *,
    config: ScannerConfig,
    requested_at: datetime,
    provider: AlpacaProvider | None,
) -> dict[str, PremarketObservation]:
    active_provider = provider or AlpacaProvider(config)
    try:
        active_provider.validate_credentials()
        start_epoch, _ = _premarket_session_bounds({}, requested_at)
        bars = active_provider.get_minute_bars(
            tickers,
            datetime.fromtimestamp(start_epoch, UTC).isoformat(),
            _as_utc(requested_at).isoformat(),
            config,
        )
        snapshots = active_provider.get_premarket_snapshot(tickers, config)
    except (DataProviderError, OSError, TypeError, ValueError) as exc:
        raise DataProviderError(
            "Systemic Alpaca premarket enrichment failure; "
            "Yahoo fallback was not attempted."
        ) from exc
    grouped: dict[str, list[dict[str, Any]]] = {ticker: [] for ticker in tickers}
    for row in bars:
        ticker = str(row.get("ticker") or "").upper()
        if ticker in grouped:
            grouped[ticker].append(row)
    previous_closes = {
        row.ticker.upper(): row.previous_close
        for row in snapshots
        if row.previous_close > 0
    }
    return {
        ticker: observation_from_alpaca_bars(
            ticker,
            grouped.get(ticker, []),
            previous_close=previous_closes.get(ticker),
            requested_at=requested_at,
            max_age_seconds=config.premarket_enrichment_max_age_seconds,
            feed=config.alpaca_data_feed,
        )
        for ticker in tickers
    }


def _observe_ticker(
    ticker: str,
    config: ScannerConfig,
    requested_at: datetime,
    fetcher: FetchChart,
) -> PremarketObservation:
    try:
        payload = fetcher(ticker, config)
    except DataProviderError as exc:
        return _failed_observation(ticker, str(exc))
    return observation_from_chart_payload(
        ticker,
        payload,
        requested_at=requested_at,
        max_age_seconds=config.premarket_enrichment_max_age_seconds,
    )


def _select_candidates(
    rows: list[dict[str, Any]],
    config: ScannerConfig,
) -> list[dict[str, Any]]:
    eligible = [
        row
        for row in rows
        if not _truthy(row.get("fixture_only"))
        and not _truthy(row.get("manual_uploaded_data"))
        and config.min_price <= (_float(row.get("premarket_price")) or -1) <= config.max_price
        and (_float(row.get("gap_pct")) or 0) >= config.min_gap_pct
        and (
            (_float(row.get("premarket_volume")) or 0) >= config.min_premarket_share_volume
            or (_float(row.get("dollar_volume")) or 0) >= config.min_premarket_dollar_volume
        )
        and _valid_ticker(str(row.get("ticker") or ""))
    ]
    return sorted(
        eligible,
        key=lambda row: (
            _float(row.get("dollar_volume")) or 0,
            _float(row.get("gap_pct")) or 0,
            str(row.get("ticker") or ""),
        ),
        reverse=True,
    )[: config.premarket_enrichment_max_candidates]


def _apply_observation(
    row: dict[str, Any],
    observation: PremarketObservation | None,
    *,
    enabled: bool,
    selected: bool,
    primary_source: str,
    fallback_status: str,
) -> dict[str, Any]:
    output = dict(row)
    discovery_source = str(
        row.get("preferred_source") or row.get("source") or "unknown"
    ).strip()
    output["premarket_price_source"] = _source_if_present(
        row.get("premarket_price"), discovery_source
    )
    output["previous_close_source"] = _source_if_present(
        row.get("previous_close"), discovery_source
    )
    output["premarket_high_source"] = _source_if_present(
        row.get("premarket_high"), discovery_source
    )
    output["premarket_low_source"] = _source_if_present(
        row.get("premarket_low"), discovery_source
    )
    output["premarket_volume_source"] = _source_if_present(
        row.get("premarket_volume"), discovery_source
    )
    output["gap_pct_source"] = _source_if_present(row.get("gap_pct"), discovery_source)
    output["dollar_volume_source"] = _source_if_present(
        row.get("dollar_volume"), discovery_source
    )
    output["enrichment_primary_source"] = primary_source
    output["enrichment_fallback_status"] = fallback_status
    output["enrichment_fallback_source"] = ""
    output["enrichment_was_fallback"] = False
    output["enrichment_observed_at"] = ""
    output["enrichment_bar_completed_at"] = ""
    output["enrichment_is_complete"] = False
    output["enrichment_observation_sha256"] = ""
    output["premarket_range_source"] = ""
    output["premarket_range_source_url"] = ""
    if not enabled:
        output["enrichment_status"] = "disabled"
        return output
    if not selected:
        output["enrichment_status"] = "not_selected"
        return output
    if observation is None:
        output["enrichment_status"] = "provider_error"
        return output
    output["enrichment_status"] = observation.status
    output["enrichment_source_url"] = observation.source_url
    output["enrichment_observed_at"] = observation.observed_at
    output["enrichment_bar_completed_at"] = observation.bar_completed_at
    output["enrichment_is_complete"] = observation.is_complete
    output["enrichment_observation_sha256"] = _observation_sha256(observation)
    if fallback_status == "applied" and observation.source != primary_source:
        output["enrichment_fallback_source"] = observation.source
        output["enrichment_was_fallback"] = True
    if not observation.is_usable:
        return output
    output["premarket_high"] = observation.premarket_high
    output["premarket_low"] = observation.premarket_low
    output["premarket_high_source"] = observation.source
    output["premarket_low_source"] = observation.source
    output["premarket_range_source"] = observation.source
    output["premarket_range_source_url"] = observation.source_url
    if observation.latest_price is not None and observation.latest_price > 0:
        output["premarket_price"] = observation.latest_price
        output["premarket_price_source"] = observation.source
    if observation.premarket_volume is not None:
        output["premarket_volume"] = observation.premarket_volume
        output["premarket_volume_source"] = observation.source
        price = _float(output.get("premarket_price"))
        if price is not None:
            output["dollar_volume"] = round(price * observation.premarket_volume, 2)
            output["dollar_volume_source"] = _derived_source(
                str(output.get("premarket_price_source") or "missing"),
                observation.source,
            )
    if observation.previous_close is not None and observation.previous_close > 0:
        output["previous_close"] = observation.previous_close
        output["previous_close_source"] = observation.source
        price = _float(output.get("premarket_price"))
        if price is not None:
            output["gap_pct"] = round(
                ((price - observation.previous_close) / observation.previous_close) * 100,
                4,
            )
            output["gap_pct_source"] = _derived_source(
                str(output.get("premarket_price_source") or "missing"),
                observation.source,
            )
    warnings = _warning_tokens(output.get("coverage_warning"))
    warnings.discard("premarket_range_unavailable_price_used")
    if observation.previous_close is not None:
        warnings.discard("previous_close_unavailable")
    warnings.add(f"premarket_range_source:{observation.source}")
    output["coverage_warning"] = ";".join(sorted(warnings))
    return output


def _premarket_session_bounds(
    meta: dict[str, Any],
    requested_at: datetime,
) -> tuple[int, int]:
    periods = meta.get("currentTradingPeriod") or {}
    premarket = periods.get("pre") if isinstance(periods, dict) else {}
    if isinstance(premarket, dict):
        start = _int(premarket.get("start"))
        end = _int(premarket.get("end"))
        if start and end and start < end:
            return start, end
    timezone_name = str(meta.get("exchangeTimezoneName") or "America/New_York")
    try:
        exchange_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        exchange_tz = EASTERN
    local_day = _as_utc(requested_at).astimezone(exchange_tz).date()
    start_dt = datetime.combine(local_day, datetime.min.time(), exchange_tz).replace(hour=4)
    end_dt = datetime.combine(local_day, datetime.min.time(), exchange_tz).replace(
        hour=9,
        minute=30,
    )
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def _is_eligible_premarket_bar(
    row: dict[str, Any],
    session_start: int,
    session_end: int,
    requested_epoch: int,
) -> bool:
    timestamp = _int(row.get("timestamp"))
    return bool(
        timestamp
        and session_start <= timestamp < session_end
        and timestamp + ONE_MINUTE_SECONDS <= requested_epoch
        and row.get("source") == YAHOO_SOURCE_NAME
    )


def _previous_close(meta: dict[str, Any]) -> float | None:
    for key in ("chartPreviousClose", "previousClose"):
        value = _float(meta.get(key))
        if value is not None and value > 0:
            return round(value, 6)
    return None


def _failed_observation(
    ticker: str,
    reason: str,
    *,
    source: str = YAHOO_SOURCE_NAME,
    source_url: str = "",
    status: str = "provider_error",
) -> PremarketObservation:
    return PremarketObservation(
        ticker=ticker,
        status=status,
        source=source,
        source_url=(
            source_url
            or (yahoo_chart_url(ticker) if source == YAHOO_SOURCE_NAME else "")
        ),
        failure_reason=reason[:500],
    )


def _observe_yahoo_tickers(
    tickers: list[str],
    *,
    config: ScannerConfig,
    requested_at: datetime,
    fetcher: FetchChart,
) -> dict[str, PremarketObservation]:
    if not tickers:
        return {}
    observations: dict[str, PremarketObservation] = {}
    worker_count = min(config.premarket_enrichment_workers, len(tickers))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(_observe_ticker, ticker, config, requested_at, fetcher): ticker
            for ticker in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                observations[ticker] = future.result()
            except (DataProviderError, OSError, TypeError, ValueError) as exc:
                observations[ticker] = _failed_observation(ticker, str(exc))
    return observations


def _bar_epoch(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _summary_status(*, enabled: bool, selected_count: int, verified_count: int) -> str:
    if not enabled:
        return "disabled"
    if selected_count == 0:
        return "no_eligible_candidates"
    if verified_count == selected_count:
        return "complete"
    if verified_count:
        return "partial"
    return "unavailable"


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _write_artifacts(output_dir: Path, result: dict[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / "premarket_snapshot.csv"
    with snapshot_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["ranking_rows"])
    source_rows_snapshot_path = output_dir / "premarket_snapshot_source_rows_audit.csv"
    with source_rows_snapshot_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["input_rows"])
    enriched_rows_snapshot_path = output_dir / "premarket_snapshot_enriched_rows_audit.csv"
    with enriched_rows_snapshot_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["rows"])
    observations_path = output_dir / "observations.json"
    observations_path.write_text(
        json.dumps(result["observations"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(result["summary"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "snapshot": str(snapshot_path),
        "source_rows_snapshot": str(source_rows_snapshot_path),
        "all_rows_snapshot": str(source_rows_snapshot_path),
        "enriched_rows_snapshot": str(enriched_rows_snapshot_path),
        "observations": str(observations_path),
        "summary": str(summary_path),
    }


def _warning_tokens(value: Any) -> set[str]:
    return {
        token.strip()
        for token in str(value or "").replace(",", ";").split(";")
        if token.strip()
    }


def _observation_sha256(observation: PremarketObservation) -> str:
    payload = json.dumps(
        asdict(observation),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_if_present(value: Any, source: str) -> str:
    return source if value not in {None, ""} else "missing"


def _derived_source(*sources: str) -> str:
    normalized = [source.strip() or "missing" for source in sources]
    unique = list(dict.fromkeys(normalized))
    return unique[0] if len(unique) == 1 else "derived:" + "+".join(unique)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _valid_ticker(value: str) -> bool:
    text = value.strip().upper()
    return 1 <= len(text) <= 6 and all(
        character.isalnum() or character in ".-" for character in text
    )


def _float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


__all__ = [
    "PremarketObservation",
    "enrich_premarket_rows",
    "observation_from_alpaca_bars",
    "observation_from_chart_payload",
]
