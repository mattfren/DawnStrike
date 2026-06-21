"""Notification event generation and dispatch."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import NotificationError
from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent
from intraday_scanner.notifiers.console import ConsoleNotifier
from intraday_scanner.notifiers.email import EmailNotifier
from intraday_scanner.notifiers.webhooks import DiscordWebhookNotifier, TelegramNotifier
from intraday_scanner.notifiers.windows import WindowsLocalNotifier
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def build_notifiers(config: ScannerConfig) -> list[BaseNotifier]:
    channels = [
        channel.strip().lower()
        for channel in config.notifier_channels.split(",")
        if channel.strip()
    ]
    if not channels:
        channels = ["console"]

    notifiers: list[BaseNotifier] = []
    for channel in channels:
        if channel == "console":
            notifiers.append(ConsoleNotifier())
        elif channel == "email":
            notifiers.append(EmailNotifier(config))
        elif channel == "discord":
            notifiers.append(DiscordWebhookNotifier(config))
        elif channel == "telegram":
            notifiers.append(TelegramNotifier(config))
        elif channel == "windows":
            notifiers.append(WindowsLocalNotifier())
        else:
            raise NotificationError(f"Unknown notifier channel: {channel}")
    return notifiers


def scan_events_from_payload(
    scan: dict[str, Any], config: ScannerConfig
) -> list[NotificationEvent]:
    summary = dict(scan.get("summary") or {})
    run_id = str(summary.get("run_id") or "unknown-run")
    top_rows = list(scan.get("top_explosive") or [])
    ranked_rows = list(scan.get("ranked_candidates") or [])
    avoid_rows = list(scan.get("avoid_list") or [])
    events: list[NotificationEvent] = []

    for row in top_rows[: config.explosive_top_n]:
        ticker = str(row.get("ticker", "")).upper()
        score = _number(row.get("score"))
        events.append(
            NotificationEvent(
                event_key=f"{run_id}:top_explosive:{ticker}",
                title=f"New explosive pick: {ticker}",
                body=(
                    f"{ticker} scored {score:.2f}. Breakout {row.get('breakout_trigger')}, "
                    f"invalid {row.get('invalidation_level')}, exit bias "
                    f"{row.get('best_exit_bias', 'n/a')}. Research/watchlist only."
                ),
                channel_hint="top_explosive",
                ticker=ticker,
                payload=dict(row),
            )
        )

    for row in ranked_rows:
        ticker = str(row.get("ticker", "")).upper()
        score = _number(row.get("score"))
        if score >= config.alert_score_threshold:
            events.append(
                NotificationEvent(
                    event_key=f"{run_id}:score_threshold:{ticker}:{config.alert_score_threshold}",
                    title=f"Score threshold hit: {ticker}",
                    body=(
                        f"{ticker} scored {score:.2f}, above threshold "
                        f"{config.alert_score_threshold:.2f}. Research/watchlist only."
                    ),
                    channel_hint="score_threshold",
                    ticker=ticker,
                    payload=dict(row),
                )
            )

    for row in avoid_rows:
        ticker = str(row.get("ticker", "")).upper()
        reasons = str(row.get("avoid_reasons") or row.get("risk_flags") or "risk flag")
        if "halt" in reasons.lower() or "offering" in reasons.lower():
            events.append(
                NotificationEvent(
                    event_key=f"{run_id}:risk_warning:{ticker}:{reasons}",
                    title=f"Risk warning: {ticker}",
                    body=f"{ticker} is blocked by {reasons}. Do not touch list.",
                    channel_hint="risk_warning",
                    ticker=ticker,
                    payload=dict(row),
                )
            )

    return events


def audit_summary_events(
    summary: dict[str, Any],
    *,
    run_id: str | None = None,
) -> list[NotificationEvent]:
    if not summary:
        return []
    key_run_id = run_id or str(summary.get("run_id") or summary.get("created_at") or "audit")
    body = (
        f"Audited {summary.get('trade_count', 0)} trade(s). "
        f"Lunch avg {summary.get('avg_lunch_return_pct', 0)}%, "
        f"close avg {summary.get('avg_close_return_pct', 0)}%, "
        f"high avg {summary.get('avg_high_return_pct', 0)}%."
    )
    return [
        NotificationEvent(
            event_key=f"{key_run_id}:audit_summary:{summary.get('created_at', '')}",
            title="Paper audit summary",
            body=body,
            channel_hint="audit_summary",
            payload=summary,
        )
    ]


def dispatch_events(
    events: Iterable[NotificationEvent],
    notifiers: Iterable[BaseNotifier],
    store: SQLiteScanStore,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    sent = 0
    skipped = 0
    for event in events:
        for notifier in notifiers:
            notification_key = f"{event.event_key}:{notifier.channel}"
            if store.has_notification(notification_key):
                skipped += 1
                continue
            if dry_run:
                print(f"[dry-run:{notifier.channel}] {event.title}: {event.body}")
                store.record_notification(
                    event_key=notification_key,
                    channel=notifier.channel,
                    run_id=_payload_run_id(event.payload),
                    ticker=event.ticker,
                    payload={
                        "title": event.title,
                        "body": event.body,
                        "channel_hint": event.channel_hint,
                        "payload": event.payload or {},
                        "dry_run": True,
                    },
                )
                sent += 1
                continue
            else:
                notifier.send(event)
            store.record_notification(
                event_key=notification_key,
                channel=notifier.channel,
                run_id=_payload_run_id(event.payload),
                ticker=event.ticker,
                payload={
                    "title": event.title,
                    "body": event.body,
                    "channel_hint": event.channel_hint,
                    "payload": event.payload or {},
                },
            )
            sent += 1
    return {"sent": sent, "skipped": skipped}


def _payload_run_id(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    run_id = payload.get("run_id")
    return str(run_id) if run_id else None


def _number(value: Any) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0
