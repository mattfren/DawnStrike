import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intraday_scanner.ai.scenario_claim_extractor import extract_claims
from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.news_provider import _scenario_article_from_alpaca
from intraday_scanner.scenario.contracts import ScenarioExtraction, ScenarioNewsArticle
from intraday_scanner.scenario.engine import PriceContext, evaluate_scenario
from intraday_scanner.services.scenario_intelligence_service import (
    SCENARIO_COST_MODEL_VERSION,
    _deterministic_bootstrap_ci,
    _forward_lifecycle_row,
    _simulate_replay_trade,
    close_open_scenario_positions,
    finalize_scenario_performance,
    run_scenario_cycle,
    run_scenario_historical_replay,
    scenario_public_snapshot,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _article() -> ScenarioNewsArticle:
    return ScenarioNewsArticle(
        article_id="news-1",
        symbols=("NOVA",),
        headline="NOVA reports material contract award",
        summary="Contract award announced.",
        content="NOVA announced a material customer contract award with binding terms.",
        source="Business Wire",
        source_url="https://example.test/release",
        created_at="2026-08-03T14:00:00Z",
    )


def _extraction(article: ScenarioNewsArticle) -> ScenarioExtraction:
    return ScenarioExtraction.from_dict(
        article_id=article.article_id,
        model="gpt-5.6-terra",
        value={
            "status": "ok",
            "claims": [
                {
                    "event_type": "contract_customer",
                    "mechanism_polarity": "positive",
                    "factual_claim": "The company announced a customer contract award.",
                    "evidence_spans": ["material customer contract award"],
                    "materiality": "high",
                    "uncertainty_flags": [],
                    "claim_status": "verified_fact",
                    "causal_mechanism": "Adds contracted customer demand.",
                    "affected_business_variable": "revenue backlog",
                    "horizon": "near_term",
                    "novelty": "new",
                }
            ],
            "abstain_reason": "",
            "input_hash_sha256": "input-1",
        },
    )


def _config(db_path: Path) -> ScannerConfig:
    return ScannerConfig(
        database_path=db_path,
        provider="alpaca",
        scenario_intelligence_enabled=True,
    )


def test_alpaca_news_normalization_labels_historical_timestamp_proxy() -> None:
    article = _scenario_article_from_alpaca(
        {
            "id": 123,
            "symbols": ["nova"],
            "headline": "NOVA reports update",
            "summary": "summary",
            "content": "body",
            "source": "Reuters",
            "url": "https://example.test/news",
            "created_at": "2026-08-03T14:00:00Z",
        },
        historical=True,
    )

    assert article.article_id == "123"
    assert article.symbols == ("NOVA",)
    assert article.timing_kind == "provider_published_at_proxy"
    assert article.tier == "T2"


def test_alpaca_news_normalization_rejects_missing_provider_timestamp() -> None:
    assert (
        _scenario_article_from_alpaca(
            {"id": "missing-time", "symbols": ["NOVA"], "headline": "Unknown timing"},
            historical=False,
        )
        is None
    )


def test_engine_generates_levels_only_from_fact_and_completed_bar_context() -> None:
    article = _article()
    decision = evaluate_scenario(
        article=article,
        extraction=_extraction(article),
        ticker="NOVA",
        decision_at="2026-08-03T14:05:00Z",
        price_context=PriceContext(
            observed_at="2026-08-03T14:04:00Z",
            price=10.0,
            atr=0.4,
            spread_pct=0.5,
            liquid=True,
            source_bar_hash_sha256="a" * 64,
            bar_completed_at="2026-08-03T14:05:00Z",
            is_complete=True,
        ),
    )

    assert decision.action == "ENTER_LONG"
    assert decision.entry_trigger is not None
    assert decision.invalidation_level is not None
    assert decision.target_1 is not None
    assert decision.calibration_status == "UNCALIBRATED"
    assert decision.broker_execution_enabled is False


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda context: None, "price_context_missing"),
        (
            lambda context: replace(
                context,
                observed_at="2026-08-03T14:05:00Z",
                bar_completed_at="2026-08-03T14:06:00Z",
            ),
            "price_evidence_future",
        ),
        (
            lambda context: replace(context, source_bar_hash_sha256="not-a-hash"),
            "price_source_hash_missing_or_invalid",
        ),
        (lambda context: replace(context, is_complete=False), "price_bar_incomplete"),
    ],
)
def test_engine_abstains_on_missing_future_unhashed_or_incomplete_price_evidence(
    mutate, reason: str
) -> None:
    article = _article()
    valid = PriceContext(
        observed_at="2026-08-03T14:04:00Z",
        price=10.0,
        atr=0.4,
        spread_pct=0.5,
        liquid=True,
        source_bar_hash_sha256="a" * 64,
        bar_completed_at="2026-08-03T14:05:00Z",
        is_complete=True,
    )

    decision = evaluate_scenario(
        article=article,
        extraction=_extraction(article),
        ticker="NOVA",
        decision_at="2026-08-03T14:05:00Z",
        price_context=mutate(valid),
    )

    assert decision.action == "ABSTAIN"
    assert reason in decision.reason_codes
    assert decision.entry_trigger is None


