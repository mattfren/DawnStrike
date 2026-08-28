from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from intraday_scanner.alpha.run_contracts import _strategy_contribution_summary
from intraday_scanner.market_calendar import (
    NEXT_SESSION_ACTIVATION_POLICY,
    registration_coverage_inception_date,
)
from intraday_scanner.services.alpha_cycle_service import (
    _merge_strategy_adapter_signals,
    _strategy_adapter_contributor_count,
)
from intraday_scanner.services.morning_strategy_adapter import (
    DATATRUTH_MANIFEST_ATTESTATION_LABEL,
    GOVERNED_SOURCE_LABEL,
    LEGACY_SOURCE_LABEL,
    _attest_bound_datatruth_manifest,
    adapt_prior_session_paper_ops,
    adapt_verified_prior_session_rows,
)
from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.data_truth.core import (
    DATA_TRUTH_MANIFEST_SCHEMA_VERSION,
    SNAPSHOT_ARTIFACT_SCHEMA_VERSION,
    _manifest_payload_hash,
    _snapshot_content_hash_from_hashes,
    _snapshot_id,
)
from intraday_scanner.v2.paper_ops.engine import (
    PaperOpsPaths,
    _config_from_payload,
    _ensure_execution_policy_manifest,
    _execution_policy_fingerprint,
    _execution_policy_fingerprint_payload,
    _fill_entry_block_reason,
    _fill_order,
    _order_entry_block_reason,
    _order_from_pick,
    _picks_from_scan,
    _position_from_fill,
    _validate_order_economics,
)
from intraday_scanner.v2.paper_ops.lifecycle_backtest import PaperOpsLifecycleBacktestEngine
from intraday_scanner.v2.paper_ops.models import (
    LEGACY_PAPER_EXECUTION_POLICY_VERSION,
    PAPER_EXECUTION_POLICY_VERSION,
    PaperOpsConfig,
    PaperPick,
    PaperPickDecision,
    PaperRun,
    PaperRunMode,
    StrategyPaperAccount,
)
from intraday_scanner.v2.paper_ops.storage import write_json
from intraday_scanner.v2.risk import RiskSettings, evaluate_signal_risk
from intraday_scanner.v2.scanner import ScanCard, ScanOutput
from intraday_scanner.v2.strategies import (
    Direction,
    StrategySignal,
    StrategySpec,
    build_strategy_catalog,
)


def _signal(*, direction: str, stop: float, target: float) -> StrategySignal:
    return StrategySignal(
        strategy_id="fixture_strategy",
        strategy_version="v1",
        symbol="AAA",
        signal_index=0,
        direction=direction,
        entry_reference=100.0,
        stop=stop,
        target=target,
        score=90.0,
        evidence=("governance fixture",),
        invalidation="stop",
    )


@pytest.mark.parametrize(
    ("direction", "stop", "target"),
    ((Direction.LONG, 85.0, 122.5), (Direction.SHORT, 115.0, 77.5)),
)
def test_common_risk_gate_accepts_exact_1_50r_and_15_percent_stop(
    direction: str, stop: float, target: float
) -> None:
    decision = evaluate_signal_risk(
        _signal(direction=direction, stop=stop, target=target),
        entry_price=100.0,
        settings=RiskSettings(min_reward_risk=1.0, max_stop_distance_pct=0.25),
    )

    assert decision.allowed is True
    assert decision.reward_risk == pytest.approx(1.5)
    assert "reward_risk_below_minimum" not in decision.warnings
    assert "stop_distance_exceeds_maximum" not in decision.warnings


def test_common_risk_gate_rejects_1_20r_and_20_percent_stop() -> None:
    decision = evaluate_signal_risk(
        _signal(direction=Direction.LONG, stop=80.0, target=124.0),
        entry_price=100.0,
        settings=RiskSettings(min_reward_risk=1.0, max_stop_distance_pct=0.25),
    )

    assert decision.allowed is False
    assert "reward_risk_below_minimum" in decision.warnings
    assert "stop_distance_exceeds_maximum" in decision.warnings


@pytest.mark.parametrize("entry_price", (0.0, -100.0, math.nan, math.inf, -math.inf))
def test_common_risk_gate_rejects_invalid_entry_without_division(
    entry_price: float,
) -> None:
    decision = evaluate_signal_risk(
        _signal(direction=Direction.LONG, stop=85.0, target=122.5),
        entry_price=entry_price,
        settings=RiskSettings(),
    )

    assert decision.allowed is False
    assert decision.quantity == 0
    assert decision.risk_per_unit == 0.0
    assert decision.warnings == ("invalid_stop_or_entry",)


@pytest.mark.parametrize("stop", (math.nan, math.inf, -math.inf))
def test_common_risk_gate_rejects_nonfinite_stop(stop: float) -> None:
    decision = evaluate_signal_risk(
        _signal(direction=Direction.LONG, stop=stop, target=122.5),
        entry_price=100.0,
        settings=RiskSettings(),
    )

    assert decision.allowed is False
    assert decision.quantity == 0
    assert decision.warnings == ("invalid_stop_or_entry",)


