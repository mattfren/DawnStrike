"""Point-in-time mover research and forward paper-observation workflow.

The lab deliberately separates descriptive end-of-day movers from prospective
intraday snapshots.  Only the latter may emit a paper signal.  All fills remain
simulated, use the next completed bar, include explicit costs, and are
same-session/EOD-flat.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import market_session
from intraday_scanner.v2.data import MarketBar, load_ohlcv_csv

from .calendar_report import write_strategy_calendar_report
from .candidate_study import (
    CandidateSplitAssignment,
    CandidateStudyAssumptions,
    CandidateUniverseDenominator,
    study_all_candidates,
)
from .contracts import (
    EVIDENCE_MODES,
    MoverPaperSignal,
    MoverStrategySpec,
    ProspectiveMoverSnapshot,
)
from .strategies import evaluate_snapshot, strategy_catalog
from .trade_truth import retained_trade_evidence_recomputes

SCHEMA_VERSION = "v2.mover_pattern_lab.v1"
DEFAULT_OUTPUT_ROOT = Path("data/v2_mover_pattern_lab")
MARKET_TZ = ZoneInfo("America/New_York")
RTH_START = time(9, 30)
RTH_END = time(16, 0)
OPENING_RANGE_END = time(9, 45)
DEFAULT_CUTOFFS = ("09:45", "10:00", "12:00", "15:00")
MIN_BASELINE_SESSIONS = 10
MIN_FORWARD_SESSIONS = 30
MIN_CLOSED_TRADES = 30
MIN_COVERAGE_PCT = 95.0
DEFAULT_NOTIONAL = 1_000.0
DEFAULT_SLIPPAGE_BPS = 10.0
DEFAULT_FEE_BPS = 1.0
MAX_CONTEXT_AGE = timedelta(minutes=5)
MAX_FORWARD_CAPTURE_DELAY = timedelta(minutes=5)
FORBIDDEN_SNAPSHOT_FIELDS = frozenset(
    {
        "close_return_pct",
        "daily_high",
        "daily_low",
        "eod_rank",
        "final_change_pct",
        "final_return_pct",
        "future_high",
        "future_low",
        "outcome",
        "outcome_return_pct",
    }
)
@dataclass(frozen=True)
class MoverLabPaths:
    root: Path
    manifests: Path
    snapshots: Path
    decisions: Path
    signals: Path
    trades: Path
    reports: Path
    audits: Path
    qa: Path
    source_artifacts: Path

    @classmethod
    def create(cls, root: Path) -> MoverLabPaths:
        paths = cls(
            root=root,
            manifests=root / "manifests",
            snapshots=root / "snapshots",
            decisions=root / "decisions",
            signals=root / "signals",
            trades=root / "trades",
            reports=root / "reports",
            audits=root / "audits",
            qa=root / "qa",
            source_artifacts=root / "source_artifacts",
        )
        for path in paths.__dict__.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths


def init(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, Any]:
    """Initialize immutable contracts and the frozen forward-paper catalog."""

    paths = MoverLabPaths.create(output_root)
    catalog = [spec.to_dict() for spec in strategy_catalog()]
    identities = [
        (str(row["strategy_id"]), str(row["version"]))
        for row in catalog
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("Mover Pattern Lab strategy identities must be unique")
    feature_contract: dict[str, Any] = {
        "schema_version": "v2.mover_feature_contract.v1",
        "dataset_roles": {
            "descriptive_eod_movers": {
                "paper_signal_eligible": False,
                "purpose": "Explain realized movers only after the session.",
            },
            "prospective_mover_snapshots": {
                "paper_signal_eligible": True,
                "purpose": "Freeze features knowable at feature_cutoff_at.",
            },
        },
        "cutoffs_et": list(DEFAULT_CUTOFFS),
        "bar_timestamp_semantics": "bar_close",
        "bar_interval_minutes": "declared by each build and signal",
        "required_prospective_provenance": [
            "universe_selected_at",
            "universe_source_ref",
            "universe_selection_method",
            "context_observed_at",
            "source_refs",
        ],
        "context_maximum_age_seconds": int(MAX_CONTEXT_AGE.total_seconds()),
        "exchange_calendar": "published fail-closed US equities calendar",
        "same_clock_rvol": (
            "current RTH cumulative volume through cutoff divided by the median "
            "cumulative volume through the same clock time over prior valid sessions"
        ),
        "forbidden_prospective_fields": sorted(FORBIDDEN_SNAPSHOT_FIELDS),
        "missing_truth_semantics": "missing values remain null and are never converted to zero",
        "paper_boundary": (
            "Research and simulated paper observation only. No broker connection, "
            "order placement, or live execution."
        ),
    }
    feature_contract["semantics_fingerprint"] = _json_fingerprint(
        feature_contract
    )
    _register_versioned_records(
        paths.manifests / "strategy_registry.jsonl",
        catalog,
        identity_fields=("strategy_id", "version"),
    )
    _register_versioned_records(
        paths.manifests / "feature_contract_registry.jsonl",
        [feature_contract],
        identity_fields=("schema_version",),
    )
    catalog_fingerprint = _json_fingerprint(catalog)
    catalog_path = (
        paths.manifests
        / f"strategy_catalog_{catalog_fingerprint[:16]}.json"
    )
    contract_path = (
        paths.manifests
        / "feature_contract_"
        f"{feature_contract['semantics_fingerprint'][:16]}.json"
    )
    _write_immutable_json(catalog_path, catalog)
    _write_immutable_json(contract_path, feature_contract)
    _write_json(
        paths.manifests / "catalog_latest.json",
        {
            "catalog_path": catalog_path.as_posix(),
            "catalog_fingerprint": catalog_fingerprint,
            "feature_contract_path": contract_path.as_posix(),
            "feature_contract_fingerprint": feature_contract[
                "semantics_fingerprint"
            ],
        },
    )
    payload = {
        "schema_version": f"{SCHEMA_VERSION}.init",
        "status": "initialized",
        "strategy_count": len(catalog),
        "strategy_ids": [row["strategy_id"] for row in catalog],
        "catalog_path": catalog_path.as_posix(),
        "catalog_fingerprint": catalog_fingerprint,
        "feature_contract_path": contract_path.as_posix(),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    _write_json(paths.manifests / "init_latest.json", payload)
    return payload


def build_snapshots_from_bars(
    *,
    bars_csv: Path,
    context_csv: Path | None = None,
    market_date: str | None = None,
    cutoffs: Iterable[str] = DEFAULT_CUTOFFS,
    min_baseline_sessions: int = MIN_BASELINE_SESSIONS,
    bar_interval_minutes: int = 5,
    bar_timestamp_semantics: str,
    evidence_mode: str = "historical_replay",
    source_captured_at: datetime | None = None,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Build cutoff-safe mover snapshots from bar-close timestamped intraday data."""

    if min_baseline_sessions < 1:
        raise ValueError("min_baseline_sessions must be positive")
    if bar_interval_minutes < 1 or bar_interval_minutes > 30:
        raise ValueError("bar_interval_minutes must be between 1 and 30")
    if bar_timestamp_semantics != "bar_close":
        raise ValueError("mover lab currently requires bar_timestamp_semantics='bar_close'")
    if evidence_mode not in EVIDENCE_MODES:
        allowed_modes = ", ".join(sorted(EVIDENCE_MODES))
        raise ValueError(f"evidence_mode must be one of: {allowed_modes}")
    cutoff_times = tuple(_parse_clock(value) for value in cutoffs)
    if not cutoff_times:
        raise ValueError("at least one feature cutoff is required")
    if source_captured_at is not None:
        source_captured_at = _aware_datetime(source_captured_at)
    system_received_at: datetime | None = None
    declared_source_captured_at = source_captured_at
    if evidence_mode == "forward_observation":
        if market_date is None:
            raise ValueError("forward_observation requires one explicit market_date")
        if len(cutoff_times) != 1:
            raise ValueError("forward_observation requires exactly one feature cutoff")
        system_received_at = _utc_now()
        target_day = date.fromisoformat(market_date)
        cutoff_at = datetime.combine(target_day, cutoff_times[0], tzinfo=MARKET_TZ)
        received_local = system_received_at.astimezone(MARKET_TZ)
        if received_local.date() != target_day:
            raise ValueError(
                "forward_observation market_date must equal the system receipt date"
            )
        if not cutoff_at <= system_received_at <= cutoff_at + MAX_FORWARD_CAPTURE_DELAY:
            raise ValueError(
                "forward_observation must be system-received between cutoff and cutoff+5m"
            )
        if (
            declared_source_captured_at is not None
            and declared_source_captured_at > system_received_at
        ):
            raise ValueError("declared source capture cannot be after system receipt")
        source_captured_at = system_received_at
    _validate_csv_timestamp_awareness(bars_csv)
    init(output_root=output_root)
    paths = MoverLabPaths.create(output_root)
    dataset = load_ohlcv_csv(
        bars_csv,
        dataset_id=f"mover_pattern_lab:{_sha256_file(bars_csv)[:16]}",
        source_kind="operator_intraday_csv",
        timeframe="intraday",
    )
    if dataset.warnings:
        raise ValueError(
            "bars CSV contains rejected rows: " + "; ".join(dataset.warnings)
        )
    _validate_market_bars(dataset.bars_by_symbol)
    contexts = _read_context_rows(context_csv)
    forward_receipt_ref = ""
    forward_receipt_path: Path | None = None
    if evidence_mode == "forward_observation":
        if system_received_at is None or market_date is None:
            raise AssertionError("forward system receipt was not initialized")
        forward_cutoff = datetime.combine(
            date.fromisoformat(market_date),
            cutoff_times[0],
            tzinfo=MARKET_TZ,
        )
        input_bar_times = [
            bar.timestamp.astimezone(MARKET_TZ)
            for symbol_bars in dataset.bars_by_symbol.values()
            for bar in symbol_bars
        ]
        if any(timestamp > forward_cutoff for timestamp in input_bar_times):
            raise ValueError(
                "forward bars input contains observations after the feature cutoff"
            )
        for context_rows in contexts.values():
            for context_row in context_rows:
                if (
                    _aware_datetime(context_row.get("context_observed_at"))
                    > forward_cutoff
                ):
                    raise ValueError(
                        "forward context input contains observations after the feature cutoff"
                    )
        bars_input_sha256 = _sha256_file(bars_csv)
        context_input_sha256 = (
            _sha256_file(context_csv) if context_csv is not None else None
        )
        receipt = {
            "schema_version": "v2.mover_forward_source_receipt.v1",
            "evidence_mode": "forward_observation",
            "market_date": market_date,
            "feature_cutoffs_at": [forward_cutoff.isoformat()],
            "system_received_at": system_received_at.isoformat(),
            "authoritative_source_captured_at": system_received_at.isoformat(),
            "declared_source_captured_at": (
                declared_source_captured_at.isoformat()
                if declared_source_captured_at is not None
                else None
            ),
            "bars_input_path": str(bars_csv.resolve()),
            "bars_input_sha256": bars_input_sha256,
            "context_input_path": (
                str(context_csv.resolve()) if context_csv is not None else None
            ),
            "context_input_sha256": context_input_sha256,
            "latest_input_bar_at": (
                max(input_bar_times).isoformat() if input_bar_times else None
            ),
            "receipt_clock": "dawnstrike_process_utc",
            "research_only": True,
            "broker_execution_enabled": False,
        }
        receipt_sha256 = _json_fingerprint(receipt)
        forward_receipt_path = (
            paths.source_artifacts
            / "forward_receipts"
            / f"{receipt_sha256}.json"
        )
        _write_immutable_json(forward_receipt_path, receipt)
        forward_receipt_ref = (
            f"sha256:{receipt_sha256}:{forward_receipt_path.resolve()}"
        )
    snapshots: list[ProspectiveMoverSnapshot] = []
    rejected: list[dict[str, Any]] = []

    for symbol in dataset.symbols:
        bars_by_day = _rth_bars_by_day(dataset.bars_by_symbol[symbol])
        session_days = sorted(bars_by_day)
        for session_day in session_days:
            if market_date and session_day.isoformat() != market_date:
                continue
            session = market_session(session_day)
            if not session.is_trading_day or session.close_time_et is None:
                rejected.append(
                    {
                        "symbol": symbol,
                        "market_date": session_day.isoformat(),
                        "reason": "not_a_published_trading_session",
                        "calendar_reason": session.reason,
                    }
                )
                continue
            session_close = time.fromisoformat(session.close_time_et)
            prior_days = [
                day
                for day in session_days
                if day < session_day
                and market_session(day).is_trading_day
                and _session_bars_complete(bars_by_day[day], day)
            ]
            expected_prior_day = _previous_market_session(session_day)
            previous_close = (
                bars_by_day[expected_prior_day][-1].close
                if expected_prior_day in bars_by_day
                and _session_bars_complete(
                    bars_by_day[expected_prior_day],
                    expected_prior_day,
                )
                else None
            )
            for cutoff_clock in cutoff_times:
                if cutoff_clock > session_close:
                    rejected.append(
                        {
                            "symbol": symbol,
                            "market_date": session_day.isoformat(),
                            "cutoff_at": cutoff_clock.isoformat(),
                            "reason": "cutoff_after_published_session_close",
                        }
                    )
                    continue
                cutoff_at = datetime.combine(session_day, cutoff_clock, tzinfo=MARKET_TZ)
                available = [
                    bar
                    for bar in bars_by_day[session_day]
                    if bar.timestamp.astimezone(MARKET_TZ) <= cutoff_at
                ]
                if not available:
                    rejected.append(
                        {
                            "symbol": symbol,
                            "market_date": session_day.isoformat(),
                            "cutoff_at": cutoff_at.isoformat(),
                            "reason": "no_completed_bar_at_or_before_cutoff",
                        }
                    )
                    continue
                if available[-1].timestamp.astimezone(MARKET_TZ) != cutoff_at:
                    rejected.append(
                        {
                            "symbol": symbol,
                            "market_date": session_day.isoformat(),
                            "cutoff_at": cutoff_at.isoformat(),
                            "reason": "no_bar_closing_exactly_at_cutoff",
                            "latest_observed_at": available[-1].timestamp.isoformat(),
                        }
                    )
                    continue
                if not _bar_grid_complete_through(
                    bars_by_day[session_day],
                    cutoff_clock,
                    interval_minutes=bar_interval_minutes,
                ):
                    rejected.append(
                        {
                            "symbol": symbol,
                            "market_date": session_day.isoformat(),
                            "cutoff_at": cutoff_at.isoformat(),
                            "reason": "incomplete_bar_grid_through_cutoff",
                            "bar_interval_minutes": bar_interval_minutes,
                        }
                    )
                    continue
                opening = [
                    bar
                    for bar in available
                    if RTH_START
                    <= bar.timestamp.astimezone(MARKET_TZ).time()
                    <= OPENING_RANGE_END
                ]
                opening_complete = _opening_range_complete(
                    available,
                    cutoff_at=cutoff_at,
                    interval_minutes=bar_interval_minutes,
                )
                prior_same_clock_volumes = [
                    sum(
                        bar.volume
                        for bar in bars_by_day[prior_day]
                        if bar.timestamp.astimezone(MARKET_TZ).time() <= cutoff_clock
                    )
                    for prior_day in prior_days
                    if _bar_grid_complete_through(
                        bars_by_day[prior_day],
                        cutoff_clock,
                        interval_minutes=bar_interval_minutes,
                    )
                ]
                prior_same_clock_volumes = [
                    value for value in prior_same_clock_volumes if value > 0
                ][-20:]
                cumulative_volume = sum(bar.volume for bar in available)
                same_clock_rvol = (
                    cumulative_volume / float(median(prior_same_clock_volumes))
                    if len(prior_same_clock_volumes) >= min_baseline_sessions
                    and median(prior_same_clock_volumes) > 0
                    else None
                )
                context = _context_at_cutoff(
                    contexts.get((session_day.isoformat(), symbol), ()),
                    cutoff_at,
                )
                if context is None:
                    rejected.append(
                        {
                            "symbol": symbol,
                            "market_date": session_day.isoformat(),
                            "cutoff_at": cutoff_at.isoformat(),
                            "reason": "missing_prospective_context_at_cutoff",
                        }
                    )
                    continue
                context_observed_at = _aware_datetime(
                    context.get("context_observed_at")
                )
                context_age = cutoff_at - context_observed_at.astimezone(MARKET_TZ)
                if context_age < timedelta(0) or context_age > MAX_CONTEXT_AGE:
                    rejected.append(
                        {
                            "symbol": symbol,
                            "market_date": session_day.isoformat(),
                            "cutoff_at": cutoff_at.isoformat(),
                            "reason": "prospective_context_stale_or_future",
                            "context_observed_at": context_observed_at.isoformat(),
                            "maximum_age_seconds": int(
                                MAX_CONTEXT_AGE.total_seconds()
                            ),
                        }
                    )
                    continue
                bar_payload = [_market_bar_payload(bar) for bar in available]
                bar_prefix_sha256 = _json_fingerprint(bar_payload)
                bar_artifact = (
                    paths.source_artifacts
                    / "bars"
                    / f"{bar_prefix_sha256}.json"
                )
                _write_immutable_json(bar_artifact, bar_payload)
                context_payload = {
                    **context,
                    "context_input_path": (
                        str(context_csv.resolve()) if context_csv else None
                    ),
                }
                context_sha256 = _json_fingerprint(context_payload)
                context_artifact = (
                    paths.source_artifacts
                    / "context"
                    / f"{context_sha256}.json"
                )
                _write_immutable_json(context_artifact, context_payload)
                snapshot_identity_parts = [
                    symbol,
                    session_day.isoformat(),
                    cutoff_at.isoformat(),
                    bar_prefix_sha256,
                    context_sha256,
                    evidence_mode,
                    (
                        source_captured_at.isoformat()
                        if source_captured_at is not None
                        else "unknown_capture_time"
                    ),
                ]
                if evidence_mode == "forward_observation":
                    snapshot_identity_parts.extend(
                        [
                            (
                                system_received_at.isoformat()
                                if system_received_at is not None
                                else "no_system_receipt"
                            ),
                            forward_receipt_ref or "no_forward_receipt",
                        ]
                    )
                raw: dict[str, Any] = {
                    "snapshot_id": _stable_id(
                        "mover_snapshot",
                        *snapshot_identity_parts,
                    ),
                    "market_date": session_day.isoformat(),
                    "symbol": symbol,
                    "observed_at": available[-1].timestamp.isoformat(),
                    "feature_cutoff_at": cutoff_at.isoformat(),
                    "universe_selected_at": context.get("universe_selected_at"),
                    "universe_source_ref": context.get("universe_source_ref"),
                    "universe_selection_method": context.get(
                        "universe_selection_method"
                    ),
                    "context_observed_at": context_observed_at.isoformat(),
                    "evidence_mode": evidence_mode,
                    "source_captured_at": (
                        source_captured_at.isoformat()
                        if source_captured_at is not None
                        else None
                    ),
                    "system_received_at": (
                        system_received_at.isoformat()
                        if system_received_at is not None
                        else None
                    ),
                    "forward_receipt_ref": forward_receipt_ref or None,
                    "price": available[-1].close,
                    "previous_close": previous_close,
                    "session_open": available[0].open,
                    "opening_range_high": max((bar.high for bar in opening), default=None),
                    "opening_range_low": min((bar.low for bar in opening), default=None),
                    "opening_range_complete": opening_complete,
                    "running_vwap": _running_vwap(available),
                    "cumulative_volume": cumulative_volume,
                    "cumulative_dollar_volume": round(
                        sum(_typical_price(bar) * bar.volume for bar in available),
                        6,
                    ),
                    "same_clock_rvol": same_clock_rvol,
                    "spread_pct": _optional_float(context.get("spread_pct")),
                    "split_adjusted": _optional_bool(context.get("split_adjusted")),
                    "reverse_split_days": _optional_int(context.get("reverse_split_days")),
                    "reverse_split_lookback_clear": _optional_bool(
                        context.get("reverse_split_lookback_clear")
                    ),
                    "recent_offering_days": _optional_int(
                        context.get("recent_offering_days")
                    ),
                    "offering_lookback_clear": _optional_bool(
                        context.get("offering_lookback_clear")
                    ),
                    "halt_state": str(context.get("halt_state") or "unknown"),
                    "source_conflict": _optional_bool(context.get("source_conflict")),
                    "catalyst_verified": _optional_bool(
                        context.get("catalyst_verified")
                    ),
                    "catalyst_published_at": (
                        str(context.get("catalyst_published_at") or "") or None
                    ),
                    "catalyst_source_url": str(
                        context.get("catalyst_source_url") or ""
                    ),
                    "catalyst_source_type": str(
                        context.get("catalyst_source_type") or ""
                    ),
                    "catalyst_artifact_ref": str(
                        context.get("catalyst_artifact_ref") or ""
                    ),
                    "source_refs": [
                        (
                            f"sha256:{bar_prefix_sha256}:"
                            f"{bar_artifact.resolve()}"
                        ),
                        (
                            f"sha256:{context_sha256}:"
                            f"{context_artifact.resolve()}"
                        ),
                        str(context.get("universe_source_ref") or ""),
                        str(context.get("catalyst_artifact_ref") or ""),
                        forward_receipt_ref,
                        *[
                            item.strip()
                            for item in str(context.get("source_refs") or "").split(";")
                            if item.strip()
                        ],
                    ],
                    "raw_payload": {
                        "bar_timestamp_semantics": "bar_close",
                        "bar_interval_minutes": bar_interval_minutes,
                        "bar_prefix_sha256": bar_prefix_sha256,
                        "bar_artifact": str(bar_artifact.resolve()),
                        "context_row_sha256": context_sha256,
                        "context_artifact": str(context_artifact.resolve()),
                        "same_clock_baseline_session_count": len(
                            prior_same_clock_volumes
                        ),
                        "same_clock_baseline_dates": [
                            day.isoformat()
                            for day in prior_days
                            if _bar_grid_complete_through(
                                bars_by_day[day],
                                cutoff_clock,
                                interval_minutes=bar_interval_minutes,
                            )
                        ][-20:],
                        "calendar_id": session.calendar_id,
                        "calendar_published_as_of": (
                            session.calendar_published_as_of
                        ),
                        "expected_previous_market_session": (
                            expected_prior_day.isoformat()
                        ),
                        "previous_close_market_date": (
                            expected_prior_day.isoformat()
                            if previous_close is not None
                            else None
                        ),
                    },
                }
                if evidence_mode == "forward_observation":
                    raw["raw_payload"].update(
                        {
                            "input_bars_sha256": _sha256_file(bars_csv),
                            "input_context_sha256": (
                                _sha256_file(context_csv)
                                if context_csv is not None
                                else None
                            ),
                            "system_received_at": (
                                system_received_at.isoformat()
                                if system_received_at is not None
                                else None
                            ),
                            "forward_receipt_ref": forward_receipt_ref,
                        }
                    )
                try:
                    snapshot = ProspectiveMoverSnapshot.from_mapping(raw)
                    if not _forward_universe_artifact_valid(snapshot.to_dict()):
                        raise ValueError(
                            "forward candidate universe artifact is invalid or late"
                        )
                    snapshots.append(snapshot)
                except (TypeError, ValueError) as exc:
                    rejected.append(
                        {
                            "symbol": symbol,
                            "market_date": session_day.isoformat(),
                            "cutoff_at": cutoff_at.isoformat(),
                            "reason": "snapshot_contract_rejected",
                            "detail": str(exc),
                        }
                    )

    snapshots.sort(
        key=lambda row: (row.market_date, row.symbol, row.feature_cutoff_at)
    )
    snapshot_rows = [row.to_dict() for row in snapshots]
    for row in snapshot_rows:
        _write_immutable_json(
            paths.snapshots / "by_id" / f"{row['snapshot_id']}.json",
            row,
        )
    run_fingerprint = _json_fingerprint(snapshot_rows)
    date_token = market_date or "all"
    output_path = (
        paths.snapshots
        / f"prospective_{date_token}_{run_fingerprint[:16]}.jsonl"
    )
    _write_immutable_jsonl(output_path, snapshot_rows)
    rejected_fingerprint = _json_fingerprint(rejected)
    rejected_path = (
        paths.snapshots
        / f"rejected_{date_token}_{rejected_fingerprint[:16]}.json"
    )
    _write_immutable_json(rejected_path, rejected)
    payload = {
        "schema_version": f"{SCHEMA_VERSION}.snapshot_build",
        "status": "passed" if snapshots else "blocked",
        "snapshot_count": len(snapshots),
        "rejected_count": len(rejected),
        "symbol_count": len({row.symbol for row in snapshots}),
        "session_count": len({row.market_date for row in snapshots}),
        "snapshot_path": str(output_path.resolve()),
        "rejected_path": str(rejected_path.resolve()),
        "run_fingerprint": run_fingerprint,
        "bar_timestamp_semantics": "bar_close",
        "bar_interval_minutes": bar_interval_minutes,
        "min_baseline_sessions": min_baseline_sessions,
        "evidence_mode": evidence_mode,
        "source_captured_at": (
            source_captured_at.isoformat()
            if source_captured_at is not None
            else None
        ),
        "system_received_at": (
            system_received_at.isoformat()
            if system_received_at is not None
            else None
        ),
        "forward_receipt_ref": forward_receipt_ref or None,
        "forward_receipt_path": (
            str(forward_receipt_path.resolve())
            if forward_receipt_path is not None
            else None
        ),
        "forward_evidence_eligible": (
            evidence_mode == "forward_observation" and bool(forward_receipt_ref)
        ),
        "warnings": list(dataset.warnings),
    }
    _write_json(paths.manifests / "snapshot_build_latest.json", payload)
    return payload


