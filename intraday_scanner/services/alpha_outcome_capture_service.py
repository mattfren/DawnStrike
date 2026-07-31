"""Automatic, sourced AlphaOps paper-outcome capture.

This module observes public market bars after the regular session and derives
paper outcomes for saved research watches.  It never places orders or infers a
price where the source has no observation.  Only complete, timestamped Yahoo
bars can become learning-eligible outcomes.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_STRATEGY_ID,
    DEFAULT_V5_POLICY,
    alphaops_strategy_contract,
)
from intraday_scanner.config import ScannerConfig, load_config
from intraday_scanner.dashboard.operator_data_service import signal_requires_outcome
from intraday_scanner.errors import DataProviderError, SnapshotValidationError
from intraday_scanner.market_calendar import market_session
from intraday_scanner.models import utc_now_iso
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.yahoo_chart_provider import (
    YAHOO_SOURCE_NAME,
    bars_from_yahoo_chart_payload,
    fetch_yahoo_chart,
    yahoo_chart_url,
)
from intraday_scanner.services.alpha_paper_reconciliation_service import (
    recover_legacy_alpha_delivery_membership,
)
from intraday_scanner.services.price_observation_service import parse_requested_at
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc
CAPTURE_MODEL_VERSION = "alphaops-sourced-outcome-v3"
YAHOO_RANGE = "5d"
BAR_INTERVAL = "1m"
DEFAULT_MAX_CLOSE_STALENESS_SECONDS = 90
MAX_BAR_GAP_SECONDS = 60
CONCLUSIVE_STATUSES = {
    "complete_sourced",
    "not_triggered",
    "captured_ineligible_missing_plan",
    "not_entered_plan_dislocated",
}

FetchChart = Callable[..., dict[str, Any]]
FetchNormalizedBars = Callable[..., list[dict[str, Any]]]


@dataclass(frozen=True)
class SessionWindow:
    market_date: str
    opened_at: datetime
    closed_at: datetime
    is_trading_day: bool
    calendar: dict[str, object]


@dataclass(frozen=True)
class OutcomeBar:
    observed_at: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": _iso_utc(self.observed_at),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class BarCoverage:
    is_complete: bool
    status: str
    detail: str
    expected_start_at: datetime
    expected_end_at: datetime
    expected_minute_count: int
    observed_minute_count: int
    maximum_gap_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_coverage_complete": self.is_complete,
            "coverage_status": self.status,
            "coverage_detail": self.detail,
            "coverage_expected_start_at": _iso_utc(self.expected_start_at),
            "coverage_expected_end_at": _iso_utc(self.expected_end_at),
            "coverage_expected_minute_count": self.expected_minute_count,
            "coverage_observed_minute_count": self.observed_minute_count,
            "coverage_maximum_gap_seconds": self.maximum_gap_seconds,
            "coverage_allowed_gap_seconds": MAX_BAR_GAP_SECONDS,
        }


def capture_sourced_alpha_outcomes(
    *,
    db_path: str | Path,
    market_date: str | None = None,
    requested_at: str | None = None,
    out_dir: str | Path = "outputs/alpha_outcomes",
    persist: bool = True,
    replace: bool = False,
    max_close_staleness_seconds: int = DEFAULT_MAX_CLOSE_STALENESS_SECONDS,
    config: ScannerConfig | None = None,
    fetcher: FetchChart | None = None,
    fallback_fetcher: FetchNormalizedBars | None = None,
    provider_attempt_limit: int | None = None,
) -> dict[str, Any]:
    """Capture regular-session outcomes through a bounded market-data chain.

    The operation is fail-closed before the regular close.  It records a
    conclusive ``not_triggered`` outcome only when a complete EOD source window
    proves the trigger was never touched. Provider failures and missing bars
    remain ineligible outcomes, but their exhausted attempts are persisted as
    terminal operator evidence rather than disappearing.
    """

    if max_close_staleness_seconds <= 0:
        raise SnapshotValidationError("max_close_staleness_seconds must be positive.")
    at = parse_requested_at(requested_at, market_date=market_date)
    resolved_date = market_date or at.astimezone(EASTERN).date().isoformat()
    strategy_id = alphaops_strategy_contract(
        f"{resolved_date}T12:00:00-04:00"
    )[0]
    session = _session_window(resolved_date)
    captured_at = utc_now_iso()
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteScanStore(db_path)
    store.initialize()
    recover_legacy_alpha_delivery_membership(
        store,
        market_date=resolved_date,
        persist=persist,
    )
    selection_rows = [
        row
        for row in store.load_signal_selections(
            strategy_id=strategy_id,
            cohort="official_telegram",
            limit=50_000,
        )
        if str(row.get("selected_at") or "")[:10] == resolved_date
    ]
    _validate_exact_session_selections(selection_rows, market_date=resolved_date)
    no_trade_rows = [
        row
        for row in selection_rows
        if str(row.get("decision") or "").lower() == "no_trade"
        or str(row.get("ticker") or "").upper() == "NO_TRADE"
    ]
    selected_ids = {
        str(row.get("signal_id") or "")
        for row in selection_rows
        if str(row.get("decision") or "").lower() != "no_trade"
        and str(row.get("ticker") or "").upper() != "NO_TRADE"
    }
    if not selection_rows:
        raise SnapshotValidationError(
            "Exact AlphaOps session selection evidence is absent; outcome capture "
            "is blocked rather than attributing ranked or partial rows."
        )
    if no_trade_rows and selected_ids:
        raise SnapshotValidationError(
            "AlphaOps session selection evidence is contradictory: explicit no-trade "
            "and selected signals coexist."
        )
    signals = _outcome_targets(
        store.load_historical_signals(market_date=resolved_date, limit=50_000),
        selected_signal_ids=selected_ids,
    )
    recovered_signal_ids = {str(row.get("signal_id") or "") for row in signals}
    missing_signal_ids = selected_ids - recovered_signal_ids
    if missing_signal_ids:
        raise SnapshotValidationError(
            "Exact AlphaOps session selection is only partially persisted; missing "
            "watchable historical signals: " + ", ".join(sorted(missing_signal_ids))
        )
    existing = {
        str(row.get("signal_id") or ""): row
        for row in store.load_signal_outcomes(
            start=resolved_date,
            end=resolved_date,
            limit=50_000,
        )
        if str(row.get("signal_id") or "")
    }
    pending = [
        signal
        for signal in signals
        if replace or str(signal.get("signal_id") or "") not in existing
    ]
    diagnostics: list[dict[str, Any]] = [
        _existing_diagnostic(signal, existing[str(signal.get("signal_id") or "")])
        for signal in signals
        if not replace and str(signal.get("signal_id") or "") in existing
    ]
    source_bars: dict[str, list[dict[str, Any]]] = {}
    source_requests: list[dict[str, Any]] = []
    repairable_events = [
        _outcome_event(row)
        for row in existing.values()
        if not replace
        and row.get("automatic_sourced_data") is True
        and str(row.get("outcome_status") or "") in CONCLUSIVE_STATUSES
    ]
    repair_event_stats = (
        store.persist_signal_events(repairable_events)
        if persist and repairable_events
        else {"inserted": 0, "skipped": 0}
    )
    audit_event_stats = {
        "inserted": repair_event_stats["inserted"],
        "skipped": repair_event_stats["skipped"],
        "repaired_inserted": repair_event_stats["inserted"],
        "repaired_skipped": repair_event_stats["skipped"],
        "new_inserted": 0,
        "new_skipped": 0,
    }

    if not session.is_trading_day:
        summary = _summary(
            status="market_closed",
            market_date=resolved_date,
            requested_at=at,
            captured_at=captured_at,
            signal_count=len(signals),
            pending_count=len(pending),
            diagnostics=diagnostics,
            persisted={"inserted": 0, "skipped": 0},
            source_requests=source_requests,
            market_session=session.calendar,
            audit_events=audit_event_stats,
        )
        _write_artifacts(output_dir, summary, [], diagnostics, source_bars)
        return {**summary, "outcomes": [], "diagnostics": diagnostics, "out_dir": str(output_dir)}

    if at < session.closed_at:
        summary = _summary(
            status="session_incomplete",
            market_date=resolved_date,
            requested_at=at,
            captured_at=captured_at,
            signal_count=len(signals),
            pending_count=len(pending),
            diagnostics=diagnostics,
            persisted={"inserted": 0, "skipped": 0},
            source_requests=source_requests,
            market_session=session.calendar,
            audit_events=audit_event_stats,
        )
        _write_artifacts(output_dir, summary, [], diagnostics, source_bars)
        return {**summary, "outcomes": [], "diagnostics": diagnostics, "out_dir": str(output_dir)}

    if not pending:
        status = "already_captured" if signals else "no_targets"
        summary = _summary(
            status=status,
            market_date=resolved_date,
            requested_at=at,
            captured_at=captured_at,
            signal_count=len(signals),
            pending_count=0,
            diagnostics=diagnostics,
            persisted={"inserted": 0, "skipped": 0},
            source_requests=source_requests,
            market_session=session.calendar,
            audit_events=audit_event_stats,
        )
        _write_artifacts(output_dir, summary, [], diagnostics, source_bars)
        return {**summary, "outcomes": [], "diagnostics": diagnostics, "out_dir": str(output_dir)}

    scanner_config = config or load_config()
    chart_fetcher = fetcher or fetch_yahoo_chart
    attempt_limit = min(
        3,
        max(
            1,
            provider_attempt_limit
            if provider_attempt_limit is not None
            else scanner_config.request_retries,
        ),
    )
    bars_by_ticker: dict[str, list[OutcomeBar]] = {}
    source_evidence_by_ticker: dict[str, dict[str, Any]] = {}
    errors_by_ticker: dict[str, str] = {}
    signal_tickers = sorted({
        str(row.get("ticker") or "").upper() for row in pending
    })
    requested_tickers = [*signal_tickers]
    if "SPY" not in requested_tickers:
        requested_tickers.append("SPY")
    for ticker in requested_tickers:
        bars, evidence, requests, error = _fetch_outcome_bars(
            ticker,
            scanner_config,
            session=session,
            requested_at=at,
            captured_at=captured_at,
            chart_fetcher=chart_fetcher,
            fallback_fetcher=fallback_fetcher,
            attempt_limit=attempt_limit,
        )
        source_requests.extend(requests)
        bars_by_ticker[ticker] = bars
        source_bars[ticker] = [bar.to_dict() for bar in bars]
        source_evidence_by_ticker[ticker] = evidence
        if error and ticker in signal_tickers and not bars:
            errors_by_ticker[ticker] = error

    entry_intents: dict[str, dict[str, Any]] = {}
    for row in store.load_trade_intents(market_date=resolved_date, limit=50_000):
        signal_id = str(row.get("signal_id") or "")
        if (
            signal_id
            and signal_id not in entry_intents
            and str(row.get("action") or "").upper() == "ENTER_LONG"
            and row.get("official_paper_eligible") is True
        ):
            entry_intents[signal_id] = row
    fills_by_signal: dict[str, list[dict[str, Any]]] = {}
    for row in store.load_paper_trade_fills(market_date=resolved_date, limit=50_000):
        signal_id = str(row.get("signal_id") or "")
        if signal_id:
            fills_by_signal.setdefault(signal_id, []).append(row)

    outcomes: list[dict[str, Any]] = []
    capture_attempts: list[dict[str, Any]] = []
    for signal in pending:
        ticker = str(signal.get("ticker") or "").upper()
        if ticker in errors_by_ticker:
            diagnostic = _diagnostic(
                signal,
                "ineligible_provider_error",
                errors_by_ticker[ticker],
            )
            diagnostics.append(diagnostic)
            capture_attempts.append(
                _capture_attempt(
                    signal,
                    diagnostic,
                    market_date=resolved_date,
                    captured_at=captured_at,
                    requested_at=at,
                    source_evidence=source_evidence_by_ticker.get(ticker, {}),
                    source_requests=source_requests,
                )
            )
            continue
        outcome = _derive_outcome(
            signal,
            bars_by_ticker.get(ticker, []),
            session=session,
            requested_at=at,
            captured_at=captured_at,
            max_close_staleness_seconds=max_close_staleness_seconds,
            strategy_id=strategy_id,
            source_evidence=source_evidence_by_ticker.get(ticker, {}),
            benchmark_bars=bars_by_ticker.get("SPY", []),
            benchmark_evidence=source_evidence_by_ticker.get("SPY", {}),
            entry_intent=entry_intents.get(str(signal.get("signal_id") or "")),
            paper_fills=fills_by_signal.get(str(signal.get("signal_id") or ""), []),
        )
        diagnostic = _diagnostic_from_outcome(outcome)
        diagnostics.append(diagnostic)
        capture_attempts.append(
            _capture_attempt(
                signal,
                diagnostic,
                market_date=resolved_date,
                captured_at=captured_at,
                requested_at=at,
                source_evidence=source_evidence_by_ticker.get(ticker, {}),
                source_requests=source_requests,
                outcome=outcome,
            )
        )
        if str(outcome.get("outcome_status") or "") in CONCLUSIVE_STATUSES:
            outcomes.append(outcome)

    if persist and outcomes:
        atomic_stats = store.persist_signal_outcomes_with_events(
            outcomes,
            [_outcome_event(row) for row in outcomes],
            replace=replace,
        )
        persisted = atomic_stats["outcomes"]
        new_event_stats = atomic_stats["events"]
        audit_event_stats.update({
            "inserted": audit_event_stats["inserted"] + new_event_stats["inserted"],
            "skipped": audit_event_stats["skipped"] + new_event_stats["skipped"],
            "new_inserted": new_event_stats["inserted"],
            "new_skipped": new_event_stats["skipped"],
        })
    else:
        persisted = {"inserted": 0, "skipped": 0}
    attempt_persisted = (
        store.persist_outcome_capture_attempts(capture_attempts)
        if persist and capture_attempts
        else {"inserted": 0, "skipped": 0, "row_count": len(capture_attempts)}
    )
    unresolved_count = sum(
        1
        for row in capture_attempts
        if str(row.get("status") or "") == "terminal_missing"
    )
    status = "partial" if unresolved_count else "complete"
    summary = _summary(
        status=status,
        market_date=resolved_date,
        requested_at=at,
        captured_at=captured_at,
        signal_count=len(signals),
        pending_count=len(pending),
        diagnostics=diagnostics,
        persisted=persisted,
        source_requests=source_requests,
        market_session=session.calendar,
        audit_events=audit_event_stats,
        capture_attempts=capture_attempts,
        capture_attempt_persistence=attempt_persisted,
    )
    _write_artifacts(
        output_dir,
        summary,
        outcomes,
        diagnostics,
        source_bars,
        capture_attempts,
    )
    return {
        **summary,
        "outcomes": outcomes,
        "diagnostics": diagnostics,
        "capture_attempt_rows": capture_attempts,
        "out_dir": str(output_dir),
    }


def _outcome_targets(
    rows: list[dict[str, Any]],
    *,
    selected_signal_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen_signal_ids: set[str] = set()
    for row in sorted(rows, key=lambda item: str(item.get("generated_at") or ""), reverse=True):
        ticker = str(row.get("ticker") or "").upper().strip()
        signal_id = str(row.get("signal_id") or "").strip()
        if (
            not ticker
            or not signal_id
            or signal_id in seen_signal_ids
            or (
                selected_signal_ids is not None
                and signal_id not in selected_signal_ids
            )
            or not signal_requires_outcome(row)
        ):
            continue
        seen_signal_ids.add(signal_id)
        output.append(dict(row))
    return sorted(
        output,
        key=lambda row: (
            _int(row.get("rank"), 999_999),
            str(row.get("ticker") or ""),
            str(row.get("generated_at") or ""),
            str(row.get("signal_id") or ""),
        ),
    )


def _validate_exact_session_selections(
    rows: list[dict[str, Any]],
    *,
    market_date: str,
) -> None:
    required = (
        "selection_id",
        "signal_id",
        "ticker",
        "strategy_id",
        "strategy_version",
        "cohort",
        "decision",
        "selected_at",
        "event_key",
        "body_sha256",
    )
    for row in rows:
        missing = [name for name in required if not str(row.get(name) or "").strip()]
        if missing or str(row.get("selected_at") or "")[:10] != market_date:
            detail = ", ".join(missing) if missing else "wrong selected_at date"
            raise SnapshotValidationError(
                "AlphaOps session selection evidence is partially persisted: " + detail
            )


def _fetch_outcome_bars(
    ticker: str,
    config: ScannerConfig,
    *,
    session: SessionWindow,
    requested_at: datetime,
    captured_at: str,
    chart_fetcher: FetchChart,
    fallback_fetcher: FetchNormalizedBars | None,
    attempt_limit: int,
) -> tuple[
    list[OutcomeBar],
    dict[str, Any],
    list[dict[str, Any]],
    str,
]:
    """Resolve one complete source, retaining every bounded provider attempt."""

    requests: list[dict[str, Any]] = []
    candidates: list[tuple[list[OutcomeBar], dict[str, Any]]] = []
    errors: list[str] = []
    yahoo_url = yahoo_chart_url(
        ticker,
        range_name=YAHOO_RANGE,
        interval=BAR_INTERVAL,
        include_pre_post=False,
    )
    for attempt in range(1, attempt_limit + 1):
        try:
            payload = chart_fetcher(
                ticker,
                config,
                range_name=YAHOO_RANGE,
                interval=BAR_INTERVAL,
                include_pre_post=False,
            )
            bars = _regular_session_bars(
                ticker,
                payload,
                session=session,
                requested_at=requested_at,
            )
            request = _provider_request(
                ticker=ticker,
                source=YAHOO_SOURCE_NAME,
                source_url=yahoo_url,
                bars=bars,
                session=session,
                fetched_at=captured_at,
                attempt=attempt,
            )
            requests.append(request)
            candidates.append((bars, request))
            if request["source_coverage_complete"] is True:
                return bars, _selected_source_evidence(request, requests), requests, ""
        except (DataProviderError, ValueError, TypeError) as exc:
            detail = f"{YAHOO_SOURCE_NAME} attempt {attempt}: {exc}"
            errors.append(detail)
            requests.append({
                "ticker": ticker,
                "status": "provider_error",
                "source": YAHOO_SOURCE_NAME,
                "source_url": yahoo_url,
                "attempt": attempt,
                "attempt_limit": attempt_limit,
                "fetched_at": captured_at,
                "bar_count": 0,
                "error": str(exc),
            })

    configured_fallback = fallback_fetcher
    if configured_fallback is None and config.alpaca_api_key_id and config.alpaca_api_secret_key:
        configured_fallback = _fetch_alpaca_rows
    alpaca_source = f"alpaca_market_data_{config.alpaca_data_feed}"
    alpaca_url = "https://data.alpaca.markets/v2/stocks/bars"
    if configured_fallback is None:
        requests.append({
            "ticker": ticker,
            "status": "not_configured",
            "source": alpaca_source,
            "source_url": alpaca_url,
            "attempt": 0,
            "attempt_limit": attempt_limit,
            "fetched_at": captured_at,
            "bar_count": 0,
            "error": "Read-only Alpaca market-data credentials are not configured.",
        })
    else:
        for attempt in range(1, attempt_limit + 1):
            try:
                rows = configured_fallback(
                    ticker,
                    config,
                    start=_iso_utc(session.opened_at),
                    end=_iso_utc(session.closed_at),
                )
                bars = _regular_session_bars_from_rows(
                    ticker,
                    rows,
                    session=session,
                    requested_at=requested_at,
                )
                request = _provider_request(
                    ticker=ticker,
                    source=alpaca_source,
                    source_url=alpaca_url,
                    bars=bars,
                    session=session,
                    fetched_at=captured_at,
                    attempt=attempt,
                )
                requests.append(request)
                candidates.append((bars, request))
                if request["source_coverage_complete"] is True:
                    return bars, _selected_source_evidence(request, requests), requests, ""
            except (DataProviderError, ValueError, TypeError) as exc:
                detail = f"{alpaca_source} attempt {attempt}: {exc}"
                errors.append(detail)
                requests.append({
                    "ticker": ticker,
                    "status": "provider_error",
                    "source": alpaca_source,
                    "source_url": alpaca_url,
                    "attempt": attempt,
                    "attempt_limit": attempt_limit,
                    "fetched_at": captured_at,
                    "bar_count": 0,
                    "error": str(exc),
                })

    if candidates:
        bars, request = max(candidates, key=lambda item: _source_choice_key(item[0]))
        return (
            bars,
            _selected_source_evidence(request, requests),
            requests,
            "; ".join(errors),
        )
    evidence = {
        "source": "",
        "source_url": "",
        "source_fetched_at": captured_at,
        "source_bar_hash_sha256": _bars_hash([]),
        "source_lineage": requests,
        "provider_chain_exhausted": True,
    }
    return [], evidence, requests, "; ".join(errors) or "No provider returned bars."


def _fetch_alpaca_rows(
    ticker: str,
    config: ScannerConfig,
    *,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Use Alpaca's read-only market-data endpoint; no trading API is imported."""

    return AlpacaProvider(config).get_minute_bars([ticker], start, end, config)


