# ruff: noqa: E501
"""Dependency-free HTML rendering for the mover strategy calendar.

The renderer consumes the retained ``strategy_daily_calendar`` rows emitted by
the mover-pattern analysis pipeline.  It intentionally performs no return
calculation: the report presents the already reconciled values and preserves
null outcomes as null.
"""

from __future__ import annotations

import calendar
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from html import escape
from pathlib import Path
from typing import Any

_EM_DASH = "\N{EM DASH}"
_MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_KNOWN_STRATEGY_NAMES = {
    "mover_opening_drive_rvol_v1": "Opening Drive + Same-Clock RVOL",
    "mover_verified_catalyst_gap_hold_v1": "Verified Catalyst Gap Hold",
}
_MODE_ORDER = {
    "forward_observation": 0,
    "historical_replay": 1,
}
_MODE_LABELS = {
    "forward_observation": "Forward observation",
    "historical_replay": "Historical replay",
}
_NULL_STATUSES = {
    "not_evaluated",
    "skipped",
    "incomplete",
    "pending",
    "pending_outcome",
    "missing_outcome",
}


@dataclass(frozen=True)
class _CalendarRow:
    market_date: date
    strategy_id: str
    strategy_version: str
    evidence_mode: str
    status: str
    paper_book_return_pct: float | None
    pnl: float | None
    capital_deployed: float | None
    decision_count: int
    signal_count: int
    closed_trade_count: int
    pending_trade_count: int
    not_entered_count: int
    symbols: tuple[str, ...]
    return_semantics: str
    learning_eligible: bool


def render_strategy_calendar_report(
    payload: Mapping[str, Any],
    *,
    title: str = "Dawnstrike Strategy Calendar",
) -> str:
    """Return a deterministic, self-contained strategy calendar document.

    ``payload`` may be the complete analysis mapping returned by
    ``mover_pattern_lab.core.analyze`` or a calendar wrapper whose ``rows`` or
    ``calendar`` member contains those same calendar row mappings.

    The report is research-only.  It never converts missing outcomes to zero,
    never combines replay evidence with forward evidence, and contains no
    scripts or network dependencies.
    """

    rows = _parse_rows(payload)
    grouped: dict[date, list[_CalendarRow]] = defaultdict(list)
    for row in rows:
        grouped[row.market_date].append(row)
    for day_rows in grouped.values():
        day_rows.sort(key=_row_sort_key)

    escaped_title = _html(title)
    schema_version = _html(str(payload.get("schema_version") or "calendar"))
    body = _render_calendar(grouped)
    summary = _render_summary(rows)
    date_range = _date_range_label(rows)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
  <title>{escaped_title}</title>
  <style>
{_STYLES}
  </style>
</head>
<body>
  <a class="skip-link" href="#calendar">Skip to calendar</a>
  <main class="shell">
    <header class="masthead">
      <div>
        <p class="eyebrow">DAWNSTRIKE / PAPER RESEARCH</p>
        <h1>{escaped_title}</h1>
        <p class="range">{_html(date_range)}</p>
      </div>
      <div class="integrity" role="note">
        <span class="integrity-dot" aria-hidden="true"></span>
        <span>Missing truth stays missing</span>
      </div>
    </header>

    {summary}

    <section class="truth-bar" aria-label="Evidence guide">
      <div><span class="mode-pill forward">FORWARD</span><p>Live point-in-time observation. Eligible for learning only when complete.</p></div>
      <div><span class="mode-pill replay">REPLAY</span><p>Historical research only. Never blended into forward performance.</p></div>
      <div><span class="metric-null">{_EM_DASH}</span><p>Not evaluated or incomplete. It is not a zero return.</p></div>
    </section>

    <section id="calendar" class="calendar" aria-label="Strategy performance calendar">
      {body}
    </section>

    <footer>
      <p>Research and paper-audit only. No broker execution. Returns are after-cost only where the retained row says so.</p>
      <p>Schema: <code>{schema_version}</code></p>
    </footer>
  </main>