def test_legacy_risk_scan_preserves_warning_only_predicate() -> None:
    signal = _signal(direction=Direction.LONG, stop=80.0, target=90.0)
    decision = evaluate_signal_risk(
        signal,
        entry_price=100.0,
        settings=RiskSettings(
            min_reward_risk=1.0,
            max_stop_distance_pct=0.15,
            enforce_governed_common_gates=False,
        ),
        stale=True,
    )

    assert decision.allowed is True
    assert "stop_distance_exceeds_maximum" not in decision.warnings
    assert "reward_risk_below_minimum" in decision.warnings
    assert "stale_data" in decision.warnings


def _paper_run() -> PaperRun:
    return PaperRun(
        run_id="paper_ops:forward:2026-08-28:snapshot",
        mode=PaperRunMode.FORWARD,
        run_date="2026-08-28",
        data_snapshot_id="snapshot",
        created_at="2026-08-28T12:00:00+00:00",
    )


def _attested_datatruth_fixture(tmp_path):
    paths = PaperOpsPaths.create(tmp_path / "paper_ops")
    config = PaperOpsConfig(
        execution_policy_version=PAPER_EXECUTION_POLICY_VERSION,
        universe_id="fixture-universe-v1",
        universe_symbols=("AAA",),
    )
    write_json(paths.state / "paper_ops_config.json", config.to_dict())
    normalized_hash = "a" * 64
    content_hash = _snapshot_content_hash_from_hashes(
        provider_id="fixture_provider",
        timeframe="1d",
        symbols=("AAA",),
        requested_start="2026-08-01",
        requested_end="2026-08-27",
        accepted_start="2026-08-01",
        accepted_end="2026-08-27",
        normalized_hash=normalized_hash,
        source_artifact_hashes=(("raw/source.json", "b" * 64),),
    )
    snapshot_id = _snapshot_id(
        provider_id="fixture_provider",
        timeframe="1d",
        accepted_end="2026-08-27",
        content_hash=content_hash,
    )
    snapshot_relative_path = f"snapshots/{snapshot_id}"
    raw_artifact_hashes = {
        f"{snapshot_relative_path}/raw/source.json": "b" * 64,
    }
    payload = {
        "accepted_bar_count": 1,
        "accepted_end": "2026-08-27",
        "accepted_start": "2026-08-01",
        "artifact_schema_version": SNAPSHOT_ARTIFACT_SCHEMA_VERSION,
        "bar_count": 1,
        "code_version": "fixture",
        "created_at": "2026-08-27T20:00:00+00:00",
        "normalized_artifact_hash": normalized_hash,
        "normalized_artifact_path": f"{snapshot_relative_path}/normalized/ohlcv.csv",
        "provider_id": "fixture_provider",
        "provider_name": "Fixture Provider",
        "raw_artifact_hashes": raw_artifact_hashes,
        "raw_artifact_paths": list(raw_artifact_hashes),
        "rejected_bar_count": 0,
        "requested_end": "2026-08-27",
        "requested_start": "2026-08-01",
        "schema_version": DATA_TRUTH_MANIFEST_SCHEMA_VERSION,
        "skipped_incomplete_bars": 0,
        "snapshot_content_hash": content_hash,
        "snapshot_id": snapshot_id,
        "snapshot_relative_path": snapshot_relative_path,
        "source_url_or_reference": ["fixture://source"],
        "symbols": ["AAA"],
        "timeframe": "1d",
        "validation_status": "passed",
        "warnings": [],
    }
    payload["manifest_payload_hash"] = _manifest_payload_hash(payload)
    alias_path = paths.root.parent / "v2_data_truth" / "manifests" / f"{snapshot_id}.json"
    write_json(alias_path, payload)
    run_manifest = {
        "data_snapshot_content_hash": content_hash,
        "data_snapshot_id": snapshot_id,
        "data_snapshot_manifest_payload_hash": payload["manifest_payload_hash"],
        "data_snapshot_normalized_hash": normalized_hash,
        "data_snapshot_normalized_path": payload["normalized_artifact_path"],
        "data_truth_root_relative": "../v2_data_truth",
        "execution_policy_fingerprint": _execution_policy_fingerprint(config),
        "execution_policy_version": config.execution_policy_version,
        "mode": "forward",
        "run_date": "2026-08-27",
        "schema_version": "v2.paper_ops_manifest.v3",
        "universe_id": config.universe_id,
        "universe_symbols": list(config.universe_symbols),
    }
    run_manifest["manifest_payload_hash"] = _manifest_payload_hash(run_manifest)
    return paths, alias_path, payload, run_manifest


