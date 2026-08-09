from __future__ import annotations

from datetime import datetime, timedelta, timezone

from intraday_scanner.alpha.v5_policy import evaluate_v5_causal_exit

UTC = timezone.utc
START = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def _bar(index: int, **values: float) -> dict[str, object]:
    return {
        "timestamp": START + timedelta(minutes=index),
        "open": values.get("open", 10.0),
        "high": values.get("high", 10.2),
        "low": values.get("low", 9.8),
        "close": values.get("close", 10.0),
    }


def test_v5_causal_exit_uses_shared_path_truth() -> None:
    payload = evaluate_v5_causal_exit(
        [_bar(0), _bar(1, high=11.1, close=10.8)],
        decision_at=START,
        trigger=10.1,
        target=11.0,
        stop=9.0,
    )

    assert payload["path_truth_status"] == "RESOLVED_TARGET_FIRST"
    assert payload["exit_policy"] == "target_stop_first"
    assert payload["promotion_eligible"] is False


def test_v5_session_close_challenger_remains_causal_and_research_only() -> None:
    payload = evaluate_v5_causal_exit(
        [_bar(0), _bar(1, high=10.4, close=10.3), _bar(2, close=10.6)],
        decision_at=START,
        trigger=10.1,
        target=11.0,
        stop=9.0,
        exit_policy="session_close",
    )

    assert payload["conservative_policy_result"] == "session_close"
    assert payload["exit_price"] == 10.6
    assert payload["research_only"] is True
