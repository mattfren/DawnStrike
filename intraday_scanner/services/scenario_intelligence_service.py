"""Orchestrate sourced news facts through the governed paper-trade lifecycle."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.ai.scenario_claim_extractor import extract_claims, extraction_input_hash
from intraday_scanner.config import ScannerConfig, load_config
from intraday_scanner.errors import DataProviderError
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.news_provider import AlpacaNewsProvider
from intraday_scanner.scenario.contracts import (
    SCENARIO_FEATURE_SCHEMA_VERSION,
    SCENARIO_FORWARD_COHORT,
    SCENARIO_POLICY_VERSION,
    SCENARIO_STRATEGY_ID,
    ScenarioExtraction,
    ScenarioNewsArticle,
    canonical_hash,
    utc_now_iso,
)
from intraday_scanner.scenario.engine import PriceContext, evaluate_scenario
from intraday_scanner.services.trade_watcher_service import MODE_PAPER, run_trade_watcher
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def scenario_doctor(*, db_path: str | Path, config: ScannerConfig | None = None) -> dict[str, Any]:
    config = config or load_config(database_path=Path(db_path))
    store = SQLiteScanStore(db_path)
    store.initialize()
    _register_scenario_policy(store, config)
    checks = {
        "feature_enabled": config.scenario_intelligence_enabled,
        "openai_key_present": bool(config.openai_api_key),
        "alpaca_key_present": bool(config.alpaca_api_key_id),
        "alpaca_secret_present": bool(config.alpaca_api_secret_key),
        "model": config.scenario_openai_model,
        "research_only": True,
        "broker_execution_enabled": False,
        "calibration_status": "UNCALIBRATED",
    }
    blocking = [
        name
        for name in ("openai_key_present", "alpaca_key_present", "alpaca_secret_present")
        if not checks[name]
    ]
    return {
        "status": "READY" if checks["feature_enabled"] and not blocking else "NOT_READY",
        "checks": checks,
        "blocking": blocking if checks["feature_enabled"] else ["feature_enabled"],
        "model_registry": {
            "policy_version": SCENARIO_POLICY_VERSION,
            "calibration_status": "UNCALIBRATED",
            "promotion_state": "research_only",
        },
    }


def run_scenario_cycle(
    *,
    db_path: str | Path,
    symbols: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    historical: bool = False,
    dry_run: bool = False,
    config: ScannerConfig | None = None,
    news_provider: AlpacaNewsProvider | None = None,
    extractor: Any = extract_claims,
    price_contexts: dict[str, PriceContext] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Fetch current or historical news, extract facts, and create governed paper candidates.

    Historical provider records are deliberately labeled timestamp proxies and
    cannot be mixed into forward performance cohorts.
    """

    config = config or load_config(database_path=Path(db_path))
    if not config.scenario_intelligence_enabled:
        raise DataProviderError(
            "Scenario intelligence is disabled by DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED."
        )
    if historical:
        cohort = "scenario_historical_replay"
    else:
        cohort = SCENARIO_FORWARD_COHORT
    now = now or utc_now_iso()
    market_date = now[:10]
    store = SQLiteScanStore(db_path)
    store.initialize()
    _register_scenario_policy(store, config)
    run_id = "scenario_" + uuid.uuid4().hex
    started_at = utc_now_iso()
    wanted_symbols = _resolve_symbols(store, symbols=symbols, market_date=market_date)
    provider = news_provider or AlpacaNewsProvider(config)
    try:
        articles = provider.get_articles(
            wanted_symbols,
            since=since,
            until=until,
            historical=historical,
            limit=config.scenario_openai_max_articles_per_run,
        )
        if not dry_run:
            store.persist_scenario_news_items([article.as_dict() for article in articles])
        if price_contexts is None and not historical:
            price_contexts = _live_price_contexts(config, wanted_symbols, as_of=now)
        decisions: list[dict[str, Any]] = []
        materialized: list[dict[str, Any]] = []
        cached_count = 0
        for article in articles:
            extraction, cached = _extraction_for_article(
                store=store,
                article=article,
                config=config,
                extractor=extractor,
                dry_run=dry_run,
            )
            cached_count += int(cached)
            for ticker in article.symbols:
                if ticker not in wanted_symbols:
                    continue
                decision = evaluate_scenario(
                    article=article,
                    extraction=extraction,
                    ticker=ticker,
                    decision_at=now,
                    price_context=(price_contexts or {}).get(ticker),
                    cohort=cohort,
                )
                decision_payload = decision.as_dict()
                decisions.append(decision_payload)
                if not dry_run:
                    store.persist_scenario_decisions([decision_payload])
                    _record_scenario_event(
                        store,
                        decision_id=decision.decision_id,
                        event_type="DECISION_RECORDED",
                        event_timestamp=now,
                        payload={
                            "action": decision.action,
                            "cohort": cohort,
                            "policy_version": SCENARIO_POLICY_VERSION,
                        },
                    )
                if (
                    decision.action == "ENTER_LONG"
                    and cohort == SCENARIO_FORWARD_COHORT
                    and not dry_run
                ):
                    materialized.append(_materialize_decision(store, decision_payload))
        watcher = None
        if materialized and not dry_run:
            watcher = run_trade_watcher(
                db_path=db_path,
                mode=MODE_PAPER,
                source="alpaca",
                tickers=sorted({row["ticker"] for row in materialized}),
                market_date=market_date,
                requested_at=now,
                include_scenarios=True,
                config=config,
            )
            _link_watcher_records(store, materialized, watcher, now=now)
        result = {
            "run_id": run_id,
            "run_type": "historical_cycle" if historical else "forward_cycle",
            "status": "ok",
            "market_date": market_date,
            "cohort": cohort,
            "symbol_count": len(wanted_symbols),
            "article_count": len(articles),
            "cached_extraction_count": cached_count,
            "decision_count": len(decisions),
            "action_counts": _counts(decisions, "action"),
            "materialized_signal_count": len(materialized),
            "watcher": watcher,
            "historical_timing_note": (
                "provider publication timestamps are proxies; this cohort is excluded "
                "from forward performance."
                if historical
                else "forward-observed article timestamps are eligible for separate "
                "forward paper evaluation."
            ),
            "research_only": True,
            "broker_execution_enabled": False,
            "completed_at": utc_now_iso(),
        }
        if not dry_run:
            store.persist_scenario_run_receipt(
                {**result, "started_at": started_at, "error_code": ""}
            )
        return result
    except Exception as exc:
        receipt = {
            "run_id": run_id,
            "run_type": "historical_cycle" if historical else "forward_cycle",
            "status": "failed",
            "started_at": started_at,
            "completed_at": utc_now_iso(),
            "error_code": type(exc).__name__,
        }
        if not dry_run:
            store.persist_scenario_run_receipt(receipt)
        raise