def _provider_request(
    *,
    ticker: str,
    source: str,
    source_url: str,
    bars: list[OutcomeBar],
    session: SessionWindow,
    fetched_at: str,
    attempt: int,
) -> dict[str, Any]:
    coverage = _validate_bar_coverage(
        bars,
        expected_start_at=session.opened_at,
        expected_end_at=session.closed_at - timedelta(minutes=1),
    )
    return {
        "ticker": ticker,
        "status": "ok" if coverage.is_complete else coverage.status,
        "source": source,
        "source_url": source_url,
        "attempt": attempt,
        "fetched_at": fetched_at,
        "bar_count": len(bars),
        "first_bar_at": _iso_utc(bars[0].observed_at) if bars else None,
        "last_bar_at": _iso_utc(bars[-1].observed_at) if bars else None,
        "source_bar_hash_sha256": _bars_hash(bars),
        "source_coverage_complete": coverage.is_complete,
        "coverage_status": coverage.status,
        "coverage_detail": coverage.detail,
    }


def _selected_source_evidence(
    selected: dict[str, Any],
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "source": selected.get("source"),
        "source_url": selected.get("source_url"),
        "source_fetched_at": selected.get("fetched_at"),
        "source_bar_hash_sha256": selected.get("source_bar_hash_sha256"),
        "source_coverage_complete": selected.get("source_coverage_complete"),
        "source_lineage": [dict(row) for row in requests],
        "provider_chain_exhausted": selected.get("source_coverage_complete") is not True,
    }


