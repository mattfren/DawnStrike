from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from intraday_scanner.v2.mover_pattern_lab.contracts import (
    MoverPaperSignal,
    ProspectiveMoverSnapshot,
    stable_id,
)
from intraday_scanner.v2.mover_pattern_lab.core import (
    MoverLabPaths,
    _claim_session_signal,
    build_snapshots_from_bars,
    init,
    paper_scan,
    reconcile_paper_signals,
    verify,
)
from intraday_scanner.v2.mover_pattern_lab.strategies import (
    evaluate_snapshot,
    strategy_catalog,
)


def _snapshot_row(**overrides: Any) -> dict[str, Any]:
    catalyst_url = "https://www.sec.gov/Archives/edgar/data/example/filing.htm"
    catalyst_artifact = "sha256:" + "a" * 64 + ":C:/evidence/catalyst.json"
    universe_ref = "universe://premarket-screen/2026-07-15/abc"
    row: dict[str, Any] = {
        "snapshot_id": "snapshot-20260715-abc-1000",
        "market_date": "2026-07-15",
        "symbol": "ABC",
        "observed_at": "2026-07-15T10:00:00-04:00",
        "feature_cutoff_at": "2026-07-15T10:00:00-04:00",
        "universe_selected_at": "2026-07-15T08:30:00-04:00",
        "universe_source_ref": universe_ref,
        "universe_selection_method": "premarket_screen",
        "context_observed_at": "2026-07-15T09:59:00-04:00",
        "price": 11.50,
        "previous_close": 10.00,
        "session_open": 10.80,
        "opening_range_high": 11.30,
        "opening_range_low": 10.60,
        "opening_range_complete": True,
        "running_vwap": 11.00,
        "cumulative_volume": 2_500_000,
        "cumulative_dollar_volume": 27_500_000.0,
        "same_clock_rvol": 4.0,
        "spread_pct": 0.001,
        "split_adjusted": True,
        "reverse_split_days": 180,
        "reverse_split_lookback_clear": True,
        "recent_offering_days": 60,
        "offering_lookback_clear": True,
        "halt_state": "clear",
        "source_conflict": False,
        "catalyst_verified": True,
        "catalyst_published_at": "2026-07-15T09:20:00-04:00",
        "catalyst_source_url": catalyst_url,
        "catalyst_source_type": "sec_filing",
        "catalyst_artifact_ref": catalyst_artifact,
        "source_refs": [
            "bars://immutable/abc-20260715",
            universe_ref,
            catalyst_url,
            catalyst_artifact,
        ],
        "raw_payload": {
            "bar_timestamp_semantics": "bar_close",
            "same_clock_baseline_session_count": 20,
        },
    }
    row.update(overrides)
    return row


