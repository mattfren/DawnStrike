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
                "paper_entry_eligible": False,
                "first_blocking_failure": "borrow_or_locate_verified",
            }
        ],
        edge_label="HIGH",
    )
    assert "sdr-fixture" in body
    assert "CONDITIONAL_PICK" in body
    assert "entry confirmation required" in body
