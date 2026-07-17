"""Institutional strategy-performance calendar for the mounted Streamlit app."""

from __future__ import annotations

import calendar as calendar_lib
import html
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from intraday_scanner.dashboard.paper_ops_calendar_service import (
    PaperOpsCalendarError,
    build_paper_ops_calendar_view,
    format_return_fraction,
    load_paper_ops_calendar,
    strategy_label,
)
from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import market_session

JsonDict = dict[str, Any]

_MODE_LABELS = {
    "forward": "Forward Paper",
    "replay": "Historical Replay",
    "demo": "Synthetic Demo",
}
_MODE_CONTROL_LABELS = {
    "forward": "Paper",
    "replay": "Replay",
    "demo": "Demo",
}


def render_strategy_calendar(output_root: str | Path | None = None) -> bool:
    """Render the one-click PaperOps calendar; return False when truth is unavailable."""

    _calendar_styles()
    try:
        dataset = load_paper_ops_calendar(output_root)
    except (OSError, PaperOpsCalendarError, ValueError) as exc:
        st.error(f"Strategy calendar blocked: {exc}")
        return False
    if dataset.get("status") == "unavailable":
        st.info("No canonical PaperOps strategy calendar is retained yet.")
        return False
    if dataset.get("status") != "verified":
        st.error(
            "Strategy performance is blocked because one or more truth gates are not passed. "
            "No return values are displayed as official."
        )
        _evidence_panel(dataset, None)
        return False

    available = [str(item) for item in dataset.get("available_modes") or []]
    default_mode = "forward" if "forward" in available else (
        "replay" if "replay" in available else available[0]
    )
    _terminal_header(dataset, default_mode)

    controls = st.columns([1, 1], gap="medium")
    if len(available) > 1:
        with controls[0]:
            mode = st.segmented_control(
                "Results",
                options=available,
                default=default_mode,
                format_func=lambda value: _MODE_CONTROL_LABELS[str(value)],
                key="strategy_calendar_mode",
                help="Paper is recorded forward performance. Replay is historical research.",
                width="stretch",
            )
        selected_mode = str(mode or default_mode)
    else:
        selected_mode = default_mode
    view = build_paper_ops_calendar_view(dataset, selected_mode)
    if view.get("status") == "empty":
        _empty_mode(selected_mode, available)
        _evidence_panel(dataset, view)
        return True
    if view.get("status") != "verified":
        st.error(
            f"{_MODE_LABELS.get(selected_mode, selected_mode.title())} values are blocked "
            "because retained source-bar truth is not passed."
        )
        _evidence_panel(dataset, view)
        return True

    dates = [date.fromisoformat(value) for value in view.get("dates") or []]
    month_options = sorted({value.strftime("%Y-%m") for value in dates})
    latest_month = month_options[-1]
    month_control = controls[1] if len(available) > 1 else controls[0]
    with month_control:
        selected_month = st.selectbox(
            "Month",
            month_options,
            index=len(month_options) - 1,
            format_func=_month_label,
            key=f"strategy_calendar_month_{selected_mode}",
        )

    if selected_mode == "replay" and "forward" not in available:
        st.markdown(
            _notice(
                "Historical research lane",
                "No completed forward PaperOps sessions are retained yet. This view is "
                "verified historical replay and is never counted as forward evidence.",
            ),
            unsafe_allow_html=True,
        )
    if selected_month != latest_month:
        st.caption(f"Viewing {_month_label(selected_month)}; latest retained month is "
                   f"{_month_label(latest_month)}.")

    month_rows = _rows_for_month(view.get("rows") or [], selected_month)
    month_days = _days_for_month(view.get("day_summaries") or [], selected_month)
    retained_days = sorted(str(row["date"]) for row in month_days)
    selected_key = f"strategy_calendar_selected_day_{selected_mode}_{selected_month}"
    selected_day = _resolve_selected_day(
        retained_days,
        st.session_state.get(selected_key),
    )
    if selected_day is None:
        st.info("No completed strategy results are retained for this month.")
        return True

    st.markdown(
        '<div class="dsx-simple-section"><strong>Choose a day</strong>'
        '<span>Day cells show the official fleet return.</span></div>',
        unsafe_allow_html=True,
    )
    year, month_number = (int(part) for part in selected_month.split("-"))
    with st.container(key="strategy_calendar_picker"):
        selected_day = _render_calendar_day_picker(
            month_days,
            year,
            month_number,
            selected_day=selected_day,
            mode=selected_mode,
        )
    st.session_state[selected_key] = selected_day

    day_summary = next(
        (row for row in month_days if str(row.get("date")) == selected_day),
        {},
    )
    strategy_rows = _selected_day_strategy_rows(
        month_rows,
        view.get("strategy_summaries") or [],
        selected_day,
    )
    st.markdown(
        _selected_day_panel_html(day_summary, strategy_rows),
        unsafe_allow_html=True,
    )

    all_day_rows = [
        row for row in month_rows if str(row.get("date")) == selected_day
    ]
    with st.expander("More details", expanded=False):
        st.caption(
            "Benchmark, P&L, drawdown, positions, costs, and source lineage for the "
            "selected day."
        )
        _day_close_header(day_summary, all_day_rows)
        st.markdown(_day_ledger_html(all_day_rows), unsafe_allow_html=True)
        st.caption(
            "Source SHA-256: " + str(dataset.get("source_sha256") or "N/A")
            + " · Paper research only · No broker orders."
        )
        for warning in sorted(
            dict.fromkeys(
                [
                    *[str(item) for item in dataset.get("warnings") or []],
                    *[str(item) for item in view.get("warnings") or []],
                ]
            )
        ):
            st.warning(warning)
    st.caption(
        "Verified paper results. Returns include configured paper costs. "
        "Missing evidence is shown as N/A, never zero."
    )
    return True


def _terminal_header(dataset: JsonDict, default_mode: str) -> None:
    lane = _MODE_CONTROL_LABELS.get(default_mode, default_mode.title())
    st.markdown(
        '<section class="dsx-hero">'
        '<h2>Performance calendar</h2>'
        '<p>Click a day. See every strategy return.</p>'
        f'<span class="dsx-hero-status">Verified · {html.escape(lane)} results</span>'
        '</section>',
        unsafe_allow_html=True,
    )


def _empty_mode(mode: str, available: list[str]) -> None:
    alternatives = ", ".join(_MODE_LABELS.get(item, item.title()) for item in available)
    st.markdown(
        _notice(
            f"No {_MODE_LABELS.get(mode, mode.title())} sessions",
            "No completed strategy-account rows exist in this lane. Nothing is shown as "
            f"zero. Available retained evidence: {alternatives or 'none'}.",
        ),
        unsafe_allow_html=True,
    )


def _resolve_selected_day(
    retained_days: list[str],
    requested_day: object,
) -> str | None:
    """Keep a valid clicked day, otherwise select the latest retained day."""

    if not retained_days:
        return None
    requested = str(requested_day or "")[:10]
    return requested if requested in retained_days else retained_days[-1]


