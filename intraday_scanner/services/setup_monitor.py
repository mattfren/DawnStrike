"""Monitor earlier ranked setups against a fresh market snapshot.

The monitor is research/paper-trading infrastructure only. It never places
orders; it checks whether each saved setup is still following the original
levels created by the scanner.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import SnapshotRow, utc_now_iso

MONITOR_COLUMNS = [
    "checked_at",
    "ticker",
    "status",
    "rank",
    "score",
    "monitor_confidence_pct",
    "current_price",
    "recommended_price",
    "breakout_trigger",
    "invalidation_level",
    "first_target",
    "stretch_target",
    "expected_return_to_first_pct",
    "current_return_from_watch_pct",
    "distance_to_breakout_pct",
    "distance_to_first_target_pct",
    "path_progress_pct",
    "live_range_position_pct",
    "gap_pct",
    "dollar_volume",
    "spread_pct",
    "risk_flags",
    "expected_path",
    "reason",
    "as_of_timestamp",
]

STATUS_ORDER = {
    "confirming": 0,
    "watching": 1,
    "extended": 2,
    "fading": 3,
    "invalidated": 4,
    "missing": 5,
}

STATUS_CONFIDENCE_ADJUSTMENT = {
    "confirming": 10.0,
    "watching": 0.0,
    "extended": -8.0,
    "fading": -28.0,
    "invalidated": -70.0,
    "missing": -100.0,
}


def run_setup_monitor(
    *,
    candidates: Sequence[dict[str, Any]],
    snapshots: Sequence[SnapshotRow],
    out_dir: str | Path,
    store: Any | None = None,
    persist: bool = False,
    source_run_id: str | None = None,
    checked_at: str | None = None,
    top_n: int | None = None,
    symbols: Iterable[str] | None = None,
    config: ScannerConfig | None = None,
) -> dict[str, Any]:
    """Evaluate saved candidates, write outputs, and optionally persist checks."""

    rows = evaluate_setup_monitor(
        candidates=candidates,
        snapshots=snapshots,
        checked_at=checked_at,
        top_n=top_n,
        symbols=symbols,
        config=config,
    )
    summary = summarize_monitor_rows(rows, source_run_id=source_run_id)
    paths = write_monitor_outputs(rows, summary, out_dir)
    if persist:
        if store is None:
            raise ValueError("persist=True requires a store")
        store.persist_monitor_checks(rows, run_id=source_run_id)
    return {"rows": rows, "summary": summary, "paths": paths}


def evaluate_setup_monitor(
    *,
    candidates: Sequence[dict[str, Any]],
    snapshots: Sequence[SnapshotRow],
    checked_at: str | None = None,
    top_n: int | None = None,
    symbols: Iterable[str] | None = None,
    config: ScannerConfig | None = None,
) -> list[dict[str, Any]]:
    monitor_config = config or ScannerConfig()
    checked_at_value = checked_at or utc_now_iso()
    snapshot_by_ticker = {snapshot.ticker.upper(): snapshot for snapshot in snapshots}
    wanted = {symbol.strip().upper() for symbol in symbols or [] if symbol.strip()}
    selected_candidates = list(candidates)
    if wanted:
        selected_candidates = [
            candidate
            for candidate in selected_candidates
            if str(candidate.get("ticker", "")).upper() in wanted
        ]
    if top_n is not None:
        selected_candidates = selected_candidates[:top_n]

    rows = []
    for candidate in selected_candidates:
        ticker = str(candidate.get("ticker", "")).upper()
        snapshot = snapshot_by_ticker.get(ticker)
        if snapshot is None:
            rows.append(_missing_row(candidate, checked_at_value))
            continue
        rows.append(_evaluate_candidate(candidate, snapshot, checked_at_value, monitor_config))
    return sorted(rows, key=lambda row: (STATUS_ORDER.get(str(row["status"]), 99), row["ticker"]))


def summarize_monitor_rows(
    rows: Sequence[dict[str, Any]], source_run_id: str | None = None
) -> dict[str, Any]:
    counts = Counter(str(row.get("status", "missing")) for row in rows)
    checked_at = str(rows[0].get("checked_at", utc_now_iso())) if rows else utc_now_iso()
    actionable = counts.get("confirming", 0)
    warning = counts.get("fading", 0) + counts.get("invalidated", 0) + counts.get("extended", 0)
    top_row = rows[0] if rows else {}
    return {
        "checked_at": checked_at,
        "source_run_id": source_run_id,
        "setup_count": len(rows),
        "confirming_count": counts.get("confirming", 0),
        "watching_count": counts.get("watching", 0),
        "extended_count": counts.get("extended", 0),
        "fading_count": counts.get("fading", 0),
        "invalidated_count": counts.get("invalidated", 0),
        "missing_count": counts.get("missing", 0),
        "actionable_count": actionable,
        "warning_count": warning,
        "top_status": str(top_row.get("status", "none")),
        "top_ticker": str(top_row.get("ticker", "none")),
    }


def write_monitor_outputs(
    rows: Sequence[dict[str, Any]], summary: dict[str, Any], out_dir: str | Path
) -> dict[str, Path]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "checks": output_dir / "setup_monitor_checks.csv",
        "summary": output_dir / "setup_monitor_summary.json",
    }
    with paths["checks"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MONITOR_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    paths["summary"].write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return paths


def _evaluate_candidate(
    candidate: dict[str, Any],
    snapshot: SnapshotRow,
    checked_at: str,
    config: ScannerConfig,
) -> dict[str, Any]:
    current_price = snapshot.premarket_price
    recommended_price = _number(candidate.get("premarket_price"), current_price)
    breakout = _number(candidate.get("breakout_trigger"), snapshot.premarket_high)
    invalidation = _number(candidate.get("invalidation_level"), snapshot.premarket_low)
    first_target = _number(candidate.get("first_target"), breakout)
    stretch_target = _number(candidate.get("stretch_target"), first_target)
    pullback_low, pullback_high = _pullback_zone(candidate.get("pullback_zone"))
    live_range_position = _range_position(
        current_price, snapshot.premarket_low, snapshot.premarket_high
    )
    original_dollar_volume = _number(candidate.get("dollar_volume"))
    volume_ratio = (
        snapshot.dollar_volume / original_dollar_volume if original_dollar_volume else 1.0
    )
    current_return_from_watch_pct = _pct(current_price, recommended_price)

    status, reason = _classify_status(
        snapshot=snapshot,
        current_price=current_price,
        recommended_price=recommended_price,
        breakout=breakout,
        invalidation=invalidation,
        first_target=first_target,
        stretch_target=stretch_target,
        pullback_low=pullback_low,
        live_range_position=live_range_position,
        volume_ratio=volume_ratio,
        drop_from_watch_pct=config.monitor_drop_from_watch_pct,
        volume_collapse_ratio=config.monitor_volume_collapse_ratio,
        rejection_range_pct=config.monitor_rejection_range_pct,
    )
    confidence = _monitor_confidence(candidate, snapshot, status, live_range_position)
    risk_flags = _risk_flags(
        candidate,
        snapshot,
        current_return_from_watch_pct=current_return_from_watch_pct,
        drop_from_watch_pct=config.monitor_drop_from_watch_pct,
        volume_ratio=volume_ratio,
        volume_collapse_ratio=config.monitor_volume_collapse_ratio,
        breakout=breakout,
        current_price=current_price,
        live_range_position=live_range_position,
        rejection_range_pct=config.monitor_rejection_range_pct,
    )
    return {
        "checked_at": checked_at,
        "ticker": snapshot.ticker,
        "status": status,
        "rank": _int(candidate.get("rank")),
        "score": round(_number(candidate.get("score")), 2),
        "monitor_confidence_pct": confidence,
        "current_price": round(current_price, 4),
        "recommended_price": round(recommended_price, 4),
        "breakout_trigger": round(breakout, 4),
        "invalidation_level": round(invalidation, 4),
        "first_target": round(first_target, 4),
        "stretch_target": round(stretch_target, 4),
        "expected_return_to_first_pct": _pct(first_target, breakout),
        "current_return_from_watch_pct": current_return_from_watch_pct,
        "distance_to_breakout_pct": _pct(current_price, breakout),
        "distance_to_first_target_pct": _pct(current_price, first_target),
        "path_progress_pct": _path_progress(current_price, invalidation, first_target),
        "live_range_position_pct": round(live_range_position, 2),
        "gap_pct": round(snapshot.gap_pct, 2),
        "dollar_volume": round(snapshot.dollar_volume, 2),
        "spread_pct": round(snapshot.spread_pct, 4),
        "risk_flags": ";".join(risk_flags),
        "expected_path": _expected_path_text(
            breakout=breakout,
            invalidation=invalidation,
            first_target=first_target,
            pullback_high=pullback_high,
        ),
        "reason": reason,
        "as_of_timestamp": snapshot.as_of_timestamp,
    }


def _classify_status(
    *,
    snapshot: SnapshotRow,
    current_price: float,
    recommended_price: float,
    breakout: float,
    invalidation: float,
    first_target: float,
    stretch_target: float,
    pullback_low: float,
    live_range_position: float,
    volume_ratio: float,
    drop_from_watch_pct: float,
    volume_collapse_ratio: float,
    rejection_range_pct: float,
) -> tuple[str, str]:
    if snapshot.current_halt:
        return "invalidated", "Current halt flag is active."
    if snapshot.recent_offering:
        return "invalidated", "Recent offering flag is active."
    if invalidation > 0 and current_price <= invalidation:
        return "invalidated", "Price is at or below the original invalidation level."
    if recommended_price > 0 and _pct(current_price, recommended_price) <= -drop_from_watch_pct:
        return "invalidated", "Price dropped beyond the configured watch-price limit."
    if stretch_target > 0 and current_price >= stretch_target:
        return "extended", "Price is beyond the stretch target; the original entry is late."
    if first_target > 0 and current_price >= first_target:
        return "extended", "Price is already beyond the first target."
    if breakout > 0 and current_price >= breakout:
        return "confirming", "Price is above the breakout trigger and below target."
    if (
        breakout > 0
        and snapshot.premarket_high >= breakout
        and live_range_position < rejection_range_pct
    ):
        return "fading", "Price rejected the breakout area after testing it."
    if pullback_low > 0 and current_price < pullback_low:
        return "fading", "Price has lost the lower edge of the planned pullback zone."
    if volume_ratio <= volume_collapse_ratio:
        return "fading", "Dollar volume collapsed versus the original setup snapshot."
    if live_range_position < 35:
        return "fading", "Price is sitting in the lower third of the current range."
    return "watching", "Setup is intact but has not confirmed the breakout trigger."


def _missing_row(candidate: dict[str, Any], checked_at: str) -> dict[str, Any]:
    ticker = str(candidate.get("ticker", "")).upper()
    return {
        "checked_at": checked_at,
        "ticker": ticker,
        "status": "missing",
        "rank": _int(candidate.get("rank")),
        "score": round(_number(candidate.get("score")), 2),
        "monitor_confidence_pct": 0.0,
        "current_price": 0.0,
        "recommended_price": round(_number(candidate.get("premarket_price")), 4),
        "breakout_trigger": round(_number(candidate.get("breakout_trigger")), 4),
        "invalidation_level": round(_number(candidate.get("invalidation_level")), 4),
        "first_target": round(_number(candidate.get("first_target")), 4),
        "stretch_target": round(_number(candidate.get("stretch_target")), 4),
        "expected_return_to_first_pct": 0.0,
        "current_return_from_watch_pct": 0.0,
        "distance_to_breakout_pct": 0.0,
        "distance_to_first_target_pct": 0.0,
        "path_progress_pct": 0.0,
        "live_range_position_pct": 0.0,
        "gap_pct": 0.0,
        "dollar_volume": 0.0,
        "spread_pct": 0.0,
        "risk_flags": "missing_snapshot",
        "expected_path": "No current snapshot row was available for this ticker.",
        "reason": "No current snapshot row was available for this ticker.",
        "as_of_timestamp": "",
    }


def _monitor_confidence(
    candidate: dict[str, Any],
    snapshot: SnapshotRow,
    status: str,
    live_range_position: float,
) -> float:
    base = _number(candidate.get("score"))
    status_adjustment = STATUS_CONFIDENCE_ADJUSTMENT.get(status, -20.0)
    range_adjustment = max(-10.0, min(10.0, (live_range_position - 50.0) * 0.2))
    original_dollar_volume = _number(candidate.get("dollar_volume"))
    if original_dollar_volume > 0:
        volume_ratio = snapshot.dollar_volume / original_dollar_volume
        volume_adjustment = max(-8.0, min(8.0, (volume_ratio - 1.0) * 8.0))
    else:
        volume_adjustment = 0.0
    spread_adjustment = -8.0 if snapshot.spread_pct >= 5 else 0.0
    risk_adjustment = -12.0 if snapshot.current_halt or snapshot.recent_offering else 0.0
    value = base + status_adjustment + range_adjustment + volume_adjustment
    value += spread_adjustment + risk_adjustment
    return round(max(0.0, min(100.0, value)), 1)


def _risk_flags(
    candidate: dict[str, Any],
    snapshot: SnapshotRow,
    *,
    current_return_from_watch_pct: float,
    drop_from_watch_pct: float,
    volume_ratio: float,
    volume_collapse_ratio: float,
    breakout: float,
    current_price: float,
    live_range_position: float,
    rejection_range_pct: float,
) -> list[str]:
    flags: list[str] = []
    raw_flags = str(candidate.get("risk_flags", "") or "")
    flags.extend(flag for flag in raw_flags.split(";") if flag)
    if snapshot.current_halt:
        flags.append("current_halt")
    if snapshot.recent_offering:
        flags.append("recent_offering")
    if snapshot.spread_pct >= 5:
        flags.append("wide_spread")
    if current_return_from_watch_pct <= -drop_from_watch_pct:
        flags.append("drop_from_watch")
    if volume_ratio <= volume_collapse_ratio:
        flags.append("volume_collapse")
    if breakout > 0 and snapshot.premarket_high >= breakout and current_price < breakout:
        if live_range_position < rejection_range_pct:
            flags.append("breakout_rejection")
    return sorted(set(flags))


def _expected_path_text(
    *,
    breakout: float,
    invalidation: float,
    first_target: float,
    pullback_high: float,
) -> str:
    pullback_text = f" or hold pullback under ${pullback_high:.4f}" if pullback_high > 0 else ""
    return (
        f"Stay above ${invalidation:.4f}, reclaim ${breakout:.4f}{pullback_text}, "
        f"then work toward ${first_target:.4f}."
    )


def _pullback_zone(value: Any) -> tuple[float, float]:
    raw = str(value or "").replace("$", "").strip()
    if not raw or "-" not in raw:
        return 0.0, 0.0
    left, right = raw.split("-", 1)
    low = _number(left)
    high = _number(right)
    if low > high:
        low, high = high, low
    return low, high


def _range_position(current_price: float, low: float, high: float) -> float:
    if high <= low:
        return 50.0
    return ((current_price - low) / (high - low)) * 100


def _path_progress(current_price: float, invalidation: float, first_target: float) -> float:
    if first_target <= invalidation:
        return 0.0
    return round(((current_price - invalidation) / (first_target - invalidation)) * 100, 2)


def _pct(value: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return round(((value - reference) / reference) * 100, 2)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value in {None, ""}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any) -> int:
    try:
        if value in {None, ""}:
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0
