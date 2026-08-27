import pytest

from intraday_scanner.alpha.run_contracts import build_alpha_run_contract
from intraday_scanner.services import luna_research_slate_service as slate_service
from intraday_scanner.services.luna_research_slate_service import (
    apply_publication_semantics,
    build_ranked_research_slate,
)


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


def _frozen_contract_inputs():
    signal = {
        "ticker": "FROZEN",
        "signal_id": "signal-frozen",
        "alpha_score": 10,
        "universe_lane": "mover",
    }
    slate = build_ranked_research_slate(
        [signal],
        generated_at="2026-08-27T12:00:00+00:00",
        market_date="2026-08-27",
        scan_id="scan-frozen",
        lane_statuses={"mover": {"data_eligible": True, "promotion_limited": False}},
    )
    publication_rows = apply_publication_semantics(
        list(slate["rows"]),
        slate=slate,
        coverage={"lanes": slate["lane_statuses"]},
    )
    lineage = {
        "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
        "slate_id": slate["slate_id"],
        "slate_content_hash_sha256": slate["content_hash_sha256"],
        "frozen_source_scan_id": "scan-frozen",
        "current_scan_id": "scan-frozen",
        "reuse_status": "CURRENT_SCAN",
    }
    return signal, slate, publication_rows, lineage


def _build_frozen_contract(*, publication_rows, receipt_verifier=None, frozen_inputs=None):
    signal, slate, _, lineage = frozen_inputs or _frozen_contract_inputs()
    return build_alpha_run_contract(
        scan_id="scan-frozen",
        generated_at="2026-08-27T12:01:00+00:00",
        ranked_count=1,
        signals=[signal],
        review={"decision": {"reason": "No clean edge."}, "watchlist": []},
        source_summary={
            "status": "success",
            "ranked_research_slate": slate,
            "ranked_research_publication_rows": publication_rows,
            "ranked_research_slate_lineage": lineage,
        },
        enrichment_summary={
            "status": "complete",
            "selected_count": 1,
            "selected_symbols": ["FROZEN"],
            "verified_count": 1,
        },
        notification_stats={},
        receipt_verifier=receipt_verifier,
    )


def test_contract_accepts_exact_rederived_frozen_publication_rows():
    _, _, publication_rows, _ = _frozen_contract_inputs()
    contract = _build_frozen_contract(publication_rows=publication_rows)
    assert contract.ranked_research_count == 1


def test_contract_preserves_tier_two_only_with_authenticated_receipt_resolver(
    monkeypatch,
):
    frozen_inputs = _frozen_contract_inputs()
    _, slate, _, _ = frozen_inputs

    def authenticated_receipt_resolver(row):
        return row.get("signal_id") == "signal-frozen"

    monkeypatch.setattr(
        slate_service,
        "_plan_qualified",
        lambda row, *, receipt_verifier: (
            receipt_verifier is authenticated_receipt_resolver
            and receipt_verifier(row)
        ),
    )
    monkeypatch.setattr(
        slate_service,
        "_alertable",
        lambda row, *, require_watcher_proof: False,
    )
    tier_two_rows = apply_publication_semantics(
        list(slate["rows"]),
        slate=slate,
        coverage={"lanes": slate["lane_statuses"]},
        receipt_verifier=authenticated_receipt_resolver,
    )
    assert tier_two_rows[0]["publication_tier"] == "PAPER_PLAN_QUALIFIED"

    contract = _build_frozen_contract(
        publication_rows=tier_two_rows,
        receipt_verifier=authenticated_receipt_resolver,
        frozen_inputs=frozen_inputs,
    )
    assert contract.paper_plan_qualified_count == 1
    with pytest.raises(ValueError, match="exact authenticated frozen-slate"):
        _build_frozen_contract(
            publication_rows=tier_two_rows,
            frozen_inputs=frozen_inputs,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ticker", "ATTACKER"),
        ("signal_id", "signal-replacement"),
        ("broker_execution", "live"),
        ("broker_execution_enabled", True),
        ("publication_tier", "ALERTABLE_PAPER_ENTRY"),
        ("alert_gate_status", "PASS"),
    ],
)
def test_contract_rejects_frozen_publication_row_metadata_smuggling(field, value):
    _, _, publication_rows, _ = _frozen_contract_inputs()
    hostile = [dict(publication_rows[0])]
    hostile[0][field] = value
    with pytest.raises(ValueError, match="exact authenticated frozen-slate"):
        _build_frozen_contract(publication_rows=hostile)


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


