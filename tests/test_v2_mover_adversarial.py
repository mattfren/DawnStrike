from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from intraday_scanner.mover_pattern_audit import (
    audit_retained_data,
    outcome_is_learning_eligible,
)
from intraday_scanner.v2.mover_pattern_lab.contracts import (
    MoverPaperSignal,
    ProspectiveMoverSnapshot,
    stable_id,
)
from intraday_scanner.v2.mover_pattern_lab.core import (
    MoverLabPaths,
    _chronological_date_partitions,
    _chronological_splits,
    _claim_session_signal,
    _immutable_split_name,
    _json_fingerprint,
    _register_versioned_records,
    _return_metrics,
    _strategy_day_book_rows,
    _strategy_key,
    analyze,
    build_snapshots_from_bars,
    init,
    reconcile_paper_signals,
    verify,
)
from intraday_scanner.v2.mover_pattern_lab.strategies import strategy_catalog
from intraday_scanner.v2.mover_pattern_lab.trade_truth import (
    closed_trade_recomputes,
)

BAR_FIELDS = ("symbol", "timestamp", "open", "high", "low", "close", "volume")
CONTEXT_FIELDS = (
    "market_date",
    "symbol",
    "context_observed_at",
    "universe_selected_at",
    "universe_source_ref",
    "universe_selection_method",
    "spread_pct",
    "split_adjusted",
    "reverse_split_days",
    "reverse_split_lookback_clear",
    "recent_offering_days",
    "offering_lookback_clear",
    "halt_state",
    "source_conflict",
    "catalyst_verified",
    "catalyst_published_at",
    "catalyst_source_url",
    "catalyst_source_type",
    "catalyst_artifact_ref",
    "source_refs",
)


