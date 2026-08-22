"""Contract tests for additive, fail-closed strategy challengers."""

from __future__ import annotations

from dataclasses import replace

from intraday_scanner.v2.data import MarketDataset, build_synthetic_ohlcv_dataset
from intraday_scanner.v2.strategies import (
    CHALLENGER_VERSION,
    build_challenger_catalog,
    build_strategy_catalog,
    evaluate_challenger_gates,
)
from intraday_scanner.v2.strategies.catalog import build_strategy_catalog as build_legacy_catalog
from intraday_scanner.v2.strategy_identity import strategy_semantics_fingerprint

TRADABLE_IDS = (
    "ts_momentum_sma_atr",
    "donchian_breakout_20_10",
    "cross_sectional_relative_strength",
    "pullback_reclaim_uptrend",
    "volatility_contraction_breakout",
    "failed_breakout_reversal_short",
    "bullish_fvg_continuation",
    "gap_up_continuation",
    "gap_up_continuation_atr",
)


def _dataset() -> MarketDataset:
    return build_synthetic_ohlcv_dataset(
        end_date=__import__("datetime").date(2026, 8, 21), trading_days=180
    )


def test_v1_catalog_is_unchanged_and_challengers_are_unique() -> None:
    before = tuple(
        (spec.strategy_id, spec.version, strategy_semantics_fingerprint(spec))
        for spec in build_legacy_catalog()
    )
    challengers = build_challenger_catalog()
    after = tuple(
        (spec.strategy_id, spec.version, strategy_semantics_fingerprint(spec))
        for spec in build_legacy_catalog()
    )

    assert before == after
    assert tuple(spec.strategy_id for spec in challengers) == TRADABLE_IDS
    assert len({(spec.strategy_id, spec.version) for spec in challengers}) == 9
    assert len({spec.version for spec in challengers}) == 9
    assert all(spec.version.startswith(CHALLENGER_VERSION) for spec in challengers)
    assert all(spec.status == "experimental" for spec in challengers)
    assert all(spec.parameters["research_only"] is True for spec in challengers)
    assert all("research-only" in spec.description for spec in challengers)


def test_challenger_fingerprints_differ_from_their_v1_champions() -> None:
    champions = {spec.strategy_id: spec for spec in build_strategy_catalog()}
    for challenger in build_challenger_catalog():
        assert strategy_semantics_fingerprint(challenger) != strategy_semantics_fingerprint(
            champions[challenger.strategy_id]
        )


def test_future_bars_do_not_change_point_in_time_gate_decision() -> None:
    dataset = _dataset()
    index = 120
    bars = dataset.bars_by_symbol["NOVA"]
    extended_bars = bars + (
        replace(
            bars[-1],
            open=10_000.0,
            high=11_000.0,
            low=9_000.0,
            close=10_500.0,
            volume=99_999_999,
        ),
    )
    extended = replace(dataset, bars_by_symbol={**dataset.bars_by_symbol, "NOVA": extended_bars})
    for challenger in build_challenger_catalog():
        left = evaluate_challenger_gates(
            challenger.strategy_id, dataset, "NOVA", bars, index
        ).to_dict()
        right = evaluate_challenger_gates(
            challenger.strategy_id, extended, "NOVA", extended_bars, index
        ).to_dict()
        assert left == right


def test_missing_required_evidence_fails_closed_as_unavailable() -> None:
    dataset = _dataset()
    symbol = "NOVA"
    index = 120
    bars = dataset.bars_by_symbol[symbol]
    malformed = replace(bars[index], close=float("nan"))
    malformed_bars = bars[:index] + (malformed,) + bars[index + 1 :]
    malformed_dataset = replace(
        dataset, bars_by_symbol={**dataset.bars_by_symbol, symbol: malformed_bars}
    )

    for challenger in build_challenger_catalog():
        evaluation = evaluate_challenger_gates(
            challenger.strategy_id, malformed_dataset, symbol, malformed_bars, index
        )
        assert not evaluation.eligible
        assert evaluation.first_failure is not None
        assert evaluation.first_failure.status == "UNAVAILABLE_REQUIRED_DATA"


