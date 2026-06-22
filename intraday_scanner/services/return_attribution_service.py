"""Historical signal ledger and paper return attribution services."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Any

from intraday_scanner.config import load_config
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.models import utc_now_iso, validate_required_columns
from intraday_scanner.notifiers import (
    ConsoleNotifier,
    NotificationEvent,
    build_notifiers,
    dispatch_events,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

OUTCOME_COLUMNS = [
    "date",
    "ticker",
    "entry_time",
    "entry_price",
    "price_1m",
    "price_5m",
    "price_15m",
    "lunch_price",
    "close_price",
    "high_after_entry",
    "low_after_entry",
    "halted",
    "source",
    "notes",
]

SIGNAL_EVENT_ENTRY = "ENTRY_WATCH_CREATED"
SIGNAL_EVENT_NO_EDGE = "NO_CLEAN_EDGE_CREATED"
SIGNAL_EVENT_TELEGRAM = "TELEGRAM_SENT"
SIGNAL_EVENT_OUTCOME = "OUTCOME_IMPORTED"
SIGNAL_EVENT_AUDITED = "AUDITED"

ENTRY_POLICY = "first_available_after_signal"
SCENARIO_POLICIES = {
    "one_min": "price_1m",
    "five_min": "price_5m",
    "fifteen_min": "price_15m",
    "lunch": "lunch_price",
    "close": "close_price",
    "high_opportunity": "high_after_entry",
}


def record_alpha_historical_signals(
    store: SQLiteScanStore,
    signals: list[dict[str, Any]],
    *,
    source_summary: dict[str, Any] | None = None,
    no_trade_reason: str = "",
) -> list[dict[str, Any]]:
    """Persist AlphaOps signals to the permanent historical ledger."""
    rows = [
        _historical_signal_from_alpha(
            row,
            source_summary=source_summary or {},
            no_trade_reason=no_trade_reason,
        )
        for row in signals
    ]
    if not rows:
        return []
    store.persist_historical_signals(rows)
    store.persist_signal_events([
            _signal_event(
                signal_id=str(row["signal_id"]),
            event_type=(
                SIGNAL_EVENT_NO_EDGE
                if row.get("signal_label") == "NO CLEAN EDGE"
                else SIGNAL_EVENT_ENTRY
            ),
            event_timestamp=str(row.get("generated_at") or utc_now_iso()),
            source="alphaops",
            notes=_created_note(row),
            payload=row,
        )
        for row in rows
    ])
    return rows


def record_no_trade_historical_signal(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    generated_at: str,
    reason: str,
    source_summary: dict[str, Any] | None = None,
    candidate_count: int = 0,
) -> dict[str, Any]:
    """Persist a day-level no-clean-edge decision when no watchlist is produced."""
    source_summary = dict(source_summary or {})
    market_date = _date_key(generated_at)
    row = {
        "signal_id": f"no_trade:{scan_id}:{market_date}",
        "scan_id": scan_id,
        "alpha_signal_id": "",
        "generated_at": generated_at,
        "market_date": market_date,
        "ticker": "NO_TRADE",
        "company": "",
        "rank": 0,
        "source": str(source_summary.get("primary_source") or source_summary.get("source") or ""),
        "source_url": "",
        "source_confidence": _optional_float(source_summary.get("source_confidence")),
        "data_source_kind": str(source_summary.get("data_source_kind") or "public_free_shadow"),
        "model_version": "dawnstrike-alphaops-v4",
        "config_hash": "",
        "primary_setup": "",
        "setup_grade": "",
        "signal_label": "NO CLEAN EDGE",
        "entry_watch_level": None,
        "entry_trigger_type": "none",
        "entry_condition": "",
        "confirmation_condition": "",
        "exit_line": None,
        "invalidation_level": None,
        "target_1": None,
        "target_2": None,
        "risk_flags_json": [],
        "avoid_reasons_json": [reason] if reason else [],
        "catalyst_summary": "",
        "telegram_event_key": "",
        "was_alerted": False,
        "no_trade_reason": reason,
        "raw_payload_json": {
            "reason": reason,
            "source_summary": source_summary,
            "candidate_count": candidate_count,
            "research_only": True,
        },
    }
    store.persist_historical_signals([row])
    store.persist_signal_events([
        _signal_event(
            signal_id=str(row["signal_id"]),
            event_type=SIGNAL_EVENT_NO_EDGE,
            event_timestamp=generated_at,
            source="alphaops",
            notes=reason,
            payload=row,
        )
    ])
    return row


def link_historical_notification(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    event_key: str,
    was_alerted: bool,
    channel: str,
) -> dict[str, Any]:
    """Link a sent notification event back to all historical signals for a scan."""
    updated = store.link_historical_signal_notification(
        scan_id=scan_id,
        telegram_event_key=event_key,
        was_alerted=was_alerted,
    )
    signals = store.load_historical_signals(scan_id=scan_id, limit=500)
    if signals:
        store.persist_signal_events([
            _signal_event(
                signal_id=str(row["signal_id"]),
                event_type=SIGNAL_EVENT_TELEGRAM,
                event_timestamp=utc_now_iso(),
                source=channel,
                notes="Notification linked to historical signal.",
                payload={
                    "event_key": event_key,
                    "channel": channel,
                    "was_alerted": was_alerted,
                },
            )
            for row in signals
        ])
    return {"updated": updated, "signal_count": len(signals)}


def record_monitor_signal_events(
    store: SQLiteScanStore,
    *,
    signals: list[dict[str, Any]],
    monitor_events: list[dict[str, Any]],
) -> dict[str, int]:
    """Persist monitor readouts as historical signal events when prices exist."""
    signal_by_ticker = {str(row.get("ticker") or "").upper(): row for row in signals}
    historical = {
        (str(row.get("scan_id") or ""), str(row.get("ticker") or "").upper()): row
        for row in store.load_historical_signals(limit=5000)
    }
    rows: list[dict[str, Any]] = []
    for event in monitor_events:
        ticker = str(event.get("ticker") or "").upper()
        source_signal = signal_by_ticker.get(ticker, {})
        hist = historical.get((str(source_signal.get("scan_id") or ""), ticker))
        if not hist:
            continue
        event_type = _monitor_event_type(event)
        rows.append(
            _signal_event(
                signal_id=str(hist["signal_id"]),
                event_type=event_type,
                event_timestamp=str(event.get("created_at") or utc_now_iso()),
                event_price=_optional_float(
                    event.get("current_price") or event.get("exit_price") or event.get("price")
                ),
                source="alpha_monitor",
                notes=str(event.get("label") or event.get("status") or ""),
                payload=event,
            )
        )
    return store.persist_signal_events(rows) if rows else {"inserted": 0, "skipped": 0}


def import_historical_outcomes(
    *,
    input_path: str | Path,
    store: SQLiteScanStore,
    persist: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    rows = _read_csv(input_path)
    if not rows:
        raise SnapshotValidationError(f"{input_path} has no outcome rows")
    validate_required_columns(set(rows[0]), OUTCOME_COLUMNS, str(input_path))
    historical = store.load_historical_signals(limit=50000)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        try:
            accepted.append(_normalize_historical_outcome(row, historical))
        except SnapshotValidationError as exc:
            rejected.append({**row, "reject_reason": str(exc)})
    rejected_path = Path(input_path).with_name("rejected_outcomes.csv")
    if rejected:
        _write_csv(rejected_path, rejected)
    stats = {"inserted": 0, "skipped": 0}
    if persist and accepted:
        stats = store.persist_signal_outcomes(accepted, replace=replace)
        store.persist_signal_events([
            _signal_event(
                signal_id=str(row["signal_id"]),
                event_type=SIGNAL_EVENT_OUTCOME,
                event_timestamp=str(row["imported_at"]),
                event_price=row.get("entry_price"),
                source=str(row.get("outcome_source") or ""),
                notes="Manual outcome imported.",
                payload=row,
            )
            for row in accepted
        ])
    missing = _missing_outcome_tickers(accepted, historical)
    result = {
        "created_at": utc_now_iso(),
        "input_path": str(input_path),
        "row_count": len(rows),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "rejected_path": str(rejected_path) if rejected else "",
        "missing_tickers": missing,
        **stats,
        "rows": accepted,
        "rejected_rows": rejected,
    }
    if rejected and not accepted:
        raise SnapshotValidationError(
            f"No valid outcome rows imported. Rejected rows written to {rejected_path}."
        )
    return result


def attribute_returns(
    *,
    db_path: str | Path = "data/shadow_real.sqlite",
    out_dir: str | Path = "outputs/return_attribution",
    persist: bool = False,
    notify: str = "",
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    store.initialize()
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = sorted(
        store.load_historical_signals(limit=50000),
        key=lambda row: (str(row.get("market_date") or ""), int(row.get("rank") or 999999)),
    )
    outcomes = {str(row.get("signal_id") or ""): row for row in store.load_signal_outcomes()}
    events_by_signal = _events_by_signal(store.load_signal_events(limit=50000))
    attribution_rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for signal in signals:
        if _is_no_trade_signal(signal):
            continue
        outcome = outcomes.get(str(signal.get("signal_id") or ""))
        if not outcome:
            missing.append(_missing_outcome(signal))
            attribution_rows.append(_missing_attribution(signal))
            continue
        attribution_rows.extend(_attribution_for_signal(signal, outcome, events_by_signal))
    daily = _daily_performance(signals, attribution_rows)
    curve = _cumulative_curve(daily)
    summary = _attribution_summary(signals, attribution_rows, daily, missing)
    paths = {
        "signal_return_attribution": output_dir / "signal_return_attribution.csv",
        "daily_signal_performance": output_dir / "daily_signal_performance.csv",
        "cumulative_equity_curve": output_dir / "cumulative_equity_curve.csv",
        "missing_outcomes": output_dir / "missing_outcomes.csv",
        "attribution_summary": output_dir / "attribution_summary.json",
        "attribution_report": output_dir / "attribution_report.md",
    }
    _write_csv(paths["signal_return_attribution"], attribution_rows)
    _write_csv(paths["daily_signal_performance"], daily)
    _write_csv(paths["cumulative_equity_curve"], curve)
    _write_csv(paths["missing_outcomes"], missing)
    paths["attribution_summary"].write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _write_attribution_markdown(paths["attribution_report"], summary, missing)
    if persist:
        store.persist_signal_return_attribution(attribution_rows)
        store.persist_daily_signal_performance(daily)
        audited_signal_ids = sorted(
            {
                str(row.get("signal_id") or "")
                for row in attribution_rows
                if row.get("audit_status") == "audited"
            }
        )
        store.persist_signal_events([
            _signal_event(
                signal_id=signal_id,
                event_type=SIGNAL_EVENT_AUDITED,
                event_timestamp=summary["created_at"],
                source="return_attribution",
                notes="Signal return attribution calculated.",
                payload={"out_dir": str(output_dir)},
            )
            for signal_id in audited_signal_ids
        ])
    notification_stats = _send_accuracy_summary(
        store=store,
        db_path=db_path,
        notify=notify,
        summary=summary,
        missing=missing,
    )
    return {
        "status": "complete",
        "db_path": str(db_path),
        "out_dir": str(output_dir),
        "signal_count": len(signals),
        "attribution_count": len(attribution_rows),
        "daily_count": len(daily),
        "missing_outcome_count": len(missing),
        "summary": summary,
        "notification_stats": notification_stats,
        "paths": {key: str(value) for key, value in paths.items()},
    }


def historical_report(
    *,
    db_path: str | Path = "data/shadow_real.sqlite",
    out_dir: str | Path = "outputs/historical_report",
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    store.initialize()
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = store.load_historical_signals(start=start, end=end, limit=50000)
    events = store.load_signal_events(limit=50000)
    outcomes = store.load_signal_outcomes(start=start, end=end, limit=50000)
    attribution = store.load_signal_return_attribution(start=start, end=end, limit=50000)
    daily = store.load_daily_signal_performance(start=start, end=end, limit=10000)
    if not attribution and signals:
        computed = attribute_returns(
            db_path=db_path,
            out_dir=output_dir / "_computed_attribution",
            persist=False,
        )
        attribution = list(computed["summary"].get("attribution_rows") or [])
        daily = list(computed["summary"].get("daily_rows") or [])
    curve = _cumulative_curve(daily)
    missing = [_missing_outcome(row) for row in signals if _needs_outcome(row, outcomes)]
    accuracy_setup = _bucket_accuracy(signals, attribution, "primary_setup")
    accuracy_source = _bucket_accuracy(signals, attribution, "source")
    accuracy_score = _bucket_accuracy(signals, attribution, "score_bucket")
    report = _historical_report_payload(
        signals=signals,
        events=events,
        outcomes=outcomes,
        attribution=attribution,
        daily=daily,
        missing=missing,
        accuracy_setup=accuracy_setup,
        accuracy_source=accuracy_source,
        accuracy_score=accuracy_score,
    )
    paths = {
        "historical_signals": output_dir / "historical_signals.csv",
        "historical_signal_events": output_dir / "historical_signal_events.csv",
        "historical_signal_outcomes": output_dir / "historical_signal_outcomes.csv",
        "return_attribution": output_dir / "return_attribution.csv",
        "daily_performance": output_dir / "daily_performance.csv",
        "cumulative_equity_curve": output_dir / "cumulative_equity_curve.csv",
        "accuracy_by_setup": output_dir / "accuracy_by_setup.csv",
        "accuracy_by_source": output_dir / "accuracy_by_source.csv",
        "accuracy_by_score_bucket": output_dir / "accuracy_by_score_bucket.csv",
        "missing_outcomes": output_dir / "missing_outcomes.csv",
        "historical_report_md": output_dir / "historical_report.md",
        "historical_report_json": output_dir / "historical_report.json",
    }
    _write_csv(paths["historical_signals"], [_flatten_signal(row) for row in signals])
    _write_csv(paths["historical_signal_events"], events)
    _write_csv(paths["historical_signal_outcomes"], outcomes)
    _write_csv(paths["return_attribution"], attribution)
    _write_csv(paths["daily_performance"], daily)
    _write_csv(paths["cumulative_equity_curve"], curve)
    _write_csv(paths["accuracy_by_setup"], accuracy_setup)
    _write_csv(paths["accuracy_by_source"], accuracy_source)
    _write_csv(paths["accuracy_by_score_bucket"], accuracy_score)
    _write_csv(paths["missing_outcomes"], missing)
    paths["historical_report_json"].write_text(
        json.dumps(report, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _write_historical_markdown(paths["historical_report_md"], report)
    return {
        "status": "complete",
        "db_path": str(db_path),
        "out_dir": str(output_dir),
        "signal_count": len(signals),
        "outcome_count": len(outcomes),
        "attribution_count": len(attribution),
        "audited_day_count": report["audited_day_count"],
        "evidence_status": report["evidence_status"],
        "missing_outcome_count": len(missing),
        "paths": {key: str(value) for key, value in paths.items()},
    }


def _historical_signal_from_alpha(
    row: dict[str, Any],
    *,
    source_summary: dict[str, Any],
    no_trade_reason: str,
) -> dict[str, Any]:
    generated_at = str(row.get("timestamp") or row.get("as_of_timestamp") or utc_now_iso())
    ticker = str(row.get("ticker") or "").upper()
    reason = no_trade_reason or str(row.get("no_trade_reason") or "")
    signal_label = _signal_label(row, reason)
    trigger = _optional_float(row.get("entry_trigger") or row.get("breakout_trigger"))
    invalidation = _optional_float(row.get("invalidation") or row.get("invalidation_level"))
    target_1 = _optional_float(row.get("target_1") or row.get("first_target"))
    signal_id = str(
        row.get("signal_key") or f"{row.get('scan_id')}:{row.get('rank')}:{ticker}"
    )
    return {
        "signal_id": signal_id,
        "scan_id": str(row.get("scan_id") or ""),
        "alpha_signal_id": str(row.get("signal_key") or ""),
        "generated_at": generated_at,
        "market_date": _date_key(generated_at),
        "ticker": ticker,
        "company": str(row.get("company") or row.get("name") or ""),
        "rank": _optional_int(row.get("rank")),
        "source": str(row.get("preferred_source") or row.get("source") or ""),
        "source_url": str(row.get("source_url") or row.get("catalyst_url") or ""),
        "source_confidence": _optional_float(row.get("source_confidence")),
        "data_source_kind": str(
            row.get("data_source_kind")
            or source_summary.get("data_source_kind")
            or "public_free_shadow"
        ),
        "model_version": str(row.get("model_version") or ""),
        "config_hash": str(row.get("config_hash") or row.get("feature_config_hash") or ""),
        "primary_setup": str(row.get("primary_setup") or row.get("setup_key") or ""),
        "setup_grade": str(row.get("setup_grade") or ""),
        "signal_label": signal_label,
        "entry_watch_level": trigger,
        "entry_trigger_type": "breakout_confirmation" if trigger is not None else "not_recorded",
        "entry_condition": str(
            row.get("entry_condition")
            or f"Watch only if price confirms above {trigger}."
            if trigger is not None
            else ""
        ),
        "confirmation_condition": str(
            row.get("confirmation_condition")
            or "Prefer sustained volume and clean holds above the trigger."
        ),
        "exit_line": invalidation,
        "invalidation_level": invalidation,
        "target_1": target_1,
        "target_2": _optional_float(row.get("target_2") or row.get("stretch_target")),
        "risk_flags_json": _split_flags(row.get("risk_flags")),
        "avoid_reasons_json": _split_flags(row.get("avoid_reasons") or reason),
        "catalyst_summary": str(
            row.get("catalyst_summary") or row.get("catalyst_headline") or ""
        ),
        "telegram_event_key": str(row.get("telegram_key") or ""),
        "was_alerted": bool(row.get("alert_sent")),
        "no_trade_reason": reason,
        "raw_payload_json": row,
    }


def _signal_label(row: dict[str, Any], reason: str) -> str:
    if reason or not row.get("can_alert", True):
        return "NO CLEAN EDGE"
    edge = str(row.get("edge_bucket") or "").upper()
    trigger = _optional_float(row.get("entry_trigger") or row.get("breakout_trigger"))
    if trigger is not None and edge in {"HIGH", "MEDIUM"}:
        return "BREAKOUT WATCH"
    if trigger is not None:
        return "ENTRY WATCH"
    return "WATCH ONLY"


def _created_note(row: dict[str, Any]) -> str:
    if row.get("signal_label") == "NO CLEAN EDGE":
        return str(row.get("no_trade_reason") or "No clean edge.")
    return (
        f"{row.get('signal_label')} for {row.get('ticker')} at "
        f"{row.get('entry_watch_level') or 'not recorded'}."
    )


def _signal_event(
    *,
    signal_id: str,
    event_type: str,
    event_timestamp: str,
    source: str,
    notes: str,
    payload: dict[str, Any],
    event_price: Any | None = None,
) -> dict[str, Any]:
    return {
        "event_id": _event_id(signal_id, event_type, event_timestamp, source, payload),
        "signal_id": signal_id,
        "event_type": event_type,
        "event_timestamp": event_timestamp,
        "event_price": _optional_float(event_price),
        "source": source,
        "notes": notes,
        "payload_json": payload,
    }


def _event_id(
    signal_id: str,
    event_type: str,
    event_timestamp: str,
    source: str,
    payload: dict[str, Any],
) -> str:
    ticker = str(payload.get("ticker") or payload.get("event_key") or "")
    return f"{signal_id}:{event_type}:{event_timestamp}:{source}:{ticker}"


def _normalize_historical_outcome(
    row: dict[str, Any],
    historical_signals: list[dict[str, Any]],
) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "").upper().strip()
    market_date = str(row.get("date") or "")[:10]
    entry_time = _normalize_entry_time(market_date, str(row.get("entry_time") or ""))
    signal = _match_historical_signal(ticker, market_date, entry_time, historical_signals)
    imported_at = utc_now_iso()
    outcome = {
        "signal_id": signal["signal_id"],
        "market_date": market_date,
        "date": market_date,
        "ticker": ticker,
        "scan_id": signal.get("scan_id") or "",
        "rank": signal.get("rank"),
        "recommendation_timestamp": signal.get("generated_at") or "",
        "outcome_source": str(row.get("source") or "manual_outcome_upload"),
        "entry_time": entry_time,
        "entry_price": _optional_float(row.get("entry_price")),
        "price_1m": _optional_float(row.get("price_1m")),
        "price_5m": _optional_float(row.get("price_5m")),
        "price_15m": _optional_float(row.get("price_15m")),
        "lunch_price": _optional_float(row.get("lunch_price")),
        "close_price": _optional_float(row.get("close_price")),
        "high_after_entry": _optional_float(row.get("high_after_entry")),
        "low_after_entry": _optional_float(row.get("low_after_entry")),
        "halted": _bool(row.get("halted")),
        "notes": str(row.get("notes") or ""),
        "imported_at": imported_at,
        "validated_against_signal_timestamp": True,
        "outcome_status": _outcome_status(row),
        "manual_uploaded_data": True,
        "paid_data": False,
    }
    outcome["payload_json"] = dict(outcome)
    return outcome


def _match_historical_signal(
    ticker: str,
    market_date: str,
    entry_time: str,
    historical_signals: list[dict[str, Any]],
) -> dict[str, Any]:
    same_day = [
        row
        for row in historical_signals
        if str(row.get("ticker") or "").upper() == ticker
        and str(row.get("market_date") or "") == market_date
        and not _is_no_trade_signal(row)
    ]
    if not same_day:
        raise SnapshotValidationError(
            f"No historical signal exists for {ticker} on {market_date}"
        )
    valid = [
        row
        for row in same_day
        if _parse_time(entry_time) >= _parse_time(str(row.get("generated_at") or ""))
    ]
    if not valid:
        first = min(str(row.get("generated_at") or "") for row in same_day)
        raise SnapshotValidationError(
            f"{ticker} outcome entry_time {entry_time} is before recommendation {first}"
        )
    return sorted(valid, key=lambda row: str(row.get("generated_at") or ""), reverse=True)[0]


def _attribution_for_signal(
    signal: dict[str, Any],
    outcome: dict[str, Any],
    events_by_signal: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    entry = _optional_float(outcome.get("entry_price"))
    rows = []
    for exit_policy, field in SCENARIO_POLICIES.items():
        exit_price = _optional_float(outcome.get(field))
        rows.append(
            _attribution_row(
                signal,
                outcome,
                entry_policy=ENTRY_POLICY,
                exit_policy=exit_policy,
                entry_price=entry,
                exit_price=exit_price,
                scenario_or_recommended="scenario",
                audit_status=(
                    "audited"
                    if entry is not None and exit_price is not None
                    else "unavailable"
                ),
            )
        )
    rows.append(_target_row(signal, outcome, "target_1"))
    rows.append(_target_row(signal, outcome, "target_2"))
    rows.append(_invalidation_row(signal, outcome))
    monitor = _monitor_exit_row(signal, outcome, events_by_signal)
    if monitor:
        rows.append(monitor)
    trigger = _trigger_row(signal, outcome)
    if trigger:
        rows.extend(trigger)
    return rows


def _target_row(signal: dict[str, Any], outcome: dict[str, Any], target_key: str) -> dict[str, Any]:
    entry = _optional_float(outcome.get("entry_price"))
    target = _optional_float(signal.get(target_key))
    high = _optional_float(outcome.get("high_after_entry"))
    hit = target is not None and high is not None and high >= target
    return _attribution_row(
        signal,
        outcome,
        entry_policy=ENTRY_POLICY,
        exit_policy=target_key,
        entry_price=entry,
        exit_price=target if hit else None,
        scenario_or_recommended="scenario",
        audit_status="audited" if hit else "unavailable",
    )


def _invalidation_row(signal: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    entry = _optional_float(outcome.get("entry_price"))
    invalidation = _optional_float(signal.get("invalidation_level") or signal.get("exit_line"))
    low = _optional_float(outcome.get("low_after_entry"))
    hit = invalidation is not None and low is not None and low <= invalidation
    return _attribution_row(
        signal,
        outcome,
        entry_policy=ENTRY_POLICY,
        exit_policy="invalidation",
        entry_price=entry,
        exit_price=invalidation if hit else None,
        scenario_or_recommended="scenario",
        audit_status="audited" if hit else "unavailable",
    )


def _monitor_exit_row(
    signal: dict[str, Any],
    outcome: dict[str, Any],
    events_by_signal: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    events = [
        row
        for row in events_by_signal.get(str(signal.get("signal_id") or ""), [])
        if row.get("event_type") in {"EXIT_SIGNAL", "INVALIDATED", "THESIS_BROKEN"}
    ]
    if not events:
        return None
    event = sorted(events, key=lambda row: str(row.get("event_timestamp") or ""))[0]
    return _attribution_row(
        signal,
        outcome,
        entry_policy=ENTRY_POLICY,
        exit_policy="monitor_exit_signal",
        entry_price=_optional_float(outcome.get("entry_price")),
        exit_price=_optional_float(event.get("event_price")),
        scenario_or_recommended="recommended",
        audit_status=(
            "audited"
            if _optional_float(event.get("event_price")) is not None
            else "unavailable"
        ),
    )


def _trigger_row(signal: dict[str, Any], outcome: dict[str, Any]) -> list[dict[str, Any]]:
    trigger = _optional_float(signal.get("entry_watch_level"))
    high = _optional_float(outcome.get("high_after_entry"))
    if trigger is None or high is None or high < trigger:
        return []
    rows = []
    for exit_policy, field in {
        "five_min": "price_5m",
        "lunch": "lunch_price",
        "close": "close_price",
    }.items():
        rows.append(
            _attribution_row(
                signal,
                outcome,
                entry_policy="trigger_touch",
                exit_policy=exit_policy,
                entry_price=trigger,
                exit_price=_optional_float(outcome.get(field)),
                scenario_or_recommended="scenario",
                audit_status=(
                    "audited"
                    if _optional_float(outcome.get(field)) is not None
                    else "unavailable"
                ),
            )
        )
    return rows


def _attribution_row(
    signal: dict[str, Any],
    outcome: dict[str, Any],
    *,
    entry_policy: str,
    exit_policy: str,
    entry_price: float | None,
    exit_price: float | None,
    scenario_or_recommended: str,
    audit_status: str,
) -> dict[str, Any]:
    signal_id = str(signal.get("signal_id") or "")
    ret = _return_pct(entry_price, exit_price)
    high = _optional_float(outcome.get("high_after_entry"))
    low = _optional_float(outcome.get("low_after_entry"))
    target_1 = _optional_float(signal.get("target_1"))
    target_2 = _optional_float(signal.get("target_2"))
    invalidation = _optional_float(signal.get("invalidation_level") or signal.get("exit_line"))
    trigger = _optional_float(signal.get("entry_watch_level"))
    row = {
        "attribution_id": f"{signal_id}:{entry_policy}:{exit_policy}",
        "signal_id": signal_id,
        "ticker": signal.get("ticker"),
        "market_date": signal.get("market_date"),
        "rank": signal.get("rank"),
        "entry_policy": entry_policy,
        "exit_policy": exit_policy,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "return_pct": ret,
        "max_favorable_excursion": _return_pct(entry_price, high),
        "max_adverse_excursion": _return_pct(entry_price, low),
        "drawdown_pct": _return_pct(entry_price, low),
        "hit_target_1": target_1 is not None and high is not None and high >= target_1,
        "hit_target_2": target_2 is not None and high is not None and high >= target_2,
        "hit_invalidation": invalidation is not None and low is not None and low <= invalidation,
        "trigger_activated": trigger is not None and high is not None and high >= trigger,
        "audit_status": audit_status,
        "scenario_or_recommended": scenario_or_recommended,
        "calculated_at": utc_now_iso(),
        "primary_setup": signal.get("primary_setup") or "",
        "source": signal.get("source") or "",
        "score_bucket": _score_bucket(signal),
        "risk_flags": ";".join(str(item) for item in signal.get("risk_flags_json") or []),
    }
    row["payload_json"] = dict(row)
    return row


def _missing_attribution(signal: dict[str, Any]) -> dict[str, Any]:
    signal_id = str(signal.get("signal_id") or "")
    row = {
        "attribution_id": f"{signal_id}:outcome_needed",
        "signal_id": signal_id,
        "ticker": signal.get("ticker"),
        "market_date": signal.get("market_date"),
        "rank": signal.get("rank"),
        "entry_policy": ENTRY_POLICY,
        "exit_policy": "outcome_needed",
        "entry_price": None,
        "exit_price": None,
        "return_pct": None,
        "max_favorable_excursion": None,
        "max_adverse_excursion": None,
        "drawdown_pct": None,
        "hit_target_1": None,
        "hit_target_2": None,
        "hit_invalidation": None,
        "trigger_activated": None,
        "audit_status": "missing_outcome",
        "scenario_or_recommended": "unavailable",
        "calculated_at": utc_now_iso(),
        "primary_setup": signal.get("primary_setup") or "",
        "source": signal.get("source") or "",
        "score_bucket": _score_bucket(signal),
        "risk_flags": ";".join(str(item) for item in signal.get("risk_flags_json") or []),
    }
    row["payload_json"] = dict(row)
    return row


def _daily_performance(
    signals: list[dict[str, Any]],
    attribution_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    signals_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        signals_by_day[str(signal.get("market_date") or "")].append(signal)
    for row in attribution_rows:
        rows_by_day[str(row.get("market_date") or "")].append(row)
    audited_days = {
        day
        for day, rows in rows_by_day.items()
        if any(row.get("audit_status") == "audited" for row in rows)
    }
    evidence = _evidence_status(len(audited_days))
    daily = []
    for day in sorted(signals_by_day):
        day_signals = sorted(
            signals_by_day[day],
            key=lambda row: int(row.get("rank") or 999999),
        )
        trade_signals = [row for row in day_signals if not _is_no_trade_signal(row)]
        day_rows = rows_by_day.get(day, [])
        close = _policy_returns(day_rows, "close")
        lunch = _policy_returns(day_rows, "lunch")
        highs = _policy_returns(day_rows, "high_opportunity")
        usable_highs = [float(value) for value in highs if value is not None]
        drawdowns = [
            _optional_float(row.get("drawdown_pct"))
            for row in day_rows
            if row.get("entry_policy") == ENTRY_POLICY and row.get("exit_policy") == "close"
        ]
        audited_signal_ids = {
            str(row.get("signal_id") or "")
            for row in day_rows
            if row.get("audit_status") == "audited"
        }
        missing_count = len([
            signal
            for signal in trade_signals
            if str(signal.get("signal_id") or "") not in audited_signal_ids
        ])
        row = {
            "market_date": day,
            "signal_count": len(trade_signals),
            "alerted_count": sum(1 for signal in trade_signals if signal.get("was_alerted")),
            "no_trade_count": sum(1 for signal in day_signals if _is_no_trade_signal(signal)),
            "audited_count": len(audited_signal_ids),
            "missing_outcome_count": missing_count,
            "top1_return": _basket_return(close, 1),
            "top3_return": _basket_return(close, 3),
            "top5_return": _basket_return(close, 5),
            "top1_close_return": _basket_return(close, 1),
            "top3_close_return": _basket_return(close, 3),
            "top5_close_return": _basket_return(close, 5),
            "top1_lunch_return": _basket_return(lunch, 1),
            "top3_lunch_return": _basket_return(lunch, 3),
            "top5_lunch_return": _basket_return(lunch, 5),
            "best_pick_return": max(usable_highs) if usable_highs else None,
            "worst_pick_return": _min_or_none(close),
            "max_drawdown": min([value for value in drawdowns if value is not None])
            if any(value is not None for value in drawdowns)
            else None,
            "hit_rate": _win_rate(close),
            "outcome_coverage_pct": (
                round((len(audited_signal_ids) / len(trade_signals)) * 100, 2)
                if trade_signals
                else None
            ),
            "evidence_status": evidence,
        }
        daily.append(row)
    return daily


def _policy_returns(rows: list[dict[str, Any]], exit_policy: str) -> list[float | None]:
    selected = [
        row
        for row in sorted(rows, key=lambda item: int(item.get("rank") or 999999))
        if row.get("entry_policy") == ENTRY_POLICY and row.get("exit_policy") == exit_policy
    ]
    return [_optional_float(row.get("return_pct")) for row in selected]


def _basket_return(values: list[float | None], count: int) -> float | None:
    selected = values[:count]
    if not selected or len(selected) < min(count, len(values)):
        return None
    if any(value is None for value in selected):
        return None
    usable = [float(value) for value in selected if value is not None]
    return round(sum(usable) / len(usable), 4) if usable else None


def _cumulative_curve(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    equity = {"top1": 1.0, "top3": 1.0, "top5": 1.0}
    curve = []
    for row in sorted(daily, key=lambda item: str(item.get("market_date") or "")):
        point: dict[str, Any] = {"market_date": row.get("market_date")}
        for key in ("top1", "top3", "top5"):
            value = _optional_float(row.get(f"{key}_close_return"))
            if value is None:
                point[f"{key}_equity"] = None
                point[f"{key}_compounded_return"] = None
                continue
            equity[key] *= 1 + value / 100
            point[f"{key}_equity"] = round(equity[key], 6)
            point[f"{key}_compounded_return"] = round((equity[key] - 1) * 100, 4)
        curve.append(point)
    return curve


def _attribution_summary(
    signals: list[dict[str, Any]],
    attribution_rows: list[dict[str, Any]],
    daily: list[dict[str, Any]],
    missing: list[dict[str, Any]],
) -> dict[str, Any]:
    close = [
        _optional_float(row.get("return_pct"))
        for row in attribution_rows
        if row.get("entry_policy") == ENTRY_POLICY
        and row.get("exit_policy") == "close"
        and row.get("audit_status") == "audited"
    ]
    usable = [float(value) for value in close if value is not None]
    audited_days = [row for row in daily if row.get("audited_count")]
    return {
        "created_at": utc_now_iso(),
        "research_only": True,
        "scenario_return_note": "Scenario returns are paper returns from imported outcomes.",
        "recommended_return_note": "Recommended returns require a saved exit signal.",
        "signal_count": len([row for row in signals if not _is_no_trade_signal(row)]),
        "no_trade_count": len([row for row in signals if _is_no_trade_signal(row)]),
        "audited_day_count": len(audited_days),
        "missing_outcome_count": len(missing),
        "evidence_status": _evidence_status(len(audited_days)),
        "sample_size": len(usable),
        "average_close_return": round(sum(usable) / len(usable), 4) if usable else None,
        "median_close_return": round(median(usable), 4) if usable else None,
        "win_rate": _win_rate(usable),
        "best_day": _best_or_worst_day(daily, best=True),
        "worst_day": _best_or_worst_day(daily, best=False),
        "max_drawdown": _min_or_none([
            _optional_float(row.get("max_drawdown")) for row in daily
        ]),
        "outlier_dependence": _outlier_dependence(usable),
        "attribution_rows": attribution_rows,
        "daily_rows": daily,
    }


def _historical_report_payload(
    *,
    signals: list[dict[str, Any]],
    events: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    attribution: list[dict[str, Any]],
    daily: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    accuracy_setup: list[dict[str, Any]],
    accuracy_source: list[dict[str, Any]],
    accuracy_score: list[dict[str, Any]],
) -> dict[str, Any]:
    audited_days = [row for row in daily if int(row.get("audited_count") or 0) > 0]
    return {
        "created_at": utc_now_iso(),
        "research_only": True,
        "signal_count": len(signals),
        "event_count": len(events),
        "outcome_count": len(outcomes),
        "attribution_count": len(attribution),
        "daily_count": len(daily),
        "audited_day_count": len(audited_days),
        "missing_outcome_count": len(missing),
        "evidence_status": _evidence_status(len(audited_days)),
        "accuracy_by_setup": accuracy_setup,
        "accuracy_by_source": accuracy_source,
        "accuracy_by_score_bucket": accuracy_score,
    }


def _bucket_accuracy(
    signals: list[dict[str, Any]],
    attribution: list[dict[str, Any]],
    field: str,
) -> list[dict[str, Any]]:
    signal_by_id = {str(row.get("signal_id") or ""): row for row in signals}
    grouped: dict[str, list[float]] = defaultdict(list)
    trigger_hits: dict[str, list[bool]] = defaultdict(list)
    for row in attribution:
        if row.get("entry_policy") != ENTRY_POLICY or row.get("exit_policy") != "close":
            continue
        value = _optional_float(row.get("return_pct"))
        if value is None:
            continue
        signal = signal_by_id.get(str(row.get("signal_id") or ""), {})
        bucket = _bucket_value(signal, row, field)
        grouped[bucket].append(value)
        trigger_hits[bucket].append(bool(row.get("trigger_activated")))
    output = []
    for bucket, values in sorted(grouped.items()):
        output.append({
            "bucket": bucket or "unknown",
            "sample_size": len(values),
            "avg_close_return": round(sum(values) / len(values), 4),
            "median_close_return": round(median(values), 4),
            "win_rate": _win_rate(values),
            "trigger_accuracy": _bool_rate(trigger_hits[bucket]),
            "evidence_status": _evidence_status_from_samples(len(values)),
        })
    return output


def _bucket_value(signal: dict[str, Any], row: dict[str, Any], field: str) -> str:
    if field == "score_bucket":
        return _score_bucket(signal)
    return str(signal.get(field) or row.get(field) or "unknown")


def _score_bucket(signal: dict[str, Any]) -> str:
    raw = dict(signal.get("raw_payload_json") or {})
    score = _optional_float(raw.get("alpha_score") or raw.get("score"))
    if score is None:
        return "unknown"
    lower = int(score // 10) * 10
    return f"{lower}-{lower + 9}"


def _missing_outcome(signal: dict[str, Any]) -> dict[str, Any]:
    day = str(signal.get("market_date") or "")
    return {
        "market_date": day,
        "date": day,
        "signal_id": signal.get("signal_id"),
        "ticker": signal.get("ticker"),
        "rank": signal.get("rank"),
        "status": "Outcome Needed",
        "expected_path": f"data\\inbox\\outcomes\\outcomes_{day}.csv",
    }


def _needs_outcome(signal: dict[str, Any], outcomes: list[dict[str, Any]]) -> bool:
    if _is_no_trade_signal(signal):
        return False
    signal_id = str(signal.get("signal_id") or "")
    return not any(str(row.get("signal_id") or "") == signal_id for row in outcomes)


def _missing_outcome_tickers(
    accepted: list[dict[str, Any]],
    historical_signals: list[dict[str, Any]],
) -> list[str]:
    dates = {str(row.get("market_date") or "") for row in accepted}
    accepted_keys = {
        (str(row.get("market_date") or ""), str(row.get("ticker") or "").upper())
        for row in accepted
    }
    missing = []
    for signal in historical_signals:
        key = (str(signal.get("market_date") or ""), str(signal.get("ticker") or "").upper())
        if key[0] in dates and key not in accepted_keys and not _is_no_trade_signal(signal):
            missing.append(key[1])
    return sorted(set(missing))


def _send_accuracy_summary(
    *,
    store: SQLiteScanStore,
    db_path: str | Path,
    notify: str,
    summary: dict[str, Any],
    missing: list[dict[str, Any]],
) -> dict[str, int]:
    channels = [channel.strip().lower() for channel in notify.split(",") if channel.strip()]
    if not channels:
        return {"sent": 0, "skipped": 0}
    if missing and not summary.get("sample_size"):
        tickers = ", ".join(str(row.get("ticker") or "") for row in missing[:8])
        day = str(missing[0].get("market_date") or date.today().isoformat())
        body = (
            "Outcome Data Needed\n"
            f"Tickers: {tickers}\n"
            f"Save: data\\inbox\\outcomes\\outcomes_{day}.csv"
        )
        hint = "outcome_needed"
    else:
        body = (
            "Dawnstrike Accuracy\n"
            f"Audited days: {summary.get('audited_day_count')} / 20 needed\n"
            f"Top1: {_fmt_pct(_latest_daily(summary, 'top1_close_return'))}\n"
            f"Top3: {_fmt_pct(_latest_daily(summary, 'top3_close_return'))}\n"
            f"Win rate: {_fmt_pct(summary.get('win_rate'))}\n"
            f"Missing outcomes: {summary.get('missing_outcome_count')}\n"
            f"Evidence: {summary.get('evidence_status')}"
        )
        hint = "accuracy_summary"
    event = NotificationEvent(
        event_key=f"historical_attribution:{summary.get('created_at')}:{hint}",
        title="Dawnstrike Accuracy",
        body=body,
        channel_hint=hint,
        payload={"source": "return_attribution", "summary": _summary_without_rows(summary)},
    )
    config = load_config(database_path=Path(db_path), notifier_channels=",".join(channels))
    notifiers = [ConsoleNotifier()] if channels == ["console"] else build_notifiers(config)
    return dispatch_events([event], notifiers, store)


def _summary_without_rows(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in summary.items()
        if key not in {"attribution_rows", "daily_rows"}
    }


def _write_attribution_markdown(
    path: Path,
    summary: dict[str, Any],
    missing: list[dict[str, Any]],
) -> None:
    lines = [
        "# Dawnstrike Return Attribution",
        "",
        "Research/watchlist only. These are paper scenario returns from imported outcomes.",
        "",
        f"- Signals: {summary.get('signal_count')}",
        f"- Audited days: {summary.get('audited_day_count')}",
        f"- Evidence: {summary.get('evidence_status')}",
        f"- Missing outcomes: {len(missing)}",
        f"- Average close return: {_fmt_pct(summary.get('average_close_return'))}",
        f"- Win rate: {_fmt_pct(summary.get('win_rate'))}",
        "",
        "High-opportunity rows are not realized exits. Recommended returns require a",
        "persisted monitor exit or explicit exit signal.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_historical_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Dawnstrike Historical Signal Report",
        "",
        "Research/watchlist only. No broker actions are created here.",
        "",
        f"- Signals: {report.get('signal_count')}",
        f"- Outcomes: {report.get('outcome_count')}",
        f"- Attribution rows: {report.get('attribution_count')}",
        f"- Audited days: {report.get('audited_day_count')}",
        f"- Missing outcomes: {report.get('missing_outcome_count')}",
        f"- Evidence: {report.get('evidence_status')}",
        "",
        "Missing outcomes remain pending and are not counted as zero returns.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _flatten_signal(row: dict[str, Any]) -> dict[str, Any]:
    flattened = dict(row)
    flattened["risk_flags_json"] = ";".join(str(item) for item in row.get("risk_flags_json") or [])
    flattened["avoid_reasons_json"] = ";".join(
        str(item) for item in row.get("avoid_reasons_json") or []
    )
    flattened.pop("raw_payload_json", None)
    return flattened


def _events_by_signal(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("signal_id") or "")].append(row)
    return grouped


def _monitor_event_type(event: dict[str, Any]) -> str:
    text = " ".join(str(event.get(key) or "") for key in ("label", "status")).upper()
    if "INVALIDATED" in text:
        return "INVALIDATED"
    if "THESIS" in text:
        return "THESIS_BROKEN"
    if "BREAKOUT" in text:
        return "BREAKOUT_CONFIRMED"
    return "TRIGGER_TOUCHED"


def _outcome_status(row: dict[str, Any]) -> str:
    price_fields = [
        "entry_price",
        "price_1m",
        "price_5m",
        "price_15m",
        "lunch_price",
        "close_price",
        "high_after_entry",
        "low_after_entry",
    ]
    available = sum(1 for field in price_fields if _optional_float(row.get(field)) is not None)
    if available == len(price_fields):
        return "complete"
    return "partial" if available else "unavailable"


def _read_csv(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SnapshotValidationError(f"{path} is empty or missing a header row")
        return list(reader)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames and key != "payload_json":
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["status"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            [{key: _redact_value(value) for key, value in row.items()} for row in rows]
        )


def _normalize_entry_time(date_value: str, raw: str) -> str:
    if "T" in raw:
        return raw
    if len(raw) == 5 and ":" in raw:
        return f"{date_value}T{raw}:00"
    if raw:
        return raw
    raise SnapshotValidationError("entry_time is required")


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise SnapshotValidationError(f"Invalid timestamp: {value}") from exc


def _date_key(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else date.today().isoformat()


def _split_flags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").replace(",", ";").split(";") if part.strip()]


def _is_no_trade_signal(signal: dict[str, Any]) -> bool:
    return (
        str(signal.get("signal_label") or "").upper() == "NO CLEAN EDGE"
        or str(signal.get("ticker") or "").upper() == "NO_TRADE"
        or bool(signal.get("no_trade_reason"))
    )


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    text = str(value).replace("$", "").replace(",", "").replace("%", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    parsed = _optional_float(value)
    return int(parsed) if parsed is not None else None


def _bool(value: Any) -> bool | None:
    if value in {None, ""}:
        return None
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    return None


def _return_pct(entry: float | None, exit_price: float | None) -> float | None:
    if entry is None or exit_price is None or entry <= 0:
        return None
    return round(((exit_price - entry) / entry) * 100, 4)


def _win_rate(values: Iterable[float | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return round((sum(1 for value in usable if value > 0) / len(usable)) * 100, 2)


def _bool_rate(values: list[bool]) -> float | None:
    if not values:
        return None
    return round((sum(1 for value in values if value) / len(values)) * 100, 2)


def _min_or_none(values: list[float | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return min(usable) if usable else None


def _best_or_worst_day(rows: list[dict[str, Any]], *, best: bool) -> dict[str, Any] | None:
    usable = [row for row in rows if _optional_float(row.get("top3_close_return")) is not None]
    if not usable:
        return None
    return (max if best else min)(
        usable,
        key=lambda row: _optional_float(row.get("top3_close_return")) or 0.0,
    )


def _outlier_dependence(values: list[float]) -> dict[str, Any]:
    if len(values) < 3:
        return {"status": "insufficient_sample", "outlier_dependent": False}
    total = sum(values)
    if total == 0:
        return {"status": "flat_total", "outlier_dependent": False}
    largest = max(values, key=abs)
    share = abs(largest / total)
    return {
        "largest_abs_return": largest,
        "largest_share_of_total": round(share, 4),
        "outlier_dependent": share >= 0.5,
    }


def _evidence_status(audited_days: int) -> str:
    if audited_days < 20:
        return "Not enough history yet."
    if audited_days < 60:
        return "Early evidence."
    return "Stronger evidence."


def _evidence_status_from_samples(samples: int) -> str:
    if samples < 20:
        return "Not enough history yet."
    if samples < 60:
        return "Early evidence."
    return "Stronger evidence."


def _latest_daily(summary: dict[str, Any], field: str) -> Any:
    rows = list(summary.get("daily_rows") or [])
    if not rows:
        return None
    return rows[-1].get(field)


def _fmt_pct(value: Any) -> str:
    parsed = _optional_float(value)
    return "Outcome Needed" if parsed is None else f"{parsed:+.2f}%"


def _redact_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    lowered = value.lower()
    if any(
        marker in lowered
        for marker in ("telegram_bot_token", "telegram_chat_id", "bot_token", "secret")
    ):
        return "[redacted]"
    return value
