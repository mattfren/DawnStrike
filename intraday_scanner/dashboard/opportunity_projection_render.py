"""Safe Streamlit rendering for the canonical opportunity projection."""

from __future__ import annotations

from typing import Any

from .opportunity_projection import (
    NOT_AVAILABLE,
    OpportunityProjection,
    OpportunityProjectionState,
)


def render_streamlit_opportunity_projection(
    streamlit: Any,
    projection: OpportunityProjection,
) -> None:
    """Render persisted public text without unsafe HTML or action controls."""

    if projection.state is OpportunityProjectionState.DISABLED:
        return
    streamlit.markdown("### Today's Best Opportunities")
    streamlit.caption(
        "Research-only persisted evidence. Displayed decisions never authorize orders."
    )
    if projection.state is OpportunityProjectionState.DATA_UNAVAILABLE:
        streamlit.warning(projection.message)
        return
    if projection.state is OpportunityProjectionState.NO_QUALIFYING:
        streamlit.info(projection.message)
        return

    if projection.as_of is not None:
        streamlit.caption(f"Verified persisted run as of {projection.as_of.isoformat()}")
    for row in projection.rows:
        streamlit.markdown(f"#### Opportunity {row.rank}")
        streamlit.text(
            f"{row.symbol} | {row.strategy_id} {row.strategy_version} | "
            f"{row.direction.upper()} | {row.decision.upper()}"
        )
        streamlit.write(
            {
                "Lifecycle": row.lifecycle,
                "Evidence kind": row.evidence_kind,
                "Validation wording": row.validation_wording,
                "Market regime": row.market_regime,
                "Market regime evidence": row.market_regime_evidence_kind,
                "Security regime": row.security_regime,
                "Security regime evidence": row.security_regime_evidence_kind,
                "Liquidity": _display_value(row.liquidity_score),
                "Liquidity evidence": row.liquidity_evidence_kind or NOT_AVAILABLE,
            }
        )
        streamlit.write(
            "Triggered anomalies",
            [
                {
                    "name": item.name,
                    "strength": _display_value(item.strength),
                    "evidence_kind": item.evidence_kind,
                }
                for item in row.triggered_anomalies
            ]
            or [
                {
                    "name": NOT_AVAILABLE,
                    "strength": NOT_AVAILABLE,
                    "evidence_kind": NOT_AVAILABLE,
                }
            ],
        )
        streamlit.write("Why", list(row.why) or [NOT_AVAILABLE])
        streamlit.write("Risks", list(row.risks) or [NOT_AVAILABLE])
        streamlit.write("Vetoes", list(row.vetoes) or [NOT_AVAILABLE])
        streamlit.write(
            {
                "Entry": _display_value(row.entry_price),
                "Invalidation": _display_value(row.invalidation_price),
                "Target": _display_value(row.target_price),
            }
        )
        streamlit.write("Limitations", list(row.limitations) or [NOT_AVAILABLE])
        streamlit.caption(
            "Persisted decision only; no TAKE authorization, broker route, or lifecycle control."
        )


def _display_value(value: object | None) -> str:
    return NOT_AVAILABLE if value is None else str(value)


__all__ = ["render_streamlit_opportunity_projection"]