def _calendar_button_label(day_value: date, summary: JsonDict, *, selected: bool) -> str:
    value = _number(summary.get("fleet_daily_return"))
    rendered = format_return_fraction(value, decimals=4)
    if value is None:
        state = "N/A"
    elif value > 0:
        state = f"↑ {rendered}"
    elif value < 0:
        state = f"↓ {rendered}"
    else:
        state = "— Flat"
    prefix = "✓ " if selected else ""
    return f"{prefix}**{day_value.day}**  \n{state}"


def _render_calendar_day_picker(
    days: list[JsonDict],
    year: int,
    month_number: int,
    *,
    selected_day: str,
    mode: str,
) -> str:
    by_date = {str(row.get("date")): row for row in days}
    weekday_columns = st.columns(7, gap="small")
    for name, column in zip(calendar_lib.day_abbr, weekday_columns, strict=False):
        with column:
            st.markdown(
                f'<div class="dsx-weekday-simple">{html.escape(name)}</div>',
                unsafe_allow_html=True,
            )

    for week in calendar_lib.Calendar(firstweekday=0).monthdatescalendar(
        year, month_number
    ):
        columns = st.columns(7, gap="small")
        for day_value, column in zip(week, columns, strict=False):
            day_key = day_value.isoformat()
            summary = by_date.get(day_key)
            with column:
                if day_value.month != month_number:
                    st.markdown(
                        '<div class="dsx-calendar-empty dsx-calendar-empty--outside"></div>',
                        unsafe_allow_html=True,
                    )
                    continue
                if summary is None:
                    st.markdown(
                        '<div class="dsx-calendar-empty">'
                        f'<strong>{day_value.day}</strong><span>—</span></div>',
                        unsafe_allow_html=True,
                    )
                    continue
                selected = day_key == selected_day
                value_class = _value_class(summary.get("fleet_daily_return"))
                if st.button(
                    _calendar_button_label(day_value, summary, selected=selected),
                    key=(
                        f"strategy_calendar_day_{mode}_{day_key.replace('-', '_')}_"
                        f"{value_class}"
                    ),
                    help=(
                        f"{_day_label(day_key)}. Fleet return "
                        f"{format_return_fraction(summary.get('fleet_daily_return'), decimals=4)}. "
                        "Select this day."
                    ),
                    type="primary" if selected else "secondary",
                    width="stretch",
                ):
                    selected_day = day_key
    return selected_day


def _selected_day_strategy_rows(
    rows: list[JsonDict],
    summaries: list[JsonDict],
    selected_day: str,
) -> list[JsonDict]:
    """Outer-join a day onto every official strategy identity."""

    official_summaries = [
        dict(row) for row in summaries if row.get("series_role") == "official"
    ]
    if not official_summaries:
        by_key: dict[str, JsonDict] = {}
        for row in rows:
            if row.get("series_role") == "official":
                by_key.setdefault(str(row.get("series_key")), dict(row))
        official_summaries = list(by_key.values())

    selected_rows = {
        str(row.get("series_key")): dict(row)
        for row in rows
        if row.get("series_role") == "official"
        and str(row.get("date")) == selected_day
    }
    materialized: list[JsonDict] = []
    for summary in official_summaries:
        key = str(summary.get("series_key"))
        materialized_row: JsonDict = {
            **summary,
            "date": selected_day,
            "daily_return_pct": None,
            "total_pnl": None,
            "trades_opened": None,
            "trades_closed": None,
            "pending_orders": None,
            "open_positions": None,
        }
        if key in selected_rows:
            materialized_row.update(selected_rows[key])
        materialized.append(materialized_row)

    def sort_key(row: JsonDict) -> tuple[bool, float, str]:
        value = _number(row.get("daily_return_pct"))
        return (
            value is None,
            -(value if value is not None else 0.0),
            str(row.get("strategy_label") or ""),
        )

    return sorted(materialized, key=sort_key)


def _strategy_day_status(value: object) -> str:
    number = _number(value)
    if number is None:
        return "N/A"
    if number > 0:
        return "Gain"
    if number < 0:
        return "Loss"
    return "Flat"


def _signed_money(value: object) -> str:
    number = _number(value)
    if number is None:
        return "N/A"
    if number > 0:
        return f"+${number:,.2f}"
    if number < 0:
        return f"-${abs(number):,.2f}"
    return "$0.00"


def _simple_return_text(value: object) -> str:
    number = _number(value)
    if number is None:
        return "N/A"
    if number == 0:
        return "0.0000%"
    return f"{number * 100:+.4f}%"


def _selected_day_panel_html(summary: JsonDict, rows: list[JsonDict]) -> str:
    selected_day = str(summary.get("date") or (rows[0].get("date") if rows else ""))
    cards = []
    for index, row in enumerate(rows, start=1):
        value = row.get("daily_return_pct")
        value_class = _value_class(value)
        status = _strategy_day_status(value)
        return_text = _simple_return_text(value)
        secondary = (
            "No verified return"
            if value is None
            else _signed_money(row.get("total_pnl"))
        )
        cards.append(
            f'<article class="dsx-result dsx-result--{value_class}">'
            f'<span class="dsx-result-rank">{index:02d}</span>'
            '<div class="dsx-result-name">'
            f'<strong>{html.escape(str(row.get("strategy_label") or "Unknown strategy"))}</strong>'
            f'<small>{html.escape(status)}</small></div>'
            '<div class="dsx-result-return">'
            f'<strong>{html.escape(return_text)}</strong>'
            f'<small>{html.escape(secondary)}</small></div></article>'
        )
    if not cards:
        cards.append(
            '<div class="dsx-result-empty">No official strategy rows for this day.</div>'
        )
    return (
        '<section class="dsx-selected-day">'
        '<div class="dsx-selected-head"><div>'
        '<span>Selected day</span>'
        f'<h3>{html.escape(_day_label(selected_day)) if selected_day else "N/A"}</h3>'
        f'<small>{len(rows)} official strategies</small></div>'
        '<div class="dsx-selected-fleet"><span>Fleet return</span>'
        f'<strong class="dsx-tone-{_value_class(summary.get("fleet_daily_return"))}">'
        f'{html.escape(_simple_return_text(summary.get("fleet_daily_return")))}'
        '</strong></div></div>'
        f'<div class="dsx-results">{"".join(cards)}</div></section>'
    )