def paper_scan(
    *,
    snapshots_path: Path,
    expected_market_dates: Iterable[str],
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Apply frozen rules to prospective snapshots and retain every decision."""

    init(output_root=output_root)
    paths = MoverLabPaths.create(output_root)
    snapshots = []
    for row in _read_jsonl(snapshots_path):
        snapshot = ProspectiveMoverSnapshot.from_mapping(row)
        artifact_path = (
            paths.snapshots / "by_id" / f"{snapshot.snapshot_id}.json"
        )
        if not artifact_path.exists():
            raise ValueError(
                "paper scan requires a retained snapshot/by_id artifact: "
                f"{snapshot.snapshot_id}"
            )
        retained = json.loads(artifact_path.read_text(encoding="utf-8"))
        if retained != snapshot.to_dict():
            raise ValueError("snapshot input does not match its retained artifact")
        if not _snapshot_identity_valid(retained):
            raise ValueError("snapshot immutable identity does not match its content")
        if not _source_artifact_refs_valid([retained]):
            raise ValueError("snapshot source artifacts are missing or hash-invalid")
        if not _forward_receipt_valid(retained):
            raise ValueError("snapshot forward receipt is missing or inconsistent")
        if not _forward_universe_artifact_valid(retained):
            raise ValueError(
                "snapshot forward candidate universe is missing or inconsistent"
            )
        snapshots.append(snapshot)
    snapshots_source_ref, retained_snapshots_path = _retain_raw_input(
        snapshots_path,
        paths.source_artifacts / "paper_scan" / "snapshots",
    )
    snapshot_ids = [row.snapshot_id for row in snapshots]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        raise ValueError("snapshots input contains duplicate snapshot_id values")
    expected_dates = tuple(sorted(set(str(value) for value in expected_market_dates)))
    if not expected_dates:
        raise ValueError("paper scan requires at least one expected market date")
    for market_date in expected_dates:
        session = market_session(date.fromisoformat(market_date))
        if not session.is_trading_day:
            raise ValueError(f"expected date is not a published trading session: {market_date}")
    snapshot_dates = {row.market_date for row in snapshots}
    if not snapshot_dates.issubset(set(expected_dates)):
        raise ValueError("snapshot dates must be included in expected_market_dates")
    decisions: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    for snapshot in sorted(
        snapshots,
        key=lambda row: (row.market_date, row.symbol, row.feature_cutoff_at),
    ):
        for spec in strategy_catalog():
            decision = evaluate_snapshot(spec, snapshot)
            decision_payload = decision.to_dict()
            signal = getattr(decision, "signal", None)
            if signal is not None:
                signal_payload = signal.to_dict()
                claimed, existing_signal_id, registry_path = _claim_session_signal(
                    paths,
                    signal_payload,
                )
                if not claimed:
                    decision_payload["decision"] = "skipped"
                    decision_payload["reason"] = (
                        "already_signaled_this_strategy_symbol_session"
                    )
                    decision_payload["signal"] = None
                    decision_payload["session_signal_registry_path"] = str(
                        registry_path.resolve()
                    )
                    decision_payload["suppressed_by_signal_id"] = existing_signal_id
                else:
                    signals.append(signal_payload)
            decisions.append(decision_payload)

    for row in decisions:
        _write_immutable_json(
            paths.decisions / "by_id" / f"{row['decision_id']}.json",
            row,
        )
    for row in signals:
        _write_immutable_json(
            paths.signals / "by_id" / f"{row['signal_id']}.json",
            row,
        )
    decision_fingerprint = _json_fingerprint(decisions)
    signal_fingerprint = _json_fingerprint(signals)
    decisions_path = (
        paths.decisions / f"decisions_{decision_fingerprint[:16]}.jsonl"
    )
    signals_path = paths.signals / f"signals_{signal_fingerprint[:16]}.jsonl"
    _write_immutable_jsonl(decisions_path, decisions)
    _write_immutable_jsonl(signals_path, signals)
    not_evaluated_dates = sorted(set(expected_dates) - snapshot_dates)
    payload = {
        "schema_version": f"{SCHEMA_VERSION}.paper_scan",
        "status": (
            "passed_with_not_evaluated"
            if not_evaluated_dates
            else "passed"
        ),
        "snapshot_count": len(snapshots),
        "decision_count": len(decisions),
        "signal_count": len(signals),
        "no_signal_count": len(decisions) - len(signals),
        "decisions_path": str(decisions_path.resolve()),
        "signals_path": str(signals_path.resolve()),
        "decision_fingerprint": decision_fingerprint,
        "signal_fingerprint": signal_fingerprint,
        "strategy_count": len(strategy_catalog()),
        "expected_market_dates": list(expected_dates),
        "not_evaluated_market_dates": not_evaluated_dates,
        "snapshots_path": str(retained_snapshots_path),
        "snapshots_sha256": _sha256_file(retained_snapshots_path),
        "snapshots_source_ref": snapshots_source_ref,
        "original_snapshots_path": str(snapshots_path.resolve()),
        "decisions_sha256": _sha256_file(decisions_path),
        "signals_sha256": _sha256_file(signals_path),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    run_fingerprint = _json_fingerprint(payload)
    run_manifest_path = paths.manifests / f"paper_scan_{run_fingerprint[:16]}.json"
    run_payload = {**payload, "run_fingerprint": run_fingerprint}
    _write_immutable_json(run_manifest_path, run_payload)
    payload = {
        **run_payload,
        "run_manifest_path": str(run_manifest_path.resolve()),
    }
    _write_json(paths.manifests / "paper_scan_latest.json", payload)
    return payload


def reconcile_paper_signals(
    *,
    signals_path: Path,
    bars_csv: Path,
    notional_per_trade: float = DEFAULT_NOTIONAL,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    fee_bps: float = DEFAULT_FEE_BPS,
    bar_interval_minutes: int = 5,
    bar_timestamp_semantics: str,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Reconcile forward paper signals with the next and subsequent source bars."""

    if notional_per_trade <= 0:
        raise ValueError("notional_per_trade must be positive")
    if min(slippage_bps, fee_bps) < 0:
        raise ValueError("cost assumptions must be non-negative")
    if bar_interval_minutes < 1 or bar_interval_minutes > 30:
        raise ValueError("bar_interval_minutes must be between 1 and 30")
    if bar_timestamp_semantics != "bar_close":
        raise ValueError("mover lab currently requires bar_timestamp_semantics='bar_close'")
    _validate_csv_timestamp_awareness(bars_csv)
    init(output_root=output_root)
    paths = MoverLabPaths.create(output_root)
    dataset = load_ohlcv_csv(
        bars_csv,
        dataset_id=f"mover_reconcile:{_sha256_file(bars_csv)[:16]}",
        source_kind="operator_intraday_csv",
        timeframe="intraday",
    )
    if dataset.warnings:
        raise ValueError(
            "bars CSV contains rejected rows: " + "; ".join(dataset.warnings)
        )
    if dataset.bars_by_symbol:
        _validate_market_bars(dataset.bars_by_symbol)
    signal_input_rows = _read_jsonl(signals_path)
    signal_contracts = [
        MoverPaperSignal.from_mapping(row) for row in signal_input_rows
    ]
    signal_ids = [row.signal_id for row in signal_contracts]
    if len(signal_ids) != len(set(signal_ids)):
        raise ValueError("signals input contains duplicate signal_id values")
    catalog = {
        (spec.strategy_id, spec.version): spec.to_dict()
        for spec in strategy_catalog()
    }
    for signal in signal_contracts:
        strategy = catalog.get((signal.strategy_id, signal.strategy_version))
        if strategy is None:
            raise ValueError(
                "signal references unknown strategy identity: "
                f"{signal.strategy_id}@{signal.strategy_version}"
            )
        if signal.strategy_semantics_fingerprint != str(
            strategy["semantics_fingerprint"]
        ):
            raise ValueError("signal strategy semantics fingerprint mismatch")
        expected_id = _stable_id(
            "mover_paper_signal",
            signal.strategy_id,
            signal.strategy_version,
            signal.snapshot_id,
        )
        if signal.signal_id != expected_id:
            raise ValueError("signal_id does not match its immutable identity")
        declared_interval = _optional_int(
            signal.features.get("bar_interval_minutes")
        )
        if declared_interval is not None and declared_interval != bar_interval_minutes:
            raise ValueError(
                "signal bar interval does not match reconciliation interval"
            )
    if not _rows_match_by_id_artifacts(
        signal_input_rows,
        paths.signals / "by_id",
        "signal_id",
    ):
        raise ValueError(
            "reconciliation requires the exact retained paper-scan signal ledger"
        )
    signal_fingerprint = _json_fingerprint(signal_input_rows)
    retained_signals_path = (
        paths.signals / f"signals_{signal_fingerprint[:16]}.jsonl"
    )
    if (
        not retained_signals_path.is_file()
        or _read_jsonl(retained_signals_path) != signal_input_rows
    ):
        raise ValueError(
            "reconciliation requires the canonical retained paper-scan ledger"
        )
    bars_source_ref, retained_bars_path = _retain_raw_input(
        bars_csv,
        paths.source_artifacts / "reconcile" / "bars",
    )
    snapshot_rows = _read_json_objects(paths.snapshots / "by_id")
    if not _signals_reference_snapshots(signal_input_rows, snapshot_rows):
        raise ValueError(
            "reconciliation signals lack validated retained snapshot lineage"
        )
    referenced_snapshot_ids = {
        str(row.get("snapshot_id") or "") for row in signal_input_rows
    }
    referenced_snapshots = [
        row
        for row in snapshot_rows
        if str(row.get("snapshot_id") or "") in referenced_snapshot_ids
    ]
    if not _source_artifact_refs_valid(referenced_snapshots):
        raise ValueError(
            "reconciliation snapshot source artifacts are missing or hash-invalid"
        )
    if not all(_forward_receipt_valid(row) for row in referenced_snapshots):
        raise ValueError(
            "reconciliation snapshot forward receipt is missing or inconsistent"
        )
    if not all(
        _forward_universe_artifact_valid(row) for row in referenced_snapshots
    ):
        raise ValueError(
            "reconciliation snapshot candidate universe is missing or inconsistent"
        )
    session_signal_rows = _read_json_objects(
        paths.signals / "session_registry"
    )
    relevant_signal_ids = set(signal_ids)
    relevant_registry_rows = [
        row
        for row in session_signal_rows
        if str(row.get("signal_id") or "") in relevant_signal_ids
    ]
    if not _session_signal_registry_valid(
        relevant_registry_rows,
        signal_input_rows,
    ):
        raise ValueError(
            "reconciliation signals do not match the immutable session registry"
        )
    snapshot_by_id = {
        str(row.get("snapshot_id") or ""): row for row in referenced_snapshots
    }
    signals: list[dict[str, Any]] = []
    for contract in signal_contracts:
        signal_row = contract.to_dict()
        signal_artifact_path = (
            paths.signals / "by_id" / f"{contract.signal_id}.json"
        )
        snapshot_artifact_path = (
            paths.snapshots / "by_id" / f"{contract.snapshot_id}.json"
        )
        snapshot_payload = snapshot_by_id.get(contract.snapshot_id)
        if snapshot_payload is None:
            raise ValueError("signal snapshot artifact is unavailable")
        signal_artifact_ref = _retained_json_artifact_ref(signal_artifact_path)
        snapshot_artifact_ref = _retained_json_artifact_ref(snapshot_artifact_path)
        signal_row["signal_artifact_ref"] = signal_artifact_ref
        signal_row["snapshot_artifact_ref"] = snapshot_artifact_ref
        signal_row["source_refs"] = list(
            dict.fromkeys(
                [
                    *[
                        str(item)
                        for item in signal_row.get("source_refs") or []
                    ],
                    signal_artifact_ref,
                    snapshot_artifact_ref,
                ]
            )
        )
        signals.append(signal_row)
    trades = [
        _reconcile_one(
            signal,
            dataset.bars_by_symbol.get(str(signal.get("symbol") or "").upper(), ()),
            notional_per_trade=notional_per_trade,
            slippage_bps=slippage_bps,
            fee_bps=fee_bps,
            bar_interval_minutes=bar_interval_minutes,
            bars_source=str(retained_bars_path),
            evidence_root=paths.source_artifacts / "outcomes",
        )
        for signal in signals
    ]
    for row in trades:
        observation_id = _stable_id(
            "mover_reconciliation_observation",
            _json_fingerprint(row),
        )
        row["observation_id"] = observation_id
        _write_immutable_json(
            paths.trades / "by_observation" / f"{observation_id}.json",
            row,
        )
    trade_fingerprint = _json_fingerprint(trades)
    trades_path = paths.trades / f"trades_{trade_fingerprint[:16]}.jsonl"
    _write_immutable_jsonl(trades_path, trades)
    closed = [row for row in trades if row["status"] == "closed"]
    pending = [
        row for row in trades if str(row.get("status") or "").startswith("pending_")
    ]
    not_entered = [row for row in trades if row["status"] == "not_entered"]
    payload = {
        "schema_version": f"{SCHEMA_VERSION}.reconcile",
        "status": "passed" if not pending else "passed_with_pending",
        "signal_count": len(signals),
        "closed_trade_count": len(closed),
        "pending_trade_count": len(pending),
        "not_entered_count": len(not_entered),
        "resolved_signal_count": len(closed) + len(not_entered),
        "trades_path": str(trades_path.resolve()),
        "notional_per_trade": notional_per_trade,
        "slippage_bps": slippage_bps,
        "fee_bps": fee_bps,
        "bar_interval_minutes": bar_interval_minutes,
        "trade_fingerprint": trade_fingerprint,
        "signals_path": str(retained_signals_path.resolve()),
        "signals_sha256": _sha256_file(retained_signals_path),
        "original_signals_path": str(signals_path.resolve()),
        "bars_csv": str(retained_bars_path),
        "bars_csv_sha256": _sha256_file(retained_bars_path),
        "bars_source_ref": bars_source_ref,
        "original_bars_csv": str(bars_csv.resolve()),
        "trades_sha256": _sha256_file(trades_path),
        "bar_timestamp_semantics": bar_timestamp_semantics,
        "missing_return_semantics": "pending rows retain net_return_pct=null",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    run_fingerprint = _json_fingerprint(payload)
    run_manifest_path = paths.manifests / f"reconcile_{run_fingerprint[:16]}.json"
    run_payload = {**payload, "run_fingerprint": run_fingerprint}
    _write_immutable_json(run_manifest_path, run_payload)
    payload = {
        **run_payload,
        "run_manifest_path": str(run_manifest_path.resolve()),
    }
    _write_json(paths.manifests / "reconcile_latest.json", payload)
    return payload


def analyze(
    *,
    scan_manifest_path: Path,
    reconcile_manifest_path: Path,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Produce chronological, after-cost evidence without automatic promotion."""

    paths = MoverLabPaths.create(output_root)
    (
        trades,
        decisions,
        signals,
        expected_market_dates,
        included_run_pairs,
        excluded_incompatible_run_count,
    ) = _load_cumulative_analysis_inputs(
        paths=paths,
        scan_manifest_path=scan_manifest_path,
        reconcile_manifest_path=reconcile_manifest_path,
    )
    all_closed = [
        row
        for row in trades
        if row.get("status") == "closed"
        and _optional_float(row.get("net_return_pct")) is not None
    ]
    closed = [
        row
        for row in all_closed
        if row.get("evidence_mode") == "forward_observation"
    ]
    replay_closed = [
        row
        for row in all_closed
        if row.get("evidence_mode") == "historical_replay"
    ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in closed:
        grouped[_strategy_key(row)].append(row)
    trades_by_strategy: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in trades:
        trades_by_strategy[_strategy_key(row)].append(row)
    replay_by_strategy: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in replay_closed:
        replay_by_strategy[_strategy_key(row)].append(row)
    decision_counts: dict[tuple[str, str], int] = defaultdict(int)
    forward_decision_counts: dict[tuple[str, str], int] = defaultdict(int)
    emitted_signal_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    forward_decisions_by_day: dict[
        tuple[str, str],
        dict[str, list[dict[str, Any]]],
    ] = defaultdict(lambda: defaultdict(list))
    for row in decisions:
        key = _strategy_key(row)
        decision_counts[key] += 1
        if row.get("evidence_mode") == "forward_observation":
            forward_decision_counts[key] += 1
            if row.get("market_date"):
                forward_decisions_by_day[key][str(row["market_date"])].append(row)
        signal = row.get("signal")
        if (
            isinstance(signal, dict)
            and signal.get("signal_id")
            and signal.get("evidence_mode") == "forward_observation"
        ):
            emitted_signal_ids[key].add(str(signal["signal_id"]))

    catalog_by_key = {
        (spec.strategy_id, spec.version): spec for spec in strategy_catalog()
    }
    observed_keys = set(grouped) | set(trades_by_strategy) | set(decision_counts)
    strategy_keys = sorted(set(catalog_by_key) | observed_keys)

    strategy_rows: list[dict[str, Any]] = []
    correlation_groups: list[
        tuple[tuple[str, str], list[dict[str, Any]]]
    ] = []
    for key in strategy_keys:
        spec = catalog_by_key.get(key)
        rows = sorted(
            grouped.get(key, []),
            key=lambda row: (
                str(row.get("market_date") or ""),
                str(row.get("entry_at") or ""),
            ),
        )
        metrics = _return_metrics(rows)
        day_level_metrics = _return_metrics(_strategy_day_book_rows(rows))
        replay_metrics = _return_metrics(replay_by_strategy.get(key, []))
        correlation_groups.append((key, rows))
        opportunity_count = decision_counts.get(key, 0)
        forward_opportunity_count = forward_decision_counts.get(key, 0)
        strategy_trades = [
            row
            for row in trades_by_strategy.get(key, [])
            if row.get("evidence_mode") == "forward_observation"
        ]
        expected_signal_ids = set(emitted_signal_ids.get(key, set()))
        resolved_signal_ids = {
            str(row.get("signal_id") or "")
            for row in strategy_trades
            if row.get("signal_id")
            and row.get("status") in {"closed", "not_entered"}
        }
        pending_count = sum(
            1
            for row in strategy_trades
            if str(row.get("status") or "").startswith("pending_")
        )
        not_entered_count = sum(
            1 for row in strategy_trades if row.get("status") == "not_entered"
        )
        coverage_pct = (
            len(resolved_signal_ids & expected_signal_ids)
            / len(expected_signal_ids)
            * 100.0
            if expected_signal_ids
            else None
        )
        splits = _chronological_splits(rows, spec=spec)
        fully_evaluated_forward_days = {
            market_date
            for market_date, day_decisions in forward_decisions_by_day.get(
                key,
                {},
            ).items()
            if spec is not None
            and _forward_strategy_day_fully_evaluated(
                spec,
                market_date,
                day_decisions,
            )
        }
        forward_session_count = len(fully_evaluated_forward_days)
        gates = {
            "current_frozen_strategy_version": spec is not None,
            "minimum_forward_sessions": forward_session_count
            >= MIN_FORWARD_SESSIONS,
            "minimum_closed_trades": len(rows) >= MIN_CLOSED_TRADES,
            "coverage": (
                coverage_pct is not None
                and coverage_pct >= MIN_COVERAGE_PCT
            ),
            "positive_after_cost_expectancy": (
                day_level_metrics["mean_net_return_pct"] is not None
                and day_level_metrics["mean_net_return_pct"] > 0
            ),
            "positive_lower_confidence_bound": (
                day_level_metrics["mean_return_lower_95_pct"] is not None
                and day_level_metrics["mean_return_lower_95_pct"] > 0
            ),
            "positive_validation_and_locked_test": _split_gate(splits),
        }
        strategy_rows.append(
            {
                "strategy_id": key[0],
                "strategy_version": key[1],
                "status": (
                    "manual_review_candidate"
                    if gates and all(gates.values())
                    else (
                        "insufficient_forward_evidence"
                        if spec is not None
                        else "unregistered_or_retired_version"
                    )
                ),
                "automatic_promotion_enabled": False,
                "opportunity_count": opportunity_count,
                "forward_opportunity_count": forward_opportunity_count,
                "emitted_signal_count": len(expected_signal_ids),
                "resolved_signal_count": len(
                    resolved_signal_ids & expected_signal_ids
                ),
                "pending_signal_count": pending_count,
                "not_entered_count": not_entered_count,
                "forward_session_count": forward_session_count,
                "fully_evaluated_forward_market_dates": sorted(
                    fully_evaluated_forward_days
                ),
                "coverage_pct": (
                    round(coverage_pct, 4)
                    if coverage_pct is not None
                    else None
                ),
                "metrics": metrics,
                "day_level_metrics": day_level_metrics,
                "historical_replay_metrics": replay_metrics,
                "chronological_splits": splits,
                "promotion_gates": gates,
                "warnings": (
                    (
                        []
                        if rows
                        else ["no_closed_forward_paper_trades"]
                    )
                    + (
                        []
                        if spec is not None
                        else ["strategy_version_not_in_current_frozen_catalog"]
                    )
                ),
            }
        )

    correlations: list[dict[str, Any]] = []
    tested_feature_count = 0
    for key, rows in correlation_groups:
        spec = catalog_by_key.get(key)
        strategy_correlations, tested = _feature_correlations(rows, spec=spec)
        tested_feature_count += tested
        correlations.extend(
            {
                **item,
                "strategy_id": key[0],
                "strategy_version": key[1],
                "population": "emitted_signals_only_not_general_mover_learning",
            }
            for item in strategy_correlations
        )
    family_alpha = (
        0.05 / tested_feature_count if tested_feature_count else None
    )
    for item in correlations:
        item["bonferroni_alpha"] = (
            round(family_alpha, 8) if family_alpha is not None else None
        )
        item["passes_multiple_testing_gate"] = bool(
            family_alpha is not None
            and float(item["approx_p_value"]) < family_alpha
        )
    payload = {
        "schema_version": f"{SCHEMA_VERSION}.analysis",
        "status": (
            "passed" if closed else "passed_without_forward_performance"
        ),
        "closed_trade_count": len(closed),
        "all_closed_trade_count": len(all_closed),
        "historical_replay_closed_trade_count": len(replay_closed),
        "forward_performance_metrics_available": bool(closed),
        "validated_signal_count": len(signals),
        "expected_market_dates": list(expected_market_dates),
        "scan_manifest_path": str(scan_manifest_path.resolve()),
        "reconcile_manifest_path": str(reconcile_manifest_path.resolve()),
        "analysis_series_mode": "cumulative_compatible_daily_runs",
        "included_run_pair_count": len(included_run_pairs),
        "included_run_pairs": included_run_pairs,
        "excluded_incompatible_run_count": excluded_incompatible_run_count,
        "pending_or_missing_count": sum(
            1
            for row in trades
            if str(row.get("status") or "").startswith("pending_")
            and row.get("evidence_mode") == "forward_observation"
        ),
        "not_entered_count": sum(
            1
            for row in trades
            if row.get("status") == "not_entered"
            and row.get("evidence_mode") == "forward_observation"
        ),
        "strategy_results": strategy_rows,
        "feature_correlations": correlations,
        "feature_correlation_scope": (
            "diagnostic within emitted signals only; candidate-study outcomes are "
            "required for general mover-pattern learning"
        ),
        "multiple_testing": {
            "tested_feature_count": tested_feature_count,
            "reported_feature_count": len(correlations),
            "method": "Bonferroni family-wise threshold over discovery-split features",
            "automatic_strategy_creation": False,
        },
        "research_only": True,
        "broker_execution_enabled": False,
        "truth_note": (
            "A higher historical return is not a guarantee. Only frozen forward, "
            "after-cost, source-complete evidence can reach manual review."
        ),
    }
    daily_calendar = _daily_strategy_calendar(
        trades,
        decisions,
        expected_market_dates=expected_market_dates,
    )
    calendar_fingerprint = _json_fingerprint(daily_calendar)
    calendar_json_path = (
        paths.reports
        / f"strategy_daily_calendar_{calendar_fingerprint[:16]}.json"
    )
    calendar_csv_path = (
        paths.reports
        / f"strategy_daily_calendar_{calendar_fingerprint[:16]}.csv"
    )
    _write_immutable_json(calendar_json_path, daily_calendar)
    _write_csv_rows(calendar_csv_path, daily_calendar)
    payload["strategy_daily_calendar"] = daily_calendar
    payload["strategy_daily_calendar_path"] = str(calendar_json_path.resolve())
    payload["strategy_daily_calendar_csv_path"] = str(calendar_csv_path.resolve())
    calendar_html_path = (
        paths.reports
        / f"strategy_calendar_{calendar_fingerprint[:16]}.html"
    )
    write_strategy_calendar_report(payload, calendar_html_path)
    payload["strategy_daily_calendar_html_path"] = str(
        calendar_html_path.resolve()
    )
    analysis_fingerprint = _json_fingerprint(payload)
    report_path = (
        paths.reports
        / f"mover_pattern_analysis_{analysis_fingerprint[:16]}.json"
    )
    markdown_path = (
        paths.reports
        / f"mover_pattern_analysis_{analysis_fingerprint[:16]}.md"
    )
    _write_immutable_json(report_path, payload)
    _write_immutable_text(markdown_path, _analysis_markdown(payload))
    latest = {
        "schema_version": f"{SCHEMA_VERSION}.analysis_latest",
        "analysis_fingerprint": analysis_fingerprint,
        "report_path": str(report_path.resolve()),
        "report_sha256": _sha256_file(report_path),
        "markdown_path": str(markdown_path.resolve()),
        "markdown_sha256": _sha256_file(markdown_path),
        "calendar_html_path": str(calendar_html_path.resolve()),
        "calendar_html_sha256": _sha256_file(calendar_html_path),
    }
    _write_json(paths.reports / "mover_pattern_analysis_latest.json", latest)
    return {
        **payload,
        "analysis_fingerprint": analysis_fingerprint,
        "report_path": str(report_path.resolve()),
        "markdown_path": str(markdown_path.resolve()),
    }


def _load_analysis_run_inputs(
    *,
    paths: MoverLabPaths,
    scan_manifest_path: Path,
    reconcile_manifest_path: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    tuple[str, ...],
]:
    manifests_directory = paths.manifests.resolve()
    if (
        scan_manifest_path.resolve().parent != manifests_directory
        or reconcile_manifest_path.resolve().parent != manifests_directory
    ):
        raise ValueError("analysis manifests must be retained under output_root")
    scan_manifest = _read_content_addressed_run_manifest(scan_manifest_path)
    reconcile_manifest = _read_content_addressed_run_manifest(
        reconcile_manifest_path
    )
    if scan_manifest.get("schema_version") != f"{SCHEMA_VERSION}.paper_scan":
        raise ValueError("analysis requires a mover paper-scan run manifest")
    if reconcile_manifest.get("schema_version") != f"{SCHEMA_VERSION}.reconcile":
        raise ValueError("analysis requires a mover reconciliation run manifest")
    decisions_path = Path(str(scan_manifest.get("decisions_path") or ""))
    signals_path = Path(str(scan_manifest.get("signals_path") or ""))
    reconcile_signals_path = Path(
        str(reconcile_manifest.get("signals_path") or "")
    )
    trades_path = Path(str(reconcile_manifest.get("trades_path") or ""))
    required_paths = {
        "decisions": decisions_path,
        "signals": signals_path,
        "reconcile signals": reconcile_signals_path,
        "trades": trades_path,
    }
    expected_parents = {
        "decisions": paths.decisions.resolve(),
        "signals": paths.signals.resolve(),
        "reconcile signals": paths.signals.resolve(),
        "trades": paths.trades.resolve(),
    }
    for label, path in required_paths.items():
        if not str(path) or not path.exists():
            raise ValueError(f"analysis {label} artifact is missing")
        if path.resolve().parent != expected_parents[label]:
            raise ValueError(f"analysis {label} artifact escaped output_root")
    if signals_path.resolve() != reconcile_signals_path.resolve():
        raise ValueError("scan and reconciliation manifests reference different signals")
    for manifest, path, field in (
        (scan_manifest, decisions_path, "decisions_sha256"),
        (scan_manifest, signals_path, "signals_sha256"),
        (reconcile_manifest, reconcile_signals_path, "signals_sha256"),
        (reconcile_manifest, trades_path, "trades_sha256"),
    ):
        if str(manifest.get(field) or "") != _sha256_file(path):
            raise ValueError(f"analysis artifact hash mismatch: {field}")
    decisions = _read_jsonl(decisions_path)
    signals = _read_jsonl(signals_path)
    trades = _read_jsonl(trades_path)
    if not _rows_match_by_id_artifacts(
        decisions,
        paths.decisions / "by_id",
        "decision_id",
    ):
        raise ValueError("analysis decisions do not match retained by-id artifacts")
    if not _rows_match_by_id_artifacts(
        signals,
        paths.signals / "by_id",
        "signal_id",
    ):
        raise ValueError("analysis signals do not match retained by-id artifacts")
    if not _rows_match_by_id_artifacts(
        trades,
        paths.trades / "by_observation",
        "observation_id",
    ):
        raise ValueError("analysis trades do not match retained observations")
    closed_trades = [row for row in trades if row.get("status") == "closed"]
    if (
        not _trade_evidence_valid(trades)
        or any(not _closed_trade_math_valid(row) for row in closed_trades)
        or any(not _closed_trade_timestamps_valid(row) for row in closed_trades)
        or any(
            row.get("source_coverage_complete") is not True
            for row in closed_trades
        )
        or any(
            row.get("net_return_pct") is not None
            for row in trades
            if row.get("status") != "closed"
        )
    ):
        raise ValueError("analysis trade outcome integrity validation failed")
    try:
        signal_contracts = [MoverPaperSignal.from_mapping(row) for row in signals]
    except (TypeError, ValueError) as exc:
        raise ValueError("analysis signal contract validation failed") from exc
    signal_ids = {row.signal_id for row in signal_contracts}
    decision_signal_ids = {
        str(row.get("signal", {}).get("signal_id") or "")
        for row in decisions
        if isinstance(row.get("signal"), dict)
    }
    trade_signal_ids = {
        str(row.get("signal_id") or "")
        for row in trades
        if row.get("signal_id")
    }
    if decision_signal_ids != signal_ids or trade_signal_ids != signal_ids:
        raise ValueError(
            "analysis requires complete decision, signal, and reconciliation ledgers"
        )
    signal_by_id = {
        str(row.get("signal_id") or ""): row for row in signals
    }
    if not _analysis_decision_lineage_valid(decisions, signal_by_id):
        raise ValueError("analysis decision lineage validation failed")
    if not _analysis_trade_lineage_valid(trades, signal_by_id):
        raise ValueError("analysis trade lineage validation failed")
    snapshot_rows = _read_json_objects(paths.snapshots / "by_id")
    if not _signals_reference_snapshots(signals, snapshot_rows):
        raise ValueError("analysis signals lack validated retained snapshot lineage")
    referenced_snapshot_ids = {
        str(row.get("snapshot_id") or "") for row in signals
    }
    referenced_snapshots = [
        row
        for row in snapshot_rows
        if str(row.get("snapshot_id") or "") in referenced_snapshot_ids
    ]
    if (
        len(referenced_snapshots) != len(referenced_snapshot_ids)
        or not _source_artifact_refs_valid(referenced_snapshots)
        or not all(_snapshot_identity_valid(row) for row in referenced_snapshots)
        or not all(_forward_receipt_valid(row) for row in referenced_snapshots)
        or not all(
            _forward_universe_artifact_valid(row)
            for row in referenced_snapshots
        )
    ):
        raise ValueError("analysis snapshot source lineage validation failed")
    expected_raw = scan_manifest.get("expected_market_dates")
    if not isinstance(expected_raw, list) or not expected_raw:
        raise ValueError("scan manifest requires expected_market_dates")
    expected_dates = tuple(sorted({str(value) for value in expected_raw}))
    for market_date in expected_dates:
        if not market_session(date.fromisoformat(market_date)).is_trading_day:
            raise ValueError("scan manifest contains a non-session expected date")
    return trades, decisions, signals, expected_dates


def _load_cumulative_analysis_inputs(
    *,
    paths: MoverLabPaths,
    scan_manifest_path: Path,
    reconcile_manifest_path: Path,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    tuple[str, ...],
    list[dict[str, str]],
    int,
]:
    """Load every compatible retained daily run exactly once."""

    anchor_scan = _read_content_addressed_run_manifest(scan_manifest_path)
    anchor_reconcile = _read_content_addressed_run_manifest(
        reconcile_manifest_path
    )
    if anchor_scan.get("schema_version") != f"{SCHEMA_VERSION}.paper_scan":
        raise ValueError("analysis anchor scan manifest has the wrong schema")
    if anchor_reconcile.get("schema_version") != f"{SCHEMA_VERSION}.reconcile":
        raise ValueError("analysis anchor reconciliation has the wrong schema")
    anchor_policy = _reconciliation_policy(anchor_reconcile)

    scan_paths = _content_addressed_manifest_paths(paths.manifests, "paper_scan")
    reconcile_paths = _content_addressed_manifest_paths(paths.manifests, "reconcile")
    if scan_manifest_path.resolve() not in {path.resolve() for path in scan_paths}:
        scan_paths.append(scan_manifest_path.resolve())
    if reconcile_manifest_path.resolve() not in {
        path.resolve() for path in reconcile_paths
    }:
        reconcile_paths.append(reconcile_manifest_path.resolve())

    reconcile_records: list[tuple[Path, dict[str, Any]]] = []
    excluded_incompatible = 0
    for path in sorted(reconcile_paths):
        manifest = _read_content_addressed_run_manifest(path)
        if manifest.get("schema_version") != f"{SCHEMA_VERSION}.reconcile":
            raise ValueError("retained reconciliation manifest has the wrong schema")
        if _reconciliation_policy(manifest) != anchor_policy:
            excluded_incompatible += 1
            continue
        reconcile_records.append((path, manifest))

    all_trades: list[dict[str, Any]] = []
    all_decisions: list[dict[str, Any]] = []
    all_signals: list[dict[str, Any]] = []
    all_dates: set[str] = set()
    included_pairs: list[dict[str, str]] = []
    seen_pair_ids: set[str] = set()
    for scan_path in sorted(scan_paths):
        scan_manifest = _read_content_addressed_run_manifest(scan_path)
        if scan_manifest.get("schema_version") != f"{SCHEMA_VERSION}.paper_scan":
            raise ValueError("retained paper-scan manifest has the wrong schema")
        signals_path = Path(str(scan_manifest.get("signals_path") or "")).resolve()
        candidates = [
            (path, manifest)
            for path, manifest in reconcile_records
            if Path(str(manifest.get("signals_path") or "")).resolve()
            == signals_path
        ]
        if not candidates:
            continue
        best_rank = max(_reconciliation_completion_rank(item[1]) for item in candidates)
        best = [
            item
            for item in candidates
            if _reconciliation_completion_rank(item[1]) == best_rank
        ]
        if len({str(item[1].get("trades_sha256") or "") for item in best}) > 1:
            raise ValueError(
                "compatible reconciliations conflict at the same completion rank"
            )
        selected_reconcile_path, selected_reconcile = sorted(
            best,
            key=lambda item: str(item[1].get("run_fingerprint") or ""),
        )[-1]
        pair_id = _stable_id(
            "mover_analysis_run_pair",
            str(scan_manifest.get("run_fingerprint") or ""),
            str(selected_reconcile.get("run_fingerprint") or ""),
        )
        if pair_id in seen_pair_ids:
            continue
        seen_pair_ids.add(pair_id)
        trades, decisions, signals, expected_dates = _load_analysis_run_inputs(
            paths=paths,
            scan_manifest_path=scan_path,
            reconcile_manifest_path=selected_reconcile_path,
        )
        all_trades.extend(trades)
        all_decisions.extend(decisions)
        all_signals.extend(signals)
        all_dates.update(expected_dates)
        included_pairs.append(
            {
                "pair_id": pair_id,
                "scan_run_fingerprint": str(
                    scan_manifest.get("run_fingerprint") or ""
                ),
                "reconcile_run_fingerprint": str(
                    selected_reconcile.get("run_fingerprint") or ""
                ),
                "scan_manifest_path": str(scan_path.resolve()),
                "reconcile_manifest_path": str(
                    selected_reconcile_path.resolve()
                ),
            }
        )
    anchor_scan_fingerprint = str(anchor_scan.get("run_fingerprint") or "")
    if not any(
        row["scan_run_fingerprint"] == anchor_scan_fingerprint
        for row in included_pairs
    ):
        raise ValueError("analysis anchor scan was not selected into its series")
    return (
        _deduplicate_analysis_rows(all_trades, "signal_id"),
        _deduplicate_analysis_rows(all_decisions, "decision_id"),
        _deduplicate_analysis_rows(all_signals, "signal_id"),
        tuple(sorted(all_dates)),
        sorted(included_pairs, key=lambda row: row["pair_id"]),
        excluded_incompatible,
    )


def _content_addressed_manifest_paths(directory: Path, prefix: str) -> list[Path]:
    output: list[Path] = []
    for path in directory.glob(f"{prefix}_*.json"):
        token = path.stem.removeprefix(f"{prefix}_")
        if len(token) == 16 and all(
            character in "0123456789abcdef" for character in token
        ):
            output.append(path.resolve())
    return output


def _reconciliation_policy(manifest: dict[str, Any]) -> tuple[Any, ...]:
    return (
        manifest.get("notional_per_trade"),
        manifest.get("slippage_bps"),
        manifest.get("fee_bps"),
        manifest.get("bar_interval_minutes"),
        manifest.get("bar_timestamp_semantics"),
    )


def _reconciliation_completion_rank(manifest: dict[str, Any]) -> tuple[int, int]:
    return (
        int(manifest.get("resolved_signal_count") or 0),
        -int(manifest.get("pending_trade_count") or 0),
    )


def _deduplicate_analysis_rows(
    rows: list[dict[str, Any]],
    identity_field: str,
) -> list[dict[str, Any]]:
    retained: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = str(row.get(identity_field) or "")
        if not identity:
            raise ValueError(f"analysis row is missing {identity_field}")
        existing = retained.get(identity)
        if existing is not None and existing != row:
            raise ValueError(
                f"analysis contains conflicting {identity_field}: {identity}"
            )
        retained[identity] = row
    return [retained[key] for key in sorted(retained)]


def _analysis_decision_lineage_valid(
    decisions: list[dict[str, Any]],
    signal_by_id: dict[str, dict[str, Any]],
) -> bool:
    for decision in decisions:
        expected_id = _stable_id(
            "mover_strategy_decision",
            str(decision.get("strategy_id") or ""),
            str(decision.get("strategy_version") or ""),
            str(decision.get("snapshot_id") or ""),
        )
        embedded = decision.get("signal")
        if (
            decision.get("decision_id") != expected_id
            or decision.get("research_only") is not True
            or decision.get("broker_execution_enabled") is not False
            or decision.get("decision")
            not in {"paper_signal", "rejected", "skipped"}
        ):
            return False
        if embedded is None:
            if decision.get("decision") == "paper_signal":
                return False
            continue
        if not isinstance(embedded, dict):
            return False
        signal_id = str(embedded.get("signal_id") or "")
        if (
            decision.get("decision") != "paper_signal"
            or signal_by_id.get(signal_id) != embedded
            or embedded.get("strategy_id") != decision.get("strategy_id")
            or embedded.get("strategy_version")
            != decision.get("strategy_version")
            or embedded.get("snapshot_id") != decision.get("snapshot_id")
            or embedded.get("market_date") != decision.get("market_date")
            or embedded.get("symbol") != decision.get("symbol")
            or embedded.get("evidence_mode") != decision.get("evidence_mode")
            or embedded.get("signal_at")
            != decision.get("feature_cutoff_at")
        ):
            return False
    return True


def _analysis_trade_lineage_valid(
    trades: list[dict[str, Any]],
    signal_by_id: dict[str, dict[str, Any]],
) -> bool:
    if len(trades) != len(signal_by_id):
        return False
    for trade in trades:
        signal = signal_by_id.get(str(trade.get("signal_id") or ""))
        if signal is None:
            return False
        without_observation = {
            key: value
            for key, value in trade.items()
            if key != "observation_id"
        }
        expected_observation_id = _stable_id(
            "mover_reconciliation_observation",
            _json_fingerprint(without_observation),
        )
        if (
            trade.get("observation_id") != expected_observation_id
            or trade.get("strategy_id") != signal.get("strategy_id")
            or trade.get("strategy_version") != signal.get("strategy_version")
            or trade.get("strategy_semantics_fingerprint")
            != signal.get("strategy_semantics_fingerprint")
            or trade.get("market_date") != signal.get("market_date")
            or trade.get("symbol") != signal.get("symbol")
            or trade.get("signal_at") != signal.get("signal_at")
            or trade.get("evidence_mode") != signal.get("evidence_mode")
            or trade.get("source_captured_at")
            != signal.get("source_captured_at")
            or trade.get("system_received_at")
            != signal.get("system_received_at")
            or trade.get("forward_receipt_ref")
            != signal.get("forward_receipt_ref")
            or trade.get("research_only") is not True
            or trade.get("broker_execution_enabled") is not False
        ):
            return False
    return True


def _read_content_addressed_run_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("run manifest must be a JSON object")
    fingerprint = str(payload.get("run_fingerprint") or "")
    unhashed = {key: value for key, value in payload.items() if key != "run_fingerprint"}
    if len(fingerprint) != 64 or _json_fingerprint(unhashed) != fingerprint:
        raise ValueError("run manifest content fingerprint mismatch")
    if fingerprint[:16] not in path.stem:
        raise ValueError("run manifest filename does not match its fingerprint")
    return payload


def _run_manifest_series_valid(
    directory: Path,
    *,
    prefix: str,
    schema_version: str,
    artifact_hash_fields: tuple[tuple[str, str], ...],
    artifact_parent_fields: tuple[tuple[str, Path], ...] = (),
) -> tuple[int, bool]:
    manifest_paths = [
        path
        for path in directory.glob(f"{prefix}_*.json")
        if len(path.stem.removeprefix(f"{prefix}_")) == 16
        and all(
            character in "0123456789abcdef"
            for character in path.stem.removeprefix(f"{prefix}_")
        )
    ]
    if not manifest_paths:
        return 0, True
    try:
        for manifest_path in manifest_paths:
            payload = _read_content_addressed_run_manifest(manifest_path)
            if (
                payload.get("schema_version") != schema_version
                or payload.get("research_only") is not True
                or payload.get("broker_execution_enabled") is not False
            ):
                return len(manifest_paths), False
            for path_field, hash_field in artifact_hash_fields:
                artifact_path = Path(str(payload.get(path_field) or ""))
                if (
                    not artifact_path.is_file()
                    or str(payload.get(hash_field) or "")
                    != _sha256_file(artifact_path)
                ):
                    return len(manifest_paths), False
            for path_field, expected_parent in artifact_parent_fields:
                artifact_path = Path(str(payload.get(path_field) or ""))
                if artifact_path.resolve().parent != expected_parent.resolve():
                    return len(manifest_paths), False
    except (OSError, ValueError, json.JSONDecodeError):
        return len(manifest_paths), False
    return len(manifest_paths), True


def _candidate_study_manifests_valid(
    paths: MoverLabPaths,
) -> tuple[int, bool]:
    count, valid = _run_manifest_series_valid(
        paths.manifests,
        prefix="candidate_study",
        schema_version="v2.mover_candidate_study_runtime.v1",
        artifact_hash_fields=(
            ("study_path", "study_sha256"),
            ("outcomes_path", "outcomes_sha256"),
            ("coverage_path", "coverage_sha256"),
            ("snapshots_path", "snapshots_sha256"),
            ("bars_csv", "bars_csv_sha256"),
            ("universe_denominators_path", "universe_denominators_sha256"),
            ("split_assignments_path", "split_assignments_sha256"),
            ("split_registry_path", "split_registry_sha256"),
        ),
    )
    if not valid or count == 0:
        return count, valid
    try:
        for manifest_path in paths.manifests.glob("candidate_study_*.json"):
            token = manifest_path.stem.removeprefix("candidate_study_")
            if len(token) != 16 or any(
                character not in "0123456789abcdef" for character in token
            ):
                continue
            manifest = _read_content_addressed_run_manifest(manifest_path)
            if not _candidate_manifest_paths_confined(paths, manifest):
                return count, False
            eod_path_raw = manifest.get("descriptive_eod_movers_path")
            if eod_path_raw is not None:
                eod_path = Path(str(eod_path_raw))
                if (
                    not eod_path.is_file()
                    or str(manifest.get("descriptive_eod_movers_sha256") or "")
                    != _sha256_file(eod_path)
                ):
                    return count, False
            study = json.loads(
                Path(str(manifest["study_path"])).read_text(encoding="utf-8")
            )
            evidence_mode = manifest.get("evidence_mode")
            outcomes = study.get("outcomes") if isinstance(study, dict) else None
            coverage = study.get("coverage") if isinstance(study, dict) else None
            research_complete = (
                manifest.get("general_mover_research_data_complete") is True
            )
            snapshot_rows = _read_jsonl(Path(str(manifest["snapshots_path"])))
            snapshots = [
                ProspectiveMoverSnapshot.from_mapping(row) for row in snapshot_rows
            ]
            denominator_rows = _read_structured_rows(
                Path(str(manifest["universe_denominators_path"]))
            )
            denominators = [
                CandidateUniverseDenominator.from_mapping(row)
                for row in denominator_rows
            ]
            snapshot_group_keys = {
                (snapshot.market_date, snapshot.feature_cutoff_at)
                for snapshot in snapshots
            }
            denominator_group_keys = {
                (denominator.market_date, denominator.feature_cutoff_at)
                for denominator in denominators
            }
            if denominator_group_keys != snapshot_group_keys:
                return count, False
            for denominator in denominators:
                group = [
                    snapshot
                    for snapshot in snapshots
                    if snapshot.market_date == denominator.market_date
                    and snapshot.feature_cutoff_at == denominator.feature_cutoff_at
                ]
                if (
                    not _candidate_universe_artifact_matches(denominator)
                    or any(
                        snapshot.universe_source_ref != denominator.source_ref
                        or snapshot.universe_selection_method
                        != denominator.universe_selection_method
                        or snapshot.evidence_mode != denominator.evidence_mode
                        for snapshot in group
                    )
                ):
                    return count, False
            split_registry = json.loads(
                Path(str(manifest["split_registry_path"])).read_text(
                    encoding="utf-8"
                )
            )
            if not isinstance(split_registry, dict) or not isinstance(
                split_registry.get("assignments"), dict
            ):
                return count, False
            if not _split_registry_matches_source_file(
                split_registry,
                Path(str(manifest["split_assignments_path"])),
            ):
                return count, False
            split_assignment = CandidateSplitAssignment.create(
                {
                    str(key): str(value)
                    for key, value in split_registry["assignments"].items()
                },
                source_ref=str(split_registry.get("source_ref") or ""),
            )
            assumptions_raw = manifest.get("assumptions")
            if not isinstance(assumptions_raw, dict):
                return count, False
            assumptions = CandidateStudyAssumptions(
                bar_interval_minutes=int(assumptions_raw["bar_interval_minutes"]),
                slippage_bps=float(assumptions_raw["slippage_bps"]),
                fee_bps=float(assumptions_raw["fee_bps"]),
            )
            bars_path = Path(str(manifest["bars_csv"]))
            dataset = load_ohlcv_csv(
                bars_path,
                dataset_id=f"mover_candidate_verify:{_sha256_file(bars_path)[:16]}",
                source_kind="operator_intraday_csv",
                timeframe="intraday",
            )
            if dataset.warnings:
                return count, False
            bars = tuple(
                bar
                for symbol in sorted(dataset.bars_by_symbol)
                for bar in dataset.bars_by_symbol[symbol]
            )
            eod_rows = (
                [
                    _normalize_candidate_eod_row(row)
                    for row in _read_structured_rows(eod_path)
                ]
                if eod_path_raw is not None
                else []
            )
            if not all(_candidate_eod_artifacts_valid(row) for row in eod_rows):
                return count, False
            recomputed = study_all_candidates(
                snapshots=snapshots,
                bars=bars,
                universe_denominators=denominators,
                split_assignment=split_assignment,
                assumptions=assumptions,
                bars_source_ref=str(manifest.get("bars_source_ref") or ""),
                descriptive_eod_movers=eod_rows,
            ).to_dict()
            outcomes_artifact = _read_jsonl(Path(str(manifest["outcomes_path"])))
            coverage_rows = coverage if isinstance(coverage, list) else []
            actual_coverage_complete = bool(coverage_rows) and all(
                isinstance(row, dict)
                and row.get("snapshot_coverage_complete") is True
                and row.get("outcome_coverage_complete") is True
                for row in coverage_rows
            )
            forward_lineage_valid = bool(
                snapshot_rows
                and all(_snapshot_identity_valid(row) for row in snapshot_rows)
                and _source_artifact_refs_valid(snapshot_rows)
                and all(_forward_receipt_valid(row) for row in snapshot_rows)
                and all(_forward_universe_artifact_valid(row) for row in snapshot_rows)
            )
            expected_forward_eligible = bool(
                evidence_mode == "forward_observation"
                and actual_coverage_complete
                and forward_lineage_valid
                and coverage_rows
                and all(
                    isinstance(row, dict)
                    and row.get("expected_symbols_complete") is True
                    and row.get("snapshot_coverage_complete") is True
                    and row.get("outcome_coverage_complete") is True
                    for row in coverage_rows
                )
            )
            bars_source_ref = str(manifest.get("bars_source_ref") or "")
            split_source_ref = str(split_registry.get("source_ref") or "")
            if (
                evidence_mode not in EVIDENCE_MODES
                or not isinstance(outcomes, list)
                or not isinstance(coverage, list)
                or study.get("evidence_mode") != evidence_mode
                or study.get("study_id") != manifest.get("study_id")
                or recomputed != study
                or outcomes_artifact != outcomes
                or not _candidate_coverage_csv_matches(
                    Path(str(manifest["coverage_path"])), coverage
                )
                or split_registry.get("assignment_id")
                != split_assignment.assignment_id
                or study.get("split_assignment_id")
                != split_assignment.assignment_id
                or manifest.get("snapshot_count") != len(snapshot_rows)
                or manifest.get("complete_outcome_count")
                != sum(1 for row in outcomes if row.get("status") == "complete")
                or manifest.get("pending_outcome_count")
                != sum(
                    1
                    for row in outcomes
                    if str(row.get("status") or "").startswith("pending_")
                )
                or manifest.get("coverage_group_count") != len(coverage)
                or manifest.get("all_candidate_coverage_complete")
                is not actual_coverage_complete
                or research_complete is not actual_coverage_complete
                or manifest.get("forward_learning_eligible")
                is not expected_forward_eligible
                or (
                    evidence_mode == "forward_observation"
                    and not forward_lineage_valid
                )
                or manifest.get("automatic_strategy_creation") is not False
                or manifest.get("automatic_promotion_enabled") is not False
                or manifest.get("performance_claim_eligible") is not False
                or not _content_artifact_ref_valid(bars_source_ref)
                or not _content_artifact_ref_valid(split_source_ref)
                or bars_source_ref.split(":", 2)[1]
                != str(manifest.get("bars_csv_sha256") or "")
                or split_source_ref.split(":", 2)[1]
                != str(manifest.get("split_assignments_sha256") or "")
                or manifest.get("research_only") is not True
                or manifest.get("broker_execution_enabled") is not False
                or any(
                    not isinstance(row, dict)
                    or row.get("evidence_mode") != evidence_mode
                    for row in outcomes
                )
            ):
                return count, False
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return count, False
    return count, True


def _read_structured_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"structured rows CSV is empty: {path}")
            return [dict(row) for row in reader]
    if text.startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list) or not all(
            isinstance(row, dict) for row in payload
        ):
            raise ValueError(f"structured rows must be a JSON array: {path}")
        return [dict(row) for row in payload]
    return _read_jsonl(path)


def _normalize_candidate_eod_row(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    artifact_ref = str(
        normalized.get("source_ref")
        or normalized.get("source_artifact_ref")
        or ""
    )
    artifact_path = str(normalized.get("source_artifact_path") or "").strip()
    if artifact_ref.count(":") == 1 and artifact_path:
        artifact_ref = f"{artifact_ref}:{artifact_path}"
    normalized["source_ref"] = artifact_ref
    normalized["source_artifact_ref"] = artifact_ref
    corporate_action_ref = str(
        normalized.get("corporate_action_source_ref") or ""
    ).strip()
    corporate_action_path = str(
        normalized.get("corporate_action_source_path") or ""
    ).strip()
    if corporate_action_ref.count(":") == 1 and corporate_action_path:
        corporate_action_ref = (
            f"{corporate_action_ref}:{corporate_action_path}"
        )
    normalized["corporate_action_source_ref"] = corporate_action_ref
    if "source_complete" not in normalized:
        normalized["source_complete"] = normalized.get("source_coverage_complete")
    if "list_coverage_complete" not in normalized:
        normalized["list_coverage_complete"] = normalized.get(
            "source_coverage_complete"
        )
    for field in (
        "source_complete",
        "list_coverage_complete",
        "source_coverage_complete",
        "eod_label_eligible",
        "prospective_signal_eligible",
    ):
        if field in normalized:
            normalized[field] = _strict_candidate_bool(normalized[field], field)
    for field in ("rank", "mover_rank", "expected_row_count"):
        value = normalized.get(field)
        if value not in {None, ""}:
            normalized[field] = int(str(value))
    return normalized


def _candidate_manifest_paths_confined(
    paths: MoverLabPaths,
    manifest: Mapping[str, Any],
) -> bool:
    expected_parents = {
        "study_path": paths.reports / "candidate_studies",
        "outcomes_path": paths.trades / "candidate_outcomes",
        "coverage_path": paths.reports / "candidate_studies",
        "snapshots_path": paths.source_artifacts / "candidate_study" / "snapshots",
        "bars_csv": paths.source_artifacts / "candidate_study" / "bars",
        "universe_denominators_path": (
            paths.source_artifacts / "candidate_study" / "denominators"
        ),
        "split_assignments_path": (
            paths.source_artifacts / "candidate_study" / "splits"
        ),
        "split_registry_path": paths.manifests / "candidate_split_registry",
    }
    eod_path = manifest.get("descriptive_eod_movers_path")
    if eod_path is not None:
        expected_parents["descriptive_eod_movers_path"] = (
            paths.source_artifacts / "candidate_study" / "eod"
        )
    try:
        return all(
            Path(str(manifest.get(field) or "")).resolve().parent
            == parent.resolve()
            for field, parent in expected_parents.items()
        )
    except OSError:
        return False


def _split_registry_matches_source_file(
    split_registry: Mapping[str, Any],
    split_assignments_path: Path,
) -> bool:
    try:
        payload = json.loads(split_assignments_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    source_assignments = payload.get("assignments", payload)
    registry_assignments = split_registry.get("assignments")
    if not isinstance(source_assignments, Mapping) or not isinstance(
        registry_assignments, Mapping
    ):
        return False
    return {
        str(key): str(value) for key, value in source_assignments.items()
    } == {
        str(key): str(value) for key, value in registry_assignments.items()
    }


def _candidate_eod_artifacts_valid(row: Mapping[str, Any]) -> bool:
    source_ref = str(row.get("source_ref") or "")
    corporate_ref = str(row.get("corporate_action_source_ref") or "")
    if (
        not _content_artifact_ref_valid(source_ref)
        or not _content_artifact_ref_valid(corporate_ref)
        or source_ref == corporate_ref
        or _content_artifact_paths_equal(source_ref, corporate_ref)
    ):
        return False
    corporate_parts = corporate_ref.split(":", 2)
    try:
        payload = json.loads(
            Path(corporate_parts[2]).read_text(encoding="utf-8")
        )
        observed_at = _aware_datetime(payload.get("observed_at"))
        received_at = _aware_datetime(row.get("system_received_at"))
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return False
    market_date = str(row.get("market_date") or row.get("date") or "")
    return bool(
        isinstance(payload, Mapping)
        and payload.get("schema_version")
        == "v2.corporate_action_evidence.v1"
        and payload.get("market_date") == market_date
        and str(payload.get("symbol") or "").upper()
        == str(row.get("symbol") or row.get("ticker") or "").upper()
        and str(payload.get("corporate_action_status") or "").lower()
        == str(row.get("corporate_action_status") or "").lower()
        and bool(str(payload.get("source") or "").strip())
        and _candidate_timestamp_after_published_close(market_date, observed_at)
        and observed_at <= received_at
        and payload.get("research_only") is True
        and payload.get("broker_execution_enabled") is False
    )


def _content_artifact_paths_equal(first_ref: str, second_ref: str) -> bool:
    first_parts = first_ref.split(":", 2)
    second_parts = second_ref.split(":", 2)
    if len(first_parts) != 3 or len(second_parts) != 3:
        return True
    try:
        return Path(first_parts[2]).resolve() == Path(second_parts[2]).resolve()
    except OSError:
        return True


def _candidate_timestamp_after_published_close(
    market_date: str,
    observed_at: datetime,
) -> bool:
    try:
        market_day = date.fromisoformat(market_date)
        session = market_session(market_day)
    except (ValueError, MarketCalendarCoverageError):
        return False
    if not session.is_trading_day or session.close_time_et is None:
        return False
    close_at = datetime.combine(
        market_day,
        time.fromisoformat(session.close_time_et),
        tzinfo=MARKET_TZ,
    )
    observed_et = observed_at.astimezone(MARKET_TZ)
    return observed_et.date() == market_day and observed_et >= close_at


def _strict_candidate_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"{field} must be an explicit boolean")


def _candidate_coverage_csv_matches(
    path: Path,
    rows: list[dict[str, Any]],
) -> bool:
    fieldnames = (
        "market_date",
        "feature_cutoff_at",
        "denominator_id",
        "expected_count",
        "observed_count",
        "complete_outcome_count",
        "missing_symbols",
        "unexpected_symbols",
        "snapshot_coverage_pct",
        "complete_outcome_coverage_pct",
        "expected_symbols_complete",
        "snapshot_coverage_complete",
        "outcome_coverage_complete",
    )
    if any(set(row) != set(fieldnames) for row in rows):
        return False
    rendered_rows = [
        {
            key: (
                json.dumps(value, separators=(",", ":"))
                if isinstance(value, (list, dict))
                else value
            )
            for key, value in row.items()
        }
        for row in rows
    ]
    stream = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rendered_rows)
    expected = stream.getvalue().replace("\r\n", "\n")
    return path.read_text(encoding="utf-8") == expected


def _content_artifact_ref_valid(reference: str) -> bool:
    parts = reference.split(":", 2)
    if (
        len(parts) != 3
        or parts[0] != "sha256"
        or len(parts[1]) != 64
        or any(character not in "0123456789abcdef" for character in parts[1])
    ):
        return False
    path = Path(parts[2])
    if not path.is_file():
        return False
    try:
        payload = path.read_bytes()
    except OSError:
        return False
    if hashlib.sha256(payload).hexdigest() == parts[1]:
        return True
    try:
        decoded = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return _json_fingerprint(decoded) == parts[1]


def _candidate_universe_artifact_matches(
    denominator: CandidateUniverseDenominator,
) -> bool:
    if not _content_artifact_ref_valid(denominator.source_ref):
        return False
    parts = denominator.source_ref.split(":", 2)
    try:
        payload = json.loads(Path(parts[2]).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == "v2.mover_candidate_universe.v1"
        and payload.get("market_date") == denominator.market_date
        and payload.get("feature_cutoff_at")
        == denominator.feature_cutoff_at.isoformat()
        and payload.get("evidence_mode") == denominator.evidence_mode
        and payload.get("system_received_at")
        == (
            denominator.system_received_at.isoformat()
            if denominator.system_received_at is not None
            else None
        )
        and payload.get("universe_selection_method")
        == denominator.universe_selection_method
        and payload.get("expected_symbols") == list(denominator.expected_symbols)
        and payload.get("expected_symbols_complete")
        is denominator.expected_symbols_complete
        and payload.get("research_only") is True
        and payload.get("broker_execution_enabled") is False
    )


def _analysis_latest_valid(path: Path) -> bool:
    try:
        latest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(latest, dict):
            return False
        reports_directory = path.resolve().parent
        report_path = Path(str(latest.get("report_path") or "")).resolve()
        markdown_path = Path(str(latest.get("markdown_path") or "")).resolve()
        html_path = Path(str(latest.get("calendar_html_path") or "")).resolve()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        fingerprint = str(latest.get("analysis_fingerprint") or "")
        calendar = report.get("strategy_daily_calendar")
        if (
            latest.get("schema_version") != f"{SCHEMA_VERSION}.analysis_latest"
            or not isinstance(report, dict)
            or not isinstance(calendar, list)
            or len(fingerprint) != 64
            or _json_fingerprint(report) != fingerprint
            or report_path.parent != reports_directory
            or markdown_path.parent != reports_directory
            or html_path.parent != reports_directory
            or report_path.name
            != f"mover_pattern_analysis_{fingerprint[:16]}.json"
            or markdown_path.name
            != f"mover_pattern_analysis_{fingerprint[:16]}.md"
            or not html_path.name.startswith("strategy_calendar_")
            or html_path.suffix != ".html"
            or str(latest.get("report_sha256") or "")
            != _sha256_file(report_path)
            or str(latest.get("markdown_sha256") or "")
            != _sha256_file(markdown_path)
            or str(latest.get("calendar_html_sha256") or "")
            != _sha256_file(html_path)
            or not html_path.is_file()
            or str(report.get("strategy_daily_calendar_html_path") or "")
            != str(html_path)
            or report.get("research_only") is not True
            or report.get("broker_execution_enabled") is not False
        ):
            return False
        for row in calendar:
            if not isinstance(row, dict):
                return False
            if str(row.get("status") or "") in {
                "not_evaluated",
                "skipped",
                "incomplete",
            } and any(
                row.get(field) is not None
                for field in (
                    "paper_book_return_pct",
                    "pnl",
                    "capital_deployed",
                )
            ):
                return False
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return True


def _rows_match_by_id_artifacts(
    rows: list[dict[str, Any]],
    directory: Path,
    identity_field: str,
) -> bool:
    identities: set[str] = set()
    for row in rows:
        identity = str(row.get(identity_field) or "")
        if not identity or identity in identities:
            return False
        identities.add(identity)
        path = directory / f"{identity}.json"
        if not path.exists():
            return False
        try:
            retained = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if retained != row:
            return False
    return True


def verify(*, output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, Any]:
    """Verify manifests, evidence semantics, and the no-execution boundary."""

    paths = MoverLabPaths.create(output_root)
    checks: list[dict[str, Any]] = []
    catalog = strategy_catalog()
    catalog_payloads = {
        (row.strategy_id, row.version): row.to_dict() for row in catalog
    }
    checks.append(
        _check(
            "strategy_identity_unique",
            len({(row.strategy_id, row.version) for row in catalog}) == len(catalog),
        )
    )
    checks.append(
        _check(
            "all_strategies_forward_observation_only",
            all(
                str(getattr(row, "validation_status", "")).startswith(
                    "forward_"
                )
                for row in catalog
            ),
        )
    )
    checks.append(
        _check(
            "all_strategies_research_only",
            all(
                row.research_only and not row.broker_execution_enabled
                for row in catalog
            ),
        )
    )
    registry_path = paths.manifests / "strategy_registry.jsonl"
    registry_rows = _read_jsonl(registry_path)
    checks.append(
        _check(
            "strategy_registry_semantics_integrity",
            _versioned_registry_valid(
                registry_rows,
                identity_fields=("strategy_id", "version"),
            ),
            applicable=registry_path.exists(),
        )
    )
    checks.append(
        _check(
            "current_catalog_matches_registry",
            all(
                any(
                    str(prior.get("strategy_id")) == identity[0]
                    and str(prior.get("version")) == identity[1]
                    and prior.get("semantics_fingerprint")
                    == payload.get("semantics_fingerprint")
                    for prior in registry_rows
                )
                for identity, payload in catalog_payloads.items()
            ),
            applicable=registry_path.exists(),
        )
    )
    feature_registry_path = (
        paths.manifests / "feature_contract_registry.jsonl"
    )
    feature_registry_rows = _read_jsonl(feature_registry_path)
    checks.append(
        _check(
            "feature_contract_registry_semantics_integrity",
            _versioned_registry_valid(
                feature_registry_rows,
                identity_fields=("schema_version",),
            ),
            applicable=feature_registry_path.exists(),
        )
    )
    paper_manifest_count, paper_manifests_valid = _run_manifest_series_valid(
        paths.manifests,
        prefix="paper_scan",
        schema_version=f"{SCHEMA_VERSION}.paper_scan",
        artifact_hash_fields=(
            ("snapshots_path", "snapshots_sha256"),
            ("decisions_path", "decisions_sha256"),
            ("signals_path", "signals_sha256"),
        ),
        artifact_parent_fields=(
            (
                "snapshots_path",
                paths.source_artifacts / "paper_scan" / "snapshots",
            ),
            ("decisions_path", paths.decisions),
            ("signals_path", paths.signals),
        ),
    )
    checks.append(
        _check(
            "paper_scan_run_manifests_are_content_addressed",
            paper_manifests_valid,
            applicable=paper_manifest_count > 0,
        )
    )
    reconcile_manifest_count, reconcile_manifests_valid = (
        _run_manifest_series_valid(
            paths.manifests,
            prefix="reconcile",
            schema_version=f"{SCHEMA_VERSION}.reconcile",
            artifact_hash_fields=(
                ("signals_path", "signals_sha256"),
                ("bars_csv", "bars_csv_sha256"),
                ("trades_path", "trades_sha256"),
            ),
            artifact_parent_fields=(
                ("signals_path", paths.signals),
                (
                    "bars_csv",
                    paths.source_artifacts / "reconcile" / "bars",
                ),
                ("trades_path", paths.trades),
            ),
        )
    )
    checks.append(
        _check(
            "reconciliation_run_manifests_are_content_addressed",
            reconcile_manifests_valid,
            applicable=reconcile_manifest_count > 0,
        )
    )
    candidate_manifest_count, candidate_manifests_valid = (
        _candidate_study_manifests_valid(paths)
    )
    checks.append(
        _check(
            "candidate_studies_preserve_mode_and_artifact_lineage",
            candidate_manifests_valid,
            applicable=candidate_manifest_count > 0,
        )
    )
    analysis_latest_path = paths.reports / "mover_pattern_analysis_latest.json"
    checks.append(
        _check(
            "analysis_and_calendar_artifacts_are_truthful",
            _analysis_latest_valid(analysis_latest_path),
            applicable=analysis_latest_path.exists(),
        )
    )
    package_root = Path(__file__).resolve().parent
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package_root.glob("*.py")
    ).lower()
    forbidden = (
        "submit" + "_order",
        "place" + "_order",
        "create" + "_order",
        "broker" + "_client",
    )
    checks.append(
        _check(
            "no_broker_execution_code",
            not any(term in source for term in forbidden),
        )
    )
    snapshot_rows = _read_json_objects(paths.snapshots / "by_id")
    decision_rows = _read_json_objects(paths.decisions / "by_id")
    signal_rows = _read_json_objects(paths.signals / "by_id")
    session_signal_rows = _read_json_objects(paths.signals / "session_registry")
    trade_rows = _read_json_objects(paths.trades / "by_observation")
    snapshot_valid = _all_contract_rows_valid(
        snapshot_rows,
        ProspectiveMoverSnapshot.from_mapping,
    )
    signal_valid = _all_contract_rows_valid(
        signal_rows,
        MoverPaperSignal.from_mapping,
    )
    checks.append(
        _check(
            "snapshot_contract_and_cutoff_lineage",
            snapshot_valid,
            applicable=bool(snapshot_rows),
        )
    )
    checks.append(
        _check(
            "snapshot_ids_unique",
            _unique_nonblank(snapshot_rows, "snapshot_id"),
            applicable=bool(snapshot_rows),
        )
    )
    checks.append(
        _check(
            "snapshot_source_artifacts_match_hashes",
            _source_artifact_refs_valid(snapshot_rows)
            and all(_snapshot_identity_valid(row) for row in snapshot_rows)
            and all(_forward_receipt_valid(row) for row in snapshot_rows)
            and all(
                _forward_universe_artifact_valid(row) for row in snapshot_rows
            ),
            applicable=bool(snapshot_rows),
        )
    )
    checks.append(
        _check(
            "paper_signal_contracts_valid",
            signal_valid,
            applicable=bool(signal_rows),
        )
    )
    checks.append(
        _check(
            "paper_signals_reference_retained_snapshots",
            _signals_reference_snapshots(signal_rows, snapshot_rows),
            applicable=bool(signal_rows),
        )
    )
    checks.append(
        _check(
            "decision_ids_unique_and_research_only",
            _unique_nonblank(decision_rows, "decision_id")
            and all(
                row.get("research_only") is True
                and row.get("broker_execution_enabled") is False
                for row in decision_rows
            ),
            applicable=bool(decision_rows),
        )
    )
    checks.append(
        _check(
            "decision_signals_have_immutable_signal_artifacts",
            {
                str(row.get("signal", {}).get("signal_id") or "")
                for row in decision_rows
                if isinstance(row.get("signal"), dict)
            }
            <= {str(row.get("signal_id") or "") for row in signal_rows},
            applicable=any(
                isinstance(row.get("signal"), dict) for row in decision_rows
            ),
        )
    )
    checks.append(
        _check(
            "paper_signal_ids_unique",
            _unique_nonblank(signal_rows, "signal_id"),
            applicable=bool(signal_rows),
        )
    )
    checks.append(
        _check(
            "one_paper_signal_per_strategy_symbol_session",
            _session_signal_registry_valid(session_signal_rows, signal_rows),
            applicable=bool(signal_rows) or bool(session_signal_rows),
        )
    )
    checks.append(
        _check(
            "paper_signals_match_frozen_strategy_semantics",
            all(
                (
                    str(row.get("strategy_id") or ""),
                    str(row.get("strategy_version") or ""),
                )
                in catalog_payloads
                and row.get("strategy_semantics_fingerprint")
                == catalog_payloads[
                    (
                        str(row.get("strategy_id") or ""),
                        str(row.get("strategy_version") or ""),
                    )
                ]["semantics_fingerprint"]
                for row in signal_rows
            ),
            applicable=bool(signal_rows),
        )
    )
    checks.append(
        _check(
            "trade_observation_ids_unique",
            _unique_nonblank(trade_rows, "observation_id"),
            applicable=bool(trade_rows),
        )
    )
    checks.append(
        _check(
            "trade_observations_reference_immutable_signals",
            {str(row.get("signal_id") or "") for row in trade_rows}
            <= {str(row.get("signal_id") or "") for row in signal_rows},
            applicable=bool(trade_rows),
        )
    )
    checks.append(
        _check(
            "missing_returns_are_null",
            all(
                row.get("net_return_pct") is None
                for row in trade_rows
                if row.get("status") != "closed"
            ),
            applicable=bool(trade_rows),
        )
    )
    checks.append(
        _check(
            "closed_trades_have_source_and_costs",
            all(
                row.get("entry_source_bar_at")
                and row.get("exit_source_bar_at")
                and row.get("bars_evidence_sha256")
                and row.get("total_cost") is not None
                and row.get("source_bar_sequence_complete") is True
                for row in trade_rows
                if row.get("status") == "closed"
            ),
            applicable=any(row.get("status") == "closed" for row in trade_rows),
        )
    )
    checks.append(
        _check(
            "closed_trade_math_recomputes",
            all(
                _closed_trade_math_valid(row)
                for row in trade_rows
                if row.get("status") == "closed"
            ),
            applicable=any(row.get("status") == "closed" for row in trade_rows),
        )
    )
    checks.append(
        _check(
            "closed_trade_timestamps_and_session_valid",
            all(
                _closed_trade_timestamps_valid(row)
                for row in trade_rows
                if row.get("status") == "closed"
            ),
            applicable=any(row.get("status") == "closed" for row in trade_rows),
        )
    )
    checks.append(
        _check(
            "trade_bar_evidence_hashes_match",
            _trade_evidence_valid(trade_rows),
            applicable=any(row.get("bars_evidence_sha256") for row in trade_rows),
        )
    )
    failed = [
        row["check"]
        for row in checks
        if row.get("applicable", True) and row.get("passed") is not True
    ]
    closed_count = sum(1 for row in trade_rows if row.get("status") == "closed")
    pending_count = sum(
        1
        for row in trade_rows
        if str(row.get("status") or "").startswith("pending_")
    )
    evidence_status = (
        "not_available"
        if not trade_rows
        else "incomplete"
        if pending_count
        else "integrity_verified"
    )
    payload = {
        "schema_version": f"{SCHEMA_VERSION}.verify",
        "status": "passed" if not failed else "failed",
        "checks": checks,
        "failed_checks": failed,
        "snapshot_evidence_count": len(snapshot_rows),
        "decision_evidence_count": len(decision_rows),
        "signal_evidence_count": len(signal_rows),
        "trade_observation_count": len(trade_rows),
        "closed_trade_count": closed_count,
        "pending_trade_count": pending_count,
        "evidence_status": evidence_status,
        "performance_claim_eligible": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    _write_json(paths.qa / "verify_latest.json", payload)
    return payload


def _read_context_rows(
    path: Path | None,
) -> dict[tuple[str, str], tuple[dict[str, Any], ...]]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("context CSV is empty")
        forbidden = FORBIDDEN_SNAPSHOT_FIELDS & {
            str(name).strip().lower() for name in reader.fieldnames
        }
        if forbidden:
            raise ValueError(
                "context CSV contains future/outcome fields: "
                + ", ".join(sorted(forbidden))
            )
        rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        seen: set[tuple[str, str, str]] = set()
        for raw in reader:
            market_date = str(raw.get("market_date") or raw.get("date") or "")[:10]
            symbol = str(raw.get("symbol") or raw.get("ticker") or "").upper()
            if not market_date or not symbol:
                raise ValueError("context rows require market_date and symbol")
            observed_at = _aware_datetime(raw.get("context_observed_at"))
            if observed_at.astimezone(MARKET_TZ).date().isoformat() != market_date:
                raise ValueError(
                    "context_observed_at must match market_date in America/New_York"
                )
            key = (market_date, symbol)
            identity = (market_date, symbol, observed_at.isoformat())
            if identity in seen:
                raise ValueError(
                    "duplicate context observation: " + ":".join(identity)
                )
            seen.add(identity)
            rows[key].append(dict(raw))
        return {
            key: tuple(
                sorted(
                    values,
                    key=lambda row: _aware_datetime(
                        row.get("context_observed_at")
                    ),
                )
            )
            for key, values in rows.items()
        }


def _context_at_cutoff(
    rows: tuple[dict[str, Any], ...],
    cutoff_at: datetime,
) -> dict[str, Any] | None:
    eligible = [
        row
        for row in rows
        if _aware_datetime(row.get("context_observed_at")) <= cutoff_at
    ]
    return eligible[-1] if eligible else None


def _rth_bars_by_day(
    bars: tuple[MarketBar, ...],
) -> dict[date, list[MarketBar]]:
    grouped: dict[date, list[MarketBar]] = defaultdict(list)
    for bar in sorted(bars, key=lambda item: item.timestamp):
        local = bar.timestamp.astimezone(MARKET_TZ)
        if RTH_START <= local.time() <= RTH_END:
            grouped[local.date()].append(bar)
    return grouped


def _previous_market_session(session_day: date) -> date:
    candidate = session_day - timedelta(days=1)
    while not market_session(candidate).is_trading_day:
        candidate -= timedelta(days=1)
    return candidate


def _validate_csv_timestamp_awareness(path: Path) -> None:
    """Reject timestamps the shared loader would otherwise coerce to UTC."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = {str(field or "").strip().lower() for field in reader.fieldnames or ()}
        if "timestamp" not in fields:
            raise ValueError("bars CSV requires a timestamp column")
        for row_number, row in enumerate(reader, start=2):
            raw = str(row.get("timestamp") or "").strip()
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"bars CSV row {row_number} has an invalid ISO timestamp"
                ) from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(
                    f"bars CSV row {row_number} timestamp must include a timezone"
                )


def _validate_market_bars(
    bars_by_symbol: dict[str, tuple[MarketBar, ...]],
) -> None:
    if not bars_by_symbol:
        raise ValueError("bars CSV contains no valid OHLCV rows")
    for symbol, bars in sorted(bars_by_symbol.items()):
        seen: set[datetime] = set()
        for bar in bars:
            if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
                raise ValueError(f"{symbol}: bar timestamp must include timezone")
            if bar.timestamp in seen:
                raise ValueError(
                    f"{symbol}: duplicate bar timestamp {bar.timestamp.isoformat()}"
                )
            seen.add(bar.timestamp)
            if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                raise ValueError(f"{symbol}: non-positive OHLC value")
            if bar.high < max(bar.open, bar.close, bar.low):
                raise ValueError(f"{symbol}: bar high is internally inconsistent")
            if bar.low > min(bar.open, bar.close, bar.high):
                raise ValueError(f"{symbol}: bar low is internally inconsistent")
            if bar.volume <= 0:
                raise ValueError(f"{symbol}: zero or negative volume is not executable")


def _session_bars_complete(bars: list[MarketBar], session_day: date) -> bool:
    if not bars:
        return False
    session = market_session(session_day)
    if not session.is_trading_day or session.close_time_et is None:
        return False
    close_clock = time.fromisoformat(session.close_time_et)
    return bars[-1].timestamp.astimezone(MARKET_TZ).time() == close_clock


def _bar_grid_complete_through(
    bars: list[MarketBar],
    cutoff_clock: time,
    *,
    interval_minutes: int,
) -> bool:
    if not bars:
        return False
    session_day = bars[0].timestamp.astimezone(MARKET_TZ).date()
    expected_at = datetime.combine(session_day, RTH_START, tzinfo=MARKET_TZ)
    expected: set[datetime] = set()
    step = timedelta(minutes=interval_minutes)
    cutoff_at = datetime.combine(session_day, cutoff_clock, tzinfo=MARKET_TZ)
    expected_at += step
    while expected_at <= cutoff_at:
        expected.add(expected_at)
        expected_at += step
    actual = {
        bar.timestamp.astimezone(MARKET_TZ)
        for bar in bars
        if bar.timestamp.astimezone(MARKET_TZ) <= cutoff_at
    }
    return bool(expected) and actual == expected


def _opening_range_complete(
    bars: list[MarketBar],
    *,
    cutoff_at: datetime,
    interval_minutes: int,
) -> bool:
    if cutoff_at.astimezone(MARKET_TZ).time() < OPENING_RANGE_END:
        return False
    return _bar_grid_complete_through(
        bars,
        OPENING_RANGE_END,
        interval_minutes=interval_minutes,
    )


def _market_bar_payload(bar: MarketBar) -> dict[str, Any]:
    return {
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _running_vwap(bars: list[MarketBar]) -> float | None:
    volume = sum(bar.volume for bar in bars)
    if volume <= 0:
        return None
    return sum(_typical_price(bar) * bar.volume for bar in bars) / volume


def _typical_price(bar: MarketBar) -> float:
    return (bar.high + bar.low + bar.close) / 3.0


def _eligible_entry_open_at(
    *,
    signal_at: datetime,
    source_captured_at: datetime | None,
    bar_step: timedelta,
) -> datetime:
    """Return the first bar-grid open that is not earlier than available truth."""

    if source_captured_at is None or source_captured_at <= signal_at:
        return signal_at
    elapsed_seconds = (source_captured_at - signal_at).total_seconds()
    step_seconds = bar_step.total_seconds()
    steps = math.ceil(elapsed_seconds / step_seconds)
    return signal_at + steps * bar_step


def _reconcile_one(
    signal: dict[str, Any],
    bars: tuple[MarketBar, ...],
    *,
    notional_per_trade: float,
    slippage_bps: float,
    fee_bps: float,
    bar_interval_minutes: int,
    bars_source: str,
    evidence_root: Path,
) -> dict[str, Any]:
    signal_id = str(signal.get("signal_id") or "")
    symbol = str(signal.get("symbol") or "").upper()
    market_date = str(signal.get("market_date") or "")
    signal_at = _aware_datetime(signal.get("signal_at"))
    session = market_session(date.fromisoformat(market_date))
    if not session.is_trading_day or session.close_time_et is None:
        raise ValueError(f"signal market_date is not a trading session: {market_date}")
    session_close = time.fromisoformat(session.close_time_et)
    session_close_at = datetime.combine(
        date.fromisoformat(market_date),
        session_close,
        tzinfo=MARKET_TZ,
    )
    bar_step = timedelta(minutes=bar_interval_minutes)
    source_captured_at = (
        _aware_datetime(signal.get("source_captured_at"))
        if signal.get("evidence_mode") == "forward_observation"
        else None
    )
    eligible_entry_at = _eligible_entry_open_at(
        signal_at=signal_at,
        source_captured_at=source_captured_at,
        bar_step=bar_step,
    )
    expected_first_at = eligible_entry_at + bar_step
    source_bars = [
        bar
        for bar in sorted(bars, key=lambda item: item.timestamp)
        if bar.timestamp.astimezone(MARKET_TZ).date().isoformat() == market_date
        and bar.timestamp >= expected_first_at
        and RTH_START
        <= bar.timestamp.astimezone(MARKET_TZ).time()
        <= session_close
    ]
    base = {
        "schema_version": "v2.mover_paper_trade.v1",
        "trade_id": _stable_id("mover_trade", signal_id),
        "signal_id": signal_id,
        "snapshot_id": signal.get("snapshot_id"),
        "signal_artifact_ref": signal.get("signal_artifact_ref"),
        "snapshot_artifact_ref": signal.get("snapshot_artifact_ref"),
        "strategy_id": signal.get("strategy_id"),
        "strategy_version": signal.get("strategy_version"),
        "strategy_semantics_fingerprint": signal.get(
            "strategy_semantics_fingerprint"
        ),
        "market_date": market_date,
        "symbol": symbol,
        "direction": "long",
        "signal_at": signal_at.isoformat(),
        "evidence_mode": signal.get("evidence_mode") or "historical_replay",
        "source_captured_at": signal.get("source_captured_at"),
        "system_received_at": signal.get("system_received_at"),
        "forward_receipt_ref": signal.get("forward_receipt_ref"),
        "signal_entry_reference": signal.get("entry_reference"),
        "eligible_entry_at": eligible_entry_at.isoformat(),
        "session_close_at": session_close_at.isoformat(),
        "bar_interval_minutes": bar_interval_minutes,
        "bar_timestamp_semantics": "bar_close",
        "entry_fill_policy": "next_bar_open",
        "entry_time_semantics": (
            "source bar open derived as source bar close minus bar interval"
        ),
        "intrabar_ambiguity_policy": "stop_first",
        "notional_per_trade": notional_per_trade,
        "slippage_bps": slippage_bps,
        "fee_bps": fee_bps,
        "bars_source": bars_source,
        "research_only": True,
        "broker_execution_enabled": False,
        "features": signal.get("features") or {},
        "source_refs": signal.get("source_refs") or [],
    }

    def with_evidence(
        payload: dict[str, Any],
        evidence_bars: list[MarketBar],
    ) -> dict[str, Any]:
        evidence_payload = [_market_bar_payload(bar) for bar in evidence_bars]
        if evidence_payload:
            evidence_sha256 = _json_fingerprint(evidence_payload)
            evidence_path = evidence_root / f"{evidence_sha256}.json"
            _write_immutable_json(evidence_path, evidence_payload)
            evidence_ref = f"sha256:{evidence_sha256}:{evidence_path.resolve()}"
        else:
            evidence_sha256 = None
            evidence_path = None
            evidence_ref = None
        base_source_refs = base.get("source_refs")
        source_refs = (
            [str(item) for item in base_source_refs if str(item)]
            if isinstance(base_source_refs, list)
            else []
        )
        if evidence_ref:
            source_refs.append(evidence_ref)
        return {
            **base,
            **payload,
            "source_coverage_complete": (
                payload.get("status") == "closed"
                and payload.get("source_bar_sequence_complete") is True
            ),
            "bars_evidence_sha256": evidence_sha256,
            "bars_evidence_path": (
                str(evidence_path.resolve()) if evidence_path else None
            ),
            "source_refs": list(dict.fromkeys(source_refs)),
        }

    if not source_bars:
        return with_evidence(
            {
                "status": "pending_missing_outcome",
                "reason": "no_next_completed_same_session_bar",
                "expected_next_bar_at": expected_first_at.isoformat(),
                "entry_at": None,
                "exit_at": None,
                "entry_price": None,
                "exit_price": None,
                "gross_return_pct": None,
                "net_return_pct": None,
                "pnl": None,
                "total_cost": None,
                "entry_source_bar_at": None,
                "exit_source_bar_at": None,
                "source_bar_sequence_complete": False,
            },
            [],
        )
    first = source_bars[0]
    first_local = first.timestamp.astimezone(MARKET_TZ)
    if first_local != expected_first_at:
        return with_evidence(
            {
                "status": "pending_missing_outcome",
                "reason": "next_expected_bar_missing_or_misaligned",
                "expected_next_bar_at": expected_first_at.isoformat(),
                "first_available_bar_at": first_local.isoformat(),
                "entry_at": None,
                "exit_at": None,
                "entry_price": None,
                "exit_price": None,
                "gross_return_pct": None,
                "net_return_pct": None,
                "pnl": None,
                "total_cost": None,
                "entry_source_bar_at": None,
                "exit_source_bar_at": None,
                "source_bar_sequence_complete": False,
            },
            [first],
        )
    rate = slippage_bps / 10_000.0
    fee_rate = fee_bps / 10_000.0
    stop = _required_float(signal.get("stop"), "signal stop")
    target = _required_float(signal.get("target"), "signal target")
    entry_price = first.open * (1.0 + rate)
    entry_at = first.timestamp - bar_step
    if source_captured_at is not None and entry_at < source_captured_at:
        raise ValueError("forward paper entry cannot predate source capture")
    if not stop < entry_price < target:
        return with_evidence(
            {
                "status": "not_entered",
                "reason": (
                    "next_bar_fill_at_or_below_stop"
                    if entry_price <= stop
                    else "next_bar_fill_at_or_above_target"
                ),
                "entry_at": None,
                "exit_at": None,
                "entry_price": None,
                "exit_price": None,
                "gross_return_pct": None,
                "net_return_pct": None,
                "pnl": None,
                "total_cost": None,
                "entry_source_bar_at": first.timestamp.isoformat(),
                "exit_source_bar_at": None,
                "next_bar_open": first.open,
                "stop": stop,
                "target": target,
                "source_bar_sequence_complete": True,
            },
            [first],
        )
    quantity = notional_per_trade / entry_price
    exit_bar: MarketBar | None = None
    exit_reference: float | None = None
    reason: str | None = None
    contiguous_bars: list[MarketBar] = []
    expected_at = expected_first_at
    missing_expected_at: datetime | None = None
    for bar in source_bars:
        bar_local = bar.timestamp.astimezone(MARKET_TZ)
        if bar_local != expected_at:
            missing_expected_at = expected_at
            break
        contiguous_bars.append(bar)
        stop_touched = bar.low <= stop
        target_touched = bar.high >= target
        if stop_touched:
            exit_bar = bar
            exit_reference = min(stop, bar.open)
            reason = "stop_gap" if bar.open < stop else "stop"
            break
        if target_touched:
            exit_bar = bar
            exit_reference = target
            reason = "target"
            break
        expected_at += bar_step
    terminal_bar_present = bool(
        contiguous_bars
        and contiguous_bars[-1].timestamp.astimezone(MARKET_TZ)
        == session_close_at
    )
    if exit_bar is None and terminal_bar_present:
        exit_bar = contiguous_bars[-1]
        exit_reference = exit_bar.close
        reason = "eod_flat"
    if exit_bar is None or exit_reference is None or reason is None:
        entry_fee = notional_per_trade * fee_rate
        return with_evidence(
            {
                "status": "pending_incomplete_session_bars",
                "reason": "no_stop_or_target_and_incomplete_session_grid",
                "missing_expected_bar_at": (
                    (missing_expected_at or expected_at).isoformat()
                ),
                "entry_at": entry_at.isoformat(),
                "exit_at": None,
                "entry_reference": first.open,
                "entry_price": round(entry_price, 8),
                "exit_price": None,
                "stop": stop,
                "target": target,
                "quantity": round(quantity, 8),
                "gross_return_pct": None,
                "net_return_pct": None,
                "pnl": None,
                "entry_fee": round(entry_fee, 6),
                "exit_fee": None,
                "total_cost": None,
                "cost_incurred_to_date": round(
                    entry_fee + quantity * (entry_price - first.open),
                    6,
                ),
                "entry_source_bar_at": first.timestamp.isoformat(),
                "exit_source_bar_at": None,
                "source_bar_sequence_complete": False,
            },
            contiguous_bars,
        )
    exit_price = exit_reference * (1.0 - rate)
    entry_fee = quantity * entry_price * fee_rate
    exit_fee = quantity * exit_price * fee_rate
    fee_cost = entry_fee + exit_fee
    slippage_cost = quantity * (
        (entry_price - first.open) + (exit_reference - exit_price)
    )
    total_cost = fee_cost + slippage_cost
    reference_gross_pnl = quantity * (exit_reference - first.open)
    fill_pnl = quantity * (exit_price - entry_price)
    net_pnl = fill_pnl - fee_cost
    gross_return = reference_gross_pnl / notional_per_trade * 100.0
    fill_return = fill_pnl / notional_per_trade * 100.0
    net_return = net_pnl / notional_per_trade * 100.0
    high_water = max(bar.high for bar in contiguous_bars)
    low_water = min(bar.low for bar in contiguous_bars)
    exit_window_start_at = exit_bar.timestamp - bar_step
    exit_time_status = (
        "exact_session_close"
        if reason == "eod_flat"
        else "interval_censored_within_source_bar"
    )
    return with_evidence(
        {
            "status": "closed",
            "reason": reason,
            "entry_at": entry_at.isoformat(),
            "exit_at": exit_bar.timestamp.isoformat(),
            "exit_at_semantics": (
                "official session close"
                if reason == "eod_flat"
                else "source bar close observation time; exact intrabar touch time unknown"
            ),
            "exit_time_status": exit_time_status,
            "exit_window_start_at": exit_window_start_at.isoformat(),
            "exit_window_end_at": exit_bar.timestamp.isoformat(),
            "entry_source_bar_at": first.timestamp.isoformat(),
            "exit_source_bar_at": exit_bar.timestamp.isoformat(),
            "entry_reference": first.open,
            "exit_reference": exit_reference,
            "entry_price": round(entry_price, 8),
            "exit_price": round(exit_price, 8),
            "stop": stop,
            "target": target,
            "quantity": round(quantity, 8),
            "gross_return_pct": round(gross_return, 6),
            "fill_return_pct": round(fill_return, 6),
            "net_return_pct": round(net_return, 6),
            "reference_gross_pnl": round(reference_gross_pnl, 6),
            "pnl": round(net_pnl, 6),
            "entry_fee": round(entry_fee, 6),
            "exit_fee": round(exit_fee, 6),
            "fee_cost": round(fee_cost, 6),
            "slippage_cost": round(slippage_cost, 6),
            "total_cost": round(total_cost, 6),
            "mfe_pct": round((high_water / entry_price - 1.0) * 100.0, 6),
            "mae_pct": round((low_water / entry_price - 1.0) * 100.0, 6),
            "path_metric_semantics": (
                "bar_extremes_through_exit_inclusive_order_unknown"
            ),
            "source_bar_sequence_complete": True,
            "same_session": True,
        },
        contiguous_bars,
    )


def _return_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [
        float(row["net_return_pct"])
        for row in rows
        if _optional_float(row.get("net_return_pct")) is not None
    ]
    pnls = [
        float(row["pnl"])
        for row in rows
        if _optional_float(row.get("pnl")) is not None
    ]
    if not returns:
        return {
            "sample_size": 0,
            "mean_net_return_pct": None,
            "median_net_return_pct": None,
            "win_rate_pct": None,
            "profit_factor": None,
            "total_pnl": None,
            "max_drawdown": None,
            "mean_return_lower_95_pct": None,
        }
    standard_error = (
        _sample_standard_deviation(returns) / math.sqrt(len(returns))
        if len(returns) > 1
        else None
    )
    lower = mean(returns) - 1.96 * standard_error if standard_error is not None else None
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = abs(sum(value for value in pnls if value < 0))
    return {
        "sample_size": len(returns),
        "mean_net_return_pct": round(mean(returns), 6),
        "median_net_return_pct": round(float(median(returns)), 6),
        "win_rate_pct": round(
            sum(1 for value in returns if value > 0) / len(returns) * 100.0,
            4,
        ),
        "profit_factor": (
            round(gross_profit / gross_loss, 6)
            if gross_loss > 0
            else None
        ),
        "total_pnl": round(sum(pnls), 6),
        "max_drawdown": round(_max_drawdown(pnls), 6),
        "mean_return_lower_95_pct": round(lower, 6) if lower is not None else None,
    }


def _daily_strategy_calendar(
    trades: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    expected_market_dates: Iterable[str],
) -> list[dict[str, Any]]:
    CalendarKey = tuple[str, str, str, str]
    trade_groups: dict[CalendarKey, list[dict[str, Any]]] = defaultdict(list)
    decision_groups: dict[
        CalendarKey, list[dict[str, Any]]
    ] = defaultdict(list)
    for row in trades:
        key = (
            str(row.get("market_date") or ""),
            str(row.get("strategy_id") or "unknown"),
            str(row.get("strategy_version") or "unknown"),
            str(row.get("evidence_mode") or "historical_replay"),
        )
        trade_groups[key].append(row)
    for row in decisions:
        key = (
            str(row.get("market_date") or ""),
            str(row.get("strategy_id") or "unknown"),
            str(row.get("strategy_version") or "unknown"),
            str(row.get("evidence_mode") or "historical_replay"),
        )
        decision_groups[key].append(row)
    expected_keys: set[CalendarKey] = {
        (
            market_date,
            spec.strategy_id,
            spec.version,
            "forward_observation",
        )
        for market_date in expected_market_dates
        for spec in strategy_catalog()
    }
    output: list[dict[str, Any]] = []
    for key in sorted(set(trade_groups) | set(decision_groups) | expected_keys):
        day_trades = trade_groups.get(key, [])
        day_decisions = decision_groups.get(key, [])
        closed = [row for row in day_trades if row.get("status") == "closed"]
        pending = [
            row
            for row in day_trades
            if str(row.get("status") or "").startswith("pending_")
        ]
        not_entered = [
            row for row in day_trades if row.get("status") == "not_entered"
        ]
        signal_ids = {
            str(row.get("signal", {}).get("signal_id") or "")
            for row in day_decisions
            if isinstance(row.get("signal"), dict)
        } | {
            str(row.get("signal_id") or "")
            for row in day_trades
            if row.get("signal_id")
        }
        skip_reasons = {
            str(row.get("reason") or "")
            for row in day_decisions
            if row.get("decision") == "skipped"
        }
        missing_truth = any(
            bool(row.get("missing_features"))
            or row.get("reason")
            in {
                "required_point_in_time_truth_missing",
                "catalyst_lineage_missing",
            }
            for row in day_decisions
        )
        if not day_decisions and not day_trades:
            status = "not_evaluated"
            book_return = None
            pnl = None
            deployed = None
            semantics = "no complete strategy evaluation was retained"
        elif missing_truth:
            status = "not_evaluated"
            book_return = None
            pnl = None
            deployed = None
            semantics = "required point-in-time truth was missing"
        elif skip_reasons:
            status = "not_evaluated"
            book_return = None
            pnl = None
            deployed = None
            semantics = (
                "strategy version was not active for forward observation"
                if skip_reasons
                == {"strategy_not_active_for_forward_observation"}
                else "evaluation was retained but intentionally suppressed"
            )
        elif pending or (signal_ids and len(day_trades) < len(signal_ids)):
            status = "incomplete"
            book_return = None
            pnl = None
            deployed = None
            semantics = "missing outcomes remain null"
        elif closed:
            status = "complete"
            pnl = sum(float(row.get("pnl") or 0.0) for row in closed)
            deployed = sum(
                float(row.get("notional_per_trade") or 0.0) for row in closed
            )
            book_return = pnl / deployed * 100.0 if deployed > 0 else None
            semantics = "after-cost PnL divided by capital deployed in closed trades"
        elif signal_ids and not_entered:
            status = "resolved_no_entry"
            pnl = 0.0
            deployed = 0.0
            book_return = 0.0
            semantics = "signal invalidated before fill; no capital deployed and no PnL"
        else:
            status = "no_setup"
            pnl = 0.0
            deployed = 0.0
            book_return = 0.0
            semantics = "strategy evaluated and remained in cash"
        output.append(
            {
                "market_date": key[0],
                "strategy_id": key[1],
                "strategy_version": key[2],
                "evidence_mode": key[3],
                "status": status,
                "paper_book_return_pct": (
                    round(book_return, 6) if book_return is not None else None
                ),
                "pnl": round(pnl, 6) if pnl is not None else None,
                "capital_deployed": (
                    round(deployed, 6) if deployed is not None else None
                ),
                "decision_count": len(day_decisions),
                "signal_count": len(signal_ids),
                "closed_trade_count": len(closed),
                "pending_trade_count": len(pending),
                "not_entered_count": len(not_entered),
                "symbols": sorted(
                    {
                        str(row.get("symbol") or "")
                        for row in day_trades
                        if row.get("symbol")
                    }
                    | {
                        str(row.get("symbol") or "")
                        for row in day_decisions
                        if row.get("symbol")
                    }
                ),
                "return_semantics": semantics,
                "learning_eligible": (
                    status == "complete" and key[3] == "forward_observation"
                ),
            }
        )
    return output


def _strategy_day_book_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        market_date = str(row.get("market_date") or "")
        if market_date:
            grouped[market_date].append(row)
    output: list[dict[str, Any]] = []
    for market_date, day_rows in sorted(grouped.items()):
        deployed = sum(
            float(row.get("notional_per_trade") or 0.0) for row in day_rows
        )
        pnl = sum(float(row.get("pnl") or 0.0) for row in day_rows)
        if deployed <= 0:
            continue
        output.append(
            {
                "market_date": market_date,
                "net_return_pct": pnl / deployed * 100.0,
                "pnl": pnl,
            }
        )
    return output


def _chronological_splits(
    rows: list[dict[str, Any]],
    *,
    spec: MoverStrategySpec | None,
) -> dict[str, Any]:
    if not rows or spec is None:
        return {
            "discovery": {**_return_metrics([]), "session_count": 0},
            "validation": {**_return_metrics([]), "session_count": 0},
            "locked_test": {**_return_metrics([]), "session_count": 0},
        }
    output: dict[str, Any] = {}
    for name in ("discovery", "validation", "locked_test"):
        selected = [
            row
            for row in rows
            if _immutable_split_name(
                spec,
                str(row.get("market_date") or ""),
            )
            == name
        ]
        dates = {
            str(row.get("market_date") or "")
            for row in selected
            if row.get("market_date")
        }
        output[name] = {
            **_return_metrics(_strategy_day_book_rows(selected)),
            "session_count": len(dates),
            "start_date": min(dates) if dates else None,
            "end_date": max(dates) if dates else None,
        }
    return output


def _forward_strategy_day_fully_evaluated(
    spec: MoverStrategySpec,
    market_date: str,
    decisions: list[dict[str, Any]],
) -> bool:
    if not decisions or _immutable_split_name(spec, market_date) in {
        "pre_activation",
        "not_a_session",
    }:
        return False
    missing_truth_reasons = {
        "required_point_in_time_truth_missing",
        "catalyst_lineage_missing",
        "strategy_not_active_for_forward_observation",
    }
    return all(
        row.get("evidence_mode") == "forward_observation"
        and row.get("research_only") is True
        and row.get("broker_execution_enabled") is False
        and str(row.get("reason") or "") not in missing_truth_reasons
        and not row.get("missing_features")
        for row in decisions
    )


def _immutable_split_name(spec: MoverStrategySpec, market_date: str) -> str:
    target = date.fromisoformat(market_date)
    activation = date.fromisoformat(
        str(spec.parameters["activation_market_date"])
    )
    if target < activation:
        return "pre_activation"
    session_index = 0
    current = activation
    while current < target:
        if market_session(current).is_trading_day:
            session_index += 1
        current += timedelta(days=1)
    if not market_session(target).is_trading_day:
        return "not_a_session"
    discovery_count = int(spec.parameters["discovery_session_count"])
    validation_count = int(spec.parameters["validation_session_count"])
    locked_count = int(spec.parameters["locked_test_session_count"])
    if session_index < discovery_count:
        return "discovery"
    if session_index < discovery_count + validation_count:
        return "validation"
    if session_index < discovery_count + validation_count + locked_count:
        return "locked_test"
    return "walk_forward"


def _chronological_date_partitions(
    rows: list[dict[str, Any]],
) -> dict[str, set[str]]:
    dates = sorted(
        {
            str(row.get("market_date") or "")
            for row in rows
            if row.get("market_date")
        }
    )
    if not dates:
        return {"discovery": set(), "validation": set(), "locked_test": set()}
    count = len(dates)
    discovery_end = max(1, int(count * 0.60))
    validation_end = max(discovery_end, int(count * 0.80))
    if count >= 3:
        discovery_end = min(discovery_end, count - 2)
        validation_end = min(
            max(validation_end, discovery_end + 1),
            count - 1,
        )
    return {
        "discovery": set(dates[:discovery_end]),
        "validation": set(dates[discovery_end:validation_end]),
        "locked_test": set(dates[validation_end:]),
    }


def _split_gate(splits: dict[str, Any]) -> bool:
    return all(
        (splits[name].get("mean_net_return_pct") or 0.0) > 0
        and int(splits[name].get("sample_size") or 0) >= 5
        for name in ("validation", "locked_test")
    )


def _feature_correlations(
    rows: list[dict[str, Any]],
    *,
    spec: MoverStrategySpec | None,
) -> tuple[list[dict[str, Any]], int]:
    if not rows or spec is None:
        return [], 0
    feature_values: dict[str, list[tuple[float, float]]] = defaultdict(list)
    discovery_rows = [
        row
        for row in sorted(
            rows,
            key=lambda row: (
                str(row.get("market_date") or ""),
                str(row.get("entry_at") or ""),
            ),
        )
        if _immutable_split_name(
            spec,
            str(row.get("market_date") or ""),
        )
        == "discovery"
    ]
    for row in discovery_rows:
        target = _optional_float(row.get("net_return_pct"))
        if target is None:
            continue
        features = row.get("features")
        if not isinstance(features, dict):
            continue
        for key, raw in features.items():
            value = _optional_float(raw)
            if value is not None and not isinstance(raw, bool):
                feature_values[str(key)].append((value, target))
    tested = len(feature_values)
    alpha = 0.05 / tested if tested else None
    results: list[dict[str, Any]] = []
    for feature, pairs in sorted(feature_values.items()):
        if len(pairs) < 10:
            continue
        x = [pair[0] for pair in pairs]
        y = [pair[1] for pair in pairs]
        rho = _spearman(x, y)
        p_value = _approx_correlation_p_value(rho, len(pairs))
        results.append(
            {
                "feature": feature,
                "sample_size": len(pairs),
                "discovery_spearman": round(rho, 6),
                "approx_p_value": round(p_value, 8),
                "bonferroni_alpha": round(alpha, 8) if alpha is not None else None,
                "passes_multiple_testing_gate": (
                    alpha is not None and p_value < alpha
                ),
                "validation_status": "discovery_only_not_a_strategy",
            }
        )
    return results, tested


def _analysis_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Mover Pattern Lab Analysis",
        "",
        f"- Closed forward paper trades: `{payload.get('closed_trade_count')}`",
        (
            "- Pending or missing outcomes: "
            f"`{payload.get('pending_or_missing_count')}`"
        ),
        "- Automatic promotion: `disabled`",
        "",
        "| Strategy | Status | Trades | Mean net | Win rate | Coverage |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in payload.get("strategy_results") or []:
        metrics = row.get("metrics") or {}
        lines.append(
            "| {strategy_id} | {status} | {sample} | {mean} | {win} | {coverage}% |".format(
                strategy_id=row.get("strategy_id"),
                status=row.get("status"),
                sample=metrics.get("sample_size"),
                mean=_display_pct(metrics.get("mean_net_return_pct")),
                win=_display_pct(metrics.get("win_rate_pct")),
                coverage=row.get("coverage_pct"),
            )
        )
    lines.extend(
        [
            "",
            "No result in this report is a return guarantee or personalized investment advice.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _write_immutable_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError(f"immutable artifact conflict: {path}")
        return
    path.write_text(text, encoding="utf-8")


def _retain_raw_input(source: Path, directory: Path) -> tuple[str, Path]:
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    suffix = source.suffix.lower() or ".bin"
    target = directory / f"{digest}{suffix}"
    directory.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != payload:
            raise ValueError(f"immutable raw input conflict: {target}")
    else:
        target.write_bytes(payload)
    return f"sha256:{digest}:{target.resolve()}", target.resolve()


def _retained_json_artifact_ref(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"retained JSON artifact must be an object: {path}")
    digest = _json_fingerprint(payload)
    return f"sha256:{digest}:{path.resolve()}"


def _claim_session_signal(
    paths: MoverLabPaths,
    signal: dict[str, Any],
) -> tuple[bool, str, Path]:
    """Atomically retain the first observed signal for one strategy session."""

    identity = {
        "strategy_id": str(signal.get("strategy_id") or ""),
        "strategy_version": str(signal.get("strategy_version") or ""),
        "market_date": str(signal.get("market_date") or ""),
        "symbol": str(signal.get("symbol") or "").upper(),
        "evidence_mode": str(
            signal.get("evidence_mode") or "historical_replay"
        ),
    }
    session_signal_key = _stable_id(
        "mover_session_signal",
        identity["strategy_id"],
        identity["strategy_version"],
        identity["market_date"],
        identity["symbol"],
        identity["evidence_mode"],
    )
    registry_path = (
        paths.signals / "session_registry" / f"{session_signal_key}.json"
    )
    payload = {
        "schema_version": "v2.mover_session_signal_registry.v2",
        "session_signal_key": session_signal_key,
        **identity,
        "signal_id": str(signal.get("signal_id") or ""),
        "snapshot_id": str(signal.get("snapshot_id") or ""),
        "signal_at": str(signal.get("signal_at") or ""),
        "strategy_semantics_fingerprint": str(
            signal.get("strategy_semantics_fingerprint") or ""
        ),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=registry_path.parent,
        prefix=f".{session_signal_key}.",
        suffix=".claim",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, registry_path)
            existing = payload
        except FileExistsError:
            existing_raw = json.loads(registry_path.read_text(encoding="utf-8"))
            if not isinstance(existing_raw, dict):
                raise ValueError(
                    "session signal registry artifact must be an object"
                ) from None
            existing = existing_raw
    finally:
        temporary_path.unlink(missing_ok=True)
    if not isinstance(existing, dict):
        raise ValueError("session signal registry artifact must be an object")
    for field, expected in {
        "schema_version": "v2.mover_session_signal_registry.v2",
        "session_signal_key": session_signal_key,
        **identity,
        "research_only": True,
        "broker_execution_enabled": False,
    }.items():
        if existing.get(field) != expected:
            raise ValueError(
                f"session signal registry identity mismatch for {field}"
            )
    existing_signal_id = str(existing.get("signal_id") or "")
    if not existing_signal_id:
        raise ValueError("session signal registry is missing signal_id")
    return (
        existing_signal_id == str(signal.get("signal_id") or ""),
        existing_signal_id,
        registry_path,
    )


def _write_immutable_json(path: Path, payload: Any) -> None:
    text = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        default=_json_default,
    ) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError(f"immutable artifact conflict: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = tuple(rows[0]) if rows else ()
    buffer: list[str] = []
    if fieldnames:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        json.dumps(value, sort_keys=True)
                        if isinstance(value, (list, dict))
                        else value
                    )
                    for key, value in row.items()
                }
            )
        buffer.append(stream.getvalue().replace("\r\n", "\n"))
    text = "".join(buffer)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError(f"immutable artifact conflict: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _register_versioned_records(
    path: Path,
    records: list[dict[str, Any]],
    *,
    identity_fields: tuple[str, ...],
) -> None:
    existing_rows = _read_jsonl(path)
    existing = {
        tuple(str(row.get(field) or "") for field in identity_fields): row
        for row in existing_rows
    }
    additions: list[dict[str, Any]] = []
    for record in records:
        identity = tuple(
            str(record.get(field) or "") for field in identity_fields
        )
        if not all(identity):
            raise ValueError(f"versioned record has blank identity: {identity}")
        prior = existing.get(identity)
        fingerprint = str(record.get("semantics_fingerprint") or "")
        if prior is not None:
            if str(prior.get("semantics_fingerprint") or "") != fingerprint:
                raise ValueError(
                    "immutable semantics drift for " + "@".join(identity)
                )
            continue
        additions.append(record)
        existing[identity] = record
    if not additions:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in additions:
            handle.write(
                json.dumps(record, sort_keys=True, default=_json_default) + "\n"
            )


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(row, sort_keys=True, default=_json_default) + "\n"
        for row in rows
    )
    path.write_text(text, encoding="utf-8")


def _write_immutable_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(row, sort_keys=True, default=_json_default) + "\n"
        for row in rows
    )
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != text:
            raise ValueError(f"immutable artifact conflict: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number} must contain a JSON object")
        rows.append(payload)
    return rows


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
    ).hexdigest()


