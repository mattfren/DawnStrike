import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from intraday_scanner.notifiers.telegram_formatter import (
    format_alpha_no_trade,
    format_alpha_watch,
)
from intraday_scanner.services.luna_core_universe_service import (
    build_core_universe_contract,
    discover_core_universe_rows,
    merge_core_universe_rows,
    rank_core_universe_rows,
)
from intraday_scanner.services.luna_research_slate_service import (
    TIER1,
    apply_publication_semantics,
    build_ranked_research_slate,
    official_publication_rows,
    publication_counts,
)
from intraday_scanner.services.signal_review_service import monitor_alpha_signals


def _manifest(
    observed="2026-08-26T12:00:00Z",
    members=None,
    source_id="source",
    index_name=None,
    expected_count=None,
):
    return {
        "source_id": source_id,
        "source_uri": f"https://example.test/{source_id}",
        "observed_at": observed,
        "effective_date": "2026-08-26",
        "members": members or [],
        **({"index_name": index_name} if index_name else {}),
        **({"expected_count": expected_count} if expected_count is not None else {}),
    }


def test_core_union_dedupes_symbols_and_keeps_index_memberships():
    contract = build_core_universe_contract(
        [
            _manifest(
                index_name="S&P 500",
                expected_count=1,
                members=[{"ticker": "aapl", "index_memberships": ["S&P 500"]}],
            ),
            _manifest(
                source_id="ndx",
                index_name="Nasdaq-100",
                expected_count=2,
                members=[
                    {"ticker": "AAPL", "index_memberships": ["Nasdaq-100"]},
                    {"ticker": "NVDA", "index_memberships": ["Nasdaq 100"]},
                ],
            ),
        ],
        observed_at="2026-08-26T13:00:00Z",
        allow_test_override=True,
    )
    assert contract["status"] == "READY"
    assert contract["membership_count"] == 2
    aapl = next(row for row in contract["members"] if row["symbol"] == "AAPL")
    assert aapl["index_memberships"] == ["Nasdaq-100", "S&P 500"]
    assert contract["content_hash_sha256"]


def test_core_union_requires_both_complete_indexes_and_stale_is_unavailable():
    absent = build_core_universe_contract(None, observed_at="2026-08-26T13:00:00Z")
    stale = build_core_universe_contract(
        _manifest(
            observed="2026-01-01T12:00:00Z",
            index_name="S&P 500",
            expected_count=1,
            members=[{"ticker": "AAPL", "index": "S&P 500"}],
        ),
        observed_at="2026-08-26T13:00:00Z",
    )
    missing_index = build_core_universe_contract(
        _manifest(
            index_name="S&P 500", expected_count=1, members=[{"ticker": "AAPL", "index": "S&P 500"}]
        ),
        observed_at="2026-08-26T13:00:00Z",
    )
    assert absent["status"] == "DATA_UNAVAILABLE"
    assert stale["status"] == "DATA_UNAVAILABLE"
    assert stale["freshness_verdict"] == "STALE"
    assert missing_index["status"] == "DATA_UNAVAILABLE"
    assert missing_index["index_verdicts"]["Nasdaq-100"]["status"] == "DATA_UNAVAILABLE"


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


def test_soft_no_trade_reason_is_disclosed_on_tier_one_but_hard_reason_is_excluded():
    slate = build_ranked_research_slate(
        [
            {"ticker": "SOFT", "alpha_score": 2, "no_trade_reason": "confidence floor"},
            {"ticker": "HARD", "alpha_score": 3, "hard_no_trade_reason": "halt"},
        ],
        target=5,
    )
    assert slate["symbols"] == ["SOFT"]
    assert slate["rows"][0]["no_trade_reason"] == "confidence floor"


