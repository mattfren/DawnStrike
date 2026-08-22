from __future__ import annotations

import json
from types import SimpleNamespace

from intraday_scanner.ai.strategy_gap_resolver import StrategyGapResolver


def test_cited_context_is_resolved_without_market_numbers() -> None:
    body = {
        "claims": [
            {
                "claim_id": "claim-1",
                "symbol": "TEST",
                "condition_id": "catalyst_event",
                "claim_type": "catalyst_event",
                "statement": "Issuer announced a sanitized event.",
                "source_urls": ["https://issuer.example/release"],
                "source_hashes": [],
                "published_at": "2026-08-22T12:00:00+00:00",
                "effective_at": None,
                "authoritative": True,
                "supported": True,
            }
        ],
        "unresolved_unknowns": [],
    }
    response = SimpleNamespace(
        output_text=json.dumps(body),
        model="gpt-test",
        id="resp-test",
        usage={"total_tokens": 12},
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: response))
    result = StrategyGapResolver(
        api_key="test-key",
        model="gpt-requested",
        client=client,
    ).resolve(
        symbol="TEST",
        market_date="2026-08-22",
        decision_at="2026-08-22T14:00:00+00:00",
        condition_ids=["catalyst_event"],
        source_identity="fixture-source",
    )
    assert result["status"] == "completed"
    assert result["condition_results"][0]["status"] == "RESOLVED_FROM_SOURCE"
    assert result["run"]["actual_model"] == "gpt-test"


def test_provider_failure_becomes_disclosed_gap() -> None:
    result = StrategyGapResolver(api_key="", model="gpt-test").resolve(
        symbol="TEST",
        market_date="2026-08-22",
        decision_at="2026-08-22T14:00:00+00:00",
        condition_ids=["catalyst_event"],
        source_identity="fixture-source",
    )
    assert result["condition_results"][0]["status"] == "MISSING_DISCLOSED"
    assert result["claims"] == []