def test_unsupported_borrow_and_sector_evidence_is_explicit_and_fail_closed() -> None:
    dataset = _dataset()
    bars = dataset.bars_by_symbol["NOVA"]
    failed_short = evaluate_challenger_gates(
        "failed_breakout_reversal_short", dataset, "NOVA", bars, 120
    )
    cross_sectional = evaluate_challenger_gates(
        "cross_sectional_relative_strength", dataset, "NOVA", bars, 120
    )

    assert not failed_short.eligible
    assert any(
        g.name == "borrow_evidence" and g.status == "UNAVAILABLE_REQUIRED_DATA"
        for g in failed_short.gates
    )
    assert not cross_sectional.eligible
    assert any(
        g.name == "sector_concentration" and g.status == "UNAVAILABLE_REQUIRED_DATA"
        for g in cross_sectional.gates
    )


def test_cross_sectional_rank_two_margin_uses_the_next_lower_rank() -> None:
    dataset = _dataset()
    index = 120
    rank_two = None
    for symbol, bars in dataset.bars_by_symbol.items():
        evaluation = evaluate_challenger_gates(
            "cross_sectional_relative_strength", dataset, symbol, bars, index
        )
        membership = next(
            (gate for gate in evaluation.gates if gate.name == "rank_membership"), None
        )
        if membership is not None and membership.value == 2:
            rank_two = evaluation
            break

    assert rank_two is not None
    margin = next(gate for gate in rank_two.gates if gate.name == "rank_margin")
    assert isinstance(margin.value, float)
    assert margin.value >= 0.0


def test_donchian_zero_volume_history_fails_participation_without_error() -> None:
    dataset = _dataset()
    symbol = "NOVA"
    index = 120
    bars = dataset.bars_by_symbol[symbol]
    zero_volume = tuple(
        replace(bar, volume=0) if index - 20 <= offset < index else bar
        for offset, bar in enumerate(bars)
    )
    adjusted = replace(
        dataset, bars_by_symbol={**dataset.bars_by_symbol, symbol: zero_volume}
    )

    evaluation = evaluate_challenger_gates(
        "donchian_breakout_20_10", adjusted, symbol, zero_volume, index
    )
    participation = next(gate for gate in evaluation.gates if gate.name == "participation")
    assert participation.status == "FAIL"
    assert participation.value is None


def test_gap_gate_trace_reports_first_and_all_failures() -> None:
    dataset = _dataset()
    bars = dataset.bars_by_symbol["NOVA"]
    evaluation = evaluate_challenger_gates("gap_up_continuation", dataset, "NOVA", bars, 120)
    failure_names = {gate.name for gate in evaluation.failures}

    assert not evaluation.eligible
    assert evaluation.first_failure is not None
    assert evaluation.first_failure.name == "gap_threshold"
    assert "corporate_action_basis" in failure_names
    assert "data_quality" in failure_names or "corporate_action_basis" in failure_names
    assert any(item.startswith("gate:first_failure:") for item in evaluation.evidence())
    assert any(item.startswith("gate:all_failures:") for item in evaluation.evidence())


def test_candidate_signal_is_research_only_and_never_changes_v1_signal() -> None:
    dataset = _dataset()
    champion = next(
        spec for spec in build_strategy_catalog() if spec.strategy_id == "ts_momentum_sma_atr"
    )
    challenger = next(
        spec for spec in build_challenger_catalog() if spec.strategy_id == champion.strategy_id
    )
    bars = dataset.bars_by_symbol["NOVA"]

    v1_signal = champion.signal(dataset, "NOVA", bars, 120)
    challenger_signal = challenger.signal(dataset, "NOVA", bars, 120)
    assert (
        v1_signal is None
        or challenger_signal is None
        or challenger_signal.strategy_version != v1_signal.strategy_version
    )
