from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from intraday_scanner.v2.data import MarketBar
from intraday_scanner.v2.paper_ops.experiment_registry import (
    REQUIRED_PROMOTION_METRICS,
    REQUIRED_PROMOTION_THRESHOLDS,
    build_experiment_registry,
    build_governance_overlay,
)
from intraday_scanner.v2.paper_ops.fleet_allocator import (
    FleetAllocatorPolicy,
    FleetCandidate,
    allocate_shadow_fleet,
)
from intraday_scanner.v2.paper_ops.position_management import (
    BorrowAvailability,
    challenger_position_policies,
    evaluate_entry_availability,
    evaluate_position_management,
    trading_sessions_elapsed,
)
from intraday_scanner.v2.strategies import Direction


def _bar(
    value: str,
    *,
    open_price: float = 100.0,
    high: float = 102.0,
    low: float = 99.0,
    close: float = 101.0,
) -> MarketBar:
    return MarketBar(
        symbol="AAA",
        timestamp=datetime.fromisoformat(value).replace(tzinfo=timezone.utc),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1_000_000,
    )


def test_timeout_uses_market_sessions_and_strategy_invalidation_is_causal() -> None:
    policy = challenger_position_policies()[0]
    opened = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
    monday = _bar("2026-08-03T20:00:00", close=98.0)

    assert trading_sessions_elapsed(opened.date(), monday.timestamp.date()) == 1
    invalidated = evaluate_position_management(
        policy,
        direction=Direction.LONG,
        opened_at=opened,
        stop=90.0,
        target=120.0,
        bar=monday,
        context={"sma50": 99.0},
    )
    assert invalidated.action == "CLOSE"
    assert invalidated.reason == "strategy_invalidation"
    assert invalidated.raw_exit_price == 98.0

    one_session_policy = replace(
        policy,
        policy_version="test-one-session-timeout",
        timeout_trading_sessions=1,
        invalidation_callback=lambda _bar, _context: False,
    )
    timeout = evaluate_position_management(
        one_session_policy,
        direction=Direction.LONG,
        opened_at=opened,
        stop=90.0,
        target=120.0,
        bar=monday,
    )
    assert timeout.reason == "trading_session_timeout"
    assert timeout.trading_sessions_held == 1


def test_same_bar_is_stop_first_and_short_borrow_fails_closed() -> None:
    policy = challenger_position_policies()[0]
    opened = datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc)
    same_bar = _bar(
        "2026-07-31T20:00:00",
        open_price=100.0,
        high=111.0,
        low=89.0,
        close=105.0,
    )

    decision = evaluate_position_management(
        policy,
        direction=Direction.LONG,
        opened_at=opened,
        stop=90.0,
        target=110.0,
        bar=same_bar,
        context={"sma50": 95.0},
    )
    assert decision.reason == "stop"
    assert decision.raw_exit_price == 90.0

    blocked = evaluate_entry_availability(
        policy,
        direction=Direction.SHORT,
        borrow=None,
    )
    assert blocked.action == "BLOCK_ENTRY"
    assert blocked.reason == "short_borrow_not_verified"
    verified = evaluate_entry_availability(
        policy,
        direction=Direction.SHORT,
        borrow=BorrowAvailability(
            status="verified_available",
            located_at="2026-07-31T13:00:00Z",
            borrow_cost_bps_per_session=35.0,
            source_ref="borrow-feed:AAA:2026-07-31",
        ),
    )
    assert verified.action == "ALLOW_ENTRY"


def test_experiments_are_one_change_forward_only_and_never_auto_promote() -> None:
    registry = build_experiment_registry()

    assert len(registry) == 6
    assert len({row.experiment_id for row in registry}) == len(registry)
    for contract in registry:
        assert contract.primary_hypothesis
        assert contract.controlled_change
        assert len(contract.frozen_configuration_hash) == 64
        assert contract.required_metrics == REQUIRED_PROMOTION_METRICS
        assert contract.promotion_thresholds == (
            REQUIRED_PROMOTION_THRESHOLDS
        )
        assert contract.promotion_thresholds[
            "forward_market_days_min"
        ] == 60
        assert contract.promotion_thresholds[
            "forward_closed_trades_min"
        ] == 100
        assert contract.training_cutoff == "2026-07-30"
        assert contract.validation_start == "2026-07-31"
        assert contract.untouched_holdout_start > contract.validation_end
        assert contract.auto_promotion_enabled is False
        assert contract.promotion_decision == (
            "NOT_ELIGIBLE_AWAITING_OPERATOR_REVIEW"
        )
        assert contract.broker_execution_enabled is False


def test_governance_quarantines_unverified_short_and_proven_inert_series() -> None:
    overlay = build_governance_overlay(
        strategy_rows=[
            {
                "strategy_id": "failed_breakout_reversal_short",
                "strategy_version": "v1.0",
                "execution_policy_version": "policy-v2",
                "strategy_semantics_fingerprint": "a" * 64,
                "eligible_sessions": 12,
                "accepted_signals": 3,
            },
            {
                "strategy_id": "inert",
                "strategy_version": "v1.0",
                "execution_policy_version": "policy-v2",
                "strategy_semantics_fingerprint": "b" * 64,
                "eligible_sessions": 20,
                "accepted_signals": 0,
            },
        ],
        generated_at="2026-07-30T22:00:00Z",
    )

    reasons = {row["reason"] for row in overlay["entries"]}
    assert reasons == {
        "short_borrow_not_verified",
        "inert_activation_logic_unproven",
    }
    assert all(row["allow_entries"] is False for row in overlay["entries"])
    assert overlay["auto_enable_supported"] is False


def test_fleet_allocator_separates_asset_ranks_and_blocks_overlap_and_borrow() -> None:
    candidates = [
        FleetCandidate(
            "stock-a",
            "s1",
            "v1",
            "AAA",
            "stock",
            Direction.LONG,
            90.0,
            100.0,
            5_000.0,
            "technology",
            "accepted",
        ),
        FleetCandidate(
            "etf-a",
            "s2",
            "v1",
            "SPY",
            "etf",
            Direction.LONG,
            70.0,
            100.0,
            5_000.0,
            "broad_market",
            "accepted",
        ),
        FleetCandidate(
            "stock-duplicate",
            "s3",
            "v1",
            "AAA",
            "stock",
            Direction.LONG,
            80.0,
            100.0,
            5_000.0,
            "technology",
            "accepted",
        ),
        FleetCandidate(
            "short-no-borrow",
            "s4",
            "v1",
            "BBB",
            "stock",
            Direction.SHORT,
            75.0,
            100.0,
            5_000.0,
            "financials",
            "accepted",
        ),
    ]

    result = allocate_shadow_fleet(
        candidates,
        policy=FleetAllocatorPolicy(
            max_positions=3,
            max_symbol_overlap=1,
            max_correlation_group_positions=1,
        ),
    )

    assert [(row["asset_type"], row["asset_cohort_rank"]) for row in result["selected"]] == [
        ("stock", 1),
        ("etf", 1),
    ]
    blocked_reasons = {row["reason"] for row in result["blocked"]}
    assert "duplicate_symbol_overlap" in blocked_reasons
    assert "short_borrow_not_verified" in blocked_reasons
    assert result["diagnostics"]["duplicate_symbol_candidates"] == 1
    assert result["diagnostics"]["stock_etf_ranked_separately"] is True
    assert result["diagnostics"]["individual_strategy_accounts_mutated"] is False