def _paper_pick(
    *,
    entry: float = 100.0,
    stop: float = 85.0,
    target: float = 200.0,
    reward_risk: float = 6.6666666667,
    policy: str = PAPER_EXECUTION_POLICY_VERSION,
) -> PaperPick:
    return PaperPick(
        pick_id="pick-fixture",
        run_id=_paper_run().run_id,
        mode=PaperRunMode.FORWARD,
        trade_date="2026-08-28",
        strategy_id="gap_up_continuation",
        strategy_version="v1.0",
        strategy_status="experimental",
        symbol="AAA",
        signal_time="2026-08-28T13:30:00+00:00",
        direction=Direction.LONG,
        setup_score=90.0,
        entry_reference=entry,
        stop=stop,
        target=target,
        risk_per_unit=abs(entry - stop),
        reward_per_unit=abs(target - entry),
        reward_risk=reward_risk,
        decision=PaperPickDecision.ACCEPTED,
        reason="accepted",
        evidence=("fixture",),
        execution_policy_version=policy,
    )


def _account(order) -> StrategyPaperAccount:
    return StrategyPaperAccount(
        strategy_id=order.strategy_id,
        strategy_version=order.strategy_version,
        starting_equity=100_000.0,
        current_equity=100_000.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        execution_policy_version=order.execution_policy_version,
        strategy_semantics_fingerprint=order.strategy_semantics_fingerprint,
    )


def test_v3_order_admission_rejects_weak_reward_risk_and_stop_distance() -> None:
    config = PaperOpsConfig()
    run = _paper_run()
    weak_rr = _order_from_pick(
        _paper_pick(stop=90.0, target=102.0, reward_risk=0.2), run, config
    )
    wide_stop = _order_from_pick(
        _paper_pick(stop=80.0, target=140.0, reward_risk=2.0), run, config
    )

    assert (
        _order_entry_block_reason(
            weak_rr,
            position_rows=[],
            pending_rows=[],
            account=_account(weak_rr),
            config=config,
            daily_closed_net=0.0,
        )
        == "reward_risk_below_threshold"
    )
    assert (
        _order_entry_block_reason(
            wide_stop,
            position_rows=[],
            pending_rows=[],
            account=_account(wide_stop),
            config=config,
            daily_closed_net=0.0,
        )
        == "stop_distance_exceeds_threshold"
    )


@pytest.mark.parametrize(
    ("direction", "stop", "target"),
    ((Direction.LONG, 85.0, 90.0), (Direction.SHORT, 115.0, 110.0)),
)
def test_v3_order_admission_rejects_target_on_wrong_side(
    direction: str, stop: float, target: float
) -> None:
    pick = _paper_pick(stop=stop, target=target, reward_risk=2.0)
    pick = replace(pick, direction=direction)
    order = _order_from_pick(pick, _paper_run(), PaperOpsConfig())
    assert (
        _order_entry_block_reason(
            order,
            position_rows=[],
            pending_rows=[],
            account=_account(order),
            config=PaperOpsConfig(),
            daily_closed_net=0.0,
        )
        == "invalid_level_geometry"
    )


def test_v3_order_validation_recomputes_reward_risk_from_levels() -> None:
    config = PaperOpsConfig()
    order = _order_from_pick(_paper_pick(), _paper_run(), config)
    payload = order.to_dict()
    payload["reward_risk"] = 1.5
    with pytest.raises(ValueError, match="governed risk gates"):
        _validate_order_economics(payload, config)


def test_fill_admission_rechecks_actual_fill_stop_distance_after_gap() -> None:
    config = PaperOpsConfig()
    run = _paper_run()
    order = _order_from_pick(_paper_pick(), run, config)
    bar = MarketBar(
        symbol="AAA",
        timestamp=datetime(2026, 8, 29, 13, 30, tzinfo=timezone.utc),
        open=120.0,
        high=121.0,
        low=119.0,
        close=120.0,
        volume=1_000,
    )
    fill = _fill_order(order, bar, run, config)
    position = _position_from_fill(order, fill)

    assert _fill_entry_block_reason(
        order,
        fill=fill,
        position=position,
        fill_bar=bar,
        position_rows=[],
        pending_rows=[],
        account=_account(order),
        config=config,
        daily_closed_net=0.0,
    ) == "fill_stop_distance_exceeds_threshold"


def test_legacy_scan_keeps_accepted_pick_but_order_and_fill_are_management_only() -> None:
    strategy = next(
        item for item in build_strategy_catalog() if item.strategy_id == "gap_up_continuation"
    )
    card = ScanCard(
        symbol="AAA",
        timestamp=datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc),
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version,
        direction=Direction.LONG,
        status="candidate",
        setup_score=90.0,
        entry_trigger="trigger",
        stop=80.0,
        target=124.0,
        risk_per_share=20.0,
        reward=24.0,
        reward_risk=1.2,
        invalidation="stop",
        evidence=("legacy fixture",),
        historical_summary="",
        warnings=(),
        data_snapshot_id="snapshot",
        run_manifest_id="manifest",
    )
    config = PaperOpsConfig(
        execution_policy_version=LEGACY_PAPER_EXECUTION_POLICY_VERSION,
        min_reward_risk=1.0,
        max_stop_distance_pct=1.0,
    )
    picks = _picks_from_scan(
        ScanOutput(cards=(card,), no_setup=(), warnings=()),
        (strategy,),
        _paper_run(),
        config,
        (),
    )

    assert picks[0].decision is PaperPickDecision.ACCEPTED
    order = _order_from_pick(picks[0], _paper_run(), config)
    assert _order_entry_block_reason(
        order,
        position_rows=[],
        pending_rows=[],
        account=_account(order),
        config=config,
        daily_closed_net=0.0,
    ) == "legacy_policy_management_only"