def _kpi_strip(days: list[JsonDict], rows: list[JsonDict]) -> str:
    fleet_returns = [row.get("fleet_daily_return") for row in days]
    benchmark_returns = [row.get("benchmark_daily_return") for row in days]
    fleet_period = _compound(fleet_returns)
    benchmark_period = _compound(benchmark_returns)
    excess = _subtract(fleet_period, benchmark_period)
    pnl = _sum_values(row.get("fleet_daily_pnl") for row in days)
    official = [row for row in rows if row.get("series_role") == "official"]
    drawdowns: list[float | None] = [_number(row.get("drawdown_pct")) for row in official]
    valid_drawdowns = [value for value in drawdowns if value is not None]
    max_drawdown = (
        min(valid_drawdowns)
        if valid_drawdowns and len(valid_drawdowns) == len(drawdowns)
        else None
    )
    closes = _sum_values(row.get("trades_closed") for row in official)
    latest_date = max((str(row.get("date")) for row in official), default="")
    latest_rows = [row for row in official if row.get("date") == latest_date]
    exposure = _sum_values(row.get("exposure_pct") for row in latest_rows)
    cards = [
        ("Fleet period", format_return_fraction(fleet_period), "Official strategies"),
        ("Benchmark", format_return_fraction(benchmark_period), "Configured universe"),
        ("Excess", format_return_fraction(excess), "Fleet minus benchmark"),
        ("Net P&L", _money(pnl), "Session equity change"),
        ("Max drawdown", format_return_fraction(max_drawdown), "Worst strategy mark"),
        ("Closed trades", _integer(closes), f"{len(days)} retained sessions"),
        ("Open exposure", format_return_fraction(exposure), "Latest account marks"),
    ]
    return '<div class="dsx-kpis">' + "".join(
        '<div class="dsx-kpi">'
        f'<span>{html.escape(label)}</span><strong>{html.escape(value)}</strong>'
        f'<small>{html.escape(note)}</small></div>'
        for label, value, note in cards
    ) + "</div>"


def _month_calendar_html(
    days: list[JsonDict], year: int, month_number: int
) -> str:
    by_date = {str(row.get("date")): row for row in days}
    cells = [
        f'<div class="dsx-weekday">{html.escape(day)}</div>'
        for day in calendar_lib.day_abbr
    ]
    for week in calendar_lib.Calendar(firstweekday=0).monthdatescalendar(year, month_number):
        for current in week:
            if current.month != month_number:
                cells.append('<div class="dsx-day dsx-day--outside" aria-hidden="true"></div>')
                continue
            row = by_date.get(current.isoformat())
            if row is None:
                try:
                    session = market_session(current)
                except MarketCalendarCoverageError:
                    state = "missing"
                    label = "Session status unavailable"
                else:
                    state = "missing" if session.is_trading_day else "closed"
                    label = (
                        "No retained evidence"
                        if session.is_trading_day
                        else session.reason.replace("_", " ").title()
                    )
                cells.append(
                    f'<div class="dsx-day dsx-day--{state}">'
                    f'<div class="dsx-day-num">{current.day:02d}</div>'
                    f'<div class="dsx-day-empty">{label}</div></div>'
                )
                continue
            status = str(row.get("status") or "unavailable")
            breadth = (
                f'{int(row.get("positive_strategies") or 0)}+ · '
                f'{int(row.get("negative_strategies") or 0)}- · '
                f'{int(row.get("flat_strategies") or 0)} flat · '
                f'{int(row.get("missing_strategies") or 0)} N/A'
            )
            cells.append(
                f'<div class="dsx-day dsx-day--{html.escape(status)}">'
                f'<div class="dsx-day-top"><span>{current.day:02d}</span>'
                f'<small>{html.escape(status.replace("_", " "))}</small></div>'
                f'<div class="dsx-day-return">'
                f'{html.escape(format_return_fraction(row.get("fleet_daily_return"), decimals=4))}'
                "</div>"
                f'<div class="dsx-day-excess">Excess '
                f'{html.escape(_basis_points(row.get("excess_daily_return")))}</div>'
                f'<div class="dsx-day-breadth">{html.escape(breadth)}</div>'
                f'<div class="dsx-day-activity">'
                f'{html.escape(_integer(row.get("trades_opened")))} fills · '
                f'{html.escape(_integer(row.get("trades_closed")))} closes</div></div>'
            )
    return '<div class="dsx-calendar" role="grid">' + "".join(cells) + "</div>"


def _strategy_matrix_html(
    rows: list[JsonDict],
    summaries: list[JsonDict],
    selected_month: str,
    *,
    show_references: bool,
) -> str:
    dates = sorted({str(row.get("date")) for row in rows})
    summary_by_key = {str(row.get("series_key")): row for row in summaries}
    grouped: dict[str, list[JsonDict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("series_key")), []).append(row)
    body = []
    for role in ("official", "challenger", "benchmark", "cash"):
        if role in {"benchmark", "cash"} and not show_references:
            continue
        role_keys = [
            key for key, values in grouped.items() if values[0].get("series_role") == role
        ]
        if not role_keys:
            continue
        body.append(
            f'<tr class="dsx-group"><th colspan="{len(dates) + 4}">'
            f'{html.escape(_role_label(role))}</th></tr>'
        )
        for key in sorted(
            role_keys,
            key=lambda item: str(grouped[item][0].get("strategy_label") or ""),
        ):
            by_date = {str(row.get("date")): row for row in grouped[key]}
            first = grouped[key][0]
            summary = summary_by_key.get(key, {})
            cells = []
            for session_date in dates:
                day_row = by_date.get(session_date)
                value = day_row.get("daily_return_pct") if day_row else None
                css = _value_class(value) if day_row else "missing"
                cells.append(
                    f'<td class="dsx-cell dsx-cell--{css}" title="'
                    f'{html.escape(_day_label(session_date))} · '
                    f'{html.escape(format_return_fraction(value, decimals=4))}">'
                    f'{html.escape(_compact_return(value))}</td>'
                )
            body.append(
                '<tr>'
                '<th class="dsx-strategy">'
                f'<strong>{html.escape(str(first.get("strategy_label") or ""))}</strong>'
                f'<small>{html.escape(str(first.get("strategy_version") or ""))} · '
                f'{html.escape(str(first.get("execution_policy_version") or ""))}</small></th>'
                + "".join(cells)
                + '<td class="dsx-summary">'
                + html.escape(format_return_fraction(summary.get("period_return")))
                + "</td>"
                + f'<td class="dsx-summary">{html.escape(_money(summary.get("net_pnl")))}</td>'
                + f'<td class="dsx-summary">{int(summary.get("positive_days") or 0)}/'
                f'{int(summary.get("negative_days") or 0)}/'
                f'{int(summary.get("flat_days") or 0)}/'
                f'{int(summary.get("missing_days") or 0)}</td></tr>'
            )
    header_dates = "".join(
        f'<th><span>{html.escape(_matrix_day_label(value))}</span></th>' for value in dates
    )
    return (
        '<div class="dsx-matrix-wrap"><table class="dsx-matrix">'
        '<thead><tr><th class="dsx-strategy">Series</th>'
        f'{header_dates}<th>Period</th><th>Net P&L</th><th>W/L/F/N/A</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
        f'<div class="dsx-matrix-foot">{html.escape(_month_label(selected_month))} · '
        'W/L/F/N/A = positive, negative, flat, missing sessions · '
        'Missing is never zero</div>'
    )


