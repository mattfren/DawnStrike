from __future__ import annotations

from copy import deepcopy

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.alpha.v6.cycle2_features import (
    FEATURE_NAMES,
    UNKNOWN,
    build_cycle2_feature_vector,
    build_shadow_regime_interaction_receipt,
    cycle7_shadow_regime_policy,
)

CONFIG = {
    "universe": {
        "price_min_usd": 1.0,
        "price_max_usd": 500.0,
        "gap_min_pct": 1.0,
        "gap_max_pct": 50.0,
    }
}
WINDOW = {"start": "2026-08-01T00:00:00Z", "end": "2026-08-29T23:59:59Z"}


def _complete_inputs() -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, list[dict[str, object]]],
]:
    candidate = {
        "ticker": "TARGET",
        "premarket_price": 10.0,
        "gap_pct": 50.0,
        "momentum_pct": 6.0,
        "volume": 25000,
        "shares_outstanding": 100000,
        "spread_bps": 15.0,
        "source_hash_sha256": "f" * 64,
    }
    benchmark = [
        {
            "observed_at": f"2026-08-{28 - index // 12:02d}T{13 - index % 3:02d}:00:00Z",
            "close": 100 + index * (1 if index % 2 else -1),
            "source_hash_sha256": f"{index + 1:064x}",
        }
        for index in range(20)
    ]
    universe = [
        {
            "ticker": f"S{index:02d}",
            "observed_at": "2026-08-29T13:00:00Z",
            "return_pct": (-1 if index % 3 == 0 else 1) * (index + 1) / 10,
            "momentum_pct": (-1 if index % 3 == 0 else 1) * (index + 1) / 10,
            "gap_pct": 1 + index * 2.5,
            "price": 10,
            "volume": 25000 + index,
            "shares_outstanding": 100000,
            "spread_bps": 15,
            "source_hash_sha256": f"{100 + index:064x}",
        }
        for index in range(20)
    ]
    sector = {
        "technology": [
            {
                "observed_at": f"2026-08-{28 - index:02d}T13:00:00Z",
                "close": 100 + index,
                "source_hash_sha256": f"{200 + index:064x}",
            }
            for index in range(5)
        ]
    }
    return candidate, benchmark, universe, sector


def _vector(**overrides: object) -> dict[str, object]:
    candidate, benchmark, universe, sector = _complete_inputs()
    candidate.update(
        overrides.pop("candidate", {}) if isinstance(overrides.get("candidate"), dict) else {}
    )
    return build_cycle2_feature_vector(
        candidate,
        decision_id="cycle7-target",
        decision_at="2026-08-29T14:00:00Z",
        benchmark_bars=overrides.pop("benchmark_bars", benchmark),
        universe_rows=overrides.pop("universe_rows", universe),
        sector_bars=overrides.pop("sector_bars", sector),
        config=CONFIG,
        config_hash_sha256=canonical_hash(CONFIG),
        code_hash_sha256="a" * 40,
        model_hash_sha256="b" * 64,
        evaluation_window=WINDOW,
        window_hash_sha256=canonical_hash(WINDOW),
        **overrides,
    )


def test_cycle7_schema_has_all_additive_accuracy_blocks() -> None:
    vector = _vector()
    assert {
        "gap_volatility_normalized",
        "momentum_volatility_normalized",
        "gap_percentile",
        "momentum_percentile",
        "dollar_volume_proxy",
        "turnover_proxy",
        "spread_bps_proxy",
        "round_trip_cost_bps_proxy",
        "residual_vs_market",
        "residual_vs_sector",
    }.issubset(FEATURE_NAMES)
    assert vector["features"]["gap_percentile"]["status"] == "OBSERVED"
    assert vector["features"]["dollar_volume_proxy"]["value"] == 250000.0


def test_cycle7_permutation_stable_bytes_and_hashes() -> None:
    vector = _vector()
    _, benchmark, universe, sector = _complete_inputs()
    reverse = _vector(
        benchmark_bars=list(reversed(benchmark)),
        universe_rows=list(reversed(universe)),
        sector_bars={"technology": list(reversed(sector["technology"]))},
    )
    assert vector["feature_hash_sha256"] == reverse["feature_hash_sha256"]
    assert vector["features"] == reverse["features"]


def test_cycle7_future_or_naive_market_observation_is_blocked_not_dropped() -> None:
    _, _, universe, _ = _complete_inputs()
    future = deepcopy(universe)
    future[0]["observed_at"] = "2026-08-29T15:00:00Z"
    blocked = _vector(universe_rows=future)
    assert blocked["status"] == "BLOCKED_INPUT_INTEGRITY"
    assert all(item["status"] == UNKNOWN for item in blocked["features"].values())
    naive = deepcopy(universe)
    naive[0]["observed_at"] = "2026-08-29T13:00:00"
    assert _vector(universe_rows=naive)["status"] == "BLOCKED_INPUT_INTEGRITY"


def test_cycle7_missing_liquidity_never_becomes_zero() -> None:
    _, _, universe, _ = _complete_inputs()
    missing = _vector(candidate={"volume": None, "shares_outstanding": None, "spread_bps": None})
    for name in (
        "dollar_volume_proxy",
        "turnover_proxy",
        "spread_bps_proxy",
        "round_trip_cost_bps_proxy",
    ):
        assert missing["features"][name]["value"] is None


def test_cycle7_shadow_receipt_is_not_evaluable_without_full_lineage() -> None:
    vector = _vector()
    incomplete = dict(vector)
    incomplete["model_hash_sha256"] = None
    receipt = build_shadow_regime_interaction_receipt(incomplete)
    assert receipt["status"] == "NOT_EVALUABLE"
    assert receipt["broker_execution_enabled"] is False
    assert receipt["ranking_mutated"] is False
    assert receipt["promotion_mutated"] is False


def test_cycle7_policy_has_no_legacy_gap_threshold() -> None:
    policy = cycle7_shadow_regime_policy()
    assert policy["legacy_gap_threshold_used"] is False
    assert policy["max_gap_pct"] == 50.0
    assert "120" not in str(policy)
