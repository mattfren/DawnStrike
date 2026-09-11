from unittest.mock import patch

from intraday_scanner.alpha.run_contracts import (
    build_alpha_run_contract,
    declared_core_coverage_truth,
)
from intraday_scanner.notifiers.telegram_formatter import format_alpha_no_trade


def _contract(core, *, declared=True, selected=False):
    row = {"ticker": "MOVER", "can_alert": False, "no_trade_reason": "watch"}
    return build_alpha_run_contract(
        scan_id="d160-test",
        generated_at="2026-09-11T13:00:00+00:00",
        ranked_count=1 if selected else 0,
        signals=[row] if selected else [],
        review={"decision": {"reason": "No clean edge today."}, "watchlist": []},
        source_summary={
            "status": "success",
            "core_universe_declared": declared,
            "core_universe": core,
        },
        enrichment_summary={
            "status": "complete",
            "selected_count": 1 if selected else 0,
            "selected_symbols": ["MOVER"] if selected else [],
            "verified_count": 1 if selected else 0,
        },
        notification_stats={},
    ).to_dict()


def test_required_missing_status_is_incomplete_and_snapshot_not_complete():
    core = {"contract_status": "", "status": "", "coverage_status": "", "requested_count": 10}
    truth = declared_core_coverage_truth({"core_universe_declared": True, "core_universe": core})
    result = _contract(core)
    assert truth["complete"] is False
    assert result["selection_outcome"] == "data_ineligible"
    assert result["core_snapshot_complete"] is False


def test_ready_labels_do_not_override_inconsistent_counts():
    core = {
        "contract_status": "READY",
        "status": "READY",
        "coverage_status": "COMPLETE",
        "contract_membership_count": 10,
        "requested_count": 10,
        "returned_count": 9,
        "eligible_count": 9,
        "fresh_count": 8,
        "fresh_verified_count": 8,
        "stale_count": 1,
        "rows": [{"ticker": f"S{i}"} for i in range(9)],
    }
    result = _contract(core)
    assert result["selection_outcome"] == "data_ineligible"
    assert result["core_snapshot_complete"] is False


def test_complete_current_core_preserves_valid_no_edge():
    core = {
        "contract_status": "READY",
        "status": "READY",
        "coverage_status": "COMPLETE",
        "contract_membership_count": 2,
        "requested_count": 2,
        "returned_count": 2,
        "eligible_count": 2,
        "fresh_count": 2,
        "fresh_verified_count": 2,
        "rows": [{"ticker": "SPY"}, {"ticker": "QQQ"}],
    }
    result = _contract(core)
    assert result["selection_outcome"] == "valid_no_edge"
    assert result["core_snapshot_complete"] is True


def test_independent_official_mover_remains_watchlist_ready_during_core_gap():
    sha = "a" * 40
    slate = {
        "schema_version": "dawnstrike.luna.frozen_slate.v2",
        "slate_id": "slate-d160",
        "content_hash_sha256": "a" * 64,
        "producer_code_sha": sha,
        "scan_id": "d160-test",
        "generated_at": "2026-09-11T13:00:00+00:00",
        "market_date": "2026-09-11",
        "research_only": True,
        "broker_execution": "disabled",
        "broker_execution_enabled": False,
        "missing_truth_is_zero": False,
        "require_safety": False,
        "enrichment_max_age_seconds": 1200,
        "coverage_status": "COMPLETE",
        "coverage_limitations": [],
        "published_count": 1,
        "ranked_research_count": 1,
        "target_count": 1,
        "selection_ids": ["selection-d160"],
        "symbols": ["MOVER"],
        "lane_statuses": {"mover": {"data_eligible": True}},
        "rows": [{"ticker": "MOVER", "research_selection_id": "selection-d160"}],
    }
    rows = [{"ticker": "MOVER", "research_selection_id": "selection-d160"}]
    source = {
        "status": "success",
        "require_watcher_proof": True,
        "code_sha": sha,
        "core_universe_declared": True,
        "core_universe": {
            "contract_status": "READY",
            "status": "DATA_UNAVAILABLE",
            "coverage_status": "DATA_UNAVAILABLE",
            "contract_membership_count": 2,
        },
        "ranked_research_slate": slate,
        "ranked_research_publication_rows": rows,
        "ranked_research_slate_lineage": {
            "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
            "slate_id": "slate-d160",
            "slate_content_hash_sha256": "a" * 64,
            "frozen_source_scan_id": "d160-test",
            "current_scan_id": "d160-test",
            "reuse_status": "CURRENT_SCAN",
            "frozen_source_code_sha": sha,
        },
    }
    with patch("intraday_scanner.alpha.run_contracts.validate_ranked_research_slate", return_value=slate), \
        patch("intraday_scanner.alpha.run_contracts.validate_frozen_publication_rows", return_value=None), \
        patch("intraday_scanner.alpha.run_contracts.apply_publication_semantics", return_value=rows), \
        patch("intraday_scanner.alpha.run_contracts.official_publication_rows", return_value=rows), \
        patch("intraday_scanner.alpha.run_contracts.publication_counts", return_value={"ranked_research": 1, "paper_plan_qualified": 0, "alertable_trade": 0, "official_selected": 1}), \
        patch("intraday_scanner.alpha.run_contracts._strategy_contribution_summary", return_value={}):
        result = build_alpha_run_contract(
            scan_id="d160-test",
            generated_at="2026-09-11T13:00:00+00:00",
            ranked_count=1,
            signals=[{"ticker": "MOVER", "can_alert": False, "no_trade_reason": "watch"}],
            review={"decision": {"reason": "No clean edge today."}, "watchlist": []},
            source_summary=source,
            enrichment_summary={"status": "complete", "selected_count": 1, "selected_symbols": ["MOVER"], "verified_count": 1},
            notification_stats={},
        ).to_dict()
    assert result["selection_outcome"] == "watchlist_ready"
    assert result["official_selected_count"] == 1


def test_warning_replaces_supplied_clean_edge_reason_everywhere():
    body = format_alpha_no_trade(
        reason="No clean edge today.",
        next_action="wait",
        core_coverage_warning="Declared core coverage is unavailable or incomplete: stale rows",
        max_chars=4096,
    )
    assert "No clean edge today." not in body
    assert "Core coverage incomplete; decision withheld." in body
    assert "No orders placed. Research only." in body