</body>
</html>
"""


def write_strategy_calendar_report(
    payload: Mapping[str, Any],
    output_path: Path,
    *,
    title: str = "Dawnstrike Strategy Calendar",
) -> Path:
    """Write the deterministic report as UTF-8 and return its resolved path."""

    html = render_strategy_calendar_report(payload, title=title)
    destination = output_path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8", newline="\n")
    return destination


def _parse_rows(payload: Mapping[str, Any]) -> list[_CalendarRow]:
    raw_rows: object | None = payload.get("strategy_daily_calendar")
    if raw_rows is None:
        raw_rows = payload.get("calendar")
    if raw_rows is None:
        raw_rows = payload.get("rows")
    if raw_rows is None:
        return []
    if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
        raise ValueError("strategy calendar rows must be a sequence of mappings")

    parsed: list[_CalendarRow] = []
    seen: set[tuple[date, str, str, str]] = set()
    for index, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, Mapping):
            raise ValueError(f"strategy calendar row {index} must be a mapping")
        row = _parse_row(raw_row, index=index)
        identity = (
            row.market_date,
            row.strategy_id,
            row.strategy_version,
            row.evidence_mode,
        )
        if identity in seen:
            raise ValueError(
                "duplicate strategy calendar row: "
                f"{row.market_date.isoformat()} {row.strategy_id} "
                f"{row.strategy_version} {row.evidence_mode}"
            )
        seen.add(identity)
        parsed.append(row)
    return sorted(parsed, key=lambda row: (row.market_date, *_row_sort_key(row)))


def _parse_row(raw: Mapping[object, object], *, index: int) -> _CalendarRow:
    market_date_text = str(raw.get("market_date") or "")
    try:
        market_date = date.fromisoformat(market_date_text)
    except ValueError as exc:
        raise ValueError(
            f"strategy calendar row {index} has invalid market_date"
        ) from exc
    strategy_id = str(raw.get("strategy_id") or "unknown_strategy")
    strategy_version = str(raw.get("strategy_version") or "unknown_version")
    evidence_mode = str(raw.get("evidence_mode") or "historical_replay")
    status = str(raw.get("status") or "not_evaluated").lower()
    return_pct = _optional_number(raw.get("paper_book_return_pct"))
    pnl = _optional_number(raw.get("pnl"))
    capital_deployed = _optional_number(raw.get("capital_deployed"))
    if status in _NULL_STATUSES:
        return_pct = None
        pnl = None
        capital_deployed = None
    raw_symbols = raw.get("symbols")
    if raw_symbols is None:
        symbols: tuple[str, ...] = ()
    elif isinstance(raw_symbols, (str, bytes)) or not isinstance(
        raw_symbols, Sequence
    ):
        raise ValueError(f"strategy calendar row {index} symbols must be a sequence")
    else:
        symbols = tuple(sorted({str(symbol) for symbol in raw_symbols if str(symbol)}))
    return _CalendarRow(
        market_date=market_date,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        evidence_mode=evidence_mode,
        status=status,
        paper_book_return_pct=return_pct,
        pnl=pnl,
        capital_deployed=capital_deployed,
        decision_count=_nonnegative_int(raw.get("decision_count"), index=index),
        signal_count=_nonnegative_int(raw.get("signal_count"), index=index),
        closed_trade_count=_nonnegative_int(
            raw.get("closed_trade_count"), index=index
        ),
        pending_trade_count=_nonnegative_int(
            raw.get("pending_trade_count"), index=index
        ),
        not_entered_count=_nonnegative_int(
            raw.get("not_entered_count"), index=index
        ),
        symbols=symbols,
        return_semantics=str(raw.get("return_semantics") or "Return basis not retained"),
        learning_eligible=(
            bool(raw.get("learning_eligible", False))
            and status == "complete"
            and evidence_mode == "forward_observation"
        ),
    )


def _optional_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_int(value: object, *, index: int) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"strategy calendar row {index} has an invalid count")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"strategy calendar row {index} has an invalid count"
        ) from exc
    if number < 0:
        raise ValueError(f"strategy calendar row {index} has a negative count")
    return number


def _row_sort_key(row: _CalendarRow) -> tuple[int, str, str, str]:
    return (
        _MODE_ORDER.get(row.evidence_mode, 2),
        row.strategy_id,
        row.strategy_version,
        row.evidence_mode,
    )


def _render_summary(rows: list[_CalendarRow]) -> str:
    retained_dates = len({row.market_date for row in rows})
    forward = [row for row in rows if row.evidence_mode == "forward_observation"]
    forward_complete = sum(row.status == "complete" for row in forward)
    missing_truth = sum(row.status in _NULL_STATUSES for row in forward)
    replay_rows = sum(row.evidence_mode == "historical_replay" for row in rows)
    cards = (
        ("Retained dates", str(retained_dates), "calendar dates represented"),
        ("Forward complete", str(forward_complete), "strategy-days reconciled"),
        ("Needs truth", str(missing_truth), "forward strategy-days remain null"),
        ("Replay", str(replay_rows), "historical strategy-days separated"),
    )
    rendered_cards = "\n".join(
        f"""      <article class="summary-card">
        <p>{_html(label)}</p><strong>{_html(value)}</strong><span>{_html(description)}</span>
      </article>"""
        for label, value, description in cards
    )
    return (
        '<section class="summary-grid" aria-label="Calendar summary">\n'
        f"{rendered_cards}\n"
        "    </section>"
    )


def _render_calendar(grouped: Mapping[date, list[_CalendarRow]]) -> str:
    if not grouped:
        return """<div class="empty-state" role="status">
        <span class="metric-null">\N{EM DASH}</span>
        <h2>No retained strategy calendar</h2>
        <p>No return is shown because no evaluated strategy-day was supplied.</p>
      </div>"""
    by_month: dict[tuple[int, int], dict[date, list[_CalendarRow]]] = defaultdict(dict)
    for market_date, rows in grouped.items():
        by_month[(market_date.year, market_date.month)][market_date] = rows
    return "\n".join(
        _render_month(year, month, day_rows)
        for (year, month), day_rows in sorted(by_month.items())
    )


def _render_month(
    year: int,
    month: int,
    retained: Mapping[date, list[_CalendarRow]],
) -> str:
    month_label = f"{_MONTH_NAMES[month]} {year}"
    cells: list[str] = []
    weeks = calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(
        year, month
    )
    for week in weeks:
        for day in week:
            if day.month != month:
                cells.append('<div class="outside-month" aria-hidden="true"></div>')
            elif day in retained:
                cells.append(_render_day(day, retained[day]))
            else:
                cells.append(
                    f"""<div class="unscheduled-day" aria-label="{day.isoformat()}: no retained schedule">
              <span>{day.day}</span><small>{_EM_DASH}</small>
            </div>"""
                )
    weekdays = "".join(
        f'<span aria-hidden="true">{name}</span>'
        for name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    )
    return f"""<section class="month" aria-labelledby="month-{year}-{month:02d}">
        <div class="month-heading">
          <h2 id="month-{year}-{month:02d}">{month_label}</h2>
          <p>Select a retained day for strategy detail</p>
        </div>
        <div class="weekday-row">{weekdays}</div>
        <div class="month-grid">{''.join(cells)}</div>
      </section>"""


def _render_day(market_date: date, rows: list[_CalendarRow]) -> str:
    forward = [row for row in rows if row.evidence_mode == "forward_observation"]
    replay = [row for row in rows if row.evidence_mode == "historical_replay"]
    forward_resolved = sum(row.paper_book_return_pct is not None for row in forward)
    forward_total = len(forward)
    null_count = sum(row.status in _NULL_STATUSES for row in rows)
    summary_status = (
        f"{forward_resolved}/{forward_total} forward results"
        if forward_total
        else "Replay only"
    )
    if null_count:
        tile_metric = _EM_DASH
        tile_label = "Needs truth"
        tile_class = "null"
    elif forward_total:
        tile_metric = f"{forward_resolved}/{forward_total}"
        tile_label = "Forward complete"
        tile_class = "complete"
    else:
        tile_metric = "R"
        tile_label = "Replay"
        tile_class = "replay"
    badges = []
    if forward:
        badges.append('<span class="mode-pill forward">FWD</span>')
    if replay:
        badges.append('<span class="mode-pill replay">REPLAY</span>')
    if not forward and not replay:
        badges.append('<span class="mode-pill other">OTHER</span>')
    unknown_modes = sorted(
        {
            row.evidence_mode
            for row in rows
            if row.evidence_mode not in _MODE_LABELS
        }
    )
    if unknown_modes:
        badges.append('<span class="mode-pill other">OTHER</span>')
    aria_label = (
        f"{market_date.isoformat()}, {summary_status}. "
        "Open strategy return details"
    )
    full_date = (
        f"{market_date.strftime('%A')}, {_MONTH_NAMES[market_date.month]} "
        f"{market_date.day}, {market_date.year}"
    )
    mode_groups = []
    for mode in sorted(
        {row.evidence_mode for row in rows},
        key=lambda value: (_MODE_ORDER.get(value, 2), value),
    ):
        mode_rows = [row for row in rows if row.evidence_mode == mode]
        mode_groups.append(_render_mode_group(mode, mode_rows))
    return f"""<details class="day-card {tile_class}">
              <summary aria-label="{_html(aria_label)}">
                <span class="day-number">{market_date.day}</span>
                <span class="day-badges">{''.join(badges)}</span>
                <strong>{_html(tile_metric)}</strong>
                <small>{_html(tile_label)}</small>
              </summary>
              <div class="day-panel" role="region" aria-label="{_html(full_date)} strategy details">
                <header>
                  <div><p class="eyebrow">STRATEGY DAY</p><h3>{_html(full_date)}</h3></div>
                  <p class="close-hint">Select the day tile again to close</p>
                </header>
                {''.join(mode_groups)}
              </div>
            </details>"""


def _render_mode_group(mode: str, rows: list[_CalendarRow]) -> str:
    mode_label = _MODE_LABELS.get(mode, f"Other evidence: {mode}")
    mode_class = (
        "forward" if mode == "forward_observation" else
        "replay" if mode == "historical_replay" else "other"
    )
    return f"""<section class="mode-section" aria-label="{_html(mode_label)}">
                  <div class="mode-heading"><span class="mode-pill {mode_class}">{_html(mode_label.upper())}</span><span>{len(rows)} strateg{'y' if len(rows) == 1 else 'ies'}</span></div>
                  <div class="strategy-list">{''.join(_render_strategy_row(row) for row in rows)}</div>
                </section>"""


def _render_strategy_row(row: _CalendarRow) -> str:
    status_label, status_class = _status(row.status)
    return_text = _format_percent(row.paper_book_return_pct)
    return_class = _return_class(row.paper_book_return_pct)
    strategy_name = _strategy_name(row.strategy_id)
    symbols = ", ".join(row.symbols) if row.symbols else "No symbols retained"
    learning = (
        "Learning eligible"
        if row.learning_eligible and row.evidence_mode == "forward_observation"
        else "Not learning eligible"
    )
    return f"""<article class="strategy-row">
                      <div class="strategy-title">
                        <div><h4>{_html(strategy_name)}</h4><code>{_html(row.strategy_id)} · {_html(row.strategy_version)}</code></div>
                        <span class="status {status_class}">{_html(status_label)}</span>
                      </div>
                      <div class="return-line">
                        <strong class="{return_class}" aria-label="Paper book return: {_html(_spoken_return(row.paper_book_return_pct))}">{_html(return_text)}</strong>
                        <span>paper book return</span>
                      </div>
                      <dl class="counts">
                        <div><dt>Decisions</dt><dd>{row.decision_count}</dd></div>
                        <div><dt>Signals</dt><dd>{row.signal_count}</dd></div>
                        <div><dt>Closed</dt><dd>{row.closed_trade_count}</dd></div>
                        <div><dt>Pending</dt><dd>{row.pending_trade_count}</dd></div>
                        <div><dt>No entry</dt><dd>{row.not_entered_count}</dd></div>
                      </dl>
                      <div class="strategy-foot">
                        <p><span>Symbols</span>{_html(symbols)}</p>
                        <p><span>P&amp;L / capital</span>{_html(_money_pair(row.pnl, row.capital_deployed))}</p>
                        <p><span>Truth status</span>{_html(learning)}</p>
                      </div>
                      <p class="semantics">{_html(row.return_semantics)}</p>
                    </article>"""


def _status(status: str) -> tuple[str, str]:
    labels = {
        "complete": ("Complete", "complete"),
        "no_setup": ("No setup", "cash"),
        "resolved_no_entry": ("Resolved / no entry", "cash"),
        "not_evaluated": ("Not evaluated", "null"),
        "skipped": ("Not evaluated", "null"),
        "incomplete": ("Incomplete", "null"),
    }
    if status in labels:
        return labels[status]
    if status in _NULL_STATUSES or status.startswith("pending"):
        return "Incomplete", "null"
    return status.replace("_", " ").strip().title() or "Unknown", "other"


def _strategy_name(strategy_id: str) -> str:
    if strategy_id in _KNOWN_STRATEGY_NAMES:
        return _KNOWN_STRATEGY_NAMES[strategy_id]
    words = [word for word in strategy_id.replace("-", "_").split("_") if word]
    if words and len(words[-1]) > 1 and words[-1][0] == "v" and words[-1][1:].isdigit():
        words.pop()
    return " ".join(word.upper() if word in {"rvol", "vwap"} else word.title() for word in words) or "Unknown strategy"


def _format_percent(value: float | None) -> str:
    if value is None:
        return _EM_DASH
    return f"{value:+.2f}%" if value != 0 else "0.00%"


def _spoken_return(value: float | None) -> str:
    if value is None:
        return "not evaluated"
    return f"{value:.2f} percent"


def _return_class(value: float | None) -> str:
    if value is None:
        return "metric-null"
    if value > 0:
        return "metric-positive"
    if value < 0:
        return "metric-negative"
    return "metric-flat"


def _money_pair(pnl: float | None, capital: float | None) -> str:
    if pnl is None or capital is None:
        return _EM_DASH
    return f"${pnl:,.2f} / ${capital:,.2f}"


def _date_range_label(rows: list[_CalendarRow]) -> str:
    if not rows:
        return "No retained calendar dates"
    dates = sorted({row.market_date for row in rows})
    if len(dates) == 1:
        return dates[0].isoformat()
    return f"{dates[0].isoformat()} — {dates[-1].isoformat()}"


def _html(value: str) -> str:
    return escape(value, quote=True)


_STYLES = r"""    :root {
      color-scheme: dark;
      --bg: #07090d;
      --panel: #0e1219;
      --panel-2: #141a23;
      --line: #242d3a;
      --muted: #8692a3;
      --text: #f4f7fb;
      --green: #48e09b;
      --red: #ff6b7d;
      --amber: #f3bd5b;
      --blue: #66a8ff;
      --violet: #a693ff;
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    html { background: var(--bg); scroll-behavior: smooth; }
    body { margin: 0; min-width: 320px; background: radial-gradient(circle at 80% -10%, #15243a 0, transparent 35rem), var(--bg); color: var(--text); }
    .skip-link { position: fixed; left: 1rem; top: -4rem; z-index: 30; padding: .7rem 1rem; background: var(--text); color: var(--bg); border-radius: .5rem; }
    .skip-link:focus { top: 1rem; }
    .shell { width: min(1500px, calc(100% - 2rem)); margin: 0 auto; padding: 3rem 0 5rem; }
    .masthead { display: flex; align-items: flex-end; justify-content: space-between; gap: 2rem; margin-bottom: 2rem; }
    .eyebrow { margin: 0 0 .65rem; color: var(--blue); font: 700 .7rem/1.2 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .18em; }
    h1 { margin: 0; font-size: clamp(2rem, 5vw, 4rem); letter-spacing: -.055em; line-height: .95; }
    .range { margin: .9rem 0 0; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    .integrity { display: flex; align-items: center; gap: .65rem; padding: .7rem 1rem; border: 1px solid #28443b; background: #0e211b; border-radius: 999px; color: #a8e8ce; font-size: .82rem; white-space: nowrap; }
    .integrity-dot { width: .55rem; height: .55rem; border-radius: 50%; background: var(--green); box-shadow: 0 0 1rem var(--green); }
    .summary-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: .75rem; margin-bottom: .75rem; }
    .summary-card { min-width: 0; padding: 1rem 1.1rem; background: linear-gradient(145deg, #121822, #0d1118); border: 1px solid var(--line); border-radius: .9rem; }
    .summary-card p, .summary-card span { margin: 0; color: var(--muted); font-size: .72rem; }
    .summary-card strong { display: block; margin: .4rem 0 .2rem; font: 650 1.7rem/1 ui-monospace, SFMono-Regular, Consolas, monospace; }
    .truth-bar { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; overflow: hidden; margin-bottom: 2rem; border: 1px solid var(--line); background: var(--line); border-radius: .9rem; }
    .truth-bar > div { display: flex; align-items: center; gap: .8rem; min-height: 4.2rem; padding: .85rem 1rem; background: #0c1016; }
    .truth-bar p { margin: 0; color: var(--muted); font-size: .75rem; line-height: 1.4; }
    .mode-pill { display: inline-flex; align-items: center; width: max-content; padding: .25rem .42rem; border-radius: .32rem; font: 750 .61rem/1 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .06em; white-space: nowrap; }
    .mode-pill.forward { color: #9ecaff; background: #102743; border: 1px solid #224a78; }
    .mode-pill.replay { color: #c8beff; background: #211d3f; border: 1px solid #413979; }
    .mode-pill.other { color: #d3d9e2; background: #222832; border: 1px solid #3b4654; }
    .calendar { display: grid; gap: 2rem; }
    .month { padding: 1.2rem; border: 1px solid var(--line); border-radius: 1rem; background: rgba(12, 16, 22, .92); box-shadow: 0 1.2rem 4rem rgba(0,0,0,.22); }
    .month-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; margin: .15rem .2rem 1rem; }
    .month-heading h2 { margin: 0; font-size: 1.25rem; letter-spacing: -.025em; }
    .month-heading p { margin: 0; color: var(--muted); font-size: .73rem; }
    .weekday-row, .month-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: .5rem; }
    .weekday-row { margin-bottom: .45rem; }
    .weekday-row span { padding: 0 .35rem; color: #687487; font: 650 .63rem/1 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: .08em; text-transform: uppercase; }
    .outside-month, .unscheduled-day, .day-card > summary { min-height: 7.1rem; border-radius: .7rem; }
    .outside-month { background: #090c11; }
    .unscheduled-day { display: flex; flex-direction: column; justify-content: space-between; padding: .75rem; border: 1px solid #161d27; color: #465162; background: #0a0d12; }
    .unscheduled-day small { font-size: 1.1rem; color: #353e4a; }
    details.day-card { min-width: 0; }
    .day-card > summary { display: flex; position: relative; flex-direction: column; padding: .72rem; border: 1px solid #2b3543; background: linear-gradient(145deg, #151c26, #0f141c); cursor: pointer; list-style: none; transition: border-color .15s ease, transform .15s ease, background .15s ease; }
    .day-card > summary::-webkit-details-marker { display: none; }
    .day-card > summary:hover { border-color: #45576e; background: linear-gradient(145deg, #182230, #111821); transform: translateY(-1px); }
    .day-card > summary:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
    .day-card[open] > summary { border-color: var(--blue); box-shadow: inset 0 0 0 1px var(--blue); }
    .day-number { color: #dbe3ef; font: 700 .82rem/1 ui-monospace, SFMono-Regular, Consolas, monospace; }
    .day-badges { display: flex; flex-wrap: wrap; gap: .25rem; margin-top: .5rem; }
    .day-card summary strong { margin-top: auto; font: 680 1.25rem/1 ui-monospace, SFMono-Regular, Consolas, monospace; }
    .day-card summary small { margin-top: .35rem; color: var(--muted); font-size: .65rem; }
    .day-card.null summary strong { color: var(--muted); }
    .day-card.complete summary strong { color: var(--green); }
    .day-card.replay summary strong { color: var(--violet); }
    .day-panel { position: fixed; right: 1.25rem; bottom: 1.25rem; z-index: 20; width: min(760px, calc(100vw - 2.5rem)); max-height: calc(100vh - 2.5rem); overflow: auto; padding: 1.25rem; border: 1px solid #3c4c61; border-radius: 1rem; background: rgba(10, 14, 20, .98); box-shadow: 0 2rem 8rem rgba(0,0,0,.7), 0 0 0 9999px rgba(0,0,0,.43); }
    .day-panel > header { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; position: sticky; top: -1.25rem; z-index: 2; margin: -1.25rem -1.25rem 1rem; padding: 1.25rem; border-bottom: 1px solid var(--line); background: rgba(10,14,20,.97); backdrop-filter: blur(16px); }
    .day-panel h3 { margin: 0; font-size: 1.25rem; }
    .close-hint { margin: 0; color: var(--muted); font-size: .68rem; text-align: right; }
    .mode-section + .mode-section { margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--line); }
    .mode-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: .55rem; color: var(--muted); font-size: .7rem; }
    .strategy-list { display: grid; gap: .6rem; }
    .strategy-row { padding: 1rem; border: 1px solid var(--line); border-radius: .8rem; background: var(--panel-2); }
    .strategy-title { display: flex; align-items: flex-start; justify-content: space-between; gap: .8rem; }
    .strategy-title h4 { margin: 0 0 .25rem; font-size: .92rem; }
    code { color: #8795a9; font: .62rem/1.4 ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
    .status { padding: .28rem .45rem; border-radius: .35rem; font: 700 .6rem/1 ui-monospace, SFMono-Regular, Consolas, monospace; white-space: nowrap; }
    .status.complete { color: #9af1ca; background: #123426; }
    .status.cash { color: #b7c4d5; background: #29313d; }
    .status.null { color: #f5cf8b; background: #3a2b15; }
    .status.other { color: #d3d9e2; background: #29313d; }
    .return-line { display: flex; align-items: baseline; gap: .7rem; margin: 1.1rem 0 .85rem; }
    .return-line strong { font: 700 2rem/1 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing: -.05em; }
    .return-line span { color: var(--muted); font-size: .66rem; text-transform: uppercase; letter-spacing: .08em; }
    .metric-positive { color: var(--green); }
    .metric-negative { color: var(--red); }
    .metric-flat { color: #ced6e2; }
    .metric-null { color: var(--muted); font: 700 1rem/1 ui-monospace, SFMono-Regular, Consolas, monospace; }
    .counts { display: grid; grid-template-columns: repeat(5, 1fr); gap: 1px; overflow: hidden; margin: 0; border: 1px solid var(--line); border-radius: .55rem; background: var(--line); }
    .counts div { padding: .55rem; background: #0e131b; }
    .counts dt { color: var(--muted); font-size: .59rem; }
    .counts dd { margin: .3rem 0 0; font: 650 .85rem/1 ui-monospace, SFMono-Regular, Consolas, monospace; }
    .strategy-foot { display: grid; grid-template-columns: repeat(3, 1fr); gap: .7rem; margin-top: .8rem; }
    .strategy-foot p { min-width: 0; margin: 0; color: #d2dae6; font-size: .68rem; overflow-wrap: anywhere; }
    .strategy-foot span { display: block; margin-bottom: .2rem; color: var(--muted); font-size: .58rem; text-transform: uppercase; letter-spacing: .06em; }
    .semantics { margin: .8rem 0 0; padding-top: .65rem; border-top: 1px solid var(--line); color: var(--muted); font-size: .65rem; line-height: 1.5; }
    .empty-state { padding: 5rem 1.5rem; border: 1px dashed #364153; border-radius: 1rem; text-align: center; background: #0b0f15; }
    .empty-state .metric-null { font-size: 2.5rem; }
    .empty-state h2 { margin: 1rem 0 .4rem; }
    .empty-state p { margin: 0; color: var(--muted); }
    footer { display: flex; justify-content: space-between; gap: 1rem; margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--line); color: var(--muted); font-size: .67rem; }
    footer p { margin: 0; }
    @media (max-width: 900px) {
      .summary-grid { grid-template-columns: repeat(2, 1fr); }
      .truth-bar { grid-template-columns: 1fr; }
      .weekday-row, .month-grid { gap: .3rem; }
      .outside-month, .unscheduled-day, .day-card > summary { min-height: 6rem; }
      .day-badges .mode-pill.replay { display: none; }
      .strategy-foot { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      .shell { width: min(100% - 1rem, 1500px); padding-top: 1.5rem; }
      .masthead { align-items: flex-start; flex-direction: column; }
      .integrity { white-space: normal; }
      .month { padding: .65rem; }
      .month-heading p, .weekday-row { display: none; }
      .month-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); }
      .outside-month, .unscheduled-day { display: none; }
      .day-card > summary { min-height: 5.5rem; }
      .day-panel { right: .5rem; bottom: .5rem; width: calc(100vw - 1rem); max-height: calc(100vh - 1rem); }
      .counts { grid-template-columns: repeat(3, 1fr); }
      footer { flex-direction: column; }
    }
    @media (prefers-reduced-motion: reduce) {
      html { scroll-behavior: auto; }
      .day-card > summary { transition: none; }
    }
    @media print {
      body { background: #fff; color: #000; }
      .shell { width: 100%; }
      .day-panel { display: none; }
      .month, .summary-card { border-color: #aaa; background: #fff; box-shadow: none; }
    }"""
