from __future__ import annotations

from intraday_scanner.alpha.v6.registry import promotion_review_packet
from intraday_scanner.alpha.v6_shadow import promotion_readiness


def test_v6_promotion_is_manual_even_when_packet_exists() -> None:
    packet = promotion_review_packet(evidence={"sample_size": 0})
    readiness = promotion_readiness([])

    assert packet["approved"] is False
    assert packet["automatic_promotion"] is False
    assert readiness["status"] == "NOT_ELIGIBLE_FOR_PROMOTION"
