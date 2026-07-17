"""Fail-closed recomputation of retained mover paper-trade evidence.

This module intentionally has no runtime/provider imports. A matching evidence
file hash alone is not learning truth: the outcome must recompute from the bars,
timestamps, fill policy, fees, and slippage retained with it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import market_session
from intraday_scanner.v2.mover_pattern_lab.contracts import stable_id

MARKET_TZ = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)


def retained_trade_evidence_recomputes(row: Mapping[str, Any]) -> bool:
    """Return whether a closed v2 trade exactly recomputes from retained bars."""

    digest = str(row.get("bars_evidence_sha256") or "").lower()
    path_text = str(row.get("bars_evidence_path") or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None or not path_text:
        return False
    path = Path(path_text)
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != digest:
        return False
    return closed_trade_recomputes(row, payload)


def closed_trade_recomputes(
    row: Mapping[str, Any],
    evidence_payload: Any,
) -> bool:
    """Recompute timestamps, trigger, fills, costs, returns, and path metrics."""

    try:
        if (
            row.get("schema_version") != "v2.mover_paper_trade.v1"
            or row.get("status") != "closed"
            or row.get("direction") != "long"
            or row.get("bar_timestamp_semantics") != "bar_close"
            or row.get("entry_fill_policy") != "next_bar_open"
            or row.get("intrabar_ambiguity_policy") != "stop_first"
            or not _strict_true(row.get("source_coverage_complete"))
            or not _strict_true(row.get("source_bar_sequence_complete"))
            or not _strict_true(row.get("research_only"))
            or not _strict_false(row.get("broker_execution_enabled"))
            or not isinstance(evidence_payload, list)
            or not evidence_payload
        ):
            return False

        interval_minutes = _integer(row.get("bar_interval_minutes"))
        if interval_minutes < 1 or interval_minutes > 30:
            return False
        step = timedelta(minutes=interval_minutes)
        market_date = str(row.get("market_date") or "")
        symbol = str(row.get("symbol") or "").upper()
        if not market_date or not symbol:
            return False
        market_day = date.fromisoformat(market_date)
        session = market_session(market_day)
        if not session.is_trading_day or session.close_time_et is None:
            return False
        published_close_at = datetime.combine(
            market_day,
            time.fromisoformat(session.close_time_et),
            tzinfo=MARKET_TZ,
        )
        published_open_at = datetime.combine(
            market_day,
            RTH_OPEN,
            tzinfo=MARKET_TZ,
        )

        bars = [
            _bar(item, symbol=symbol, market_date=market_date)
            for item in evidence_payload
        ]
        if any(
            later["timestamp"] - earlier["timestamp"] != step
            for earlier, later in zip(bars, bars[1:], strict=False)
        ):
            return False

        signal_at = _aware_datetime(row.get("signal_at"))
        eligible_entry_at = _aware_datetime(row.get("eligible_entry_at"))
        entry_at = _aware_datetime(row.get("entry_at"))
        exit_at = _aware_datetime(row.get("exit_at"))
        session_close_at = _aware_datetime(row.get("session_close_at"))
        entry_source_at = _aware_datetime(row.get("entry_source_bar_at"))
        exit_source_at = _aware_datetime(row.get("exit_source_bar_at"))
        if not signal_at <= eligible_entry_at == entry_at <= exit_at <= session_close_at:
            return False
        if (
            session_close_at != published_close_at
            or signal_at.astimezone(MARKET_TZ).time() < RTH_OPEN
            or entry_at.astimezone(MARKET_TZ).time() < RTH_OPEN
            or eligible_entry_at >= session_close_at
            or any(
                not RTH_OPEN < bar["timestamp"].astimezone(MARKET_TZ).time()
                <= time.fromisoformat(session.close_time_et)
                for bar in bars
            )
            or any(
                (
                    bar["timestamp"].astimezone(MARKET_TZ)
                    - published_open_at
                ).total_seconds()
                % step.total_seconds()
                != 0
                for bar in bars
            )
        ):
            return False
        if (
            bars[0]["timestamp"] != entry_source_at
            or bars[-1]["timestamp"] != exit_source_at
            or entry_source_at != entry_at + step
            or exit_source_at != exit_at
        ):
            return False
        if any(
            value.astimezone(MARKET_TZ).date().isoformat() != market_date
            for value in (
                signal_at,
                eligible_entry_at,
                entry_at,
                exit_at,
                session_close_at,
                entry_source_at,
                exit_source_at,
            )
        ):
            return False

        evidence_mode = str(row.get("evidence_mode") or "")
        if evidence_mode not in {"historical_replay", "forward_observation"}:
            return False
        expected_eligible_entry_at = signal_at
        if evidence_mode == "forward_observation":
            source_captured_at = _aware_datetime(row.get("source_captured_at"))
            system_received_at = _aware_datetime(row.get("system_received_at"))
            if (
                source_captured_at != system_received_at
                or not signal_at
                <= source_captured_at
                <= signal_at + timedelta(minutes=5)
                or source_captured_at > entry_at
                or not _forward_receipt_matches_trade(row)
            ):
                return False
            elapsed_seconds = (source_captured_at - signal_at).total_seconds()
            expected_eligible_entry_at = signal_at + math.ceil(
                elapsed_seconds / step.total_seconds()
            ) * step
        if eligible_entry_at != expected_eligible_entry_at:
            return False

        stop = _number(row.get("stop"))
        target = _number(row.get("target"))
        slippage_bps = _number(row.get("slippage_bps"))
        fee_bps = _number(row.get("fee_bps"))
        notional = _number(row.get("notional_per_trade"))
        if (
            stop <= 0
            or target <= stop
            or slippage_bps < 0
            or fee_bps < 0
            or notional <= 0
        ):
            return False

        entry_reference = bars[0]["open"]
        rate = slippage_bps / 10_000.0
        fee_rate = fee_bps / 10_000.0
        entry_price = entry_reference * (1.0 + rate)
        if not stop < entry_price < target:
            return False

        expected_reason: str | None = None
        expected_exit_reference: float | None = None
        expected_exit_index: int | None = None
        for index, bar in enumerate(bars):
            if bar["low"] <= stop:
                expected_reason = "stop_gap" if bar["open"] < stop else "stop"
                expected_exit_reference = min(stop, bar["open"])
                expected_exit_index = index
                break
            if bar["high"] >= target:
                expected_reason = "target"
                expected_exit_reference = target
                expected_exit_index = index
                break
        if expected_reason is None:
            if bars[-1]["timestamp"] != session_close_at:
                return False
            expected_reason = "eod_flat"
            expected_exit_reference = bars[-1]["close"]
            expected_exit_index = len(bars) - 1
        if expected_exit_index != len(bars) - 1:
            return False
        if row.get("reason") != expected_reason:
            return False
        if expected_exit_reference is None:
            return False
        exit_reference = _number(row.get("exit_reference"))
        if not _close(exit_reference, expected_exit_reference, 1e-8):
            return False
        if expected_reason == "eod_flat":
            if (
                exit_at != session_close_at
                or row.get("exit_time_status") != "exact_session_close"
            ):
                return False
        elif row.get("exit_time_status") != "interval_censored_within_source_bar":
            return False
        if (
            _aware_datetime(row.get("exit_window_start_at")) != exit_at - step
            or _aware_datetime(row.get("exit_window_end_at")) != exit_at
        ):
            return False

        exit_price = expected_exit_reference * (1.0 - rate)
        quantity = notional / entry_price
        entry_fee = quantity * entry_price * fee_rate
        exit_fee = quantity * exit_price * fee_rate
        fee_cost = entry_fee + exit_fee
        slippage_cost = quantity * (
            (entry_price - entry_reference) + (expected_exit_reference - exit_price)
        )
        total_cost = fee_cost + slippage_cost
        reference_gross_pnl = quantity * (expected_exit_reference - entry_reference)
        fill_pnl = quantity * (exit_price - entry_price)
        net_pnl = fill_pnl - fee_cost
        gross_return = reference_gross_pnl / notional * 100.0
        fill_return = fill_pnl / notional * 100.0
        net_return = net_pnl / notional * 100.0
        high_water = max(bar["high"] for bar in bars)
        low_water = min(bar["low"] for bar in bars)
        mfe = (high_water / entry_price - 1.0) * 100.0
        mae = (low_water / entry_price - 1.0) * 100.0

        expected_numbers = (
            ("entry_reference", entry_reference, 1e-8),
            ("entry_price", entry_price, 2e-8),
            ("exit_price", exit_price, 2e-8),
            ("quantity", quantity, 2e-8),
            ("entry_fee", entry_fee, 2e-6),
            ("exit_fee", exit_fee, 2e-6),
            ("fee_cost", fee_cost, 2e-6),
            ("slippage_cost", slippage_cost, 2e-6),
            ("total_cost", total_cost, 3e-6),
            ("reference_gross_pnl", reference_gross_pnl, 2e-6),
            ("pnl", net_pnl, 2e-6),
            ("gross_return_pct", gross_return, 2e-6),
            ("fill_return_pct", fill_return, 2e-6),
            ("net_return_pct", net_return, 2e-6),
            ("mfe_pct", mfe, 2e-6),
            ("mae_pct", mae, 2e-6),
        )
        return all(
            _close(_number(row.get(field)), expected, tolerance)
            for field, expected, tolerance in expected_numbers
        )
    except (MarketCalendarCoverageError, TypeError, ValueError, OverflowError):
        return False


def _bar(item: Any, *, symbol: str, market_date: str) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ValueError("bar evidence row must be an object")
    timestamp = _aware_datetime(item.get("timestamp"))
    open_price = _number(item.get("open"))
    high = _number(item.get("high"))
    low = _number(item.get("low"))
    close = _number(item.get("close"))
    volume = _number(item.get("volume"))
    if (
        str(item.get("symbol") or "").upper() != symbol
        or timestamp.astimezone(MARKET_TZ).date().isoformat() != market_date
        or min(open_price, high, low, close) <= 0
        or volume <= 0
        or high < max(open_price, low, close)
        or low > min(open_price, high, close)
    ):
        raise ValueError("invalid retained bar evidence")
    return {
        "timestamp": timestamp,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _aware_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("number must be finite")
    return number


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer")
    number = int(value)
    if float(value) != number:
        raise ValueError("integer value required")
    return number


def _close(actual: float, expected: float, tolerance: float) -> bool:
    return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=tolerance)


def _forward_receipt_matches_trade(row: Mapping[str, Any]) -> bool:
    receipt_ref = str(row.get("forward_receipt_ref") or "")
    source_refs = row.get("source_refs")
    if not isinstance(source_refs, list) or receipt_ref not in source_refs:
        return False
    parts = receipt_ref.split(":", 2)
    if (
        len(parts) != 3
        or parts[0] != "sha256"
        or re.fullmatch(r"[0-9a-f]{64}", parts[1]) is None
    ):
        return False
    receipt = _json_artifact_payload(receipt_ref)
    if receipt is None:
        return False
    cutoffs = receipt.get("feature_cutoffs_at")
    return bool(
        receipt.get("schema_version") == "v2.mover_forward_source_receipt.v1"
        and receipt.get("evidence_mode") == "forward_observation"
        and receipt.get("market_date") == row.get("market_date")
        and isinstance(cutoffs, list)
        and row.get("signal_at") in cutoffs
        and receipt.get("system_received_at") == row.get("system_received_at")
        and receipt.get("authoritative_source_captured_at")
        == row.get("source_captured_at")
        and receipt.get("research_only") is True
        and receipt.get("broker_execution_enabled") is False
        and _forward_signal_snapshot_lineage_matches(row)
    )


def _forward_signal_snapshot_lineage_matches(row: Mapping[str, Any]) -> bool:
    signal_ref = str(row.get("signal_artifact_ref") or "")
    snapshot_ref = str(row.get("snapshot_artifact_ref") or "")
    source_refs = row.get("source_refs")
    if (
        not isinstance(source_refs, list)
        or signal_ref not in source_refs
        or snapshot_ref not in source_refs
    ):
        return False
    signal = _json_artifact_payload(signal_ref)
    snapshot = _json_artifact_payload(snapshot_ref)
    if signal is None or snapshot is None:
        return False
    signal_id = str(row.get("signal_id") or "")
    snapshot_id = str(row.get("snapshot_id") or "")
    strategy_id = str(row.get("strategy_id") or "")
    strategy_version = str(row.get("strategy_version") or "")
    if signal_id != stable_id(
        "mover_paper_signal",
        strategy_id,
        strategy_version,
        snapshot_id,
    ):
        return False
    exact_signal_fields = (
        "signal_id",
        "snapshot_id",
        "strategy_id",
        "strategy_version",
        "market_date",
        "symbol",
        "signal_at",
        "evidence_mode",
        "source_captured_at",
        "system_received_at",
        "forward_receipt_ref",
        "stop",
        "target",
    )
    if any(signal.get(field) != row.get(field) for field in exact_signal_fields):
        return False
    return bool(
        signal.get("research_only") is True
        and signal.get("broker_execution_enabled") is False
        and signal.get("entry_reference") == row.get("signal_entry_reference")
        and snapshot.get("snapshot_id") == snapshot_id
        and snapshot.get("market_date") == row.get("market_date")
        and snapshot.get("symbol") == row.get("symbol")
        and snapshot.get("feature_cutoff_at") == row.get("signal_at")
        and snapshot.get("evidence_mode") == "forward_observation"
        and snapshot.get("source_captured_at") == row.get("source_captured_at")
        and snapshot.get("system_received_at") == row.get("system_received_at")
        and snapshot.get("forward_receipt_ref") == row.get("forward_receipt_ref")
    )


def _json_artifact_payload(reference: str) -> dict[str, Any] | None:
    parts = reference.split(":", 2)
    if (
        len(parts) != 3
        or parts[0] != "sha256"
        or re.fullmatch(r"[0-9a-f]{64}", parts[1]) is None
    ):
        return None
    try:
        payload = json.loads(Path(parts[2]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload if hashlib.sha256(canonical).hexdigest() == parts[1] else None


def _strict_true(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1)


def _strict_false(value: Any) -> bool:
    return value is False or (type(value) is int and value == 0)


__all__ = ["closed_trade_recomputes", "retained_trade_evidence_recomputes"]