def _stable_id(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _parse_clock(value: str) -> time:
    parsed = time.fromisoformat(value)
    if not (RTH_START <= parsed <= RTH_END):
        raise ValueError(f"cutoff must be within RTH: {value}")
    return parsed


def _aware_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return parsed


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _required_float(value: Any, field: str) -> float:
    number = _optional_float(value)
    if number is None:
        raise ValueError(f"{field} is required and must be finite")
    return number


def _optional_int(value: Any) -> int | None:
    number = _optional_float(value)
    return int(number) if number is not None else None


def _optional_bool(value: Any) -> bool | None:
    if value in {None, ""}:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"cannot parse optional bool from {value!r}")


def _strategy_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("strategy_id") or "unknown"),
        str(row.get("strategy_version") or "unknown"),
    )


def _sample_standard_deviation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and values[order[end]] == values[order[index]]:
            end += 1
        rank_value = (index + end - 1) / 2.0 + 1.0
        for position in order[index:end]:
            ranks[position] = rank_value
        index = end
    return ranks


def _spearman(x: list[float], y: list[float]) -> float:
    rx = _rank(x)
    ry = _rank(y)
    avg_x = mean(rx)
    avg_y = mean(ry)
    numerator = sum((a - avg_x) * (b - avg_y) for a, b in zip(rx, ry, strict=True))
    denom_x = math.sqrt(sum((a - avg_x) ** 2 for a in rx))
    denom_y = math.sqrt(sum((b - avg_y) ** 2 for b in ry))
    return numerator / (denom_x * denom_y) if denom_x and denom_y else 0.0