def _day_close_header(summary: JsonDict, rows: list[JsonDict]) -> None:
    official = [row for row in rows if row.get("series_role") == "official"]
    ranked = [row for row in official if row.get("daily_return_pct") is not None]
    best = max(ranked, key=lambda row: float(row["daily_return_pct"])) if ranked else {}
    worst = min(ranked, key=lambda row: float(row["daily_return_pct"])) if ranked else {}
    cards = [
        ("Fleet close", format_return_fraction(summary.get("fleet_daily_return"), decimals=4)),
        ("Benchmark", format_return_fraction(summary.get("benchmark_daily_return"), decimals=4)),
        ("Excess", format_return_fraction(summary.get("excess_daily_return"), decimals=4)),
        (
            "Best series",
            f'{strategy_label(str(best.get("strategy_id") or ""))} '
            f'{format_return_fraction(best.get("daily_return_pct"), decimals=4)}'
            if best
            else "N/A",
        ),
        (
            "Worst series",
            f'{strategy_label(str(worst.get("strategy_id") or ""))} '
            f'{format_return_fraction(worst.get("daily_return_pct"), decimals=4)}'
            if worst
            else "N/A",
        ),
    ]
    st.markdown(
        '<div class="dsx-close-strip">' + "".join(
            '<div><span>' + html.escape(label) + '</span><strong>'
            + html.escape(value) + '</strong></div>' for label, value in cards
        ) + "</div>",
        unsafe_allow_html=True,
    )


def _day_ledger_html(rows: list[JsonDict]) -> str:
    ordered = sorted(
        rows,
        key=lambda row: (
            {"official": 0, "challenger": 1, "benchmark": 2, "cash": 3}.get(
                str(row.get("series_role")), 9
            ),
            str(row.get("strategy_label")),
        ),
    )
    body = []
    for row in ordered:
        costs = _sum_values([row.get("fees_paid"), row.get("slippage_estimate")])
        body.append(
            '<tr>'
            f'<th><strong>{html.escape(str(row.get("strategy_label") or ""))}</strong>'
            f'<small>{html.escape(_role_label(str(row.get("series_role") or "")))}</small></th>'
            f'<td>{html.escape(format_return_fraction(row.get("daily_return_pct"), decimals=4))}'
            "</td>"
            f'<td>{html.escape(format_return_fraction(row.get("cumulative_return_pct")))}</td>'
            f'<td>{html.escape(_money(row.get("total_pnl")))}</td>'
            f'<td>{html.escape(_money(row.get("realized_pnl")))}</td>'
            f'<td>{html.escape(_money(row.get("unrealized_pnl")))}</td>'
            f'<td>{html.escape(format_return_fraction(row.get("drawdown_pct")))}</td>'
            f'<td>{html.escape(_integer(row.get("trades_opened")))} / '
            f'{html.escape(_integer(row.get("trades_closed")))}</td>'
            f'<td>{html.escape(_integer(row.get("open_positions")))} / '
            f'{html.escape(_integer(row.get("pending_orders")))}</td>'
            f'<td>{html.escape(format_return_fraction(row.get("exposure_pct")))}</td>'
            f'<td>{html.escape(_money(costs))}</td></tr>'
        )
    return (
        '<div class="dsx-ledger-wrap"><table class="dsx-ledger"><thead><tr>'
        '<th>Strategy</th><th>Daily</th><th>Cumulative</th><th>Net P&L</th>'
        '<th>Realized</th><th>Open MTM</th><th>Drawdown</th><th>Fills / closes</th>'
        '<th>Open / pending</th><th>Exposure</th><th>Costs</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _lifecycle_panel(view: JsonDict, selected_day: str, selected_series: list[str]) -> None:
    blotter = [
        row
        for row in view.get("blotter_rows") or []
        if str(row.get("series_key") or "") in set(selected_series)
        and _blotter_touches_day(row, selected_day)
    ]
    # Older blotter rows do not retain the combined series_key; match the same immutable fields.
    if not blotter:
        selected_identities = {
            tuple(key.split("|")) for key in selected_series if len(key.split("|")) == 5
        }
        blotter = [
            row
            for row in view.get("blotter_rows") or []
            if (
                str(row.get("mode") or ""),
                str(row.get("strategy_id") or ""),
                str(row.get("strategy_version") or ""),
                str(row.get("execution_policy_version") or ""),
                str(row.get("strategy_semantics_fingerprint") or ""),
            ) in selected_identities
            and _blotter_touches_day(row, selected_day)
        ]
    with st.expander("Trade lifecycle and decision evidence", expanded=False):
        if not view.get("blotter_verified"):
            st.warning("The lifecycle blotter is not verified for this evidence lane.")
            return
        statuses = Counter(str(row.get("lifecycle_status") or "unknown") for row in blotter)
        if statuses:
            st.caption(
                " · ".join(
                    f"{key.replace('_', ' ').title()}: {value}"
                    for key, value in sorted(statuses.items())
                )
            )
        actionable = [row for row in blotter if row.get("lifecycle_status") != "no_setup"]
        if not actionable:
            st.info("No accepted, pending, open, or closed lifecycle rows for this selection.")
            return
        frame = pd.DataFrame(
            [
                {
                    "Strategy": strategy_label(str(row.get("strategy_id") or "")),
                    "Symbol": row.get("symbol"),
                    "Decision": str(row.get("decision_status") or "").replace("_", " "),
                    "Lifecycle": str(row.get("lifecycle_status") or "").replace("_", " "),
                    "Direction": row.get("direction"),
                    "Fill": row.get("fill_price"),
                    "Mark": row.get("last_mark_price"),
                    "Close": row.get("close_price"),
                    "Reason": row.get("close_reason") or row.get("decision_reason"),
                    "Net P&L": row.get("net_pnl"),
                    "Return": format_return_fraction(
                        _percent_to_fraction(row.get("trade_return_pct"))
                    ),
                    "R": row.get("r_multiple"),
                }
                for row in actionable
            ]
        )
        st.dataframe(frame, hide_index=True, width="stretch")


def _strategy_scorecard_html(summaries: list[JsonDict]) -> str:
    body = []
    for row in summaries:
        body.append(
            '<tr>'
            f'<th><strong>{html.escape(str(row.get("strategy_label") or ""))}</strong>'
            f'<small>{html.escape(_role_label(str(row.get("series_role") or "")))} · '
            f'{html.escape(str(row.get("strategy_version") or ""))}</small></th>'
            f'<td>{html.escape(format_return_fraction(row.get("period_return")))}</td>'
            f'<td>{html.escape(_money(row.get("ending_equity")))}</td>'
            f'<td>{html.escape(_money(row.get("net_pnl")))}</td>'
            f'<td>{html.escape(format_return_fraction(row.get("max_drawdown")))}</td>'
            f'<td>{int(row.get("positive_days") or 0)} / '
            f'{int(row.get("negative_days") or 0)} / '
            f'{int(row.get("flat_days") or 0)} / '
            f'{int(row.get("missing_days") or 0)}</td>'
            f'<td>{html.escape(_integer(row.get("trades_opened")))} / '
            f'{html.escape(_integer(row.get("trades_closed")))}</td>'
            f'<td>{html.escape(_integer(row.get("open_positions")))} / '
            f'{html.escape(_integer(row.get("pending_orders")))}</td>'
            f'<td>{html.escape(format_return_fraction(row.get("latest_exposure")))}</td></tr>'
        )
    return (
        '<div class="dsx-ledger-wrap"><table class="dsx-ledger"><thead><tr>'
        '<th>Series</th><th>Return</th><th>Ending equity</th><th>Net P&L</th>'
        '<th>Max drawdown</th><th>W / L / F / N/A</th><th>Fills / closes</th>'
        '<th>Open / pending</th><th>Exposure</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def _return_curve(rows: list[JsonDict], mode: str) -> alt.Chart:
    frame = pd.DataFrame(
        [
            {
                "date": row.get("date"),
                "series": row.get("strategy_label"),
                "series_key": row.get("series_key"),
                "version": row.get("strategy_version"),
                "policy": row.get("execution_policy_version"),
                "fingerprint": str(row.get("strategy_semantics_fingerprint") or "")[:12],
                "return_pct": _fraction_to_percent(row.get("cumulative_return_pct")),
                "role": row.get("series_role"),
            }
            for row in rows
            if row.get("cumulative_return_pct") is not None
        ]
    )
    if frame.empty:
        return _empty_chart("No cumulative return evidence")
    chart = (
        alt.Chart(frame)
        .mark_line(point=alt.OverlayMarkDef(size=35), strokeWidth=2)
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=0)),
            y=alt.Y("return_pct:Q", title="Cumulative return %"),
            color=alt.Color("series:N", title=None),
            detail=alt.Detail("series_key:N"),
            strokeDash=alt.StrokeDash(
                "role:N",
                title=None,
                scale=alt.Scale(
                    domain=["official", "challenger", "benchmark", "cash"],
                    range=[[1, 0], [6, 3], [4, 2], [2, 2]],
                ),
            ),
            tooltip=[
                alt.Tooltip("date:T", title="Session"),
                alt.Tooltip("series:N", title="Series"),
                alt.Tooltip("version:N", title="Version"),
                alt.Tooltip("policy:N", title="Execution policy"),
                alt.Tooltip("fingerprint:N", title="Semantics"),
                alt.Tooltip("return_pct:Q", title="Return %", format="+.3f"),
            ],
        )
        .properties(
            height=300,
            title=f"{_MODE_LABELS.get(mode, mode.title())} · cumulative return",
        )
    )
    return _style_chart(chart)


