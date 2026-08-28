from datetime import datetime, timezone

from intraday_scanner.services import premarket_enrichment_service as premarket
from intraday_scanner.services.alpha_cycle_service import _alpha_scoring_cohort
from intraday_scanner.services.luna_research_slate_service import build_ranked_research_slate


def test_full_enriched_cohort_refills_five_after_top_twenty_safety_failures():
    cycle_at = datetime(2026, 8, 26, 13, 30, tzinfo=timezone.utc)

    def candidate(ticker: str, alpha_score: int) -> dict[str, object]:
        observation = premarket.observation_from_alpaca_bars(
            ticker,
            [
                {
                    "ticker": ticker,
                    "timestamp": "2026-08-26T13:28:00+00:00",
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.0,
                    "volume": 1_000,
                }
            ],
            previous_close=9.5,
            requested_at=cycle_at,
            max_age_seconds=600,
            feed="iex",
        )
        observation_hash, observation_payload = premarket._canonical_observation_payload(
            observation
        )
        return {
            "ticker": ticker,
            "alpha_score": alpha_score,
            "market_date": cycle_at.date().isoformat(),
            "source_count": 1,
            "source_quality_status": "VERIFIED",
            "freshness_status": "FRESH",
            "halt_status": "CLEAR",
            "sec_risk_status": "CLEAR",
            "corporate_action_status": "CLEAR",
            "input_status": "VERIFIED",
            "evidence_status": "VERIFIED",
            "enrichment_observation_sha256": observation_hash,
            "enrichment_observation_payload_json": observation_payload,
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

    slate = build_ranked_research_slate(
        cohort,
        target=5,
        require_safety=True,
        generated_at=cycle_at.isoformat(),
        market_date=cycle_at.date().isoformat(),
    )

    assert slate["published_count"] == 5
    assert slate["symbols"] == [f"RESERVE{index}" for index in range(5)]
    assert "FORMULA_AVOID" not in slate["symbols"]