def test_legacy_lifecycle_replay_keeps_v2_1_20r_trade_while_forward_is_blocked() -> None:
    def generate(
        strategy: StrategySpec,
        _dataset: MarketDataset,
        symbol: str,
        _bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        if index != 0:
            return None
        return StrategySignal(
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            symbol=symbol,
            signal_index=index,
            direction=Direction.LONG,
            entry_reference=100.0,
            stop=80.0,
            target=124.0,
            score=90.0,
            evidence=("legacy replay fixture",),
            invalidation="stop",
        )

    strategy = StrategySpec(
        strategy_id="legacy_replay_fixture",
        version="v1",
        status="production",
        description="legacy lifecycle replay fixture",
        compatible_timeframe="1d",
        required_data_fields=("open", "high", "low", "close", "volume"),
        parameters={},
        indicators=(),
        entry_logic="fixture",
        exit_logic="fixture",
        stop_logic="fixture",
        target_logic="fixture",
        position_sizing_assumption="fixed risk",
        known_failure_modes=(),
        validation_status="fixture",
        generate_signal=generate,
    )
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    dataset = MarketDataset(
        dataset_id="legacy-replay-fixture",
        source_kind="test_fixture",
        timeframe="1d",
        bars_by_symbol={
            "AAA": tuple(
                MarketBar(
                    symbol="AAA",
                    timestamp=start + timedelta(days=index),
                    open=values[0],
                    high=values[1],
                    low=values[2],
                    close=values[3],
                    volume=1_000_000,
                )
                for index, values in enumerate(
                    (
                        (100.0, 101.0, 99.0, 100.0),
                            (100.0, 125.0, 99.0, 124.0),
                            (124.0, 125.0, 123.0, 124.0),
                    )
                )
            )
        },
    )
    config = PaperOpsConfig(
        execution_policy_version=LEGACY_PAPER_EXECUTION_POLICY_VERSION,
        min_reward_risk=1.0,
        max_stop_distance_pct=1.0,
    )

    engine = PaperOpsLifecycleBacktestEngine(config)
    engine.run((strategy,), dataset)
    assert engine.audit is not None
    assert len(engine.audit.picks) == 1
    assert any(pick.decision is PaperPickDecision.ACCEPTED for pick in engine.audit.picks)
    assert len(engine.audit.orders_created) == 1
    assert len(engine.audit.fills) == 1
    assert len(engine.audit.closes) == 1

    forward_engine = PaperOpsLifecycleBacktestEngine(config, mode=PaperRunMode.FORWARD)
    forward_engine.run((strategy,), dataset)
    assert forward_engine.audit is not None
    assert forward_engine.audit.orders_created == ()
    assert forward_engine.audit.fills == ()
    assert forward_engine.audit.entry_blocks
    assert forward_engine.audit.entry_blocks[0]["reason"] == "legacy_policy_management_only"

    order = _order_from_pick(
        next(pick for pick in engine.audit.picks if pick.decision is PaperPickDecision.ACCEPTED),
        _paper_run(),
        config,
    )
    assert _order_entry_block_reason(
        order,
        position_rows=[],
        pending_rows=[],
        account=_account(order),
        config=config,
        daily_closed_net=0.0,
        management_only=True,
    ) == "legacy_policy_management_only"


def test_v2_config_fingerprint_is_stable_and_v3_serializes_stop_cap() -> None:
    legacy_payload = {
        "execution_policy_version": LEGACY_PAPER_EXECUTION_POLICY_VERSION,
        "min_reward_risk": 1.0,
        "schema_version": "v2.paper_ops_config.v4",
    }
    legacy = _config_from_payload(legacy_payload)
    legacy_changed_cap = _config_from_payload(
        {**legacy_payload, "max_stop_distance_pct": 0.15}
    )
    v3 = _config_from_payload({"execution_policy_version": PAPER_EXECUTION_POLICY_VERSION})

    assert legacy.min_reward_risk == 1.0
    assert legacy.max_stop_distance_pct == 1.0
    assert _execution_policy_fingerprint(legacy) == _execution_policy_fingerprint(
        legacy_changed_cap
    )
    assert v3.min_reward_risk == 1.5
    assert v3.max_stop_distance_pct == 0.15
    assert "max_stop_distance_pct" in _execution_policy_fingerprint_payload(v3)
    assert _execution_policy_fingerprint(legacy) != _execution_policy_fingerprint(v3)
    with pytest.raises(ValueError, match="execution_policy_version is unsupported"):
        _config_from_payload({"execution_policy_version": "paperops_unknown_v9"})


def test_v2_manifest_with_live_exposure_remains_manageable_but_v3_rollover_is_blocked(
    tmp_path,
) -> None:
    paths = PaperOpsPaths.create(tmp_path)
    legacy = _config_from_payload(
        {
            "execution_policy_version": LEGACY_PAPER_EXECUTION_POLICY_VERSION,
            "min_reward_risk": 1.0,
            "schema_version": "v2.paper_ops_config.v4",
        }
    )
    registered_at = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    policy_entry = {
        "activation_policy": NEXT_SESSION_ACTIVATION_POLICY,
        "configuration": _execution_policy_fingerprint_payload(legacy),
        "fingerprint": _execution_policy_fingerprint(legacy),
        "registered_at": registered_at.isoformat(),
        "coverage_inception_date": registration_coverage_inception_date(
            registered_at, NEXT_SESSION_ACTIVATION_POLICY
        ).isoformat(),
    }
    write_json(
        paths.state / "execution_policy_manifest.json",
        {
            "active_execution_policy_version": LEGACY_PAPER_EXECUTION_POLICY_VERSION,
            "policies": {LEGACY_PAPER_EXECUTION_POLICY_VERSION: policy_entry},
            "schema_version": "v2.paper_execution_policy_manifest.v1",
        },
    )
    write_json(paths.state / "pending_orders.json", [{"order_id": "live-v2"}])

    _ensure_execution_policy_manifest(paths, legacy)
    v3 = _config_from_payload({})
    v3 = replace(v3, execution_policy_version=PAPER_EXECUTION_POLICY_VERSION)
    with pytest.raises(ValueError, match="live forward exposure"):
        _ensure_execution_policy_manifest(paths, v3)


def test_datatruth_acl_sealed_attestation_revalidates_alias_identity(tmp_path) -> None:
    paths, _alias_path, _payload, run_manifest = _attested_datatruth_fixture(tmp_path)

    assert _attest_bound_datatruth_manifest(paths, run_manifest) == (
        DATATRUTH_MANIFEST_ATTESTATION_LABEL
    )


def test_datatruth_acl_sealed_attestation_rejects_mutated_or_missing_alias(tmp_path) -> None:
    paths, alias_path, payload, run_manifest = _attested_datatruth_fixture(tmp_path)
    payload["raw_artifact_hashes"][
        next(iter(payload["raw_artifact_hashes"]))
    ] = "c" * 64
    write_json(alias_path, payload)
    with pytest.raises(ValueError, match="manifest payload hash mismatch"):
        _attest_bound_datatruth_manifest(paths, run_manifest)

    alias_path.unlink()
    with pytest.raises(ValueError, match="missing or malformed"):
        _attest_bound_datatruth_manifest(paths, run_manifest)


def test_datatruth_acl_sealed_attestation_rejects_accepted_end_mismatch(tmp_path) -> None:
    paths, _alias_path, _payload, run_manifest = _attested_datatruth_fixture(tmp_path)
    run_manifest["run_date"] = "2026-08-26"
    run_manifest["manifest_payload_hash"] = _manifest_payload_hash(run_manifest)

    with pytest.raises(ValueError, match="accepted end conflicts"):
        _attest_bound_datatruth_manifest(paths, run_manifest)


def test_datatruth_acl_sealed_attestation_rejects_config_policy_or_universe_drift(
    tmp_path,
) -> None:
    paths, _alias_path, _payload, run_manifest = _attested_datatruth_fixture(tmp_path)
    run_manifest["execution_policy_fingerprint"] = "0" * 64
    run_manifest["manifest_payload_hash"] = _manifest_payload_hash(run_manifest)

    with pytest.raises(ValueError, match="execution policy fingerprint conflicts"):
        _attest_bound_datatruth_manifest(paths, run_manifest)

    config = PaperOpsConfig(
        execution_policy_version=PAPER_EXECUTION_POLICY_VERSION,
        universe_id="fixture-universe-v1",
        universe_symbols=("BBB",),
    )
    write_json(paths.state / "paper_ops_config.json", config.to_dict())
    run_manifest["execution_policy_fingerprint"] = _execution_policy_fingerprint(config)
    run_manifest["universe_symbols"] = list(config.universe_symbols)
    run_manifest["manifest_payload_hash"] = _manifest_payload_hash(run_manifest)

    with pytest.raises(ValueError, match="DataTruth/config universe conflicts"):
        _attest_bound_datatruth_manifest(paths, run_manifest)


def _current_row(*, lane: str = "mover") -> dict[str, object]:
    return {
        "ticker": "AAA",
        "symbol": "AAA",
        "universe_lane": lane,
        "evidence_lane": "core" if lane == "mover+core" else lane,
        "source_count": 1,
        "source_quality_status": "VERIFIED",
        "freshness_status": "FRESH",
        "halt_status": "CLEAR",
        "sec_risk_status": "CLEAR",
        "corporate_action_status": "CLEAR",
        "current_price": 100.0,
        "current_volume": 10_000,
        "as_of_timestamp": "2026-08-28T12:59:00+00:00",
        "as_of_timestamp_is_source_observation": True,
        "source_identity": "current:fixture",
        "spread_pct": 0.0,
        "research_only": True,
        "broker_execution": "disabled",
        "broker_execution_enabled": False,
        "fixture_only": False,
    }


def _prior_row(*, strategy_id: str, signal_id: str = "prior-signal") -> dict[str, object]:
    strategy = next(item for item in build_strategy_catalog() if item.strategy_id == strategy_id)
    from intraday_scanner.v2.paper_ops.engine import _strategy_semantics_fingerprint

    return {
        "ticker": "AAA",
        "symbol": "AAA",
        "signal_id": signal_id,
        "strategy_id": strategy_id,
        "strategy_version": strategy.version,
        "strategy_semantics_fingerprint": _strategy_semantics_fingerprint(strategy),
        "execution_policy_version": LEGACY_PAPER_EXECUTION_POLICY_VERSION,
        "direction": Direction.LONG,
        "entry_reference": 100.0,
        "stop": 85.0,
        "target": 122.5,
        "reward_risk": 1.5,
        "setup_score": 90.0,
        "signal_time": "2026-08-27T13:30:00+00:00",
        "trade_date": "2026-08-27",
        "decision_status": "accepted",
    }


def test_prior_adapter_rebuilds_current_evidence_and_preserves_strategy_identity() -> None:
    rows = adapt_verified_prior_session_rows(
        [_prior_row(strategy_id="gap_up_continuation")],
        current_rows=[_current_row()],
        prior_session_date="2026-08-27",
        current_market_date="2026-08-28",
        current_snapshot_id="current-snapshot",
        current_source_identity="current:fixture",
        decision_at="2026-08-28T12:59:00+00:00",
        current_universe_membership={"AAA"},
        provenance={"ledger_sha256": "l"},
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["strategy_id"] == "gap_up_continuation"
    assert row["strategy_version"] == "v1.0"
    assert row["strategy_adapter"] == LEGACY_SOURCE_LABEL
    assert row["research_only"] is True
    assert row["broker_execution_enabled"] is False
    assert row["condition_results"]["gap_threshold"]["observed_at"].startswith("2026-08-27")
    assert row["condition_results"]["point_in_time_ohlcv"]["observed_at"].startswith("2026-08-28")
    assert row["condition_results"]["within_risk_budget"]["status"] == "PASS"


def test_prior_adapter_accepts_exact_governed_v3_source_with_distinct_provenance() -> None:
    source = _prior_row(strategy_id="gap_up_continuation")
    source["execution_policy_version"] = PAPER_EXECUTION_POLICY_VERSION
    rows = adapt_verified_prior_session_rows(
        [source],
        current_rows=[_current_row()],
        prior_session_date="2026-08-27",
        current_market_date="2026-08-28",
        current_snapshot_id="current-snapshot",
        current_source_identity="current:fixture",
        decision_at="2026-08-28T12:59:00+00:00",
        current_universe_membership={"AAA"},
        provenance={
            "current_code_sha": "a" * 40,
            "paper_ops_root": "C:/state/v2_paper_ops_live",
        },
    )

    assert len(rows) == 1
    assert rows[0]["strategy_adapter"] == GOVERNED_SOURCE_LABEL
    assert rows[0]["research_adapter_source_policy"] == PAPER_EXECUTION_POLICY_VERSION
    assert rows[0]["prior_session_paper_ops"]["source_provenance"]["current_code_sha"] == "a" * 40


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-08-27T12:59:00+00:00",
        "2026-08-28T13:00:00+00:00",
    ),
)
def test_prior_adapter_rejects_stale_or_future_current_observation(timestamp: str) -> None:
    current = _current_row()
    current["as_of_timestamp"] = timestamp
    assert (
        adapt_verified_prior_session_rows(
            [_prior_row(strategy_id="gap_up_continuation")],
            current_rows=[current],
            prior_session_date="2026-08-27",
            current_market_date="2026-08-28",
            current_snapshot_id="snapshot",
            current_source_identity="source",
            decision_at="2026-08-28T12:59:00+00:00",
            current_universe_membership={"AAA"},
        )
        == []
    )


