"""Reusable Streamlit display helpers."""

from __future__ import annotations

from html import escape
from typing import Any

from intraday_scanner.dashboard.display_text import (
    evidence_status,
    no_trade_reason,
    research_label,
    source_label,
    translate_label,
    translate_list,
)


def filter_candidates(
    rows: list[dict[str, Any]], min_score: float, top_n: int
) -> list[dict[str, Any]]:
    filtered = [row for row in rows if float(row.get("score") or 0) >= min_score]
    return filtered[:top_n]


def status_banner(status: dict[str, Any]) -> str:
    variant = str(status.get("variant") or "neutral")
    title = str(status.get("title") or "No status")
    explanation = str(status.get("explanation") or "")
    return (
        f'<div class="ds-simple-banner ds-simple-banner--{_html(variant)}">'
        f'<div class="ds-simple-banner-title">{_html(title)}</div>'
        f'<div class="ds-simple-banner-copy">{_html(explanation)}</div>'
        "</div>"
    )


def main_pick_card(pick: dict[str, Any] | None) -> str:
    if not pick:
        return (
            '<div class="ds-main-pick ds-main-pick--empty">'
            '<div class="ds-main-pick-kicker">Main pick</div>'
            '<div class="ds-main-pick-title">No clean setup loaded</div>'
            '<div class="ds-main-pick-copy">Run the scan or check the data source.</div>'
            "</div>"
        )
    levels = [
        ("Price", _format_price(pick.get("price"))),
        ("Watch Level", _format_price(pick.get("watch_level"))),
        ("Exit Line", _format_price(pick.get("exit_line"))),
        ("Target", _format_price(pick.get("target"))),
        ("Confidence", str(pick.get("confidence") or "Not enough history yet")),
        ("Data Quality", str(pick.get("data_quality") or "Unknown")),
    ]
    level_html = "".join(
        '<div class="ds-level-item">'
        f'<span>{_html(label)}</span><strong>{_html(value)}</strong>'
        "</div>"
        for label, value in levels
    )
    return (
        '<div class="ds-main-pick">'
        '<div class="ds-main-pick-kicker">Main pick</div>'
        f'<div class="ds-main-pick-title">{_html(pick.get("ticker", "n/a"))}</div>'
        f'<div class="ds-main-pick-copy">{_html(pick.get("company", ""))}</div>'
        '<div class="ds-pill-row">'
        f'<span class="ds-pill">{_html(pick.get("setup", "Setup"))}</span>'
        '<span class="ds-pill ds-pill--blue">'
        f'{_html(pick.get("decision", "Watch Only"))}</span></div>'
        f'<div class="ds-level-list">{level_html}</div>'
        '<div class="ds-main-pick-note">Risk: '
        f'{_html(pick.get("main_risk", "No hard risk flags"))}</div>'
        "</div>"
    )


def top_three_cards(picks: list[dict[str, Any]]) -> str:
    cards = []
    for pick in picks[:3]:
        cards.append(
            '<div class="ds-watch-card">'
            f'<div class="ds-watch-rank">#{_html(pick.get("rank", ""))}</div>'
            f'<div class="ds-watch-ticker">{_html(pick.get("ticker", "n/a"))}</div>'
            f'<div class="ds-watch-label">{_html(pick.get("plain_label", "Watch Only"))}</div>'
            '<div class="ds-watch-line"><span>Score</span><strong>'
            f'{_html(_format_number(pick.get("score")))}</strong></div>'
            '<div class="ds-watch-line"><span>Watch Level</span><strong>'
            f'{_html(_format_price(pick.get("watch_level")))}</strong></div>'
            '<div class="ds-watch-line"><span>Exit Line</span><strong>'
            f'{_html(_format_price(pick.get("exit_line")))}</strong></div>'
            f'<div class="ds-watch-risk">{_html(pick.get("main_risk", "No hard risk flags"))}</div>'
            "</div>"
        )
    while len(cards) < 3:
        cards.append(
            '<div class="ds-watch-card ds-watch-card--empty">'
            '<div class="ds-watch-ticker">Empty</div>'
            '<div class="ds-watch-risk">No additional pick.</div>'
            "</div>"
        )
    return f'<div class="ds-watch-grid">{"".join(cards)}</div>'


def next_steps_panel(steps: list[dict[str, Any]]) -> str:
    items = []
    for step in steps:
        state = "done" if step.get("done") else "todo"
        icon = "✅" if step.get("done") else "⏳"
        items.append(
            f'<div class="ds-next-step ds-next-step--{state}">'
            f'<span>{icon}</span><strong>{_html(step.get("label", ""))}</strong>'
            f'<small>{_html(step.get("detail", ""))}</small>'
            "</div>"
        )
    return f'<div class="ds-next-panel">{"".join(items)}</div>'


def risk_summary_panel(summary: dict[str, Any]) -> str:
    cards = [
        ("Avoid count", _format_number(summary.get("avoid_count"))),
        ("Top avoid reason", str(summary.get("top_avoid_reason") or "No hard risk flags")),
        ("Data warnings", _format_number(summary.get("data_warning_count"))),
        ("Missing outcomes", _format_number(summary.get("missing_outcome_count"))),
    ]
    return _mini_card_grid(cards)


