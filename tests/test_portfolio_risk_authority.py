from intraday_scanner.risk.portfolio import (
    PortfolioOrderProposal,
    PortfolioRiskLimits,
    PortfolioRiskSnapshot,
    evaluate_portfolio_risk,
)


def _snapshot(**overrides):
    values = {
        "equity": 100_000.0,
        "daily_realized_pnl": 0.0,
        "daily_unrealized_pnl": 0.0,
        "peak_equity": 100_000.0,
        "as_of": "2026-08-30T14:00:00Z",
        "metadata_complete": True,
    }
    values.update(overrides)
    return PortfolioRiskSnapshot.from_mappings(**values)


def _proposal(**overrides):
    values = {
        "symbol": "ABC",
        "side": "long",
        "quantity": 100,
        "price": 100.0,
        "stop_price": 99.0,
        "strategy_id": "alphaops_v5",
        "price_observed_at": "2026-08-30T14:00:00Z",
        "metadata_complete": True,
    }
    values.update(overrides)
    return PortfolioOrderProposal(**values)


def test_clean_proposal_is_allowed_and_target_never_controls_admission():
    decision = evaluate_portfolio_risk(_proposal(), _snapshot())
    assert decision.allowed is True
    assert decision.action == "PAPER_ALLOW"
    assert decision.computed["daily_return_target_pct"] == 0.01
    assert "1%" not in " ".join(decision.reason_codes)


def test_multi_strategy_positions_are_aggregated_for_gross_and_net_caps():
    positions = [
        {
            "symbol": "AAA",
            "side": "long",
            "quantity": 500,
            "mark_price": 100,
            "entry_price": 100,
            "stop_price": 99,
            "price_observed_at": "2026-08-30T14:00:00Z",
        },
        {
            "symbol": "BBB",
            "side": "long",
            "quantity": 400,
            "mark_price": 100,
            "entry_price": 100,
            "stop_price": 99,
            "price_observed_at": "2026-08-30T14:00:00Z",
        },
    ]
    decision = evaluate_portfolio_risk(
        _proposal(symbol="CCC", quantity=50),
        _snapshot(positions=tuple()),
        limits=PortfolioRiskLimits(max_gross_exposure_pct=1.0, max_net_exposure_pct=0.75),
    )
    assert decision.allowed is True
    aggregate = _snapshot(positions=tuple())
    aggregate = PortfolioRiskSnapshot.from_mappings(
        equity=aggregate.equity,
        positions=positions,
        daily_realized_pnl=0.0,
        daily_unrealized_pnl=0.0,
        peak_equity=100_000.0,
        as_of=aggregate.as_of,
    )
    blocked = evaluate_portfolio_risk(_proposal(symbol="CCC", quantity=200), aggregate)
    assert "GROSS_EXPOSURE_LIMIT" in blocked.reason_codes
    assert "NET_EXPOSURE_LIMIT" in blocked.reason_codes


def test_unknown_and_stale_inputs_fail_closed():
    unknown = evaluate_portfolio_risk(_proposal(price=None), _snapshot())
    assert unknown.allowed is False
    assert "PROPOSAL_PRICE_UNKNOWN" in unknown.reason_codes
    stale = evaluate_portfolio_risk(
        _proposal(price_observed_at="2026-08-30T13:00:00Z"),
        _snapshot(),
    )
    assert stale.allowed is False
    assert "STALE_PRICE" in stale.reason_codes
    missing_metadata = evaluate_portfolio_risk(
        _proposal(metadata_complete=False),
        _snapshot(),
    )
    assert missing_metadata.allowed is False
    assert "PROPOSAL_METADATA_UNKNOWN" in missing_metadata.reason_codes


def test_existing_positions_require_real_timely_marks_without_entry_fallback():
    missing_mark = PortfolioRiskSnapshot.from_mappings(
        equity=100_000.0,
        positions=(
            {
                "symbol": "HELD",
                "side": "long",
                "quantity": 10,
                "entry_price": 50.0,
                "stop_price": 49.0,
                "price_observed_at": "2026-08-30T14:00:00Z",
            },
        ),
        daily_realized_pnl=0.0,
        daily_unrealized_pnl=0.0,
        peak_equity=100_000.0,
        as_of="2026-08-30T14:00:00Z",
        metadata_complete=True,
    )
    decision = evaluate_portfolio_risk(_proposal(), missing_mark)
    assert "POSITION_PRICE_UNKNOWN" in decision.reason_codes

    stale_mark = PortfolioRiskSnapshot.from_mappings(
        equity=100_000.0,
        positions=(
            {
                "symbol": "HELD",
                "side": "long",
                "quantity": 10,
                "mark_price": 50.0,
                "entry_price": 50.0,
                "stop_price": 49.0,
                "price_observed_at": "2026-08-30T13:00:00Z",
            },
        ),
        daily_realized_pnl=0.0,
        daily_unrealized_pnl=0.0,
        peak_equity=100_000.0,
        as_of="2026-08-30T14:00:00Z",
        metadata_complete=True,
    )
    assert "STALE_PRICE" in evaluate_portfolio_risk(_proposal(), stale_mark).reason_codes


def test_future_price_timestamps_fail_closed():
    decision = evaluate_portfolio_risk(
        _proposal(price_observed_at="2026-08-30T14:01:00Z"),
        _snapshot(),
    )
    assert "FUTURE_PRICE_TIMESTAMP" in decision.reason_codes


def test_daily_stop_drawdown_and_live_execution_are_hard_blocks():
    decision = evaluate_portfolio_risk(
        _proposal(live_execution_requested=True),
        _snapshot(daily_realized_pnl=-1_000.0, daily_unrealized_pnl=-600.0, peak_equity=120_000.0),
    )
    assert decision.allowed is False
    assert {"LIVE_EXECUTION_DISABLED", "DAILY_LOSS_LIMIT", "DRAWDOWN_LIMIT"}.issubset(
        decision.reason_codes
    )


def test_symbol_and_theme_concentration_are_receipted():
    positions = (
        {
            "symbol": "ABC",
            "side": "long",
            "quantity": 900,
            "mark_price": 100,
            "entry_price": 100,
            "stop_price": 99,
            "theme": "ai",
            "price_observed_at": "2026-08-30T14:00:00Z",
        },
    )
    decision = evaluate_portfolio_risk(
        _proposal(quantity=200, theme="ai"),
        PortfolioRiskSnapshot.from_mappings(
            equity=100_000.0,
            positions=positions,
            daily_realized_pnl=0.0,
            daily_unrealized_pnl=0.0,
            peak_equity=100_000.0,
            as_of="2026-08-30T14:00:00Z",
        ),
    )
    assert decision.allowed is False
    assert "SYMBOL_CONCENTRATION_LIMIT" in decision.reason_codes
    assert "THEME_CONCENTRATION_LIMIT" in decision.reason_codes
    assert len(decision.receipt_hash_sha256) == 64
