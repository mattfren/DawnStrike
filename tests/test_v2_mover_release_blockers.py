from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from intraday_scanner.v2.mover_pattern_lab import core
from intraday_scanner.v2.mover_pattern_lab.contracts import stable_id
from intraday_scanner.v2.mover_pattern_lab.core import (
    _forward_strategy_day_fully_evaluated,
    analyze,
    build_snapshots_from_bars,
    paper_scan,
    reconcile_paper_signals,
)
from intraday_scanner.v2.mover_pattern_lab.strategies import strategy_catalog
from intraday_scanner.v2.mover_pattern_lab.trade_truth import (
    _forward_signal_snapshot_lineage_matches,
    _json_artifact_payload,
    retained_trade_evidence_recomputes,
)

ET = ZoneInfo("America/New_York")
BAR_FIELDS = ("symbol", "timestamp", "open", "high", "low", "close", "volume")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _bars(market_date: str, *, through_close: bool) -> list[dict[str, Any]]:
    timestamp = datetime.fromisoformat(f"{market_date}T09:35:00-04:00")
    end = datetime.fromisoformat(
        f"{market_date}T{'16:00' if through_close else '09:45'}:00-04:00"
    )
    rows: list[dict[str, Any]] = []
    index = 0
    opening_volume = 2_000_000 if market_date == "2026-07-21" else 400_000
    while timestamp <= end:
        if through_close and market_date == "2026-07-17":
            open_price = close_price = 10.0
            volume = 50_000
        elif index == 0:
            open_price, close_price, volume = 10.8, 11.0, opening_volume
        elif index == 1:
            open_price, close_price, volume = 11.0, 11.1, opening_volume
        elif index == 2:
            open_price, close_price, volume = 11.1, 11.2, opening_volume
        else:
            open_price = 11.2 + (index - 3) * 0.002
            close_price = open_price + 0.002
            volume = 100_000
        if through_close and timestamp.hour == 16:
            open_price = close_price = 10.0
        rows.append(
            {
                "symbol": "ABC",
                "timestamp": timestamp.isoformat(),
                "open": round(open_price, 6),
                "high": round(max(open_price, close_price) + 0.05, 6),
                "low": round(min(open_price, close_price) - 0.05, 6),
                "close": round(close_price, 6),
                "volume": volume,
            }
        )
        timestamp += timedelta(minutes=5)
        index += 1
    return rows