def test_tier_one_coexists_with_one_official_and_tier_two_three_remain_zero():
    rows = [
        {
            "ticker": "OFFICIAL",
            "alpha_score": 100,
            "plan_qualified": True,
            "can_alert": True,
            "alert_gate_status": "PASS",
        },
        {"ticker": "RESEARCH", "alpha_score": 99},
    ]
    slate = build_ranked_research_slate(rows)
    annotated = apply_publication_semantics(
        rows,
        slate=slate,
        coverage={"secondary_fallback_status": "applied_research_only_above_ceiling"},
    )
    assert annotated[0]["publication_tier"] == TIER1
    assert annotated[0]["plan_qualification_status"] == "WAITING_CURRENT_CHECKS"
    counts = publication_counts(annotated, official_selected=1)
    assert counts == {
        "ranked_research": 2,
        "paper_plan_qualified": 0,
        "alertable_trade": 0,
        "official_selected": 1,
    }
    text = format_alpha_watch(signals=annotated, edge_label="research")
    assert "OFFICIAL PAPER CANDIDATES" not in text


def test_official_rows_are_exact_frozen_promotions_not_current_review_rows():
    frozen = [
        {
            "ticker": "FROZEN",
            "publication_tier": "PAPER_PLAN_QUALIFIED",
            "alert_gate_status": "PASS",
            "manual_confirmation_required": False,
        }
    ]
    current = {
        "ticker": "CURRENT",
        "decision_tier": "clean_edge",
        "alert_gate_status": "PASS",
        "manual_confirmation_required": False,
    }

    assert [row["ticker"] for row in official_publication_rows(frozen)] == ["FROZEN"]
    assert official_publication_rows([]) == []
    assert official_publication_rows([current]) == []


def test_alpha_watch_reports_the_exact_frozen_slate_instead_of_retry_replacements():
    frozen = {
        "ticker": "FROZEN",
        "publication_tier": TIER1,
        "research_only": True,
        "broker_execution": "disabled",
    }
    current = {
        "ticker": "CURRENT",
        "publication_tier": TIER1,
        "research_only": True,
        "broker_execution": "disabled",
    }
    text = format_alpha_watch(
        signals=[current],
        edge_label="research",
        source_summary={
            "ranked_research_slate": {"published_count": 1, "rows": [frozen]},
            "ranked_research_publication_rows": [frozen],
        },
    )

    assert "FROZEN" in text
    assert "CURRENT" not in text
    assert "Research slate: 1 of 1 shown" in text


def test_alpha_no_trade_shows_all_five_frozen_research_candidates():
    rows = [
        {
            "ticker": f"RANK{index}",
            "publication_tier": TIER1,
            "research_only": True,
            "broker_execution": "disabled",
        }
        for index in range(1, 6)
    ]

    text = format_alpha_no_trade(
        reason="No plan passed current entry gates.",
        next_action="Keep the frozen research slate under observation.",
        research_signals=rows,
        research_total=5,
    )

    assert "Research slate: 5 of 5 shown" in text
    for index in range(1, 6):
        assert f"RANK{index}" in text


def test_alpha_no_trade_length_limit_preserves_slate_and_research_only_footer():
    rows = [
        {
            "ticker": f"RANK{index}",
            "publication_tier": TIER1,
            "research_only": True,
            "broker_execution": "disabled",
            "radar_reason": "conditional evidence " + ("x" * 200),
        }
        for index in range(1, 6)
    ]

    text = format_alpha_no_trade(
        reason="current safety gate blocked " + ("r" * 5_000),
        next_action="wait for verified evidence " + ("n" * 5_000),
        research_signals=rows,
        research_total=5,
        max_chars=4096,
    )

    assert len(text) <= 4096
    assert text.endswith("Radar outcomes are tracked after close. No orders placed. Research only.")
    assert "Slate symbols (5/5): RANK1, RANK2, RANK3, RANK4, RANK5" in text
    assert "No-trade reason: current safety gate blocked" in text


def test_alpha_watch_keeps_explicit_empty_slate_authoritative():
    candidate = {
        "ticker": "CURRENT",
        "publication_tier": TIER1,
        "research_only": True,
        "broker_execution": "disabled",
    }
    text = format_alpha_watch(
        signals=[candidate],
        edge_label="research",
        source_summary={
            "ranked_research_slate": {
                "published_count": 0,
                "rows": [],
            },
            "ranked_research_publication_rows": [],
        },
    )

    assert "CURRENT" not in text
    assert "Research slate: 0 of 0 shown" in text


