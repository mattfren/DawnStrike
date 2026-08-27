from intraday_scanner.services.alpha_cycle_service import _alpha_scoring_cohort
from intraday_scanner.services.luna_research_slate_service import build_ranked_research_slate


def test_full_enriched_cohort_refills_five_after_top_twenty_safety_failures():
    def candidate(ticker: str, alpha_score: int) -> dict[str, object]:
        return {
            "ticker": ticker,
            "alpha_score": alpha_score,
            "source_count": 1,
            "source_quality_status": "VERIFIED",
            "freshness_status": "FRESH",
            "halt_status": "CLEAR",
            "sec_risk_status": "CLEAR",
            "corporate_action_status": "CLEAR",
        }

    ranked_presentation = [
        candidate(f"TOP{index:02d}", 100 - index)
        for index in range(20)
    ]
    already_enriched = [
        *ranked_presentation,
        *[
            candidate(f"RESERVE{index}", 70 - index)
            for index in range(5)
        ],
        candidate("FORMULA_AVOID", 99),
    ]
    already_enriched[-1]["avoid_reasons"] = "low_dollar_volume"

    cohort = _alpha_scoring_cohort(already_enriched, ranked_presentation)
    # Simulate later authoritative safety checks: no row is admitted merely
    # to reach target, and the reserve rows are selected only because they
    # were part of the already-enriched input cohort.
    for row in cohort[:20]:
        row["hard_avoid_reasons"] = ["current_sec_safety_failed"]

    slate = build_ranked_research_slate(cohort, target=5, require_safety=True)

    assert slate["published_count"] == 5
    assert slate["symbols"] == [f"RESERVE{index}" for index in range(5)]
    assert "FORMULA_AVOID" not in slate["symbols"]