def test_prior_adapter_rejects_newer_generic_time_masking_stale_source_timestamp() -> None:
    current = _current_row()
    current["source_timestamp"] = "2026-08-27T12:59:00+00:00"
    assert (
        adapt_verified_prior_session_rows(
            [_prior_row(strategy_id="gap_up_continuation")],
            current_rows=[current],
            prior_session_date="2026-08-27",
            current_market_date="2026-08-28",
            current_snapshot_id="snapshot",
            current_source_identity="source",
            decision_at="2026-08-28T12:59:00+00:00",
            current_universe_membership={"AAA"},
        )
        == []
    )


def test_prior_adapter_rejects_newer_as_of_masking_unbound_stale_enrichment() -> None:
    current = _current_row()
    current["enrichment_observed_at"] = "2026-08-27T12:59:00+00:00"
    assert (
        adapt_verified_prior_session_rows(
            [_prior_row(strategy_id="gap_up_continuation")],
            current_rows=[current],
            prior_session_date="2026-08-27",
            current_market_date="2026-08-28",
            current_snapshot_id="snapshot",
            current_source_identity="source",
            decision_at="2026-08-28T12:59:00+00:00",
            current_universe_membership={"AAA"},
        )
        == []
    )


@pytest.mark.parametrize(
    "avoid_reasons",
    ("formula_avoid: gap below minimum", ["formula_avoid: gap below minimum"]),
)
def test_prior_adapter_rejects_serialized_avoid_reasons(avoid_reasons: object) -> None:
    current = _current_row()
    current["avoid_reasons"] = avoid_reasons
    assert (
        adapt_verified_prior_session_rows(
            [_prior_row(strategy_id="gap_up_continuation")],
            current_rows=[current],
            prior_session_date="2026-08-27",
            current_market_date="2026-08-28",
            current_snapshot_id="snapshot",
            current_source_identity="source",
            decision_at="2026-08-28T12:59:00+00:00",
            current_universe_membership={"AAA"},
        )
        == []
    )


