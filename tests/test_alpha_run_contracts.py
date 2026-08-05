import pytest

from intraday_scanner.alpha.run_contracts import build_alpha_run_contract


def _contract(*, enrichment, watchlist=None):
    signal = {
        "ticker": "NOVA",
        "can_alert": True,
        "no_trade_reason": "",
    }
    selected = list(watchlist or [])
    return build_alpha_run_contract(
        scan_id="scan-1",
        generated_at="2026-08-05T12:23:00Z",
        ranked_count=len(selected),
        signals=[signal] if selected else [],
        review={
            "decision": {"reason": "No clean edge."},
            "selection_diagnostics": {},
            "watchlist": selected,
        },
        source_summary={"status": "success"},
        enrichment_summary=enrichment,
        notification_stats={},
    )


def test_sparse_coverage_is_data_ineligible_even_when_some_rows_verified():
    contract = _contract(
        enrichment={
            "status": "partial",
            "selected_count": 9,
            "selected_symbols": [
                "HYFM",
                "BJDX",
                "ALFA",
                "BETA",
                "GAMM",
                "NOVA",
                "OMEG",
                "RHO",
                "SIGM",
            ],
            "verified_count": 2,
            "secondary_fallback_status": "ceiling_exceeded_not_applied",
        }
    )

    assert contract.selection_outcome == "data_ineligible"
    assert contract.coverage_status == "insufficient"
    assert contract.premarket_verified_ratio == 0.2222
    assert contract.primary_veto == "premarket_coverage_insufficient"
    assert contract.research_candidate_count == 9
    assert contract.research_symbols == (
        "ALFA",
        "BETA",
        "BJDX",
        "GAMM",
        "HYFM",
        "NOVA",
        "OMEG",
        "RHO",
        "SIGM",
    )


def test_sparse_coverage_vetoes_a_watchlist_instead_of_overstating_selection_truth():
    signal = {"ticker": "NOVA", "can_alert": True, "no_trade_reason": ""}
    contract = _contract(
        enrichment={
            "status": "partial",
            "selected_count": 4,
            "selected_symbols": ["NOVA", "ALFA", "BETA", "GAMM"],
            "verified_count": 1,
            "secondary_fallback_status": "ceiling_exceeded_not_applied",
        },
        watchlist=[signal],
    )

    assert contract.selection_outcome == "data_ineligible"


def test_complete_coverage_preserves_valid_no_edge_semantics():
    contract = _contract(
        enrichment={
            "status": "complete",
            "selected_count": 2,
            "selected_symbols": ["NOVA", "ALFA"],
            "verified_count": 2,
            "secondary_fallback_status": "not_needed",
        }
    )

    assert contract.selection_outcome == "valid_no_edge"
    assert contract.coverage_status == "complete"


def test_contract_rejects_a_count_without_the_explicit_research_symbols():
    with pytest.raises(ValueError, match="selected_count"):
        _contract(
            enrichment={
                "status": "partial",
                "selected_count": 2,
                "selected_symbols": ["NOVA"],
                "verified_count": 1,
                "secondary_fallback_status": "not_needed",
            }
        )
