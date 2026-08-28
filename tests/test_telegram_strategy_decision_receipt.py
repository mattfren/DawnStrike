from intraday_scanner.notifiers.telegram_formatter import format_alpha_watch


def test_notification_surfaces_receipt_tier_and_entry_requirement() -> None:
    body = format_alpha_watch(
        signals=[
            {
                "ticker": "TEST",
                "can_alert": True,
                "decision_tier": "clean_edge",
                "alert_gate_status": "PASS",
                "manual_confirmation_required": False,
                "alpha_score": 90,
                "receipt_id": "sdr-fixture",
                "pick_tier": "CONDITIONAL_PICK",
                "strategy_id": "ts_momentum_sma_atr",
                "strategy_version": "v1",
                "reward_risk_ratio": 2,
                "paper_entry_eligible": False,
                "receipt_reason": "research eligible with disclosed gaps",
                "core_conditions_passed": ["valid_symbol", "trend_regime"],
                "ai_resolved_evidence": [
                    {
                        "condition_id": "corporate_action",
                        "source_urls": ["https://www.sec.gov/fixture?ignored=1"],
                    }
                ],
                "disclosed_gaps": ["borrow_or_locate_verified"],
                "first_blocking_failure": "borrow_or_locate_verified",
            }
        ],
        edge_label="HIGH",
    )
    assert "sdr-fixture" in body
    assert "CONDITIONAL_PICK" in body
    assert "ts_momentum_sma_atr v1" in body
    assert "R/R 2.0R" in body
    assert "entry confirmation required" in body
    assert "Core passed: valid_symbol, trend_regime" in body
    assert "sec.gov/fixture" in body
    assert "Gaps: borrow_or_locate_verified" in body


def test_long_lineage_at_telegram_limit_is_not_silently_clipped() -> None:
    contributor_ids = [f"strategy_{index}_{'x' * 40}" for index in range(9)]
    contributors = [
        {
            "strategy_id": strategy_id,
            "strategy_version": "v1",
            "source_signal_id": f"source-{index}",
            "strategy_adapter": "morning_strategy_adapter_v3",
            "prior_session_lineage": {
                "source_signal_id": f"source-{index}",
                "prior_session_date": "2026-08-27",
            },
        }
        for index, strategy_id in enumerate(contributor_ids)
    ]
    signals = [
        {
            "ticker": f"T{index}ST",
            "can_alert": True,
            "decision_tier": "clean_edge",
            "alert_gate_status": "PASS",
            "manual_confirmation_required": False,
            "alpha_score": 90,
            "receipt_id": f"receipt-{index}",
            "pick_tier": "CONDITIONAL_PICK",
            "strategy_id": "alphaops_v5",
            "strategy_version": "v5",
            "strategy_contributors": contributors,
            "risk_flags": ["x" * 80],
        }
        for index in range(5)
    ]

    body = format_alpha_watch(signals=signals, edge_label="HIGH", max_chars=4096)

    assert "Contributors (one ranked row):" in body
    assert contributor_ids[-1] in body
    assert "Adapter prior-session lineage:" in body
    assert "source-8@2026-08-27" in body
    assert "No orders placed. Research only." in body
    assert len(body) <= 4096
