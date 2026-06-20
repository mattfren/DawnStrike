"""Alert trigger logic for active recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from intraday_scanner.ai.headline_classifier import HeadlineClassifier
from intraday_scanner.ai.thesis_monitor import ThesisMonitor
from intraday_scanner.models import utc_now_iso
from intraday_scanner.providers.base import FilingItem, NewsItem
from intraday_scanner.providers.sec_provider import filing_has_dilution_risk


@dataclass(frozen=True)
class AlertEvent:
    alert_key: str
    event_type: str
    severity: str
    ticker: str
    title: str
    body: str
    suggested_action: str
    payload: dict[str, Any]


def alerts_from_monitor_rows(
    rows: list[dict[str, Any]], *, run_id: str | None = None
) -> list[AlertEvent]:
    alerts: list[AlertEvent] = []
    for row in rows:
        status = str(row.get("status", "")).lower()
        ticker = str(row.get("ticker", "")).upper()
        if status == "invalidated":
            alerts.append(_event(row, run_id, "invalidated", "critical", "THESIS BROKEN"))
        elif status == "fading":
            alerts.append(_event(row, run_id, "momentum_failure", "high", "CAUTION"))
        elif status == "extended":
            alerts.append(_event(row, run_id, "extended", "medium", "WATCH"))
        risk_flags = str(row.get("risk_flags") or "").lower()
        if any(term in risk_flags for term in ("offering", "halt", "wide_spread")):
            alerts.append(_event(row, run_id, "risk_flag", "high", "CAUTION"))
        if not ticker:
            continue
    return alerts


def alerts_from_news_and_filings(
    *,
    news_items: list[NewsItem],
    filing_items: list[FilingItem],
    original_theses: dict[str, str],
    classifier: HeadlineClassifier,
    run_id: str | None = None,
) -> list[AlertEvent]:
    alerts: list[AlertEvent] = []
    thesis_monitor = ThesisMonitor()
    for item in news_items:
        classification = classifier.classify(
            ticker=item.ticker,
            headline=item.headline,
            thesis=original_theses.get(item.ticker.upper(), ""),
        )
        thesis_read = thesis_monitor.compare(classification)
        if thesis_read.state in {"broken", "weakening"}:
            alerts.append(
                _external_event(
                    ticker=item.ticker,
                    run_id=run_id,
                    event_type=f"news_{classification.label}",
                    severity=classification.severity,
                    suggested_action=(
                        "THESIS BROKEN" if thesis_read.state == "broken" else "CAUTION"
                    ),
                    reason=classification.reason,
                    source_link=item.url,
                    payload={
                        "headline": item.headline,
                        "source": item.source,
                        "published_at": item.published_at,
                        "thesis_state": thesis_read.state,
                    },
                )
            )
    for filing in filing_items:
        if filing_has_dilution_risk(filing):
            alerts.append(
                _external_event(
                    ticker=filing.ticker,
                    run_id=run_id,
                    event_type="sec_dilution_risk",
                    severity="critical",
                    suggested_action="THESIS BROKEN",
                    reason=f"SEC filing risk detected: {filing.filing_type}",
                    source_link=filing.url,
                    payload={
                        "filing_type": filing.filing_type,
                        "filed_at": filing.filed_at,
                        "headline": filing.headline,
                        "source": filing.source,
                    },
                )
            )
    return alerts


def persist_deduped_alerts(store: Any, alerts: list[AlertEvent], run_id: str | None = None) -> int:
    sent = 0
    for alert in alerts:
        if store.record_alert(
            alert_key=alert.alert_key,
            event_type=alert.event_type,
            severity=alert.severity,
            payload=alert.payload,
            run_id=run_id,
            ticker=alert.ticker,
        ):
            if hasattr(store, "persist_monitor_events"):
                store.persist_monitor_events([alert.payload], run_id=run_id)
            sent += 1
    return sent


def _event(
    row: dict[str, Any],
    run_id: str | None,
    event_type: str,
    severity: str,
    suggested_action: str,
) -> AlertEvent:
    ticker = str(row.get("ticker", "")).upper()
    created_at = utc_now_iso()
    key_run = run_id or str(row.get("run_id") or "latest")
    alert_key = f"{key_run}:{ticker}:{event_type}:{severity}"
    body = (
        f"{ticker} {event_type.replace('_', ' ')}. Latest price {row.get('current_price')}; "
        f"watch {row.get('breakout_trigger')}; invalidation {row.get('invalidation_level')}. "
        f"Reason: {row.get('reason', 'No reason saved.')}"
    )
    payload = {
        **row,
        "event_type": event_type,
        "severity": severity,
        "suggested_action": suggested_action,
        "created_at": created_at,
    }
    return AlertEvent(
        alert_key=alert_key,
        event_type=event_type,
        severity=severity,
        ticker=ticker,
        title=f"{suggested_action}: {ticker}",
        body=body,
        suggested_action=suggested_action,
        payload=payload,
    )


def _external_event(
    *,
    ticker: str,
    run_id: str | None,
    event_type: str,
    severity: str,
    suggested_action: str,
    reason: str,
    source_link: str,
    payload: dict[str, Any],
) -> AlertEvent:
    key_run = run_id or "latest"
    normalized_ticker = ticker.upper()
    alert_key = f"{key_run}:{normalized_ticker}:{event_type}:{severity}"
    created_at = utc_now_iso()
    event_payload = {
        **payload,
        "ticker": normalized_ticker,
        "event_type": event_type,
        "severity": severity,
        "suggested_action": suggested_action,
        "reason": reason,
        "source_link": source_link,
        "created_at": created_at,
    }
    return AlertEvent(
        alert_key=alert_key,
        event_type=event_type,
        severity=severity,
        ticker=normalized_ticker,
        title=f"{suggested_action}: {normalized_ticker}",
        body=f"{normalized_ticker} {event_type.replace('_', ' ')}. {reason}",
        suggested_action=suggested_action,
        payload=event_payload,
    )
