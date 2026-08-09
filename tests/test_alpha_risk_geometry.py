from __future__ import annotations

from intraday_scanner.alpha.risk_geometry import (
    RiskGeometryStatus,
    evaluate_risk_geometry,
)


def test_valid_long_geometry_is_deterministic() -> None:
    geometry = evaluate_risk_geometry(
        entry_price=10.0,
        stop_price=9.0,
        target_price=12.0,
        equity=100_000.0,
    )

    assert geometry.status is RiskGeometryStatus.ELIGIBLE
    assert geometry.stop_distance_pct == 10.0
    assert geometry.gross_reward_risk == 2.0
    assert geometry.to_dict()["challenger_id"] == "baseline"


def test_risk_geometry_fails_closed_for_manufactured_targets_and_bad_data() -> None:
    manufactured = evaluate_risk_geometry(
        entry_price=10.0,
        stop_price=9.0,
        target_price=12.0,
        equity=100_000.0,
        target_independent=False,
    )
    missing = evaluate_risk_geometry(
        entry_price=None,
        stop_price=9.0,
        target_price=12.0,
        equity=100_000.0,
    )

    assert manufactured.status is RiskGeometryStatus.TARGET_NOT_INDEPENDENT
    assert "target_not_independent" in manufactured.reasons
    assert missing.status is RiskGeometryStatus.DATA_INELIGIBLE
