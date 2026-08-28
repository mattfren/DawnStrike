"""Automatic, sourced AlphaOps paper-outcome capture.

This module observes public market bars after the regular session and derives
paper outcomes for saved research watches.  It never places orders or infers a
price where the source has no observation.  Only complete, timestamped Yahoo
bars can become learning-eligible outcomes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.alpha.canonical_return_truth import (
    CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED,
    CURRENT_CENSORED_PATH,
    CURRENT_RETURN_TRUTH,
    build_canonical_path_entry_receipt,
    build_canonical_return_truth,
    canonical_paper_enter_intent_context,
    canonical_paper_selection_context,
    canonical_replay_binding,
    canonical_return_truth_projection,
    classify_canonical_return_truth,
)
from intraday_scanner.alpha.path_replay import (
    ENTRY_MODE_ALREADY_ENTERED,
    PathTruthStatus,
    resolve_path,
)
from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_STRATEGY_ID,
    DEFAULT_V5_POLICY,
    alphaops_strategy_contract,
)
from intraday_scanner.alpha.v6_shadow import ALPHAOPS_V6_STRATEGY_VERSION
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
    yahoo_provider_symbol,
)
from intraday_scanner.services.alpha_official_cohort_service import (
    validate_or_recover_official_cohort,
)
from intraday_scanner.services.benchmark_service import (
    PRIMARY_BENCHMARK,
    SECONDARY_BENCHMARK,
)
from intraday_scanner.services.luna_research_slate_service import (
    AuthenticatedStrategyReceiptResolver,
)
from intraday_scanner.services.price_observation_service import parse_requested_at
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc
CAPTURE_MODEL_VERSION = "alphaops-sourced-outcome-v3"
YAHOO_RANGE = "5d"
BAR_INTERVAL = "1m"
ALPACA_TIMEFRAME = "1Min"
DEFAULT_MAX_CLOSE_STALENESS_SECONDS = 90
MAX_BAR_GAP_SECONDS = 60
CONCLUSIVE_STATUSES = {
    "complete_sourced",
    "not_triggered",
    "captured_ineligible",
}
FUTURE_EVIDENCE_SCHEMA_VERSION = "dawnstrike.future_evidence_receipt.v1"

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
    strategy_id, strategy_version = alphaops_strategy_contract(f"{resolved_date}T12:00:00-04:00")
    session = _session_window(resolved_date)
    captured_at = utc_now_iso()
    output_dir = Path(out_dir)
    store = SQLiteScanStore(db_path, read_only=not persist)
    store.initialize()
    contributor_receipt_verifier = AuthenticatedStrategyReceiptResolver.from_store(
        store,
        market_date=resolved_date,
        strategy_id=None,
    )
    official = validate_or_recover_official_cohort(
        store,
        market_date=resolved_date,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        persist_recovery=False,
    )
    if official.errors:
        raise SnapshotValidationError(
            "Exact AlphaOps official cohort is invalid: " + "; ".join(official.errors)
        )
    selection_rows = list(official.selections)
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
    radar_selections = (
        [
            row
            for row in store.load_signal_selections(
                cohort="research_radar", limit=50_000
            )
            if str(row.get("selected_at") or "")[:10] == resolved_date
        ]
        if persist
        else []
    )
    delivery_rows = store.load_notification_deliveries(
        channel="telegram",
        cohort="official_telegram",
        limit=50_000,
    )
    delivery_by_selection: dict[str, list[dict[str, Any]]] = {}
    for row in delivery_rows:
        selection_id = str(row.get("selection_id") or "")
        if selection_id:
            delivery_by_selection.setdefault(selection_id, []).append(row)
    paper_context_by_signal: dict[str, dict[str, object]] = {}
    for row in selection_rows:
        signal_id = str(row.get("signal_id") or "")
        selection_id = str(row.get("selection_id") or "")
        matches = delivery_by_selection.get(selection_id, [])
        if len(matches) != 1:
            raise SnapshotValidationError(
                "Exact AlphaOps selection lacks one unambiguous Telegram delivery "
                f"row: {selection_id or signal_id}"
            )
        try:
            context = canonical_paper_selection_context(
                row,
                delivery=matches[0],
                contributor_receipt_verifier=contributor_receipt_verifier,
            )
        except ValueError as exc:
            raise SnapshotValidationError(
                f"AlphaOps selection context is not canonical for {signal_id}: {exc}"
            ) from exc
        if signal_id in selected_ids:
            paper_context_by_signal[signal_id] = context
    if persist:
        frozen_official = validate_or_recover_official_cohort(
            store,
            market_date=resolved_date,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            persist_recovery=True,
        )
        if frozen_official.errors:
            raise SnapshotValidationError(
                "Exact AlphaOps official cohort could not be frozen: "
                + "; ".join(frozen_official.errors)
            )
    historical_signals = store.load_historical_signals(market_date=resolved_date, limit=50_000)
    signals = _outcome_targets(
        historical_signals,
        selected_signal_ids=selected_ids,
    )
    for signal in signals:
        signal_id = str(signal.get("signal_id") or "")
        selection_context = paper_context_by_signal.get(signal_id)
        if selection_context is None:
            raise SnapshotValidationError(
                f"Canonical paper selection context is absent for {signal_id}"
            )
        authoritative_signal = selection_context.get("authoritative_signal")
        if not isinstance(authoritative_signal, dict):
            raise SnapshotValidationError(
                f"Canonical paper selection plan is absent for {signal_id}"
            )
        signal.clear()
        signal.update(authoritative_signal)
        signal["_canonical_return_decision"] = selection_context
        signal["_canonical_return_decision_kind"] = "alpha_paper_selection"
    signals.extend(
        _v6_shadow_outcome_targets(
            store,
            market_date=resolved_date,
            historical_signals=historical_signals,
        )
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
    current_existing: dict[str, dict[str, Any]] = {}
    legacy_existing: dict[str, dict[str, Any]] = {}
    for signal in signals:
        signal_id = str(signal.get("signal_id") or "")
        existing_row = existing.get(signal_id)
        if existing_row is None:
            continue
        decision = signal.get("_canonical_return_decision")
        classification = classify_canonical_return_truth(existing_row, decision=decision)
        if classification in {
            CURRENT_RETURN_TRUTH,
            CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED,
            CURRENT_CENSORED_PATH,
        }:
            current_existing[signal_id] = existing_row
        else:
            legacy_existing[signal_id] = existing_row
    pending = [
        signal
        for signal in signals
        if replace or str(signal.get("signal_id") or "") not in current_existing
    ]
    diagnostics: list[dict[str, Any]] = [
        _existing_diagnostic(
            signal,
            current_existing[str(signal.get("signal_id") or "")],
        )
        for signal in signals
        if not replace and str(signal.get("signal_id") or "") in current_existing
    ]
    revision_summary = {
        "legacy_outcome_quarantined_count": len(legacy_existing),
        "outcome_revision_required": bool(legacy_existing),
        "canonical_source_available_revision_deferred_count": 0,
    }
    source_bars: dict[str, list[dict[str, Any]]] = {}
    source_requests: list[dict[str, Any]] = []
    repairable_events = [
        _outcome_event(row)
        for row in current_existing.values()
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
        summary.update(revision_summary)
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
        summary.update(revision_summary)
        _write_artifacts(output_dir, summary, [], diagnostics, source_bars)
        return {**summary, "outcomes": [], "diagnostics": diagnostics, "out_dir": str(output_dir)}

    if not pending and not radar_selections:
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
        summary.update(revision_summary)
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
        str(row.get("ticker") or "").upper()
        for row in [*pending, *radar_selections]
        if str(row.get("ticker") or "").strip()
    })
    requested_tickers = [*signal_tickers]
    for benchmark in (PRIMARY_BENCHMARK, SECONDARY_BENCHMARK):
        if benchmark not in requested_tickers:
            requested_tickers.append(benchmark)
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
    raw_entry_intents: dict[str, list[dict[str, Any]]] = {}
    for record in store.load_trade_intent_records(
        market_date=resolved_date,
        action="ENTER_LONG",
        limit=50_000,
    ):
        columns = record.get("columns")
        payload = record.get("payload_json")
        signal_id = str(
            (columns.get("signal_id") if isinstance(columns, dict) else None)
            or (payload.get("signal_id") if isinstance(payload, dict) else None)
            or ""
        )
        if signal_id:
            raw_entry_intents.setdefault(signal_id, []).append(record)
    for signal in signals:
        signal_id = str(signal.get("signal_id") or "")
        records = raw_entry_intents.get(signal_id, [])
        if not records:
            continue
        if len(records) != 1:
            signal["_canonical_entry_error"] = (
                "exactly one authenticated ENTER_LONG intent is required"
            )
            continue
        record = records[0]
        columns = record.get("columns")
        payload = record.get("payload_json")
        source_observation_id = str(
            (columns.get("source_observation_id") if isinstance(columns, dict) else None)
            or (payload.get("source_observation_id") if isinstance(payload, dict) else None)
            or ""
        )
        observations = store.load_price_observation_records(
            observation_id=source_observation_id,
            limit=2,
        )
        if len(observations) != 1:
            signal["_canonical_entry_error"] = "entry intent lacks one exact source observation"
            continue
        selection_context = signal.get("_canonical_return_decision")
        if not isinstance(selection_context, dict):
            signal["_canonical_entry_error"] = "canonical selection context is absent"
            continue
        try:
            composite = canonical_paper_enter_intent_context(
                selection_context,
                intent_record=record,
                source_observation_record=observations[0],
            )
        except ValueError as exc:
            signal["_canonical_entry_error"] = str(exc)
            continue
        signal["_canonical_return_decision"] = composite
        signal["_canonical_return_decision_kind"] = "alpha_paper_enter_intent"
        if isinstance(payload, dict):
            entry_intents[signal_id] = dict(payload)
    fills_by_signal: dict[str, list[dict[str, Any]]] = {}
    for row in store.load_paper_trade_fills(market_date=resolved_date, limit=50_000):
        signal_id = str(row.get("signal_id") or "")
        if signal_id:
            fills_by_signal.setdefault(signal_id, []).append(row)

    outcomes: list[dict[str, Any]] = []
    deferred_revision_candidates: list[dict[str, Any]] = []
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
        if signal.get("v6_counterfactual_policy") == "OPEN_TO_CLOSE_V1":
            outcome = _derive_rejected_counterfactual_outcome(
                signal,
                bars_by_ticker.get(ticker, []),
                session=session,
                requested_at=at,
                captured_at=captured_at,
                max_close_staleness_seconds=max_close_staleness_seconds,
                strategy_id=str(signal.get("outcome_strategy_id") or strategy_id),
                source_evidence=source_evidence_by_ticker.get(ticker, {}),
                benchmark_bars=bars_by_ticker.get(PRIMARY_BENCHMARK, []),
                benchmark_evidence=source_evidence_by_ticker.get(PRIMARY_BENCHMARK, {}),
                secondary_benchmark_bars=bars_by_ticker.get(SECONDARY_BENCHMARK, []),
                secondary_benchmark_evidence=source_evidence_by_ticker.get(SECONDARY_BENCHMARK, {}),
            )
        else:
            outcome = _derive_canonical_outcome(
                signal,
                bars_by_ticker.get(ticker, []),
                session=session,
                requested_at=at,
                captured_at=captured_at,
                max_close_staleness_seconds=max_close_staleness_seconds,
                strategy_id=str(signal.get("outcome_strategy_id") or strategy_id),
                source_evidence=source_evidence_by_ticker.get(ticker, {}),
                benchmark_bars=bars_by_ticker.get(PRIMARY_BENCHMARK, []),
                benchmark_evidence=source_evidence_by_ticker.get(PRIMARY_BENCHMARK, {}),
                secondary_benchmark_bars=bars_by_ticker.get(SECONDARY_BENCHMARK, []),
                secondary_benchmark_evidence=source_evidence_by_ticker.get(SECONDARY_BENCHMARK, {}),
                entry_intent=entry_intents.get(str(signal.get("signal_id") or "")),
                paper_fills=fills_by_signal.get(str(signal.get("signal_id") or ""), []),
            )
        signal_id = str(signal.get("signal_id") or "")
        conclusive = str(outcome.get("outcome_status") or "") in CONCLUSIVE_STATUSES
        if signal_id in legacy_existing and conclusive:
            deferred_revision_candidates.append(outcome)
            diagnostic = _diagnostic(
                signal,
                "canonical_source_available_revision_deferred",
                "Canonical source truth is available, but the legacy outcome remains "
                "immutable until additive outcome revisions are introduced.",
            )
        else:
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
        if conclusive and signal_id not in legacy_existing:
            outcomes.append(outcome)

    radar_outcomes = [
        _derive_research_selection_outcome(
            selection,
            bars_by_ticker.get(str(selection.get("ticker") or "").upper(), []),
            source_evidence_by_ticker.get(str(selection.get("ticker") or "").upper(), {}),
            session=session,
            # EOD bridge identity is anchored to the immutable market-session
            # close, never to the wall-clock acquisition retry time.
            requested_at=session.closed_at.astimezone(UTC),
            captured_at=captured_at,
        )
        for selection in radar_selections
    ]

    if persist and outcomes:
        atomic_stats = store.persist_signal_outcomes_with_events(
            outcomes,
            [_outcome_event(row) for row in outcomes],
            replace=replace,
        )
        persisted = atomic_stats["outcomes"]
        new_event_stats = atomic_stats["events"]
        audit_event_stats.update(
            {
                "inserted": audit_event_stats["inserted"] + new_event_stats["inserted"],
                "skipped": audit_event_stats["skipped"] + new_event_stats["skipped"],
                "new_inserted": new_event_stats["inserted"],
                "new_skipped": new_event_stats["skipped"],
            }
        )
    else:
        persisted = {"inserted": 0, "skipped": 0}
    radar_bridge_stats: dict[str, Any] = {
        "inserted": 0,
        "reused": 0,
        "row_count": 0,
        "status": "NOT_ATTEMPTED",
    }
    if persist:
        from intraday_scanner.services.research_episode_outcome_service import (
            build_and_persist_research_episode_outcome_bridges,
        )

        if radar_selections:
            try:
                radar_bridge_stats = build_and_persist_research_episode_outcome_bridges(
                    store,
                    radar_selections,
                    radar_outcomes,
                    market_date=resolved_date,
                    cutoff=session.closed_at.astimezone(UTC).isoformat(),
                    source_identity="alpha_sourced_eod_outcomes",
                    created_at=captured_at,
                )
            except SnapshotValidationError as exc:
                radar_bridge_stats["status"] = "INELIGIBLE"
                radar_bridge_stats["reason"] = str(exc)
    attempt_persisted = (
        store.persist_outcome_capture_attempts(capture_attempts)
        if persist and capture_attempts
        else {"inserted": 0, "skipped": 0, "row_count": len(capture_attempts)}
    )
    unresolved_count = sum(
        1 for row in capture_attempts if str(row.get("status") or "") == "terminal_missing"
    )
    status = "partial" if unresolved_count else "complete"
    if radar_selections and radar_bridge_stats.get("status") != "COMPLETE":
        status = "partial"
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
    summary["research_episode_outcome_bridges"] = radar_bridge_stats
    revision_summary["canonical_source_available_revision_deferred_count"] = len(
        deferred_revision_candidates
    )
    summary.update(revision_summary)
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
            or (selected_signal_ids is not None and signal_id not in selected_signal_ids)
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


def _v6_shadow_outcome_targets(
    store: SQLiteScanStore,
    *,
    market_date: str,
    historical_signals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Materialize V6 decisions as independent sourced paper-observation targets."""

    by_signal = {str(row.get("signal_id") or ""): row for row in historical_signals}
    targets: list[dict[str, Any]] = []
    for decision in store.load_alpha_v6_decisions(market_date=market_date):
        source_signal_id = str(decision.get("source_signal_id") or "")
        source = by_signal.get(source_signal_id)
        shadow_signal_id = str(decision.get("shadow_signal_id") or "")
        if not shadow_signal_id:
            continue
        action = str(decision.get("action") or "")
        sampling = decision.get("rejected_sampling")
        sampling_data = sampling if isinstance(sampling, dict) else {}
        sampled_reject = bool(
            action == "SHADOW_REJECTED_POLICY" and sampling_data.get("included") is True
        )
        if action == "SHADOW_TRACK" and source is not None:
            facts = decision.get("signal_facts")
            if not isinstance(facts, dict):
                continue
            target = {
                **facts,
                "signal_id": source_signal_id,
                "scan_id": decision.get("scan_id"),
                "market_date": decision.get("market_date"),
                "generated_at": decision.get("decision_at"),
            }
        elif sampled_reject:
            raw = decision.get("raw_facts")
            facts = raw if isinstance(raw, dict) else {}
            target = {
                "scan_id": decision.get("scan_id"),
                "ticker": decision.get("ticker"),
                "generated_at": decision.get("decision_at"),
                "source": facts.get("source"),
                "source_url": facts.get("source_url"),
                "v6_counterfactual_policy": "OPEN_TO_CLOSE_V1",
                "v6_sampling_probability": sampling_data.get("inclusion_probability"),
            }
        else:
            continue
        targets.append(
            {
                **target,
                "signal_id": shadow_signal_id,
                "alpha_signal_id": source_signal_id,
                "generated_at": decision.get("decision_at"),
                "strategy_id": ALPHAOPS_V6_STRATEGY_VERSION,
                "outcome_strategy_id": ALPHAOPS_V6_STRATEGY_VERSION,
                "v6_decision_id": decision.get("decision_id"),
                "v6_cost_model_version": decision.get("cost_model_version"),
                "v6_estimated_round_trip_cost_bps": decision.get("estimated_round_trip_cost_bps"),
                "research_only": True,
                "broker_execution_enabled": False,
                "_canonical_return_decision": dict(decision),
                "_canonical_return_decision_kind": "alpha_v6_shadow_decision",
            }
        )
    return targets


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
    """Resolve outcome bars through the configured, auditable provider order."""

    requests: list[dict[str, Any]] = []
    candidates: list[tuple[list[OutcomeBar], dict[str, Any]]] = []
    errors: list[str] = []
    alpaca_fetcher = fallback_fetcher
    if alpaca_fetcher is None and config.alpaca_api_key_id and config.alpaca_api_secret_key:
        alpaca_fetcher = _fetch_alpaca_rows

    for provider_name in _outcome_provider_order(config):
        if provider_name == "alpaca":
            _collect_alpaca_candidates(
                ticker=ticker,
                config=config,
                session=session,
                requested_at=requested_at,
                captured_at=captured_at,
                attempt_limit=attempt_limit,
                fetcher=alpaca_fetcher,
                requests=requests,
                candidates=candidates,
                errors=errors,
            )
        else:
            _collect_yahoo_candidates(
                ticker=ticker,
                config=config,
                session=session,
                requested_at=requested_at,
                captured_at=captured_at,
                attempt_limit=attempt_limit,
                fetcher=chart_fetcher,
                requests=requests,
                candidates=candidates,
                errors=errors,
            )

    for provider_name in _outcome_provider_order(config):
        source = (
            f"alpaca_market_data_{config.alpaca_data_feed}"
            if provider_name == "alpaca"
            else YAHOO_SOURCE_NAME
        )
        preferred = next(
            (
                (bars, request)
                for bars, request in candidates
                if request.get("source") == source
                and request.get("source_coverage_complete") is True
            ),
            None,
        )
        if preferred is not None:
            bars, request = preferred
            return (
                bars,
                _selected_source_evidence(request, requests, candidates),
                requests,
                "; ".join(errors),
            )

    if candidates:
        bars, request = max(candidates, key=lambda item: _source_choice_key(item[0]))
        return (
            bars,
            _selected_source_evidence(request, requests, candidates),
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


def _outcome_provider_order(config: ScannerConfig) -> tuple[str, str]:
    providers = tuple(
        item.strip().lower()
        for item in config.outcome_capture_provider_order.split(",")
        if item.strip()
    )
    if providers not in {("yahoo", "alpaca"), ("alpaca", "yahoo")}:
        raise SnapshotValidationError("Outcome provider order is invalid.")
    return providers


def _collect_yahoo_candidates(
    *,
    ticker: str,
    config: ScannerConfig,
    session: SessionWindow,
    requested_at: datetime,
    captured_at: str,
    attempt_limit: int,
    fetcher: FetchChart,
    requests: list[dict[str, Any]],
    candidates: list[tuple[list[OutcomeBar], dict[str, Any]]],
    errors: list[str],
) -> None:
    provider_symbol = yahoo_provider_symbol(ticker)
    yahoo_url = yahoo_chart_url(
        ticker,
        range_name=YAHOO_RANGE,
        interval=BAR_INTERVAL,
        include_pre_post=False,
    )
    for attempt in range(1, attempt_limit + 1):
        try:
            payload = fetcher(
                provider_symbol,
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
                request_contract={
                    "provider": YAHOO_SOURCE_NAME,
                    "ticker": ticker,
                    "provider_symbol": provider_symbol,
                    "endpoint": yahoo_url,
                    "range": YAHOO_RANGE,
                    "interval": BAR_INTERVAL,
                    "include_pre_post": False,
                },
            )
            requests.append(request)
            candidates.append((bars, request))
            if request["source_coverage_complete"] is True:
                return
        except (DataProviderError, ValueError, TypeError) as exc:
            detail = f"{YAHOO_SOURCE_NAME} attempt {attempt}: {exc}"
            errors.append(detail)
            requests.append(
                {
                    "ticker": ticker,
                    "status": "provider_error",
                    "source": YAHOO_SOURCE_NAME,
                    "source_url": yahoo_url,
                    "attempt": attempt,
                    "attempt_limit": attempt_limit,
                    "fetched_at": captured_at,
                    "bar_count": 0,
                    "error": str(exc),
                }
            )


def _collect_alpaca_candidates(
    *,
    ticker: str,
    config: ScannerConfig,
    session: SessionWindow,
    requested_at: datetime,
    captured_at: str,
    attempt_limit: int,
    fetcher: FetchNormalizedBars | None,
    requests: list[dict[str, Any]],
    candidates: list[tuple[list[OutcomeBar], dict[str, Any]]],
    errors: list[str],
) -> None:
    alpaca_source = f"alpaca_market_data_{config.alpaca_data_feed}"
    alpaca_url = "https://data.alpaca.markets/v2/stocks/bars"
    if fetcher is None:
        requests.append(
            {
                "ticker": ticker,
                "status": "not_configured",
                "source": alpaca_source,
                "source_url": alpaca_url,
                "attempt": 0,
                "attempt_limit": attempt_limit,
                "fetched_at": captured_at,
                "bar_count": 0,
                "error": "Read-only Alpaca market-data credentials are not configured.",
            }
        )
        return
    for attempt in range(1, attempt_limit + 1):
        try:
            rows = fetcher(
                ticker,
                config,
                start=_iso_utc(session.opened_at),
                end=_iso_utc(session.closed_at),
                timeframe=ALPACA_TIMEFRAME,
                feed=str(config.alpaca_data_feed),
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
                request_contract={
                    "provider": alpaca_source,
                    "ticker": ticker,
                    "symbols": [ticker],
                    "endpoint": alpaca_url,
                    "start": _iso_utc(session.opened_at),
                    "end": _iso_utc(session.closed_at),
                    "timeframe": ALPACA_TIMEFRAME,
                    "feed": str(config.alpaca_data_feed),
                },
            )
            requests.append(request)
            candidates.append((bars, request))
            if request["source_coverage_complete"] is True:
                return
        except (DataProviderError, ValueError, TypeError) as exc:
            detail = f"{alpaca_source} attempt {attempt}: {exc}"
            errors.append(detail)
            requests.append(
                {
                    "ticker": ticker,
                    "status": "provider_error",
                    "source": alpaca_source,
                    "source_url": alpaca_url,
                    "attempt": attempt,
                    "attempt_limit": attempt_limit,
                    "fetched_at": captured_at,
                    "bar_count": 0,
                    "error": str(exc),
                }
            )


def _fetch_alpaca_rows(
    ticker: str,
    config: ScannerConfig,
    *,
    start: str,
    end: str,
    timeframe: str,
    feed: str,
) -> list[dict[str, Any]]:
    """Use Alpaca's read-only market-data endpoint; no trading API is imported."""

    if timeframe != ALPACA_TIMEFRAME or feed != str(config.alpaca_data_feed):
        raise DataProviderError("Alpaca outcome request contract is not exact")
    provider = AlpacaProvider(config)
    if str(provider.feed) != feed:
        raise DataProviderError("Alpaca outcome feed conflicts with provider configuration")
    return provider.get_minute_bars([ticker], start, end, config)


def _provider_request(
    *,
    ticker: str,
    source: str,
    source_url: str,
    bars: list[OutcomeBar],
    session: SessionWindow,
    fetched_at: str,
    attempt: int,
    request_contract: Mapping[str, Any],
) -> dict[str, Any]:
    coverage = _validate_bar_coverage(
        bars,
        expected_start_at=session.opened_at,
        expected_end_at=session.closed_at - timedelta(minutes=1),
    )
    source_bar_hash = _bars_hash(bars)
    return {
        "ticker": ticker,
        "status": "ok" if coverage.is_complete else coverage.status,
        "source": source,
        "source_url": source_url,
        "request_contract": dict(request_contract),
        "attempt": attempt,
        "fetched_at": fetched_at,
        "bar_count": len(bars),
        "first_bar_at": _iso_utc(bars[0].observed_at) if bars else None,
        "last_bar_at": _iso_utc(bars[-1].observed_at) if bars else None,
        "source_bar_hash_sha256": source_bar_hash,
        "source_artifact_identity": (
            f"market-bars:{source}:{ticker}:{session.market_date}:{BAR_INTERVAL}:{source_bar_hash}"
        ),
        **coverage.to_dict(),
    }


def _selected_source_evidence(
    selected: dict[str, Any],
    requests: list[dict[str, Any]],
    candidates: list[tuple[list[OutcomeBar], dict[str, Any]]],
) -> dict[str, Any]:
    reconciliation = _independent_source_reconciliation(candidates)
    return {
        "source": selected.get("source"),
        "source_url": selected.get("source_url"),
        "source_artifact_identity": selected.get("source_artifact_identity"),
        "source_fetched_at": selected.get("fetched_at"),
        "source_bar_hash_sha256": selected.get("source_bar_hash_sha256"),
        "source_coverage_complete": selected.get("source_coverage_complete"),
        "request_contract": selected.get("request_contract"),
        "source_lineage": [dict(row) for row in requests],
        "provider_chain_exhausted": selected.get("source_coverage_complete") is not True,
        "independent_reconciliation": reconciliation,
        "independent_reconciliation_status": reconciliation["status"],
        "source_conflict": reconciliation["status"] == "DISAGREEMENT",
        "corporate_action_unresolved": False,
        "halt_intervals": (),
        "ordered_events": (),
        "ordered_evidence_complete": False,
        "ordered_evidence_identity": None,
        "ordered_evidence_hash_sha256": None,
        "ordered_evidence_start": None,
        "ordered_evidence_end": None,
    }


def _independent_source_reconciliation(
    candidates: list[tuple[list[OutcomeBar], dict[str, Any]]],
) -> dict[str, Any]:
    complete = [
        (bars, row)
        for bars, row in candidates
        if row.get("source_coverage_complete") is True and bars
    ]
    by_source: dict[str, tuple[list[OutcomeBar], dict[str, Any]]] = {}
    for bars, row in complete:
        by_source.setdefault(str(row.get("source") or ""), (bars, row))
    sources = set(by_source)
    if len(sources) < 2:
        return {
            "status": "NOT_AVAILABLE",
            "independent_source_count": len(sources),
            "agreement": None,
            "reason": "Two independently sourced complete bar sets are required.",
        }
    first_source, second_source = sorted(sources)[:2]
    first_bars, first_request = by_source[first_source]
    second_bars, second_request = by_source[second_source]
    first_close = {
        bar.observed_at: float(bar.close)
        for bar in first_bars
        if bar.close is not None and float(bar.close) > 0
    }
    second_close = {
        bar.observed_at: float(bar.close)
        for bar in second_bars
        if bar.close is not None and float(bar.close) > 0
    }
    overlap = sorted(set(first_close) & set(second_close))
    overlap_pct = 100.0 * len(overlap) / max(1, min(len(first_close), len(second_close)))
    maximum_close_difference_pct = max(
        (
            abs(first_close[timestamp] - second_close[timestamp])
            / max(first_close[timestamp], second_close[timestamp])
            * 100.0
            for timestamp in overlap
        ),
        default=None,
    )
    passed = bool(
        overlap_pct >= 98.0
        and maximum_close_difference_pct is not None
        and maximum_close_difference_pct <= 0.5
    )
    return {
        "status": "PASSED" if passed else "DISAGREEMENT",
        "independent_source_count": len(sources),
        "source_names": sorted(sources),
        "source_bar_hashes": sorted(
            {
                str(first_request.get("source_bar_hash_sha256") or ""),
                str(second_request.get("source_bar_hash_sha256") or ""),
            }
        ),
        "overlap_pct": round(overlap_pct, 6),
        "maximum_close_difference_pct": (
            round(maximum_close_difference_pct, 6)
            if maximum_close_difference_pct is not None
            else None
        ),
        "agreement": passed,
        "thresholds": {"minimum_overlap_pct": 98.0, "maximum_close_difference_pct": 0.5},
        "reason": (
            "Normalized one-minute close bars agree within the frozen threshold."
            if passed
            else "Independent bars do not satisfy the frozen overlap and price thresholds."
        ),
    }


def _derive_research_selection_outcome(
    selection: dict[str, Any],
    bars: list[OutcomeBar],
    source_evidence: dict[str, Any],
    *,
    session: SessionWindow,
    requested_at: datetime,
    captured_at: str,
) -> dict[str, Any]:
    """Derive selection-only research metrics from the frozen radar row.

    This deliberately has no plan, entry, fill, return, or P&L semantics.  A
    radar episode is anchored at its first complete sourced bar at/after the
    frozen selection timestamp and can only become eligible with a complete,
    contiguous current-day source window through the regular close.
    """

    payload = selection.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
    payload = payload if isinstance(payload, dict) else {}
    signal = payload.get("signal")
    if isinstance(signal, str):
        try:
            signal = json.loads(signal)
        except (TypeError, ValueError, json.JSONDecodeError):
            signal = {}
    signal = signal if isinstance(signal, dict) else {}
    selection_id = str(selection.get("selection_id") or "").strip()
    signal_id = str(selection.get("signal_id") or signal.get("signal_id") or "").strip()
    ticker = str(selection.get("ticker") or signal.get("ticker") or "").strip().upper()
    selected_at = str(selection.get("selected_at") or "").strip()
    selected_dt = _parse_datetime(selected_at)
    declared_bar_hash = str(source_evidence.get("source_bar_hash_sha256") or "").strip().lower()
    computed_bar_hash = _bars_hash(bars)
    source_bar_hash = declared_bar_hash or computed_bar_hash
    source_bar_payload = [bar.to_dict() for bar in bars]
    source_artifact_identity = str(
        source_evidence.get("source_artifact_identity") or ""
    ).strip()
    raw_lineage = source_evidence.get("source_lineage") or []
    lineage_rows = []
    if isinstance(raw_lineage, list):
        successful = [
            item
            for item in raw_lineage
            if isinstance(item, Mapping)
            and (
                str(item.get("status") or "").lower() == "ok"
                or item.get("source_coverage_complete") is True
            )
        ]
        lineage_input = successful or raw_lineage
        seen_lineage: set[str] = set()
        for item in lineage_input:
            if isinstance(item, Mapping):
                canonical_item = dict(item)
                for key in (
                    "fetched_at",
                    "attempt",
                    "attempt_limit",
                    "error",
                ):
                    canonical_item.pop(key, None)
                identity = json.dumps(
                    canonical_item, sort_keys=True, separators=(",", ":")
                )
                if identity not in seen_lineage:
                    lineage_rows.append(canonical_item)
                    seen_lineage.add(identity)
        lineage_rows.sort(
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
        )
    base: dict[str, Any] = {
        "selection_id": selection_id,
        "signal_id": signal_id,
        "ticker": ticker,
        "market_date": session.market_date,
        "date": session.market_date,
        "selected_at": selected_at,
        "requested_at": _iso_utc(requested_at),
        "captured_at": captured_at,
        "source": source_evidence.get("source") or "",
        "source_url": source_evidence.get("source_url") or "",
        "source_bar_interval": BAR_INTERVAL,
        "source_bar_count": len(bars),
        "source_first_bar_at": _iso_utc(bars[0].observed_at) if bars else None,
        "source_last_bar_at": _iso_utc(bars[-1].observed_at) if bars else None,
        "source_bar_hash_sha256": source_bar_hash,
        "source_bar_payload": source_bar_payload,
        "source_fetched_at": source_evidence.get("source_fetched_at"),
        "source_artifact_identity": source_artifact_identity,
        "source_lineage": lineage_rows,
        "source_coverage_complete": source_evidence.get("source_coverage_complete"),
        "automatic_sourced_data": True,
        # Authentication is granted only after the structured source binding
        # and exact bars hash have passed validation below.
        "source_authenticated": False,
        "no_lookahead": True,
        "research_only": True,
        "broker_execution_enabled": False,
        "capture_model_version": CAPTURE_MODEL_VERSION,
        "capture_mode": "automatic_sourced_selection_observation",
        "independent_reconciliation": source_evidence.get("independent_reconciliation"),
        "independent_reconciliation_status": str(
            source_evidence.get("independent_reconciliation_status") or ""
        ),
        "source_conflict": source_evidence.get("source_conflict") is True,
    }
    lineage_valid = bool(lineage_rows) and all(
        isinstance(item, Mapping)
        and str(item.get("source") or "").strip()
        and str(item.get("source_url") or "").strip()
        for item in lineage_rows
    )
    lineage_sources = {
        str(item.get("source") or "").strip()
        for item in lineage_rows
        if isinstance(item, Mapping)
    }
    source_binding = {
        "provider": str(source_evidence.get("source") or "").strip(),
        "source_url": str(source_evidence.get("source_url") or "").strip(),
        "source_artifact_identity": source_artifact_identity,
        "source_bar_hash_sha256": source_bar_hash,
        "source_lineage": lineage_rows,
        "request_contract": source_evidence.get("request_contract"),
        "independent_reconciliation": source_evidence.get("independent_reconciliation"),
        "independent_reconciliation_status": str(
            source_evidence.get("independent_reconciliation_status") or ""
        ),
        "source_cutoff": _iso_utc(requested_at),
    }
    try:
        source_binding["reconciliation_hash_sha256"] = hashlib.sha256(
            json.dumps(
                source_binding["independent_reconciliation"],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError):
        source_binding["reconciliation_hash_sha256"] = ""
        lineage_valid = False
    try:
        source_binding["source_request_hash_sha256"] = hashlib.sha256(
            json.dumps(
                lineage_rows,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError):
        source_binding["source_request_hash_sha256"] = ""
        lineage_valid = False
    try:
        from intraday_scanner.services.research_episode_outcome_service import (
            _expected_provider_request_contract,
        )

        expected_full_count = int(
            (session.closed_at - session.opened_at).total_seconds() // 60
        )
        expected_full_start = _iso_utc(session.opened_at)
        expected_full_end = _iso_utc(session.closed_at - timedelta(minutes=1))
        expected_maximum_gap = (
            0 if expected_full_count == 1 else MAX_BAR_GAP_SECONDS
        )
        selected_request = _expected_provider_request_contract(
            provider=str(source_binding["provider"]),
            ticker=ticker,
            source_url=str(source_binding["source_url"]),
            session_open=session.opened_at,
            session_close=session.closed_at,
        )
        if source_binding["request_contract"] != selected_request:
            raise SnapshotValidationError("selected provider request contract mismatch")
        selected_lineage_count = 0
        for item in lineage_rows:
            item_provider = str(item.get("source") or "").strip()
            item_url = str(item.get("source_url") or "").strip()
            item_hash = str(item.get("source_bar_hash_sha256") or "").lower()
            expected_request = _expected_provider_request_contract(
                provider=item_provider,
                ticker=ticker,
                source_url=item_url,
                session_open=session.opened_at,
                session_close=session.closed_at,
            )
            expected_artifact = (
                f"market-bars:{item_provider}:{ticker}:{session.market_date}:"
                f"{BAR_INTERVAL}:{item_hash}"
            )
            if (
                str(item.get("ticker") or "").upper() != ticker
                or not re.fullmatch(r"[0-9a-f]{64}", item_hash)
                or item.get("request_contract") != expected_request
                or str(item.get("source_artifact_identity") or "")
                != expected_artifact
                or str(item.get("status") or "").lower() != "ok"
                or item.get("source_coverage_complete") is not True
                or str(item.get("coverage_status") or "").lower() != "complete"
                or item.get("coverage_expected_start_at") != expected_full_start
                or item.get("coverage_expected_end_at") != expected_full_end
                or item.get("coverage_expected_minute_count") != expected_full_count
                or item.get("coverage_observed_minute_count") != expected_full_count
                or item.get("bar_count") != expected_full_count
                or item.get("first_bar_at") != expected_full_start
                or item.get("last_bar_at") != expected_full_end
                or item.get("coverage_maximum_gap_seconds") != expected_maximum_gap
                or item.get("coverage_allowed_gap_seconds") != MAX_BAR_GAP_SECONDS
            ):
                raise SnapshotValidationError("provider lineage contract is incomplete")
            if (
                item_provider == source_binding["provider"]
                and item_url == source_binding["source_url"]
            ):
                selected_lineage_count += 1
                if (
                    item_hash != source_bar_hash
                    or str(item.get("source_artifact_identity") or "")
                    != source_artifact_identity
                ):
                    raise SnapshotValidationError(
                        "selected provider lineage conflicts with source binding"
                    )
        reconciliation = source_binding["independent_reconciliation"]
        if not isinstance(reconciliation, Mapping):
            raise SnapshotValidationError("provider reconciliation is absent")
        lineage_source_names = sorted(
            {str(item.get("source") or "") for item in lineage_rows}
        )
        reconciliation_status = str(reconciliation.get("status") or "")
        if (
            selected_lineage_count < 1
            or reconciliation.get("independent_source_count")
            != len(lineage_source_names)
            or reconciliation_status == "DISAGREEMENT"
        ):
            raise SnapshotValidationError("provider reconciliation is not learning-safe")
        if len(lineage_source_names) < 2:
            if reconciliation_status != "NOT_AVAILABLE" or reconciliation.get(
                "agreement"
            ) is not None:
                raise SnapshotValidationError("provider reconciliation availability mismatch")
        elif (
            reconciliation_status != "PASSED"
            or reconciliation.get("agreement") is not True
            or reconciliation.get("source_names") != lineage_source_names
        ):
            raise SnapshotValidationError("provider reconciliation lineage mismatch")
    except (SnapshotValidationError, TypeError, ValueError):
        lineage_valid = False
    source_binding_valid = bool(
        source_binding["provider"]
        and source_binding["source_url"]
        and source_binding["source_artifact_identity"]
        and source_bar_hash in source_binding["source_artifact_identity"]
        and source_binding["source_bar_hash_sha256"] == computed_bar_hash
        and lineage_valid
        and source_binding["provider"] in lineage_sources
        and source_artifact_identity
        == (
            f"market-bars:{source_binding['provider']}:{ticker}:{session.market_date}:"
            f"{BAR_INTERVAL}:{source_bar_hash}"
        )
        and source_evidence.get("source_conflict") is not True
        and str(source_evidence.get("independent_reconciliation_status") or "")
        != "DISAGREEMENT"
    )
    try:
        from intraday_scanner.services.research_episode_outcome_service import (
            _selection_identity,
        )

        frozen_identity = _selection_identity(
            selection,
            day=session.market_date,
            cutoff=requested_at.astimezone(UTC),
        )
    except (SnapshotValidationError, TypeError, ValueError) as exc:
        base.update({
            "outcome_status": "INELIGIBLE",
            "learning_eligible": False,
            "outcome_reason": f"frozen selection lineage is invalid: {exc}",
        })
        return base
    selection_id = str(frozen_identity["selection_id"])
    signal_id = str(selection.get("signal_id") or "").strip()
    ticker = str(frozen_identity["ticker"]).upper()
    selected_at = str(frozen_identity["selected_at"])
    selected_dt = _parse_datetime(selected_at)
    base.update({
        "selection_id": selection_id,
        "signal_id": signal_id,
        "ticker": ticker,
        "market_date": str(frozen_identity["market_date"]),
        "selected_at": selected_at,
        "episode_id": str(frozen_identity["episode_id"]),
        "slate_id": str(frozen_identity["slate_id"]),
        "slate_content_hash_sha256": str(
            frozen_identity["slate_content_hash_sha256"]
        ),
    })
    if selected_dt is None:
        base.update({
            "outcome_status": "INELIGIBLE",
            "learning_eligible": False,
            "outcome_reason": "selection timestamp is invalid",
        })
        return base
    if not bars:
        base.update({
            "outcome_status": "MISSING",
            "learning_eligible": False,
            "outcome_reason": "no current sourced bars are available",
        })
        return base
    if (
        not source_binding_valid
        or declared_bar_hash != computed_bar_hash
    ):
        base.update({
            "outcome_status": "INELIGIBLE",
            "learning_eligible": False,
            "outcome_reason": (
                "source provider/artifact/request/reconciliation lineage or "
                "canonical bars hash is invalid"
            ),
        })
        return base
    first_eligible_at = max(session.opened_at, _ceil_minute(selected_dt))
    eligible_bars = [
        bar
        for bar in bars
        if first_eligible_at <= bar.observed_at < session.closed_at
    ]
    complete_bars = [bar for bar in eligible_bars if _research_bar_complete(bar)]
    if not complete_bars:
        base.update({
            "outcome_status": "MISSING",
            "learning_eligible": False,
            "outcome_reason": "no complete sourced selection observation is available",
        })
        return base
    if any(bar.observed_at > requested_at.astimezone(UTC) for bar in complete_bars):
        base.update({
            "outcome_status": "INELIGIBLE",
            "learning_eligible": False,
            "outcome_reason": "sourced selection bars exceed the outcome cutoff",
        })
        return base
    if len(complete_bars) != len(eligible_bars):
        base.update({
            "outcome_status": "INELIGIBLE",
            "learning_eligible": False,
            "outcome_reason": "sourced selection window contains incomplete OHLC bars",
        })
        return base
    coverage = _validate_bar_coverage(
        complete_bars,
        expected_start_at=first_eligible_at,
        expected_end_at=session.closed_at - timedelta(minutes=1),
    )
    base.update(coverage.to_dict())
    malformed = _malformed_ohlc_detail(complete_bars)
    if not coverage.is_complete or malformed or source_evidence.get(
        "source_coverage_complete"
    ) is not True:
        base.update({
            "outcome_status": "INELIGIBLE",
            "learning_eligible": False,
            "outcome_reason": malformed
            or coverage.detail
            or "sourced selection coverage is incomplete",
        })
        return base
    reference = complete_bars[0]
    reference_price = float(reference.close)
    subsequent_bars = complete_bars[1:]
    high = max((float(bar.high) for bar in subsequent_bars), default=None)
    low = min((float(bar.low) for bar in subsequent_bars), default=None)
    close = float(subsequent_bars[-1].close) if subsequent_bars else None
    metrics = {
        "reference_at": _iso_utc(reference.observed_at),
        "reference_price": reference_price,
        "close_at": (
            _iso_utc(subsequent_bars[-1].observed_at) if subsequent_bars else None
        ),
        "close_price": close,
        "high_after_reference": high,
        "low_after_reference": low,
        "mfe_pct": (
            round((high - reference_price) / reference_price * 100.0, 6)
            if high is not None
            else None
        ),
        "mae_pct": (
            round((low - reference_price) / reference_price * 100.0, 6)
            if low is not None
            else None
        ),
        "path_status": (
            "NO_SUBSEQUENT_OBSERVATION"
            if not subsequent_bars
            else "POSITIVE_CLOSE"
            if close > reference_price
            else "NEGATIVE_CLOSE"
            if close < reference_price
            else "FLAT_CLOSE"
        ),
        "bar_count": len(subsequent_bars),
    }
    metric_body = {
        "selection_id": selection_id,
        "signal_id": signal_id,
        "ticker": ticker,
        "market_date": session.market_date,
        "source_bar_hash_sha256": source_bar_hash,
        "source_binding": source_binding,
        "metrics": metrics,
    }
    metric_hash = hashlib.sha256(
        json.dumps(metric_body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    observation_payload = {
        "ticker": ticker,
        "market_date": session.market_date,
        **reference.to_dict(),
    }
    reference_hash = hashlib.sha256(
        json.dumps(
            observation_payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    path_id = f"selection-path:{selection_id}:{source_bar_hash[:24]}"
    path_hash = hashlib.sha256(
        json.dumps(
            {"path_id": path_id, "metrics": metrics, "source_bar_hash_sha256": source_bar_hash},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    path_payload = {
        "path_id": path_id,
        "metrics": metrics,
        "source_bar_hash_sha256": source_bar_hash,
    }
    base.update({
        "source_authenticated": bool(
            source_binding_valid
        ),
        "source_observation_id": (
            f"selection-observation:{selection_id}:{_iso_utc(reference.observed_at)}"
        ),
        "source_observation_hash_sha256": reference_hash,
        "source_observation_payload": observation_payload,
        "source_path_id": path_id,
        "source_path_hash_sha256": path_hash,
        "source_path_payload": path_payload,
        "source_cutoff": _iso_utc(requested_at),
        "source_binding": source_binding,
        "outcome_artifact_id": f"selection-outcome:{selection_id}:{metric_hash[:24]}",
        "outcome_artifact_hash_sha256": metric_hash,
        "selection_outcome_metrics": metrics,
        "outcome_status": "COMPLETE_SOURCED" if subsequent_bars else "MISSING",
        "learning_eligible": bool(subsequent_bars),
        "outcome_reason": (
            "complete sourced selection observation window"
            if subsequent_bars
            else "reference observation has no strictly subsequent path bar"
        ),
    })
    return base


def _research_bar_complete(bar: OutcomeBar) -> bool:
    try:
        values = tuple(float(value) for value in (bar.open, bar.high, bar.low, bar.close))
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(value) and value > 0 for value in values):
        return False
    if bar.volume is None:
        return True
    try:
        volume = float(bar.volume)
    except (TypeError, ValueError):
        return False
    return math.isfinite(volume) and volume >= 0


def _source_choice_key(bars: list[OutcomeBar]) -> tuple[int, int, int]:
    return (
        len(bars),
        sum(_bar_completeness(bar) for bar in bars),
        int(bars[-1].observed_at.timestamp()) if bars else 0,
    )


def _derive_canonical_outcome(
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
    secondary_benchmark_bars: list[OutcomeBar],
    secondary_benchmark_evidence: dict[str, Any],
    entry_intent: dict[str, Any] | None,
    paper_fills: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one outcome from the canonical replay receipt, without repricing."""

    del entry_intent, paper_fills
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
    decision = signal.get("_canonical_return_decision")
    decision_kind = signal.get("_canonical_return_decision_kind")
    if not isinstance(decision, dict) or decision_kind not in {
        "alpha_paper_selection",
        "alpha_paper_enter_intent",
        "alpha_v6_shadow_decision",
    }:
        return _ineligible(base, "ineligible_missing_causal_decision")
    entry_error = signal.get("_canonical_entry_error")
    if isinstance(entry_error, str) and entry_error:
        return _ineligible(
            base,
            "ineligible_ambiguous_canonical_entry_intent",
            entry_error,
        )
    decision_at = _parse_datetime(
        str(
            decision.get(
                "selected_at" if decision_kind == "alpha_paper_selection" else "decision_at"
            )
            or ""
        )
    )
    if decision_at is None:
        return _ineligible(base, "ineligible_missing_recommendation_timestamp")
    first_eligible_at = max(session.opened_at, _ceil_minute(decision_at))
    eligible_bars = [
        bar for bar in bars if first_eligible_at <= bar.observed_at < session.closed_at
    ]
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
    trigger = _first_float(
        signal.get("entry_watch_level"),
        _raw_signal(signal).get("entry_trigger"),
        _raw_signal(signal).get("breakout_trigger"),
    )
    target = _first_float(
        signal.get("target_1"),
        _raw_signal(signal).get("first_target"),
    )
    invalidation = _first_float(
        signal.get("invalidation_level"),
        signal.get("exit_line"),
        _raw_signal(signal).get("invalidation_level"),
    )
    if trigger is None or trigger <= 0:
        return _ineligible(base, "ineligible_missing_entry_trigger")
    if target is None or invalidation is None:
        return _ineligible(base, "ineligible_missing_plan")
    if target <= trigger or invalidation >= trigger or invalidation <= 0:
        return _ineligible(base, "ineligible_plan_geometry")
    path_entry_receipt: dict[str, object] | None = None
    if decision_kind == "alpha_paper_enter_intent":
        intent_receipt = decision.get("entry_intent_receipt")
        if not isinstance(intent_receipt, dict):
            return _ineligible(base, "ineligible_missing_canonical_entry_receipt")
        intent_trigger = _float(intent_receipt.get("trigger_price"))
        intent_target = _float(intent_receipt.get("target_price"))
        intent_stop = _float(intent_receipt.get("stop_price"))
        if not (
            intent_trigger == trigger and intent_target == target and intent_stop == invalidation
        ):
            return _ineligible(
                base,
                "ineligible_canonical_entry_plan_mismatch",
            )
        try:
            path_entry_receipt = build_canonical_path_entry_receipt(decision)
        except ValueError as exc:
            return _ineligible(
                base,
                "ineligible_missing_canonical_entry_receipt",
                str(exc),
            )
    raw_artifact_identity = source_evidence.get("source_artifact_identity")
    if not isinstance(raw_artifact_identity, str) or not raw_artifact_identity.strip():
        return _ineligible(base, "ineligible_missing_source_artifact_identity")
    try:
        replay_binding = canonical_replay_binding(
            decision,
            kind=str(decision_kind),
        )
        future_receipt = _future_evidence_receipt(
            eligible_bars,
            symbol=str(signal.get("ticker") or "").upper(),
            market_date=session.market_date,
            raw_artifact_identity=raw_artifact_identity,
            coverage_start=first_eligible_at,
            coverage_end=session.closed_at,
            coverage_complete=coverage.is_complete,
        )
    except ValueError as exc:
        return _ineligible(base, "ineligible_canonical_path_context", str(exc))
    path_replay = resolve_path(
        eligible_bars,
        decision_at=first_eligible_at,
        trigger=trigger,
        target=target,
        stop=invalidation,
        halt_intervals=source_evidence.get("halt_intervals") or (),
        session_close=session.closed_at,
        source_conflict=source_evidence.get("source_conflict") is True,
        corporate_action_unresolved=(source_evidence.get("corporate_action_unresolved") is True),
        source_artifact_identity=future_receipt["receipt_id"],
        source_artifact_hash_sha256=future_receipt["receipt_hash_sha256"],
        source_coverage_complete=coverage.is_complete,
        ordered_events=source_evidence.get("ordered_events") or (),
        ordered_evidence_complete=(source_evidence.get("ordered_evidence_complete") is True),
        ordered_evidence_identity=source_evidence.get("ordered_evidence_identity"),
        ordered_evidence_hash_sha256=source_evidence.get("ordered_evidence_hash_sha256"),
        ordered_evidence_start=source_evidence.get("ordered_evidence_start"),
        ordered_evidence_end=source_evidence.get("ordered_evidence_end"),
        replay_binding=replay_binding,
        future_evidence_receipt=future_receipt,
        entry_mode=(ENTRY_MODE_ALREADY_ENTERED if path_entry_receipt is not None else None),
        entry_receipt=path_entry_receipt,
    )
    path_receipt = path_replay.to_dict()
    base.update(path_receipt)
    entry_at = _parse_datetime(str(path_receipt.get("entry_time") or ""))
    exit_at = _parse_datetime(str(path_receipt.get("exit_time") or ""))
    benchmark_return = (
        _benchmark_return(benchmark_bars, entry_at=entry_at, exit_at=exit_at)
        if entry_at is not None and exit_at is not None
        else None
    )
    secondary_benchmark_return = (
        _benchmark_return(
            secondary_benchmark_bars,
            entry_at=entry_at,
            exit_at=exit_at,
        )
        if entry_at is not None and exit_at is not None
        else None
    )
    estimated_v6_cost = _float(decision.get("estimated_round_trip_cost_bps"))
    if decision_kind == "alpha_v6_shadow_decision":
        if estimated_v6_cost is None or estimated_v6_cost <= 0.0:
            return _ineligible(
                base,
                "ineligible_incomplete_canonical_return_truth",
                "V6 decision lacks a finite positive round-trip cost estimate",
            )
        entry_slippage_bps = estimated_v6_cost / 2.0
        exit_slippage_bps = estimated_v6_cost / 2.0
        fee_bps_per_side = 0.0
        commission_per_share_per_side = 0.0
        modeled_cost_identity = str(decision.get("cost_model_version") or "")
    else:
        if not (
            decision.get("strategy_id") == DEFAULT_V5_POLICY.strategy_id
            and decision.get("strategy_version") == DEFAULT_V5_POLICY.strategy_version
        ):
            return _ineligible(
                base,
                "ineligible_unsupported_paper_cost_contract",
                "paper decision predates the authenticated V5 cost contract",
            )
        entry_slippage_bps = DEFAULT_V5_POLICY.entry_slippage_bps
        exit_slippage_bps = DEFAULT_V5_POLICY.exit_slippage_bps
        fee_bps_per_side = 0.0
        commission_per_share_per_side = DEFAULT_V5_POLICY.commission_per_share_per_side
        modeled_cost_identity = DEFAULT_V5_POLICY.cost_model_version
    canonical_notional = 1_000.0
    if decision_kind == "alpha_paper_enter_intent":
        intent_receipt = decision.get("entry_intent_receipt")
        parsed_notional = _float(
            intent_receipt.get("notional") if isinstance(intent_receipt, dict) else None
        )
        canonical_notional = parsed_notional if parsed_notional is not None else 0.0
        if canonical_notional <= 0.0:
            return _ineligible(
                base,
                "ineligible_invalid_paper_notional",
                "authenticated paper entry lacks a positive notional",
            )
    try:
        canonical = build_canonical_return_truth(
            path_replay_receipt=path_receipt,
            decision=decision,
            decision_kind=str(decision_kind),
            notional_per_trade=canonical_notional,
            entry_slippage_bps=entry_slippage_bps,
            exit_slippage_bps=exit_slippage_bps,
            fee_bps_per_side=fee_bps_per_side,
            commission_per_share_per_side=commission_per_share_per_side,
            observed_cost_model_identity="alpha-outcome-capture-observed-bars.v2",
            modeled_cost_model_identity=modeled_cost_identity,
            benchmark_return_pct=benchmark_return,
            benchmark_source_bar_hash_sha256=benchmark_evidence.get("source_bar_hash_sha256"),
            benchmark_independent_reconciliation_status=str(
                benchmark_evidence.get("independent_reconciliation_status") or ""
            ),
            secondary_benchmark_return_pct=secondary_benchmark_return,
            secondary_benchmark_source_bar_hash_sha256=(
                secondary_benchmark_evidence.get("source_bar_hash_sha256")
            ),
            secondary_benchmark_independent_reconciliation_status=str(
                secondary_benchmark_evidence.get("independent_reconciliation_status") or ""
            ),
            prospective_promotion_eligible=True,
        )
    except ValueError as exc:
        return _ineligible(
            base,
            "ineligible_incomplete_canonical_return_truth",
            str(exc),
        )
    classification = classify_canonical_return_truth(canonical, decision=decision)
    projection = canonical_return_truth_projection(canonical, decision=decision)
    if not projection or classification not in {
        CURRENT_RETURN_TRUTH,
        CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED,
        CURRENT_CENSORED_PATH,
    }:
        return _ineligible(
            base,
            "ineligible_invalid_canonical_return_truth",
        )
    base.update(projection)
    base.update(
        _canonical_compatibility_projection(
            projection,
            trigger=trigger,
            target=target,
            invalidation=invalidation,
            benchmark_evidence=benchmark_evidence,
            secondary_benchmark_evidence=secondary_benchmark_evidence,
            classification=classification,
        )
    )
    base["payload_json"] = dict(base)
    return base


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
    secondary_benchmark_bars: list[OutcomeBar],
    secondary_benchmark_evidence: dict[str, Any],
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
    path_replay = resolve_path(
        eligible_bars,
        decision_at=first_eligible_at,
        trigger=trigger,
        target=target,
        stop=invalidation,
    )
    base.update(path_replay.to_dict())
    if path_replay.path_truth_status in {
        PathTruthStatus.ENTRY_BAR_AMBIGUOUS,
        PathTruthStatus.MISSING_BARS,
        PathTruthStatus.KNOWN_HALT_WINDOW,
        PathTruthStatus.SOURCE_CONFLICT,
        PathTruthStatus.CORPORATE_ACTION_UNRESOLVED,
        PathTruthStatus.DATA_INELIGIBLE,
    }:
        return _ineligible(
            base,
            f"path_truth_{path_replay.path_truth_status.value.lower()}",
            path_replay.notes[0] if path_replay.notes else "path truth is not eligible",
        )
    post_entry = [bar for bar in eligible_bars if bar.observed_at >= trigger_bar.observed_at]
    if not post_entry:
        return _ineligible(base, "ineligible_no_post_entry_bars")
    post_trigger = [bar for bar in post_entry if bar.observed_at > trigger_bar.observed_at]
    if not post_trigger:
        return _ineligible(
            base,
            "path_truth_entry_bar_ambiguous",
            "trigger-bar extrema are excluded and no later bar proves the path",
        )
    high_bar = max(
        (bar for bar in post_trigger if bar.high is not None),
        key=lambda bar: float(bar.high or 0.0),
    )
    low_bar = min(
        (bar for bar in post_trigger if bar.low is not None),
        key=lambda bar: float(bar.low or math.inf),
    )
    high = float(high_bar.high or 0.0)
    low = float(low_bar.low or 0.0)
    price_1m, price_1m_at = _horizon_price(post_entry, trigger_bar.observed_at, 1)
    price_5m, price_5m_at = _horizon_price(post_entry, trigger_bar.observed_at, 5)
    price_15m, price_15m_at = _horizon_price(post_entry, trigger_bar.observed_at, 15)
    lunch_price, lunch_at = _lunch_price(post_entry, trigger_bar.observed_at, session)
    target_at = path_replay.target_touched_at
    invalidation_at = path_replay.stop_touched_at
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
    secondary_benchmark_return = _benchmark_return(
        secondary_benchmark_bars,
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
    benchmark_contract_complete = bool(
        benchmark_return is not None
        and (
            strategy_id != ALPHAOPS_V6_STRATEGY_VERSION
            or (
                secondary_benchmark_return is not None
                and source_evidence.get("independent_reconciliation_status") == "PASSED"
                and benchmark_evidence.get("independent_reconciliation_status") == "PASSED"
                and secondary_benchmark_evidence.get("independent_reconciliation_status")
                == "PASSED"
            )
        )
    )
    learning_eligible = bool(
        benchmark_contract_complete
        and (
            strategy_id
            not in {
                ALPHAOPS_V5_STRATEGY_ID,
                ALPHAOPS_V6_STRATEGY_VERSION,
            }
            or entry_intent is not None
        )
    )
    base.update(
        {
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
            "time_to_mfe_minutes": _elapsed_minutes(trigger_bar.observed_at, high_bar.observed_at),
            "time_to_mae_minutes": _elapsed_minutes(trigger_bar.observed_at, low_bar.observed_at),
            "target_price": target,
            "invalidation_price": invalidation,
            "target_touched_at": _iso_utc(target_at) if target_at else None,
            "invalidation_touched_at": _iso_utc(invalidation_at) if invalidation_at else None,
            "planned_first_touch_outcome": first_touch,
            "exit_reason": exit_reason,
            "exit_time": _iso_utc(exit_at),
            "exit_price": raw_exit_price,
            "holding_duration_minutes": _elapsed_minutes(trigger_bar.observed_at, exit_at),
            "gross_return_pct": raw_return,
            "benchmark_symbol": PRIMARY_BENCHMARK,
            "benchmark_return_pct": benchmark_return,
            "excess_return_pct": (
                round(raw_return - benchmark_return, 4)
                if raw_return is not None and benchmark_return is not None
                else None
            ),
            "benchmark_source": benchmark_evidence.get("source"),
            "benchmark_source_url": benchmark_evidence.get("source_url"),
            "benchmark_source_bar_hash_sha256": benchmark_evidence.get("source_bar_hash_sha256"),
            "benchmark_independent_reconciliation_status": benchmark_evidence.get(
                "independent_reconciliation_status"
            ),
            "secondary_benchmark_symbol": SECONDARY_BENCHMARK,
            "secondary_benchmark_return_pct": secondary_benchmark_return,
            "secondary_benchmark_source": secondary_benchmark_evidence.get("source"),
            "secondary_benchmark_source_url": secondary_benchmark_evidence.get("source_url"),
            "secondary_benchmark_source_bar_hash_sha256": secondary_benchmark_evidence.get(
                "source_bar_hash_sha256"
            ),
            "secondary_benchmark_independent_reconciliation_status": (
                secondary_benchmark_evidence.get("independent_reconciliation_status")
            ),
            "attribution_complete": benchmark_return is not None,
            "benchmark_contract_complete": benchmark_contract_complete,
            "first_touch_precision": BAR_INTERVAL,
            "outcome_status": "complete_sourced",
            "learning_eligible": learning_eligible,
            "learning_contract": (
                "v6_shadow_outcome_requires_frozen_cost_label"
                if strategy_id == ALPHAOPS_V6_STRATEGY_VERSION
                else "candidate_outcome_requires_reconciled_trade_label"
            ),
            "validated_against_signal_timestamp": True,
            **context,
            **execution,
            "notes": (
                "Automatic read-only multi-provider EOD observation; one-minute bars; "
                "same-bar target/stop ambiguity is counted conservatively as invalidation. "
                "Production return learning still requires an exact reconciled trade label."
            ),
        }
    )
    base["payload_json"] = dict(base)
    return base


def _derive_rejected_counterfactual_outcome(
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
    secondary_benchmark_bars: list[OutcomeBar],
    secondary_benchmark_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Observe a predeclared sampled-reject policy without inventing a V5 plan."""

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
    if any(
        bar.open is None or bar.high is None or bar.low is None or bar.close is None
        for bar in eligible_bars
    ):
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
    entry_bar = eligible_bars[0]
    exit_bar = eligible_bars[-1]
    entry_price = float(entry_bar.open or 0.0)
    exit_price = float(exit_bar.close or 0.0)
    if entry_price <= 0 or exit_price <= 0:
        return _ineligible(base, "ineligible_invalid_open_close_price")
    benchmark_return = _benchmark_return(
        benchmark_bars,
        entry_at=entry_bar.observed_at,
        exit_at=exit_bar.observed_at,
    )
    secondary_benchmark_return = _benchmark_return(
        secondary_benchmark_bars,
        entry_at=entry_bar.observed_at,
        exit_at=exit_bar.observed_at,
    )
    gross_return = _return_pct(exit_price, entry_price)
    high_bar = max(eligible_bars, key=lambda bar: float(bar.high or 0.0))
    low_bar = min(eligible_bars, key=lambda bar: float(bar.low or math.inf))
    benchmark_complete = bool(
        benchmark_return is not None
        and secondary_benchmark_return is not None
        and source_evidence.get("independent_reconciliation_status") == "PASSED"
        and benchmark_evidence.get("independent_reconciliation_status") == "PASSED"
        and secondary_benchmark_evidence.get("independent_reconciliation_status") == "PASSED"
    )
    base.update(
        {
            "entry_opportunity": True,
            "entry_time": _iso_utc(entry_bar.observed_at),
            "entry_price": entry_price,
            "entry_price_policy": "sampled_reject_first_eligible_bar_open_v1",
            "close_price": exit_price,
            "close_price_observed_at": _iso_utc(exit_bar.observed_at),
            "high_after_entry": high_bar.high,
            "low_after_entry": low_bar.low,
            "max_favorable_excursion_pct": _return_pct(high_bar.high, entry_price),
            "max_adverse_excursion_pct": _return_pct(low_bar.low, entry_price),
            "planned_first_touch_outcome": None,
            "exit_reason": "sampled_reject_regular_session_close_v1",
            "exit_time": _iso_utc(exit_bar.observed_at),
            "exit_price": exit_price,
            "holding_duration_minutes": _elapsed_minutes(
                entry_bar.observed_at, exit_bar.observed_at
            ),
            "gross_return_pct": gross_return,
            "benchmark_symbol": PRIMARY_BENCHMARK,
            "benchmark_return_pct": benchmark_return,
            "benchmark_source": benchmark_evidence.get("source"),
            "benchmark_source_url": benchmark_evidence.get("source_url"),
            "benchmark_source_bar_hash_sha256": benchmark_evidence.get("source_bar_hash_sha256"),
            "benchmark_independent_reconciliation_status": benchmark_evidence.get(
                "independent_reconciliation_status"
            ),
            "secondary_benchmark_symbol": SECONDARY_BENCHMARK,
            "secondary_benchmark_return_pct": secondary_benchmark_return,
            "secondary_benchmark_source": secondary_benchmark_evidence.get("source"),
            "secondary_benchmark_source_url": secondary_benchmark_evidence.get("source_url"),
            "secondary_benchmark_source_bar_hash_sha256": (
                secondary_benchmark_evidence.get("source_bar_hash_sha256")
            ),
            "secondary_benchmark_independent_reconciliation_status": (
                secondary_benchmark_evidence.get("independent_reconciliation_status")
            ),
            "attribution_complete": benchmark_complete,
            "benchmark_contract_complete": benchmark_complete,
            "outcome_status": (
                "complete_sourced" if benchmark_complete else "ineligible_missing_benchmark"
            ),
            "learning_eligible": benchmark_complete,
            "learning_contract": "sampled_rejected_open_to_close_regret_v1",
            "validated_against_signal_timestamp": True,
            "counterfactual_rejected_candidate": True,
            "counterfactual_policy": "OPEN_TO_CLOSE_V1",
            "notes": (
                "Research-only sampled rejected-candidate counterfactual. This is not a "
                "simulated V5 trade and cannot enter the official paper scorecard."
            ),
        }
    )
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
    base = {
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
        "provider_chain_exhausted": bool(source_evidence.get("provider_chain_exhausted")),
        "independent_source_reconciliation": source_evidence.get("independent_reconciliation"),
        "independent_reconciliation_status": source_evidence.get(
            "independent_reconciliation_status"
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
    # Carry the immutable merged-row contributor receipts into every outcome
    # and learning payload.  This preserves the native Alpha primary identity
    # while keeping authenticated adapter contributors attributable after the
    # official-selection -> watcher -> outcome transition.
    for field in (
        "strategy_contributors",
        "strategy_contributor_count",
        "strategy_contributor_ids",
        "strategy_decision_receipts",
        "canonical_primary_strategy_id",
        "strategy_contribution_status",
    ):
        if field in signal:
            base[field] = copy.deepcopy(signal[field])
    return base


def _conclusive_without_entry(
    base: dict[str, Any],
    *,
    status: str,
    trigger: float,
    note: str,
) -> dict[str, Any]:
    base.update(
        {
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
        }
    )
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
    base.update(
        {
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
        }
    )
    base["payload_json"] = dict(base)
    return base


def _ineligible(base: dict[str, Any], status: str, detail: str = "") -> dict[str, Any]:
    base.update(
        {
            "outcome_status": status,
            "learning_eligible": False,
            "validated_against_signal_timestamp": False,
            "notes": detail or status.replace("_", " "),
        }
    )
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
        row_ticker = str(row.get("ticker") or "").strip().upper()
        if not row_ticker:
            raise ValueError("provider bar row lacks an explicit ticker binding")
        if row_ticker != ticker.upper():
            raise ValueError("provider bar row ticker conflicts with requested ticker")
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
        (bar for bar in bars if value <= bar.observed_at <= value + timedelta(seconds=90)),
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
        "catalyst_class": catalyst or ("sourced_unspecified" if catalyst_summary else "missing"),
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
        "sector_regime": (raw.get("sector_regime") or signal.get("sector_regime") or "unknown"),
        "source_confidence": confidence,
        "source_confidence_bucket": _confidence_bucket(confidence),
        "vetoes": _tokens(signal.get("avoid_reasons_json") or raw.get("avoid_reasons")),
        "risk_flags": _tokens(signal.get("risk_flags_json") or raw.get("risk_flags")),
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
    base.update(
        {
            "fill_status": "not_filled_official_policy",
            "non_fill_reason": "no_eligible_enter_long_intent",
            "execution_policy_version": DEFAULT_V5_POLICY.policy_version,
            "cost_model_version": DEFAULT_V5_POLICY.cost_model_version,
        }
    )
    if entry_intent is None:
        return base
    trace = dict(entry_intent.get("decision_trace") or {})
    computed = dict(trace.get("computed") or {})
    quantity = _float(entry_intent.get("quantity"))
    expected_entry = _first_float(
        computed.get("expected_entry_price"),
        entry_intent.get("decision_price"),
    )
    expected_exit = raw_exit * (1.0 - DEFAULT_V5_POLICY.exit_slippage_bps / 10_000.0)
    modeled_fees = (
        quantity * DEFAULT_V5_POLICY.commission_per_share_per_side * 2
        if quantity is not None
        else None
    )
    modeled_slippage = (
        (abs((expected_entry or raw_entry) - trigger) + abs(raw_exit - expected_exit)) * quantity
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
        if (entry_fill is not None and exit_fill is not None and quantity is not None)
        else None
    )
    base.update(
        {
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
                round(realized_slippage, 4) if realized_slippage is not None else None
            ),
            "execution_policy_version": (
                entry_intent.get("execution_policy_version") or DEFAULT_V5_POLICY.policy_version
            ),
            "cost_model_version": (
                entry_intent.get("cost_model_version") or DEFAULT_V5_POLICY.cost_model_version
            ),
            "decision_fingerprint": (
                entry_intent.get("decision_fingerprint") or trace.get("decision_fingerprint")
            ),
        }
    )
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
        (item for item in bars if lunch_at <= item.observed_at <= lunch_at + timedelta(seconds=90)),
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
    attribution_missing = bool(outcome is not None and outcome.get("attribution_complete") is False)
    terminal_missing = outcome_status.startswith("ineligible_") or attribution_missing
    status = "terminal_missing" if terminal_missing else "resolved"
    relevant_requests = [
        dict(row) for row in source_requests if str(row.get("ticker") or "").upper() == ticker
    ]
    identity = ":".join(
        (
            market_date,
            signal_id,
            _iso_utc(requested_at),
            outcome_status,
            source_hash,
        )
    )
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
    output_dir.mkdir(parents=True, exist_ok=True)
    source_bar_artifact = {
        "schema_version": "dawnstrike.alpha_outcome_source_bars.v1",
        "market_date": str(summary.get("market_date") or "")[:10],
        "bars_by_ticker": source_bars,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    source_bar_artifact_bytes = json.dumps(
        source_bar_artifact,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    source_bar_artifact_hash = hashlib.sha256(source_bar_artifact_bytes).hexdigest()
    source_bar_artifact_path = output_dir / (
        f"alpha_outcome_source_bars-{source_bar_artifact_hash}.json"
    )
    if source_bar_artifact_path.exists():
        if source_bar_artifact_path.read_bytes() != source_bar_artifact_bytes:
            raise SnapshotValidationError(
                "content-addressed source-bar artifact conflicts with existing bytes"
            )
    else:
        source_bar_artifact_path.write_bytes(source_bar_artifact_bytes)
    summary["source_bar_artifact"] = {
        "artifact_path": str(source_bar_artifact_path),
        "artifact_hash_sha256": source_bar_artifact_hash,
        "content_addressed": True,
        "mutable_alias_authoritative": False,
    }
    _write_json(output_dir / "alpha_outcome_capture.json", summary)
    _write_json(output_dir / "alpha_sourced_outcomes.json", outcomes)
    _write_json(output_dir / "alpha_outcome_capture_diagnostics.json", diagnostics)
    # Backward-compatible operator diagnostic only.  Certification uses the
    # content-addressed artifact above and the exact per-bridge bar payload.
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


def _existing_diagnostic(signal: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
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
        if not all(math.isfinite(value) and value > 0 for value in (open_price, high, low, close)):
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
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _future_evidence_receipt(
    bars: list[OutcomeBar],
    *,
    symbol: str,
    market_date: str,
    raw_artifact_identity: str,
    coverage_start: datetime,
    coverage_end: datetime,
    coverage_complete: bool,
) -> dict[str, object]:
    canonical_bars = [
        {
            "observed_at": bar.observed_at.astimezone(UTC).isoformat(),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
        }
        for bar in sorted(bars, key=lambda item: item.observed_at)
        if bar.open is not None and bar.high is not None and bar.low is not None
    ]
    if len(canonical_bars) != len(bars) or not canonical_bars:
        raise ValueError("future evidence bars are incomplete")
    body: dict[str, object] = {
        "schema_version": FUTURE_EVIDENCE_SCHEMA_VERSION,
        "subject": {"symbol": symbol, "market_date": market_date},
        "raw_artifact_identity": raw_artifact_identity,
        "raw_bar_hash_sha256": _canonical_payload_hash(canonical_bars),
        "bar_count": len(canonical_bars),
        "first_bar_at": canonical_bars[0]["observed_at"],
        "last_bar_at": canonical_bars[-1]["observed_at"],
        "coverage_start": coverage_start.astimezone(UTC).isoformat(),
        "coverage_end": coverage_end.astimezone(UTC).isoformat(),
        "coverage_complete": coverage_complete,
    }
    digest = _canonical_payload_hash(body)
    return {
        **body,
        "receipt_id": f"future-evidence-v1-{digest}",
        "receipt_hash_sha256": digest,
    }


def _canonical_payload_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_compatibility_projection(
    projection: dict[str, Any],
    *,
    trigger: float,
    target: float,
    invalidation: float,
    benchmark_evidence: dict[str, Any],
    secondary_benchmark_evidence: dict[str, Any],
    classification: str,
) -> dict[str, Any]:
    entry_at = _parse_datetime(str(projection.get("entry_time") or ""))
    exit_at = _parse_datetime(str(projection.get("exit_time") or ""))
    mfe_at = _parse_datetime(str(projection.get("mfe_at") or ""))
    mae_at = _parse_datetime(str(projection.get("mae_at") or ""))
    path_event = str(projection.get("path_event") or "")
    exit_reason = {
        "TARGET": "target_1",
        "STOP": "invalidation",
        "TIMEOUT": "session_close",
    }.get(path_event)
    activated = projection.get("entry_price") is not None
    return {
        "entry_opportunity": activated,
        "entry_trigger": trigger,
        "entry_price_policy": "canonical_path_replay_v2",
        "fill_status": (
            "canonical_path_activated"
            if activated
            else "not_filled_no_trigger"
            if classification == CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED
            else "not_filled_censored"
        ),
        "non_fill_reason": None if activated else projection.get("path_truth_status"),
        "target_price": target,
        "invalidation_price": invalidation,
        "target_touched_at": projection.get("target_touched_at"),
        "invalidation_touched_at": projection.get("stop_touched_at"),
        "planned_first_touch_outcome": (
            "target" if path_event == "TARGET" else "invalidation" if path_event == "STOP" else None
        ),
        "exit_reason": exit_reason,
        "holding_duration_minutes": (
            _elapsed_minutes(entry_at, exit_at)
            if entry_at is not None and exit_at is not None
            else None
        ),
        "close_price": projection.get("exit_price"),
        "close_price_observed_at": projection.get("exit_time"),
        "high_after_entry": projection.get("mfe_price"),
        "high_after_entry_observed_at": projection.get("mfe_at"),
        "low_after_entry": projection.get("mae_price"),
        "low_after_entry_observed_at": projection.get("mae_at"),
        "time_to_mfe_minutes": (
            _elapsed_minutes(entry_at, mfe_at)
            if entry_at is not None and mfe_at is not None
            else None
        ),
        "time_to_mae_minutes": (
            _elapsed_minutes(entry_at, mae_at)
            if entry_at is not None and mae_at is not None
            else None
        ),
        "price_1m": None,
        "price_1m_observed_at": None,
        "price_5m": None,
        "price_5m_observed_at": None,
        "price_15m": None,
        "price_15m_observed_at": None,
        "lunch_price": None,
        "lunch_price_observed_at": None,
        "excess_return_pct": projection.get("net_excess_return_pct"),
        "benchmark_source": benchmark_evidence.get("source"),
        "benchmark_source_url": benchmark_evidence.get("source_url"),
        "secondary_benchmark_source": secondary_benchmark_evidence.get("source"),
        "secondary_benchmark_source_url": secondary_benchmark_evidence.get("source_url"),
        "attribution_complete": classification == CURRENT_RETURN_TRUTH,
        "benchmark_contract_complete": classification == CURRENT_RETURN_TRUTH,
        "first_touch_precision": projection.get("event_time_precision"),
        "return_learning_eligible": classification == CURRENT_RETURN_TRUTH,
        "learning_contract": "canonical_return_truth_v2",
    }


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
    return [token.strip() for token in text.replace(",", ";").split(";") if token.strip()]
