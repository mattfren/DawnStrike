from __future__ import annotations

import json
from datetime import datetime
from types import MappingProxyType
from typing import Any

import pytest

from intraday_scanner.v2.mover_pattern_lab.contracts import (
    MoverPaperSignal,
    MoverStrategySpec,
    ProspectiveMoverSnapshot,
)

UNIVERSE_REF = "scanner://scheduled/2026-07-15/abc"
BAR_REF = "bars://immutable/abc-20260715"
CATALYST_REF = "https://www.sec.gov/Archives/edgar/data/example/filing.htm"
SEMANTICS_FINGERPRINT = "a" * 64


def _snapshot_row(**overrides: Any) -> dict[str, Any]:
    catalyst_artifact = "sha256:" + "b" * 64 + ":C:/evidence/catalyst.json"
    row: dict[str, Any] = {
        "snapshot_id": "snapshot-20260715-abc-1000",
        "market_date": "2026-07-15",
        "symbol": "ABC",
        "observed_at": "2026-07-15T10:00:00-04:00",
        "feature_cutoff_at": "2026-07-15T10:00:00-04:00",
        "universe_selected_at": "2026-07-15T09:20:00-04:00",
        "universe_source_ref": UNIVERSE_REF,
        "universe_selection_method": "premarket_screen",
        "context_observed_at": "2026-07-15T09:58:00-04:00",
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
        "catalyst_published_at": "2026-07-15T09:15:00-04:00",
        "catalyst_source_url": CATALYST_REF,
        "catalyst_source_type": "sec_filing",
        "catalyst_artifact_ref": catalyst_artifact,
        "source_refs": [UNIVERSE_REF, BAR_REF, CATALYST_REF, catalyst_artifact],
        "raw_payload": {
            "bar_timestamp_semantics": "bar_close",
            "baseline": {"session_count": 20, "volumes": [10, 12, 15]},
        },
    }
    row.update(overrides)
    return row


