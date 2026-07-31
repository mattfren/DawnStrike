"""Point-in-time premarket plan-input enrichment for AlphaOps.

Candidate discovery pages do not publish premarket high/low.  This service
resolves those facts from observed one-minute extended-hours bars before the
scanner constructs a plan.  Missing or stale observations remain explicitly
ineligible; they are never converted to zero or synthetic executable levels.
"""

from __future__ import annotations

import csv
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


@dataclass(frozen=True)
class PremarketObservation:
    ticker: str
    status: str
    premarket_high: float | None = None
    premarket_low: float | None = None
    previous_close: float | None = None
    observed_at: str = ""
    bar_count: int = 0
    age_seconds: int | None = None
    source: str = YAHOO_SOURCE_NAME
    source_url: str = ""
    failure_reason: str = ""

    @property
    def is_usable(self) -> bool:
        return (
            self.status == "verified"
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
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return rows plus auditable, point-in-time enrichment observations."""

    at = _as_utc(requested_at or datetime.now(UTC))
    copied = [dict(row) for row in rows]
    selected = _select_candidates(copied, config)
    selected_tickers = [str(row.get("ticker") or "").upper() for row in selected]
    observations: dict[str, PremarketObservation] = {}

    if config.premarket_enrichment_enabled and selected_tickers:
        worker_count = min(config.premarket_enrichment_workers, len(selected_tickers))
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(_observe_ticker, ticker, config, at, fetcher): ticker
                for ticker in selected_tickers
            }
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    observations[ticker] = future.result()
                except (DataProviderError, OSError, TypeError, ValueError) as exc:
                    observations[ticker] = _failed_observation(ticker, str(exc))

    enriched_rows = [
        _apply_observation(
            row,
            observations.get(str(row.get("ticker") or "").upper()),
            enabled=config.premarket_enrichment_enabled,
            selected=str(row.get("ticker") or "").upper() in set(selected_tickers),
        )
        for row in copied
    ]
    status_counts = Counter(observation.status for observation in observations.values())
    summary = {
        "schema_version": "alphaops.premarket_enrichment.v1",
        "status": _summary_status(
            enabled=config.premarket_enrichment_enabled,
            selected_count=len(selected_tickers),
            verified_count=sum(1 for item in observations.values() if item.is_usable),
        ),
        "requested_at": at.isoformat(),
        "source": YAHOO_SOURCE_NAME,
        "input_count": len(rows),
        "selected_count": len(selected_tickers),
        "verified_count": sum(1 for item in observations.values() if item.is_usable),
        "failed_count": sum(1 for item in observations.values() if not item.is_usable),
        "status_counts": dict(sorted(status_counts.items())),
        "max_candidates": config.premarket_enrichment_max_candidates,
        "max_age_seconds": config.premarket_enrichment_max_age_seconds,
        "research_only": True,
        "broker_execution": "disabled",
    }
    result = {
        "rows": enriched_rows,
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
    age_seconds = max(0, requested_epoch - observed_epoch)
    if age_seconds > max_age_seconds:
        return PremarketObservation(
            ticker=ticker,
            status="stale_observation",
            observed_at=datetime.fromtimestamp(observed_epoch, UTC).isoformat(),
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
        bar_count=len(eligible),
        age_seconds=age_seconds,
        source_url=source_url,
    )


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
) -> dict[str, Any]:
    output = dict(row)
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
    if not observation.is_usable:
        return output
    output["premarket_high"] = observation.premarket_high
    output["premarket_low"] = observation.premarket_low
    output["premarket_range_source"] = observation.source
    if observation.previous_close is not None and observation.previous_close > 0:
        output["previous_close"] = observation.previous_close
        output["previous_close_source"] = observation.source
        price = _float(output.get("premarket_price"))
        if price is not None:
            output["gap_pct"] = round(
                ((price - observation.previous_close) / observation.previous_close) * 100,
                4,
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
        and timestamp <= requested_epoch
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
    source_url: str = "",
    status: str = "provider_error",
) -> PremarketObservation:
    return PremarketObservation(
        ticker=ticker,
        status=status,
        source_url=source_url or yahoo_chart_url(ticker),
        failure_reason=reason[:500],
    )


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


def _write_artifacts(output_dir: Path, result: dict[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / "premarket_snapshot.csv"
    with snapshot_path.open("w", encoding="utf-8", newline="") as handle:
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
        "observations": str(observations_path),
        "summary": str(summary_path),
    }


def _warning_tokens(value: Any) -> set[str]:
    return {
        token.strip()
        for token in str(value or "").replace(",", ";").split(";")
        if token.strip()
    }


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
    "observation_from_chart_payload",
]