def _source_choice_key(bars: list[OutcomeBar]) -> tuple[int, int, int]:
    return (
        len(bars),
        sum(_bar_completeness(bar) for bar in bars),
        int(bars[-1].observed_at.timestamp()) if bars else 0,
    )


def _derive_outcome(
    signal: dict[str, Any],
    bars: list[OutcomeBar],
    *,
    session: SessionWindow,
    requested_at: datetime,
    captured_at: str,
    max_close_staleness_seconds: int,
    strategy_id: str,
    source_evidence: dict[str, Any],
    benchmark_bars: list[OutcomeBar],
    benchmark_evidence: dict[str, Any],
    entry_intent: dict[str, Any] | None,
    paper_fills: list[dict[str, Any]],
) -> dict[str, Any]:
    base = _outcome_base(
        signal,
        bars,
        session=session,
        requested_at=requested_at,
        captured_at=captured_at,
        strategy_id=strategy_id,
        source_evidence=source_evidence,
    )
    if not bars:
        return _ineligible(base, "ineligible_no_regular_session_bars")
    trigger = _first_float(
        signal.get("entry_watch_level"),
        _raw_signal(signal).get("entry_trigger"),
        _raw_signal(signal).get("breakout_trigger"),
    )
    if trigger is None or trigger <= 0:
        return _ineligible(base, "ineligible_missing_entry_trigger")
    recommendation_at = _parse_datetime(str(signal.get("generated_at") or ""))
    if recommendation_at is None:
        return _ineligible(base, "ineligible_missing_recommendation_timestamp")
    first_eligible_at = max(session.opened_at, _ceil_minute(recommendation_at))
    eligible_bars = [bar for bar in bars if bar.observed_at >= first_eligible_at]
    if not eligible_bars:
        return _ineligible(base, "ineligible_no_post_recommendation_bars")
    coverage = _validate_bar_coverage(
        eligible_bars,
        expected_start_at=first_eligible_at,
        expected_end_at=session.closed_at - timedelta(minutes=1),
    )
    base.update(coverage.to_dict())
    if not coverage.is_complete:
        return _ineligible(base, coverage.status, coverage.detail)
    if any(bar.open is None or bar.high is None or bar.low is None for bar in eligible_bars):
        return _ineligible(base, "ineligible_incomplete_source_bars")
    malformed_ohlc = _malformed_ohlc_detail(eligible_bars)
    if malformed_ohlc:
        return _ineligible(base, "ineligible_malformed_ohlc", malformed_ohlc)
    close_age = max(
        0,
        int((session.closed_at - eligible_bars[-1].observed_at).total_seconds()),
    )
    base["close_observation_age_seconds"] = close_age
    if close_age > max_close_staleness_seconds:
        return _ineligible(
            base,
            "ineligible_stale_close",
            f"last regular bar was {close_age} seconds before the session close",
        )
    trigger_bar = next(
        (bar for bar in eligible_bars if bar.high is not None and bar.high >= trigger),
        None,
    )
    if trigger_bar is None:
        return _conclusive_without_entry(
            base,
            status="not_triggered",
            trigger=trigger,
            note="Verified regular-session highs never reached the saved entry trigger.",
        )
    if trigger_bar.open is None:
        return _ineligible(base, "ineligible_missing_trigger_bar_open")
    entry_price = max(trigger, trigger_bar.open)
    target = _first_float(signal.get("target_1"), _raw_signal(signal).get("first_target"))
    invalidation = _first_float(
        signal.get("invalidation_level"),
        signal.get("exit_line"),
        _raw_signal(signal).get("invalidation_level"),
    )
    if target is None or invalidation is None:
        return _captured_ineligible_plan(
            base,
            status="captured_ineligible_missing_plan",
            trigger=trigger,
            entry_price=entry_price,
            entry_bar=trigger_bar,
            note="Source outcome captured, but target or invalidation was missing from the plan.",
        )
    if target <= entry_price or invalidation >= entry_price:
        return _captured_ineligible_plan(
            base,
            status="not_entered_plan_dislocated",
            trigger=trigger,
            entry_price=entry_price,
            entry_bar=trigger_bar,
            note="Gap-through pricing made the saved target/invalidation geometry invalid.",
        )
    post_entry = [bar for bar in eligible_bars if bar.observed_at >= trigger_bar.observed_at]
    if not post_entry:
        return _ineligible(base, "ineligible_no_post_entry_bars")
    high_bar = max(
        (bar for bar in post_entry if bar.high is not None),
        key=lambda bar: float(bar.high or 0.0),
    )
    low_bar = min(
        (bar for bar in post_entry if bar.low is not None),
        key=lambda bar: float(bar.low or math.inf),
    )
    high = float(high_bar.high or 0.0)
    low = float(low_bar.low or 0.0)
    price_1m, price_1m_at = _horizon_price(post_entry, trigger_bar.observed_at, 1)
    price_5m, price_5m_at = _horizon_price(post_entry, trigger_bar.observed_at, 5)
    price_15m, price_15m_at = _horizon_price(post_entry, trigger_bar.observed_at, 15)
    lunch_price, lunch_at = _lunch_price(post_entry, trigger_bar.observed_at, session)
    target_at = _first_touch_at(post_entry, "high", target)
    invalidation_at = _first_touch_at(post_entry, "low", invalidation, at_or_below=True)
    first_touch = _planned_first_touch(
        entry_at=trigger_bar.observed_at,
        target_at=target_at,
        invalidation_at=invalidation_at,
    )
    raw_exit_price, exit_at, exit_reason = _resolved_exit(
        first_touch=first_touch,
        target=target,
        target_at=target_at,
        invalidation=invalidation,
        invalidation_at=invalidation_at,
        final_bar=post_entry[-1],
    )
    benchmark_return = _benchmark_return(
        benchmark_bars,
        entry_at=trigger_bar.observed_at,
        exit_at=exit_at,
    )
    raw_return = _return_pct(raw_exit_price, entry_price)
    context = _outcome_context(signal)
    execution = _execution_outcome_fields(
        strategy_id=strategy_id,
        trigger=trigger,
        raw_entry=entry_price,
        raw_exit=raw_exit_price,
        entry_intent=entry_intent,
        paper_fills=paper_fills,
    )
    learning_eligible = bool(
        benchmark_return is not None
        and (
            strategy_id != ALPHAOPS_V5_STRATEGY_ID
            or entry_intent is not None
        )
    )
    base.update({
        "entry_opportunity": True,
        "entry_time": _iso_utc(trigger_bar.observed_at),
        "entry_price": entry_price,
        "entry_trigger": trigger,
        "entry_price_policy": "bar_open_if_gap_through_else_saved_trigger",
        "price_1m": price_1m,
        "price_1m_observed_at": price_1m_at,
        "price_5m": price_5m,
        "price_5m_observed_at": price_5m_at,
        "price_15m": price_15m,
        "price_15m_observed_at": price_15m_at,
        "lunch_price": lunch_price,
        "lunch_price_observed_at": lunch_at,
        "close_price": post_entry[-1].close,
        "close_price_observed_at": _iso_utc(post_entry[-1].observed_at),
        "high_after_entry": high,
        "high_after_entry_observed_at": _iso_utc(high_bar.observed_at),
        "low_after_entry": low,
        "low_after_entry_observed_at": _iso_utc(low_bar.observed_at),
        "max_favorable_excursion_pct": _return_pct(high, entry_price),
        "max_adverse_excursion_pct": _return_pct(low, entry_price),
        "time_to_mfe_minutes": _elapsed_minutes(
            trigger_bar.observed_at, high_bar.observed_at
        ),
        "time_to_mae_minutes": _elapsed_minutes(
            trigger_bar.observed_at, low_bar.observed_at
        ),
        "target_price": target,
        "invalidation_price": invalidation,
        "target_touched_at": _iso_utc(target_at) if target_at else None,
        "invalidation_touched_at": _iso_utc(invalidation_at) if invalidation_at else None,
        "planned_first_touch_outcome": first_touch,
        "exit_reason": exit_reason,
        "exit_time": _iso_utc(exit_at),
        "exit_price": raw_exit_price,
        "holding_duration_minutes": _elapsed_minutes(
            trigger_bar.observed_at, exit_at
        ),
        "gross_return_pct": raw_return,
        "benchmark_symbol": "SPY",
        "benchmark_return_pct": benchmark_return,
        "excess_return_pct": (
            round(raw_return - benchmark_return, 4)
            if raw_return is not None and benchmark_return is not None
            else None
        ),
        "benchmark_source": benchmark_evidence.get("source"),
        "benchmark_source_url": benchmark_evidence.get("source_url"),
        "benchmark_source_bar_hash_sha256": benchmark_evidence.get(
            "source_bar_hash_sha256"
        ),
        "attribution_complete": benchmark_return is not None,
        "first_touch_precision": BAR_INTERVAL,
        "outcome_status": "complete_sourced",
        "learning_eligible": learning_eligible,
        "learning_contract": (
            "candidate_outcome_requires_reconciled_trade_label"
        ),
        "validated_against_signal_timestamp": True,
        **context,
        **execution,
        "notes": (
            "Automatic read-only multi-provider EOD observation; one-minute bars; "
            "same-bar target/stop ambiguity is counted conservatively as invalidation. "
            "Production return learning still requires an exact reconciled trade label."
        ),
    })
    base["payload_json"] = dict(base)
    return base