def finalize_scenario_performance(
    *, db_path: str | Path, market_date: str | None = None
) -> dict[str, Any]:
    """Reconcile scenario-linked paper positions into honest daily return metrics."""

    store = SQLiteScanStore(db_path)
    store.initialize()
    date = market_date or datetime.now(UTC).date().isoformat()
    links = store.load_scenario_signal_links(limit=50_000)
    links_by_signal = {
        str(row.get("signal_id") or ""): row for row in links if row.get("signal_id")
    }
    decisions = store.load_scenario_decisions(
        start=date, end=date, cohort=SCENARIO_FORWARD_COHORT, limit=50_000
    )
    triggered = [row for row in decisions if row.get("action") == "ENTER_LONG"]
    positions = [
        row
        for row in store.load_paper_positions(market_date=date, limit=50_000)
        if str(row.get("signal_id") or "") in links_by_signal
    ]
    fills = store.load_paper_trade_fills(market_date=date, limit=50_000)
    fills_by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fill in fills:
        fills_by_signal[str(fill.get("signal_id") or "")].append(fill)
    closed = [
        row
        for row in positions
        if str(row.get("status") or "") == "CLOSED" and row.get("realized_return_pct") is not None
    ]
    open_rows = [row for row in positions if str(row.get("status") or "") == "OPEN"]
    gross = _mean([float(row["realized_return_pct"]) for row in closed])
    after_costs: list[float] = []
    outcome_rows: list[dict[str, Any]] = []
    for position in closed:
        signal_id = str(position.get("signal_id") or "")
        cost_pct = sum(
            float(fill.get("slippage_bps") or 0.0) / 100.0 for fill in fills_by_signal[signal_id]
        )
        after_costs.append(float(position["realized_return_pct"]) - cost_pct)
        outcome_rows.append(
            {
                "signal_id": signal_id,
                "market_date": date,
                "ticker": position.get("ticker"),
                "outcome_source": "scenario_paper_lifecycle",
                "entry_time": position.get("opened_at"),
                "entry_price": position.get("entry_price"),
                "close_price": position.get("exit_price"),
                "high_after_entry": None,
                "low_after_entry": None,
                "halted": None,
                "notes": (
                    "Scenario paper lifecycle resolved from sourced price observations; "
                    "modeled costs separately disclosed."
                ),
                "imported_at": utc_now_iso(),
                "validated_against_signal_timestamp": True,
                "outcome_status": "complete",
                "payload_json": {
                    "position_id": position.get("position_id"),
                    "modeled_cost_pct": cost_pct,
                },
            }
        )
    if outcome_rows:
        store.persist_signal_outcomes(outcome_rows, replace=True)
    metrics = {
        "market_date": date,
        "cohort": SCENARIO_FORWARD_COHORT,
        "strategy_id": SCENARIO_STRATEGY_ID,
        "policy_version": SCENARIO_POLICY_VERSION,
        "signal_count": len(decisions),
        "triggered_count": len(triggered),
        "closed_eligible_count": len(closed),
        "open_count": len(open_rows),
        "missing_count": max(len(triggered) - len(closed) - len(open_rows), 0),
        "quarantined_count": 0,
        "gross_return_pct": gross,
        "modeled_after_cost_return_pct": _mean(after_costs),
        "benchmark_return_pct": None,
        "excess_return_pct": None,
        "hit_rate_pct": _pct(
            sum(float(row["realized_return_pct"]) > 0 for row in closed), len(closed)
        ),
        "return_status": "complete" if closed else "no_closed_eligible_positions",
        "cost_model": "sum of recorded paper-fill slippage_bps; no unstated fees",
        "calibration_status": "UNCALIBRATED",
        "research_only": True,
    }
    store.persist_scenario_daily_performance([metrics])
    return metrics


