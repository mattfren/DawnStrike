"""ID10 replay harness: id10-dawnstrike-v1 on SPY, warmup + eval window.

Calls ONLY the pure decision functions in
intraday_scanner/strategies/id10_atr_band.py for the signal and position
logic -- this script owns session bookkeeping, daily-bar aggregation, fills,
costs and I/O, nothing about the LONG/SHORT/FLAT rule or the
flip/stop/EOD rule is reimplemented here.

Inputs: the cached bar CSV REUSED from the ID08 packet
(C:\\r\\dsos-00-v3\\id08_run\\spy_1min_bars_cache.csv -- no new fetch) and the
repo's existing intraday_scanner/market_calendar.py (no calendar data is
invented).

Outputs (under C:\\r\\dsos-00-v3\\id10_run\\): decisions.csv, trades.csv,
RESULTS.md.
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.market_calendar import market_session
from intraday_scanner.strategies.id10_atr_band import (
    Action,
    BAND_MULTIPLIER,
    DailyBar,
    LOOKBACK_SESSIONS,
    PositionSide,
    STRATEGY_ID,
    STRATEGY_SOURCE,
    Signal,
    decide_signal,
    decision_marks_for_session,
    next_minute_mark,
    resolve_action,
)

OUTPUT_DIR = Path(r"C:\r\dsos-00-v3\id10_run")
# Reused verbatim from the ID08 packet -- no new fetch, per the ID10 packet
# instructions.
CACHE_PATH = Path(r"C:\r\dsos-00-v3\id08_run\spy_1min_bars_cache.csv")
ET = ZoneInfo("America/New_York")

# Identical to ID08 for comparability (frozen spec, "Window").
WARMUP_START = date(2026, 1, 2)
EVAL_END = date(2026, 9, 18)

FIXED_NOTIONAL_USD = 100_000.0
COST_LEVELS = ("gross_0", "source_cited", "stressed_5bps")
SOURCE_COMMISSION_PER_SHARE = 0.0035
SOURCE_SLIPPAGE_PER_SHARE = 0.001
STRESSED_ROUND_TRIP_BPS = 5.0


@dataclass
class SessionBars:
    session_date: str
    close_time_et: str  # "HH:MM"
    open_0930: float | None
    minute_closes: dict[str, float]  # "HH:MM" -> close price of that minute bar
    minute_opens: dict[str, float]  # "HH:MM" -> open price of that minute bar
    day_high: float | None = None
    day_low: float | None = None
    day_close: float | None = None  # last RTH bar close of the session


def load_calendar_sessions(start: date, end: date) -> list[tuple[str, str]]:
    """Return [(session_date, close_time_et)] for every trading day in range.

    Uses ONLY intraday_scanner.market_calendar -- no calendar data invented.
    """

    sessions: list[tuple[str, str]] = []
    current = start
    while current <= end:
        decision = market_session(current)
        if decision.is_trading_day:
            sessions.append((current.isoformat(), decision.close_time_et))
        current += timedelta(days=1)
    return sessions


def load_bars_by_session(calendar_close_times: dict[str, str]) -> dict[str, SessionBars]:
    """Bucket cached UTC 1-min bars into ET sessions, RTH only (09:30..close).

    Also accumulates each session's daily OHLC (day_high/day_low/day_close)
    from the same RTH bars, used to derive the daily bars needed for
    Wilder's ATR(14). Session open (day open) is ``open_0930``, already
    tracked for the band anchor.
    """

    sessions: dict[str, SessionBars] = {}
    for session_date, close_time_et in calendar_close_times.items():
        sessions[session_date] = SessionBars(
            session_date=session_date,
            close_time_et=close_time_et,
            open_0930=None,
            minute_closes={},
            minute_opens={},
        )

    seen_keys: set[tuple[str, str]] = set()
    duplicate_count = 0
    with CACHE_PATH.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ts_utc = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
            ts_et = ts_utc.astimezone(ET)
            session_date = ts_et.date().isoformat()
            bucket = sessions.get(session_date)
            if bucket is None:
                continue  # outside the calendar-derived session set (e.g. extended hours edge)
            hhmm = f"{ts_et.hour:02d}:{ts_et.minute:02d}"
            if hhmm < "09:30" or hhmm >= bucket.close_time_et:
                continue  # RTH only, matched to this session's actual close
            key = (session_date, hhmm)
            if key in seen_keys:
                duplicate_count += 1
                continue  # first-seen bar wins; duplicates never silently average in
            seen_keys.add(key)
            close = float(row["close"])
            open_ = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            bucket.minute_closes[hhmm] = close
            bucket.minute_opens[hhmm] = open_
            if hhmm == "09:30":
                bucket.open_0930 = open_
            bucket.day_high = high if bucket.day_high is None else max(bucket.day_high, high)
            bucket.day_low = low if bucket.day_low is None else min(bucket.day_low, low)
    if duplicate_count:
        print(f"WARNING: dropped {duplicate_count} duplicate (session,minute) bar rows")

    for bucket in sessions.values():
        if bucket.minute_closes:
            last_mark = max(bucket.minute_closes.keys())
            bucket.day_close = bucket.minute_closes[last_mark]
    return sessions


def build_daily_bars(session_order: list[str], bars_by_session: dict[str, SessionBars]) -> dict[str, DailyBar]:
    """Aggregate each session's RTH 1-min bars into one DailyBar (O/H/L/C).

    Per-session aggregation only -- open is the 09:30 open, high/low are the
    RTH extremes, close is the last RTH bar's close (matched to that
    session's actual close, so a half-day session's daily bar naturally uses
    its own early close).
    """

    daily: dict[str, DailyBar] = {}
    for session_date in session_order:
        b = bars_by_session[session_date]
        if b.open_0930 is None or b.day_high is None or b.day_low is None or b.day_close is None:
            continue  # data gap; caller must treat this session as missing
        daily[session_date] = DailyBar(
            session_date=session_date,
            open=b.open_0930,
            high=b.day_high,
            low=b.day_low,
            close=b.day_close,
        )
    return daily


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    calendar_sessions = load_calendar_sessions(WARMUP_START, EVAL_END)
    calendar_close_times = dict(calendar_sessions)
    session_order = [s for s, _ in calendar_sessions]
    print(f"Calendar-expected trading sessions {WARMUP_START}..{EVAL_END}: {len(session_order)}")

    if len(session_order) <= LOOKBACK_SESSIONS:
        raise SystemExit("not enough calendar sessions for warmup; stopping")

    warmup_sessions = session_order[:LOOKBACK_SESSIONS]
    eval_sessions = session_order[LOOKBACK_SESSIONS:]
    print(f"Warmup sessions ({len(warmup_sessions)}): {warmup_sessions[0]} .. {warmup_sessions[-1]}")
    print(f"Eval sessions ({len(eval_sessions)}): {eval_sessions[0]} .. {eval_sessions[-1]}")

    bars_by_session = load_bars_by_session(calendar_close_times)

    coverage_rows = []
    for session_date in session_order:
        bucket = bars_by_session[session_date]
        expected_marks = decision_marks_for_session(bucket.close_time_et)
        present_marks = [m for m in expected_marks if m in bucket.minute_closes]
        coverage_rows.append(
            (session_date, bucket.close_time_et, len(expected_marks), len(present_marks))
        )
    incomplete = [r for r in coverage_rows if r[2] != r[3]]

    daily_bars = build_daily_bars(session_order, bars_by_session)

    decisions_out, trades_out, mark_stats = run_replay(
        session_order, eval_sessions, bars_by_session, daily_bars
    )

    _write_decisions_csv(decisions_out)
    _write_trades_csv(trades_out)
    _write_results_md(
        session_order, warmup_sessions, eval_sessions, coverage_rows, incomplete,
        decisions_out, trades_out, mark_stats,
    )
    print(f"decisions: {len(decisions_out)}  trades: {len(trades_out)}")
    return 0


def run_replay(
    session_order: list[str],
    eval_sessions: list[str],
    bars_by_session: dict[str, SessionBars],
    daily_bars: dict[str, DailyBar],
) -> tuple[list[dict], list[dict], dict]:
    """Produce (decisions_out, trades_out, mark_stats) for ``eval_sessions``.

    Pulled out of ``main()`` so it can be exercised directly by the
    lookahead-harness test: that test calls this same function twice on the
    SAME real cached bars, once untouched and once with a session's bar
    series truncated at a chosen fill bar, and asserts every decision
    produced up to that point is byte-for-byte unchanged.

    ``mark_stats`` accumulates, per decision mark HH:MM, the band width
    (upper-lower) seen and how many entries (ENTER_LONG/ENTER_SHORT/
    EXIT_FLIP_TO_*) fired at that mark -- the frozen spec's required
    "band width and entries by mark" diagnostic.
    """

    decisions_out: list[dict] = []
    trades_out: list[dict] = []
    mark_stats: dict[str, dict] = defaultdict(lambda: {"band_widths": [], "entries": 0})

    open_position: PositionSide = PositionSide.FLAT
    open_trade = None  # dict describing the currently open position

    def entry_costs(shares: float, price: float) -> dict[str, float]:
        notional = shares * price
        return {
            "gross_0": 0.0,
            "source_cited": shares * (SOURCE_COMMISSION_PER_SHARE + SOURCE_SLIPPAGE_PER_SHARE),
            "stressed_5bps": notional * (STRESSED_ROUND_TRIP_BPS / 10_000.0) / 2.0,
        }

    for idx, session_date in enumerate(eval_sessions):
        global_idx = LOOKBACK_SESSIONS + idx
        prior_dates = session_order[global_idx - LOOKBACK_SESSIONS: global_idx]
        pre_lookback_idx = global_idx - LOOKBACK_SESSIONS - 1
        pre_lookback_date = session_order[pre_lookback_idx] if pre_lookback_idx >= 0 else None

        current = bars_by_session[session_date]

        missing_daily = [d for d in prior_dates if d not in daily_bars]
        pre_lookback_close = (
            daily_bars[pre_lookback_date].close
            if pre_lookback_date is not None and pre_lookback_date in daily_bars
            else None
        )
        if pre_lookback_date is not None and pre_lookback_date not in daily_bars:
            missing_daily = missing_daily + [f"pre_lookback:{pre_lookback_date}"]

        if current.open_0930 is None or missing_daily:
            decisions_out.append(
                _no_data_decision_row(
                    session_date,
                    f"missing_09:30_open_or_daily_bar_gap missing={missing_daily}",
                )
            )
            continue

        prior_daily_bars = [daily_bars[d] for d in prior_dates]
        session_open = current.open_0930

        marks = decision_marks_for_session(current.close_time_et)
        for mark_idx, mark in enumerate(marks):
            is_last_mark = mark_idx == len(marks) - 1

            decision_close = current.minute_closes.get(mark)
            if decision_close is None:
                decisions_out.append(
                    {
                        "session": session_date, "mark": mark, "upper": "", "lower": "",
                        "atr14": "", "close": "",
                        "signal": "NO_DATA", "action": "NONE",
                        "reason": "no_trade_data_gap decision_bar_missing",
                    }
                )
                continue

            decision = decide_signal(
                session_date=session_date,
                mark_time=mark,
                prior_daily_bars=prior_daily_bars,
                pre_lookback_close=pre_lookback_close,
                session_open=session_open,
                decision_bar_close=decision_close,
            )
            action = resolve_action(
                position=open_position, decision=decision, session_open=session_open,
                is_last_mark_of_session=is_last_mark,
            )
            decisions_out.append(
                {
                    "session": session_date, "mark": mark,
                    "upper": f"{decision.upper:.6f}", "lower": f"{decision.lower:.6f}",
                    "atr14": f"{decision.atr14:.8f}", "close": f"{decision.decision_bar_close:.4f}",
                    "signal": decision.signal.value, "action": action.value, "reason": decision.reason,
                }
            )

            mark_stats[mark]["band_widths"].append(decision.upper - decision.lower)
            if action in (
                Action.ENTER_LONG, Action.ENTER_SHORT,
                Action.EXIT_FLIP_TO_LONG, Action.EXIT_FLIP_TO_SHORT,
            ):
                mark_stats[mark]["entries"] += 1

            fill_mark = next_minute_mark(mark)
            fill_price_next_open = current.minute_opens.get(fill_mark)
            same_bar_close_price = decision.decision_bar_close  # diagnostic only

            if action in (
                Action.EXIT_FLIP_TO_SHORT, Action.EXIT_FLIP_TO_LONG,
                Action.EXIT_STOP, Action.EXIT_EOD,
            ):
                if fill_price_next_open is not None and open_trade is not None:
                    exit_reason = {
                        Action.EXIT_EOD: "EOD",
                        Action.EXIT_STOP: "STOP",
                        Action.EXIT_FLIP_TO_SHORT: "FLIP",
                        Action.EXIT_FLIP_TO_LONG: "FLIP",
                    }[action]
                    _close_trade(
                        open_trade, trades_out, session_date, mark, fill_mark,
                        fill_price_next_open, same_bar_close_price, exit_reason,
                    )
                    open_position = PositionSide.FLAT
                    open_trade = None
                else:
                    open_position = PositionSide.FLAT
                    open_trade = None

            if action in (Action.ENTER_LONG, Action.EXIT_FLIP_TO_LONG):
                if fill_price_next_open is not None:
                    shares = FIXED_NOTIONAL_USD / fill_price_next_open
                    costs = entry_costs(shares, fill_price_next_open)
                    open_trade = {
                        "entry_session": session_date, "entry_decision_mark": mark,
                        "entry_fill_mark": fill_mark, "entry_price": fill_price_next_open,
                        "entry_same_bar_close_price": same_bar_close_price,
                        "side": "LONG", "shares": shares, "entry_costs": costs,
                        "mfe_price": fill_price_next_open,
                    }
                    open_position = PositionSide.LONG
            elif action in (Action.ENTER_SHORT, Action.EXIT_FLIP_TO_SHORT):
                if fill_price_next_open is not None:
                    shares = FIXED_NOTIONAL_USD / fill_price_next_open
                    costs = entry_costs(shares, fill_price_next_open)
                    open_trade = {
                        "entry_session": session_date, "entry_decision_mark": mark,
                        "entry_fill_mark": fill_mark, "entry_price": fill_price_next_open,
                        "entry_same_bar_close_price": same_bar_close_price,
                        "side": "SHORT", "shares": shares, "entry_costs": costs,
                        "mfe_price": fill_price_next_open,
                    }
                    open_position = PositionSide.SHORT

            if open_trade is not None:
                px = current.minute_closes.get(mark)
                if px is not None:
                    if open_trade["side"] == "LONG":
                        open_trade["mfe_price"] = max(open_trade["mfe_price"], px)
                    else:
                        open_trade["mfe_price"] = min(open_trade["mfe_price"], px)

        # Same rationale as ID08: a LONG/SHORT signal at the session's final
        # decision mark still enters an ordinary position (resolve_action
        # never blocks entry on is_last_mark_of_session when starting FLAT),
        # so there is no next decision mark left for the normal EXIT_EOD
        # path to fire on. Liquidate any such position at the session's
        # actual close.
        if open_trade is not None:
            close_price = _session_close_price(current)
            if close_price is not None:
                _close_trade(
                    open_trade, trades_out, session_date, marks[-1] if marks else "EOD",
                    "session_close_final_mark_entry", close_price, close_price,
                    "EOD_FINAL_MARK_ENTRY_LIQUIDATION",
                )
            else:
                print(
                    f"WARNING: dropping unliquidated position from {session_date} "
                    f"(no session close price available); excluded from trades.csv"
                )
            open_position = PositionSide.FLAT
            open_trade = None

    return decisions_out, trades_out, dict(mark_stats)


def _session_close_price(bucket: SessionBars) -> float | None:
    if not bucket.minute_closes:
        return None
    last_mark = max(bucket.minute_closes.keys())
    return bucket.minute_closes[last_mark]


def _no_data_decision_row(session_date: str, reason: str) -> dict:
    return {
        "session": session_date, "mark": "", "upper": "", "lower": "", "atr14": "",
        "close": "", "signal": "NO_DATA", "action": "NONE", "reason": reason,
    }


def _close_trade(
    open_trade: dict, trades_out: list, exit_session: str, exit_decision_mark: str,
    exit_fill_mark: str, exit_price: float, exit_same_bar_close_price: float, exit_reason: str,
) -> None:
    shares = open_trade["shares"]
    entry_price = open_trade["entry_price"]
    side = open_trade["side"]
    notional = shares * entry_price
    exit_notional = shares * exit_price

    exit_costs = {
        "gross_0": 0.0,
        "source_cited": shares * (SOURCE_COMMISSION_PER_SHARE + SOURCE_SLIPPAGE_PER_SHARE),
        "stressed_5bps": exit_notional * (STRESSED_ROUND_TRIP_BPS / 10_000.0) / 2.0,
    }
    if side == "LONG":
        gross_pnl = (exit_price - entry_price) * shares
        same_bar_gross_pnl = (exit_same_bar_close_price - open_trade["entry_same_bar_close_price"]) * shares
        mfe = (open_trade["mfe_price"] - entry_price) * shares
    else:
        gross_pnl = (entry_price - exit_price) * shares
        same_bar_gross_pnl = (open_trade["entry_same_bar_close_price"] - exit_same_bar_close_price) * shares
        mfe = (entry_price - open_trade["mfe_price"]) * shares

    net_by_cost = {}
    for level in COST_LEVELS:
        total_cost = open_trade["entry_costs"][level] + exit_costs[level]
        net_by_cost[level] = gross_pnl - total_cost

    trades_out.append(
        {
            "entry_session": open_trade["entry_session"],
            "entry_decision_mark": open_trade["entry_decision_mark"],
            "entry_fill_mark": open_trade["entry_fill_mark"],
            "entry_price": f"{entry_price:.4f}",
            "exit_session": exit_session,
            "exit_decision_mark": exit_decision_mark,
            "exit_fill_mark": exit_fill_mark,
            "exit_price": f"{exit_price:.4f}",
            "exit_reason": exit_reason,
            "side": side,
            "shares": f"{shares:.6f}",
            "notional_usd": f"{notional:.2f}",
            "gross_pnl_usd": f"{gross_pnl:.4f}",
            "net_pnl_gross_0": f"{net_by_cost['gross_0']:.4f}",
            "net_pnl_source_cited": f"{net_by_cost['source_cited']:.4f}",
            "net_pnl_stressed_5bps": f"{net_by_cost['stressed_5bps']:.4f}",
            "realized_return_pct_gross_0": f"{(net_by_cost['gross_0'] / notional) * 100:.6f}",
            "realized_return_pct_source_cited": f"{(net_by_cost['source_cited'] / notional) * 100:.6f}",
            "realized_return_pct_stressed_5bps": f"{(net_by_cost['stressed_5bps'] / notional) * 100:.6f}",
            "diagnostic_same_bar_close_gross_pnl_usd": f"{same_bar_gross_pnl:.4f}",
            "mfe_usd_diagnostic_only": f"{mfe:.4f}",
        }
    )


def _write_decisions_csv(rows: list[dict]) -> None:
    path = OUTPUT_DIR / "decisions.csv"
    fieldnames = ["session", "mark", "upper", "lower", "atr14", "close", "signal", "action", "reason"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_trades_csv(rows: list[dict]) -> None:
    path = OUTPUT_DIR / "trades.csv"
    if not rows:
        with path.open("w", newline="", encoding="utf-8") as handle:
            handle.write(
                "entry_session,entry_decision_mark,entry_fill_mark,entry_price,exit_session,"
                "exit_decision_mark,exit_fill_mark,exit_price,exit_reason,side,shares,notional_usd,"
                "gross_pnl_usd,net_pnl_gross_0,net_pnl_source_cited,net_pnl_stressed_5bps,"
                "realized_return_pct_gross_0,realized_return_pct_source_cited,"
                "realized_return_pct_stressed_5bps,diagnostic_same_bar_close_gross_pnl_usd,"
                "mfe_usd_diagnostic_only\n"
            )
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_results_md(
    session_order, warmup_sessions, eval_sessions, coverage_rows, incomplete, decisions_out,
    trades_out, mark_stats,
) -> None:
    path = OUTPUT_DIR / "RESULTS.md"

    n_long = sum(1 for d in decisions_out if d["signal"] == "LONG")
    n_short = sum(1 for d in decisions_out if d["signal"] == "SHORT")
    n_flat = sum(1 for d in decisions_out if d["signal"] == "FLAT")
    n_no_data = sum(1 for d in decisions_out if d["signal"] == "NO_DATA")

    def _sum(field: str) -> float:
        return sum(float(t[field]) for t in trades_out) if trades_out else 0.0

    gross_total = _sum("gross_pnl_usd")
    net_gross0 = _sum("net_pnl_gross_0")
    net_source = _sum("net_pnl_source_cited")
    net_stressed = _sum("net_pnl_stressed_5bps")
    same_bar_total = _sum("diagnostic_same_bar_close_gross_pnl_usd")
    mfe_total = _sum("mfe_usd_diagnostic_only")

    exit_reason_counts: dict[str, int] = defaultdict(int)
    for t in trades_out:
        exit_reason_counts[t["exit_reason"]] += 1

    lines = []
    lines.append("# ID10 (id10-dawnstrike-v1) replay results")
    lines.append("")
    lines.append(STRATEGY_IDENTITY_NOTE)
    lines.append("")
    lines.append("## ATR-granularity notice (read first)")
    lines.append(
        "**The ATR(14) term used for this band is computed on DAILY bars "
        "(Wilder's method), per an explicit orchestrator decision on an "
        "ambiguity the source paper never resolves in words. The alternative "
        "reading -- intraday ATR(14) on 15-minute bars, which would produce a "
        "far narrower band -- is UNTESTED. This is the single largest fidelity "
        "risk in ID 10 and is surfaced here, not buried.**"
    )
    lines.append("")
    lines.append("## Window and coverage")
    lines.append(f"- Calendar-expected trading sessions {WARMUP_START}..{EVAL_END}: **{len(session_order)}**")
    lines.append(f"- Warmup (14 completed daily sessions): {warmup_sessions[0]} .. {warmup_sessions[-1]}")
    lines.append(f"- Evaluation window: {eval_sessions[0]} .. {eval_sessions[-1]} ({len(eval_sessions)} sessions)")
    total_expected_marks = sum(r[2] for r in coverage_rows)
    total_present_marks = sum(r[3] for r in coverage_rows)
    lines.append(
        f"- Expected decision-mark intervals (calendar-derived, not raw row count): "
        f"**{total_expected_marks}**; present in reused IEX cache: **{total_present_marks}**"
    )
    if incomplete:
        lines.append(f"- Sessions with incomplete mark coverage: **{len(incomplete)}**")
        for s, ct, exp, pres in incomplete[:20]:
            lines.append(f"  - {s} (close {ct}): {pres}/{exp} marks present")
    else:
        lines.append("- All calendar-expected sessions have full mark coverage in the reused IEX cache.")
    lines.append("")
    lines.append("## Half-day coverage limitation")
    lines.append(
        "No real half-day session falls inside 2026-01-23..2026-09-18 (the calendar's only "
        "2026 early closes, 2026-11-27 and 2026-12-24, are both after this window closes). "
        "The half-day/early-close code path (`decision_marks_for_session` given a non-16:00 "
        "close) is therefore untested against real window data, same coverage gap as ID08."
    )
    lines.append("")
    lines.append("## First-TR boundary condition (not invented data)")
    lines.append(
        "The oldest daily bar entering the very first eval session's (2026-01-23) 14-day ATR "
        "lookback is 2026-01-02 -- the first day in the entire reused cache. No session precedes "
        "it, so there is no prior close to form a gap-based True Range term for that one bar; "
        "`true_range()` falls back to `high-low` only for that single boundary case, per "
        "`intraday_scanner/strategies/id10_atr_band.py`. Every later eval session's ATR lookback "
        "has a genuine prior close available and uses the full three-term True Range."
    )
    lines.append("")
    lines.append("## Decision-mark counts")
    lines.append(f"- LONG: {n_long}")
    lines.append(f"- SHORT: {n_short}")
    lines.append(f"- FLAT: {n_flat}")
    lines.append(f"- NO_DATA (data-gap abstention, never fabricated): {n_no_data}")
    lines.append("")
    lines.append("## REQUIRED diagnostic: band width and entries by decision mark")
    lines.append(
        "Per the frozen spec, entry clustering at the first mark (09:30) is a finding to report, "
        "not a parameter to adjust."
    )
    lines.append("")
    lines.append("| mark | band width (mean $) | band width (min $) | band width (max $) | entries |")
    lines.append("|---|---|---|---|---|")
    for mark in sorted(mark_stats.keys()):
        widths = mark_stats[mark]["band_widths"]
        entries = mark_stats[mark]["entries"]
        if widths:
            mean_w = sum(widths) / len(widths)
            lines.append(
                f"| {mark} | {mean_w:.4f} | {min(widths):.4f} | {max(widths):.4f} | {entries} |"
            )
        else:
            lines.append(f"| {mark} | n/a | n/a | n/a | {entries} |")
    total_entries = sum(v["entries"] for v in mark_stats.values())
    first_mark_entries = mark_stats.get("09:30", {"entries": 0})["entries"]
    lines.append("")
    if total_entries:
        pct_first = 100.0 * first_mark_entries / total_entries
        lines.append(
            f"- Entries at 09:30 (first mark): {first_mark_entries} of {total_entries} total "
            f"entries ({pct_first:.1f}%)."
        )
    else:
        lines.append("- No entries occurred at any mark; nothing to cluster.")
    lines.append("")
    lines.append("## Trades")
    lines.append(f"- Total realized round-trip trades: **{len(trades_out)}**")
    lines.append(f"- Gross P&L (fixed ${FIXED_NOTIONAL_USD:,.0f} notional per entry): ${gross_total:,.2f}")
    lines.append(f"- Exit reasons: {dict(exit_reason_counts)}")
    lines.append("")
    lines.append("### Cost sensitivity (exactly the three predeclared levels)")
    lines.append(f"1. `0` gross: net P&L = ${net_gross0:,.2f}")
    lines.append(
        f"2. source-cited ($0.0035/share commission + $0.001/share slippage, per side): "
        f"net P&L = ${net_source:,.2f}"
    )
    lines.append(f"3. stressed (5 bps round-trip): net P&L = ${net_stressed:,.2f}")
    lines.append("")
    lines.append("### Same-bar-close fill diagnostic (NEVER the headline)")
    lines.append(
        f"- Gross P&L under a hypothetical (non-executable) same-bar-close fill: ${same_bar_total:,.2f}"
    )
    lines.append(f"- Delta vs. the actual next-bar-open headline fill: ${same_bar_total - gross_total:,.2f}")
    lines.append("")
    lines.append("### MFE diagnostic (separate, never a return)")
    lines.append(f"- Sum of per-trade favorable-excursion dollars: ${mfe_total:,.2f}")
    lines.append("")
    lines.append("## Comparisons (exactly the three predeclared)")
    lines.append(f"1. **id10-dawnstrike-v1**: net P&L at the three cost levels above.")
    bh = _buy_and_hold_return(eval_sessions)
    if bh is not None:
        lines.append(
            f"2. **buy-and-hold SPY**, same window, matched ${FIXED_NOTIONAL_USD:,.0f} notional: "
            f"${bh['pnl']:,.2f} ({bh['pct']:.4f}%), entry {bh['entry_price']:.4f} on "
            f"{bh['entry_session']}, exit {bh['exit_price']:.4f} on {bh['exit_session']}"
        )
    else:
        lines.append("2. **buy-and-hold SPY**: could not be computed (missing open/close data).")
    lines.append("3. **cash**: $0.00 (0.0000%) by definition.")
    lines.append("")
    lines.append("## Leakage assertion")
    lines.append(
        "`decide_signal` (intraday_scanner/strategies/id10_atr_band.py) takes only the 14 prior "
        "completed daily bars, the close immediately before them (gap term only), "
        "`SessionOpen[t]`, and the decision bar's own close -- there is no parameter through "
        "which a bar at or after the fill bar could reach it. "
        "`tests/test_id10_run_replay_lookahead.py` exercises the HARNESS itself (not just the "
        "function signature) on the real reused cached bars: see the implementer receipt for the "
        "PASS/FAIL result of that run."
    )
    lines.append("")
    lines.append("## What is unmeasured")
    lines.append(
        "- **ATR bar granularity: only DAILY ATR(14) is tested. Intraday (15-min) ATR(14) is "
        "UNTESTED and may produce materially different (narrower) bands and different trade "
        "counts.**"
    )
    lines.append("- No slippage-vs-quoted-spread model beyond the three predeclared cost levels.")
    lines.append("- No transaction-cost impact on the buy-and-hold comparison (assumed frictionless).")
    lines.append("- No half-day session in real window data (see coverage limitation above).")
    lines.append(
        "- No out-of-sample period beyond 2026-09-18; this replay cannot speak to any later date."
    )
    lines.append(
        "- Same-bar-close diagnostic uses the SAME session's own decision-bar close for both legs "
        "of a flip/stop/EOD when the position was still open; it is a fill-price sensitivity "
        "diagnostic only, never a return."
    )
    lines.append(
        "- Standing limitation (identical to ID08): the window is a single sustained uptrend "
        "(SPY 688.10 -> 761.62, +10.68%). Any result here supports only a bounded, "
        "window-specific statement, never a durable verdict on the policy."
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


STRATEGY_IDENTITY_NOTE = (
    "**Identity: `id10-dawnstrike-v1`** -- an ADAPTATION of Zarattini, Pagani, "
    "\"Improving Performance with Fast Alphas: A Tactical Overlay for Intraday Trend "
    "Trading\" (Concretum Group, QuanTips #2, Feb 2026), Section 3 baseline only. "
    "This result does NOT claim the source paper's returns."
)


def _buy_and_hold_return(eval_sessions: list[str]) -> dict | None:
    bars_by_session = load_bars_by_session(
        {s: market_session(date.fromisoformat(s)).close_time_et for s in eval_sessions}
    )
    first = bars_by_session[eval_sessions[0]]
    last = bars_by_session[eval_sessions[-1]]
    entry_price = first.open_0930
    exit_price = _session_close_price(last)
    if entry_price is None or exit_price is None:
        return None
    shares = FIXED_NOTIONAL_USD / entry_price
    pnl = (exit_price - entry_price) * shares
    return {
        "entry_price": entry_price, "exit_price": exit_price, "pnl": pnl,
        "pct": (pnl / (shares * entry_price)) * 100.0,
        "entry_session": eval_sessions[0], "exit_session": eval_sessions[-1],
    }


if __name__ == "__main__":
    raise SystemExit(main())
