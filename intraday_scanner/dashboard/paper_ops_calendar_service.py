"""Truth-gated read model for the PaperOps strategy calendar dashboard.

The dashboard is a read-only consumer.  It never rebuilds PaperOps state and it
never fills missing values with zero.  Every strategy series is kept separate by
mode, version, execution policy, and semantics fingerprint.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import (
    FIRST_ELIGIBLE_ACTIVATION_POLICY,
    market_session,
    registration_coverage_inception_date,
)
from intraday_scanner.paper_ops_root import production_paper_ops_root

JsonDict = dict[str, Any]
SeriesIdentity = tuple[str, str, str, str]
_LEGACY_ACTIVATION_POLICY = FIRST_ELIGIBLE_ACTIVATION_POLICY

_FLOAT_FIELDS = {
    "starting_equity",
    "ending_equity",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "daily_return_pct",
    "cumulative_return_pct",
    "drawdown_pct",
    "average_r",
    "expectancy_r",
    "exposure_pct",
    "fees_paid",
    "slippage_estimate",
}
_INTEGER_FIELDS = {
    "trades_opened",
    "trades_closed",
    "pending_orders",
    "open_positions",
    "wins",
    "losses",
    "flats",
}
_IDENTITY_FIELDS = (
    "mode",
    "strategy_id",
    "strategy_version",
    "execution_policy_version",
    "strategy_semantics_fingerprint",
)
_REQUIRED_FIELDS = {
    "date",
    *_IDENTITY_FIELDS,
    "strategy_status",
    "data_snapshot_id",
    "starting_equity",
    "ending_equity",
    "total_pnl",
    "daily_return_pct",
    "cumulative_return_pct",
    "drawdown_pct",
    "run_id",
}

_STRATEGY_LABELS = {
    "ts_momentum_sma_atr": "Trend Momentum · SMA / ATR",
    "donchian_breakout_20_10": "Donchian Breakout · 20 / 10",
    "cross_sectional_relative_strength": "Cross-Sectional Strength",
    "pullback_reclaim_uptrend": "Pullback Reclaim",
    "volatility_contraction_breakout": "Volatility Contraction",
    "failed_breakout_reversal_short": "Failed Breakout · Short",
    "bullish_fvg_continuation": "Bullish FVG Continuation",
    "gap_up_continuation": "Gap-Up Continuation",
    "gap_up_continuation_atr": "ATR Gap-Up Continuation",
    "benchmark_buy_hold_equal_weight": "Configured-Universe Benchmark",
    "cash_no_trade_baseline": "Cash Baseline",
}


class PaperOpsCalendarError(ValueError):
    """Raised when retained calendar evidence is malformed or contradictory."""


def strategy_label(strategy_id: str) -> str:
    """Return a concise operator label without changing the strategy identity."""

    raw = str(strategy_id or "").strip()
    return _STRATEGY_LABELS.get(raw, raw.replace("_", " ").title() or "Unknown strategy")


def format_return_fraction(value: object, *, decimals: int = 2) -> str:
    """Format a stored fractional return as a percentage exactly once."""

    number = _finite_decimal(value)
    if number is None:
        return "N/A"
    percent = number * Decimal("100")
    if percent == 0:
        return f"Flat {Decimal(0):.{decimals}f}%"
    sign = "+" if percent > 0 else "-"
    return f"{sign}{abs(percent):.{decimals}f}%"


def load_paper_ops_calendar(output_root: str | Path | None = None) -> JsonDict:
    """Load canonical calendar, registry, truth, and blotter artifacts read-only."""

    root = production_paper_ops_root(override=output_root)
    calendar_path = root / "calendar" / "strategy_daily_returns.csv"
    if not calendar_path.is_file():
        return {
            "status": "unavailable",
            "output_root": str(root),
            "available_modes": [],
            "rows": [],
            "blotter_rows": [],
            "gates": {},
            "warnings": ["Canonical PaperOps strategy calendar is not available."],
        }

    raw_rows = _read_csv(calendar_path)
    if not raw_rows:
        return {
            "status": "unavailable",
            "output_root": str(root),
            "available_modes": [],
            "rows": [],
            "blotter_rows": [],
            "gates": {},
            "warnings": ["Canonical PaperOps strategy calendar has no retained rows."],
        }
    missing_columns = sorted(_REQUIRED_FIELDS - set(raw_rows[0]))
    if missing_columns:
        raise PaperOpsCalendarError(
            "PaperOps calendar is missing required fields: " + ", ".join(missing_columns)
        )

    official_registry = _load_list(root / "state" / "strategy_registry.json")
    challenger_registry = _load_challengers(
        root / "state" / "strategy_challenger_registry.json"
    )
    official_inceptions = (
        _official_registry_inceptions(root, official_registry)
        if official_registry
        else {}
    )
    official_keys = set(official_inceptions)
    candidate_by_key = {
        _candidate_identity(row): str(row.get("challenger_id") or "")
        for row in challenger_registry
    }

    rows: list[JsonDict] = []
    seen: set[tuple[str, ...]] = set()
    for raw in raw_rows:
        row = _normalize_row(raw)
        identity = tuple(str(row.get(field) or "") for field in _IDENTITY_FIELDS)
        row_key = (str(row["date"]), *identity)
        if row_key in seen:
            raise PaperOpsCalendarError(
                "Duplicate PaperOps calendar row for " + " | ".join(row_key)
            )
        seen.add(row_key)
        role, challenger_id = _series_role(row, official_keys, candidate_by_key)
        row["series_role"] = role
        row["challenger_id"] = challenger_id
        row["series_key"] = "|".join(identity)
        row["strategy_label"] = strategy_label(str(row["strategy_id"]))
        row["session_open_equity"] = _subtract(
            row.get("ending_equity"), row.get("total_pnl")
        )
        if role == "official":
            inception = official_inceptions[_row_identity(row)]
            row["registry_inception_date"] = inception["registry_inception_date"]
            row["registration_status"] = (
                "registered"
                if str(row["date"]) >= str(inception["registry_inception_date"])
                else "not_yet_registered"
            )
            row["evidence_scope"] = (
                "official_forward"
                if row["mode"] == "forward" and row["registration_status"] == "registered"
                else (
                    "pre_registration_forward_evidence"
                    if row["mode"] == "forward"
                    else "counterfactual_replay"
                )
            )
        rows.append(row)

    rows.sort(
        key=lambda row: (
            str(row["mode"]),
            str(row["date"]),
            _role_order(str(row["series_role"])),
            str(row["strategy_label"]),
            str(row["series_key"]),
        )
    )
    available_modes = sorted(
        {str(row["mode"]) for row in rows},
        key=lambda value: ({"forward": 0, "replay": 1, "demo": 2}.get(value, 9), value),
    )
    gates = _load_gates(root, available_modes)
    blotter = _load_object(root / "exports" / "paper_trade_blotter.json")
    blotter_rows = blotter.get("rows") if blotter.get("status") == "passed" else []
    if not isinstance(blotter_rows, list):
        blotter_rows = []

    core_names = ("reconciliation", "calendar_truth", "ledger_rebuild")
    core_passed = bool(official_registry) and all(
        gates.get(name, {}).get("status") == "passed" for name in core_names
    )
    warnings: list[str] = []
    if not official_registry:
        warnings.append("Official strategy registry is missing; fleet claims are blocked.")
    for name in core_names:
        if gates.get(name, {}).get("status") != "passed":
            warnings.append(f"{name.replace('_', ' ').title()} gate is not passed.")
    source_mtime = calendar_path.stat().st_mtime_ns
    for name in core_names:
        gate_path = Path(str(gates.get(name, {}).get("path") or ""))
        if gate_path.is_file() and gate_path.stat().st_mtime_ns < source_mtime:
            core_passed = False
            warnings.append(f"{name.replace('_', ' ').title()} gate is stale.")

    return {
        "status": "verified" if core_passed else "blocked",
        "output_root": str(root),
        "source_path": str(calendar_path),
        "source_sha256": hashlib.sha256(calendar_path.read_bytes()).hexdigest(),
        "source_modified_at": calendar_path.stat().st_mtime,
        "source_modified_at_ns": calendar_path.stat().st_mtime_ns,
        "available_modes": available_modes,
        "rows": rows,
        "blotter_rows": [dict(row) for row in blotter_rows if isinstance(row, dict)],
        "blotter_mode": str(blotter.get("mode") or ""),
        "gates": gates,
        "warnings": sorted(dict.fromkeys(warnings)),
        "official_strategy_count": len(official_registry),
        "official_series": [
            {
                "registry_key": "|".join(identity),
                "strategy_id": identity[0],
                "strategy_label": strategy_label(identity[0]),
                "strategy_version": identity[1],
                "execution_policy_version": identity[2],
                "strategy_semantics_fingerprint": identity[3],
                **official_inceptions[identity],
            }
            for identity in sorted(official_inceptions)
        ],
        "challenger_count": len(challenger_registry),
        "research_only": True,
        "broker_execution_allowed": False,
    }


def build_paper_ops_calendar_view(dataset: JsonDict, mode: str) -> JsonDict:
    """Build exact per-mode strategy, fleet, reference, and lifecycle summaries."""

    requested_mode = str(mode or "").strip().lower()
    available = [str(item) for item in dataset.get("available_modes") or []]
    if requested_mode not in available:
        return {
            "status": "empty",
            "mode": requested_mode,
            "truth_status": "unavailable",
            "dates": [],
            "rows": [],
            "day_summaries": [],
            "strategy_summaries": [],
            "blotter_rows": [],
            "warnings": [f"No {requested_mode or 'selected'} PaperOps sessions are retained."],
        }

    mode_rows = [
        dict(row) for row in dataset.get("rows") or [] if row.get("mode") == requested_mode
    ]
    impossible_forward_rows = [
        row
        for row in mode_rows
        if row.get("series_role") == "official"
        and row.get("mode") == "forward"
        and row.get("registration_status") == "not_yet_registered"
    ]
    official_rows = [
        row
        for row in mode_rows
        if row.get("series_role") == "official"
        and row not in impossible_forward_rows
    ]
    challenger_rows = [row for row in mode_rows if row.get("series_role") == "challenger"]
    benchmark_rows = [row for row in mode_rows if row.get("series_role") == "benchmark"]
    cash_rows = [row for row in mode_rows if row.get("series_role") == "cash"]
    unknown_rows = [row for row in mode_rows if row.get("series_role") == "unregistered"]
    dates = sorted({str(row["date"]) for row in mode_rows})
    if requested_mode == "forward" and len(dates) >= 2:
        dates = _retained_market_session_span(dates)
    registry_series = _official_series_for_mode(dataset, requested_mode)
    observed_inceptions: dict[str, str] = {}
    for row in official_rows:
        series_key = str(row["series_key"])
        row_date = str(row["date"])
        observed_inceptions[series_key] = min(
            row_date,
            observed_inceptions.get(series_key, row_date),
        )
    source_gate = dict(
        (dataset.get("gates") or {}).get(f"source_bar_truth_{requested_mode}") or {}
    )
    source_gate_path = Path(str(source_gate.get("path") or ""))
    source_gate_mode = str(source_gate.get("mode") or "").strip().lower()
    source_modified_at_ns = dataset.get("source_modified_at_ns")
    source_gate_fresh = (
        isinstance(source_modified_at_ns, int)
        and source_gate_path.is_file()
        and source_gate_path.stat().st_mtime_ns >= source_modified_at_ns
    )
    truth_passed = (
        dataset.get("status") == "verified"
        and source_gate.get("status") == "passed"
        and source_gate_mode == requested_mode
        and source_gate_fresh
    )
    warnings = list(dataset.get("warnings") or [])
    if source_gate.get("status") != "passed":
        warnings.append(f"{requested_mode.title()} retained source-bar truth is not passed.")
    elif source_gate_mode != requested_mode:
        warnings.append(
            f"{requested_mode.title()} retained source-bar truth names the wrong evidence lane."
        )
    elif not source_gate_fresh:
        warnings.append(f"{requested_mode.title()} retained source-bar truth gate is stale.")
    if unknown_rows:
        warnings.append("Unregistered strategy series are excluded from official fleet claims.")
    if impossible_forward_rows:
        warnings.append(
            "Pre-inception forward strategy rows are excluded from official summaries."
        )

    benchmark_by_date = {str(row["date"]): row for row in benchmark_rows}
    cash_by_date = {str(row["date"]): row for row in cash_rows}
    official_by_date: dict[str, list[JsonDict]] = defaultdict(list)
    for row in official_rows:
        official_by_date[str(row["date"])].append(row)

    day_summaries: list[JsonDict] = []
    for session_date in dates:
        day_rows = official_by_date.get(session_date, [])
        present_series = {str(row["series_key"]) for row in day_rows}
        registered_series = {
            series_key
            for series_key, metadata in registry_series.items()
            if session_date >= str(metadata["registry_inception_date"])
        }
        not_yet_registered = set(registry_series) - registered_series
        counterfactual_series = (
            {
                series_key
                for series_key in not_yet_registered
                if observed_inceptions.get(series_key, "9999-12-31") <= session_date
            }
            if requested_mode != "forward"
            else set()
        )
        expected_series = registered_series | counterfactual_series
        expected_rows = [
            row for row in day_rows if str(row["series_key"]) in expected_series
        ]
        present_expected = present_series & expected_series
        absent_series = expected_series - present_expected
        incomplete_series = {
            str(row["series_key"])
            for row in expected_rows
            if row.get("daily_return_pct") is None
        }
        missing_series = absent_series | incomplete_series
        coverage_complete = bool(expected_series) and not missing_series
        daily_pnl = _sum_required(row.get("total_pnl") for row in expected_rows)
        session_open = _sum_required(
            row.get("session_open_equity") for row in expected_rows
        )
        ending = _sum_required(row.get("ending_equity") for row in expected_rows)
        base = _sum_required(row.get("starting_equity") for row in expected_rows)
        official_claim = truth_passed and coverage_complete
        fleet_daily = _ratio(daily_pnl, session_open) if official_claim else None
        fleet_cumulative = (
            _ratio(_subtract(ending, base), base) if official_claim else None
        )
        benchmark = benchmark_by_date.get(session_date, {})
        benchmark_daily = benchmark.get("daily_return_pct") if truth_passed else None
        benchmark_cumulative = (
            benchmark.get("cumulative_return_pct") if truth_passed else None
        )
        excess_daily = _subtract(fleet_daily, benchmark_daily)
        values = [row.get("daily_return_pct") for row in expected_rows]
        day_status = _day_status(fleet_daily, expected_rows, official_claim)
        if truth_passed and not expected_series and not_yet_registered:
            day_status = "not_yet_registered"
        day_summaries.append(
            {
                "date": session_date,
                "fleet_daily_return": fleet_daily,
                "fleet_cumulative_return": fleet_cumulative,
                "fleet_daily_pnl": daily_pnl if official_claim else None,
                "fleet_ending_equity": ending if official_claim else None,
                "benchmark_daily_return": benchmark_daily,
                "benchmark_cumulative_return": benchmark_cumulative,
                "cash_daily_return": (
                    cash_by_date.get(session_date, {}).get("daily_return_pct")
                    if truth_passed
                    else None
                ),
                "excess_daily_return": excess_daily,
                "coverage_complete": coverage_complete,
                "coverage_present": len(present_expected),
                "coverage_expected": len(expected_series),
                "coverage_status": (
                    "complete"
                    if coverage_complete
                    else (
                        "not_yet_registered"
                        if not expected_series and not_yet_registered
                        else "missing"
                    )
                ),
                "missing_strategy_keys": sorted(missing_series),
                "not_yet_registered_strategy_keys": sorted(not_yet_registered),
                "not_yet_registered_strategies": len(not_yet_registered),
                "counterfactual_strategy_keys": sorted(counterfactual_series),
                "counterfactual_strategies": len(counterfactual_series),
                "claim_scope": (
                    "official_forward"
                    if requested_mode == "forward"
                    else "counterfactual_replay"
                ),
                "positive_strategies": sum(
                    1 for value in values if value is not None and value > 0
                ),
                "negative_strategies": sum(
                    1 for value in values if value is not None and value < 0
                ),
                "flat_strategies": sum(1 for value in values if value == 0),
                "missing_strategies": len(missing_series),
                "trades_opened": _sum_required_int(
                    row.get("trades_opened") for row in expected_rows
                ),
                "trades_closed": _sum_required_int(
                    row.get("trades_closed") for row in expected_rows
                ),
                "open_positions": _sum_required_int(
                    row.get("open_positions") for row in expected_rows
                ),
                "pending_orders": _sum_required_int(
                    row.get("pending_orders") for row in expected_rows
                ),
                "status": day_status,
            }
        )

    summaries = _strategy_summaries(
        [*official_rows, *challenger_rows, *benchmark_rows, *cash_rows], truth_passed
    )
    summaries = _with_missing_official_summaries(
        summaries,
        registry_series,
        latest_date=dates[-1] if dates else None,
    )
    blotter_verified = truth_passed and _blotter_verified_for_mode(dataset, requested_mode)
    blotter_rows = [
        dict(row)
        for row in dataset.get("blotter_rows") or []
        if blotter_verified and row.get("mode") == requested_mode
    ]
    return {
        "status": "verified" if truth_passed else "blocked",
        "mode": requested_mode,
        "truth_status": "verified" if truth_passed else "blocked",
        "dates": dates,
        "rows": mode_rows,
        "official_rows": official_rows,
        "challenger_rows": challenger_rows,
        "benchmark_rows": benchmark_rows,
        "cash_rows": cash_rows,
        "unknown_rows": unknown_rows,
        "impossible_forward_rows": impossible_forward_rows,
        "day_summaries": day_summaries,
        "strategy_summaries": summaries,
        "blotter_rows": blotter_rows,
        "blotter_verified": blotter_verified,
        "latest_date": dates[-1] if dates else None,
        "warnings": sorted(dict.fromkeys(warnings)),
        "claim_scope": (
            "official_forward"
            if requested_mode == "forward"
            else "counterfactual_replay"
        ),
        "research_only": True,
        "broker_execution_allowed": False,
    }


def _strategy_summaries(rows: list[JsonDict], truth_passed: bool) -> list[JsonDict]:
    grouped: dict[str, list[JsonDict]] = defaultdict(list)
    for row in rows:
        grouped[str(row["series_key"])].append(row)
    output: list[JsonDict] = []
    for series_key, series_rows in grouped.items():
        series_rows.sort(key=lambda row: str(row["date"]))
        latest = series_rows[-1]
        daily_values = [row.get("daily_return_pct") for row in series_rows]
        start = series_rows[0].get("starting_equity")
        end = latest.get("ending_equity")
        period_return = _ratio(_subtract(end, start), start) if truth_passed else None
        output.append(
            {
                "series_key": series_key,
                "series_role": latest.get("series_role"),
                "challenger_id": latest.get("challenger_id"),
                "strategy_id": latest.get("strategy_id"),
                "strategy_label": latest.get("strategy_label"),
                "strategy_version": latest.get("strategy_version"),
                "execution_policy_version": latest.get("execution_policy_version"),
                "strategy_semantics_fingerprint": latest.get(
                    "strategy_semantics_fingerprint"
                ),
                "registry_inception_date": latest.get("registry_inception_date"),
                "registration_status": latest.get("registration_status"),
                "evidence_scope": latest.get("evidence_scope"),
                "session_count": len(series_rows),
                "period_return": period_return,
                "latest_cumulative_return": (
                    latest.get("cumulative_return_pct") if truth_passed else None
                ),
                "ending_equity": end if truth_passed else None,
                "net_pnl": (
                    _sum_required(row.get("total_pnl") for row in series_rows)
                    if truth_passed
                    else None
                ),
                "max_drawdown": (
                    _min_required(row.get("drawdown_pct") for row in series_rows)
                    if truth_passed
                    else None
                ),
                "positive_days": sum(
                    1 for value in daily_values if value is not None and value > 0
                ),
                "negative_days": sum(
                    1 for value in daily_values if value is not None and value < 0
                ),
                "flat_days": sum(1 for value in daily_values if value == 0),
                "missing_days": sum(1 for value in daily_values if value is None),
                "trades_opened": _sum_required_int(
                    row.get("trades_opened") for row in series_rows
                ),
                "trades_closed": _sum_required_int(
                    row.get("trades_closed") for row in series_rows
                ),
                "open_positions": latest.get("open_positions"),
                "pending_orders": latest.get("pending_orders"),
                "latest_exposure": latest.get("exposure_pct"),
                "fees_paid": _sum_required(row.get("fees_paid") for row in series_rows),
                "slippage_estimate": _sum_required(
                    row.get("slippage_estimate") for row in series_rows
                ),
            }
        )
    output.sort(
        key=lambda row: (
            _role_order(str(row.get("series_role") or "")),
            str(row.get("strategy_label") or ""),
            str(row.get("series_key") or ""),
        )
    )
    return output


def _with_missing_official_summaries(
    summaries: list[JsonDict],
    registry_series: dict[str, JsonDict],
    *,
    latest_date: str | None,
) -> list[JsonDict]:
    """Represent registered series even when no exact row exists in the mode."""

    existing = {str(row.get("series_key") or "") for row in summaries}
    for series_key, metadata in registry_series.items():
        if series_key in existing:
            continue
        inception = str(metadata["registry_inception_date"])
        registration_status = (
            "not_yet_registered"
            if latest_date is None or latest_date < inception
            else "missing"
        )
        summaries.append(
            {
                "series_key": series_key,
                "series_role": "official",
                "challenger_id": None,
                "strategy_id": metadata["strategy_id"],
                "strategy_label": metadata["strategy_label"],
                "strategy_version": metadata["strategy_version"],
                "execution_policy_version": metadata["execution_policy_version"],
                "strategy_semantics_fingerprint": metadata[
                    "strategy_semantics_fingerprint"
                ],
                "registry_inception_date": inception,
                "registration_status": registration_status,
                "evidence_scope": None,
                "session_count": 0,
                "period_return": None,
                "latest_cumulative_return": None,
                "ending_equity": None,
                "net_pnl": None,
                "max_drawdown": None,
                "positive_days": 0,
                "negative_days": 0,
                "flat_days": 0,
                "missing_days": 0 if registration_status == "not_yet_registered" else 1,
                "trades_opened": None,
                "trades_closed": None,
                "open_positions": None,
                "pending_orders": None,
                "latest_exposure": None,
                "fees_paid": None,
                "slippage_estimate": None,
            }
        )
    summaries.sort(
        key=lambda row: (
            _role_order(str(row.get("series_role") or "")),
            str(row.get("strategy_label") or ""),
            str(row.get("series_key") or ""),
        )
    )
    return summaries


def _normalize_row(raw: JsonDict) -> JsonDict:
    row: JsonDict = {str(key): value for key, value in raw.items()}
    for field in _FLOAT_FIELDS:
        row[field] = _finite_decimal(row.get(field))
    for field in _INTEGER_FIELDS:
        row[field] = _finite_int(row.get(field))
    for field in _REQUIRED_FIELDS - _FLOAT_FIELDS:
        if field in _INTEGER_FIELDS:
            continue
        if not str(row.get(field) or "").strip():
            raise PaperOpsCalendarError(f"PaperOps calendar row is missing {field}.")
    for field in ("starting_equity", "ending_equity", "total_pnl"):
        if row.get(field) is None:
            raise PaperOpsCalendarError(f"PaperOps calendar row has invalid {field}.")
    _parse_session_date(row.get("date"), artifact="calendar row")
    return row


def _series_role(
    row: JsonDict,
    official_keys: set[tuple[str, str, str, str]],
    candidate_by_key: dict[tuple[str, str, str, str], str],
) -> tuple[str, str]:
    status = str(row.get("strategy_status") or "").lower()
    strategy_id = str(row.get("strategy_id") or "")
    if status == "benchmark" or strategy_id == "benchmark_buy_hold_equal_weight":
        return "benchmark", ""
    if status == "baseline" or strategy_id == "cash_no_trade_baseline":
        return "cash", ""
    key = _row_identity(row)
    if key in candidate_by_key:
        return "challenger", candidate_by_key[key]
    if key in official_keys:
        return "official", ""
    return "unregistered", ""


def _registry_identity(row: JsonDict) -> SeriesIdentity:
    return (
        str(row.get("strategy_id") or ""),
        str(row.get("strategy_version") or ""),
        str(row.get("execution_policy_version") or ""),
        str(row.get("strategy_semantics_fingerprint") or ""),
    )


def _candidate_identity(row: JsonDict) -> SeriesIdentity:
    return (
        str(row.get("strategy_id") or ""),
        str(row.get("candidate_strategy_version") or ""),
        str(row.get("execution_policy_version") or ""),
        str(row.get("candidate_strategy_semantics_fingerprint") or ""),
    )


def _row_identity(row: JsonDict) -> SeriesIdentity:
    return (
        str(row.get("strategy_id") or ""),
        str(row.get("strategy_version") or ""),
        str(row.get("execution_policy_version") or ""),
        str(row.get("strategy_semantics_fingerprint") or ""),
    )


def _official_registry_inceptions(
    root: Path,
    official_registry: list[JsonDict],
) -> dict[SeriesIdentity, JsonDict]:
    """Bind every active registry identity to immutable semantics and policy time."""

    semantics_manifest = _load_required_object(
        root / "state" / "strategy_semantics_manifest.json",
        artifact="strategy semantics manifest",
    )
    if semantics_manifest.get("schema_version") != "v2.strategy_semantics_manifest.v1":
        raise PaperOpsCalendarError(
            "PaperOps strategy semantics manifest schema is unsupported."
        )
    semantics = semantics_manifest.get("strategies")
    if not isinstance(semantics, dict):
        raise PaperOpsCalendarError(
            "PaperOps strategy semantics manifest has no strategies object."
        )

    policy_manifest = _load_required_object(
        root / "state" / "execution_policy_manifest.json",
        artifact="execution policy manifest",
    )
    if policy_manifest.get("schema_version") != "v2.paper_execution_policy_manifest.v1":
        raise PaperOpsCalendarError(
            "PaperOps execution policy manifest schema is unsupported."
        )
    policies = policy_manifest.get("policies")
    if not isinstance(policies, dict):
        raise PaperOpsCalendarError(
            "PaperOps execution policy manifest has no policies object."
        )

    output: dict[SeriesIdentity, JsonDict] = {}
    for registry_row in official_registry:
        identity = _registry_identity(registry_row)
        if any(not value for value in identity):
            raise PaperOpsCalendarError(
                "PaperOps official strategy registry has an incomplete exact identity."
            )
        if identity in output:
            raise PaperOpsCalendarError(
                "PaperOps official strategy registry has a duplicate exact identity: "
                + " | ".join(identity)
            )

        strategy_id, strategy_version, policy_version, fingerprint = identity
        semantics_key = f"{strategy_id}@{strategy_version}"
        semantics_entry = semantics.get(semantics_key)
        if not isinstance(semantics_entry, dict):
            raise PaperOpsCalendarError(
                "PaperOps registry inception is ambiguous; strategy semantics are "
                f"missing for {semantics_key}."
            )
        if str(semantics_entry.get("fingerprint") or "") != fingerprint:
            raise PaperOpsCalendarError(
                "PaperOps registry inception is ambiguous; strategy semantics "
                f"fingerprint does not match {semantics_key}."
            )
        configuration = semantics_entry.get("configuration")
        if not isinstance(configuration, dict) or (
            str(configuration.get("strategy_id") or "") != strategy_id
            or str(configuration.get("strategy_version") or "") != strategy_version
        ):
            raise PaperOpsCalendarError(
                "PaperOps registry inception is ambiguous; strategy semantics "
                f"configuration does not match {semantics_key}."
            )
        semantics_registered = _parse_registered_at(
            semantics_entry.get("registered_at"),
            artifact=f"strategy semantics {semantics_key}",
        )
        semantics_inception = _coverage_inception_date(
            semantics_entry,
            registered_at=semantics_registered,
            artifact=f"strategy semantics {semantics_key}",
        )

        policy_entry = policies.get(policy_version)
        if not isinstance(policy_entry, dict):
            raise PaperOpsCalendarError(
                "PaperOps registry inception is ambiguous; execution policy is "
                f"missing for {policy_version}."
            )
        if not isinstance(policy_entry.get("configuration"), dict):
            raise PaperOpsCalendarError(
                "PaperOps registry inception is ambiguous; execution policy "
                f"configuration is missing for {policy_version}."
            )
        policy_registered = _parse_registered_at(
            policy_entry.get("registered_at"),
            artifact=f"execution policy {policy_version}",
        )
        policy_inception = _coverage_inception_date(
            policy_entry,
            registered_at=policy_registered,
            artifact=f"execution policy {policy_version}",
        )
        inception = max(semantics_inception, policy_inception)
        output[identity] = {
            "registry_inception_date": inception.isoformat(),
            "strategy_semantics_registered_at": semantics_registered.isoformat(),
            "execution_policy_registered_at": policy_registered.isoformat(),
            "inception_lineage_status": "verified",
        }
    return output


def _official_series_for_mode(dataset: JsonDict, mode: str) -> dict[str, JsonDict]:
    raw_series = dataset.get("official_series")
    if raw_series is None and dataset.get("status") != "verified":
        return {}
    if not isinstance(raw_series, list):
        raise PaperOpsCalendarError(
            "PaperOps official registry inception metadata is unavailable."
        )

    output: dict[str, JsonDict] = {}
    for item in raw_series:
        if not isinstance(item, dict):
            raise PaperOpsCalendarError(
                "PaperOps official registry inception metadata is malformed."
            )
        identity = _registry_identity(item)
        if any(not value for value in identity):
            raise PaperOpsCalendarError(
                "PaperOps official registry inception metadata has an incomplete identity."
            )
        inception = str(item.get("registry_inception_date") or "")
        _parse_session_date(inception, artifact="official registry inception")
        series_key = "|".join((mode, *identity))
        if series_key in output:
            raise PaperOpsCalendarError(
                "PaperOps official registry inception metadata has a duplicate identity."
            )
        output[series_key] = dict(item)
    return output


def _parse_registered_at(value: object, *, artifact: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise PaperOpsCalendarError(f"PaperOps {artifact} has no registered_at.")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise PaperOpsCalendarError(
            f"PaperOps {artifact} registered_at is invalid."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperOpsCalendarError(
            f"PaperOps {artifact} registered_at must include a timezone."
        )
    return parsed.astimezone(timezone.utc)


def _coverage_inception_date(
    entry: JsonDict,
    *,
    registered_at: datetime,
    artifact: str,
) -> date:
    activation_policy = str(entry.get("activation_policy") or "").strip()
    try:
        expected = registration_coverage_inception_date(
            registered_at,
            activation_policy or _LEGACY_ACTIVATION_POLICY,
        )
    except ValueError as exc:
        raise PaperOpsCalendarError(
            f"PaperOps {artifact} activation_policy is unsupported."
        ) from exc
    raw = str(entry.get("coverage_inception_date") or "").strip()
    if not raw:
        return expected
    stored = _parse_session_date(raw, artifact=f"{artifact} coverage inception")
    if stored != expected:
        raise PaperOpsCalendarError(
            f"PaperOps {artifact} coverage inception conflicts with registered_at."
        )
    return stored


def _retained_market_session_span(observed_dates: list[str]) -> list[str]:
    """Expose completely absent forward sessions between retained endpoints."""

    try:
        lower = date.fromisoformat(observed_dates[0])
        upper = date.fromisoformat(observed_dates[-1])
        output: list[str] = []
        current = lower
        while current <= upper:
            if market_session(current).is_trading_day:
                output.append(current.isoformat())
            current += timedelta(days=1)
        return output
    except (ValueError, MarketCalendarCoverageError):
        # Loading already validates individual retained dates.  If the bounded
        # exchange calendar cannot enumerate their span, keep observed dates and
        # let the truth-gate warning block official claims rather than inventing.
        return observed_dates


def _parse_session_date(value: object, *, artifact: str) -> date:
    raw = str(value or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise PaperOpsCalendarError(f"PaperOps {artifact} date is invalid.") from exc


def _load_gates(root: Path, modes: list[str]) -> dict[str, JsonDict]:
    reconciliation = root / "reconciliation"
    paths = {
        "reconciliation": reconciliation / "reconciliation_latest.json",
        "calendar_truth": reconciliation / "calendar_truth_latest.json",
        "ledger_rebuild": reconciliation / "ledger_rebuild_latest.json",
        "trade_blotter": reconciliation / "trade_blotter_verify_latest.json",
    }
    for mode in modes:
        paths[f"source_bar_truth_{mode}"] = (
            reconciliation / f"source_bar_truth_{mode}_latest.json"
        )
    return {
        name: {**_load_object(path), "path": str(path)} for name, path in paths.items()
    }


def _blotter_verified_for_mode(dataset: JsonDict, mode: str) -> bool:
    gate = dict((dataset.get("gates") or {}).get("trade_blotter") or {})
    source = gate.get("source_bar_truth")
    source_mode = str(source.get("mode") or "") if isinstance(source, dict) else ""
    return gate.get("status") == "passed" and source_mode == mode


def _read_csv(path: Path) -> list[JsonDict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _load_object(path: Path) -> JsonDict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _load_required_object(path: Path, *, artifact: str) -> JsonDict:
    if not path.is_file():
        raise PaperOpsCalendarError(f"PaperOps {artifact} is missing.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaperOpsCalendarError(f"PaperOps {artifact} is unreadable.") from exc
    if not isinstance(payload, dict):
        raise PaperOpsCalendarError(f"PaperOps {artifact} must be an object.")
    return dict(payload)


def _load_list(path: Path) -> list[JsonDict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [dict(row) for row in payload if isinstance(row, dict)]


def _load_challengers(path: Path) -> list[JsonDict]:
    payload = _load_object(path)
    rows = payload.get("challengers")
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _finite_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _finite_int(value: object) -> int | None:
    number = _finite_decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _sum_required(values: Any) -> Decimal | None:
    items = list(values)
    if not items:
        return None
    total = Decimal(0)
    for value in items:
        number = _finite_decimal(value)
        if number is None:
            return None
        total += number
    return total


def _min_required(values: Any) -> Decimal | None:
    items = list(values)
    if not items:
        return None
    numbers: list[Decimal] = []
    for value in items:
        number = _finite_decimal(value)
        if number is None:
            return None
        numbers.append(number)
    return min(numbers)


def _sum_required_int(values: Any) -> int | None:
    items = list(values)
    if not items:
        return None
    numbers: list[int] = []
    for value in items:
        number = _finite_int(value)
        if number is None:
            return None
        numbers.append(number)
    return sum(numbers)


def _sum_int(values: Any) -> int:
    return sum(int(value) for value in values if value is not None)


def _subtract(left: object, right: object) -> Decimal | None:
    left_number = _finite_decimal(left)
    right_number = _finite_decimal(right)
    if left_number is None or right_number is None:
        return None
    return left_number - right_number


def _ratio(numerator: object, denominator: object) -> Decimal | None:
    top = _finite_decimal(numerator)
    bottom = _finite_decimal(denominator)
    if top is None or bottom is None or bottom == 0:
        return None
    return top / bottom


def _day_status(value: Decimal | None, rows: list[JsonDict], verified: bool) -> str:
    if not verified:
        return "unavailable"
    if value is None:
        return "missing"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    if any(
        _sum_int([row.get("trades_opened"), row.get("trades_closed")]) > 0
        or _finite_int(row.get("open_positions")) not in {None, 0}
        for row in rows
    ):
        return "flat_with_activity"
    return "flat"


def _role_order(role: str) -> int:
    return {"official": 0, "challenger": 1, "benchmark": 2, "cash": 3}.get(role, 9)