def test_engine_maps_negative_mechanism_deterministically_without_model_trade_direction() -> None:
    article = _article()
    extraction = ScenarioExtraction.from_dict(
        article_id=article.article_id,
        model="gpt-5.6-terra",
        value={
            "status": "ok",
            "claims": [
                {
                    "event_type": "financing_dilution",
                    "mechanism_polarity": "negative",
                    "factual_claim": "The company filed an at-the-market offering.",
                    "evidence_spans": ["at-the-market offering"],
                    "materiality": "high",
                    "uncertainty_flags": [],
                    "claim_status": "verified_fact",
                    "causal_mechanism": "Additional shares dilute existing ownership.",
                    "affected_business_variable": "shares outstanding",
                    "horizon": "near_term",
                    "novelty": "new",
                }
            ],
            "abstain_reason": "",
        },
    )
    context = PriceContext(
        observed_at="2026-08-03T14:04:00Z",
        price=10.0,
        atr=0.4,
        spread_pct=0.5,
        liquid=True,
        source_bar_hash_sha256="a" * 64,
        bar_completed_at="2026-08-03T14:05:00Z",
        is_complete=True,
    )

    decision = evaluate_scenario(
        article=article,
        extraction=extraction,
        ticker="NOVA",
        decision_at="2026-08-03T14:05:00Z",
        price_context=context,
    )

    assert decision.direction == "bearish"
    assert decision.action == "AVOID"
    assert extraction.claims[0].mechanism_polarity == "negative"


def test_engine_abstains_on_rumor_even_when_extraction_is_bullish() -> None:
    article = _article()
    rumor = ScenarioExtraction.from_dict(
        article_id=article.article_id,
        model="gpt-5.6-terra",
        value={
            "status": "ok",
            "claims": [
                {
                    "event_type": "rumor",
                    "mechanism_polarity": "positive",
                    "factual_claim": "An unconfirmed report asserts a deal.",
                    "evidence_spans": ["unconfirmed"],
                    "materiality": "high",
                    "uncertainty_flags": [],
                    "claim_status": "rumor",
                    "causal_mechanism": "A deal could add acquired operations.",
                    "affected_business_variable": "revenue",
                    "horizon": "medium_term",
                    "novelty": "new",
                }
            ],
            "abstain_reason": "",
        },
    )

    decision = evaluate_scenario(
        article=article,
        extraction=rumor,
        ticker="NOVA",
        decision_at="2026-08-03T14:05:00Z",
        price_context=None,
    )

    assert decision.action == "ABSTAIN"
    assert "rumor_requires_independent_corroboration" in decision.reason_codes


def test_extraction_contract_rejects_trade_instruction_fields() -> None:
    with pytest.raises(ValueError, match="forbidden decision field"):
        ScenarioExtraction.from_dict(
            article_id="news-1",
            model="gpt-5.6-terra",
            value={
                "status": "ok",
                "claims": [],
                "abstain_reason": "",
                "action": "buy",
            },
        )


def test_extraction_contract_rejects_price_target_text() -> None:
    with pytest.raises(ValueError, match="forbidden price-level content"):
        ScenarioExtraction.from_dict(
            article_id="news-1",
            model="gpt-5.6-terra",
            value={
                "status": "ok",
                "claims": [
                    {
                        "event_type": "analyst_action",
                        "mechanism_polarity": "positive",
                        "factual_claim": "An analyst raised a price target to $210.",
                        "evidence_spans": ["raised a price target to $210"],
                        "materiality": "medium",
                        "uncertainty_flags": [],
                        "claim_status": "attributed_third_party_claim",
                        "causal_mechanism": "Changed third-party valuation view.",
                        "affected_business_variable": "valuation",
                        "horizon": "near_term",
                        "novelty": "new",
                    }
                ],
                "abstain_reason": "",
            },
        )