def _outcome_base(
    signal: dict[str, Any],
    bars: list[OutcomeBar],
    *,
    session: SessionWindow,
    requested_at: datetime,
    captured_at: str,
    strategy_id: str,
    source_evidence: dict[str, Any],
) -> dict[str, Any]:
    ticker = str(signal.get("ticker") or "").upper()
    source = str(source_evidence.get("source") or YAHOO_SOURCE_NAME)
    source_url = str(
        source_evidence.get("source_url")
        or yahoo_chart_url(
            ticker,
            range_name=YAHOO_RANGE,
            interval=BAR_INTERVAL,
            include_pre_post=False,
        )
    )
    return {
        "signal_id": str(signal.get("signal_id") or ""),
        "scan_id": str(signal.get("scan_id") or ""),
        "alpha_signal_id": str(signal.get("alpha_signal_id") or ""),
        "market_date": session.market_date,
        "date": session.market_date,
        "ticker": ticker,
        "rank": signal.get("rank"),
        "strategy_id": strategy_id,
        "recommendation_timestamp": str(signal.get("generated_at") or ""),
        "outcome_source": source,
        "source": source,
        "source_url": source_url,
        "source_range": YAHOO_RANGE,
        "source_bar_interval": BAR_INTERVAL,
        "source_bar_count": len(bars),
        "source_first_bar_at": _iso_utc(bars[0].observed_at) if bars else None,
        "source_last_bar_at": _iso_utc(bars[-1].observed_at) if bars else None,
        "source_bar_hash_sha256": str(
            source_evidence.get("source_bar_hash_sha256") or _bars_hash(bars)
        ),
        "source_fetched_at": source_evidence.get("source_fetched_at"),
        "source_lineage": source_evidence.get("source_lineage") or [],
        "provider_chain_exhausted": bool(
            source_evidence.get("provider_chain_exhausted")
        ),
        "requested_at": _iso_utc(requested_at),
        "imported_at": captured_at,
        "captured_at": captured_at,
        "capture_model_version": CAPTURE_MODEL_VERSION,
        "capture_mode": "automatic_sourced_eod",
        "automatic_sourced_data": True,
        "manual_uploaded_data": False,
        "paid_data": False,
        "no_lookahead": True,
        "research_only": True,
        "broker_execution_enabled": False,
        "halted": None,
    }


