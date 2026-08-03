"""Conservative utility scoring for a shadow-only V6 challenger."""

from __future__ import annotations

import math
from typing import Any


def conservative_utility(
    *,
    activation_probability: float | None,
    conditional_net_excess_return_pct: float | None,
    tail_loss_pct: float | None,
    uncertainty_pct: float | None,
    capacity_penalty_pct: float | None,
    safety_vetoes: list[str] | None = None,
) -> dict[str, Any]:
    """Return a lower-bound utility or an explicit no-score reason.

    The calculation is intentionally not a trade instruction.  It is a
    research metric that refuses to score safety-vetoed or incomplete rows.
    """

    if safety_vetoes:
        return {"status": "BLOCKED_SAFETY_VETO", "utility_lcb_pct": None}
    values = (
        activation_probability,
        conditional_net_excess_return_pct,
        tail_loss_pct,
        uncertainty_pct,
        capacity_penalty_pct,
    )
    if any(value is None or not math.isfinite(float(value)) for value in values):
        return {"status": "UNCALIBRATED_INCOMPLETE_EVIDENCE", "utility_lcb_pct": None}
    assert activation_probability is not None
    assert conditional_net_excess_return_pct is not None
    assert tail_loss_pct is not None
    assert uncertainty_pct is not None
    assert capacity_penalty_pct is not None
    activation = min(1.0, max(0.0, float(activation_probability)))
    expected = activation * float(conditional_net_excess_return_pct)
    tail_penalty = max(0.0, -float(tail_loss_pct))
    utility = expected - tail_penalty - abs(float(uncertainty_pct)) - max(
        0.0, float(capacity_penalty_pct)
    )
    return {
        "status": "SHADOW_SCORED",
        "expected_net_excess_return_pct": round(expected, 6),
        "tail_penalty_pct": round(tail_penalty, 6),
        "uncertainty_penalty_pct": round(abs(float(uncertainty_pct)), 6),
        "capacity_penalty_pct": round(max(0.0, float(capacity_penalty_pct)), 6),
        "utility_lcb_pct": round(utility, 6),
        "research_only": True,
        "broker_execution_enabled": False,
    }


__all__ = ["conservative_utility"]
