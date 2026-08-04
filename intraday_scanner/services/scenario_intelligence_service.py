"""Orchestrate sourced news facts through the governed paper-trade lifecycle."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
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
from intraday_scanner.scenario.point_in_time import (
    completed_minute_bar_at,
    parse_aware_timestamp,
)
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
    notify: str = "console",
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
                notify=notify,
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
    *,
    db_path: str | Path,
    market_date: str | None = None,
    config: ScannerConfig | None = None,
    market_provider: AlpacaProvider | None = None,
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
    benchmark_by_position = _forward_benchmarks(
        positions=closed,
        config=config,
        market_provider=market_provider,
    )
    benchmark_returns: list[float] = []
    after_cost_excess_returns: list[float] = []
    outcome_rows: list[dict[str, Any]] = []
    for position in closed:
        signal_id = str(position.get("signal_id") or "")
        cost_pct = sum(
            float(fill.get("slippage_bps") or 0.0) / 100.0 for fill in fills_by_signal[signal_id]
        )
        after_costs.append(float(position["realized_return_pct"]) - cost_pct)
        benchmark = benchmark_by_position.get(str(position.get("position_id") or ""))
        if benchmark is not None:
            benchmark_returns.append(benchmark["return_pct"])
            after_cost_excess_returns.append(after_costs[-1] - benchmark["return_pct"])
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
                    "benchmark_return_pct": benchmark["return_pct"] if benchmark else None,
                    "benchmark_source_bar_hash_sha256": (
                        benchmark["source_bar_hash_sha256"] if benchmark else ""
                    ),
                    "benchmark_status": "sourced" if benchmark else "missing_source_bars",
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
        "benchmark_return_pct": _mean(benchmark_returns),
        "excess_return_pct": _mean(after_cost_excess_returns),
        "hit_rate_pct": _pct(
            sum(float(row["realized_return_pct"]) > 0 for row in closed), len(closed)
        ),
        "return_status": "complete" if closed else "no_closed_eligible_positions",
        "cost_model": "sum of recorded paper-fill slippage_bps; no unstated fees",
        "benchmark": {
            "ticker": "SPY",
            "source": "alpaca_minute_bars",
            "eligible_closed_count": len(benchmark_returns),
            "missing_closed_count": len(closed) - len(benchmark_returns),
            "status": (
                "sourced_complete"
                if closed and len(benchmark_returns) == len(closed)
                else "partial_source_coverage"
                if benchmark_returns
                else "missing_source_bars"
            ),
        },
        "return_distribution": _return_distribution(after_costs),
        "mae_mfe": _position_excursions(store, closed),
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
    notify: str = "console",
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
        notify=notify,
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
            try:
                benchmark_bars = market.get_minute_bars(
                    ["SPY"], window_start, window_end, config
                )
            except DataProviderError:
                benchmark_bars = []
            quote = market.get_first_quote_after(
                ticker,
                start=article.created_at,
                end=(event_time + timedelta(minutes=5)).isoformat(),
                config=config,
            )
            context = _replay_price_context(bars, event_time=event_time, quote=quote)
            decision_at = context.bar_completed_at if context is not None else article.created_at
            decision = evaluate_scenario(
                article=article,
                extraction=extraction,
                ticker=ticker,
                decision_at=decision_at,
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
                event_timestamp=decision_at,
                payload={"action": decision.action, "cohort": "scenario_historical_replay"},
            )
            if decision.action != "ENTER_LONG":
                continue
            outcome = _simulate_replay_trade(
                decision=payload,
                article=article,
                bars=bars,
                event_time=_parse_iso(decision_at),
                slippage_bps=config.slippage_bps,
                quote=quote,
            )
            outcome = _with_replay_benchmark(outcome, benchmark_bars)
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
    links_by_decision = {
        str(row.get("decision_id") or ""): row
        for row in store.load_scenario_signal_links(limit=limit)
    }
    positions_by_signal = {
        str(row.get("signal_id") or ""): row
        for row in store.load_paper_positions(limit=50_000)
        if (
            str(row.get("strategy_id") or "") == SCENARIO_STRATEGY_ID
            and str(row.get("cohort") or "") == SCENARIO_FORWARD_COHORT
        )
        or str(row.get("signal_id") or "").startswith("scenario:")
    }
    fills_by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fill in store.load_paper_trade_fills(limit=50_000):
        fills_by_signal[str(fill.get("signal_id") or "")].append(fill)
    events_by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in store.load_signal_events(limit=50_000):
        events_by_signal[str(event.get("signal_id") or "")].append(event)
    records = []
    for row in decisions:
        article = news_by_id.get(str(row.get("article_id") or ""), {})
        link = links_by_decision.get(str(row.get("decision_id") or ""), {})
        signal_id = str(link.get("signal_id") or "")
        position = positions_by_signal.get(signal_id, {})
        fills = fills_by_signal.get(signal_id, [])
        lifecycle = [
            {
                "event_type": event.get("event_type"),
                "event_timestamp": event.get("event_timestamp"),
                "event_price": event.get("event_price"),
                "notes": event.get("notes") or "",
            }
            for event in events_by_signal.get(signal_id, [])
            if str(event.get("event_type") or "")
            in {"ENTRY_SIGNAL", "EXIT_SIGNAL", "INVALIDATED", "CLOSED"}
        ]
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
                "paper_lifecycle": {
                    "signal_id": signal_id,
                    "paper_intent_id": link.get("paper_intent_id") or "",
                    "position_id": position.get("position_id") or link.get("position_id") or "",
                    "status": position.get("status") or "NOT_TRIGGERED",
                    "opened_at": position.get("opened_at"),
                    "closed_at": position.get("closed_at"),
                    "entry_price": position.get("entry_price"),
                    "exit_price": position.get("exit_price"),
                    "realized_return_pct": position.get("realized_return_pct"),
                    "modeled_cost_pct": round(
                        sum(float(fill.get("slippage_bps") or 0.0) / 100.0 for fill in fills),
                        4,
                    )
                    if fills
                    else None,
                    "events": lifecycle,
                },
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
        "analytics": {
            "forward": _performance_analytics(performance),
            "historical_replay": _performance_analytics(replay_performance),
        },
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


def _performance_analytics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = sum(int(row.get("closed_eligible_count") or 0) for row in rows)
    returns = [
        (float(row["modeled_after_cost_return_pct"]), int(row.get("closed_eligible_count") or 0))
        for row in rows
        if row.get("modeled_after_cost_return_pct") is not None
        and int(row.get("closed_eligible_count") or 0) > 0
    ]
    benchmark_returns = [
        (float(row["benchmark_return_pct"]), int(row.get("closed_eligible_count") or 0))
        for row in rows
        if row.get("benchmark_return_pct") is not None
        and int(row.get("closed_eligible_count") or 0) > 0
    ]
    excess_returns = [
        (float(row["excess_return_pct"]), int(row.get("closed_eligible_count") or 0))
        for row in rows
        if row.get("excess_return_pct") is not None
        and int(row.get("closed_eligible_count") or 0) > 0
    ]
    return {
        "day_count": len(rows),
        "signal_count": sum(int(row.get("signal_count") or 0) for row in rows),
        "triggered_count": sum(int(row.get("triggered_count") or 0) for row in rows),
        "closed_eligible_count": closed,
        "open_count": sum(int(row.get("open_count") or 0) for row in rows),
        "missing_count": sum(int(row.get("missing_count") or 0) for row in rows),
        "quarantined_count": sum(int(row.get("quarantined_count") or 0) for row in rows),
        "after_cost_return_pct": _weighted_mean(returns),
        "benchmark_return_pct": _weighted_mean(benchmark_returns),
        "excess_return_pct": _weighted_mean(excess_returns),
        "status": "evaluable" if closed else "no_closed_eligible_positions",
        "aggregation_note": (
            "Closed-position-weighted daily means; missing, open, and quarantined rows remain "
            "outside reported returns."
        ),
    }


def _weighted_mean(values: list[tuple[float, int]]) -> float | None:
    weight = sum(item[1] for item in values)
    if not weight:
        return None
    return round(sum(value * count for value, count in values) / weight, 4)


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
                "prompt_injection_detected": bool(payload.get("prompt_injection_detected")),
                "contradictions": payload.get("contradictions") or [],
                "dependencies": payload.get("dependencies") or [],
                "unresolved_unknowns": payload.get("unresolved_unknowns") or [],
            },
            model=str(payload.get("model") or ""),
            requested_model=str(payload.get("requested_model") or config.scenario_openai_model),
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
    cutoff = parse_aware_timestamp(as_of)
    if cutoff is None:
        return {}
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
        rows = []
        for row in by_ticker[ticker]:
            observed = parse_aware_timestamp(str(row.get("timestamp") or ""))
            completed = completed_minute_bar_at(observed) if observed is not None else None
            if completed is not None and completed <= cutoff and _valid_bar(row):
                rows.append(row)
        rows.sort(key=lambda row: str(row.get("timestamp") or ""))
        snapshot = snapshots.get(ticker)
        if not rows or snapshot is None:
            continue
        snapshot_at = parse_aware_timestamp(str(snapshot.as_of_timestamp or ""))
        if snapshot_at is None or snapshot_at > cutoff:
            continue
        last = rows[-1]
        sample = rows[-14:]
        closes = [float(row["close"]) for row in sample if _positive(row.get("close"))]
        ranges = [
            float(row["high"]) - float(row["low"])
            for row in sample
            if _positive(row.get("high")) and _positive(row.get("low"))
        ]
        atr = sum(ranges) / len(ranges) if ranges else None
        observed_at = str(last.get("timestamp") or "")
        completed_at = completed_minute_bar_at(observed_at)
        if completed_at is None:
            continue
        output[ticker] = PriceContext(
            observed_at=observed_at,
            price=closes[-1] if closes else None,
            atr=atr,
            spread_pct=float(snapshot.spread_pct) if snapshot.spread_pct is not None else None,
            liquid=bool(
                float(last.get("volume") or 0.0) * float(last.get("close") or 0.0) >= 20_000
            ),
            source_bar_hash_sha256=canonical_hash(
                {
                    "bars": sample,
                    "snapshot": {
                        "as_of_timestamp": snapshot.as_of_timestamp,
                        "spread_pct": snapshot.spread_pct,
                    },
                }
            ),
            bar_completed_at=completed_at.isoformat().replace("+00:00", "Z"),
            is_complete=True,
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
    if not quote:
        return None
    quote_at = parse_aware_timestamp(str(quote.get("timestamp") or ""))
    if quote_at is None or quote_at < event_time:
        return None
    completed_rows: list[tuple[datetime, dict[str, Any]]] = []
    for row in bars:
        if not _valid_bar(row):
            continue
        completed_at = completed_minute_bar_at(_bar_time(row))
        if completed_at is not None:
            completed_rows.append((completed_at, row))
    completed_rows.sort(key=lambda item: item[0])
    decision_bar = next(
        (
            (completed_at, row)
            for completed_at, row in completed_rows
            if completed_at > event_time and quote_at <= completed_at
        ),
        None,
    )
    if decision_bar is None:
        return None
    decision_at, last = decision_bar
    sample = [row for completed_at, row in completed_rows if completed_at <= decision_at][-14:]
    ranges = [float(row["high"]) - float(row["low"]) for row in sample]
    spread_pct = _nonnegative_float(quote.get("spread_pct"))
    return PriceContext(
        observed_at=str(last.get("timestamp") or ""),
        price=float(last["close"]),
        atr=sum(ranges) / len(ranges) if ranges else None,
        spread_pct=spread_pct,
        liquid=float(last.get("volume") or 0.0) * float(last["close"]) >= 20_000,
        source_bar_hash_sha256=canonical_hash({"bars": sample, "quote": quote}),
        bar_completed_at=decision_at.isoformat().replace("+00:00", "Z"),
        is_complete=True,
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


def _with_replay_benchmark(
    outcome: dict[str, Any], benchmark_bars: list[dict[str, Any]]
) -> dict[str, Any]:
    if outcome.get("outcome_status") != "complete":
        return outcome | {
            "benchmark_return_pct": None,
            "after_cost_excess_return_pct": None,
            "benchmark_source_bar_hash_sha256": "",
        }
    benchmark = _benchmark_return_from_bars(
        benchmark_bars,
        entry_at=str(outcome.get("entry_at") or ""),
        exit_at=str(outcome.get("exit_at") or ""),
    )
    if benchmark is None:
        return outcome | {
            "benchmark_return_pct": None,
            "after_cost_excess_return_pct": None,
            "benchmark_source_bar_hash_sha256": "",
        }
    after_costs = float(outcome["modeled_after_cost_return_pct"])
    return outcome | {
        "benchmark_return_pct": benchmark["return_pct"],
        "after_cost_excess_return_pct": round(after_costs - benchmark["return_pct"], 4),
        "benchmark_source_bar_hash_sha256": benchmark["source_bar_hash_sha256"],
    }


def _forward_benchmarks(
    *,
    positions: list[dict[str, Any]],
    config: ScannerConfig | None,
    market_provider: AlpacaProvider | None,
) -> dict[str, dict[str, Any]]:
    if not positions:
        return {}
    resolved_config = config
    if resolved_config is None:
        try:
            resolved_config = load_config()
        except Exception:
            return {}
    if not resolved_config.alpaca_api_key_id or not resolved_config.alpaca_api_secret_key:
        return {}
    starts = [str(row.get("opened_at") or "") for row in positions if row.get("opened_at")]
    ends = [str(row.get("closed_at") or "") for row in positions if row.get("closed_at")]
    if not starts or not ends:
        return {}
    try:
        bars = (market_provider or AlpacaProvider(resolved_config)).get_minute_bars(
            ["SPY"], min(starts), max(ends), resolved_config
        )
    except (DataProviderError, OSError, ValueError):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for position in positions:
        benchmark = _benchmark_return_from_bars(
            bars,
            entry_at=str(position.get("opened_at") or ""),
            exit_at=str(position.get("closed_at") or ""),
        )
        if benchmark is not None and position.get("position_id"):
            output[str(position["position_id"])] = benchmark
    return output


def _benchmark_return_from_bars(
    bars: list[dict[str, Any]], *, entry_at: str, exit_at: str
) -> dict[str, Any] | None:
    if not entry_at or not exit_at:
        return None
    try:
        entry_time = _parse_iso(entry_at)
        exit_time = _parse_iso(exit_at)
    except ValueError:
        return None
    if exit_time <= entry_time:
        return None
    valid = sorted((row for row in bars if _valid_bar(row)), key=_bar_time)
    entry = next((row for row in valid if _bar_time(row) >= entry_time), None)
    exit_row = next((row for row in reversed(valid) if _bar_time(row) <= exit_time), None)
    if entry is None or exit_row is None:
        return None
    entry_price = float(entry["close"])
    exit_price = float(exit_row["close"])
    if entry_price <= 0:
        return None
    return {
        "return_pct": round(((exit_price - entry_price) / entry_price) * 100.0, 4),
        "source_bar_hash_sha256": canonical_hash(
            {"entry": entry, "exit": exit_row, "ticker": "SPY"}
        ),
    }


def _return_distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "median_after_cost_return_pct": None,
            "after_cost_expectancy_pct": None,
            "profit_factor": None,
            "maximum_drawdown_pct": None,
            "bootstrap_95_ci_pct": {"lower": None, "upper": None},
        }
    positive = sum(value for value in values if value > 0)
    negative = abs(sum(value for value in values if value < 0))
    return {
        "median_after_cost_return_pct": round(float(median(values)), 4),
        "after_cost_expectancy_pct": _mean(values),
        "profit_factor": round(positive / negative, 4) if negative else None,
        "maximum_drawdown_pct": _maximum_drawdown(values),
        "bootstrap_95_ci_pct": _deterministic_bootstrap_ci(values),
    }


def _maximum_drawdown(values: list[float]) -> float:
    equity = 1.0
    high = equity
    worst = 0.0
    for value in values:
        equity *= max(0.0, 1.0 + value / 100.0)
        high = max(high, equity)
        worst = min(worst, (equity / high - 1.0) * 100.0 if high else -100.0)
    return round(worst, 4)


def _deterministic_bootstrap_ci(values: list[float]) -> dict[str, float | None]:
    if len(values) < 2:
        return {"lower": None, "upper": None}
    # Deterministic resampling makes the published interval reproducible from the same ledger.
    samples = []
    count = len(values)
    for seed in range(200):
        indexes = [((seed * 73) + (index * 37) + 11) % count for index in range(count)]
        samples.append(sum(values[index] for index in indexes) / count)
    samples.sort()
    return {
        "lower": round(samples[int((len(samples) - 1) * 0.025)], 4),
        "upper": round(samples[int((len(samples) - 1) * 0.975)], 4),
    }


def _position_excursions(store: SQLiteScanStore, positions: list[dict[str, Any]]) -> dict[str, Any]:
    maes: list[float] = []
    mfes: list[float] = []
    for position in positions:
        entry = _positive_float(position.get("entry_price"))
        signal_id = str(position.get("signal_id") or "")
        if entry is None or not signal_id:
            continue
        observations = store.load_price_observations(signal_id=signal_id, usable_only=True)
        prices = [_positive_float(row.get("price")) for row in observations]
        valid = [price for price in prices if price is not None]
        if not valid:
            continue
        maes.append(round(((min(valid) - entry) / entry) * 100.0, 4))
        mfes.append(round(((max(valid) - entry) / entry) * 100.0, 4))
    return {
        "eligible_closed_count": len(maes),
        "mean_mae_pct": _mean(maes),
        "mean_mfe_pct": _mean(mfes),
        "status": "sourced_price_observations" if maes else "missing_price_observations",
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
        after_costs = [float(row["modeled_after_cost_return_pct"]) for row in complete]
        benchmarks = [
            float(row["benchmark_return_pct"])
            for row in complete
            if row.get("benchmark_return_pct") is not None
        ]
        excess_returns = [
            float(row["after_cost_excess_return_pct"])
            for row in complete
            if row.get("after_cost_excess_return_pct") is not None
        ]
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
                "modeled_after_cost_return_pct": _mean(after_costs),
                "benchmark_return_pct": _mean(benchmarks),
                "excess_return_pct": _mean(excess_returns),
                "hit_rate_pct": _pct(
                    sum(float(row["gross_return_pct"]) > 0 for row in complete), len(complete)
                ),
                "return_status": "historical_replay_not_forward",
                "timing_disclosure": (
                    "provider publication timestamps are proxies; do not compare to "
                    "forward paper results."
                ),
                "benchmark": {
                    "ticker": "SPY",
                    "source": "alpaca_minute_bars",
                    "eligible_closed_count": len(benchmarks),
                    "missing_closed_count": len(complete) - len(benchmarks),
                    "status": (
                        "sourced_complete"
                        if complete and len(benchmarks) == len(complete)
                        else "partial_source_coverage"
                        if benchmarks
                        else "missing_source_bars"
                    ),
                },
                "return_distribution": _return_distribution(after_costs),
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


def _positive_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None