def _strategy(strategy_id: str):
    return next(
        spec for spec in strategy_catalog() if spec.strategy_id == strategy_id
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _retain_snapshot(
    output_root: Path,
    row: dict[str, Any],
) -> dict[str, Any]:
    init(output_root=output_root)
    refs: list[str] = []
    hashes: dict[str, str] = {}
    for name, payload in (
        ("bars", [{"bar": "prefix"}]),
        ("context", {"context": "point_in_time"}),
        ("catalyst", {"filing": "captured"}),
    ):
        artifact_path = output_root / "source_artifacts" / f"{name}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        hashes[name] = digest
        refs.append(f"sha256:{digest}:{artifact_path.resolve()}")
    row = {
        **row,
        "universe_source_ref": refs[1],
        "catalyst_artifact_ref": refs[2],
        "source_refs": [*refs, row["catalyst_source_url"]],
        "raw_payload": {
            "bar_timestamp_semantics": "bar_close",
            "bar_interval_minutes": 5,
            "bar_prefix_sha256": hashes["bars"],
            "context_row_sha256": hashes["context"],
        },
    }
    row["snapshot_id"] = stable_id(
        "mover_snapshot",
        row["symbol"],
        row["market_date"],
        row["feature_cutoff_at"],
        hashes["bars"],
        hashes["context"],
        row.get("evidence_mode") or "historical_replay",
        row.get("source_captured_at") or "unknown_capture_time",
    )
    retained = ProspectiveMoverSnapshot.from_mapping(row).to_dict()
    by_id = output_root / "snapshots" / "by_id" / f"{row['snapshot_id']}.json"
    by_id.parent.mkdir(parents=True, exist_ok=True)
    by_id.write_text(
        json.dumps(retained, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return retained


def _write_bars(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "symbol",
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)


def _write_context(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _paper_signal(**overrides: object) -> dict[str, object]:
    spec = _strategy("mover_opening_drive_rvol_v1")
    signal: dict[str, object] = {
        "strategy_id": spec.strategy_id,
        "strategy_version": spec.version,
        "strategy_semantics_fingerprint": spec.to_dict()[
            "semantics_fingerprint"
        ],
        "market_date": "2026-07-15",
        "symbol": "ABC",
        "signal_at": "2026-07-15T09:45:00-04:00",
        "snapshot_id": "snapshot-signal-abc",
        "entry_reference": 10.0,
        "stop": 9.5,
        "target": 11.0,
        "score": 0.5,
        "evidence": ["test_evidence"],
        "warnings": ["test_only"],
        "features": {
            "same_clock_rvol": 3.0,
            "bar_interval_minutes": 15,
        },
        "source_refs": ["bars://immutable/abc"],
        "research_only": True,
        "broker_execution_enabled": False,
    }
    signal.update(overrides)
    if "signal_id" not in signal:
        signal["signal_id"] = stable_id(
            "mover_paper_signal",
            signal["strategy_id"],
            signal["strategy_version"],
            signal["snapshot_id"],
        )
    return signal


def _retain_signal_ledger(
    output_root: Path,
    raw_signals: list[dict[str, object]],
) -> list[dict[str, Any]]:
    paths = MoverLabPaths.create(output_root)
    retained_signals: list[dict[str, Any]] = []
    for raw_signal in raw_signals:
        signal = dict(raw_signal)
        signal_at = datetime.fromisoformat(str(signal["signal_at"]))
        entry_reference = float(signal["entry_reference"])
        snapshot = _retain_snapshot(
            output_root,
            _snapshot_row(
                market_date=str(signal["market_date"]),
                symbol=str(signal["symbol"]),
                observed_at=signal_at.isoformat(),
                feature_cutoff_at=signal_at.isoformat(),
                universe_selected_at=(signal_at - timedelta(hours=1)).isoformat(),
                context_observed_at=(signal_at - timedelta(minutes=1)).isoformat(),
                price=entry_reference,
                previous_close=entry_reference * 0.9,
                session_open=entry_reference * 0.95,
                opening_range_high=entry_reference,
                opening_range_low=float(signal["stop"]),
                running_vwap=entry_reference * 0.99,
            ),
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
        retained_signals.append(retained)
    ledger_fingerprint = hashlib.sha256(
        json.dumps(
            retained_signals,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    canonical_path = paths.signals / f"signals_{ledger_fingerprint[:16]}.jsonl"
    canonical_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in retained_signals),
        encoding="utf-8",
    )
    return retained_signals


def _reconcile_case(
    tmp_path: Path,
    *,
    bars: list[dict[str, object]],
    signal: dict[str, object] | None = None,
    slippage_bps: float = 0.0,
    fee_bps: float = 0.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    output_root = tmp_path / "lab"
    retained_signals = _retain_signal_ledger(
        output_root,
        [signal or _paper_signal()],
    )
    signals_path = tmp_path / "signals.jsonl"
    signals_path.write_text(
        json.dumps(retained_signals[0], sort_keys=True) + "\n",
        encoding="utf-8",
    )
    bars_path = tmp_path / "bars.csv"
    _write_bars(bars_path, bars)
    result = reconcile_paper_signals(
        signals_path=signals_path,
        bars_csv=bars_path,
        notional_per_trade=1_000.0,
        slippage_bps=slippage_bps,
        fee_bps=fee_bps,
        bar_interval_minutes=15,
        bar_timestamp_semantics="bar_close",
        output_root=output_root,
    )
    rows = _read_jsonl(Path(result["trades_path"]))
    assert len(rows) == 1
    return result, rows[0]


def test_prospective_snapshot_is_timezone_safe_frozen_and_round_trips() -> None:
    raw_payload = {
        "bar_timestamp_semantics": "bar_close",
        "same_clock_baseline_session_count": 20,
    }
    row = _snapshot_row(raw_payload=raw_payload)

    snapshot = ProspectiveMoverSnapshot.from_mapping(row)

    assert snapshot.symbol == "ABC"
    assert snapshot.gap_pct == pytest.approx(8.0)
    assert snapshot.observed_at.tzinfo is not None
    assert snapshot.feature_cutoff_at.tzinfo is not None
    assert ProspectiveMoverSnapshot.from_mapping(snapshot.to_dict()) == snapshot
    with pytest.raises(FrozenInstanceError):
        snapshot.price = 99.0  # type: ignore[misc]

    raw_payload["same_clock_baseline_session_count"] = 0
    assert snapshot.raw_payload["same_clock_baseline_session_count"] == 20


@pytest.mark.parametrize(
    "overrides",
    [
        {"observed_at": "2026-07-15T10:00:00"},
        {"feature_cutoff_at": "2026-07-15T10:00:00"},
        {"observed_at": "2026-07-15T10:01:00-04:00"},
        {
            "market_date": "2026-07-14",
            "observed_at": "2026-07-15T10:00:00-04:00",
        },
    ],
)
def test_prospective_snapshot_rejects_bad_clock_or_cutoff(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        ProspectiveMoverSnapshot.from_mapping(_snapshot_row(**overrides))


def test_prospective_snapshot_rejects_future_and_eod_leakage() -> None:
    with pytest.raises(ValueError, match="future|outcome"):
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(future_high=14.25)
        )

    with pytest.raises(ValueError, match="future|outcome"):
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(raw_payload={"outcome_return_pct": 12.5})
        )

    with pytest.raises(ValueError, match="catalyst|publication|cutoff"):
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(
                catalyst_published_at="2026-07-15T10:01:00-04:00",
            )
        )

    with pytest.raises(ValueError, match="verified catalyst"):
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(catalyst_source_url="")
        )


def test_frozen_strategy_catalog_has_exact_research_only_identities() -> None:
    catalog = strategy_catalog()

    assert {
        f"{spec.strategy_id}@{spec.version}" for spec in catalog
    } == {
        "mover_opening_drive_rvol_v1@v1.0",
        "mover_verified_catalyst_gap_hold_v1@v1.0",
    }
    assert all(spec.research_only for spec in catalog)
    assert all(not spec.broker_execution_enabled for spec in catalog)
    assert all(spec.validation_status.startswith("forward_") for spec in catalog)


def test_missing_point_in_time_truth_is_skipped_not_coerced_to_zero() -> None:
    snapshot = ProspectiveMoverSnapshot.from_mapping(
        _snapshot_row(
            same_clock_rvol=None,
            spread_pct=None,
            cumulative_dollar_volume=None,
        )
    )

    decision = evaluate_snapshot(
        _strategy("mover_opening_drive_rvol_v1"),
        snapshot,
    )

    assert decision.signal is None
    assert decision.decision == "skipped"
    missing_text = " ".join((*decision.missing_features, decision.reason))
    assert "same_clock_rvol" in missing_text
    assert "spread_pct" in missing_text
    assert "cumulative_dollar_volume" in missing_text
    assert snapshot.to_dict()["same_clock_rvol"] is None


def test_recent_reverse_split_is_a_hard_veto() -> None:
    snapshot = ProspectiveMoverSnapshot.from_mapping(
        _snapshot_row(
            reverse_split_days=30,
            reverse_split_lookback_clear=False,
        )
    )

    decision = evaluate_snapshot(
        _strategy("mover_opening_drive_rvol_v1"),
        snapshot,
    )

    assert decision.signal is None
    assert decision.decision in {"rejected", "vetoed"}
    assert "reverse_split" in " ".join((*decision.vetoes, decision.reason))


def test_opening_drive_emits_auditable_two_r_paper_signal() -> None:
    snapshot = ProspectiveMoverSnapshot.from_mapping(_snapshot_row())

    decision = evaluate_snapshot(
        _strategy("mover_opening_drive_rvol_v1"),
        snapshot,
    )

    assert decision.decision in {"accepted", "paper_signal"}
    assert decision.signal is not None
    signal = decision.signal
    assert signal.entry_reference == snapshot.price
    assert signal.stop == snapshot.opening_range_low
    assert (signal.target - signal.entry_reference) / (
        signal.entry_reference - signal.stop
    ) == pytest.approx(2.0)
    assert signal.evidence
    assert set(snapshot.source_refs).issubset(signal.source_refs)
    assert signal.to_dict()["broker_execution_enabled"] is False
    assert signal.to_dict()["research_only"] is True


def test_forward_strategy_cannot_emit_before_frozen_activation_date() -> None:
    row = _snapshot_row()
    universe_ref = "sha256:" + "c" * 64 + ":C:/evidence/universe.json"
    receipt_ref = "sha256:" + "d" * 64 + ":C:/evidence/receipt.json"
    row.update(
        {
            "evidence_mode": "forward_observation",
            "source_captured_at": "2026-07-15T10:02:00-04:00",
            "system_received_at": "2026-07-15T10:02:00-04:00",
            "forward_receipt_ref": receipt_ref,
            "universe_source_ref": universe_ref,
            "source_refs": [
                universe_ref,
                receipt_ref,
                row["catalyst_source_url"],
                row["catalyst_artifact_ref"],
            ],
        }
    )
    snapshot = ProspectiveMoverSnapshot.from_mapping(row)
    decision = evaluate_snapshot(
        _strategy("mover_opening_drive_rvol_v1"),
        snapshot,
    )

    assert decision.decision == "skipped"
    assert decision.reason == "strategy_not_active_for_forward_observation"
    assert decision.signal is None


def test_verified_catalyst_gap_hold_requires_and_retains_catalyst_lineage() -> None:
    snapshot = ProspectiveMoverSnapshot.from_mapping(_snapshot_row())
    spec = _strategy("mover_verified_catalyst_gap_hold_v1")

    accepted = evaluate_snapshot(spec, snapshot)

    assert accepted.decision in {"accepted", "paper_signal"}
    assert accepted.signal is not None
    assert snapshot.catalyst_source_url in accepted.signal.source_refs
    assert accepted.signal.features["catalyst_verified"] is True
    assert (accepted.signal.target - accepted.signal.entry_reference) / (
        accepted.signal.entry_reference - accepted.signal.stop
    ) == pytest.approx(2.0)

    unverified = evaluate_snapshot(
        spec,
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(
                catalyst_verified=None,
                catalyst_published_at=None,
                catalyst_source_url="",
                catalyst_source_type="",
            )
        ),
    )
    assert unverified.signal is None
    assert unverified.decision == "skipped"
    assert "catalyst" in " ".join(
        (*unverified.missing_features, unverified.reason)
    )


def test_paper_scan_enforces_one_signal_per_session_across_runs(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "lab"
    first_path = tmp_path / "first.jsonl"
    later_path = tmp_path / "later.jsonl"
    first_row = _retain_snapshot(
        output_root,
        _snapshot_row(
                snapshot_id="snapshot-20260715-abc-0945",
                observed_at="2026-07-15T09:45:00-04:00",
                feature_cutoff_at="2026-07-15T09:45:00-04:00",
                context_observed_at="2026-07-15T09:44:00-04:00",
        ),
    )
    later_row = _retain_snapshot(
        output_root,
        _snapshot_row(
            snapshot_id="snapshot-20260715-abc-1000-later",
            observed_at="2026-07-15T10:00:00-04:00",
            feature_cutoff_at="2026-07-15T10:00:00-04:00",
            context_observed_at="2026-07-15T09:59:00-04:00",
        ),
    )
    first_path.write_text(
        json.dumps(first_row, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    later_path.write_text(
        json.dumps(later_row, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    first = paper_scan(
        snapshots_path=first_path,
        expected_market_dates=("2026-07-15",),
        output_root=output_root,
    )
    later = paper_scan(
        snapshots_path=later_path,
        expected_market_dates=("2026-07-15",),
        output_root=output_root,
    )
    repeated = paper_scan(
        snapshots_path=later_path,
        expected_market_dates=("2026-07-15",),
        output_root=output_root,
    )

    assert first["signal_count"] == 1
    assert later["signal_count"] == 1
    assert repeated["signal_count"] == 1
    later_decisions = _read_jsonl(Path(later["decisions_path"]))
    suppressed = next(
        row
        for row in later_decisions
        if row["strategy_id"] == "mover_opening_drive_rvol_v1"
    )
    assert suppressed["reason"] == (
        "already_signaled_this_strategy_symbol_session"
    )
    assert suppressed["signal"] is None
    verification = verify(output_root=output_root)
    signal_gate = next(
        row
        for row in verification["checks"]
        if row["check"] == "one_paper_signal_per_strategy_symbol_session"
    )
    assert signal_gate["passed"] is True


def test_snapshot_builder_uses_only_same_clock_cumulative_volume(
    tmp_path: Path,
) -> None:
    bars_path = tmp_path / "bars.csv"
    rows: list[dict[str, object]] = []
    for session_day in ("2026-07-13", "2026-07-14"):
        rows.extend(
            [
                {
                    "symbol": "ABC",
                    "timestamp": f"{session_day}T09:35:00-04:00",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.9,
                    "close": 10.1,
                    "volume": 400,
                },
                {
                    "symbol": "ABC",
                    "timestamp": f"{session_day}T09:40:00-04:00",
                    "open": 10.1,
                    "high": 10.2,
                    "low": 10.0,
                    "close": 10.1,
                    "volume": 100,
                },
                {
                    "symbol": "ABC",
                    "timestamp": f"{session_day}T09:45:00-04:00",
                    "open": 10.1,
                    "high": 10.3,
                    "low": 10.0,
                    "close": 10.2,
                    "volume": 500,
                },
                {
                    "symbol": "ABC",
                    "timestamp": f"{session_day}T16:00:00-04:00",
                    "open": 10.2,
                    "high": 10.3,
                    "low": 9.9,
                    "close": 10.0,
                    "volume": 100_000,
                },
            ]
        )
    rows.extend(
        [
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T09:35:00-04:00",
                "open": 10.8,
                "high": 11.0,
                "low": 10.7,
                "close": 10.9,
                "volume": 1_000,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T09:40:00-04:00",
                "open": 10.9,
                "high": 11.1,
                "low": 10.8,
                "close": 11.0,
                "volume": 500,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T09:45:00-04:00",
                "open": 10.9,
                "high": 11.3,
                "low": 10.8,
                "close": 11.2,
                "volume": 1_500,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T15:00:00-04:00",
                "open": 11.2,
                "high": 12.0,
                "low": 11.1,
                "close": 11.8,
                "volume": 500_000,
            },
        ]
    )
    _write_bars(bars_path, rows)
    context_path = tmp_path / "context.csv"
    universe_ref = "universe://premarket-screen/2026-07-15/abc"
    _write_context(
        context_path,
        [
            {
                "market_date": "2026-07-15",
                "symbol": "ABC",
                "context_observed_at": "2026-07-15T09:44:00-04:00",
                "universe_selected_at": "2026-07-15T08:30:00-04:00",
                "universe_source_ref": universe_ref,
                "universe_selection_method": "premarket_screen",
                "spread_pct": 0.5,
                "split_adjusted": True,
                "reverse_split_days": 180,
                "recent_offering_days": 60,
                "halt_state": "clear",
                "source_conflict": False,
                "catalyst_verified": False,
                "catalyst_published_at": "",
                "catalyst_source_url": "",
                "catalyst_source_type": "",
                "source_refs": universe_ref,
            }
        ],
    )

    result = build_snapshots_from_bars(
        bars_csv=bars_path,
        context_csv=context_path,
        market_date="2026-07-15",
        cutoffs=("09:45",),
        min_baseline_sessions=2,
        bar_timestamp_semantics="bar_close",
        output_root=tmp_path / "lab",
    )

    assert result["status"] == "passed"
    assert result["snapshot_count"] == 1
    snapshots = _read_jsonl(Path(result["snapshot_path"]))
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot["cumulative_volume"] == pytest.approx(3_000)
    assert snapshot["same_clock_rvol"] == pytest.approx(3.0)
    assert snapshot["price"] == pytest.approx(11.2)
    assert snapshot["raw_payload"]["same_clock_baseline_session_count"] == 2
    assert "daily_high" not in snapshot
    assert "outcome_return_pct" not in snapshot


def test_reconciliation_uses_next_bar_costs_and_null_missing_outcomes(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "lab"
    signals_path = tmp_path / "signals.jsonl"
    signals = _retain_signal_ledger(
        output_root,
        [
        _paper_signal(target=10.8, snapshot_id="snapshot-signal-abc"),
        _paper_signal(
            symbol="MISS",
            snapshot_id="snapshot-signal-miss",
            entry_reference=20.0,
            stop=19.0,
            target=22.0,
            features={
                "same_clock_rvol": 2.5,
                "bar_interval_minutes": 15,
            },
            source_refs=["bars://immutable/miss"],
        ),
        ],
    )
    signals_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in signals),
        encoding="utf-8",
    )
    bars_path = tmp_path / "outcome_bars.csv"
    _write_bars(
        bars_path,
        [
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T09:45:00-04:00",
                "open": 9.9,
                "high": 10.1,
                "low": 9.8,
                "close": 10.0,
                "volume": 1_000,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T10:00:00-04:00",
                "open": 10.0,
                "high": 11.0,
                "low": 9.9,
                "close": 10.8,
                "volume": 2_000,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T15:55:00-04:00",
                "open": 10.8,
                "high": 11.1,
                "low": 10.7,
                "close": 11.0,
                "volume": 3_000,
            },
        ],
    )
    result = reconcile_paper_signals(
        signals_path=signals_path,
        bars_csv=bars_path,
        notional_per_trade=1_000.0,
        slippage_bps=10.0,
        fee_bps=1.0,
        bar_interval_minutes=15,
        bar_timestamp_semantics="bar_close",
        output_root=output_root,
    )

    assert result["status"] == "passed_with_pending"
    assert result["closed_trade_count"] == 1
    assert result["pending_trade_count"] == 1
    trades = {row["symbol"]: row for row in _read_jsonl(Path(result["trades_path"]))}
    closed = trades["ABC"]
    assert closed["status"] == "closed"
    assert closed["reason"] == "target"
    entry_source_at = datetime.fromisoformat(closed["entry_source_bar_at"])
    assert entry_source_at.astimezone(ZoneInfo("America/New_York")).isoformat() == (
        "2026-07-15T10:00:00-04:00"
    )
    assert closed["entry_reference"] == pytest.approx(10.0)
    assert closed["entry_price"] > closed["entry_reference"]
    assert closed["exit_price"] < closed["exit_reference"]
    assert closed["total_cost"] > 0
    assert closed["net_return_pct"] < closed["gross_return_pct"]

    pending = trades["MISS"]
    assert pending["status"] == "pending_missing_outcome"
    assert pending["gross_return_pct"] is None
    assert pending["net_return_pct"] is None
    assert pending["pnl"] is None

    verification = verify(output_root=output_root)
    assert verification["status"] == "passed"
    assert all(
        check["passed"]
        for check in verification["checks"]
        if check["applicable"]
    )


def test_incomplete_session_without_exit_bar_stays_pending(tmp_path: Path) -> None:
    result, trade = _reconcile_case(
        tmp_path,
        bars=[
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T09:45:00-04:00",
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 1_000,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T10:00:00-04:00",
                "open": 10.0,
                "high": 10.3,
                "low": 9.8,
                "close": 10.1,
                "volume": 2_000,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T12:00:00-04:00",
                "open": 10.1,
                "high": 10.4,
                "low": 9.9,
                "close": 10.2,
                "volume": 3_000,
            },
        ],
    )

    assert result["status"] == "passed_with_pending"
    assert result["pending_trade_count"] == 1
    assert result["closed_trade_count"] == 0
    assert trade["status"] == "pending_incomplete_session_bars"
    assert trade["reason"] == "no_stop_or_target_and_incomplete_session_grid"
    assert trade["entry_price"] is not None
    assert trade["exit_price"] is None
    assert trade["gross_return_pct"] is None
    assert trade["net_return_pct"] is None
    assert trade["pnl"] is None
    assert trade["total_cost"] is None


def test_next_bar_open_through_stop_is_not_entered_and_has_no_return(
    tmp_path: Path,
) -> None:
    result, trade = _reconcile_case(
        tmp_path,
        bars=[
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T09:45:00-04:00",
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 1_000,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T10:00:00-04:00",
                "open": 9.40,
                "high": 12.0,
                "low": 9.0,
                "close": 11.5,
                "volume": 5_000,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T16:00:00-04:00",
                "open": 11.5,
                "high": 12.0,
                "low": 11.0,
                "close": 11.8,
                "volume": 6_000,
            },
        ],
    )

    assert result["status"] == "passed"
    assert result["not_entered_count"] == 1
    assert result["closed_trade_count"] == 0
    assert trade["status"] == "not_entered"
    assert trade["reason"] == "next_bar_fill_at_or_below_stop"
    assert trade["next_bar_open"] == pytest.approx(9.40)
    assert trade["entry_price"] is None
    assert trade["exit_price"] is None
    assert trade["gross_return_pct"] is None
    assert trade["net_return_pct"] is None
    assert trade["pnl"] is None
    assert trade["total_cost"] is None


def test_later_gap_through_stop_exits_at_bar_open_not_stale_stop(
    tmp_path: Path,
) -> None:
    result, trade = _reconcile_case(
        tmp_path,
        bars=[
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T09:45:00-04:00",
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 1_000,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T10:00:00-04:00",
                "open": 10.0,
                "high": 10.4,
                "low": 9.8,
                "close": 10.1,
                "volume": 2_000,
            },
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T10:15:00-04:00",
                "open": 9.0,
                "high": 9.3,
                "low": 8.8,
                "close": 9.1,
                "volume": 4_000,
            },
        ],
    )

    assert result["status"] == "passed"
    assert result["closed_trade_count"] == 1
    assert trade["status"] == "closed"
    assert trade["reason"] == "stop_gap"
    assert trade["stop"] == pytest.approx(9.5)
    assert trade["exit_reference"] == pytest.approx(9.0)
    assert trade["exit_price"] == pytest.approx(9.0)
    assert trade["exit_reference"] < trade["stop"]
    assert trade["net_return_pct"] < 0


def test_verify_confirms_frozen_forward_research_boundary(tmp_path: Path) -> None:
    output_root = tmp_path / "lab"
    initialized = init(output_root=output_root)

    result = verify(output_root=output_root)

    assert initialized["strategy_count"] == 2
    assert initialized["research_only"] is True
    assert initialized["broker_execution_enabled"] is False
    assert result["status"] == "passed"
    assert {check["check"] for check in result["checks"]} >= {
        "strategy_identity_unique",
        "all_strategies_forward_observation_only",
        "no_broker_execution_code",
        "missing_returns_are_null",
        "closed_trades_have_source_and_costs",
    }
