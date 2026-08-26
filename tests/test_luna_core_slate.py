from intraday_scanner.notifiers.telegram_formatter import format_alpha_watch
from intraday_scanner.services.luna_core_universe_service import build_core_universe_contract
from intraday_scanner.services.luna_research_slate_service import (
    TIER1,
    TIER2,
    TIER3,
    apply_publication_semantics,
    build_ranked_research_slate,
    publication_counts,
)
from intraday_scanner.services.signal_review_service import monitor_alpha_signals


def _manifest(observed="2026-08-26T12:00:00Z", members=None, source_id="source"):
    return {
        "source_id": source_id,
        "source_uri": f"https://example.test/{source_id}",
        "observed_at": observed,
        "effective_date": "2026-08-26",
        "members": members or [],
    }


def test_core_union_dedupes_symbols_and_keeps_index_memberships():
    contract = build_core_universe_contract(
        [
            _manifest(members=[{"ticker": "aapl", "index_memberships": ["S&P 500"]}]),
            _manifest(
                source_id="ndx",
                members=[
                    {"ticker": "AAPL", "index_memberships": ["Nasdaq-100"]},
                    {"ticker": "NVDA", "index_memberships": ["Nasdaq 100"]},
                ],
            ),
        ],
        observed_at="2026-08-26T13:00:00Z",
    )
    assert contract["status"] == "READY"
    assert contract["membership_count"] == 2
    aapl = next(row for row in contract["members"] if row["symbol"] == "AAPL")
    assert aapl["index_memberships"] == ["Nasdaq-100", "S&P 500"]
    assert contract["content_hash_sha256"]


def test_core_union_absent_or_stale_is_data_unavailable():
    absent = build_core_universe_contract(None, observed_at="2026-08-26T13:00:00Z")
    stale = build_core_universe_contract(
        _manifest(observed="2026-01-01T12:00:00Z", members=[{"ticker": "AAPL", "index": "S&P 500"}]),
        observed_at="2026-08-26T13:00:00Z",
    )
    assert absent["status"] == "DATA_UNAVAILABLE"
    assert stale["status"] == "DATA_UNAVAILABLE"
    assert stale["freshness_verdict"] == "STALE"


def test_slate_publishes_five_distinct_safe_episodes_and_shortfall():
    rows = [{"ticker": f"S{i}", "alpha_score": 100 - i} for i in range(7)]
    slate = build_ranked_research_slate(rows)
    assert slate["published_count"] == 5
    assert len(set(slate["symbols"])) == 5
    shortfall = build_ranked_research_slate(rows[:2])
    assert shortfall["published_count"] == 2
    assert shortfall["slate_shortfall_reason"]


def test_slate_excludes_hard_veto_stale_and_fabricated_rows():
    rows = [
        {"ticker": "VETO", "alpha_score": 100, "hard_avoid_reasons": ["halt"]},
        {"ticker": "STALE", "alpha_score": 99, "stale_data_flag": True},
        {"ticker": "FAKE", "alpha_score": 98, "is_fabricated": True},
        {"ticker": "SAFE", "alpha_score": 1},
    ]
    slate = build_ranked_research_slate(rows)
    assert slate["symbols"] == ["SAFE"]


def test_tier_one_coexists_with_one_official_and_tier_two_three_remain_zero():
    rows = [
        {"ticker": "OFFICIAL", "alpha_score": 100, "plan_qualified": True, "can_alert": True, "alert_gate_status": "PASS"},
        {"ticker": "RESEARCH", "alpha_score": 99},
    ]
    slate = build_ranked_research_slate(rows)
    annotated = apply_publication_semantics(rows, slate=slate, coverage={"secondary_fallback_status": "research_only_applied_above_ceiling"})
    assert annotated[0]["publication_tier"] == TIER1
    assert annotated[0]["plan_qualification_status"] == "WAITING_CURRENT_CHECKS"
    counts = publication_counts(annotated, official_selected=1)
    assert counts == {"ranked_research": 2, "paper_plan_qualified": 0, "alertable_trade": 0, "official_selected": 1}
    text = format_alpha_watch(signals=annotated, edge_label="research")
    assert "OFFICIAL PAPER CANDIDATES" not in text


def test_tier_semantics_and_research_monitor_label():
    row = {"ticker": "SAFE", "alpha_score": 1, "entry_trigger": 10, "invalidation": 9, "target_1": 12, "can_alert": True, "alert_gate_status": "PASS", "plan_qualified": True, "publication_tier": TIER1}
    annotated = apply_publication_semantics([row], slate={"rows": [row]}, coverage={})
    assert annotated[0]["publication_tier"] == TIER3
    assert monitor_alpha_signals(annotated, current_prices={"SAFE": 10.1})["events"][0]["label"] == "ENTRY TRIGGERED"