@pytest.mark.parametrize("current_price", (85.0, 122.5))
def test_prior_adapter_rejects_long_setup_at_or_beyond_stop_or_target(current_price: float) -> None:
    current = _current_row()
    current["current_price"] = current_price
    assert (
        adapt_verified_prior_session_rows(
            [_prior_row(strategy_id="gap_up_continuation")],
            current_rows=[current],
            prior_session_date="2026-08-27",
            current_market_date="2026-08-28",
            current_snapshot_id="snapshot",
            current_source_identity="source",
            decision_at="2026-08-28T12:59:00+00:00",
            current_universe_membership={"AAA"},
        )
        == []
    )


@pytest.mark.parametrize(
    ("current_patch", "expected_count"),
    (
        ({"current_price": 0.0, "close": 100.0}, 0),
        ({"current_price": -1.0, "close": 100.0}, 0),
        ({"current_price": math.nan, "close": 100.0}, 0),
        ({"close": 100.0}, 1),
        ({"current_price": 100.0, "close": 84.0}, 1),
    ),
)
def test_prior_adapter_current_price_precedence_fails_closed(
    current_patch: dict[str, object], expected_count: int
) -> None:
    current = _current_row()
    current.pop("current_price")
    current.update(current_patch)
    rows = adapt_verified_prior_session_rows(
        [_prior_row(strategy_id="gap_up_continuation")],
        current_rows=[current],
        prior_session_date="2026-08-27",
        current_market_date="2026-08-28",
        current_snapshot_id="snapshot",
        current_source_identity="source",
        decision_at="2026-08-28T12:59:00+00:00",
        current_universe_membership={"AAA"},
    )

    assert len(rows) == expected_count