def _conclusive_without_entry(
    base: dict[str, Any],
    *,
    status: str,
    trigger: float,
    note: str,
) -> dict[str, Any]:
    base.update({
        "entry_trigger": trigger,
        "entry_opportunity": False,
        "fill_status": "not_filled_no_trigger",
        "non_fill_reason": status,
        "entry_time": None,
        "entry_price": None,
        "price_1m": None,
        "price_5m": None,
        "price_15m": None,
        "lunch_price": None,
        "close_price": None,
        "high_after_entry": None,
        "low_after_entry": None,
        "max_favorable_excursion_pct": None,
        "max_adverse_excursion_pct": None,
        "time_to_mfe_minutes": None,
        "time_to_mae_minutes": None,
        "exit_time": None,
        "exit_price": None,
        "exit_reason": None,
        "holding_duration_minutes": None,
        "gross_return_pct": None,
        "benchmark_return_pct": None,
        "excess_return_pct": None,
        "realized_slippage_cost": None,
        "modeled_fees": None,
        "outcome_status": status,
        "learning_eligible": False,
        "validated_against_signal_timestamp": True,
        "notes": note,
    })
    base["payload_json"] = dict(base)
    return base


def _captured_ineligible_plan(
    base: dict[str, Any],
    *,
    status: str,
    trigger: float,
    entry_price: float,
    entry_bar: OutcomeBar,
    note: str,
) -> dict[str, Any]:
    base.update({
        "entry_trigger": trigger,
        "entry_opportunity": True,
        "fill_status": "not_filled_invalid_plan",
        "non_fill_reason": status,
        "entry_time": _iso_utc(entry_bar.observed_at),
        "entry_price": entry_price,
        "price_1m": None,
        "price_5m": None,
        "price_15m": None,
        "lunch_price": None,
        "close_price": None,
        "high_after_entry": None,
        "low_after_entry": None,
        "max_favorable_excursion_pct": None,
        "max_adverse_excursion_pct": None,
        "time_to_mfe_minutes": None,
        "time_to_mae_minutes": None,
        "exit_time": None,
        "exit_price": None,
        "exit_reason": None,
        "holding_duration_minutes": None,
        "gross_return_pct": None,
        "benchmark_return_pct": None,
        "excess_return_pct": None,
        "realized_slippage_cost": None,
        "modeled_fees": None,
        "outcome_status": status,
        "learning_eligible": False,
        "validated_against_signal_timestamp": True,
        "notes": note,
    })
    base["payload_json"] = dict(base)
    return base


def _ineligible(base: dict[str, Any], status: str, detail: str = "") -> dict[str, Any]:
    base.update({
        "outcome_status": status,
        "learning_eligible": False,
        "validated_against_signal_timestamp": False,
        "notes": detail or status.replace("_", " "),
    })
    return base


def _regular_session_bars(
    ticker: str,
    payload: dict[str, Any],
    *,
    session: SessionWindow,
    requested_at: datetime,
) -> list[OutcomeBar]:
    return _regular_session_bars_from_rows(
        ticker,
        bars_from_yahoo_chart_payload(ticker, payload),
        session=session,
        requested_at=requested_at,
    )