def close_open_scenario_positions(
    *,
    db_path: str | Path,
    market_date: str | None = None,
    requested_at: str = "16:00",
    source: str = "alpaca",
    config: ScannerConfig | None = None,
) -> dict[str, Any]:
    """Close only currently open scenario paper positions at the governed EOD check.

    A day with no scenario position is successful and must not inherit AlphaOps'
    selection requirement.  Existing scenario positions still go through the one
    shared paper watcher, so fills and exits stay in the durable lifecycle ledger.
    """

    store = SQLiteScanStore(db_path)
    store.initialize()
    open_rows = [
        row
        for row in store.load_paper_positions(limit=50_000)
        if str(row.get("status") or "") == "OPEN"
        and str(row.get("strategy_id") or "") == SCENARIO_STRATEGY_ID
        and str(row.get("strategy_version") or "") == SCENARIO_POLICY_VERSION
        and str(row.get("cohort") or "") == SCENARIO_FORWARD_COHORT
        and (not market_date or str(row.get("market_date") or "")[:10] == market_date)
    ]
    if not open_rows:
        return {
            "status": "no_open_scenario_positions",
            "market_date": market_date or "",
            "research_only": True,
            "broker_execution_enabled": False,
        }
    return run_trade_watcher(
        db_path=db_path,
        mode=MODE_PAPER,
        source=source,
        tickers=sorted({str(row.get("ticker") or "").upper() for row in open_rows}),
        market_date=market_date,
        requested_at=requested_at,
        include_scenarios=True,
        config=config,
    )