def outcome_needed_panel(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    first = rows[0]
    path = str(first.get("expected_path") or first.get("path") or "")
    tickers = ", ".join(str(row.get("ticker") or "") for row in rows[:8] if row.get("ticker"))
    return (
        '<div class="ds-outcome-needed">'
        '<strong>Outcome Needed</strong>'
        f'<div>Add outcome file here: <code>{_html(path)}</code></div>'
        f'<div>Tickers needing rows: {_html(tickers or "See Calendar")}</div>'
        "</div>"
    )


def evidence_status_card(summary: dict[str, Any]) -> str:
    audited = int(float(summary.get("audited_days") or 0))
    status = evidence_status(audited)
    return _mini_card_grid(
        [
            ("Real days tracked", _format_number(summary.get("real_days"))),
            ("Audited days", _format_number(audited)),
            ("Evidence status", status),
        ]
    )


def source_status_card(health: dict[str, Any]) -> str:
    status = str(health.get("status") or "Unknown")
    label = source_label(health.get("data_source_kind") or health.get("source"))
    return _mini_card_grid(
        [
            ("Data source", label),
            ("Status", translate_label(status)),
            ("Data quality", str(health.get("data_quality") or "Unknown")),
        ]
    )


def calendar_day_card(day: dict[str, Any]) -> str:
    status = str(day.get("status_label") or day.get("status") or "No data")
    css = str(day.get("status_class") or "empty")
    badge = (
        '<span class="ds-day-badge">Outcome Needed</span>'
        if day.get("missing_outcome_count")
        else ""
    )
    return (
        f'<div class="ds-calendar-card ds-calendar-card--{_html(css)}">'
        f'<div class="ds-calendar-date">{_html(str(day.get("date", ""))[-2:])}</div>'
        f'<div class="ds-calendar-status">{_html(status)}</div>'
        f'<div class="ds-calendar-metric">Top pick: {_html(day.get("top_pick", "None"))}<br>'
        f'Top3: {_html(day.get("top3_return_label", "Pending"))}</div>'
        f"{badge}</div>"
    )


def simple_picks_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Rank": row.get("rank", ""),
            "Ticker": row.get("ticker", ""),
            "Setup": row.get("setup", ""),
            "Score": _format_number(row.get("score")),
            "Gap": _format_pct(row.get("gap_pct")),
            "Price": _format_price(row.get("price")),
            "Watch Level": _format_price(row.get("watch_level")),
            "Exit Line": _format_price(row.get("exit_line")),
            "Target": _format_price(row.get("target")),
            "Confidence": row.get("confidence", ""),
            "Main Risk": row.get("main_risk", ""),
        }
        for row in rows
    ]


def simple_avoid_table(rows: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    return [
        {
            "Ticker": row.get("ticker", ""),
            "Why avoid?": row.get("why_avoid") or row.get("main_risk") or "Risky setup",
            "Gap": _format_pct(row.get("gap_pct")),
            "Volume": _format_number(row.get("volume") or row.get("dollar_volume")),
            "Risk": row.get("risk", ""),
        }
        for row in rows[:limit]
    ]


def display_pick_from_raw(row: dict[str, Any]) -> dict[str, Any]:
    score = row.get("alpha_score") or row.get("total_score") or row.get("score")
    risk = translate_list(row.get("risk_flags") or row.get("avoid_reasons"))
    if not risk:
        risk = "No hard risk flags"
    return {
        "rank": row.get("rank", ""),
        "ticker": str(row.get("ticker") or "").upper(),
        "company": row.get("company") or row.get("name") or "",
        "setup": translate_label(
            row.get("setup_key") or row.get("setup_grade") or "Momentum setup"
        ),
        "decision": research_label(
            row.get("label") or row.get("action"), risk_flags=risk, score=score
        ),
        "plain_label": research_label(
            row.get("label") or row.get("action"), risk_flags=risk, score=score
        ),
        "score": score,
        "gap_pct": row.get("gap_pct"),
        "price": row.get("premarket_price") or row.get("current_price") or row.get("price"),
        "watch_level": row.get("breakout_trigger") or row.get("entry_trigger"),
        "exit_line": row.get("invalidation_level") or row.get("invalidation"),
        "target": row.get("first_target") or row.get("target_1"),
        "confidence": translate_label(row.get("confidence_bucket") or "Not enough history yet"),
        "risk_level": "Risky" if risk != "No hard risk flags" else "Normal",
        "data_quality": source_label(row.get("data_source_kind") or row.get("source")),
        "main_risk": risk,
    }


def no_pick_message(reason: Any) -> str:
    return no_trade_reason(reason)


def _mini_card_grid(cards: list[tuple[str, str]]) -> str:
    html = "".join(
        '<div class="ds-mini-card">'
        f'<span>{_html(label)}</span><strong>{_html(value)}</strong>'
        "</div>"
        for label, value in cards
    )
    return f'<div class="ds-mini-grid">{html}</div>'


def _html(value: Any) -> str:
    return escape(str(value or ""))


def _format_number(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if abs(number) >= 1_000:
        return f"{number / 1_000:.1f}K"
    if abs(number) >= 100:
        return f"{number:.0f}"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _format_price(value: Any) -> str:
    if value in {None, ""}:
        return "Pending"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "Pending"
    return f"${number:.4f}".rstrip("0").rstrip(".")


def _format_pct(value: Any) -> str:
    if value in {None, ""}:
        return "Pending"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "Pending"
    return f"{number:.2f}%".rstrip("0").rstrip(".")