def test_contract_reports_governed_cross_scan_frozen_slate_without_current_replacement():
    slate = build_ranked_research_slate(
        [{"ticker": "FROZEN", "signal_id": "signal-frozen"}],
        generated_at="2026-08-05T12:00:00+00:00",
        market_date="2026-08-05",
        scan_id="scan-original",
    )
    contract = build_alpha_run_contract(
        scan_id="scan-retry",
        generated_at="2026-08-05T12:23:00+00:00",
        ranked_count=1,
        signals=[{"ticker": "CURRENT", "signal_id": "signal-current"}],
        review={
            "decision": {"reason": "No clean edge."},
            "selection_diagnostics": {},
            "watchlist": [],
        },
        source_summary={
            "status": "success",
            "ranked_research_slate": slate,
            "ranked_research_slate_lineage": {
                "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
                "slate_id": slate["slate_id"],
                "slate_content_hash_sha256": slate["content_hash_sha256"],
                "frozen_source_scan_id": "scan-original",
                "current_scan_id": "scan-retry",
                "reuse_status": "GOVERNED_DAILY_FREEZE_REUSE",
            },
        },
        enrichment_summary={
            "status": "complete",
            "selected_count": 1,
            "selected_symbols": ["CURRENT"],
            "verified_count": 1,
            "secondary_fallback_status": "not_needed",
        },
        notification_stats={},
    )

    assert contract.ranked_research_count == 1
    assert contract.research_candidate_count == 1
    assert contract.research_symbols == ("FROZEN",)
    assert contract.slate_selection_ids == tuple(slate["selection_ids"])
    assert contract.slate_source_scan_id == "scan-original"
    assert contract.slate_reuse_status == "GOVERNED_DAILY_FREEZE_REUSE"


def test_core_only_frozen_slate_owns_research_identity_when_mover_data_is_unavailable():
    lane_statuses = {
        "mover": {"data_eligible": False, "source_status": "SOURCE_FAILED"},
        "core": {
            "data_eligible": True,
            "snapshot_status": "PARTIAL",
            "enrichment_status": "complete",
        },
    }
    slate = build_ranked_research_slate(
        [
            {
                "ticker": "CORE",
                "signal_id": "signal-core",
                "universe_lane": "core",
                "evidence_lane": "core",
            }
        ],
        generated_at="2026-08-05T12:00:00+00:00",
        market_date="2026-08-05",
        scan_id="scan-1",
        coverage_status="LIMITED",
        lane_statuses=lane_statuses,
    )
    contract = build_alpha_run_contract(
        scan_id="scan-1",
        generated_at="2026-08-05T12:23:00+00:00",
        ranked_count=1,
        signals=[{"ticker": "CORE", "signal_id": "signal-core"}],
        review={
            "decision": {"reason": "No plan passed current entry gates."},
            "selection_diagnostics": {},
            "watchlist": [],
        },
        source_summary={
            "status": "success",
            "ranked_research_slate": slate,
            "ranked_research_slate_lineage": {
                "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
                "slate_id": slate["slate_id"],
                "slate_content_hash_sha256": slate["content_hash_sha256"],
                "frozen_source_scan_id": "scan-1",
                "current_scan_id": "scan-1",
                "reuse_status": "CURRENT_SCAN",
            },
        },
        enrichment_summary={
            "status": "partial",
            "selected_count": 4,
            "selected_symbols": ["MOVE1", "MOVE2", "MOVE3", "MOVE4"],
            "verified_count": 0,
            "secondary_fallback_status": "ceiling_exceeded_not_applied",
        },
        notification_stats={},
    )

    assert contract.research_candidate_count == 1
    assert contract.research_symbols == ("CORE",)
    assert contract.selection_outcome == "valid_no_edge"
    assert contract.coverage_status == "limited"
    assert contract.premarket_selected_count == 4
    assert contract.premarket_verified_count == 0
    assert contract.lane_statuses == lane_statuses