def test_prior_adapter_rejects_short_setup_at_or_beyond_stop_or_target() -> None:
    source = _prior_row(strategy_id="gap_up_continuation")
    source.update({"direction": Direction.SHORT, "stop": 115.0, "target": 77.5})
    current = _current_row()
    current["current_price"] = 115.0
    assert (
        adapt_verified_prior_session_rows(
            [source],
            current_rows=[current],
            prior_session_date="2026-08-27",
            current_market_date="2026-08-28",
            current_snapshot_id="snapshot",
            current_source_identity="source",
            decision_at="2026-08-28T12:59:00+00:00",
            current_universe_membership={"AAA"},
        )
        == []
    )
    current["current_price"] = 77.5
    assert (
        adapt_verified_prior_session_rows(
            [source],
            current_rows=[current],
            prior_session_date="2026-08-27",
            current_market_date="2026-08-28",
            current_snapshot_id="snapshot",
            current_source_identity="source",
            decision_at="2026-08-28T12:59:00+00:00",
            current_universe_membership={"AAA"},
        )
        == []
    )


def test_prior_adapter_rejects_weak_levels_missing_membership_and_above_ceiling() -> None:
    weak = _prior_row(strategy_id="gap_up_continuation")
    weak["stop"] = 80.0
    weak["target"] = 104.0
    weak["reward_risk"] = 1.2
    blocked_current = _current_row()
    blocked_current["enrichment_fallback_status"] = "applied_research_only_above_ceiling"

    assert (
        adapt_verified_prior_session_rows(
            [weak],
            current_rows=[_current_row()],
            prior_session_date="2026-08-27",
            current_market_date="2026-08-28",
            current_snapshot_id="snapshot",
            current_source_identity="source",
            decision_at="2026-08-28T12:59:00+00:00",
            current_universe_membership=set(),
        )
        == []
    )
    assert (
        adapt_verified_prior_session_rows(
            [_prior_row(strategy_id="gap_up_continuation")],
            current_rows=[blocked_current],
            prior_session_date="2026-08-27",
            current_market_date="2026-08-28",
            current_snapshot_id="snapshot",
            current_source_identity="source",
            decision_at="2026-08-28T12:59:00+00:00",
            current_universe_membership={"AAA"},
        )
        == []
    )