def _universe_ref(tmp_path: Path, market_date: str) -> str:
    payload = {
        "schema_version": "v2.mover_candidate_universe.v1",
        "market_date": market_date,
        "feature_cutoff_at": f"{market_date}T09:45:00-04:00",
        "evidence_mode": "forward_observation",
        "system_received_at": f"{market_date}T09:20:00-04:00",
        "universe_selection_method": "scheduled_universe",
        "expected_symbols": ["ABC"],
        "expected_symbols_complete": True,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = tmp_path / f"universe_{market_date}_{digest}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return f"sha256:{digest}:{path.resolve()}"


def _context(path: Path, market_date: str, universe_ref: str) -> None:
    _write_csv(
        path,
        [
            {
                "market_date": market_date,
                "symbol": "ABC",
                "context_observed_at": f"{market_date}T09:44:00-04:00",
                "universe_selected_at": f"{market_date}T09:20:00-04:00",
                "universe_source_ref": universe_ref,
                "universe_selection_method": "scheduled_universe",
                "spread_pct": 0.5,
                "split_adjusted": True,
                "reverse_split_days": 180,
                "reverse_split_lookback_clear": True,
                "recent_offering_days": 60,
                "offering_lookback_clear": True,
                "halt_state": "clear",
                "source_conflict": False,
                "catalyst_verified": False,
                "catalyst_published_at": "",
                "catalyst_source_url": "",
                "catalyst_source_type": "",
                "catalyst_artifact_ref": "",
                "source_refs": universe_ref,
            }
        ],
    )


def _run_forward_day(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    market_date: str,
    prior_rows: list[dict[str, Any]],
    output_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    cutoff_bars = tmp_path / f"cutoff_{market_date}.csv"
    _write_csv(cutoff_bars, [*prior_rows, *_bars(market_date, through_close=False)])
    context_path = tmp_path / f"context_{market_date}.csv"
    _context(context_path, market_date, _universe_ref(tmp_path, market_date))
    receipt_at = datetime.fromisoformat(f"{market_date}T09:47:00-04:00")
    monkeypatch.setattr(core, "_utc_now", lambda: receipt_at)
    build = build_snapshots_from_bars(
        bars_csv=cutoff_bars,
        context_csv=context_path,
        market_date=market_date,
        cutoffs=("09:45",),
        min_baseline_sessions=1,
        bar_interval_minutes=5,
        bar_timestamp_semantics="bar_close",
        evidence_mode="forward_observation",
        source_captured_at=datetime.fromisoformat(
            f"{market_date}T09:46:30-04:00"
        ),
        output_root=output_root,
    )
    scan = paper_scan(
        snapshots_path=Path(build["snapshot_path"]),
        expected_market_dates=(market_date,),
        output_root=output_root,
    )
    outcome_bars = tmp_path / f"outcomes_{market_date}.csv"
    _write_csv(outcome_bars, [*prior_rows, *_bars(market_date, through_close=True)])
    reconciliation = reconcile_paper_signals(
        signals_path=Path(scan["signals_path"]),
        bars_csv=outcome_bars,
        bar_interval_minutes=5,
        bar_timestamp_semantics="bar_close",
        output_root=output_root,
    )
    return build, scan, reconciliation


def test_forward_receipt_blocks_backdating_future_bars_and_pre_capture_fill(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = tmp_path / "lab"
    prior = _bars("2026-07-17", through_close=True)
    build, scan, reconciliation = _run_forward_day(
        tmp_path,
        monkeypatch,
        market_date="2026-07-20",
        prior_rows=prior,
        output_root=root,
    )
    snapshot = json.loads(Path(build["snapshot_path"]).read_text(encoding="utf-8"))
    assert snapshot["source_captured_at"] == "2026-07-20T09:47:00-04:00"
    assert snapshot["system_received_at"] == snapshot["source_captured_at"]
    assert snapshot["forward_receipt_ref"].startswith("sha256:")
    assert scan["signal_count"] == 1
    trade = json.loads(Path(reconciliation["trades_path"]).read_text(encoding="utf-8"))
    retained_signal = _json_artifact_payload(trade["signal_artifact_ref"])
    retained_snapshot = _json_artifact_payload(trade["snapshot_artifact_ref"])
    assert retained_signal is not None
    assert retained_snapshot is not None
    exact_fields = (
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
    differences = {
        field: (retained_signal.get(field), trade.get(field))
        for field in exact_fields
        if retained_signal.get(field) != trade.get(field)
    }
    assert not differences, differences
    assert retained_signal["entry_reference"] == trade["signal_entry_reference"]
    assert trade["signal_artifact_ref"] in trade["source_refs"]
    assert trade["snapshot_artifact_ref"] in trade["source_refs"]
    assert trade["signal_id"] == stable_id(
        "mover_paper_signal",
        trade["strategy_id"],
        trade["strategy_version"],
        trade["snapshot_id"],
    )
    snapshot_expectations = {
        "snapshot_id": trade["snapshot_id"],
        "market_date": trade["market_date"],
        "symbol": trade["symbol"],
        "feature_cutoff_at": trade["signal_at"],
        "evidence_mode": "forward_observation",
        "source_captured_at": trade["source_captured_at"],
        "system_received_at": trade["system_received_at"],
        "forward_receipt_ref": trade["forward_receipt_ref"],
    }
    snapshot_differences = {
        field: (retained_snapshot.get(field), expected)
        for field, expected in snapshot_expectations.items()
        if retained_snapshot.get(field) != expected
    }
    assert not snapshot_differences, snapshot_differences
    assert _forward_signal_snapshot_lineage_matches(trade), trade
    assert retained_trade_evidence_recomputes(trade), trade
    assert datetime.fromisoformat(trade["entry_at"]).astimezone(ET) == (
        datetime.fromisoformat("2026-07-20T09:50:00-04:00")
    )
    assert datetime.fromisoformat(trade["entry_source_bar_at"]).astimezone(ET) == (
        datetime.fromisoformat("2026-07-20T09:55:00-04:00")
    )

    future_bars = tmp_path / "future_bars.csv"
    _write_csv(
        future_bars,
        [*prior, *_bars("2026-07-20", through_close=False), {
            **_bars("2026-07-20", through_close=True)[3],
        }],
    )
    context_path = tmp_path / "future_context.csv"
    _context(context_path, "2026-07-20", _universe_ref(tmp_path, "2026-07-20"))
    with pytest.raises(ValueError, match="after the feature cutoff"):
        build_snapshots_from_bars(
            bars_csv=future_bars,
            context_csv=context_path,
            market_date="2026-07-20",
            cutoffs=("09:45",),
            min_baseline_sessions=1,
            bar_timestamp_semantics="bar_close",
            evidence_mode="forward_observation",
            output_root=tmp_path / "future_lab",
        )

    monkeypatch.setattr(
        core,
        "_utc_now",
        lambda: datetime.fromisoformat("2026-07-21T09:47:00-04:00"),
    )
    with pytest.raises(ValueError, match="system receipt date"):
        build_snapshots_from_bars(
            bars_csv=Path(build["snapshot_path"]),
            market_date="2026-07-20",
            cutoffs=("09:45",),
            bar_timestamp_semantics="bar_close",
            evidence_mode="forward_observation",
            output_root=tmp_path / "past_lab",
        )


def test_cumulative_analysis_retains_prior_days_without_double_counting(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root = tmp_path / "lab"
    day_one_prior = _bars("2026-07-17", through_close=True)
    _, scan_one, reconcile_one = _run_forward_day(
        tmp_path,
        monkeypatch,
        market_date="2026-07-20",
        prior_rows=day_one_prior,
        output_root=root,
    )
    first = analyze(
        scan_manifest_path=Path(scan_one["run_manifest_path"]),
        reconcile_manifest_path=Path(reconcile_one["run_manifest_path"]),
        output_root=root,
    )
    assert first["included_run_pair_count"] == 1

    day_two_prior = [*day_one_prior, *_bars("2026-07-20", through_close=True)]
    _, scan_two, reconcile_two = _run_forward_day(
        tmp_path,
        monkeypatch,
        market_date="2026-07-21",
        prior_rows=day_two_prior,
        output_root=root,
    )
    second = analyze(
        scan_manifest_path=Path(scan_two["run_manifest_path"]),
        reconcile_manifest_path=Path(reconcile_two["run_manifest_path"]),
        output_root=root,
    )
    forward_dates = {
        row["market_date"]
        for row in second["strategy_daily_calendar"]
        if row["evidence_mode"] == "forward_observation"
    }
    assert forward_dates == {"2026-07-20", "2026-07-21"}
    assert second["included_run_pair_count"] == 2
    calendar_statuses = [
        (
            row["market_date"],
            row["strategy_id"],
            row["status"],
            row["signal_count"],
            row["closed_trade_count"],
            row["not_entered_count"],
            row["pending_trade_count"],
        )
        for row in second["strategy_daily_calendar"]
        if row["evidence_mode"] == "forward_observation"
    ]
    assert second["closed_trade_count"] == 2, calendar_statuses

    repeated = analyze(
        scan_manifest_path=Path(scan_two["run_manifest_path"]),
        reconcile_manifest_path=Path(reconcile_two["run_manifest_path"]),
        output_root=root,
    )
    assert repeated["closed_trade_count"] == 2
    assert repeated["analysis_fingerprint"] == second["analysis_fingerprint"]


def test_sparse_signal_days_still_reach_forward_session_maturity() -> None:
    spec = strategy_catalog()[0]
    market_dates: list[str] = []
    current = datetime(2026, 7, 20, tzinfo=ET)
    while len(market_dates) < 30:
        if current.weekday() < 5:
            market_dates.append(current.date().isoformat())
        current += timedelta(days=1)
    evaluated = [
        market_date
        for market_date in market_dates
        if _forward_strategy_day_fully_evaluated(
            spec,
            market_date,
            [
                {
                    "evidence_mode": "forward_observation",
                    "decision": "rejected",
                    "reason": "setup_conditions_not_met",
                    "missing_features": [],
                    "research_only": True,
                    "broker_execution_enabled": False,
                }
            ],
        )
    ]
    assert len(evaluated) == 30
