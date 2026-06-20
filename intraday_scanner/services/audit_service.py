"""Paper-trade audit calculations."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.models import utc_now_iso, validate_required_columns

AUDIT_TRADE_COLUMNS = [
    "audit_status",
    "audit_reason",
    "entry_mode",
    "rank",
    "score",
    "ticker",
    "entry_time",
    "entry_price",
    "triggered",
    "lunch_exit_price",
    "close_exit_price",
    "high_after_entry",
    "low_after_entry",
    "slippage_bps",
    "return_1m_pct",
    "return_5m_pct",
    "return_15m_pct",
    "lunch_return_pct",
    "close_return_pct",
    "high_return_pct",
    "low_drawdown_pct",
    "max_favorable_excursion_pct",
    "max_adverse_excursion_pct",
]

EASTERN_TIME = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class AuditResult:
    trades: list[dict[str, Any]]
    summary: dict[str, Any]


def run_paper_audit(
    ranked_path: str | Path,
    minute_bars_path: str | Path,
    out_dir: str | Path,
    config: ScannerConfig,
    *,
    top_n: int,
    fixture_only: bool = False,
) -> dict[str, Path]:
    ranked = _read_csv(ranked_path, ["ticker", "breakout_trigger", "score"], "ranked candidates")
    bars = _read_csv(
        minute_bars_path,
        ["ticker", "timestamp", "open", "high", "low", "close", "volume"],
        "minute bars",
    )
    return run_paper_audit_rows(
        ranked,
        bars,
        out_dir,
        config,
        top_n=top_n,
        fixture_only=fixture_only,
    )


def run_paper_audit_rows(
    ranked_rows: list[dict[str, Any]],
    minute_bar_rows: list[dict[str, Any]],
    out_dir: str | Path,
    config: ScannerConfig,
    *,
    top_n: int,
    fixture_only: bool = False,
) -> dict[str, Path]:
    result = calculate_audit(
        ranked_rows, minute_bar_rows, config, top_n=top_n, fixture_only=fixture_only
    )
    return write_audit_outputs(result, out_dir)


def write_audit_outputs(result: AuditResult, out_dir: str | Path) -> dict[str, Path]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / "paper_audit_trades.csv"
    summary_path = output_dir / "paper_audit_summary.json"
    with trades_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_TRADE_COLUMNS)
        writer.writeheader()
        for trade in result.trades:
            writer.writerow(trade)
    summary_path.write_text(json.dumps(result.summary, indent=2), encoding="utf-8")
    return {"trades": trades_path, "summary": summary_path}


def calculate_audit(
    ranked_rows: list[dict[str, Any]],
    minute_bar_rows: list[dict[str, Any]],
    config: ScannerConfig,
    *,
    top_n: int,
    fixture_only: bool = False,
) -> AuditResult:
    selected = sorted(
        ranked_rows,
        key=lambda row: float(row.get("score") or 0),
        reverse=True,
    )[:top_n]
    bars_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for bar in minute_bar_rows:
        bars_by_ticker.setdefault(str(bar["ticker"]).upper(), []).append(bar)
    trades: list[dict[str, Any]] = []
    for candidate in selected:
        ticker = str(candidate["ticker"]).upper()
        bars = sorted(bars_by_ticker.get(ticker, []), key=lambda row: str(row["timestamp"]))
        if not bars:
            trades.append(
                _unavailable_trade(
                    candidate,
                    "No minute bars were available.",
                    entry_mode=config.entry_mode,
                )
            )
            continue
        trades.append(_audit_one(candidate, bars, config))
    summary = _summary(trades, top_n, fixture_only=fixture_only)
    return AuditResult(trades=trades, summary=summary)


def _audit_one(
    candidate: dict[str, Any], bars: list[dict[str, Any]], config: ScannerConfig
) -> dict[str, Any]:
    trigger = float(candidate.get("breakout_trigger") or 0)
    eligible = _eligible_bars(candidate, bars, config)
    if not eligible:
        return _unavailable_trade(
            candidate,
            "No minute bars were available at or after the recommendation timestamp.",
            entry_mode=config.entry_mode,
        )
    entry_mode = config.entry_mode
    if entry_mode == "breakout":
        entry_index = _breakout_entry_index(eligible, trigger)
        if entry_index is None:
            return _no_entry_trade(candidate, "Breakout trigger was not touched after signal.")
        entry_bar = eligible[entry_index]
        raw_entry_price = max(trigger, float(entry_bar["open"]))
        triggered = True
        after_entry = eligible[entry_index:]
    else:
        entry_bar = eligible[0]
        raw_entry_price = float(entry_bar["open"])
        triggered = any(float(bar["high"]) >= trigger for bar in eligible) if trigger > 0 else False
        after_entry = eligible
    entry_price = raw_entry_price
    entry_price *= 1 + (config.slippage_bps / 10_000)
    lunch_candidates = [
        bar for bar in after_entry if _time_part(str(bar["timestamp"])) <= config.lunch_exit_time
    ]
    lunch_bar = lunch_candidates[-1] if lunch_candidates else after_entry[-1]
    close_candidates = [
        bar for bar in after_entry if _time_part(str(bar["timestamp"])) <= config.close_exit_time
    ]
    close_bar = close_candidates[-1] if close_candidates else after_entry[-1]
    high_after_entry = max(float(bar["high"]) for bar in after_entry)
    low_after_entry = min(float(bar["low"]) for bar in after_entry)
    one_minute_bar = _bar_at_offset(after_entry, entry_bar, 1)
    five_minute_bar = _bar_at_offset(after_entry, entry_bar, 5)
    fifteen_minute_bar = _bar_at_offset(after_entry, entry_bar, 15)
    lunch_exit = float(lunch_bar["close"])
    close_exit = float(close_bar["close"])
    return {
        "audit_status": "audited",
        "audit_reason": "",
        "entry_mode": entry_mode,
        "rank": candidate.get("rank", ""),
        "score": candidate.get("score", ""),
        "ticker": str(candidate["ticker"]).upper(),
        "entry_time": entry_bar["timestamp"],
        "entry_price": round(entry_price, 4),
        "triggered": triggered,
        "lunch_exit_price": round(lunch_exit, 4),
        "close_exit_price": round(close_exit, 4),
        "high_after_entry": round(high_after_entry, 4),
        "low_after_entry": round(low_after_entry, 4),
        "slippage_bps": config.slippage_bps,
        "return_1m_pct": round(_return_pct(float(one_minute_bar["close"]), entry_price), 2),
        "return_5m_pct": round(_return_pct(float(five_minute_bar["close"]), entry_price), 2),
        "return_15m_pct": round(_return_pct(float(fifteen_minute_bar["close"]), entry_price), 2),
        "lunch_return_pct": round(_return_pct(lunch_exit, entry_price), 2),
        "close_return_pct": round(_return_pct(close_exit, entry_price), 2),
        "high_return_pct": round(_return_pct(high_after_entry, entry_price), 2),
        "low_drawdown_pct": round(_return_pct(low_after_entry, entry_price), 2),
        "max_favorable_excursion_pct": round(_return_pct(high_after_entry, entry_price), 2),
        "max_adverse_excursion_pct": round(_return_pct(low_after_entry, entry_price), 2),
    }


def _summary(
    trades: list[dict[str, Any]], top_n: int, *, fixture_only: bool = False
) -> dict[str, Any]:
    audited = _audited_trades(trades)
    no_entry = [row for row in trades if row.get("audit_status") == "no_entry_trigger"]
    unavailable = [row for row in trades if row.get("audit_status") == "unavailable"]
    return {
        "created_at": utc_now_iso(),
        "requested_top_n": top_n,
        "fixture_only": fixture_only,
        "entry_mode": _summary_entry_mode(trades),
        "trade_count": len(audited),
        "audit_unavailable_count": len(unavailable),
        "no_entry_trigger_count": len(no_entry),
        "avg_lunch_return_pct": _average(audited, "lunch_return_pct"),
        "avg_close_return_pct": _average(audited, "close_return_pct"),
        "avg_high_return_pct": _average(audited, "high_return_pct"),
        "avg_1m_return_pct": _average(audited, "return_1m_pct"),
        "avg_5m_return_pct": _average(audited, "return_5m_pct"),
        "avg_15m_return_pct": _average(audited, "return_15m_pct"),
        "median_lunch_return_pct": _median(audited, "lunch_return_pct"),
        "median_close_return_pct": _median(audited, "close_return_pct"),
        "median_high_return_pct": _median(audited, "high_return_pct"),
        "best_close_return_pct": _max_value(audited, "close_return_pct"),
        "worst_close_return_pct": _min_value(audited, "close_return_pct"),
        "max_drawdown_pct": _min_value(audited, "low_drawdown_pct"),
        "win_rate_close_pct": _win_rate(audited, "close_return_pct"),
        "win_rate_lunch_pct": _win_rate(audited, "lunch_return_pct"),
        "cumulative_returns": {
            f"top_{count}": {
                "lunch_return_pct": _equal_weight_top(audited, "lunch_return_pct", count),
                "close_return_pct": _equal_weight_top(audited, "close_return_pct", count),
                "high_return_pct": _equal_weight_top(audited, "high_return_pct", count),
            }
            for count in (1, 3, 5)
        },
    }


def _eligible_bars(
    candidate: dict[str, Any], bars: list[dict[str, Any]], config: ScannerConfig
) -> list[dict[str, Any]]:
    recommendation_at = _recommendation_timestamp(candidate)
    eligible = []
    for bar in bars:
        timestamp = str(bar["timestamp"])
        if _time_part(timestamp) < config.signal_time:
            continue
        if recommendation_at is not None and not _bar_at_or_after(timestamp, recommendation_at):
            continue
        eligible.append(bar)
    return eligible


def _recommendation_timestamp(candidate: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "recommendation_timestamp", "scan_timestamp", "as_of_timestamp"):
        parsed = _parse_timestamp(candidate.get(key))
        if parsed is not None:
            return parsed
    return None


def _bar_at_or_after(timestamp: str, recommendation_at: datetime) -> bool:
    parsed = _parse_timestamp(timestamp)
    if parsed is None:
        return _time_part(timestamp) >= recommendation_at.astimezone(EASTERN_TIME).strftime("%H:%M")
    if parsed.tzinfo is not None and recommendation_at.tzinfo is not None:
        return parsed.astimezone(EASTERN_TIME) >= recommendation_at.astimezone(EASTERN_TIME)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(EASTERN_TIME).replace(tzinfo=None)
    if recommendation_at.tzinfo is not None:
        recommendation_at = recommendation_at.astimezone(EASTERN_TIME).replace(tzinfo=None)
    return parsed >= recommendation_at


def _parse_timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _unavailable_trade(
    candidate: dict[str, Any], reason: str, *, entry_mode: str = ""
) -> dict[str, Any]:
    ticker = str(candidate.get("ticker", "")).upper()
    return {
        "audit_status": "unavailable",
        "audit_reason": reason,
        "entry_mode": entry_mode,
        "rank": candidate.get("rank", ""),
        "score": candidate.get("score", ""),
        "ticker": ticker,
        "entry_time": "",
        "entry_price": "",
        "triggered": False,
        "lunch_exit_price": "",
        "close_exit_price": "",
        "high_after_entry": "",
        "low_after_entry": "",
        "slippage_bps": "",
        "return_1m_pct": "",
        "return_5m_pct": "",
        "return_15m_pct": "",
        "lunch_return_pct": "",
        "close_return_pct": "",
        "high_return_pct": "",
        "low_drawdown_pct": "",
        "max_favorable_excursion_pct": "",
        "max_adverse_excursion_pct": "",
    }


def _no_entry_trade(candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    row = _unavailable_trade(candidate, reason, entry_mode="breakout")
    row["audit_status"] = "no_entry_trigger"
    row["entry_mode"] = "breakout"
    return row


def _breakout_entry_index(eligible: list[dict[str, Any]], trigger: float) -> int | None:
    if trigger <= 0:
        return 0
    for index, bar in enumerate(eligible):
        if float(bar["high"]) >= trigger:
            return index
    return None


def _summary_entry_mode(trades: list[dict[str, Any]]) -> str:
    modes = sorted({str(row.get("entry_mode", "")) for row in trades if row.get("entry_mode")})
    return ",".join(modes)


def _read_csv(path: str | Path, required: list[str], source: str) -> list[dict[str, Any]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise SnapshotValidationError(f"{source} file does not exist: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SnapshotValidationError(f"{source} file is missing a header row: {csv_path}")
        validate_required_columns(set(reader.fieldnames), required, str(csv_path))
        return list(reader)


def _time_part(timestamp: str) -> str:
    normalized = timestamp.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(EASTERN_TIME).strftime("%H:%M")
        return parsed.strftime("%H:%M")
    except ValueError:
        pass
    if "T" in timestamp:
        return timestamp.split("T", 1)[1][:5]
    if " " in timestamp:
        return timestamp.split(" ", 1)[1][:5]
    return timestamp[:5]


def _return_pct(exit_price: float, entry_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    return ((exit_price - entry_price) / entry_price) * 100


def _bar_at_offset(
    rows: list[dict[str, Any]], entry_bar: dict[str, Any], offset_minutes: int
) -> dict[str, Any]:
    if not rows:
        raise SnapshotValidationError("Cannot audit empty minute-bar set after entry")
    entry_time = _parse_timestamp(entry_bar.get("timestamp"))
    if entry_time is None:
        index = min(offset_minutes, len(rows) - 1)
        return rows[index]
    target = entry_time + timedelta(minutes=offset_minutes)
    for row in rows:
        parsed = _parse_timestamp(row.get("timestamp"))
        if parsed is not None and _datetime_at_or_after(parsed, target):
            return row
    return rows[-1]


def _average(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return round(sum(float(row[key]) for row in rows) / len(rows), 2)


def _median(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    values = sorted(float(row[key]) for row in rows)
    midpoint = len(values) // 2
    if len(values) % 2:
        return round(values[midpoint], 2)
    return round((values[midpoint - 1] + values[midpoint]) / 2, 2)


def _max_value(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return round(max(float(row[key]) for row in rows), 2)


def _min_value(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return round(min(float(row[key]) for row in rows), 2)


def _audited_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("audit_status", "audited") == "audited"]


def _equal_weight_top(rows: list[dict[str, Any]], key: str, count: int) -> float:
    if not rows:
        return 0.0
    selected = rows[:count]
    return round(sum(float(row[key]) for row in selected) / len(selected), 2)


def _win_rate(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    wins = sum(1 for row in rows if float(row[key]) > 0)
    return round((wins / len(rows)) * 100, 2)


def _datetime_at_or_after(value: datetime, target: datetime) -> bool:
    if value.tzinfo is not None and target.tzinfo is not None:
        return value.astimezone(EASTERN_TIME) >= target.astimezone(EASTERN_TIME)
    if value.tzinfo is not None:
        value = value.astimezone(EASTERN_TIME).replace(tzinfo=None)
    if target.tzinfo is not None:
        target = target.astimezone(EASTERN_TIME).replace(tzinfo=None)
    return value >= target
