from __future__ import annotations

from intraday_scanner.alpha.risk_geometry import (
    RiskGeometryStatus,
    build_risk_challenger_geometries,
)
from intraday_scanner.alpha.v5_policy import build_v5_challenger_registry


def test_challengers_are_registered_as_research_only() -> None:
    challengers = build_v5_challenger_registry()

    assert {item["challenger_id"] for item in challengers} >= {
        "v5_baseline",
        "v5_atr_stop_target",
        "v5_liquidity_aware_risk",
        "v5_session_close_exit",
        "v5_cost_stress_1_5x",
    }
    assert all(item["promotion_eligible"] is False for item in challengers)
    assert all(item["cost_model_status"] == "COST_MODEL_PROVISIONAL" for item in challengers)


def test_risk_challengers_keep_liquidity_and_geometry_statuses_explicit() -> None:
    geometries = build_risk_challenger_geometries(
        entry_price=10.0,
        stop_price=9.0,
        target_price=12.0,
        atr=0.5,
        equity=100_000.0,
        liquidity_status="watch_only",
    )

    assert len(geometries) == 3
    assert all(item.status is RiskGeometryStatus.LIQUIDITY_INELIGIBLE for item in geometries)
