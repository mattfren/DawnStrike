"""Compact Telegram message formatting for operator notifications."""

from __future__ import annotations

from typing import Any

from intraday_scanner.notifiers.base import NotificationEvent
from intraday_scanner.services.time_utils import get_operator_time_label

DEFAULT_MORNING_MAX_CHARS = 1200
DEFAULT_ALERT_MAX_CHARS = 600
DEFAULT_SUMMARY_MAX_CHARS = 900


def format_telegram_event(
    event: NotificationEvent,
    *,
    max_morning_chars: int = DEFAULT_MORNING_MAX_CHARS,
    max_alert_chars: int = DEFAULT_ALERT_MAX_CHARS,
    max_summary_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    include_debug_fields: bool = False,
) -> str:
    payload = dict(event.payload or {})
    compact = str(payload.get("telegram_compact_message") or "").strip()
    limit = _limit_for_hint(
        event.channel_hint,
        max_morning_chars,
        max_alert_chars,
        max_summary_chars,
    )
    if compact and not include_debug_fields:
        return _clip(compact, limit)
    body = event.body.strip()
    if _looks_compact(body) and not include_debug_fields:
        return _clip(body, limit)
    text = f"{event.title}\n{body}".strip()
    if include_debug_fields and payload:
        debug = {key: value for key, value in payload.items() if "token" not in key.lower()}
        text = f"{text}\n\nDebug: {debug}"
    return _clip(text, limit)


def format_morning_watchlist(
    *,
    ranked: list[dict[str, Any]],
    avoid: list[dict[str, Any]],
    source_summary: dict[str, Any],
    timezone: str = "America/Chicago",
    max_chars: int = DEFAULT_MORNING_MAX_CHARS,
) -> str:
    picks = ranked[:3]
    lines = [
        "🚀 Dawnstrike Watchlist",
        (
            f"⏱ {get_operator_time_label(timezone)} | {len(ranked[:3])} picks | "
            f"Source: {_source_label(source_summary)}"
        ),
        "",
    ]
    if not picks:
        lines.extend(["No saved picks found.", "", "Research only. No orders placed."])
        return _clip("\n".join(lines), max_chars)
    for index, row in enumerate(picks, start=1):
        ticker = _text(row.get("ticker"), "n/a")
        catalyst = _catalyst_line(row)
        lines.append(
            f"{index}) {ticker} — {format_score(row.get('score'))} | "
            f"{format_percent(row.get('gap_pct'))} | {format_price(row.get('premarket_price'))}"
        )
        lines.append(
            f"   🎯 {format_price(row.get('breakout_trigger') or row.get('target_1'))} | "
            f"🛑 {format_price(row.get('invalidation_level') or row.get('invalidation'))}"
        )
        if catalyst != "none":
            lines.append(f"   📰 {catalyst}")
        risk = _risk_text(row)
        if risk != "none":
            lines.append(f"   ⚠️ {_truncate(risk, 80)}")
        lines.append("")
    extra = len(ranked) - len(picks)
    if extra > 0:
        lines.append(f"+{extra} more in dashboard.")
        lines.append("")
    if avoid:
        lines.append(f"🚫 Avoid: {len(avoid)}")
        lines.append("")
    lines.append("Research only. No orders placed.")
    return _clip("\n".join(lines).strip(), max_chars)


def format_risk_alert(row: dict[str, Any]) -> str:
    ticker = _text(row.get("ticker"), "UNKNOWN")
    reason = _risk_text(row)
    return "\n".join(
        [
            "⚠️ Dawnstrike Alert",
            f"{ticker} — CAUTION",
            f"Reason: {reason}",
            "Action: manual review",
            "No orders placed.",
        ]
    )


def format_manual_monitor(tickers: list[str]) -> str:
    watch = ", ".join(ticker for ticker in tickers if ticker) or "No saved picks found."
    return "\n".join(
        [
            "👀 Manual Monitor Needed",
            "No live price source configured.",
            f"Watch: {watch}",
        ]
    )


def format_outcome_needed(
    *,
    run_date: str,
    reminder_path: str,
    tickers: list[str],
) -> str:
    lines = [
        "📥 Outcome Data Needed",
        "Save:",
        reminder_path or f"data\\inbox\\outcomes\\outcomes_{run_date}.csv",
        "",
        "Tickers:",
        ", ".join(tickers) if tickers else "No saved picks found.",
        "",
        "Needed:",
        "entry, 1m, 5m, 15m, lunch, close, high, low",
    ]
    return "\n".join(lines)