def test_contract_rejects_cross_scan_slate_without_explicit_reuse_lineage():
    slate = build_ranked_research_slate(
        [{"ticker": "FROZEN", "signal_id": "signal-frozen"}],
        generated_at="2026-08-05T12:00:00+00:00",
        market_date="2026-08-05",
        scan_id="scan-original",
    )
    with pytest.raises(ValueError, match="FROZEN_SLATE_SCAN_MISMATCH"):
        build_alpha_run_contract(
            scan_id="scan-retry",
            generated_at="2026-08-05T12:23:00+00:00",
            ranked_count=0,
            signals=[],
            review={"decision": {}, "selection_diagnostics": {}, "watchlist": []},
            source_summary={"status": "success", "ranked_research_slate": slate},
            enrichment_summary={
                "status": "complete",
                "selected_count": 0,
                "selected_symbols": [],
                "verified_count": 0,
            },
            notification_stats={},
        )


def test_empty_frozen_slate_cannot_publish_retry_watchlist_or_watchlist_ready():
    slate = build_ranked_research_slate(
        [],
        target=5,
        data_eligible=False,
        generated_at="2026-08-05T12:00:00+00:00",
        market_date="2026-08-05",
        scan_id="scan-original",
    )
    contract = build_alpha_run_contract(
        scan_id="scan-retry",
        generated_at="2026-08-05T12:23:00+00:00",
        ranked_count=1,
        signals=[{"ticker": "CURRENT", "signal_id": "signal-current"}],
        review={
            "decision": {
                "no_trade": False,
                "decision_tier": "clean_edge",
                "reason": "current retry found an unrelated row",
            },
            "selection_diagnostics": {},
            "watchlist": [{"ticker": "CURRENT", "signal_id": "signal-current"}],
        },
        source_summary={
            "status": "success",
            "ranked_research_slate": slate,
            "ranked_research_publication_rows": [],
            "ranked_research_slate_lineage": {
                "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
                "slate_id": slate["slate_id"],
                "slate_content_hash_sha256": slate["content_hash_sha256"],
                "frozen_source_scan_id": "scan-original",
                "current_scan_id": "scan-retry",
                "reuse_status": "GOVERNED_DAILY_FREEZE_REUSE",
            },
        },
        enrichment_summary={
            "status": "complete",
            "selected_count": 1,
            "selected_symbols": ["CURRENT"],
            "verified_count": 1,
            "secondary_fallback_status": "not_needed",
        },
        notification_stats={},
    )

    assert contract.slate_published_count == 0
    assert contract.official_selected_count == 0
    assert contract.selection_outcome == "data_ineligible"


def test_contract_rejects_caller_claimed_slate_lineage_that_disagrees_with_artifact():
    slate = build_ranked_research_slate(
        [{"ticker": "FROZEN", "signal_id": "signal-frozen"}],
        generated_at="2026-08-05T12:00:00+00:00",
        market_date="2026-08-05",
        scan_id="scan-original",
    )
    with pytest.raises(ValueError, match="FROZEN_SLATE_SCAN_MISMATCH"):
        build_alpha_run_contract(
            scan_id="scan-retry",
            generated_at="2026-08-05T12:23:00+00:00",
            ranked_count=1,
            signals=[],
            review={
                "decision": {"reason": "No clean edge."},
                "selection_diagnostics": {},
                "watchlist": [],
            },
            source_summary={
                "status": "success",
                "ranked_research_slate": slate,
                "ranked_research_slate_lineage": {
                    "frozen_source_scan_id": "scan-retry",
                    "current_scan_id": "scan-retry",
                    "reuse_status": "CURRENT_SCAN",
                },
            },
            enrichment_summary={
                "status": "complete",
                "selected_count": 0,
                "selected_symbols": [],
                "verified_count": 0,
                "secondary_fallback_status": "not_needed",
            },
            notification_stats={},
        )