def _drawdown_curve(rows: list[JsonDict], mode: str) -> alt.Chart:
    frame = pd.DataFrame(
        [
            {
                "date": row.get("date"),
                "series": row.get("strategy_label"),
                "series_key": row.get("series_key"),
                "version": row.get("strategy_version"),
                "policy": row.get("execution_policy_version"),
                "fingerprint": str(row.get("strategy_semantics_fingerprint") or "")[:12],
                "drawdown_pct": _fraction_to_percent(row.get("drawdown_pct")),
            }
            for row in rows
            if row.get("series_role") in {"official", "challenger"}
            and row.get("drawdown_pct") is not None
        ]
    )
    if frame.empty:
        return _empty_chart("No drawdown evidence")
    chart = (
        alt.Chart(frame)
        .mark_area(opacity=0.08, line={"strokeWidth": 1.8})
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=0)),
            y=alt.Y("drawdown_pct:Q", title="Drawdown %"),
            color=alt.Color("series:N", title=None),
            detail=alt.Detail("series_key:N"),
            tooltip=[
                alt.Tooltip("date:T", title="Session"),
                alt.Tooltip("series:N", title="Series"),
                alt.Tooltip("version:N", title="Version"),
                alt.Tooltip("policy:N", title="Execution policy"),
                alt.Tooltip("fingerprint:N", title="Semantics"),
                alt.Tooltip("drawdown_pct:Q", title="Drawdown %", format=".3f"),
            ],
        )
        .properties(height=300, title=f"{_MODE_LABELS.get(mode, mode.title())} · drawdown")
    )
    return _style_chart(chart)


def _empty_chart(message: str) -> alt.Chart:
    return _style_chart(
        alt.Chart(pd.DataFrame({"message": [message]}))
        .mark_text(color="#64748b", fontSize=14)
        .encode(text="message:N")
        .properties(height=300)
    )


def _style_chart(chart: alt.Chart) -> alt.Chart:
    return (
        chart.configure_view(stroke="#dbe3ee", fill="#ffffff")
        .configure(background="#ffffff")
        .configure_title(anchor="start", color="#0f172a", fontSize=14, fontWeight=700)
        .configure_axis(
            domainColor="#cbd5e1",
            gridColor="#edf2f7",
            labelColor="#64748b",
            titleColor="#475569",
        )
        .configure_legend(labelColor="#475569", titleColor="#334155")
    )


def _evidence_panel(dataset: JsonDict, view: JsonDict | None) -> None:
    with st.expander("Evidence, lineage, and limitations", expanded=False):
        gates = dataset.get("gates") or {}
        gate_rows = [
            {
                "Gate": str(name).replace("_", " ").title(),
                "Status": str(payload.get("status") or "missing").upper(),
                "Warnings": len(payload.get("warnings") or []),
                "Path": payload.get("path"),
            }
            for name, payload in gates.items()
            if isinstance(payload, dict)
        ]
        if gate_rows:
            st.dataframe(pd.DataFrame(gate_rows), hide_index=True, width="stretch")
        st.caption(
            "Source SHA-256: " + str(dataset.get("source_sha256") or "N/A")
            + " · Research and paper execution only · No broker execution."
        )
        warnings = sorted(
            dict.fromkeys(
                [
                    *[str(item) for item in dataset.get("warnings") or []],
                    *[str(item) for item in (view or {}).get("warnings") or []],
                ]
            )
        )
        if warnings:
            for warning in warnings:
                st.warning(warning)
        st.caption(
            "Yahoo daily bars are public research data, not broker-grade executions. "
            "Returns include the configured paper fees and slippage model. Missing truth is N/A."
        )