def test_production_adapter_fails_closed_without_verified_prior_artifacts(tmp_path) -> None:
    result = adapt_prior_session_paper_ops(
        output_root=tmp_path,
        market_date="2026-08-28",
        current_candidates=[_current_row()],
        current_snapshot_id="snapshot",
        current_source_identity="source",
        current_code_sha="a" * 40,
        current_universe_membership={"AAA"},
        decision_at="2026-08-28T12:59:00+00:00",
    )

    assert result["status"] == "BLOCKED_PRIOR_SESSION_EVIDENCE"
    assert result["rows"] == []
    assert result["broker_execution_enabled"] is False


def test_strategy_merge_dedupes_ticker_and_prefers_eligible_adapter_over_ineligible_alpha() -> None:
    alpha = {
        "ticker": "AAA",
        "strategy_id": "alphaops_v5",
        "strategy_version": "v5",
        "signal_id": "alpha-signal",
        "alpha_score": 99.0,
        "research_pick_eligible": False,
        "broker_execution_enabled": False,
    }
    adapter = {
        **_prior_row(strategy_id="gap_up_continuation"),
        "strategy_adapter": LEGACY_SOURCE_LABEL,
        "signal_id": "adapter-signal",
        "research_pick_eligible": True,
        "strategy_decision_receipt": {"receipt_id": "receipt-adapter"},
        "receipt_id": "receipt-adapter",
        "broker_execution_enabled": False,
    }
    merged = _merge_strategy_adapter_signals([alpha, adapter])

    assert len(merged) == 1
    assert merged[0]["strategy_id"] == "gap_up_continuation"
    assert merged[0]["strategy_contributor_count"] == 2
    assert merged[0]["strategy_contributor_ids"] == ["alphaops_v5", "gap_up_continuation"]
    assert merged[0]["strategy_decision_receipts"] == [{"receipt_id": "receipt-adapter"}]
    assert merged[0]["broker_execution_enabled"] is False


def test_strategy_merge_prefers_eligible_alpha_over_eligible_adapter() -> None:
    alpha = {
        "ticker": "AAA",
        "strategy_id": "alphaops_v5",
        "strategy_version": "v5",
        "signal_id": "alpha-signal",
        "alpha_score": 1.0,
        "research_pick_eligible": True,
    }
    adapter = {
        "ticker": "AAA",
        "strategy_id": "gap_up_continuation",
        "strategy_version": "v1.0",
        "signal_id": "adapter-signal",
        "strategy_adapter": LEGACY_SOURCE_LABEL,
        "research_pick_eligible": True,
    }

    assert _merge_strategy_adapter_signals([alpha, adapter])[0]["strategy_id"] == "alphaops_v5"


def test_strategy_adapter_contributor_count_includes_governed_v3_source() -> None:
    merged = _merge_strategy_adapter_signals(
        [
            {
                "ticker": "AAA",
                "strategy_id": "gap_up_continuation",
                "strategy_version": "v1.0",
                "signal_id": "adapter-signal",
                "strategy_adapter": GOVERNED_SOURCE_LABEL,
                "research_pick_eligible": True,
            }
        ]
    )

    assert _strategy_adapter_contributor_count(merged) == 1


def test_run_contract_contributions_keep_frozen_slate_truth_on_retry() -> None:
    frozen = {
        "ticker": "FROZEN",
        "strategy_id": "prior_strategy",
        "strategy_version": "v1",
        "strategy_semantics_fingerprint": "prior-fingerprint",
        "signal_id": "frozen-signal",
        "research_pick_eligible": True,
        "receipt_id": "frozen-receipt",
    }
    current_retry = {
        "ticker": "FROZEN",
        "strategy_id": "retry_strategy",
        "strategy_version": "v2",
        "strategy_semantics_fingerprint": "retry-fingerprint",
        "signal_id": "retry-signal",
        "research_pick_eligible": True,
    }
    summary = _strategy_contribution_summary(
        [current_retry],
        [frozen],
        source_summary={},
    )

    assert summary["prior_strategy"]["slate_count"] == 1
    assert summary["prior_strategy"]["selected_symbols"] == ["FROZEN"]
    assert summary["prior_strategy"]["receipt_ids"] == ["frozen-receipt"]
    assert summary["prior_strategy"]["strategy_versions"] == ["v1"]
    assert summary["prior_strategy"]["strategy_semantics_fingerprints"] == [
        "prior-fingerprint"
    ]
    assert summary["prior_strategy"]["candidate_count"] == 0
    assert summary["retry_strategy"]["candidate_count"] == 1
    assert summary["retry_strategy"]["slate_count"] == 0
    assert summary["retry_strategy"]["selected_symbols"] == []