def _regular_session_bars_from_rows(
    ticker: str,
    rows: list[dict[str, Any]],
    *,
    session: SessionWindow,
    requested_at: datetime,
) -> list[OutcomeBar]:
    by_time: dict[datetime, OutcomeBar] = {}
    for row in rows:
        row_ticker = str(row.get("ticker") or ticker).upper()
        if row_ticker != ticker.upper():
            continue
        observed_at = _timestamp(row.get("timestamp"))
        close = _float(row.get("close"))
        if observed_at is None or close is None:
            continue
        if not (session.opened_at <= observed_at < session.closed_at):
            continue
        if observed_at > requested_at:
            continue
        candidate = OutcomeBar(
            observed_at=observed_at,
            open=_float(row.get("open")),
            high=_float(row.get("high")),
            low=_float(row.get("low")),
            close=close,
            volume=_float(row.get("volume")),
        )
        prior = by_time.get(observed_at)
        if prior is None or _bar_completeness(candidate) > _bar_completeness(prior):
            by_time[observed_at] = candidate
    return [by_time[key] for key in sorted(by_time)]


def _planned_first_touch(
    *,
    entry_at: datetime,
    target_at: datetime | None,
    invalidation_at: datetime | None,
) -> str:
    if target_at is not None and invalidation_at is not None:
        if target_at == invalidation_at:
            return "ambiguous_same_bar_counted_as_invalidation"
        return "target_1" if target_at < invalidation_at else "invalidation"
    if invalidation_at is not None:
        if invalidation_at == entry_at:
            return "ambiguous_entry_bar_counted_as_invalidation"
        return "invalidation"
    if target_at is not None:
        return "target_1"
    return "close"


def _resolved_exit(
    *,
    first_touch: str,
    target: float,
    target_at: datetime | None,
    invalidation: float,
    invalidation_at: datetime | None,
    final_bar: OutcomeBar,
) -> tuple[float, datetime, str]:
    if first_touch == "target_1" and target_at is not None:
        return target, target_at, "target_1"
    if (
        first_touch == "invalidation" or first_touch.startswith("ambiguous_")
    ) and invalidation_at is not None:
        return invalidation, invalidation_at, first_touch
    return final_bar.close, final_bar.observed_at, "eod_close"


def _benchmark_return(
    bars: list[OutcomeBar],
    *,
    entry_at: datetime,
    exit_at: datetime,
) -> float | None:
    entry_bar = _bar_at_or_after(bars, entry_at)
    exit_bar = _bar_at_or_after(bars, exit_at)
    if entry_bar is None or exit_bar is None or entry_bar.close <= 0:
        return None
    return round(((exit_bar.close - entry_bar.close) / entry_bar.close) * 100.0, 4)


def _bar_at_or_after(
    bars: list[OutcomeBar],
    value: datetime,
) -> OutcomeBar | None:
    return next(
        (
            bar
            for bar in bars
            if value <= bar.observed_at <= value + timedelta(seconds=90)
        ),
        None,
    )