def _write_csv(
    path: Path,
    fields: tuple[str, ...],
    rows: list[dict[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _bars_through(
    market_date: str,
    end_clock: str,
    *,
    interval_minutes: int = 5,
    volume: int = 1_000,
    include_close: bool = False,
) -> list[dict[str, object]]:
    current = datetime.fromisoformat(f"{market_date}T09:30:00-04:00")
    end = datetime.fromisoformat(f"{market_date}T{end_clock}:00-04:00")
    rows: list[dict[str, object]] = []
    sequence = 0
    while current + timedelta(minutes=interval_minutes) <= end:
        current += timedelta(minutes=interval_minutes)
        price = 10.0 + sequence * 0.01
        rows.append(
            {
                "symbol": "ABC",
                "timestamp": current.isoformat(),
                "open": price,
                "high": price + 0.10,
                "low": price - 0.10,
                "close": price + 0.02,
                "volume": volume,
            }
        )
        sequence += 1
    if include_close:
        rows.append(
            {
                "symbol": "ABC",
                "timestamp": f"{market_date}T16:00:00-04:00",
                "open": 10.50,
                "high": 10.60,
                "low": 10.40,
                "close": 10.50,
                "volume": volume,
            }
        )
    return rows


def _context_row(
    observed_at: str,
    **overrides: object,
) -> dict[str, object]:
    universe_ref = "universe://premarket-screen/2026-07-15/abc"
    row: dict[str, object] = {
        "market_date": "2026-07-15",
        "symbol": "ABC",
        "context_observed_at": observed_at,
        "universe_selected_at": "2026-07-15T08:30:00-04:00",
        "universe_source_ref": universe_ref,
        "universe_selection_method": "premarket_screen",
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
    row.update(overrides)
    return row


def _snapshot_row(**overrides: object) -> dict[str, object]:
    universe_ref = "universe://premarket-screen/2026-07-15/abc"
    row: dict[str, object] = {
        "snapshot_id": "snapshot-abc-1000",
        "market_date": "2026-07-15",
        "symbol": "ABC",
        "observed_at": "2026-07-15T10:00:00-04:00",
        "feature_cutoff_at": "2026-07-15T10:00:00-04:00",
        "universe_selected_at": "2026-07-15T08:30:00-04:00",
        "universe_source_ref": universe_ref,
        "universe_selection_method": "premarket_screen",
        "context_observed_at": "2026-07-15T09:59:00-04:00",
        "price": 11.0,
        "previous_close": 10.0,
        "session_open": 10.5,
        "opening_range_high": 11.1,
        "opening_range_low": 10.4,
        "opening_range_complete": True,
        "running_vwap": 10.8,
        "cumulative_volume": 1_000_000,
        "cumulative_dollar_volume": 10_000_000,
        "same_clock_rvol": 3.0,
        "spread_pct": 0.5,
        "split_adjusted": True,
        "reverse_split_days": 180,
        "recent_offering_days": 60,
        "halt_state": "clear",
        "source_conflict": False,
        "catalyst_verified": False,
        "catalyst_published_at": None,
        "catalyst_source_url": "",
        "catalyst_source_type": "",
        "source_refs": [universe_ref, "bars://immutable/abc"],
        "raw_payload": {"bar_timestamp_semantics": "bar_close"},
    }
    row.update(overrides)
    return row


def _paper_signal(
    *,
    market_date: str = "2026-07-15",
    signal_at: str = "2026-07-15T09:45:00-04:00",
    bar_interval_minutes: int = 15,
    **overrides: object,
) -> dict[str, object]:
    spec = strategy_catalog()[0]
    row: dict[str, object] = {
        "strategy_id": spec.strategy_id,
        "strategy_version": spec.version,
        "strategy_semantics_fingerprint": spec.to_dict()[
            "semantics_fingerprint"
        ],
        "market_date": market_date,
        "symbol": "ABC",
        "signal_at": signal_at,
        "snapshot_id": f"snapshot-{market_date}-abc",
        "entry_reference": 10.0,
        "stop": 9.5,
        "target": 11.0,
        "score": 0.5,
        "evidence": ["adversarial_test"],
        "warnings": [],
        "source_refs": ["bars://immutable/abc"],
        "features": {"bar_interval_minutes": bar_interval_minutes},
        "research_only": True,
        "broker_execution_enabled": False,
    }
    row.update(overrides)
    if "signal_id" not in row:
        row["signal_id"] = stable_id(
            "mover_paper_signal",
            row["strategy_id"],
            row["strategy_version"],
            row["snapshot_id"],
        )
    return row


def _retain_signal_ledger(
    output_root: Path,
    raw_signal: dict[str, object],
) -> dict[str, Any]:
    init(output_root=output_root)
    paths = MoverLabPaths.create(output_root)
    signal = dict(raw_signal)
    signal_at = datetime.fromisoformat(str(signal["signal_at"]))
    payloads = {
        "bars": [{"symbol": signal["symbol"], "through": signal_at.isoformat()}],
        "context": {"observed_at": (signal_at - timedelta(minutes=1)).isoformat()},
    }
    refs: list[str] = []
    hashes: dict[str, str] = {}
    for name, payload in payloads.items():
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        artifact_path = output_root / "source_artifacts" / f"{digest}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        hashes[name] = digest
        refs.append(f"sha256:{digest}:{artifact_path.resolve()}")
    entry_reference = float(signal["entry_reference"])
    snapshot_raw = _snapshot_row(
        market_date=str(signal["market_date"]),
        symbol=str(signal["symbol"]),
        observed_at=signal_at.isoformat(),
        feature_cutoff_at=signal_at.isoformat(),
        universe_selected_at=(signal_at - timedelta(hours=1)).isoformat(),
        universe_source_ref=refs[1],
        context_observed_at=(signal_at - timedelta(minutes=1)).isoformat(),
        price=entry_reference,
        previous_close=entry_reference * 0.9,
        session_open=entry_reference * 0.95,
        opening_range_high=entry_reference,
        opening_range_low=float(signal["stop"]),
        running_vwap=entry_reference * 0.99,
        reverse_split_lookback_clear=True,
        offering_lookback_clear=True,
        catalyst_artifact_ref="",
        source_refs=refs,
        raw_payload={
            "bar_timestamp_semantics": "bar_close",
            "bar_interval_minutes": int(
                dict(signal.get("features") or {}).get(
                    "bar_interval_minutes", 15
                )
            ),
            "bar_prefix_sha256": hashes["bars"],
            "context_row_sha256": hashes["context"],
        },
    )
    snapshot_raw["snapshot_id"] = stable_id(
        "mover_snapshot",
        snapshot_raw["symbol"],
        snapshot_raw["market_date"],
        snapshot_raw["feature_cutoff_at"],
        hashes["bars"],
        hashes["context"],
        "historical_replay",
        "unknown_capture_time",
    )
    snapshot = ProspectiveMoverSnapshot.from_mapping(snapshot_raw).to_dict()
    snapshot_path = paths.snapshots / "by_id" / f"{snapshot['snapshot_id']}.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    signal.update(
        {
            "snapshot_id": snapshot["snapshot_id"],
            "source_refs": snapshot["source_refs"],
            "evidence_mode": snapshot["evidence_mode"],
            "source_captured_at": snapshot["source_captured_at"],
        }
    )
    signal["signal_id"] = stable_id(
        "mover_paper_signal",
        signal["strategy_id"],
        signal["strategy_version"],
        signal["snapshot_id"],
    )
    retained = MoverPaperSignal.from_mapping(signal).to_dict()
    signal_path = paths.signals / "by_id" / f"{retained['signal_id']}.json"
    signal_path.parent.mkdir(parents=True, exist_ok=True)
    signal_path.write_text(
        json.dumps(retained, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    claimed, _, _ = _claim_session_signal(paths, retained)
    assert claimed is True
    return retained


def _reconcile(
    tmp_path: Path,
    signal: dict[str, object],
    bars: list[dict[str, object]],
    *,
    interval: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    output_root = tmp_path / "lab"
    retained_signal = _retain_signal_ledger(output_root, signal)
    paths = MoverLabPaths.create(output_root)
    signals_path = (
        paths.signals
        / f"signals_{_json_fingerprint([retained_signal])[:16]}.jsonl"
    )
    bars_path = tmp_path / "bars.csv"
    _write_jsonl(signals_path, [retained_signal])
    _write_csv(bars_path, BAR_FIELDS, bars)
    result = reconcile_paper_signals(
        signals_path=signals_path,
        bars_csv=bars_path,
        slippage_bps=0,
        fee_bps=0,
        bar_interval_minutes=interval,
        bar_timestamp_semantics="bar_close",
        output_root=output_root,
    )
    trades = _read_jsonl(Path(result["trades_path"]))
    assert len(trades) == 1
    return result, trades[0]


def test_same_version_semantics_registry_drift_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    init(output_root=root)
    registry_path = root / "manifests" / "strategy_registry.jsonl"
    prior = _read_jsonl(registry_path)[0]
    drifted = {**prior, "description": "changed meaning under the same version"}
    unhashed = {
        key: value
        for key, value in drifted.items()
        if key != "semantics_fingerprint"
    }
    drifted["semantics_fingerprint"] = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True).encode("utf-8")
    ).hexdigest()

    with pytest.raises(ValueError, match="immutable semantics drift"):
        _register_versioned_records(
            registry_path,
            [drifted],
            identity_fields=("strategy_id", "version"),
        )


def test_future_appends_preserve_cutoff_identity_and_incremental_bundles(
    tmp_path: Path,
) -> None:
    bars_path = tmp_path / "bars.csv"
    context_path = tmp_path / "context.csv"
    root = tmp_path / "lab"
    bars = [
        *_bars_through("2026-07-14", "10:00", include_close=True),
        *_bars_through("2026-07-15", "10:00", volume=3_000),
    ]
    contexts = [
        _context_row("2026-07-15T09:44:00-04:00"),
        _context_row("2026-07-15T09:59:00-04:00"),
    ]
    _write_csv(bars_path, BAR_FIELDS, bars)
    _write_csv(context_path, CONTEXT_FIELDS, contexts)

    first = build_snapshots_from_bars(
        bars_csv=bars_path,
        context_csv=context_path,
        market_date="2026-07-15",
        cutoffs=("09:45",),
        min_baseline_sessions=1,
        bar_timestamp_semantics="bar_close",
        output_root=root,
    )
    first_row = _read_jsonl(Path(first["snapshot_path"]))[0]
    first_by_id = root / "snapshots" / "by_id" / f"{first_row['snapshot_id']}.json"
    first_bytes = first_by_id.read_bytes()

    incremental = build_snapshots_from_bars(
        bars_csv=bars_path,
        context_csv=context_path,
        market_date="2026-07-15",
        cutoffs=("09:45", "10:00"),
        min_baseline_sessions=1,
        bar_timestamp_semantics="bar_close",
        output_root=root,
    )
    incremental_rows = _read_jsonl(Path(incremental["snapshot_path"]))
    by_cutoff = {row["feature_cutoff_at"][11:16]: row for row in incremental_rows}
    assert by_cutoff["09:45"]["snapshot_id"] == first_row["snapshot_id"]
    assert first_by_id.read_bytes() == first_bytes
    ten_id = by_cutoff["10:00"]["snapshot_id"]
    ten_by_id = root / "snapshots" / "by_id" / f"{ten_id}.json"
    ten_bytes = ten_by_id.read_bytes()
    assert first["snapshot_path"] != incremental["snapshot_path"]

    bars.append(
        {
            "symbol": "ABC",
            "timestamp": "2026-07-15T10:05:00-04:00",
            "open": 10.40,
            "high": 10.60,
            "low": 10.30,
            "close": 10.50,
            "volume": 50_000,
        }
    )
    contexts.append(_context_row("2026-07-15T10:04:00-04:00", spread_pct=0.1))
    _write_csv(bars_path, BAR_FIELDS, bars)
    _write_csv(context_path, CONTEXT_FIELDS, contexts)

    appended = build_snapshots_from_bars(
        bars_csv=bars_path,
        context_csv=context_path,
        market_date="2026-07-15",
        cutoffs=("09:45", "10:00"),
        min_baseline_sessions=1,
        bar_timestamp_semantics="bar_close",
        output_root=root,
    )
    appended_rows = _read_jsonl(Path(appended["snapshot_path"]))
    appended_ten = next(
        row for row in appended_rows if row["feature_cutoff_at"][11:16] == "10:00"
    )
    assert appended_ten["snapshot_id"] == ten_id
    assert ten_by_id.read_bytes() == ten_bytes
    assert appended["snapshot_path"] == incremental["snapshot_path"]
    assert len(list((root / "snapshots").glob("prospective_*.jsonl"))) == 2


@pytest.mark.parametrize(
    "overrides, match",
    [
        (
            {"universe_selection_method": "realized_eod_gainers"},
            "universe_selection_method",
        ),
        (
            {"context_observed_at": "2026-07-15T10:01:00-04:00"},
            "context_observed_at",
        ),
        (
            {"universe_selected_at": "2026-07-15T10:01:00-04:00"},
            "universe_selected_at",
        ),
    ],
)
def test_eod_unapproved_or_post_cutoff_context_is_rejected(
    overrides: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        ProspectiveMoverSnapshot.from_mapping(_snapshot_row(**overrides))


def test_stale_context_is_rejected_from_snapshot_build(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    context_path = tmp_path / "context.csv"
    _write_csv(
        bars_path,
        BAR_FIELDS,
        [
            *_bars_through("2026-07-14", "10:00", include_close=True),
            *_bars_through("2026-07-15", "10:00"),
        ],
    )
    _write_csv(
        context_path,
        CONTEXT_FIELDS,
        [_context_row("2026-07-15T09:50:00-04:00")],
    )

    result = build_snapshots_from_bars(
        bars_csv=bars_path,
        context_csv=context_path,
        market_date="2026-07-15",
        cutoffs=("10:00",),
        min_baseline_sessions=1,
        bar_timestamp_semantics="bar_close",
        output_root=tmp_path / "lab",
    )

    assert result["status"] == "blocked"
    rejected = json.loads(Path(result["rejected_path"]).read_text(encoding="utf-8"))
    assert rejected[0]["reason"] == "prospective_context_stale_or_future"


def test_zero_volume_market_bars_are_rejected(tmp_path: Path) -> None:
    bars_path = tmp_path / "zero.csv"
    _write_csv(
        bars_path,
        BAR_FIELDS,
        [
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T09:35:00-04:00",
                "open": 10,
                "high": 10.1,
                "low": 9.9,
                "close": 10,
                "volume": 0,
            }
        ],
    )

    with pytest.raises(ValueError, match="zero or negative volume"):
        build_snapshots_from_bars(
            bars_csv=bars_path,
            market_date="2026-07-15",
            cutoffs=("09:35",),
            min_baseline_sessions=1,
            bar_timestamp_semantics="bar_close",
            output_root=tmp_path / "lab",
        )


@pytest.mark.parametrize(
    "case, match",
    [
        ("tampered_signal_id", "signal_id does not match"),
        ("unknown_strategy", "unknown strategy identity"),
        ("tampered_fingerprint", "semantics fingerprint mismatch"),
    ],
)
def test_invalid_signal_identity_or_strategy_semantics_is_rejected(
    tmp_path: Path,
    case: str,
    match: str,
) -> None:
    signal = _paper_signal()
    if case == "tampered_signal_id":
        signal["signal_id"] = "tampered-id"
    elif case == "unknown_strategy":
        signal["strategy_version"] = "v999.0"
        signal["signal_id"] = stable_id(
            "mover_paper_signal",
            signal["strategy_id"],
            signal["strategy_version"],
            signal["snapshot_id"],
        )
    else:
        signal["strategy_semantics_fingerprint"] = "0" * 64
    signals_path = tmp_path / "signals.jsonl"
    bars_path = tmp_path / "bars.csv"
    _write_jsonl(signals_path, [signal])
    _write_csv(
        bars_path,
        BAR_FIELDS,
        [
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T10:00:00-04:00",
                "open": 10,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 1_000,
            }
        ],
    )

    with pytest.raises(ValueError, match=match):
        reconcile_paper_signals(
            signals_path=signals_path,
            bars_csv=bars_path,
            bar_interval_minutes=15,
            bar_timestamp_semantics="bar_close",
            output_root=tmp_path / "lab",
        )


def test_bar_touching_stop_and_target_uses_stop_first(tmp_path: Path) -> None:
    _, trade = _reconcile(
        tmp_path,
        _paper_signal(),
        [
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T10:00:00-04:00",
                "open": 10.0,
                "high": 11.5,
                "low": 9.0,
                "close": 10.5,
                "volume": 10_000,
            }
        ],
        interval=15,
    )

    assert trade["status"] == "closed"
    assert trade["reason"] == "stop"
    assert trade["exit_reference"] == pytest.approx(9.5)
    assert trade["net_return_pct"] < 0


def test_early_close_complete_grid_flattens_at_1300(tmp_path: Path) -> None:
    _, trade = _reconcile(
        tmp_path,
        _paper_signal(
            market_date="2026-11-27",
            signal_at="2026-11-27T12:30:00-05:00",
            bar_interval_minutes=30,
        ),
        [
            {
                "symbol": "ABC",
                "timestamp": "2026-11-27T13:00:00-05:00",
                "open": 10.0,
                "high": 10.4,
                "low": 9.8,
                "close": 10.2,
                "volume": 10_000,
            }
        ],
        interval=30,
    )

    assert trade["status"] == "closed"
    assert trade["reason"] == "eod_flat"
    exit_at = datetime.fromisoformat(trade["exit_at"]).astimezone(
        ZoneInfo("America/New_York")
    )
    assert exit_at.isoformat() == "2026-11-27T13:00:00-05:00"
    assert trade["session_close_at"] == "2026-11-27T13:00:00-05:00"
    assert trade["source_bar_sequence_complete"] is True


def test_zero_return_with_full_source_cost_and_time_truth_is_learning_eligible(
    tmp_path: Path,
) -> None:
    with sqlite3.connect(":memory:") as connection:
        sqlite_true = connection.execute("SELECT 1").fetchone()[0]
    bars = [
        {
            "symbol": "ABC",
            "timestamp": "2026-07-15T16:00:00-04:00",
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "volume": 1_000,
        }
    ]
    evidence_sha = hashlib.sha256(
        json.dumps(bars, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    evidence_path = tmp_path / "bars.json"
    evidence_path.write_text(json.dumps(bars), encoding="utf-8")
    receipt = {
        "schema_version": "v2.mover_forward_source_receipt.v1",
        "evidence_mode": "forward_observation",
        "market_date": "2026-07-15",
        "feature_cutoffs_at": ["2026-07-15T15:50:00-04:00"],
        "system_received_at": "2026-07-15T15:52:00-04:00",
        "authoritative_source_captured_at": "2026-07-15T15:52:00-04:00",
        "bars_input_sha256": "a" * 64,
        "context_input_sha256": "b" * 64,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt_sha = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_ref = f"sha256:{receipt_sha}:{receipt_path}"
    snapshot_id = "snapshot-zero-return-evidence"
    snapshot = {
        "snapshot_id": snapshot_id,
        "market_date": "2026-07-15",
        "symbol": "ABC",
        "feature_cutoff_at": "2026-07-15T15:50:00-04:00",
        "evidence_mode": "forward_observation",
        "source_captured_at": "2026-07-15T15:52:00-04:00",
        "system_received_at": "2026-07-15T15:52:00-04:00",
        "forward_receipt_ref": receipt_ref,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    snapshot_sha = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    snapshot_ref = f"sha256:{snapshot_sha}:{snapshot_path}"
    strategy_id = "mover_test_strategy"
    strategy_version = "v1"
    signal_id = stable_id(
        "mover_paper_signal",
        strategy_id,
        strategy_version,
        snapshot_id,
    )
    signal = {
        "signal_id": signal_id,
        "snapshot_id": snapshot_id,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "market_date": "2026-07-15",
        "symbol": "ABC",
        "signal_at": "2026-07-15T15:50:00-04:00",
        "evidence_mode": "forward_observation",
        "source_captured_at": "2026-07-15T15:52:00-04:00",
        "system_received_at": "2026-07-15T15:52:00-04:00",
        "forward_receipt_ref": receipt_ref,
        "entry_reference": 10.0,
        "stop": 9.0,
        "target": 11.0,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    signal_sha = hashlib.sha256(
        json.dumps(signal, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    signal_path = tmp_path / "signal.json"
    signal_path.write_text(json.dumps(signal), encoding="utf-8")
    signal_ref = f"sha256:{signal_sha}:{signal_path}"
    outcome = {
        "schema_version": "v2.mover_paper_trade.v1",
        "outcome_status": "closed",
        "status": "closed",
        "symbol": "ABC",
        "signal_id": signal_id,
        "snapshot_id": snapshot_id,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "direction": "long",
        "evidence_mode": "forward_observation",
        "market_date": "2026-07-15",
        "source_coverage_complete": sqlite_true,
        "source_bar_sequence_complete": sqlite_true,
        "research_only": sqlite_true,
        "broker_execution_enabled": 0,
        "signal_at": "2026-07-15T15:50:00-04:00",
        "source_captured_at": "2026-07-15T15:52:00-04:00",
        "system_received_at": "2026-07-15T15:52:00-04:00",
        "forward_receipt_ref": receipt_ref,
        "signal_entry_reference": 10.0,
        "signal_artifact_ref": signal_ref,
        "snapshot_artifact_ref": snapshot_ref,
        "source_refs": [receipt_ref, signal_ref, snapshot_ref],
        "eligible_entry_at": "2026-07-15T15:55:00-04:00",
        "entry_at": "2026-07-15T15:55:00-04:00",
        "exit_at": "2026-07-15T16:00:00-04:00",
        "session_close_at": "2026-07-15T16:00:00-04:00",
        "entry_source_bar_at": "2026-07-15T16:00:00-04:00",
        "exit_source_bar_at": "2026-07-15T16:00:00-04:00",
        "exit_window_start_at": "2026-07-15T15:55:00-04:00",
        "exit_window_end_at": "2026-07-15T16:00:00-04:00",
        "bar_interval_minutes": 5,
        "bar_timestamp_semantics": "bar_close",
        "entry_fill_policy": "next_bar_open",
        "intrabar_ambiguity_policy": "stop_first",
        "reason": "eod_flat",
        "exit_time_status": "exact_session_close",
        "stop": 9.0,
        "target": 11.0,
        "entry_reference": 10.0,
        "exit_reference": 10.0,
        "entry_price": 10.0,
        "exit_price": 10.0,
        "quantity": 100.0,
        "slippage_bps": 0.0,
        "fee_bps": 0.0,
        "entry_fee": 0.0,
        "exit_fee": 0.0,
        "net_return_pct": 0.0,
        "gross_return_pct": 0.0,
        "fill_return_pct": 0.0,
        "reference_gross_pnl": 0.0,
        "pnl": 0.0,
        "notional_per_trade": 1_000.0,
        "fee_cost": 0.0,
        "slippage_cost": 0.0,
        "total_cost": 0.0,
        "mfe_pct": 1.0,
        "mae_pct": -1.0,
        "bars_evidence_sha256": evidence_sha,
        "bars_evidence_path": str(evidence_path),
    }

    assert type(sqlite_true) is int
    assert outcome_is_learning_eligible(outcome)
    assert not outcome_is_learning_eligible(
        {**outcome, "evidence_mode": "historical_replay"}
    )
    assert not outcome_is_learning_eligible({**outcome, "total_cost": None})
    assert not outcome_is_learning_eligible(
        {**outcome, "entry_at": "2026-07-15T15:55:00"}
    )
    assert not outcome_is_learning_eligible({**outcome, "exit_reference": 10.01})
    assert not outcome_is_learning_eligible({**outcome, "reason": "target"})
    assert not outcome_is_learning_eligible(
        {
            **outcome,
            "signal_id": stable_id(
                "mover_paper_signal",
                strategy_id,
                strategy_version,
                "fabricated-snapshot",
            ),
            "snapshot_id": "fabricated-snapshot",
        }
    )
    zero_volume_bars = [{**bars[0], "volume": 0}]
    zero_volume_sha = hashlib.sha256(
        json.dumps(
            zero_volume_bars,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    zero_volume_path = tmp_path / "zero-volume-bars.json"
    zero_volume_path.write_text(json.dumps(zero_volume_bars), encoding="utf-8")
    assert not outcome_is_learning_eligible(
        {
            **outcome,
            "bars_evidence_sha256": zero_volume_sha,
            "bars_evidence_path": str(zero_volume_path),
        }
    )
    evidence_path.write_text("[]", encoding="utf-8")
    assert not outcome_is_learning_eligible(outcome)


def test_closed_trade_recomputation_rejects_off_grid_bars() -> None:
    bars = [
        {
            "symbol": "ABC",
            "timestamp": "2026-07-15T15:59:00-04:00",
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.0,
            "volume": 1_000,
        }
    ]
    row = {
        "schema_version": "v2.mover_paper_trade.v1",
        "status": "closed",
        "direction": "long",
        "bar_timestamp_semantics": "bar_close",
        "entry_fill_policy": "next_bar_open",
        "intrabar_ambiguity_policy": "stop_first",
        "source_coverage_complete": True,
        "source_bar_sequence_complete": True,
        "research_only": True,
        "broker_execution_enabled": False,
        "bar_interval_minutes": 5,
        "market_date": "2026-07-15",
        "symbol": "ABC",
        "signal_at": "2026-07-15T15:49:00-04:00",
        "eligible_entry_at": "2026-07-15T15:54:00-04:00",
        "entry_at": "2026-07-15T15:54:00-04:00",
        "exit_at": "2026-07-15T15:59:00-04:00",
        "session_close_at": "2026-07-15T16:00:00-04:00",
        "entry_source_bar_at": "2026-07-15T15:59:00-04:00",
        "exit_source_bar_at": "2026-07-15T15:59:00-04:00",
        "evidence_mode": "historical_replay",
    }

    assert not closed_trade_recomputes(row, bars)


def test_review_date_without_complete_mover_truth_is_quarantined(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE daily_review_runs (review_id TEXT, market_date TEXT)"
        )
        connection.execute(
            "INSERT INTO daily_review_runs VALUES (?, ?)",
            ("review-without-denominator", "2026-07-15"),
        )

    result = audit_retained_data(
        db_path=database,
        output_root=tmp_path / "audit-output",
    )
    quarantine = json.loads(
        Path(result["quarantine_manifest_path"]).read_text(encoding="utf-8")
    )

    assert result["quarantined_review_count"] == 1
    assert quarantine["review_ids"] == ["review-without-denominator"]
    assert quarantine["eligible_review_ids"] == []
    assert quarantine["audit_input_fingerprint"].startswith("sha256:")
    assert quarantine["dates_without_valid_complete_mover_truth"] == ["2026-07-15"]


def test_blank_and_orphan_review_ids_are_quarantined(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE daily_review_runs (review_id TEXT, market_date TEXT)"
        )
        connection.execute(
            "CREATE TABLE daily_review_items (item_id TEXT, review_id TEXT)"
        )
        connection.execute(
            "CREATE TABLE learning_backfeed_events (event_id TEXT, review_id TEXT)"
        )
        connection.execute(
            "INSERT INTO daily_review_runs VALUES (?, ?)",
            ("blank-date", ""),
        )
        connection.execute(
            "INSERT INTO daily_review_items VALUES (?, ?)",
            ("orphan-item", "orphan-review"),
        )

    result = audit_retained_data(
        db_path=database,
        output_root=tmp_path / "audit-output",
    )
    quarantine = json.loads(
        Path(result["quarantine_manifest_path"]).read_text(encoding="utf-8")
    )

    assert quarantine["review_ids"] == ["blank-date", "orphan-review"]
    assert quarantine["orphan_review_ids"] == ["orphan-review"]
    assert quarantine["eligible_review_ids"] == []


def test_analysis_requires_content_addressed_ledgers_and_versions_do_not_merge(
    tmp_path: Path,
) -> None:
    spec = strategy_catalog()[0]
    current_key = (spec.strategy_id, spec.version)
    legacy_key = (spec.strategy_id, "v0.9")
    assert _strategy_key(
        {"strategy_id": current_key[0], "strategy_version": current_key[1]}
    ) != _strategy_key(
        {"strategy_id": legacy_key[0], "strategy_version": legacy_key[1]}
    )

    scan_manifest = tmp_path / "paper_scan_deadbeefdeadbeef.json"
    reconcile_manifest = tmp_path / "reconcile_deadbeefdeadbeef.json"
    scan_manifest.write_text("{}", encoding="utf-8")
    reconcile_manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="content fingerprint"):
        analyze(
            scan_manifest_path=scan_manifest,
            reconcile_manifest_path=reconcile_manifest,
            output_root=tmp_path / "lab",
        )


def test_chronological_splits_never_split_a_market_date() -> None:
    rows = [
        {"market_date": f"2026-07-{day:02d}", "signal_id": f"{day}-{index}"}
        for day in range(1, 11)
        for index in range(3)
    ]

    partitions = _chronological_date_partitions(rows)
    discovery = partitions["discovery"]
    validation = partitions["validation"]
    locked = partitions["locked_test"]

    assert discovery.isdisjoint(validation)
    assert discovery.isdisjoint(locked)
    assert validation.isdisjoint(locked)
    assert discovery | validation | locked == {
        str(row["market_date"]) for row in rows
    }


def test_frozen_split_assignments_do_not_migrate_when_future_rows_arrive() -> None:
    spec = strategy_catalog()[0]
    base_rows = [
        {
            "market_date": "2026-07-20",
            "net_return_pct": 1.0,
            "pnl": 10.0,
            "notional_per_trade": 1_000.0,
        }
    ]
    before = _chronological_splits(base_rows, spec=spec)
    after = _chronological_splits(
        base_rows
        + [
            {
                "market_date": "2026-09-01",
                "net_return_pct": -5.0,
                "pnl": -50.0,
                "notional_per_trade": 1_000.0,
            }
        ],
        spec=spec,
    )

    assert _immutable_split_name(spec, "2026-07-20") == "discovery"
    assert _immutable_split_name(spec, "2026-09-01") == "walk_forward"
    assert before["discovery"] == after["discovery"]


def test_inference_clusters_multiple_trades_on_the_same_market_day() -> None:
    rows = [
        {
            "market_date": "2026-07-20",
            "net_return_pct": 1.0,
            "pnl": 10.0,
            "notional_per_trade": 1_000.0,
        }
        for _ in range(50)
    ]
    day_rows = _strategy_day_book_rows(rows)
    metrics = _return_metrics(day_rows)

    assert len(day_rows) == 1
    assert metrics["sample_size"] == 1
    assert metrics["mean_return_lower_95_pct"] is None


def test_verify_marks_empty_evidence_na_and_fails_corrupted_evidence(
    tmp_path: Path,
) -> None:
    empty = verify(output_root=tmp_path / "empty")
    empty_checks = {row["check"]: row for row in empty["checks"]}
    for name in (
        "snapshot_contract_and_cutoff_lineage",
        "snapshot_source_artifacts_match_hashes",
        "paper_signal_contracts_valid",
        "trade_bar_evidence_hashes_match",
    ):
        assert empty_checks[name]["applicable"] is False
        assert empty_checks[name]["passed"] is None
    assert empty["status"] == "passed"
    assert empty["evidence_status"] == "not_available"

    root = tmp_path / "corrupt"
    evidence_path = root / "source_artifacts" / "outcomes" / "bad.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text('[{"close": 10.0}]\n', encoding="utf-8")
    observation_path = root / "trades" / "by_observation" / "obs.json"
    observation_path.parent.mkdir(parents=True, exist_ok=True)
    observation_path.write_text(
        json.dumps(
            {
                "observation_id": "observation-corrupt",
                "status": "pending_missing_outcome",
                "net_return_pct": None,
                "bars_evidence_sha256": "0" * 64,
                "bars_evidence_path": str(evidence_path.resolve()),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    corrupted = verify(output_root=root)
    corrupted_checks = {row["check"]: row for row in corrupted["checks"]}
    assert corrupted["status"] == "failed"
    assert corrupted_checks["trade_bar_evidence_hashes_match"] == {
        "check": "trade_bar_evidence_hashes_match",
        "passed": False,
        "applicable": True,
    }
    assert "trade_bar_evidence_hashes_match" in corrupted["failed_checks"]
