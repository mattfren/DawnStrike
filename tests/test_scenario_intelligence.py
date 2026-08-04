from pathlib import Path

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.news_provider import _scenario_article_from_alpaca
from intraday_scanner.scenario.contracts import ScenarioExtraction, ScenarioNewsArticle
from intraday_scanner.scenario.engine import PriceContext, evaluate_scenario
from intraday_scanner.services.scenario_intelligence_service import (
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
                    "direction": "bullish",
                    "factual_claim": "The company announced a customer contract award.",
                    "evidence_spans": ["material customer contract award"],
                    "materiality": "high",
                    "uncertainty_flags": [],
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
            source_bar_hash_sha256="barhash",
        ),
    )

    assert decision.action == "ENTER_LONG"
    assert decision.entry_trigger is not None
    assert decision.invalidation_level is not None
    assert decision.target_1 is not None
    assert decision.calibration_status == "UNCALIBRATED"
    assert decision.broker_execution_enabled is False


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
                    "direction": "bullish",
                    "factual_claim": "An unconfirmed report asserts a deal.",
                    "evidence_spans": ["unconfirmed"],
                    "materiality": "high",
                    "uncertainty_flags": [],
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
    assert first["action_counts"] == {"WATCH": 1}
    assert second["cached_extraction_count"] == 1
    assert calls == ["news-1"]
    assert len(store.load_scenario_news_items()) == 1
    assert len(store.load_scenario_decisions()) == 1


def test_finalize_and_public_snapshot_report_only_resolved_paper_return(tmp_path: Path) -> None:
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

    result = finalize_scenario_performance(db_path=db_path, market_date=date)
    public = scenario_public_snapshot(db_path=db_path)

    assert result["gross_return_pct"] == 10.0
    assert result["modeled_after_cost_return_pct"] == 9.5
    assert result["benchmark_return_pct"] is None
    assert public["calibration_status"] == "UNCALIBRATED"
    assert public["records"][0]["headline"] == _article().headline
    assert "content" not in public["records"][0]


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
        def get_minute_bars(self, *args, **kwargs):
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