def run_scenario_historical_replay(
    *,
    db_path: str | Path,
    symbols: list[str],
    start: str,
    end: str,
    config: ScannerConfig | None = None,
    news_provider: AlpacaNewsProvider | None = None,
    market_provider: AlpacaProvider | None = None,
    extractor: Any = extract_claims,
) -> dict[str, Any]:
    """Strict point-in-time historical replay, isolated from forward paper results.

    An entry uses only a completed pre-event bar and the first quote after the
    provider timestamp. Ambiguous stop/target bars are quarantined rather than
    assigned a favorable fill. This is an audit cohort, never a forward result.
    """

    config = config or load_config(database_path=Path(db_path))
    if not config.scenario_intelligence_enabled:
        raise DataProviderError("Scenario intelligence is disabled by configuration.")
    store = SQLiteScanStore(db_path)
    store.initialize()
    _register_scenario_policy(store, config)
    news = news_provider or AlpacaNewsProvider(config)
    market = market_provider or AlpacaProvider(config)
    articles = news.get_articles(
        symbols,
        since=start,
        until=end,
        historical=True,
        limit=config.scenario_openai_max_articles_per_run,
    )
    store.persist_scenario_news_items([article.as_dict() for article in articles])
    decisions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for article in articles:
        extraction, _ = _extraction_for_article(
            store=store, article=article, config=config, extractor=extractor, dry_run=False
        )
        event_time = _parse_iso(article.created_at)
        window_start = (event_time - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        window_end = _regular_session_end(event_time).isoformat().replace("+00:00", "Z")
        for ticker in article.symbols:
            if ticker not in symbols:
                continue
            bars = market.get_minute_bars([ticker], window_start, window_end, config)
            quote = market.get_first_quote_after(
                ticker,
                start=article.created_at,
                end=(event_time + timedelta(minutes=5)).isoformat(),
                config=config,
            )
            context = _replay_price_context(bars, event_time=event_time, quote=quote)
            decision = evaluate_scenario(
                article=article,
                extraction=extraction,
                ticker=ticker,
                decision_at=article.created_at,
                price_context=context,
                cohort="scenario_historical_replay",
            )
            payload = decision.as_dict()
            decisions.append(payload)
            store.persist_scenario_decisions([payload])
            _record_scenario_event(
                store,
                decision_id=decision.decision_id,
                event_type="REPLAY_DECISION_RECORDED",
                event_timestamp=article.created_at,
                payload={"action": decision.action, "cohort": "scenario_historical_replay"},
            )
            if decision.action != "ENTER_LONG":
                continue
            outcome = _simulate_replay_trade(
                decision=payload,
                article=article,
                bars=bars,
                event_time=event_time,
                slippage_bps=config.slippage_bps,
                quote=quote,
            )
            trades.append(outcome)
    if trades:
        store.persist_scenario_replay_trades(trades)
    metrics = _historical_replay_metrics(decisions, trades)
    store.persist_scenario_daily_performance(metrics)
    return {
        "status": "complete",
        "cohort": "scenario_historical_replay",
        "article_count": len(articles),
        "decision_count": len(decisions),
        "trade_count": len(trades),
        "daily_metrics": metrics,
        "research_only": True,
        "forward_performance_affected": False,
        "timing_disclosure": (
            "Alpaca historical provider publication timestamps are proxies, not "
            "recorded first-seen timestamps."
        ),
    }


def scenario_public_snapshot(*, db_path: str | Path, limit: int = 250) -> dict[str, Any]:
    """Safe static projection: no article content, secrets, or fabricated metrics."""

    store = SQLiteScanStore(db_path)
    store.initialize()
    decisions = store.load_scenario_decisions(limit=limit)
    news_by_id = {row["article_id"]: row for row in store.load_scenario_news_items(limit=limit)}
    all_performance = store.load_scenario_daily_performance(limit=365)
    performance = [row for row in all_performance if row.get("cohort") == SCENARIO_FORWARD_COHORT]
    replay_performance = [
        row for row in all_performance if row.get("cohort") == "scenario_historical_replay"
    ]
    replay_trades = store.load_scenario_replay_trades(limit=250)
    recent_runs = store.load_scenario_run_receipts(limit=20)
    records = []
    for row in decisions:
        article = news_by_id.get(str(row.get("article_id") or ""), {})
        records.append(
            {
                "decision_id": row.get("decision_id"),
                "market_date": row.get("market_date"),
                "decision_at": row.get("decision_at"),
                "ticker": row.get("ticker"),
                "event_type": row.get("event_type"),
                "direction": row.get("direction"),
                "directional_evidence_score": row.get("directional_evidence_score"),
                "action": row.get("action"),
                "reason_codes": row.get("reason_codes") or [],
                "entry_trigger": row.get("entry_trigger"),
                "invalidation_level": row.get("invalidation_level"),
                "target_1": row.get("target_1"),
                "time_stop": row.get("time_stop"),
                "source_tier": row.get("source_tier"),
                "source_url": article.get("source_url") or "",
                "headline": article.get("headline") or "",
                "provider": article.get("provider") or "",
                "timing_kind": article.get("timing_kind") or "",
                "calibration_status": "UNCALIBRATED",
                "policy_version": row.get("policy_version"),
                "research_only": True,
            }
        )
    return {
        "schema_version": "dawnstrike-scenarios-public-v1",
        "generated_at": utc_now_iso(),
        "research_only": True,
        "broker_execution_enabled": False,
        "calibration_status": "UNCALIBRATED",
        "records": records,
        "performance": performance,
        "historical_replay": {
            "performance": replay_performance,
            "trades": [
                {
                    key: row.get(key)
                    for key in (
                        "market_date",
                        "ticker",
                        "outcome_status",
                        "gross_return_pct",
                        "modeled_after_cost_return_pct",
                        "exit_reason",
                        "quarantine_reason",
                    )
                }
                for row in replay_trades
            ],
            "disclosure": (
                "Historical replay uses provider publication timestamps as proxies and is "
                "kept separate from forward paper performance."
            ),
        },
        "recent_runs": [
            {
                key: row.get(key)
                for key in (
                    "run_id",
                    "run_type",
                    "status",
                    "started_at",
                    "completed_at",
                    "error_code",
                )
            }
            for row in recent_runs
        ],
        "disclosures": [
            "Scenario intelligence is research-only and cannot place broker orders.",
            "OpenAI extracts constrained factual claims; deterministic policy owns "
            "every action and level.",
            "Historical Alpaca news is labeled as provider publication timestamp proxy "
            "and excluded from forward return reporting.",
            "No probabilities are displayed until separately calibrated on adequate "
            "forward out-of-sample evidence.",
        ],
    }


def _resolve_symbols(
    store: SQLiteScanStore, *, symbols: list[str] | None, market_date: str
) -> list[str]:
    if symbols:
        output = sorted({str(value).upper().strip() for value in symbols if str(value).strip()})
    else:
        output = sorted(
            {
                str(row.get("ticker") or "").upper()
                for row in store.load_historical_signals(market_date=market_date, limit=100)
                if str(row.get("ticker") or "").strip()
                and str(row.get("ticker") or "").upper() != "NO_TRADE"
            }
        )
    if not output:
        raise DataProviderError(
            "Scenario cycle needs --symbols or current AlphaOps candidates; "
            "no universe was available."
        )
    return output


def _extraction_for_article(
    *,
    store: SQLiteScanStore,
    article: ScenarioNewsArticle,
    config: ScannerConfig,
    extractor: Any,
    dry_run: bool,
) -> tuple[ScenarioExtraction, bool]:
    input_hash = extraction_input_hash(article, max_article_chars=config.scenario_article_max_chars)
    cached = store.load_scenario_extraction(
        article_id=article.article_id,
        model=config.scenario_openai_model,
        input_hash_sha256=input_hash,
    )
    if cached:
        payload = dict(cached)
        payload.pop("input_hash_sha256", None)
        extraction = ScenarioExtraction.from_dict(
            article_id=article.article_id,
            value={
                "status": payload.get("status"),
                "claims": payload.get("claims") or [],
                "abstain_reason": payload.get("abstain_reason") or "",
                "input_hash_sha256": input_hash,
            },
            model=str(payload.get("model") or config.scenario_openai_model),
            response_id=str(payload.get("response_id") or ""),
            usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
        )
        return extraction, True
    extraction = extractor(
        article=article,
        api_key=config.openai_api_key,
        model=config.scenario_openai_model,
        timeout_seconds=config.scenario_openai_timeout_seconds,
        max_article_chars=config.scenario_article_max_chars,
    )
    if extraction.input_hash_sha256 != input_hash:
        extraction = replace(extraction, input_hash_sha256=input_hash)
    if not dry_run:
        store.persist_scenario_extractions([extraction.as_dict()])
    return extraction, False


def _live_price_contexts(
    config: ScannerConfig, symbols: list[str], *, as_of: str
) -> dict[str, PriceContext]:
    provider = AlpacaProvider(config)
    try:
        snapshots = {row.ticker: row for row in provider.get_premarket_snapshot(symbols, config)}
        start = f"{as_of[:10]}T00:00:00Z"
        bars = provider.get_minute_bars(symbols, start, as_of, config)
    except DataProviderError:
        return {}
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bars:
        by_ticker[str(row.get("ticker") or "").upper()].append(row)
    output: dict[str, PriceContext] = {}
    for ticker in symbols:
        rows = sorted(by_ticker[ticker], key=lambda row: str(row.get("timestamp") or ""))
        snapshot = snapshots.get(ticker)
        if not rows or snapshot is None:
            continue
        last = rows[-1]
        closes = [float(row["close"]) for row in rows if _positive(row.get("close"))]
        ranges = [
            float(row["high"]) - float(row["low"])
            for row in rows[-14:]
            if _positive(row.get("high")) and _positive(row.get("low"))
        ]
        atr = sum(ranges) / len(ranges) if ranges else None
        output[ticker] = PriceContext(
            observed_at=str(last.get("timestamp") or as_of),
            price=closes[-1] if closes else None,
            atr=atr,
            spread_pct=float(snapshot.spread_pct) if snapshot.spread_pct is not None else None,
            liquid=bool(
                float(last.get("volume") or 0.0) * float(last.get("close") or 0.0) >= 20_000
            ),
            source_bar_hash_sha256=canonical_hash(rows[-14:]),
        )
    return output


def _materialize_decision(store: SQLiteScanStore, decision: dict[str, Any]) -> dict[str, Any]:
    decision_id = str(decision["decision_id"])
    signal_id = f"scenario:{decision_id}"
    now = str(decision["decision_at"])
    signal = {
        "signal_id": signal_id,
        "scan_id": f"scenario:{decision['market_date']}",
        "generated_at": now,
        "market_date": decision["market_date"],
        "ticker": decision["ticker"],
        "company": decision["ticker"],
        "rank": 1,
        "source": "alpaca_news_scenario",
        "source_url": "",
        "source_confidence": {"T1": 0.9, "T2": 0.75, "T3": 0.5}.get(
            str(decision.get("source_tier") or ""), 0.0
        ),
        "data_source_kind": "alpaca_news_and_minute_bars",
        "model_version": SCENARIO_POLICY_VERSION,
        "config_hash": decision["feature_hash_sha256"],
        "primary_setup": decision["event_type"],
        "setup_grade": "scenario_research",
        "signal_label": "scenario_enter_long",
        "entry_watch_level": decision["entry_trigger"],
        "entry_trigger_type": "deterministic_news_confirmation",
        "entry_condition": "price reaches deterministic trigger using a sourced observation",
        "confirmation_condition": "fact extraction, source tier, liquidity and spread gates passed",
        "exit_line": decision["invalidation_level"],
        "invalidation_level": decision["invalidation_level"],
        "target_1": decision["target_1"],
        "target_2": None,
        "risk_flags_json": ["research_only", "uncalibrated"],
        "avoid_reasons_json": [],
        "catalyst_summary": f"{decision['event_type']} / {decision['direction']}",
        "raw_payload_json": {"scenario_decision": decision},
    }
    selection = {
        "selection_id": f"scenario-selection:{decision_id}",
        "scan_id": signal["scan_id"],
        "signal_id": signal_id,
        "ticker": signal["ticker"],
        "rank": 1,
        "strategy_id": SCENARIO_STRATEGY_ID,
        "strategy_version": SCENARIO_POLICY_VERSION,
        "cohort": SCENARIO_FORWARD_COHORT,
        "decision": "paper_entry",
        "selected_at": now,
        "event_key": f"scenario-paper:{decision_id}",
        "body_sha256": canonical_hash(decision),
        "payload_json": {"scenario_decision_id": decision_id, "research_only": True},
    }
    store.persist_historical_signals([signal], replace=False)
    store.persist_signal_selections([selection])
    store.upsert_scenario_signal_links(
        [
            {
                "decision_id": decision_id,
                "signal_id": signal_id,
                "scan_id": signal["scan_id"],
                "cohort": SCENARIO_FORWARD_COHORT,
                "strategy_id": SCENARIO_STRATEGY_ID,
                "strategy_version": SCENARIO_POLICY_VERSION,
                "created_at": now,
                "updated_at": now,
            }
        ]
    )
    return {"decision_id": decision_id, "signal_id": signal_id, "ticker": signal["ticker"]}


def _link_watcher_records(
    store: SQLiteScanStore, materialized: list[dict[str, Any]], watcher: dict[str, Any], *, now: str
) -> None:
    positions = {
        str(row.get("signal_id") or ""): row for row in watcher.get("paper_positions") or []
    }
    intents = {str(row.get("signal_id") or ""): row for row in watcher.get("intents") or []}
    rows = []
    for item in materialized:
        signal_id = item["signal_id"]
        rows.append(
            {
                "decision_id": item["decision_id"],
                "signal_id": signal_id,
                "paper_intent_id": (intents.get(signal_id) or {}).get("intent_id") or "",
                "position_id": (positions.get(signal_id) or {}).get("position_id") or "",
                "cohort": SCENARIO_FORWARD_COHORT,
                "strategy_id": SCENARIO_STRATEGY_ID,
                "strategy_version": SCENARIO_POLICY_VERSION,
                "created_at": now,
                "updated_at": now,
            }
        )
    store.upsert_scenario_signal_links(rows)
    for row in rows:
        _record_scenario_event(
            store,
            decision_id=str(row["decision_id"]),
            event_type="PAPER_LIFECYCLE_LINKED",
            event_timestamp=now,
            payload={
                "signal_id": row.get("signal_id"),
                "paper_intent_id": row.get("paper_intent_id"),
                "position_id": row.get("position_id"),
            },
        )


def _replay_price_context(
    bars: list[dict[str, Any]], *, event_time: datetime, quote: dict[str, Any] | None
) -> PriceContext | None:
    prior = [row for row in bars if _bar_time(row) < event_time and _valid_bar(row)]
    if not prior or not quote:
        return None
    sample = prior[-14:]
    last = sample[-1]
    ranges = [float(row["high"]) - float(row["low"]) for row in sample]
    return PriceContext(
        observed_at=str(last.get("timestamp") or ""),
        price=float(last["close"]),
        atr=sum(ranges) / len(ranges) if ranges else None,
        spread_pct=float(quote.get("spread_pct") or 0.0),
        liquid=float(last.get("volume") or 0.0) * float(last["close"]) >= 20_000,
        source_bar_hash_sha256=canonical_hash(sample),
        source_kind="historical_minute_bars_and_quote",
    )


def _simulate_replay_trade(
    *,
    decision: dict[str, Any],
    article: ScenarioNewsArticle,
    bars: list[dict[str, Any]],
    event_time: datetime,
    slippage_bps: float,
    quote: dict[str, Any] | None,
) -> dict[str, Any]:
    session_end = _regular_session_end(event_time)
    future = [
        row
        for row in bars
        if event_time < _bar_time(row) <= session_end and _valid_bar(row)
    ]
    future.sort(key=_bar_time)
    trade_id = canonical_hash(
        {"decision": decision["decision_id"], "policy": SCENARIO_POLICY_VERSION}
    )[:32]
    base = {
        "replay_trade_id": trade_id,
        "decision_id": decision["decision_id"],
        "article_id": article.article_id,
        "ticker": decision["ticker"],
        "market_date": decision["market_date"],
        "source_bar_hash_sha256": canonical_hash(future),
        "source_quote_hash_sha256": canonical_hash(quote or {}),
        "quarantine_reason": "",
    }
    trigger = float(decision["entry_trigger"])
    stop = float(decision["invalidation_level"])
    target = float(decision["target_1"])
    entry_index = None
    entry_price = None
    for index, bar in enumerate(future):
        if float(bar["high"]) >= trigger:
            entry_index = index
            entry_price = max(trigger, float(bar["open"]))
            break
    if entry_index is None or entry_price is None:
        return {
            **base,
            "outcome_status": "not_triggered",
            "entry_at": "",
            "entry_price": None,
            "exit_at": "",
            "exit_price": None,
            "gross_return_pct": None,
            "modeled_after_cost_return_pct": None,
        }
    for bar in future[entry_index + 1 :]:
        hit_stop = float(bar["low"]) <= stop
        hit_target = float(bar["high"]) >= target
        if hit_stop and hit_target:
            return {
                **base,
                "outcome_status": "quarantined",
                "entry_at": str(future[entry_index]["timestamp"]),
                "entry_price": entry_price,
                "exit_at": str(bar["timestamp"]),
                "exit_price": None,
                "gross_return_pct": None,
                "modeled_after_cost_return_pct": None,
                "quarantine_reason": "same_bar_stop_and_target_ambiguous",
            }
        if hit_stop or hit_target:
            exit_price = stop if hit_stop else target
            return _completed_replay_trade(
                base,
                entry_at=str(future[entry_index]["timestamp"]),
                entry_price=entry_price,
                exit_at=str(bar["timestamp"]),
                exit_price=exit_price,
                exit_reason="stop" if hit_stop else "target",
                slippage_bps=slippage_bps,
            )
    if not future:
        return {
            **base,
            "outcome_status": "missing_bars",
            "entry_at": "",
            "entry_price": None,
            "exit_at": "",
            "exit_price": None,
            "gross_return_pct": None,
            "modeled_after_cost_return_pct": None,
        }
    final = future[-1]
    return _completed_replay_trade(
        base,
        entry_at=str(future[entry_index]["timestamp"]),
        entry_price=entry_price,
        exit_at=str(final["timestamp"]),
        exit_price=float(final["close"]),
        exit_reason="time_stop",
        slippage_bps=slippage_bps,
    )


def _completed_replay_trade(
    base: dict[str, Any],
    *,
    entry_at: str,
    entry_price: float,
    exit_at: str,
    exit_price: float,
    exit_reason: str,
    slippage_bps: float,
) -> dict[str, Any]:
    gross = ((exit_price - entry_price) / entry_price) * 100.0
    modeled_cost_pct = (float(slippage_bps) / 100.0) * 2.0
    return {
        **base,
        "outcome_status": "complete",
        "entry_at": entry_at,
        "entry_price": round(entry_price, 4),
        "exit_at": exit_at,
        "exit_price": round(exit_price, 4),
        "gross_return_pct": round(gross, 4),
        "modeled_after_cost_return_pct": round(gross - modeled_cost_pct, 4),
        "exit_reason": exit_reason,
        "cost_model": f"two paper fills at {slippage_bps} bps each",
    }


def _historical_replay_metrics(
    decisions: list[dict[str, Any]], trades: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_day[str(trade["market_date"])].append(trade)
    decision_counts = _counts(decisions, "market_date")
    output = []
    for date in sorted(set(decision_counts) | set(by_day)):
        rows = by_day.get(date, [])
        complete = [row for row in rows if row.get("outcome_status") == "complete"]
        quarantined = [row for row in rows if row.get("outcome_status") == "quarantined"]
        output.append(
            {
                "market_date": date,
                "cohort": "scenario_historical_replay",
                "strategy_id": SCENARIO_STRATEGY_ID,
                "policy_version": SCENARIO_POLICY_VERSION,
                "signal_count": decision_counts.get(date, 0),
                "triggered_count": sum(row.get("entry_price") is not None for row in rows),
                "closed_eligible_count": len(complete),
                "open_count": 0,
                "missing_count": sum(row.get("outcome_status") == "missing_bars" for row in rows),
                "quarantined_count": len(quarantined),
                "gross_return_pct": _mean([float(row["gross_return_pct"]) for row in complete]),
                "modeled_after_cost_return_pct": _mean(
                    [float(row["modeled_after_cost_return_pct"]) for row in complete]
                ),
                "benchmark_return_pct": None,
                "excess_return_pct": None,
                "hit_rate_pct": _pct(
                    sum(float(row["gross_return_pct"]) > 0 for row in complete), len(complete)
                ),
                "return_status": "historical_replay_not_forward",
                "timing_disclosure": (
                    "provider publication timestamps are proxies; do not compare to "
                    "forward paper results."
                ),
                "research_only": True,
            }
        )
    return output


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _bar_time(row: dict[str, Any]) -> datetime:
    return _parse_iso(str(row.get("timestamp") or "1970-01-01T00:00:00Z"))


def _regular_session_end(event_time: datetime) -> datetime:
    """Return the same-day 16:00 America/New_York session cutoff in UTC."""

    eastern = ZoneInfo("America/New_York")
    local = event_time.astimezone(eastern)
    return local.replace(hour=16, minute=0, second=0, microsecond=0).astimezone(UTC)


def _register_scenario_policy(store: SQLiteScanStore, config: ScannerConfig) -> None:
    store.upsert_scenario_model_registry(
        {
            "model_id": f"{SCENARIO_POLICY_VERSION}:{config.scenario_openai_model}",
            "created_at": utc_now_iso(),
            "policy_version": SCENARIO_POLICY_VERSION,
            "feature_schema_version": SCENARIO_FEATURE_SCHEMA_VERSION,
            "calibration_status": "UNCALIBRATED",
            "sample_count": 0,
            "promotion_state": "research_only",
            "extractor_model": config.scenario_openai_model,
            "broker_execution_enabled": False,
        }
    )


def _record_scenario_event(
    store: SQLiteScanStore,
    *,
    decision_id: str,
    event_type: str,
    event_timestamp: str,
    payload: dict[str, Any],
) -> None:
    store.persist_scenario_events(
        [
            {
                "event_id": canonical_hash(
                    {
                        "decision_id": decision_id,
                        "event_type": event_type,
                        "event_timestamp": event_timestamp,
                        "payload": payload,
                    }
                )[:32],
                "decision_id": decision_id,
                "event_type": event_type,
                "event_timestamp": event_timestamp,
                "payload_json": payload,
            }
        ]
    )


def _valid_bar(row: dict[str, Any]) -> bool:
    return all(_positive(row.get(field)) for field in ("open", "high", "low", "close"))


def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    output: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        output[value] = output.get(value, 0) + 1
    return output


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _pct(numerator: int, denominator: int) -> float | None:
    return round((numerator / denominator) * 100.0, 2) if denominator else None


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False