def _calendar_styles() -> None:
    st.markdown(
        """
        <style>
        .block-container { max-width: 1480px; }
        [class*="st-key-strategy_calendar_"] [data-testid="stWidgetLabel"] p {
            color: #475569 !important; font-size: .72rem !important;
            font-weight: 750 !important;
        }
        .st-key-calendar_view button[kind="segmented_control"],
        .st-key-strategy_calendar_mode button[kind="segmented_control"] {
            background: #f8fafc !important; border-color: #cbd5e1 !important;
            color: #334155 !important;
        }
        .st-key-calendar_view button[kind="segmented_control"] p,
        .st-key-strategy_calendar_mode button[kind="segmented_control"] p {
            color: #334155 !important; font-weight: 750 !important;
        }
        .st-key-calendar_view button[kind="segmented_controlActive"],
        .st-key-strategy_calendar_mode button[kind="segmented_controlActive"] {
            background: #10233d !important; border-color: #10233d !important;
            color: #f8fafc !important;
        }
        .st-key-calendar_view button[kind="segmented_controlActive"] p,
        .st-key-strategy_calendar_mode button[kind="segmented_controlActive"] p {
            color: #f8fafc !important; font-weight: 800 !important;
        }
        [class*="st-key-strategy_calendar_series_"] [data-baseweb="tag"] {
            background: #dce9f7 !important; color: #173b61 !important;
        }
        [class*="st-key-strategy_calendar_series_"] [data-baseweb="tag"] span,
        [class*="st-key-strategy_calendar_series_"] [data-baseweb="tag"] svg {
            color: #173b61 !important; fill: #173b61 !important;
        }
        .dsx-hero, .dsx-kpis, .dsx-calendar, .dsx-matrix, .dsx-ledger,
        .dsx-close-strip { font-variant-numeric: tabular-nums; }
        .dsx-hero {
            background: radial-gradient(circle at 85% 10%, #193456 0, #0b1b2f 33%, #07111f 72%);
            border: 1px solid #1d3552; border-radius: 14px; color: #e7eef8;
            margin: .35rem 0 .75rem; overflow: hidden; padding: .9rem 1.05rem;
            box-shadow: 0 20px 45px rgba(7,17,31,.16);
        }
        .dsx-eyebrow {
            color: #7dd3fc; font-size: .68rem; font-weight: 850;
            letter-spacing: .14em;
        }
        .dsx-hero-row {
            align-items: flex-start; display: flex; gap: 1rem;
            justify-content: space-between;
        }
        .dsx-hero h2 {
            color: #f8fbff; font-size: 1.55rem; letter-spacing: -.035em;
            margin: 0;
        }
        .dsx-hero p { color: #aebed2; font-size: .8rem; margin: .18rem 0 0; }
        .dsx-hero-status {
            color: #a7f3d0; display: inline-block; font-size: .7rem;
            font-weight: 800; letter-spacing: .02em; margin-top: .48rem;
        }
        .dsx-simple-section {
            align-items: baseline; display: flex; justify-content: space-between;
            margin: 1rem 0 .45rem;
        }
        .dsx-simple-section strong { color: #172033; font-size: .95rem; }
        .dsx-simple-section span { color: #708198; font-size: .72rem; }
        .st-key-strategy_calendar_picker [data-testid="stHorizontalBlock"] {
            gap: .4rem;
        }
        .st-key-strategy_calendar_picker [data-testid="stMarkdownContainer"] p {
            margin: 0;
        }
        .dsx-weekday-simple {
            color: #718096; font-size: .62rem; font-weight: 850;
            letter-spacing: .08em; padding: .12rem 0 .22rem;
            text-align: center; text-transform: uppercase;
        }
        .dsx-calendar-empty {
            align-items: center; background: #f8fafc; border: 1px solid #e6ecf2;
            border-radius: 10px; color: #94a3b8; display: flex;
            flex-direction: column; justify-content: center; min-height: 58px;
        }
        .dsx-calendar-empty strong { color: #64748b; font-size: .72rem; }
        .dsx-calendar-empty span { font-size: .7rem; margin-top: .15rem; }
        .dsx-calendar-empty--outside {
            background: transparent; border-color: transparent;
        }
        [class*="st-key-strategy_calendar_day_"] button {
            border-radius: 10px !important; min-height: 58px !important;
            padding: .38rem .18rem !important; transition: all .15s ease !important;
        }
        [class*="st-key-strategy_calendar_day_"] button p {
            font-size: .68rem !important; line-height: 1.45 !important;
            text-align: center !important; white-space: normal !important;
        }
        [class*="st-key-strategy_calendar_day_"] button:focus-visible {
            outline: 3px solid #38bdf8 !important; outline-offset: 2px !important;
        }
        [class*="st-key-strategy_calendar_day_"][class*="_positive"] button[kind="secondary"] {
            background: #ecfdf3 !important; border-color: #9fdbb7 !important;
            color: #166534 !important;
        }
        [class*="st-key-strategy_calendar_day_"][class*="_negative"] button[kind="secondary"] {
            background: #fff1f2 !important; border-color: #fecdd3 !important;
            color: #9f1239 !important;
        }
        [class*="st-key-strategy_calendar_day_"][class*="_missing"] button[kind="secondary"] {
            background: #f8fafc !important; border-color: #dbe3ee !important;
            color: #64748b !important;
        }
        [class*="st-key-strategy_calendar_day_"] button[kind="primary"] {
            background: #10233d !important; border-color: #10233d !important;
            box-shadow: 0 8px 20px rgba(15,35,61,.16) !important;
            color: #f8fafc !important;
        }
        .dsx-selected-day {
            background: #ffffff; border: 1px solid #dbe3ee; border-radius: 16px;
            box-shadow: 0 14px 35px rgba(15,23,42,.07); margin: 1rem 0 .7rem;
            overflow: hidden;
        }
        .dsx-selected-head {
            align-items: center; background: #f8fafc; border-bottom: 1px solid #e4eaf1;
            display: flex; justify-content: space-between; padding: 1rem 1.1rem;
        }
        .dsx-selected-head span, .dsx-selected-fleet span {
            color: #708198; display: block; font-size: .61rem; font-weight: 850;
            letter-spacing: .07em; text-transform: uppercase;
        }
        .dsx-selected-head h3 {
            color: #172033; font-size: 1.12rem; letter-spacing: -.02em;
            margin: .16rem 0 .05rem;
        }
        .dsx-selected-head small { color: #7b8ba2; font-size: .68rem; }
        .dsx-selected-fleet { text-align: right; }
        .dsx-selected-fleet strong {
            display: block; font-size: 1.35rem; line-height: 1.1; margin-top: .2rem;
        }
        .dsx-tone-positive, .dsx-result--positive .dsx-result-return strong {
            color: #087443;
        }
        .dsx-tone-negative, .dsx-result--negative .dsx-result-return strong {
            color: #b42335;
        }
        .dsx-tone-flat, .dsx-result--flat .dsx-result-return strong {
            color: #334155;
        }
        .dsx-tone-missing, .dsx-result--missing .dsx-result-return strong {
            color: #7b8ba2;
        }
        .dsx-results { padding: .3rem 1rem .65rem; }
        .dsx-result {
            align-items: center; border-bottom: 1px solid #edf1f5; display: grid;
            gap: .7rem; grid-template-columns: 34px minmax(0,1fr) minmax(130px,auto);
            min-height: 62px; padding: .55rem .15rem;
        }
        .dsx-result:last-child { border-bottom: 0; }
        .dsx-result-rank {
            color: #94a3b8; font-size: .66rem; font-weight: 850;
            font-variant-numeric: tabular-nums;
        }
        .dsx-result-name strong {
            color: #172033; display: block; font-size: .8rem;
        }
        .dsx-result-name small {
            color: #708198; display: block; font-size: .64rem; margin-top: .12rem;
        }
        .dsx-result-return { text-align: right; }
        .dsx-result-return strong {
            display: block; font-size: 1.03rem; font-variant-numeric: tabular-nums;
        }
        .dsx-result-return small {
            color: #7b8ba2; display: block; font-size: .62rem; margin-top: .12rem;
        }
        .dsx-result-empty { color: #64748b; padding: 1rem; text-align: center; }
        .dsx-live {
            color: #a7f3d0; font-size: .7rem; font-weight: 850;
            letter-spacing: .08em; margin-top: .5rem;
        }
        .dsx-live span {
            background: #34d399; border-radius: 50%;
            box-shadow: 0 0 0 4px rgba(52,211,153,.15);
            display: inline-block; height: 7px; margin-right: .45rem; width: 7px;
        }
        .dsx-chips { display: flex; flex-wrap: wrap; gap: .42rem; margin-top: .9rem; }
        .dsx-chip {
            background: rgba(255,255,255,.06);
            border: 1px solid rgba(255,255,255,.12); border-radius: 999px;
            color: #c6d4e6; font-size: .68rem; padding: .25rem .55rem;
        }
        .dsx-chip--good { border-color: rgba(52,211,153,.35); color: #a7f3d0; }
        .dsx-hero-foot {
            border-top: 1px solid rgba(255,255,255,.09); color: #7890aa;
            font-size: .68rem; margin-top: .9rem; padding-top: .7rem;
        }
        .dsx-notice {
            background: #f0f7ff; border: 1px solid #c7dcf7;
            border-left: 4px solid #2563eb; border-radius: 10px;
            margin: .65rem 0 1rem; padding: .75rem .9rem;
        }
        .dsx-notice strong { color: #173b6c; display: block; font-size: .82rem; }
        .dsx-notice span { color: #4d6684; font-size: .78rem; }
        .dsx-kpis {
            display: grid; gap: .62rem;
            grid-template-columns: repeat(7,minmax(0,1fr)); margin: .8rem 0 1.1rem;
        }
        .dsx-kpi {
            background: #fff; border: 1px solid #dbe3ee; border-radius: 12px;
            min-height: 92px; padding: .75rem .8rem;
        }
        .dsx-kpi span {
            color: #64748b; display: block; font-size: .65rem; font-weight: 800;
            letter-spacing: .035em; text-transform: uppercase;
        }
        .dsx-kpi strong {
            color: #0f172a; display: block; font-size: 1.18rem;
            line-height: 1.15; margin-top: .32rem;
        }
        .dsx-kpi small { color: #7b8ba2; display: block; font-size: .67rem; margin-top: .35rem; }
        .dsx-section-head {
            align-items: end; display: flex; justify-content: space-between;
            margin: 1.15rem 0 .5rem;
        }
        .dsx-section-head strong { color: #172033; font-size: .92rem; }
        .dsx-section-head span { color: #708198; font-size: .73rem; }
        .dsx-calendar {
            display: grid; gap: .5rem;
            grid-template-columns: repeat(7,minmax(130px,1fr));
            overflow-x: auto; padding-bottom: .3rem;
        }
        .dsx-weekday {
            color: #64748b; font-size: .65rem; font-weight: 850;
            letter-spacing: .08em; padding: .25rem .55rem; text-transform: uppercase;
        }
        .dsx-day {
            background: #fff; border: 1px solid #d9e2ec; border-radius: 12px;
            min-height: 138px; padding: .68rem .72rem;
        }
        .dsx-day--positive { background: #ecfdf3; border-color: #9fdbb7; }
        .dsx-day--negative { background: #fef2f2; border-color: #f2b8b8; }
        .dsx-day--flat, .dsx-day--flat_with_activity { background: #f8fafc; }
        .dsx-day--missing {
            background: repeating-linear-gradient(
                135deg,#f8fafc,#f8fafc 8px,#f1f5f9 8px,#f1f5f9 16px
            );
        }
        .dsx-day--closed, .dsx-day--outside {
            background: transparent; border-color: #e8edf3; box-shadow: none;
        }
        .dsx-day-top { display: flex; justify-content: space-between; }
        .dsx-day-top span { color: #334155; font-size: .78rem; font-weight: 850; }
        .dsx-day-top small {
            color: #64748b; font-size: .55rem; font-weight: 800;
            text-transform: uppercase;
        }
        .dsx-day-return {
            color: #0f172a; font-size: 1.16rem; font-weight: 850;
            margin-top: .75rem;
        }
        .dsx-day-excess, .dsx-day-breadth, .dsx-day-activity, .dsx-day-empty {
            color: #607089; font-size: .68rem; margin-top: .28rem;
        }
        .dsx-day-empty { margin-top: 2.1rem; text-align: center; }
        .dsx-matrix-wrap, .dsx-ledger-wrap {
            background: #fff; border: 1px solid #dbe3ee; border-radius: 12px;
            overflow: auto;
        }
        .dsx-matrix, .dsx-ledger {
            border-collapse: separate; border-spacing: 0;
            min-width: 100%; width: max-content;
        }
        .dsx-matrix th, .dsx-matrix td, .dsx-ledger th, .dsx-ledger td {
            border-bottom: 1px solid #e7edf4; color: #334155; font-size: .69rem;
            padding: .58rem .62rem; text-align: right; white-space: nowrap;
        }
        .dsx-matrix thead th, .dsx-ledger thead th {
            background: #f8fafc; color: #64748b; font-size: .61rem;
            font-weight: 850; letter-spacing: .035em; position: sticky;
            text-transform: uppercase; top: 0; z-index: 2;
        }
        .dsx-strategy {
            background: #fff !important; left: 0; min-width: 220px;
            position: sticky; text-align: left !important; z-index: 3 !important;
        }
        .dsx-strategy strong, .dsx-ledger th strong {
            color: #172033; display: block; font-size: .72rem;
        }
        .dsx-strategy small, .dsx-ledger th small {
            color: #8391a5; display: block; font-size: .56rem;
            font-weight: 600; margin-top: .15rem;
        }
        .dsx-cell { font-weight: 800; min-width: 72px; }
        .dsx-cell--positive { background: #ecfdf3; color: #166534 !important; }
        .dsx-cell--negative { background: #fef2f2; color: #991b1b !important; }
        .dsx-cell--flat { background: #f8fafc; color: #475569 !important; }
        .dsx-cell--missing {
            background: repeating-linear-gradient(
                135deg,#fafbfc,#fafbfc 6px,#f1f5f9 6px,#f1f5f9 12px
            );
            color: #94a3b8 !important;
        }
        .dsx-summary { background: #fbfcfe; font-weight: 800; }
        .dsx-group th {
            background: #eef3f8 !important; color: #52657c !important;
            font-size: .58rem !important; letter-spacing: .08em;
            text-align: left !important; text-transform: uppercase;
        }
        .dsx-matrix-foot { color: #7a899e; font-size: .65rem; margin: .35rem .2rem 1rem; }
        .dsx-close-strip {
            display: grid; gap: .6rem;
            grid-template-columns: repeat(5,minmax(0,1fr)); margin: .6rem 0;
        }
        .dsx-close-strip div {
            background: #f8fafc; border: 1px solid #e1e8f0;
            border-radius: 10px; padding: .62rem .72rem;
        }
        .dsx-close-strip span {
            color: #718096; display: block; font-size: .61rem;
            font-weight: 800; text-transform: uppercase;
        }
        .dsx-close-strip strong {
            color: #172033; display: block; font-size: .82rem;
            margin-top: .22rem;
        }
        .dsx-ledger th { min-width: 200px; text-align: left; }
        .dsx-ledger td { min-width: 88px; }
        @media (max-width: 1100px) {
            .dsx-kpis { grid-template-columns: repeat(4,minmax(0,1fr)); }
        }
        @media (max-width: 760px) {
            .block-container { padding-left: .7rem; padding-right: .7rem; }
            .dsx-hero { border-radius: 14px; padding: 1rem; }
            .dsx-hero-row { display: block; }
            .dsx-live { margin-top: .8rem; }
            .dsx-simple-section { align-items: flex-start; display: block; }
            .dsx-simple-section span { display: block; margin-top: .12rem; }
            .st-key-strategy_calendar_picker [data-testid="stHorizontalBlock"] {
                flex-wrap: nowrap !important; gap: .2rem;
            }
            .st-key-strategy_calendar_picker [data-testid="stHorizontalBlock"]
            > [data-testid="stColumn"] {
                flex: 1 1 0 !important; min-width: 0 !important; width: auto !important;
            }
            .dsx-calendar-empty,
            [class*="st-key-strategy_calendar_day_"] button {
                border-radius: 8px !important; min-height: 58px !important;
            }
            [class*="st-key-strategy_calendar_day_"] button p {
                font-size: .56rem !important; line-height: 1.35 !important;
            }
            .dsx-selected-head { align-items: flex-start; padding: .85rem; }
            .dsx-selected-head h3 { font-size: .95rem; }
            .dsx-selected-fleet strong { font-size: 1.05rem; }
            .dsx-results { padding: .2rem .75rem .55rem; }
            .dsx-result {
                gap: .45rem; grid-template-columns: 24px minmax(0,1fr) minmax(104px,auto);
                min-height: 58px;
            }
            .dsx-result-name strong { font-size: .72rem; }
            .dsx-result-return strong { font-size: .86rem; }
            .dsx-kpis { grid-template-columns: repeat(2,minmax(0,1fr)); }
            .dsx-calendar { grid-template-columns: repeat(7,minmax(122px,1fr)); }
            .dsx-close-strip { grid-template-columns: repeat(2,minmax(0,1fr)); }
            .dsx-section-head { align-items: flex-start; display: block; }
            .dsx-section-head span { display: block; margin-top: .15rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _notice(title: str, copy: str) -> str:
    return (
        '<div class="dsx-notice"><strong>' + html.escape(title) + '</strong><span>'
        + html.escape(copy) + "</span></div>"
    )


def _section_header(title: str, note: str) -> str:
    return (
        '<div class="dsx-section-head"><strong>' + html.escape(title) + '</strong><span>'
        + html.escape(note) + "</span></div>"
    )


def _rows_for_month(rows: list[JsonDict], month: str) -> list[JsonDict]:
    return [dict(row) for row in rows if str(row.get("date") or "").startswith(month)]


def _days_for_month(rows: list[JsonDict], month: str) -> list[JsonDict]:
    return [dict(row) for row in rows if str(row.get("date") or "").startswith(month)]


def _series_option(summary: JsonDict) -> str:
    return f'{summary.get("strategy_label", "Unknown")} · {summary.get("strategy_version", "")}'


def _role_label(role: str) -> str:
    return {
        "official": "Official strategies",
        "challenger": "Shadow challengers",
        "benchmark": "Configured-universe benchmark",
        "cash": "Cash reference",
        "unregistered": "Unregistered series",
    }.get(role, role.replace("_", " ").title())


def _month_label(value: str) -> str:
    return date.fromisoformat(f"{value}-01").strftime("%B %Y")


def _day_label(value: str) -> str:
    return date.fromisoformat(str(value)[:10]).strftime("%A, %B %d, %Y")


def _matrix_day_label(value: str) -> str:
    return date.fromisoformat(str(value)[:10]).strftime("%b %d")


def _value_class(value: object) -> str:
    number = _number(value)
    if number is None:
        return "missing"
    if number > 0:
        return "positive"
    if number < 0:
        return "negative"
    return "flat"


def _compact_return(value: object) -> str:
    number = _number(value)
    if number is None:
        return "N/A"
    if number == 0:
        return "Flat"
    return f"{number * 100:+.4f}%"


def _basis_points(value: object) -> str:
    number = _number(value)
    return "N/A" if number is None else f"{number * 10_000:+.1f} bp"


def _money(value: object) -> str:
    number = _number(value)
    if number is None:
        return "N/A"
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def _integer(value: object) -> str:
    number = _number(value)
    return "N/A" if number is None else f"{int(number):,}"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) and number not in {float("inf"), float("-inf")} else None


def _compound(values: list[object]) -> float | None:
    numbers = [_number(value) for value in values]
    if not numbers:
        return None
    factor = 1.0
    for value in numbers:
        if value is None:
            return None
        factor *= 1.0 + value
    return factor - 1.0


def _sum_values(values: Any) -> float | None:
    items = [_number(value) for value in values]
    if not items:
        return None
    total = 0.0
    for value in items:
        if value is None:
            return None
        total += value
    return total


def _subtract(left: object, right: object) -> float | None:
    left_number = _number(left)
    right_number = _number(right)
    if left_number is None or right_number is None:
        return None
    return left_number - right_number


def _fraction_to_percent(value: object) -> float | None:
    number = _number(value)
    return number * 100 if number is not None else None


def _percent_to_fraction(value: object) -> float | None:
    number = _number(value)
    return number / 100 if number is not None else None


def _blotter_touches_day(row: JsonDict, selected_day: str) -> bool:
    return any(
        str(row.get(field) or "")[:10] == selected_day
        for field in ("signal_time", "fill_time", "close_time", "earliest_fill_date")
    )
