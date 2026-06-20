from intraday_scanner.config import ScannerConfig
from intraday_scanner.expectancy import estimate_expectancy
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.scoring import score_universe


def test_expectancy_returns_conservative_estimates_from_paper_audit():
    candidates = [
        candidate.to_dict()
        for candidate in score_universe(
            read_snapshot_csv("sample_data/premarket_snapshot_sample.csv"), ScannerConfig()
        ).ranked_candidates
    ]
    audit_rows = [
        {"ticker": "NOVA", "lunch_return_pct": "11.96", "close_return_pct": "22.51"},
        {"ticker": "RIFT", "lunch_return_pct": "7.79", "close_return_pct": "0.60"},
        {"ticker": "MOON", "lunch_return_pct": "5.77", "close_return_pct": "2.70"},
    ]

    estimates = estimate_expectancy(candidates, audit_rows)

    assert estimates
    top = estimates[0]
    assert top.ticker in {"NOVA", "RIFT", "WIDE", "MOON"}
    assert top.expected_return_pct > -20
    assert 0 < top.confidence_pct <= 38
    assert top.lower_return_pct < top.upper_return_pct
    assert top.uncertainty_width_pct > 0
    assert top.risk_adjusted_return_pct <= top.expected_return_pct
    assert top.confidence_tier == "sparse but usable"
    assert "more audited setups" in top.next_confidence_step
    assert top.sample_size == 3
    assert "tier:" in top.explanation


def test_expectancy_falls_back_to_low_confidence_without_audits():
    candidates = [
        candidate.to_dict()
        for candidate in score_universe(
            read_snapshot_csv("sample_data/premarket_snapshot_sample.csv"), ScannerConfig()
        ).ranked_candidates
    ]

    estimates = estimate_expectancy(candidates, [])

    assert estimates
    assert estimates[0].confidence_pct <= 18
    assert estimates[0].model_basis == "score prior only"
    assert estimates[0].confidence_tier == "exploratory"