def _signal_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_version": "v2.mover_paper_signal.v1",
        "signal_id": "signal-abc",
        "strategy_id": "mover_opening_drive_rvol_v1",
        "strategy_version": "v1.0",
        "strategy_semantics_fingerprint": SEMANTICS_FINGERPRINT,
        "market_date": "2026-07-15",
        "symbol": "ABC",
        "signal_at": "2026-07-15T10:00:00-04:00",
        "snapshot_id": "snapshot-20260715-abc-1000",
        "direction": "long",
        "entry_reference": 11.50,
        "stop": 10.60,
        "target": 13.30,
        "score": 0.82,
        "evidence": ["same_clock_rvol=4.0"],
        "warnings": [],
        "source_refs": [UNIVERSE_REF, BAR_REF],
        "features": {
            "same_clock_rvol": 4.0,
            "context": {"spread_pct": 0.001, "flags": ["clear"]},
        },
        "research_only": True,
        "broker_execution_enabled": False,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "method",
    [
        "premarket_screen",
        "scheduled_universe",
        "prior_session_watchlist",
        "live_intraday_scan",
    ],
)
def test_snapshot_accepts_only_named_prospective_universe_methods(
    method: str,
) -> None:
    snapshot = ProspectiveMoverSnapshot.from_mapping(
        _snapshot_row(universe_selection_method=method)
    )

    assert snapshot.universe_selection_method == method
    assert snapshot.universe_source_ref in snapshot.source_refs
    assert json.loads(json.dumps(snapshot.to_dict()))["universe_selection_method"] == method


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("universe_selection_method", "eod_top_gainers", "selection_method"),
        ("universe_source_ref", "", "universe_source_ref"),
        ("universe_selected_at", "2026-07-15T10:01:00-04:00", "after"),
        ("context_observed_at", "2026-07-15T10:01:00-04:00", "after"),
        ("universe_selected_at", "2026-07-15T09:20:00", "timezone"),
        ("context_observed_at", "2026-07-15T09:58:00", "timezone"),
    ],
)
def test_snapshot_rejects_unproven_or_post_cutoff_provenance(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ProspectiveMoverSnapshot.from_mapping(_snapshot_row(**{field: value}))

    if field == "universe_source_ref" and value == "":
        with pytest.raises(ValueError, match="universe_source_ref"):
            ProspectiveMoverSnapshot.from_mapping(
                _snapshot_row(
                    universe_source_ref="scanner://not-retained",
                )
            )


def test_snapshot_raw_payload_is_recursively_immutable_and_defensive() -> None:
    raw_payload = {
        "baseline": {"session_count": 20, "volumes": [10, 12, 15]},
    }
    snapshot = ProspectiveMoverSnapshot.from_mapping(
        _snapshot_row(raw_payload=raw_payload)
    )

    assert isinstance(snapshot.raw_payload, MappingProxyType)
    assert isinstance(snapshot.raw_payload["baseline"], MappingProxyType)
    assert snapshot.raw_payload["baseline"]["volumes"] == (10, 12, 15)
    with pytest.raises(TypeError):
        snapshot.raw_payload["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.raw_payload["baseline"]["session_count"] = 0  # type: ignore[index]

    raw_payload["baseline"]["session_count"] = 0
    assert snapshot.raw_payload["baseline"]["session_count"] == 20

    serialized = snapshot.to_dict()
    serialized["raw_payload"]["baseline"]["session_count"] = 1
    assert snapshot.raw_payload["baseline"]["session_count"] == 20
    json.dumps(serialized)


def test_strategy_parameters_are_recursively_immutable_and_json_safe() -> None:
    parameters: dict[str, Any] = {
        "thresholds": {"min_rvol": 2.5},
        "gates": ["spread", "liquidity"],
    }
    strategy = MoverStrategySpec(
        strategy_id="mover_contract_test_v1",
        version="v1.0",
        display_name="Contract Test",
        description="Tests immutable strategy semantics.",
        parameters=parameters,
        required_features=("same_clock_rvol",),
        entry_logic="test entry",
        stop_logic="test stop",
        target_logic="test target",
    )

    parameters["thresholds"]["min_rvol"] = 0
    assert strategy.parameters["thresholds"]["min_rvol"] == 2.5
    with pytest.raises(TypeError):
        strategy.parameters["thresholds"]["min_rvol"] = 1  # type: ignore[index]

    serialized = strategy.to_dict()
    assert len(serialized["semantics_fingerprint"]) == 64
    serialized["parameters"]["thresholds"]["min_rvol"] = 99
    assert strategy.parameters["thresholds"]["min_rvol"] == 2.5
    json.dumps(serialized)


def test_signal_round_trip_validates_identity_prices_and_provenance() -> None:
    features = {
        "same_clock_rvol": 4.0,
        "context": {"spread_pct": 0.001, "flags": ["clear"]},
    }
    signal = MoverPaperSignal.from_mapping(_signal_row(features=features))

    assert signal.strategy_semantics_fingerprint == SEMANTICS_FINGERPRINT
    assert signal.stop < signal.entry_reference < signal.target
    assert isinstance(signal.features, MappingProxyType)
    assert isinstance(signal.features["context"], MappingProxyType)
    with pytest.raises(TypeError):
        signal.features["context"]["spread_pct"] = 1.0  # type: ignore[index]

    features["context"]["spread_pct"] = 5.0
    assert signal.features["context"]["spread_pct"] == pytest.approx(0.001)

    serialized = signal.to_dict()
    assert serialized["strategy_semantics_fingerprint"] == SEMANTICS_FINGERPRINT
    assert serialized["direction"] == "long"
    serialized["features"]["context"]["spread_pct"] = 2.0
    assert signal.features["context"]["spread_pct"] == pytest.approx(0.001)
    assert MoverPaperSignal.from_mapping(signal.to_dict()) == signal
    json.dumps(serialized)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"signal_at": "2026-07-15T10:00:00"}, "timezone"),
        ({"signal_at": "2026-07-14T10:00:00-04:00"}, "market_date"),
        ({"signal_at": "2026-07-15T08:00:00-04:00"}, "regular trading"),
        ({"stop": 11.50}, "stop.*entry_reference.*target"),
        ({"target": 11.50}, "stop.*entry_reference.*target"),
        ({"entry_reference": 0}, "stop.*entry_reference.*target"),
        ({"source_refs": []}, "source_refs"),
        ({"strategy_semantics_fingerprint": "abc"}, "64-hex"),
        ({"schema_version": "v2.mover_paper_signal.v2"}, "schema_version"),
        ({"direction": "short"}, "direction='long'"),
        ({"research_only": False}, "research-only"),
        ({"broker_execution_enabled": True}, "execution"),
        ({"features": {"future_high": 14.0}}, "future|outcome"),
    ],
)
def test_signal_rejects_invalid_or_nonresearch_identity(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        MoverPaperSignal.from_mapping(_signal_row(**overrides))


def test_direct_signal_constructor_rejects_naive_timestamp() -> None:
    row = _signal_row()
    row.pop("direction")
    row.pop("research_only")
    row.pop("broker_execution_enabled")
    row["signal_at"] = datetime(2026, 7, 15, 10, 0)

    with pytest.raises(ValueError, match="timezone"):
        MoverPaperSignal.from_mapping(row)


def test_forward_snapshot_requires_near_cutoff_capture_and_hashed_universe() -> None:
    receipt_ref = "sha256:" + "d" * 64 + ":C:/evidence/receipt.json"
    with pytest.raises(ValueError, match="source_captured_at"):
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(evidence_mode="forward_observation")
        )
    with pytest.raises(ValueError, match="sha256"):
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(
                evidence_mode="forward_observation",
                source_captured_at="2026-07-15T10:02:00-04:00",
                system_received_at="2026-07-15T10:02:00-04:00",
                forward_receipt_ref=receipt_ref,
                source_refs=[receipt_ref, *_snapshot_row()["source_refs"]],
            )
        )
    hashed_ref = "sha256:" + "c" * 64 + ":C:/evidence/universe.json"
    snapshot = ProspectiveMoverSnapshot.from_mapping(
        _snapshot_row(
            evidence_mode="forward_observation",
            source_captured_at="2026-07-15T10:02:00-04:00",
            system_received_at="2026-07-15T10:02:00-04:00",
            forward_receipt_ref=receipt_ref,
            universe_source_ref=hashed_ref,
            source_refs=[
                hashed_ref,
                receipt_ref,
                BAR_REF,
                CATALYST_REF,
                _snapshot_row()["catalyst_artifact_ref"],
            ],
        )
    )
    assert snapshot.evidence_mode == "forward_observation"

    with pytest.raises(ValueError, match="five minutes"):
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(
                evidence_mode="forward_observation",
                source_captured_at="2026-07-15T10:06:00-04:00",
                system_received_at="2026-07-15T10:06:00-04:00",
                forward_receipt_ref=receipt_ref,
                universe_source_ref=hashed_ref,
                source_refs=[
                    hashed_ref,
                    receipt_ref,
                    BAR_REF,
                    CATALYST_REF,
                    _snapshot_row()["catalyst_artifact_ref"],
                ],
            )
        )


def test_catalyst_must_have_been_published_by_context_observation() -> None:
    with pytest.raises(ValueError, match="context observation"):
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(
                context_observed_at="2026-07-15T09:58:00-04:00",
                catalyst_published_at="2026-07-15T09:59:00-04:00",
            )
        )


def test_verified_clear_risk_state_cannot_contradict_recent_event() -> None:
    with pytest.raises(ValueError, match="reverse-split lookback"):
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(
                reverse_split_days=30,
                reverse_split_lookback_clear=True,
            )
        )
    with pytest.raises(ValueError, match="offering lookback"):
        ProspectiveMoverSnapshot.from_mapping(
            _snapshot_row(
                recent_offering_days=10,
                offering_lookback_clear=True,
            )
        )