def _elapsed_minutes(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


def _outcome_context(signal: dict[str, Any]) -> dict[str, Any]:
    raw = _raw_signal(signal)
    gap = _first_float(signal.get("gap_pct"), raw.get("gap_pct"))
    float_shares = _first_float(signal.get("float_shares"), raw.get("float_shares"))
    dollar_volume = _first_float(
        signal.get("dollar_volume"),
        raw.get("dollar_volume"),
        raw.get("premarket_dollar_volume"),
    )
    confidence = _first_float(
        signal.get("source_confidence"),
        raw.get("source_confidence"),
        raw.get("data_confidence"),
    )
    catalyst = str(
        signal.get("catalyst_category")
        or raw.get("catalyst_category")
        or raw.get("catalyst_class")
        or ""
    ).strip()
    catalyst_summary = str(
        signal.get("catalyst_summary") or raw.get("catalyst_summary") or ""
    ).strip()
    return {
        "gap_pct": gap,
        "gap_bucket": _gap_bucket(gap),
        "catalyst_class": catalyst or (
            "sourced_unspecified" if catalyst_summary else "missing"
        ),
        "float_shares": float_shares,
        "float_bucket": _float_bucket(float_shares),
        "premarket_dollar_volume": dollar_volume,
        "liquidity_bucket": _liquidity_bucket(dollar_volume),
        "market_regime": (
            raw.get("market_regime")
            or raw.get("regime")
            or signal.get("market_regime")
            or "unknown"
        ),
        "sector_regime": (
            raw.get("sector_regime")
            or signal.get("sector_regime")
            or "unknown"
        ),
        "source_confidence": confidence,
        "source_confidence_bucket": _confidence_bucket(confidence),
        "vetoes": _tokens(
            signal.get("avoid_reasons_json") or raw.get("avoid_reasons")
        ),
        "risk_flags": _tokens(
            signal.get("risk_flags_json") or raw.get("risk_flags")
        ),
    }


def _execution_outcome_fields(
    *,
    strategy_id: str,
    trigger: float,
    raw_entry: float,
    raw_exit: float,
    entry_intent: dict[str, Any] | None,
    paper_fills: list[dict[str, Any]],
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "fill_status": "modeled_legacy_trigger",
        "non_fill_reason": None,
        "official_entry_intent_id": None,
        "paper_quantity": None,
        "paper_entry_fill_price": None,
        "paper_exit_fill_price": None,
        "modeled_fees": None,
        "modeled_slippage_cost": None,
        "realized_slippage_cost": None,
        "execution_policy_version": None,
        "cost_model_version": None,
        "decision_fingerprint": None,
    }
    if strategy_id != ALPHAOPS_V5_STRATEGY_ID:
        return base
    base.update({
        "fill_status": "not_filled_official_policy",
        "non_fill_reason": "no_eligible_enter_long_intent",
        "execution_policy_version": DEFAULT_V5_POLICY.policy_version,
        "cost_model_version": DEFAULT_V5_POLICY.cost_model_version,
    })
    if entry_intent is None:
        return base
    trace = dict(entry_intent.get("decision_trace") or {})
    computed = dict(trace.get("computed") or {})
    quantity = _float(entry_intent.get("quantity"))
    expected_entry = _first_float(
        computed.get("expected_entry_price"),
        entry_intent.get("decision_price"),
    )
    expected_exit = raw_exit * (
        1.0 - DEFAULT_V5_POLICY.exit_slippage_bps / 10_000.0
    )
    modeled_fees = (
        quantity * DEFAULT_V5_POLICY.commission_per_share_per_side * 2
        if quantity is not None
        else None
    )
    modeled_slippage = (
        (
            abs((expected_entry or raw_entry) - trigger)
            + abs(raw_exit - expected_exit)
        )
        * quantity
        if quantity is not None
        else None
    )
    ordered_fills = sorted(
        paper_fills,
        key=lambda row: str(row.get("fill_time") or ""),
    )
    entry_fill = _float(ordered_fills[0].get("fill_price")) if ordered_fills else None
    exit_fill = _float(ordered_fills[-1].get("fill_price")) if len(ordered_fills) > 1 else None
    realized_slippage = (
        (abs(entry_fill - raw_entry) + abs(exit_fill - raw_exit)) * quantity
        if (
            entry_fill is not None
            and exit_fill is not None
            and quantity is not None
        )
        else None
    )
    base.update({
        "fill_status": (
            "paper_filled_and_closed"
            if exit_fill is not None
            else "paper_entry_filled"
            if entry_fill is not None
            else "eligible_intent_pending_fill"
        ),
        "non_fill_reason": None,
        "official_entry_intent_id": entry_intent.get("intent_id"),
        "paper_quantity": quantity,
        "paper_entry_fill_price": entry_fill or expected_entry,
        "paper_exit_fill_price": exit_fill,
        "modeled_fees": round(modeled_fees, 4) if modeled_fees is not None else None,
        "modeled_slippage_cost": (
            round(modeled_slippage, 4) if modeled_slippage is not None else None
        ),
        "realized_slippage_cost": (
            round(realized_slippage, 4)
            if realized_slippage is not None
            else None
        ),
        "execution_policy_version": (
            entry_intent.get("execution_policy_version")
            or DEFAULT_V5_POLICY.policy_version
        ),
        "cost_model_version": (
            entry_intent.get("cost_model_version")
            or DEFAULT_V5_POLICY.cost_model_version
        ),
        "decision_fingerprint": (
            entry_intent.get("decision_fingerprint")
            or trace.get("decision_fingerprint")
        ),
    })
    return base


def _gap_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 15:
        return "under_15"
    if value <= 30:
        return "15_to_30"
    if value <= 50:
        return "30_to_50"
    return "over_50"


def _float_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 5_000_000:
        return "under_5m"
    if value < 20_000_000:
        return "5m_to_20m"
    return "over_20m"


def _liquidity_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 1_000_000:
        return "under_1m"
    if value < 5_000_000:
        return "1m_to_5m"
    return "over_5m"


def _confidence_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 50:
        return "under_50"
    if value < 80:
        return "50_to_79"
    return "80_plus"


def _return_pct(exit_price: float | None, entry_price: float | None) -> float | None:
    if exit_price is None or entry_price is None or entry_price <= 0:
        return None
    return round(((exit_price - entry_price) / entry_price) * 100.0, 4)


def _first_touch_at(
    bars: list[OutcomeBar],
    field: str,
    level: float,
    *,
    at_or_below: bool = False,
) -> datetime | None:
    for bar in bars:
        value = getattr(bar, field)
        if value is None:
            continue
        if (value <= level) if at_or_below else (value >= level):
            return bar.observed_at
    return None


def _horizon_price(
    bars: list[OutcomeBar],
    entry_at: datetime,
    minutes: int,
) -> tuple[float | None, str | None]:
    target_at = entry_at + timedelta(minutes=minutes)
    bar = next(
        (
            item
            for item in bars
            if target_at <= item.observed_at <= target_at + timedelta(seconds=90)
        ),
        None,
    )
    return (bar.close, _iso_utc(bar.observed_at)) if bar else (None, None)


def _lunch_price(
    bars: list[OutcomeBar],
    entry_at: datetime,
    session: SessionWindow,
) -> tuple[float | None, str | None]:
    lunch_at = datetime.combine(
        date.fromisoformat(session.market_date),
        time(12, 0),
        tzinfo=EASTERN,
    )
    if entry_at >= lunch_at:
        return None, None
    bar = next(
        (
            item
            for item in bars
            if lunch_at <= item.observed_at <= lunch_at + timedelta(seconds=90)
        ),
        None,
    )
    return (bar.close, _iso_utc(bar.observed_at)) if bar else (None, None)


def _capture_attempt(
    signal: dict[str, Any],
    diagnostic: dict[str, Any],
    *,
    market_date: str,
    captured_at: str,
    requested_at: datetime,
    source_evidence: dict[str, Any],
    source_requests: list[dict[str, Any]],
    outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signal_id = str(signal.get("signal_id") or "")
    ticker = str(signal.get("ticker") or "").upper()
    outcome_status = str(diagnostic.get("status") or "ineligible_unknown")
    source_hash = str(
        (outcome or {}).get("source_bar_hash_sha256")
        or source_evidence.get("source_bar_hash_sha256")
        or ""
    )
    attribution_missing = bool(
        outcome is not None and outcome.get("attribution_complete") is False
    )
    terminal_missing = outcome_status.startswith("ineligible_") or attribution_missing
    status = "terminal_missing" if terminal_missing else "resolved"
    relevant_requests = [
        dict(row)
        for row in source_requests
        if str(row.get("ticker") or "").upper() == ticker
    ]
    identity = ":".join((
        market_date,
        signal_id,
        _iso_utc(requested_at),
        outcome_status,
        source_hash,
    ))
    attempt_id = "outcome-attempt:" + hashlib.sha256(identity.encode()).hexdigest()
    source_refs = [
        {
            "source": row.get("source"),
            "source_url": row.get("source_url"),
            "fetched_at": row.get("fetched_at"),
            "status": row.get("status"),
            "source_bar_hash_sha256": row.get("source_bar_hash_sha256"),
        }
        for row in relevant_requests
    ]
    payload = {
        "attempt_id": attempt_id,
        "run_id": f"dawnstrike:{market_date}",
        "signal_id": signal_id,
        "market_date": market_date,
        "ticker": ticker,
        "status": status,
        "terminal": True,
        "learning_eligible": bool((outcome or {}).get("learning_eligible")),
        "provider_chain": relevant_requests,
        "source_refs": source_refs,
        "source_bar_hash_sha256": source_hash,
        "attempted_at": captured_at,
        "resolved_at": captured_at,
        "error_code": (
            "ineligible_missing_benchmark"
            if attribution_missing
            else outcome_status
            if terminal_missing
            else ""
        ),
        "error_detail": (
            "Complete candidate bars were captured, but sourced SPY benchmark "
            "alignment was unavailable."
            if attribution_missing
            else str(diagnostic.get("detail") or "")
        ),
        "outcome_status": outcome_status,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["payload_json"] = dict(payload)
    return payload


def _summary(
    *,
    status: str,
    market_date: str,
    requested_at: datetime,
    captured_at: str,
    signal_count: int,
    pending_count: int,
    diagnostics: list[dict[str, Any]],
    persisted: dict[str, int],
    source_requests: list[dict[str, Any]],
    market_session: dict[str, object],
    audit_events: dict[str, int],
    capture_attempts: list[dict[str, Any]] | None = None,
    capture_attempt_persistence: dict[str, int] | None = None,
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in diagnostics:
        key = str(row.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    attempts = capture_attempts or []
    terminal_missing_count = sum(
        1 for row in attempts if str(row.get("status") or "") == "terminal_missing"
    )
    operator_alert = (
        {
            "severity": "error",
            "event_type": "required_eod_outcome_capture_failed",
            "market_date": market_date,
            "terminal_missing_count": terminal_missing_count,
            "message": (
                f"{terminal_missing_count} required outcome capture(s) exhausted "
                "their bounded provider chain. Readiness must remain degraded."
            ),
        }
        if terminal_missing_count
        else None
    )
    return {
        "status": status,
        "market_date": market_date,
        "requested_at": _iso_utc(requested_at),
        "captured_at": captured_at,
        "capture_model_version": CAPTURE_MODEL_VERSION,
        "signal_count": signal_count,
        "pending_count": pending_count,
        "learning_eligible_count": sum(
            1 for row in diagnostics if row.get("learning_eligible") is True
        ),
        "not_triggered_count": counts.get("not_triggered", 0),
        "ineligible_count": sum(
            count for key, count in counts.items() if key.startswith("ineligible_")
        ),
        "outcome_status_counts": counts,
        "persisted": persisted,
        "source_requests": source_requests,
        "market_session": market_session,
        "audit_events": audit_events,
        "capture_attempts": {
            "row_count": len(attempts),
            "terminal_missing_count": terminal_missing_count,
            "persistence": capture_attempt_persistence
            or {"inserted": 0, "skipped": 0, "row_count": len(attempts)},
        },
        "required_stage_failed": terminal_missing_count > 0,
        "operator_alert": operator_alert,
        "missing_values_are_zero": False,
        "no_lookahead": True,
        "research_only": True,
        "broker_execution_enabled": False,
        "limitations": [
            (
                "Yahoo and configured Alpaca market-data availability and one-minute "
                "bar coverage are external dependencies."
            ),
            "Ordering inside one-minute bars is unknowable and is resolved conservatively.",
            "Missing horizon bars remain null and are never converted to zero.",
            "Any missing eligible core-session minute makes the outcome ineligible.",
        ],
    }


def _write_artifacts(
    output_dir: Path,
    summary: dict[str, Any],
    outcomes: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    source_bars: dict[str, list[dict[str, Any]]],
    capture_attempts: list[dict[str, Any]] | None = None,
) -> None:
    _write_json(output_dir / "alpha_outcome_capture.json", summary)
    _write_json(output_dir / "alpha_sourced_outcomes.json", outcomes)
    _write_json(output_dir / "alpha_outcome_capture_diagnostics.json", diagnostics)
    _write_json(output_dir / "alpha_outcome_source_bars.json", source_bars)
    _write_json(
        output_dir / "alpha_outcome_capture_attempts.json",
        capture_attempts or [],
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _outcome_event(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("outcome_status") or "")
    event_type = "OUTCOME_CAPTURED" if status == "complete_sourced" else "OUTCOME_RESOLVED"
    signal_id = str(row.get("signal_id") or "")
    captured_at = str(row.get("captured_at") or row.get("imported_at") or "")
    evidence_revision = str(row.get("source_bar_hash_sha256") or "missing-hash")[:16]
    source = str(row.get("outcome_source") or row.get("source") or "unknown")
    return {
        "event_id": (
            f"{signal_id}:{event_type}:{row.get('market_date')}:"
            f"{source}:{status}:{evidence_revision}"
        ),
        "signal_id": signal_id,
        "event_type": event_type,
        "event_timestamp": captured_at,
        "event_price": row.get("close_price"),
        "source": source,
        "notes": status,
        "payload_json": {
            "outcome_status": status,
            "capture_model_version": row.get("capture_model_version"),
            "source_url": row.get("source_url"),
            "source_bar_hash_sha256": row.get("source_bar_hash_sha256"),
            "learning_eligible": row.get("learning_eligible"),
            "research_only": True,
        },
    }


def _existing_diagnostic(
    signal: dict[str, Any], outcome: dict[str, Any]
) -> dict[str, Any]:
    return {
        "signal_id": signal.get("signal_id"),
        "ticker": signal.get("ticker"),
        "status": "already_captured",
        "existing_outcome_status": outcome.get("outcome_status"),
        "learning_eligible": bool(outcome.get("learning_eligible")),
    }


def _diagnostic(signal: dict[str, Any], status: str, detail: str = "") -> dict[str, Any]:
    return {
        "signal_id": signal.get("signal_id"),
        "ticker": signal.get("ticker"),
        "status": status,
        "detail": detail,
        "learning_eligible": False,
    }


def _diagnostic_from_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_id": outcome.get("signal_id"),
        "ticker": outcome.get("ticker"),
        "status": outcome.get("outcome_status"),
        "detail": outcome.get("notes"),
        "learning_eligible": bool(outcome.get("learning_eligible")),
        "entry_time": outcome.get("entry_time"),
        "source_bar_hash_sha256": outcome.get("source_bar_hash_sha256"),
    }


def _session_window(value: str) -> SessionWindow:
    try:
        market_day = date.fromisoformat(value)
    except ValueError as exc:
        raise SnapshotValidationError(f"Invalid market date: {value}") from exc
    decision = market_session(market_day)
    opened = time.fromisoformat(decision.open_time_et or "00:00")
    closed = time.fromisoformat(decision.close_time_et or "00:00")
    return SessionWindow(
        market_date=value,
        opened_at=datetime.combine(market_day, opened, tzinfo=EASTERN),
        closed_at=datetime.combine(market_day, closed, tzinfo=EASTERN),
        is_trading_day=decision.is_trading_day,
        calendar=decision.to_dict(),
    )


def _raw_signal(signal: dict[str, Any]) -> dict[str, Any]:
    value = signal.get("raw_payload_json") or {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC).astimezone(EASTERN)
        return _parse_datetime(str(value))
    except (ValueError, OSError, OverflowError):
        return None


def _parse_datetime(value: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(EASTERN)
    except (OverflowError, ValueError):
        return None


def _validate_bar_coverage(
    bars: list[OutcomeBar],
    *,
    expected_start_at: datetime,
    expected_end_at: datetime,
) -> BarCoverage:
    expected_count = max(
        0,
        int((expected_end_at - expected_start_at).total_seconds() // 60) + 1,
    )
    gaps = [
        int((current.observed_at - prior.observed_at).total_seconds())
        for prior, current in zip(bars, bars[1:], strict=False)
    ]
    maximum_gap = max(gaps, default=0)

    def result(is_complete: bool, status: str, detail: str) -> BarCoverage:
        return BarCoverage(
            is_complete=is_complete,
            status=status,
            detail=detail,
            expected_start_at=expected_start_at,
            expected_end_at=expected_end_at,
            expected_minute_count=expected_count,
            observed_minute_count=len(bars),
            maximum_gap_seconds=maximum_gap,
        )

    if expected_start_at > expected_end_at:
        return result(
            False,
            "ineligible_no_complete_post_recommendation_window",
            "Recommendation occurred after the final eligible regular-session minute.",
        )
    if not bars or bars[0].observed_at != expected_start_at:
        observed = _iso_utc(bars[0].observed_at) if bars else "missing"
        return result(
            False,
            "ineligible_missing_start_bar",
            f"Expected first eligible bar at {_iso_utc(expected_start_at)}; observed {observed}.",
        )
    if bars[-1].observed_at != expected_end_at:
        return result(
            False,
            "ineligible_missing_final_bar",
            (
                f"Expected final regular-session bar at {_iso_utc(expected_end_at)}; "
                f"observed {_iso_utc(bars[-1].observed_at)}."
            ),
        )
    if maximum_gap > MAX_BAR_GAP_SECONDS:
        return result(
            False,
            "ineligible_bar_gap",
            (
                f"Observed a {maximum_gap}-second gap; contiguous one-minute source "
                f"coverage permits at most {MAX_BAR_GAP_SECONDS} seconds."
            ),
        )
    if len(bars) != expected_count:
        return result(
            False,
            "ineligible_bar_count_mismatch",
            f"Expected {expected_count} minute bars; observed {len(bars)}.",
        )
    return result(True, "complete", "Contiguous eligible one-minute coverage verified.")


def _malformed_ohlc_detail(bars: list[OutcomeBar]) -> str:
    for bar in bars:
        values = (bar.open, bar.high, bar.low, bar.close)
        if any(value is None for value in values):
            continue
        open_price = float(bar.open) if bar.open is not None else 0.0
        high = float(bar.high) if bar.high is not None else 0.0
        low = float(bar.low) if bar.low is not None else 0.0
        close = float(bar.close)
        if not all(
            math.isfinite(value) and value > 0
            for value in (open_price, high, low, close)
        ):
            return f"Non-finite or non-positive OHLC at {_iso_utc(bar.observed_at)}."
        if not low <= min(open_price, close) <= max(open_price, close) <= high:
            return f"Invalid OHLC ordering at {_iso_utc(bar.observed_at)}."
    return ""


def _ceil_minute(value: datetime) -> datetime:
    normalized = value.astimezone(EASTERN)
    if normalized.second or normalized.microsecond:
        return normalized.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return normalized.replace(second=0, microsecond=0)


def _bars_hash(bars: list[OutcomeBar]) -> str:
    encoded = json.dumps(
        [bar.to_dict() for bar in bars],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bar_completeness(bar: OutcomeBar) -> int:
    return sum(value is not None for value in (bar.open, bar.high, bar.low, bar.volume))


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = _float(value)
        if parsed is not None:
            return parsed
    return None


def _float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tokens(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [
        token.strip()
        for token in text.replace(",", ";").split(";")
        if token.strip()
    ]