def test_alpha_watch_empty_slate_cannot_promote_current_plan_candidate():
    current = {
        "ticker": "CURRENT",
        "publication_tier": "PAPER_PLAN_QUALIFIED",
        "alert_gate_status": "PASS",
        "manual_confirmation_required": False,
        "research_only": True,
        "broker_execution": "disabled",
    }
    text = format_alpha_watch(
        signals=[current],
        edge_label="research",
        source_summary={
            "ranked_research_slate": {
                "published_count": 0,
                "rows": [],
            }
        },
    )

    assert "CURRENT" not in text
    assert "0 official candidates" in text
    assert "Research slate: 0 of 0 shown" in text


def test_tier_semantics_and_research_monitor_label():
    plan = {
        "schema_version": "alphaops.structural_plan.v1",
        "status": "COMPLETE",
        "entry": {"value": 10, "source_id": "entry", "observation_hash": "e" * 64},
        "stop": {"value": 9, "source_id": "stop", "observation_hash": "s" * 64},
        "target": {"value": 12, "source_id": "target", "observation_hash": "t" * 64},
        "provenance": {
            "independent": True,
            "observations": [
                {"source_id": "entry", "observation_hash": "e" * 64},
                {"source_id": "stop", "observation_hash": "s" * 64},
                {"source_id": "target", "observation_hash": "t" * 64},
            ],
        },
    }
    plan["plan_hash_sha256"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    row = {
        "ticker": "SAFE",
        "alpha_score": 1,
        "entry_trigger": 10,
        "invalidation": 9,
        "target_1": 12,
        "can_alert": True,
        "alert_gate_status": "PASS",
        "plan_qualified": True,
        "publication_tier": TIER1,
        "strategy_id": "alphaops_v4",
        "strategy_receipt_status": "COMPLETE",
        "structural_plan_contract": plan,
        "after_cost_rr": 1.5,
    }
    annotated = apply_publication_semantics([row], slate={"rows": [row]}, coverage={})
    assert annotated[0]["publication_tier"] == TIER1
    assert (
        monitor_alpha_signals(annotated, current_prices={"SAFE": 10.1})["events"][0]["label"]
        == "RESEARCH CONDITION MET"
    )


def test_live_fallback_ceiling_demotes_a_genuinely_qualified_plan_to_tier_one():
    plan = {
        "schema_version": "alphaops.structural_plan.v1",
        "status": "COMPLETE",
        "entry": {"value": 10, "source_id": "entry", "observation_hash": "e" * 64},
        "stop": {"value": 9, "source_id": "stop", "observation_hash": "s" * 64},
        "target": {"value": 12, "source_id": "target", "observation_hash": "t" * 64},
        "provenance": {
            "independent": True,
            "observations": [
                {"source_id": "entry", "observation_hash": "e" * 64},
                {"source_id": "stop", "observation_hash": "s" * 64},
                {"source_id": "target", "observation_hash": "t" * 64},
            ],
        },
    }
    plan["plan_hash_sha256"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    row = {
        "ticker": "QUAL",
        "entry_trigger": 10,
        "invalidation": 9,
        "target_1": 12,
        "strategy_id": "alphaops_v4",
        "strategy_receipt_status": "COMPLETE",
        "structural_plan_contract": plan,
        "after_cost_rr": 1.5,
        "can_alert": True,
        "alert_gate_status": "PASS",
    }
    slate = build_ranked_research_slate([row])
    assert (
        apply_publication_semantics([row], slate=slate, coverage={})[0]["publication_tier"] == TIER1
    )
    assert (
        apply_publication_semantics(
            [row],
            slate=slate,
            coverage={"secondary_fallback_status": "applied_research_only_above_ceiling"},
        )[0]["publication_tier"]
        == TIER1
    )


def test_receipt_hash_or_three_urls_cannot_stand_in_for_structural_plan():
    row = {
        "ticker": "WEAK",
        "strategy_id": "alphaops_v4",
        "strategy_receipt_status": "COMPLETE",
        "receipt_hash_sha256": "a" * 64,
        "condition_results": [
            {"source_urls": ["u1"]},
            {"source_urls": ["u2"]},
            {"source_urls": ["u3"]},
        ],
        "entry_trigger": 10,
        "invalidation": 9,
        "target_1": 12,
        "after_cost_rr": 1.5,
    }
    slate = build_ranked_research_slate([row])
    assert apply_publication_semantics([row], slate=slate)[0]["publication_tier"] == TIER1


def test_core_discovery_uses_only_ready_members_and_merges_lanes():
    class Provider:
        def validate_credentials(self):
            return None

        def get_premarket_snapshot(self, symbols, config):
            return [
                {
                    "ticker": symbols[0],
                    "source": "alpaca_iex",
                    "source_timestamp": datetime.now(timezone.utc).isoformat(),
                    "premarket_price": 10,
                }
            ]

    contract = {
        "status": "READY",
        "members": [{"symbol": "AAPL", "index_memberships": ["S&P 500", "Nasdaq-100"]}],
    }
    result = discover_core_universe_rows(contract, config=object(), provider=Provider())
    assert result["status"] == "READY"
    assert result["rows"][0]["universe_lane"] == "core"
    assert "S&P 500" in result["rows"][0]["discovery_context"]
    merged = merge_core_universe_rows(
        [{"ticker": "AAPL", "discovery_context": "mover"}], result["rows"]
    )
    assert merged[0]["universe_lane"] == "mover+core"


def test_core_lane_eligibility_does_not_apply_mover_gap_floor():
    ranked = rank_core_universe_rows(
        [{"ticker": "FLAT", "premarket_price": 10, "premarket_volume": 100, "gap_pct": 0}],
        allow_legacy_unbound=True,
    )
    assert [row["ticker"] for row in ranked] == ["FLAT"]


def test_production_scheduler_exposes_governed_core_manifest_path():
    script = Path("scripts/run_alphaops_morning.ps1").read_text(encoding="utf-8")
    assert "$CoreUniverseManifest" in script
    assert "--core-universe-manifest" in script
    assert "config\\luna_core_universe.json" in script
    assert "refresh_luna_core_universe.py" in script
    assert "luna_core_refresh-$MarketDate" in script
    assert '"--market-date", $MarketDate' in script
    assert "if ($CoreUniverseManifest)" in script
    assert "lane-local" in script
    assert '$CoreUniverseManifest = ""' in script


def test_alpha_watch_length_limit_preserves_slate_blockers_and_no_broker_footer():
    def verbose_row(ticker: str) -> dict:
        return {
            "ticker": ticker,
            "publication_tier": TIER1,
            "research_only": True,
            "broker_execution": "disabled",
            "alpha_score": 80,
            "receipt_id": f"receipt-{ticker}-" + ("r" * 120),
            "pick_tier": "PICK_WITH_DISCLOSED_GAPS",
            "strategy_id": "strategy-" + ("s" * 80),
            "strategy_version": "version-" + ("v" * 80),
            "receipt_reason": "qualified evidence " + ("e" * 180),
            "core_conditions_passed": [f"condition-{index}-" + ("c" * 30) for index in range(6)],
            "ai_resolved_evidence": [
                {
                    "condition_id": f"ai-{index}-" + ("a" * 30),
                    "source_urls": [f"https://example.test/{ticker}/{index}/" + ("u" * 80)],
                }
                for index in range(3)
            ],
            "disclosed_gaps": ["gap-" + ("g" * 80) for _ in range(4)],
        }

    slate_rows = [verbose_row(f"SAFE{index}") for index in range(1, 6)]
    blocked_rows = [
        {
            **verbose_row(f"BLOCK{index}"),
            "no_trade_reason": f"reason-{index}-" + ("x" * 180),
        }
        for index in range(1, 4)
    ]
    body = format_alpha_watch(
        signals=slate_rows,
        edge_label="research",
        blocked_signals=blocked_rows,
        source_summary={
            "ranked_research_slate": {"published_count": 5, "rows": slate_rows},
            "ranked_research_publication_rows": slate_rows,
        },
        max_chars=4096,
    )

    assert len(body) <= 4096
    assert body.endswith("No orders placed. Research only.")
    assert "Slate symbols (5/5): SAFE1, SAFE2, SAFE3, SAFE4, SAFE5" in body
    assert "Blocked rows: 3" in body
    for ticker in ("BLOCK1", "BLOCK2", "BLOCK3"):
        assert ticker in body