def format_daily_summary(summary: dict[str, Any]) -> str:
    report = dict(summary.get("shadow_report") or {})
    return "\n".join(
        [
            "📊 Dawnstrike Summary",
            f"Top1: {format_percent(report.get('top_1_close_return_pct'), signed=False)}",
            f"Top3: {format_percent(report.get('top_3_close_return_pct'), signed=False)}",
            f"Top5: {format_percent(report.get('top_5_close_return_pct'), signed=False)}",
            f"Missing outcomes: {_text(summary.get('missing_outcome_count'), 'n/a')}",
            f"Dashboard: {_text(summary.get('dashboard_url'), 'http://127.0.0.1:8502/')}",
            "",
            "Manual/free shadow results only.",
        ]
    )


def format_source_check(source_summary: dict[str, Any]) -> str:
    attempts = list(source_summary.get("attempts") or [])
    top_reason = _text(source_summary.get("top_failure_reason"), "")
    lines = [
        "📡 Dawnstrike Source Check",
        "No usable rows found.",
    ]
    if top_reason:
        lines.append(f"Top reason: {top_reason.replace('_', ' ')}")
    lines.extend(["", "Tried:"])
    if not attempts:
        lines.append("- no enabled candidate sources")
    else:
        for attempt in attempts[:5]:
            source = _attempt_label(attempt)
            status = str(attempt.get("status") or attempt.get("reason") or "unknown")
            if status == "failed":
                status = str(attempt.get("reason") or attempt.get("failure_reason") or "failed")
            lines.append(f"- {source}: {status.replace('_', ' ')}")
    lines.extend(
        [
            "",
            "Next:",
            "Try again during premarket or drop CSV into data\\inbox\\screener.",
        ]
    )
    return "\n".join(lines)


def format_score(value: Any) -> str:
    number = _number(value)
    return "n/a" if number is None else f"{number:.1f}"


def format_percent(value: Any, *, signed: bool = True) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}{number:.0f}%"


def format_price(value: Any) -> str:
    number = _number(value)
    return "n/a" if number is None else f"${number:.2f}"


def format_dollar_volume(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    abs_value = abs(number)
    if abs_value >= 1_000_000_000:
        return f"${number / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:.0f}"


def _source_label(source_summary: dict[str, Any]) -> str:
    attempts = list(source_summary.get("attempts") or [])
    if any(
        str(item.get("source") or "").lower() == "local_inbox"
        and item.get("status") == "success"
        for item in attempts
    ):
        return "manual"
    if attempts:
        return "web"
    return "manual/web"


def _attempt_label(attempt: dict[str, Any]) -> str:
    source_type = str(attempt.get("source_type") or "")
    source = str(attempt.get("source") or "")
    if source_type == "local_inbox" or source == "local_inbox":
        return "local inbox"
    if source:
        return source
    return source_type or "source"


def _risk_text(row: dict[str, Any]) -> str:
    raw = row.get("risk_flags") or row.get("avoid_reasons") or ""
    if isinstance(raw, list):
        raw = ", ".join(str(item) for item in raw if item)
    text = str(raw or "").strip().strip(";")
    return text if text else "none"


def _action_parts(value: Any) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", "CAUTION"
    return "", text


def _catalyst_line(row: dict[str, Any]) -> str:
    summary = _text(
        row.get("catalyst_summary") or row.get("catalyst_headline"),
        "none",
    )
    if summary.lower() in {"no clear catalyst", "none"}:
        return "none"
    return _truncate(summary, 60)


def _issue_count(ranked: list[dict[str, Any]], avoid: list[dict[str, Any]]) -> int:
    warnings = 0
    for row in ranked:
        raw = str(row.get("data_warnings") or "").strip()
        if raw:
            warnings += len([part for part in raw.replace(",", ";").split(";") if part.strip()])
    return warnings + len(avoid)


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    text = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    if not text or text.lower() in {"n/a", "na", "none"}:
        return None
    multiplier = 1.0
    suffix = text[-1:].lower()
    if suffix in {"k", "m", "b"}:
        multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix]
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _text(value: Any, default: str = "n/a") -> str:
    text = str(value or "").strip()
    return text if text else default


def _truncate(value: str, max_chars: int) -> str:
    return value if len(value) <= max_chars else value[: max_chars - 3].rstrip() + "..."


def _clip(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def _looks_compact(body: str) -> bool:
    return body.startswith(("🚀", "⚠️", "👀", "📥", "📊", "📡"))


def _limit_for_hint(
    channel_hint: str,
    max_morning_chars: int,
    max_alert_chars: int,
    max_summary_chars: int,
) -> int:
    if channel_hint in {"top_picks", "web_auto_pilot"}:
        return max_morning_chars
    if channel_hint == "daily_summary":
        return max_summary_chars
    return max_alert_chars