def _approx_correlation_p_value(rho: float, sample_size: int) -> float:
    if sample_size <= 2 or abs(rho) >= 1:
        return 0.0 if abs(rho) >= 1 else 1.0
    statistic = abs(rho) * math.sqrt((sample_size - 2) / max(1 - rho * rho, 1e-12))
    return math.erfc(statistic / math.sqrt(2.0))


def _read_json_objects(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a JSON object")
        rows.append(payload)
    return rows


def _all_contract_rows_valid(
    rows: list[dict[str, Any]],
    parser: Any,
) -> bool:
    try:
        for row in rows:
            parser(row)
    except (TypeError, ValueError):
        return False
    return True


def _unique_nonblank(rows: list[dict[str, Any]], field: str) -> bool:
    values = [str(row.get(field) or "") for row in rows]
    return bool(values) and all(values) and len(values) == len(set(values))


def _versioned_registry_valid(
    rows: list[dict[str, Any]],
    *,
    identity_fields: tuple[str, ...],
) -> bool:
    identities: set[tuple[str, ...]] = set()
    for row in rows:
        identity = tuple(str(row.get(field) or "") for field in identity_fields)
        if not all(identity) or identity in identities:
            return False
        identities.add(identity)
        stored = str(row.get("semantics_fingerprint") or "")
        unhashed = {
            key: value
            for key, value in row.items()
            if key != "semantics_fingerprint"
        }
        legacy_fingerprint = hashlib.sha256(
            json.dumps(unhashed, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if stored not in {_json_fingerprint(unhashed), legacy_fingerprint}:
            return False
    return bool(rows)


def _source_artifact_refs_valid(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        refs = row.get("source_refs")
        if not isinstance(refs, list):
            return False
        hashed_refs = [str(ref) for ref in refs if str(ref).startswith("sha256:")]
        if not hashed_refs:
            return False
        for ref in hashed_refs:
            parts = ref.split(":", 2)
            if len(parts) != 3 or len(parts[1]) != 64:
                return False
            path = Path(parts[2])
            if not path.exists():
                return False
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return False
            if _json_fingerprint(payload) != parts[1]:
                return False
    return True


def _forward_receipt_valid(row: dict[str, Any]) -> bool:
    evidence_mode = str(row.get("evidence_mode") or "historical_replay")
    receipt_ref = str(row.get("forward_receipt_ref") or "")
    system_received_at = str(row.get("system_received_at") or "")
    if evidence_mode != "forward_observation":
        return not receipt_ref and not system_received_at
    source_captured_at = str(row.get("source_captured_at") or "")
    source_refs = row.get("source_refs")
    raw = row.get("raw_payload")
    if (
        not isinstance(source_refs, list)
        or not receipt_ref
        or receipt_ref not in source_refs
    ):
        return False
    if not isinstance(raw, dict):
        return False
    parts = receipt_ref.split(":", 2)
    if len(parts) != 3 or len(parts[1]) != 64:
        return False
    receipt_path = Path(parts[2])
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict) or _json_fingerprint(receipt) != parts[1]:
        return False
    cutoffs = receipt.get("feature_cutoffs_at")
    return bool(
        receipt.get("schema_version") == "v2.mover_forward_source_receipt.v1"
        and receipt.get("evidence_mode") == "forward_observation"
        and receipt.get("market_date") == row.get("market_date")
        and isinstance(cutoffs, list)
        and row.get("feature_cutoff_at") in cutoffs
        and receipt.get("system_received_at") == system_received_at
        and receipt.get("authoritative_source_captured_at")
        == source_captured_at
        and source_captured_at == system_received_at
        and receipt.get("bars_input_sha256")
        == raw.get("input_bars_sha256")
        and receipt.get("context_input_sha256")
        == raw.get("input_context_sha256")
        and raw.get("system_received_at") == system_received_at
        and raw.get("forward_receipt_ref") == receipt_ref
        and receipt.get("research_only") is True
        and receipt.get("broker_execution_enabled") is False
    )


def _forward_universe_artifact_valid(row: dict[str, Any]) -> bool:
    if row.get("evidence_mode") != "forward_observation":
        return True
    universe_ref = str(row.get("universe_source_ref") or "")
    parts = universe_ref.split(":", 2)
    if len(parts) != 3 or parts[0] != "sha256" or len(parts[1]) != 64:
        return False
    try:
        payload = json.loads(Path(parts[2]).read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            return False
        cutoff = _aware_datetime(row.get("feature_cutoff_at"))
        received = _aware_datetime(payload.get("system_received_at"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    symbols = payload.get("expected_symbols")
    if not isinstance(symbols, list):
        return False
    normalized_symbols = sorted(
        {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
    )
    return bool(
        _json_fingerprint(payload) == parts[1]
        and payload.get("schema_version") == "v2.mover_candidate_universe.v1"
        and payload.get("market_date") == row.get("market_date")
        and payload.get("feature_cutoff_at") == row.get("feature_cutoff_at")
        and payload.get("evidence_mode") == "forward_observation"
        and received <= cutoff
        and received.astimezone(MARKET_TZ).date().isoformat()
        == row.get("market_date")
        and payload.get("universe_selection_method")
        == row.get("universe_selection_method")
        and symbols == normalized_symbols
        and str(row.get("symbol") or "").upper() in normalized_symbols
        and payload.get("expected_symbols_complete") is True
        and payload.get("research_only") is True
        and payload.get("broker_execution_enabled") is False
    )


def _snapshot_identity_valid(row: dict[str, Any]) -> bool:
    raw = row.get("raw_payload")
    if not isinstance(raw, dict):
        return False
    bar_hash = str(raw.get("bar_prefix_sha256") or "")
    context_hash = str(raw.get("context_row_sha256") or "")
    if len(bar_hash) != 64 or len(context_hash) != 64:
        return False
    source_captured_at = str(row.get("source_captured_at") or "")
    identity_parts = [
        str(row.get("symbol") or "").upper(),
        str(row.get("market_date") or ""),
        str(row.get("feature_cutoff_at") or ""),
        bar_hash,
        context_hash,
        str(row.get("evidence_mode") or "historical_replay"),
        source_captured_at or "unknown_capture_time",
    ]
    if row.get("evidence_mode") == "forward_observation":
        identity_parts.extend(
            [
                str(row.get("system_received_at") or "no_system_receipt"),
                str(row.get("forward_receipt_ref") or "no_forward_receipt"),
            ]
        )
    expected = _stable_id("mover_snapshot", *identity_parts)
    return str(row.get("snapshot_id") or "") == expected


def _signals_reference_snapshots(
    signal_rows: list[dict[str, Any]],
    snapshot_rows: list[dict[str, Any]],
) -> bool:
    snapshots = {
        str(row.get("snapshot_id") or ""): row
        for row in snapshot_rows
        if row.get("snapshot_id")
    }
    for signal in signal_rows:
        snapshot = snapshots.get(str(signal.get("snapshot_id") or ""))
        if snapshot is None:
            return False
        signal_refs = signal.get("source_refs")
        snapshot_refs = snapshot.get("source_refs")
        if not isinstance(signal_refs, list) or not isinstance(snapshot_refs, list):
            return False
        if not set(map(str, snapshot_refs)).issubset(set(map(str, signal_refs))):
            return False
        if signal.get("market_date") != snapshot.get("market_date"):
            return False
        if str(signal.get("symbol") or "").upper() != str(
            snapshot.get("symbol") or ""
        ).upper():
            return False
        if signal.get("signal_at") != snapshot.get("feature_cutoff_at"):
            return False
        if signal.get("evidence_mode") != snapshot.get("evidence_mode"):
            return False
        if signal.get("source_captured_at") != snapshot.get("source_captured_at"):
            return False
        if signal.get("system_received_at") != snapshot.get("system_received_at"):
            return False
        if signal.get("forward_receipt_ref") != snapshot.get("forward_receipt_ref"):
            return False
    return True


def _utc_now() -> datetime:
    """Return the authoritative receipt clock; tests may monkeypatch this seam."""

    return datetime.now(timezone.utc)


def _session_signal_registry_valid(
    registry_rows: list[dict[str, Any]],
    signal_rows: list[dict[str, Any]],
) -> bool:
    if len(registry_rows) != len(signal_rows):
        return False
    signals_by_id = {
        str(row.get("signal_id") or ""): row
        for row in signal_rows
        if row.get("signal_id")
    }
    seen_keys: set[str] = set()
    for registry in registry_rows:
        identity = (
            str(registry.get("strategy_id") or ""),
            str(registry.get("strategy_version") or ""),
            str(registry.get("market_date") or ""),
            str(registry.get("symbol") or "").upper(),
            str(registry.get("evidence_mode") or "historical_replay"),
        )
        expected_key = _stable_id("mover_session_signal", *identity)
        signal_id = str(registry.get("signal_id") or "")
        signal = signals_by_id.get(signal_id)
        if (
            registry.get("schema_version")
            != "v2.mover_session_signal_registry.v2"
            or registry.get("session_signal_key") != expected_key
            or expected_key in seen_keys
            or registry.get("research_only") is not True
            or registry.get("broker_execution_enabled") is not False
            or signal is None
            or identity
            != (
                str(signal.get("strategy_id") or ""),
                str(signal.get("strategy_version") or ""),
                str(signal.get("market_date") or ""),
                str(signal.get("symbol") or "").upper(),
                str(signal.get("evidence_mode") or "historical_replay"),
            )
            or str(registry.get("snapshot_id") or "")
            != str(signal.get("snapshot_id") or "")
            or str(registry.get("signal_at") or "")
            != str(signal.get("signal_at") or "")
            or str(registry.get("strategy_semantics_fingerprint") or "")
            != str(signal.get("strategy_semantics_fingerprint") or "")
        ):
            return False
        seen_keys.add(expected_key)
    return len(seen_keys) == len(signal_rows)


def _trade_evidence_valid(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        evidence_sha = row.get("bars_evidence_sha256")
        evidence_path = row.get("bars_evidence_path")
        if evidence_sha is None and evidence_path is None:
            continue
        if not evidence_sha or not evidence_path:
            return False
        path = Path(str(evidence_path))
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if _json_fingerprint(payload) != str(evidence_sha):
            return False
        if row.get("status") == "closed" and not retained_trade_evidence_recomputes(row):
            return False
    return True


def _closed_trade_math_valid(row: dict[str, Any]) -> bool:
    try:
        quantity = _required_float(row.get("quantity"), "quantity")
        notional = _required_float(row.get("notional_per_trade"), "notional")
        entry_reference = _required_float(
            row.get("entry_reference"), "entry_reference"
        )
        exit_reference = _required_float(
            row.get("exit_reference"), "exit_reference"
        )
        entry_price = _required_float(row.get("entry_price"), "entry_price")
        exit_price = _required_float(row.get("exit_price"), "exit_price")
        fee_bps = _required_float(row.get("fee_bps"), "fee_bps")
        fee_cost = _required_float(row.get("fee_cost"), "fee_cost")
        slippage_cost = _required_float(
            row.get("slippage_cost"), "slippage_cost"
        )
        total_cost = _required_float(row.get("total_cost"), "total_cost")
        pnl = _required_float(row.get("pnl"), "pnl")
        gross_return = _required_float(
            row.get("gross_return_pct"), "gross_return_pct"
        )
        net_return = _required_float(
            row.get("net_return_pct"), "net_return_pct"
        )
    except ValueError:
        return False
    expected_fee = quantity * entry_price * fee_bps / 10_000.0
    expected_fee += quantity * exit_price * fee_bps / 10_000.0
    expected_slippage = quantity * (
        (entry_price - entry_reference) + (exit_reference - exit_price)
    )
    expected_reference_pnl = quantity * (exit_reference - entry_reference)
    expected_net_pnl = quantity * (exit_price - entry_price) - expected_fee
    return all(
        (
            math.isclose(fee_cost, expected_fee, abs_tol=2e-5),
            math.isclose(slippage_cost, expected_slippage, abs_tol=2e-5),
            math.isclose(total_cost, expected_fee + expected_slippage, abs_tol=3e-5),
            math.isclose(pnl, expected_net_pnl, abs_tol=2e-5),
            math.isclose(
                gross_return,
                expected_reference_pnl / notional * 100.0,
                abs_tol=2e-5,
            ),
            math.isclose(
                net_return,
                expected_net_pnl / notional * 100.0,
                abs_tol=2e-5,
            ),
        )
    )


def _closed_trade_timestamps_valid(row: dict[str, Any]) -> bool:
    try:
        signal_at = _aware_datetime(row.get("signal_at"))
        entry_at = _aware_datetime(row.get("entry_at"))
        exit_at = _aware_datetime(row.get("exit_at"))
        close_at = _aware_datetime(row.get("session_close_at"))
        entry_source_bar_at = _aware_datetime(row.get("entry_source_bar_at"))
    except (TypeError, ValueError):
        return False
    market_date = str(row.get("market_date") or "")
    if not signal_at <= entry_at <= exit_at <= close_at:
        return False
    interval = _optional_int(row.get("bar_interval_minutes"))
    if interval is None or entry_source_bar_at != entry_at + timedelta(minutes=interval):
        return False
    if any(
        value.astimezone(MARKET_TZ).date().isoformat() != market_date
        for value in (signal_at, entry_at, exit_at, close_at, entry_source_bar_at)
    ):
        return False
    if row.get("reason") == "eod_flat" and exit_at != close_at:
        return False
    return row.get("source_bar_sequence_complete") is True


def _check(
    name: str,
    passed: bool,
    *,
    applicable: bool = True,
) -> dict[str, Any]:
    return {
        "check": name,
        "passed": bool(passed) if applicable else None,
        "applicable": applicable,
    }


def _display_pct(value: Any) -> str:
    number = _optional_float(value)
    return "N/A" if number is None else f"{number:.4f}%"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


__all__ = [
    "DEFAULT_OUTPUT_ROOT",
    "MoverLabPaths",
    "analyze",
    "build_snapshots_from_bars",
    "init",
    "paper_scan",
    "reconcile_paper_signals",
    "verify",
]