@pytest.mark.parametrize(
    "forbidden_text",
    [
        "An analyst issued a buy recommendation.",
        "The entry level is $10 and the stop loss is $9.",
        "The probability of a rally is high.",
        "The projected return is 12 percent.",
        "The position size should be 500 shares.",
    ],
)
def test_extraction_contract_rejects_non_fact_output_categories(forbidden_text: str) -> None:
    with pytest.raises(ValueError, match="forbidden"):
        ScenarioExtraction.from_dict(
            article_id="news-1",
            model="gpt-5.6-terra",
            value={
                "status": "ok",
                "claims": [
                    {
                        "event_type": "analyst_action",
                        "mechanism_polarity": "positive",
                        "factual_claim": forbidden_text,
                        "evidence_spans": [forbidden_text],
                        "materiality": "medium",
                        "uncertainty_flags": [],
                        "claim_status": "attributed_third_party_claim",
                        "causal_mechanism": "Changed third-party view.",
                        "affected_business_variable": "valuation",
                        "horizon": "near_term",
                        "novelty": "new",
                    }
                ],
                "abstain_reason": "",
            },
        )


def test_extractor_sanitizes_contract_violations_to_rejected_receipt() -> None:
    article = _article()

    class FakeResponse:
        id = "resp-test"
        model = "gpt-5.6-terra-2026-08-01"
        usage = {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
        output_text = """{
          \"status\": \"ok\",
          \"claims\": [{
            \"event_type\": \"analyst_action\",
            \"mechanism_polarity\": \"positive\",
            \"factual_claim\": \"The analyst raised a price target to $210.\",
            \"evidence_spans\": [],
            \"materiality\": \"medium\",
            \"uncertainty_flags\": [],
            \"claim_status\": \"attributed_third_party_claim\",
            \"causal_mechanism\": \"Sentiment changed.\",
            \"affected_business_variable\": \"valuation\",
            \"horizon\": \"near_term\",
            \"novelty\": \"new\"
          }],
          \"abstain_reason\": \"\",
          \"prompt_injection_detected\": false,
          \"contradictions\": [],
          \"dependencies\": [],
          \"unresolved_unknowns\": []
        }"""

    class FakeResponses:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    extraction = extract_claims(
        article=article,
        api_key="test-key",  # pragma: allowlist secret
        model="gpt-5.6-terra",
        timeout_seconds=1.0,
        max_article_chars=1000,
        client=FakeClient(),
    )

    assert extraction.status == "rejected"
    assert extraction.claims == ()
    assert extraction.abstain_reason == "fact_only_contract_violation"
    assert extraction.response_id == "resp-test"
    assert extraction.model == "gpt-5.6-terra-2026-08-01"
    assert extraction.requested_model == "gpt-5.6-terra"
    assert extraction.usage["total_tokens"] == 5


def test_extractor_rejects_response_without_actual_model_identifier() -> None:
    article = _article()

    class FakeResponse:
        id = "resp-no-model"
        usage = {"total_tokens": 5}
        output_text = json.dumps(
            {
                "status": "ok",
                "claims": [
                    {
                        "event_type": "contract_customer",
                        "mechanism_polarity": "positive",
                        "factual_claim": "The company announced a customer contract.",
                        "evidence_spans": ["announced a customer contract"],
                        "materiality": "high",
                        "uncertainty_flags": [],
                        "claim_status": "company_claim",
                        "causal_mechanism": "Adds contracted demand.",
                        "affected_business_variable": "revenue backlog",
                        "horizon": "near_term",
                        "novelty": "new",
                    }
                ],
                "abstain_reason": "",
                "prompt_injection_detected": False,
                "contradictions": [],
                "dependencies": [],
                "unresolved_unknowns": [],
            }
        )

    class FakeResponses:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    extraction = extract_claims(
        article=article,
        api_key="test-key",  # pragma: allowlist secret
        model="gpt-5.6-terra",
        timeout_seconds=1.0,
        max_article_chars=1000,
        client=FakeClient(),
    )

    assert extraction.status == "rejected"
    assert extraction.abstain_reason == "actual_model_identifier_missing"
    assert extraction.model == ""
    assert extraction.requested_model == "gpt-5.6-terra"


def test_engine_abstains_on_unresolved_or_injected_extraction() -> None:
    article = _article()
    extraction = ScenarioExtraction.from_dict(
        article_id=article.article_id,
        model="gpt-5.6-terra",
        value={
            "status": "ok",
            "claims": [
                {
                    "event_type": "contract_customer",
                    "mechanism_polarity": "positive",
                    "factual_claim": "The company announced a customer contract award.",
                    "evidence_spans": ["material customer contract award"],
                    "materiality": "high",
                    "uncertainty_flags": [],
                    "claim_status": "verified_fact",
                    "causal_mechanism": "Adds contracted revenue demand.",
                    "affected_business_variable": "revenue backlog",
                    "horizon": "near_term",
                    "novelty": "new",
                }
            ],
            "abstain_reason": "",
            "prompt_injection_detected": True,
            "contradictions": [],
            "dependencies": ["customer delivery acceptance"],
            "unresolved_unknowns": ["contract value"],
        },
    )

    decision = evaluate_scenario(
        article=article,
        extraction=extraction,
        ticker="NOVA",
        decision_at="2026-08-03T14:05:00Z",
        price_context=None,
    )

    assert decision.action == "ABSTAIN"
    assert "prompt_injection_detected" in decision.reason_codes
    assessment = decision.features["extraction_assessment"]
    assert assessment["dependencies"] == ["customer delivery acceptance"]
    assert assessment["unresolved_unknowns"] == ["contract value"]


def test_cycle_persists_fact_only_records_and_reuses_extraction_cache(tmp_path: Path) -> None:
    db_path = tmp_path / "scenario.sqlite"
    article = _article()

    class FakeNews:
        def get_articles(self, *args, **kwargs):
            return [article]

    calls = []

    def fake_extractor(**kwargs):
        calls.append(kwargs["article"].article_id)
        return _extraction(article)

    first = run_scenario_cycle(
        db_path=db_path,
        symbols=["NOVA"],
        config=_config(db_path),
        news_provider=FakeNews(),
        extractor=fake_extractor,
        price_contexts={},
        now="2026-08-03T14:05:00Z",
    )
    second = run_scenario_cycle(
        db_path=db_path,
        symbols=["NOVA"],
        config=_config(db_path),
        news_provider=FakeNews(),
        extractor=fake_extractor,
        price_contexts={},
        now="2026-08-03T14:10:00Z",
    )

    store = SQLiteScanStore(db_path)
    assert first["action_counts"] == {"ABSTAIN": 1}
    assert second["cached_extraction_count"] == 1
    assert calls == ["news-1"]
    assert len(store.load_scenario_news_items()) == 1
    assert len(store.load_scenario_decisions()) == 1


def test_finalize_excludes_closed_position_without_complete_return_truth(tmp_path: Path) -> None:
    db_path = tmp_path / "scenario.sqlite"
    store = SQLiteScanStore(db_path)
    store.initialize()
    date = "2026-08-03"
    store.persist_scenario_decisions(
        [
            {
                "decision_id": "decision-1",
                "article_id": "article-1",
                "ticker": "NOVA",
                "market_date": date,
                "decision_at": f"{date}T14:00:00Z",
                "event_type": "contract_customer",
                "direction": "bullish",
                "directional_evidence_score": 6.0,
                "action": "ENTER_LONG",
                "cohort": "scenario_forward",
                "policy_version": "dawnstrike-news-scenario-v1",
                "source_tier": "T1",
                "source_lineage_hash_sha256": "source",
                "feature_hash_sha256": "features",
                "features": {},
            }
        ]
    )
    store.persist_scenario_news_items([_article().as_dict() | {"article_id": "article-1"}])
    store.upsert_scenario_signal_links(
        [
            {
                "decision_id": "decision-1",
                "signal_id": "scenario:decision-1",
                "cohort": "scenario_forward",
                "strategy_id": "news_scenario_v1",
                "strategy_version": "dawnstrike-news-scenario-v1",
                "created_at": f"{date}T14:00:00Z",
                "updated_at": f"{date}T15:00:00Z",
            }
        ]
    )
    store.persist_paper_positions(
        [
            {
                "position_id": "position-1",
                "signal_id": "scenario:decision-1",
                "market_date": date,
                "ticker": "NOVA",
                "status": "CLOSED",
                "quantity": 10,
                "entry_price": 10,
                "exit_price": 11,
                "opened_at": f"{date}T14:00:00Z",
                "closed_at": f"{date}T15:00:00Z",
                "realized_pnl": 10,
                "realized_return_pct": 10,
                "updated_at": f"{date}T15:00:00Z",
            }
        ]
    )
    store.persist_paper_trade_fills(
        [
            {
                "fill_id": "fill-1",
                "position_id": "position-1",
                "intent_id": "intent-1",
                "signal_id": "scenario:decision-1",
                "market_date": date,
                "ticker": "NOVA",
                "side": "BUY",
                "fill_time": f"{date}T14:00:00Z",
                "fill_price": 10,
                "quantity": 10,
                "gross_notional": 100,
                "slippage_bps": 50,
            }
        ]
    )

    result = finalize_scenario_performance(
        db_path=db_path,
        market_date=date,
        config=_config(db_path),
    )
    public = scenario_public_snapshot(db_path=db_path)

    assert result["gross_return_pct"] is None
    assert result["modeled_after_cost_return_pct"] is None
    assert result["benchmark_return_pct"] is None
    assert result["closed_eligible_count"] == 0
    assert result["return_denominator_count"] == 0
    assert result["missing_count"] == 1
    assert result["lifecycle_states"][0]["eligibility_reason"] == (
        "closed_position_missing_entry_or_exit_fill"
    )
    assert public["calibration_status"] == "UNCALIBRATED"
    assert public["records"][0]["headline"] == _article().headline
    assert "content" not in public["records"][0]
    assert public["records"][0]["paper_lifecycle"]["status"] == "CLOSED"
    assert public["records"][0]["paper_lifecycle"]["return_eligibility_status"] == "missing"
    assert public["records"][0]["paper_lifecycle"]["outcome_id"] == "scenario:decision-1"


def test_finalize_uses_sourced_spy_bars_for_after_cost_excess(tmp_path: Path) -> None:
    db_path = tmp_path / "scenario.sqlite"
    store = SQLiteScanStore(db_path)
    store.initialize()
    date = "2026-08-03"
    store.persist_scenario_decisions(
        [
            {
                "decision_id": "decision-1",
                "article_id": "article-1",
                "ticker": "NOVA",
                "market_date": date,
                "decision_at": f"{date}T14:00:00Z",
                "event_type": "contract_customer",
                "direction": "bullish",
                "directional_evidence_score": 6.0,
                "action": "ENTER_LONG",
                "cohort": "scenario_forward",
                "policy_version": "dawnstrike-news-scenario-v1",
                "source_tier": "T1",
                "source_lineage_hash_sha256": "source",
                "feature_hash_sha256": "features",
                "features": {},
            }
        ]
    )
    store.upsert_scenario_signal_links(
        [
            {
                "decision_id": "decision-1",
                "signal_id": "scenario:decision-1",
                "cohort": "scenario_forward",
                "strategy_id": "news_scenario_v1",
                "strategy_version": "dawnstrike-news-scenario-v1",
                "created_at": f"{date}T14:00:00Z",
                "updated_at": f"{date}T15:00:00Z",
            }
        ]
    )
    store.persist_paper_positions(
        [
            {
                "position_id": "position-1",
                "signal_id": "scenario:decision-1",
                "market_date": date,
                "ticker": "NOVA",
                "status": "CLOSED",
                "quantity": 10,
                "entry_intent_id": "intent-entry",
                "exit_intent_id": "intent-exit",
                "entry_price": 10,
                "exit_price": 11,
                "opened_at": f"{date}T14:00:00Z",
                "closed_at": f"{date}T15:00:00Z",
                "realized_pnl": 10,
                "realized_return_pct": 10,
                "updated_at": f"{date}T15:00:00Z",
            }
        ]
    )
    store.persist_paper_trade_fills(
        [
            {
                "fill_id": "fill-entry",
                "position_id": "position-1",
                "intent_id": "intent-entry",
                "signal_id": "scenario:decision-1",
                "market_date": date,
                "ticker": "NOVA",
                "side": "BUY",
                "fill_time": f"{date}T14:00:00Z",
                "fill_price": 10,
                "quantity": 10,
                "gross_notional": 100,
                "slippage_bps": 50,
                "cost_model_version": SCENARIO_COST_MODEL_VERSION,
            },
            {
                "fill_id": "fill-exit",
                "position_id": "position-1",
                "intent_id": "intent-exit",
                "signal_id": "scenario:decision-1",
                "market_date": date,
                "ticker": "NOVA",
                "side": "SELL",
                "fill_time": f"{date}T15:00:00Z",
                "fill_price": 11,
                "quantity": 10,
                "gross_notional": 110,
                "slippage_bps": 50,
                "cost_model_version": SCENARIO_COST_MODEL_VERSION,
            },
        ]
    )

    class FakeMarket:
        def get_minute_bars(self, *args, **kwargs):
            return [
                {
                    "ticker": "SPY",
                    "timestamp": f"{date}T14:00:00Z",
                    "open": 100,
                    "high": 100,
                    "low": 100,
                    "close": 100,
                },
                {
                    "ticker": "SPY",
                    "timestamp": f"{date}T15:00:00Z",
                    "open": 101,
                    "high": 101,
                    "low": 101,
                    "close": 101,
                },
            ]

    config = ScannerConfig(
        database_path=db_path,
        provider="alpaca",
        scenario_intelligence_enabled=True,
        alpaca_api_key_id="test-key",
        alpaca_api_secret_key="test-secret",  # pragma: allowlist secret
    )
    result = finalize_scenario_performance(
        db_path=db_path,
        market_date=date,
        config=config,
        market_provider=FakeMarket(),
    )

    assert result["benchmark_return_pct"] == 1.0
    assert result["gross_return_pct"] == 10.0
    assert result["modeled_after_cost_return_pct"] == 9.0
    assert result["excess_return_pct"] == 8.0
    assert result["closed_eligible_count"] == 1
    assert result["return_denominator_count"] == 1
    assert result["benchmark"]["status"] == "sourced_complete"
    link = store.load_scenario_signal_links(decision_id="decision-1")[0]
    assert link["entry_fill_id"] == "fill-entry"
    assert link["exit_fill_id"] == "fill-exit"
    assert link["paper_trade_id"] == "position-1"
    assert link["outcome_id"] == "scenario:decision-1"


def test_scenario_close_succeeds_when_nothing_is_open(tmp_path: Path) -> None:
    result = close_open_scenario_positions(
        db_path=tmp_path / "scenario.sqlite", market_date="2026-08-03"
    )

    assert result["status"] == "no_open_scenario_positions"
    assert result["broker_execution_enabled"] is False


def test_historical_replay_uses_strictly_later_bars_and_stays_out_of_forward_metrics(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "scenario.sqlite"
    article = _article()
    bars = [
        {
            "ticker": "NOVA",
            "timestamp": f"2026-08-03T{hour:02d}:{minute:02d}:00Z",
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.0,
            "volume": 10_000,
        }
        for hour, minute in [(12, value) for value in range(45, 60)]
    ] + [
        {
            "ticker": "NOVA",
            "timestamp": "2026-08-03T14:01:00Z",
            "open": 10.0,
            "high": 10.2,
            "low": 10.0,
            "close": 10.1,
            "volume": 10_000,
        },
        {
            "ticker": "NOVA",
            "timestamp": "2026-08-03T14:02:00Z",
            "open": 10.1,
            "high": 10.2,
            "low": 10.0,
            "close": 10.1,
            "volume": 10_000,
        },
        {
            "ticker": "NOVA",
            "timestamp": "2026-08-03T14:03:00Z",
            "open": 10.2,
            "high": 11.0,
            "low": 10.2,
            "close": 10.9,
            "volume": 10_000,
        },
    ]

    class FakeNews:
        def get_articles(self, *args, **kwargs):
            return [article]

    class FakeMarket:
        def get_minute_bars(self, symbols, *args, **kwargs):
            if symbols == ["SPY"]:
                return [
                    {
                        "ticker": "SPY",
                        "timestamp": "2026-08-03T14:01:00Z",
                        "open": 100.0,
                        "high": 100.0,
                        "low": 100.0,
                        "close": 100.0,
                    },
                    {
                        "ticker": "SPY",
                        "timestamp": "2026-08-03T14:02:00Z",
                        "open": 101.0,
                        "high": 101.0,
                        "low": 101.0,
                        "close": 101.0,
                    },
                ]
            return bars

        def get_first_quote_after(self, *args, **kwargs):
            return {"spread_pct": 0.25, "timestamp": "2026-08-03T14:00:01Z"}

    result = run_scenario_historical_replay(
        db_path=db_path,
        symbols=["NOVA"],
        start="2026-08-03T00:00:00Z",
        end="2026-08-03T23:59:59Z",
        config=_config(db_path),
        news_provider=FakeNews(),
        market_provider=FakeMarket(),
        extractor=lambda **kwargs: _extraction(article),
    )
    public = scenario_public_snapshot(db_path=db_path)

    assert result["trade_count"] == 1
    assert result["daily_metrics"][0]["cohort"] == "scenario_historical_replay"
    assert result["daily_metrics"][0]["closed_eligible_count"] == 1
    assert public["performance"] == []
    assert len(public["historical_replay"]["performance"]) == 1


def test_forward_lifecycle_states_remain_distinct() -> None:
    decision = {"decision_id": "decision-1", "ticker": "NOVA"}
    link = {"signal_id": "scenario:decision-1", "position_id": "position-1"}
    entry = {
        "fill_id": "fill-entry",
        "position_id": "position-1",
        "intent_id": "intent-entry",
        "signal_id": "scenario:decision-1",
        "side": "BUY",
        "fill_time": "2026-08-03T14:00:00Z",
        "fill_price": 10.0,
        "quantity": 10,
        "slippage_bps": 25,
        "cost_model_version": SCENARIO_COST_MODEL_VERSION,
    }
    exit_fill = {
        **entry,
        "fill_id": "fill-exit",
        "intent_id": "intent-exit",
        "side": "SELL",
        "fill_price": 11.0,
    }

    untriggered = _forward_lifecycle_row(
        decision=decision, link=link, position=None, fills=[]
    )
    open_row = _forward_lifecycle_row(
        decision=decision,
        link=link,
        position={
            "position_id": "position-1",
            "status": "OPEN",
            "entry_intent_id": "intent-entry",
        },
        fills=[entry],
    )
    missing = _forward_lifecycle_row(
        decision=decision,
        link=link,
        position={
            "position_id": "position-1",
            "status": "CLOSED",
            "entry_intent_id": "intent-entry",
            "exit_intent_id": "intent-exit",
        },
        fills=[entry],
    )
    quarantined = _forward_lifecycle_row(
        decision=decision,
        link=link,
        position={
            "position_id": "position-1",
            "status": "CLOSED",
            "entry_intent_id": "intent-entry",
            "exit_intent_id": "intent-exit",
            "realized_return_pct": 10.0,
        },
        fills=[entry, exit_fill],
    )

    assert [
        untriggered["eligibility_state"],
        open_row["eligibility_state"],
        missing["eligibility_state"],
        quarantined["eligibility_state"],
    ] == ["untriggered", "open", "missing", "quarantined"]
    assert quarantined["eligibility_reason"] == "same_bar_or_reversed_fill_order_ambiguous"


def test_bootstrap_ci_is_seeded_deterministic_and_samples_with_replacement() -> None:
    first = _deterministic_bootstrap_ci([-10.0, 10.0])
    second = _deterministic_bootstrap_ci([10.0, -10.0])

    assert first == second
    assert first["method"] == "seeded_with_replacement_mean"
    assert first["lower"] == -10.0
    assert first["upper"] == 10.0


def test_replay_quarantines_entry_bar_exit_order_without_return() -> None:
    outcome = _simulate_replay_trade(
        decision={
            "decision_id": "decision-ambiguous",
            "ticker": "NOVA",
            "market_date": "2026-08-03",
            "entry_trigger": 10.0,
            "invalidation_level": 9.0,
            "target_1": 11.0,
        },
        article=_article(),
        bars=[
            {
                "ticker": "NOVA",
                "timestamp": "2026-08-03T14:01:00Z",
                "open": 9.8,
                "high": 11.5,
                "low": 8.5,
                "close": 10.5,
            }
        ],
        event_time=datetime(2026, 8, 3, 14, 0, tzinfo=UTC),
        slippage_bps=50,
        quote={"spread_pct": 0.25},
    )

    assert outcome["outcome_status"] == "quarantined"
    assert outcome["quarantine_reason"] == "same_bar_entry_exit_order_ambiguous"
    assert outcome["gross_return_pct"] is None
    assert outcome["modeled_after_cost_return_pct"] is None
