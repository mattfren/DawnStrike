from __future__ import annotations

from intraday_scanner.alpha.v6.scoring import conservative_utility


def test_v6_scoring_never_overrides_safety_or_missing_evidence() -> None:
    vetoed = conservative_utility(
        activation_probability=0.9,
        conditional_net_excess_return_pct=2.0,
        tail_loss_pct=-0.5,
        uncertainty_pct=0.1,
        capacity_penalty_pct=0.1,
        safety_vetoes=["halt_status_unknown"],
    )
    missing = conservative_utility(
        activation_probability=None,
        conditional_net_excess_return_pct=2.0,
        tail_loss_pct=-0.5,
        uncertainty_pct=0.1,
        capacity_penalty_pct=0.1,
    )

    assert vetoed["status"] == "BLOCKED_SAFETY_VETO"
    assert vetoed["utility_lcb_pct"] is None
    assert missing["status"] == "UNCALIBRATED_INCOMPLETE_EVIDENCE"
