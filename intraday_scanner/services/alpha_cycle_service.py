"""AlphaOps v4 orchestration services."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.ai.strategy_gap_resolver import StrategyGapResolver
from intraday_scanner.alpha.alert_gate import apply_alert_gates
from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION, AlphaModel
from intraday_scanner.alpha.feature_factory import build_feature_vector
from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.regime_detector import detect_regime
from intraday_scanner.alpha.risk_governor import evaluate_risk
from intraday_scanner.alpha.run_contracts import AlphaRunContract, build_alpha_run_contract
from intraday_scanner.alpha.v5_policy import alphaops_strategy_contract
from intraday_scanner.alpha.v6.decision_ledger import build_candidate_decisions
from intraday_scanner.config import load_config
from intraday_scanner.dashboard.operator_data_service import calculate_missing_outcome_status
from intraday_scanner.errors import DataProviderError, SnapshotValidationError
from intraday_scanner.market_calendar import (
    MarketSessionDecision,
    core_session_phase,
    session_for_timestamp,
)
from intraday_scanner.models import utc_now_iso
from intraday_scanner.notifiers import (
    BaseNotifier,
    ConsoleNotifier,
    NotificationEvent,
    build_notifiers,
    dispatch_events,
)
from intraday_scanner.notifiers.telegram_formatter import (
    format_alpha_monitor,
    format_alpha_no_trade,
    format_alpha_summary,
    format_alpha_watch,
)
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.providers.sec_edgar_provider import (
    collect_sec_risk,
    enrich_rows_with_sec_risk,
)
from intraday_scanner.providers.web_source_base import get_source, load_web_sources_config
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.services.alpha_official_cohort_service import (
    build_official_cohort_row,
)
from intraday_scanner.services.alpha_v6_universe_service import (
    active_alpha_v6_membership_by_ticker,
    register_alpha_v6_universe,
)
from intraday_scanner.services.candidate_news_service import enrich_candidate_news
from intraday_scanner.services.learning_service import (
    load_production_alpha_learning_labels,
    run_alpha_learning,
)
from intraday_scanner.services.premarket_enrichment_service import enrich_premarket_rows
from intraday_scanner.services.price_observation_service import collect_price_observations
from intraday_scanner.services.return_attribution_service import (
    record_alpha_historical_signals,
    record_monitor_signal_events,
    record_no_trade_historical_signal,
)
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.services.signal_review_service import (
    monitor_alpha_signals,
    review_alpha_signals,
)
from intraday_scanner.services.source_reliability_service import build_source_reliability
from intraday_scanner.services.strategy_decision_service import StrategyDecisionService
from intraday_scanner.services.web_collection_service import web_auto_collect, web_source_doctor
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

DEFAULT_DB_PATH = "data/shadow_real.sqlite"
DEFAULT_WEB_CONFIG = "config/web_sources.yaml"
LEGACY_ALPHAOPS_STRATEGY_ID = "alphaops_v4"
# Public compatibility name for pre-V5 evidence/recovery callers.
ALPHAOPS_STRATEGY_ID = LEGACY_ALPHAOPS_STRATEGY_ID
ALPHAOPS_OFFICIAL_COHORT = "official_telegram"
ALPHAOPS_RADAR_COHORT = "research_radar"
ALPHAOPS_RADAR_VERSION = "dawnstrike-research-radar-v1"
_LEGACY_PICK_PATTERN = re.compile(
    r"^\s*\d+\)\s+([A-Z][A-Z0-9.-]{0,11})\s+-\s+Opportunity\b",
    re.MULTILINE,
)
_LEGACY_PICK_COUNT_PATTERN = re.compile(
    r"\|\s*(\d+)\s+(?:picks?|names?)\s*\|",
    re.IGNORECASE,
)


def alpha_morning(
    *,
    config_path: str | Path = DEFAULT_WEB_CONFIG,
    db_path: str | Path = DEFAULT_DB_PATH,
    out_dir: str | Path = "outputs/alpha_morning",
    notify: str = "console",
    dry_run: bool = False,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    return alpha_cycle(
        config_path=config_path,
        db_path=db_path,
        out_dir=out_dir,
        notify=notify,
        dry_run=dry_run,
        cycle_name="alpha_morning",
        as_of=as_of,
    )


def alpha_cycle(
    *,
    config_path: str | Path = DEFAULT_WEB_CONFIG,
    db_path: str | Path = DEFAULT_DB_PATH,
    out_dir: str | Path = "outputs/alpha_cycle",
    notify: str = "console",
    dry_run: bool = False,
    cycle_name: str = "alpha_cycle",
    as_of: datetime | None = None,
) -> dict[str, Any]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    session_gate = _scheduled_session_gate(notify=notify, as_of=as_of)
    if session_gate is not None:
        phase = core_session_phase(as_of)
        skipped_result: dict[str, Any] | None = None
        if not session_gate.is_trading_day:
            skipped_result = _session_skip_result(
                run_type=cycle_name,
                status="skipped_market_closed",
                decision=session_gate,
            )
        elif phase != "before_core_session":
            skipped_result = _session_skip_result(
                run_type=cycle_name,
                status="skipped_outside_premarket_session",
                decision=session_gate,
                phase=phase,
            )
    else:
        skipped_result = None
    if skipped_result is not None:
        skipped_result["out_dir"] = str(output_dir)
        _write_json(output_dir / "alpha_session_gate.json", skipped_result)
        return skipped_result
    store = SQLiteScanStore(db_path)
    store.initialize()
    collection = web_auto_collect(
        config_path=config_path,
        db_path=db_path,
        out_dir=output_dir / "web_collect",
        persist=True,
        print_rows=False,
    )
    source_summary = dict(collection.get("source_summary") or {})
    source_reliability = build_source_reliability(
        source_summary,
        outcomes=load_production_alpha_learning_labels(store),
        previous=store.load_alpha_source_reliability(),
    )
    if source_reliability:
        store.persist_alpha_source_reliability(source_reliability)

    if collection.get("status") != "success":
        review = review_alpha_signals([], source_summary=source_summary)
        no_data_scan_id = f"{cycle_name}:source_failure:{utc_now_iso()[:10]}"
        no_data_generated_at = utc_now_iso()
        no_data_no_trade_row = record_no_trade_historical_signal(
            store,
            scan_id=no_data_scan_id,
            generated_at=no_data_generated_at,
            reason=str(review["decision"]["reason"]),
            source_summary=source_summary,
            candidate_count=int(source_summary.get("candidate_count") or 0),
        )
        message = format_alpha_no_trade(
            reason=str(review["decision"]["reason"]),
            next_action=str(review["decision"]["next_action"]),
        )
        events = [
            _official_selection_notification_event(
                no_data_scan_id,
                "alpha_no_trade",
                "Dawnstrike Alpha Check",
                message,
                selected_signals=[],
            )
        ]
        selected_rows, selection_stats = _persist_official_selections(
            store,
            scan_id=no_data_scan_id,
            selected_signals=[no_data_no_trade_row],
            decision=dict(review["decision"]),
            selected_at=no_data_generated_at,
            event=events[0],
        )
        preexisting_notification_keys = _existing_notification_keys(
            store,
            events=events,
            notify=notify,
        )
        source_contract_args: dict[str, Any] = {
            "scan_id": no_data_scan_id,
            "generated_at": no_data_generated_at,
            "ranked_count": 0,
            "signals": [],
            "review": review,
            "source_summary": source_summary,
            "enrichment_summary": None,
        }
        _persist_run_contract(
            output_dir,
            **source_contract_args,
            notification_stats={},
            notification_channel=notify,
            notification_dry_run=dry_run,
            notification_status_override="pending",
        )
        try:
            notification_stats = _dispatch(
                events,
                notify=notify,
                db_path=db_path,
                dry_run=dry_run,
            )
        except Exception:
            _persist_notification_delivery_memberships(
                store,
                selections=selected_rows,
                events=events,
                notify=notify,
                preexisting_notification_keys=preexisting_notification_keys,
            )
            _persist_run_contract(
                output_dir,
                **source_contract_args,
                notification_stats={},
                notification_channel=notify,
                notification_dry_run=dry_run,
                notification_status_override="delivery_failed",
            )
            raise
        notification_deliveries = _persist_notification_delivery_memberships(
            store,
            selections=selected_rows,
            events=events,
            notify=notify,
            preexisting_notification_keys=preexisting_notification_keys,
        )
        run_contract = _persist_run_contract(
            output_dir,
            **source_contract_args,
            notification_stats=notification_stats,
            notification_channel=notify,
            notification_dry_run=dry_run,
        )
        _link_notification_events(
            store,
            scan_id=no_data_scan_id,
            events=events,
            notify=notify,
            dry_run=dry_run,
            signal_ids=[str(no_data_no_trade_row["signal_id"])],
            notification_deliveries=notification_deliveries,
        )
        no_data_result: dict[str, Any] = {
            "status": "no_trade",
            "run_type": cycle_name,
            "scan_id": no_data_scan_id,
            "source_summary": source_summary,
            "review": review,
            "selection_stats": selection_stats,
            "notification_stats": notification_stats,
            "notification_deliveries": notification_deliveries,
            "run_contract": run_contract.to_dict(),
            "out_dir": str(output_dir),
        }
        if session_gate is not None:
            no_data_result["session_gate"] = session_gate.to_dict()
        _write_json(output_dir / "alpha_cycle.json", no_data_result)
        return no_data_result

    scanner_config = load_config(
        provider="csv",
        output_dir=output_dir / "scan",
        database_path=Path(db_path),
    )
    source_config = load_web_sources_config(config_path)
    fixture_mode = any(
        source.enabled and bool(source.fixture_path) for source in source_config.sources
    )
    if not fixture_mode:
        scanner_config = _alphaops_scanner_config(scanner_config)
    enrichment = enrich_premarket_rows(
        list(collection.get("rows") or []),
        config=scanner_config,
        source=("yahoo" if fixture_mode else "alpaca"),
        allow_yahoo_fallback=not fixture_mode,
        rehearsal_mode=fixture_mode,
        out_dir=output_dir / "premarket_enrichment",
    )
    source_summary["premarket_enrichment"] = enrichment["summary"]
    news_enrichment = enrich_candidate_news(
        list(enrichment.get("ranking_rows") or []),
        config=scanner_config,
        requested_at=as_of,
        max_symbols=scanner_config.premarket_enrichment_max_candidates,
        rehearsal_mode=fixture_mode,
        out_dir=output_dir / "candidate_news",
    )
    source_summary["candidate_news"] = dict(news_enrichment["summary"])
    enriched_snapshot_path = str(
        news_enrichment.get("snapshot_path")
        or dict(enrichment.get("paths") or {}).get("snapshot")
        or collection["snapshot_path"]
    )
    scan_result = ScanService(
        CsvSnapshotProvider(enriched_snapshot_path),
        store=store,
    ).run(scanner_config, persist=True)
    scan_paths = write_scan_outputs(scan_result, scanner_config.output_dir)
    ranked = [candidate.to_dict() for candidate in scan_result.ranked_candidates]
    all_candidates = [candidate.to_dict() for candidate in scan_result.all_candidates]
    timestamp = scan_result.created_at
    ranked, ranked_sec_summary = _verify_ranked_sec_safety(
        ranked,
        source_config=source_config,
        store=store,
        out_dir=output_dir / "ranked_sec_safety",
        as_of=timestamp,
        rehearsal_mode=fixture_mode,
    )
    source_summary["ranked_sec_safety"] = ranked_sec_summary
    all_candidates = _merge_ranked_safety(all_candidates, ranked)
    reliability_by_source = {row["source"]: row for row in source_reliability}
    feature_vectors = [
        build_feature_vector(
            row,
            scan_id=scan_result.run_id,
            timestamp=timestamp,
            source_summary=source_summary,
            source_reliability=reliability_by_source,
        )
        for row in all_candidates
    ]
    store.persist_alpha_feature_vectors(feature_vectors)
    historical_labels = load_production_alpha_learning_labels(store)
    model = AlphaModel()
    signals = model.score_candidates(
        ranked,
        feature_vectors,
        historical_outcomes=historical_labels,
        setup_memory=store.load_alpha_setup_memory(),
        real_shadow_days=_real_days(historical_labels),
    )
    signals = [
        _signal_payload(row, scan_result.run_id, timestamp, index)
        for index, row in enumerate(signals, 1)
    ]
    strategy_receipt_stats = _apply_strategy_decision_receipts(
        signals,
        store=store,
        config=scanner_config,
        decision_at=timestamp,
        source_summary=source_summary,
    )
    if scanner_config.strategy_evidence_enabled:
        signals = _apply_receipt_risk_gates(signals, feature_vectors)
    signals = apply_alert_gates(signals)
    review = review_alpha_signals(signals, source_summary=source_summary)
    decision = dict(review["decision"])
    research_radar = _research_radar(signals) if decision.get("no_trade") else []
    signals = _annotate_research_radar(signals, research_radar)
    store.persist_alpha_signals(signals)
    regime = detect_regime(signals, source_summary)
    v6_universe_registration = _register_alpaca_screening_universe(
        store,
        source_summary=source_summary,
        market_date=timestamp[:10],
    )
    universe_memberships = active_alpha_v6_membership_by_ticker(
        store,
        market_date=timestamp[:10],
        tickers=[str(row.get("ticker") or "") for row in all_candidates],
    )
    candidate_tickers = {
        str(row.get("ticker") or "").upper() for row in all_candidates if row.get("ticker")
    }
    missing_v6_universe_memberships = sorted(candidate_tickers - set(universe_memberships))
    if (
        isinstance(source_summary.get("production_contract"), dict)
        and source_summary["production_contract"].get("status") == "READY"
        and missing_v6_universe_memberships
    ):
        raise SnapshotValidationError(
            "Production V6 universe coverage is incomplete; refusing to create "
            "shadow decisions without point-in-time membership. Missing: "
            + ", ".join(missing_v6_universe_memberships[:20])
        )
    v6_model_runs = store.load_alpha_v6_model_runs(limit=1)
    v6_decisions = build_candidate_decisions(
        signals=signals,
        candidates=all_candidates,
        feature_vectors=feature_vectors,
        source_summary=source_summary,
        regime=regime,
        prior_outcomes=store.load_alpha_v6_outcomes(),
        frozen_model_run=v6_model_runs[0] if v6_model_runs else None,
        decision_at=timestamp,
        scan_id=scan_result.run_id,
        universe_membership_by_ticker=universe_memberships,
    )
    v6_decision_stats = store.persist_alpha_v6_decisions(v6_decisions)
    historical_rows = record_alpha_historical_signals(
        store,
        signals,
        source_summary=source_summary,
        no_trade_reason=str(decision.get("reason") or "") if decision.get("no_trade") else "",
    )
    no_trade_row: dict[str, Any] | None = None
    if decision.get("no_trade"):
        no_trade_row = record_no_trade_historical_signal(
            store,
            scan_id=scan_result.run_id,
            generated_at=timestamp,
            reason=str(decision.get("reason") or ""),
            source_summary=source_summary,
            candidate_count=len(ranked),
        )
    selected_signals = list(review["watchlist"])
    if decision.get("no_trade"):
        message = format_alpha_no_trade(
            reason=str(decision.get("reason") or ""),
            next_action=str(decision.get("next_action") or ""),
            research_signals=research_radar,
        )
        hint = "alpha_no_trade"
        title = "Dawnstrike Alpha Check"
    else:
        edge_label = (
            _trust_gate_edge_label(list(review["watchlist"]))
            if str(decision.get("decision_tier") or "") == "probability_fallback"
            else _edge_label(signals)
        )
        message = format_alpha_watch(
            signals=selected_signals,
            edge_label=edge_label,
            source_summary=source_summary,
            blocked_signals=list(review["blocked"]),
        )
        hint = "alpha_morning_watch"
        title = "Dawnstrike Alpha Watch"
    events = [
        _official_selection_notification_event(
            scan_result.run_id,
            hint,
            title,
            message,
            selected_signals=selected_signals,
            research_signals=research_radar,
        )
    ]
    selection_members = selected_signals or ([no_trade_row] if no_trade_row is not None else [])
    selected_rows, selection_stats = _persist_official_selections(
        store,
        scan_id=scan_result.run_id,
        selected_signals=selection_members,
        decision=decision,
        selected_at=timestamp,
        event=events[0],
    )
    radar_selection_rows, radar_selection_stats = _persist_research_radar_selections(
        store,
        scan_id=scan_result.run_id,
        radar=research_radar,
        selected_at=timestamp,
        event=events[0],
    )
    delivery_selection_rows = [*selected_rows, *radar_selection_rows]
    selected_signal_ids = [str(row["signal_id"]) for row in delivery_selection_rows]
    preexisting_notification_keys = _existing_notification_keys(
        store,
        events=events,
        notify=notify,
    )
    cycle_contract_args: dict[str, Any] = {
        "scan_id": scan_result.run_id,
        "generated_at": timestamp,
        "ranked_count": len(ranked),
        "signals": signals,
        "review": review,
        "source_summary": source_summary,
        "enrichment_summary": dict(enrichment["summary"]),
    }
    _persist_run_contract(
        output_dir,
        **cycle_contract_args,
        notification_stats={},
        notification_channel=notify,
        notification_dry_run=dry_run,
        notification_status_override="pending",
    )
    try:
        notification_stats = _dispatch(
            events,
            notify=notify,
            db_path=db_path,
            dry_run=dry_run,
        )
    except Exception:
        _persist_notification_delivery_memberships(
            store,
            selections=delivery_selection_rows,
            events=events,
            notify=notify,
            preexisting_notification_keys=preexisting_notification_keys,
        )
        _persist_run_contract(
            output_dir,
            **cycle_contract_args,
            notification_stats={},
            notification_channel=notify,
            notification_dry_run=dry_run,
            notification_status_override="delivery_failed",
        )
        raise
    notification_deliveries = _persist_notification_delivery_memberships(
        store,
        selections=delivery_selection_rows,
        events=events,
        notify=notify,
        preexisting_notification_keys=preexisting_notification_keys,
    )
    run_contract = _persist_run_contract(
        output_dir,
        **cycle_contract_args,
        notification_stats=notification_stats,
        notification_channel=notify,
        notification_dry_run=dry_run,
    )
    notification_link = _link_notification_events(
        store,
        scan_id=scan_result.run_id,
        events=events,
        notify=notify,
        dry_run=dry_run,
        signal_ids=selected_signal_ids,
        notification_deliveries=notification_deliveries,
    )
    result: dict[str, Any] = {
        "status": "complete" if not decision.get("no_trade") else "no_trade",
        "run_type": cycle_name,
        "scan_id": scan_result.run_id,
        "model_version": ALPHA_MODEL_VERSION,
        "source_summary": source_summary,
        "source_reliability": source_reliability,
        "premarket_enrichment": enrichment["summary"],
        "regime": regime,
        "feature_vector_count": len(feature_vectors),
        "v6_shadow": {
            "strategy_version": "dawnstrike-alphaops-v6-shadow",
            "decision_count": len(v6_decisions),
            "tracked_count": sum(1 for row in v6_decisions if row.get("action") == "SHADOW_TRACK"),
            "persistence": v6_decision_stats,
            "versioned_universe_membership_count": len(universe_memberships),
            "missing_versioned_universe_memberships": missing_v6_universe_memberships,
            "universe_registration": v6_universe_registration,
            "research_only": True,
            "broker_execution_enabled": False,
        },
        "signal_count": len(signals),
        "strategy_decision_receipts": strategy_receipt_stats,
        "research_radar": research_radar,
        "historical_signal_count": len(historical_rows),
        "historical_notification_link": notification_link,
        "top_signal": signals[0] if signals else None,
        "review": review,
        "selection_stats": selection_stats,
        "research_radar_selection_stats": radar_selection_stats,
        "notification_stats": notification_stats,
        "notification_deliveries": notification_deliveries,
        "run_contract": run_contract.to_dict(),
        "scan_paths": {key: str(value) for key, value in scan_paths.items()},
        "out_dir": str(output_dir),
    }
    if session_gate is not None:
        result["session_gate"] = session_gate.to_dict()
    _write_json(output_dir / "alpha_cycle.json", result)
    _write_json(output_dir / "alpha_signals.json", signals)
    _write_json(output_dir / "alpha_features.json", feature_vectors)
    return result


def alpha_monitor(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    notify: str = "console",
    dry_run: bool = False,
    current_prices: dict[str, float] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    session_gate = _scheduled_session_gate(notify=notify, as_of=as_of)
    if session_gate is not None:
        phase = core_session_phase(as_of)
        if not session_gate.is_trading_day:
            return _session_skip_result(
                run_type="alpha_monitor",
                status="skipped_market_closed",
                decision=session_gate,
            )
        if phase != "core_session_open":
            return _session_skip_result(
                run_type="alpha_monitor",
                status="skipped_outside_core_session",
                decision=session_gate,
                phase=phase,
            )
    store = SQLiteScanStore(db_path)
    signals = store.load_alpha_signals(limit=25)
    latest_scan_id = str(signals[0].get("scan_id") or "") if signals else ""
    signals = [row for row in signals if str(row.get("scan_id") or "") == latest_scan_id]
    latest_signal_date = _signal_market_date(signals[0]) if signals else ""
    if session_gate is not None and signals and latest_signal_date != session_gate.market_date:
        return {
            "status": "stale_watchlist",
            "label": "NO CURRENT WATCH",
            "message": (
                "The latest saved AlphaOps watchlist is not from the current market "
                "session, so no monitor notification was created."
            ),
            "latest_watchlist_market_date": latest_signal_date or "unknown",
            "required_market_date": session_gate.market_date,
            "tickers": [],
            "events": [],
            "notification_stats": {"sent": 0, "skipped": 0},
            "session_gate": session_gate.to_dict(),
        }
    exact_selections = store.load_signal_selections(
        scan_id=latest_scan_id,
        cohort=ALPHAOPS_OFFICIAL_COHORT,
        limit=500,
    )
    if session_gate is not None and latest_scan_id and not exact_selections:
        # Older AlphaOps runs predate the immutable selection tables.  Recovery
        # is deliberately based on the exact persisted Telegram body; an
        # ambiguous/truncated message remains unknown and therefore fail-closed.
        recover_legacy_alpha_notification_memberships(db_path=db_path, limit=500)
        exact_selections = store.load_signal_selections(
            scan_id=latest_scan_id,
            cohort=ALPHAOPS_OFFICIAL_COHORT,
            limit=500,
        )
    radar_selections = store.load_signal_selections(
        scan_id=latest_scan_id,
        cohort=ALPHAOPS_RADAR_COHORT,
        limit=50,
    )
    selection_evidence_status = "legacy_manual_fallback"
    if exact_selections:
        official_signal_ids = {
            str(row.get("signal_id") or "")
            for row in exact_selections
            if str(row.get("decision") or "").lower() != "no_trade"
            and str(row.get("ticker") or "").upper() != "NO_TRADE"
        }
        if official_signal_ids:
            signals = [
                row
                for row in signals
                if str(row.get("signal_id") or row.get("signal_key") or "") in official_signal_ids
            ]
            selection_evidence_status = "exact_official_cohort"
        elif radar_selections:
            signals = _radar_monitor_signals(signals, radar_selections)
            selection_evidence_status = "exact_research_radar_cohort"
        else:
            signals = []
            selection_evidence_status = "exact_no_trade_cohort"
    elif radar_selections:
        signals = _radar_monitor_signals(signals, radar_selections)
        selection_evidence_status = "exact_research_radar_cohort"
    elif session_gate is not None and signals:
        return {
            "status": "selection_evidence_unavailable",
            "label": "SELECTION AUDIT REQUIRED",
            "message": (
                "The exact delivered AlphaOps cohort could not be proven, so no "
                "signals were monitored and no paper lifecycle was inferred."
            ),
            "latest_watchlist_market_date": latest_signal_date or "unknown",
            "required_market_date": session_gate.market_date,
            "tickers": [],
            "events": [],
            "notification_stats": {"sent": 0, "skipped": 0},
            "selection_evidence_status": "unavailable",
            "session_gate": session_gate.to_dict(),
        }
    active_signals = [
        row
        for row in signals
        if (
            str(row.get("monitor_cohort") or "") == ALPHAOPS_RADAR_COHORT
            or (bool(row.get("can_alert")) and not str(row.get("no_trade_reason") or "").strip())
        )
    ]
    price_observation: dict[str, Any] | None = None
    current_quotes: dict[str, dict[str, Any]] | None = None
    if current_prices is None and active_signals:
        try:
            price_observation = collect_price_observations(
                db_path=db_path,
                source="alpaca",
                tickers=[str(row.get("ticker") or "") for row in active_signals],
                max_age_seconds=360,
                persist=False,
            )
        except DataProviderError:
            if not dry_run:
                raise
            price_observation = {
                "status": "provider_unavailable_dry_run",
                "usable_count": 0,
                "observations": [],
                "research_only": True,
                "broker_execution_enabled": False,
            }
        current_prices = {
            str(row.get("ticker") or "").upper(): float(row["current_price"])
            for row in price_observation.get("observations", [])
            if row.get("is_usable") and row.get("current_price") not in {None, ""}
        }
        try:
            live_config = load_config(database_path=Path(db_path))
            quote_provider = AlpacaProvider(live_config)
            quote_provider.validate_credentials()
            quote_rows = quote_provider.get_latest_quotes(
                [str(row.get("ticker") or "") for row in active_signals],
                live_config,
            )
            current_quotes = {
                ticker: {
                    **row,
                    "is_usable": _live_quote_is_usable(row, as_of=as_of),
                }
                for ticker, row in quote_rows.items()
            }
        except DataProviderError as exc:
            current_quotes = {}
            if price_observation is not None:
                price_observation["quote_status"] = "provider_unavailable"
                price_observation["quote_error"] = str(exc)
    if not active_signals:
        result: dict[str, Any] = {
            "status": "no_active_watchlist",
            "label": "NO ACTIVE WATCH",
            "message": "The latest AlphaOps run has no alertable research watchlist to monitor.",
            "tickers": [],
            "events": [],
        }
    else:
        result = monitor_alpha_signals(
            active_signals,
            current_prices=current_prices,
            current_quotes=current_quotes,
        )
    if price_observation is not None:
        result["price_observation"] = {
            key: value for key, value in price_observation.items() if key != "observations"
        }
        if not current_prices:
            result.update(
                {
                    "status": "manual_monitor_required",
                    "label": "MANUAL REVIEW",
                    "message": (
                        "No fresh sourced current prices were available for the "
                        "active watchlist; manual review is required."
                    ),
                    "events": [],
                    "tickers": [
                        str(row.get("ticker") or "")
                        for row in active_signals
                        if str(row.get("ticker") or "")
                    ],
                }
            )
    if current_quotes is not None:
        result["live_quote_check"] = {
            "required_for_research_radar": True,
            "usable_count": sum(1 for row in current_quotes.values() if row.get("is_usable")),
            "requested_count": len(active_signals),
            "maximum_spread_pct": 3.0,
        }
    result["historical_event_stats"] = record_monitor_signal_events(
        store,
        signals=active_signals,
        monitor_events=list(result.get("events") or []),
    )
    events: list[NotificationEvent] = []
    if result.get("status") != "no_active_watchlist":
        message = format_alpha_monitor(result)
        event_key = _monitor_event_key(latest_scan_id, result)
        events.append(
            _notification_event("alpha_monitor", event_key, "Dawnstrike Alpha Monitor", message)
        )
    result["notification_stats"] = _dispatch(
        events,
        notify=notify,
        db_path=db_path,
        dry_run=dry_run,
    )
    if session_gate is not None:
        result["session_gate"] = session_gate.to_dict()
    result["selection_evidence_status"] = selection_evidence_status
    return result


def _monitor_event_key(scan_id: str, result: dict[str, Any]) -> str:
    states = sorted(
        f"{row.get('ticker')}:{row.get('status') or row.get('label')}"
        for row in result.get("events", [])
    )
    state_text = ";".join(states) or str(result.get("status") or "unknown")
    digest = hashlib.sha256(f"{scan_id}|{state_text}".encode()).hexdigest()[:16]
    return f"{scan_id or 'no-scan'}:{digest}"


def _radar_monitor_signals(
    signals: list[dict[str, Any]],
    selections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selection_by_signal = {
        str(row.get("signal_id") or ""): row for row in selections if row.get("signal_id")
    }
    monitored: list[dict[str, Any]] = []
    for signal in signals:
        signal_id = str(signal.get("signal_id") or signal.get("signal_key") or "")
        selection = selection_by_signal.get(signal_id)
        if selection is None:
            continue
        selection_payload = dict(selection.get("payload_json") or {})
        radar_signal = dict(selection_payload.get("signal") or {})
        target = _number(radar_signal.get("radar_target") or signal.get("research_radar_target"))
        monitored.append(
            {
                **signal,
                "monitor_cohort": ALPHAOPS_RADAR_COHORT,
                "monitor_strategy_version": ALPHAOPS_RADAR_VERSION,
                "target_1": target or signal.get("target_1"),
                "first_target": target or signal.get("first_target"),
                "research_radar_target": target,
                "research_only": True,
                "broker_execution_enabled": False,
            }
        )
    return monitored


def _live_quote_is_usable(
    quote: dict[str, Any],
    *,
    as_of: datetime | None,
) -> bool:
    bid = _number(quote.get("bid"))
    ask = _number(quote.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return False
    raw_timestamp = str(quote.get("timestamp") or "").strip()
    if not raw_timestamp:
        return False
    try:
        observed_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    reference = as_of.astimezone(timezone.utc) if as_of else datetime.now(timezone.utc)
    age_seconds = (reference - observed_at.astimezone(timezone.utc)).total_seconds()
    return -5.0 <= age_seconds <= 120.0


def _scheduled_session_gate(
    *,
    notify: str,
    as_of: datetime | None,
) -> MarketSessionDecision | None:
    channels = {channel.strip().lower() for channel in notify.split(",") if channel.strip()}
    if not channels or channels <= {"console"}:
        return None
    return session_for_timestamp(as_of)


def _session_skip_result(
    *,
    run_type: str,
    status: str,
    decision: MarketSessionDecision,
    phase: str = "market_closed",
) -> dict[str, Any]:
    return {
        "status": status,
        "run_type": run_type,
        "phase": phase,
        "message": "No external research notification was attempted outside its allowed session.",
        "session_gate": decision.to_dict(),
        "notification_stats": {"sent": 0, "skipped": 0, "errors": []},
        "research_only": True,
        "order_execution_enabled": False,
    }


def alpha_outcomes(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    return run_alpha_learning(store)


def alpha_learn(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    return run_alpha_learning(store)


def alpha_status(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    store = SQLiteScanStore(db_path, read_only=True)
    store.initialize()
    latest_scan = store.load_latest_scan()
    signals = store.load_alpha_signals(limit=20)
    labels = load_production_alpha_learning_labels(store)
    learning = store.load_alpha_learning_runs(limit=1)
    reliability = store.load_alpha_source_reliability()
    setup_memory = store.load_alpha_setup_memory()
    real_days = _real_days(labels)
    latest_market_date = _latest_signal_date(store)
    outcome_status = calculate_missing_outcome_status(db_path, latest_market_date)
    return {
        "status": "ok",
        "db_path": str(db_path),
        "latest_scan_id": dict(latest_scan or {}).get("run_id"),
        "model_version": ALPHA_MODEL_VERSION,
        "signal_count": len(signals),
        "latest_signal": signals[0] if signals else None,
        "feature_vector_count": len(store.load_alpha_feature_vectors(limit=5000)),
        "outcome_label_count": len(labels),
        "source_reliability_count": len(reliability),
        "setup_memory_count": len(setup_memory),
        "real_days_collected": real_days,
        "missing_outcome_status": outcome_status,
        "missing_outcome_count": outcome_status.get("missing_outcome_count", 0),
        "enough_evidence": real_days >= 20,
        "last_learning_run": _normalize_learning_run(learning[0]) if learning else None,
        "research_only": True,
        "order_execution_enabled": False,
    }


def _latest_signal_date(store: SQLiteScanStore) -> str | None:
    signals = store.load_historical_signals(limit=1)
    if not signals:
        return None
    day = str(signals[0].get("market_date") or "")[:10]
    return day or None


def _normalize_learning_run(row: dict[str, Any]) -> dict[str, Any]:
    """Preserve legacy audit payloads while removing fake zero performance truth."""

    normalized = dict(row)
    truth = dict(normalized.get("truth_report") or {})
    if int(truth.get("sample_size") or 0) == 0:
        for key in (
            "average_return_pct",
            "median_return_pct",
            "win_rate_pct",
            "worst_day_return_pct",
            "best_day_return_pct",
            "max_drawdown_pct",
            "missing_outcome_rate_pct",
        ):
            truth[key] = None
        truth["evidence_status"] = "insufficient_real_outcomes"
        outlier = dict(truth.get("outlier") or {})
        outlier["outlier_dependency"] = None
        outlier["outlier_dependent"] = False
        truth["outlier"] = outlier
        for key in ("top1", "top3", "top5"):
            summary = dict(truth.get(key) or {})
            if int(summary.get("sample_size") or 0) == 0:
                for metric in (
                    "avg_return_pct",
                    "median_return_pct",
                    "win_rate_pct",
                    "max_drawdown_pct",
                ):
                    summary[metric] = None
            truth[key] = summary
    normalized["truth_report"] = truth
    return normalized


def alpha_doctor(
    *,
    config_path: str | Path = DEFAULT_WEB_CONFIG,
    out_dir: str | Path = "outputs/alpha_doctor",
) -> dict[str, Any]:
    result = web_source_doctor(config_path=config_path, out_dir=out_dir, print_rows=False)
    result["alphaops_checks"] = {
        "research_only": True,
        "order_execution": "disabled_by_design",
        "manual_fallback": "data/inbox/screener",
    }
    return result


def alpha_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    out_dir: str | Path = "outputs/alpha_report",
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path, read_only=True)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    labels = load_production_alpha_learning_labels(store)
    real_days = _real_days(labels)
    truth = build_truth_report(labels, real_days_collected=real_days)
    status = alpha_status(db_path=db_path)
    review_metrics = _alpha_daily_review_metrics(
        store.load_daily_review_runs(limit=5000),
        store.load_learning_backfeed_events(limit=50000),
    )
    summary = {
        "created_at": utc_now_iso(),
        "status": status,
        "truth_report": truth,
        "daily_review_metrics": review_metrics,
        "source_reliability": store.load_alpha_source_reliability(),
        "setup_memory": store.load_alpha_setup_memory(),
        "alpha_summary_message": format_alpha_summary({"truth_report": truth}),
    }
    _write_json(output_dir / "alpha_report.json", summary)
    _write_markdown(output_dir / "alpha_report.md", summary)
    return {**summary, "out_dir": str(output_dir)}


def _persist_run_contract(
    output_dir: Path,
    *,
    scan_id: str,
    generated_at: str,
    ranked_count: int,
    signals: list[dict[str, Any]],
    review: dict[str, Any],
    source_summary: dict[str, Any],
    enrichment_summary: dict[str, Any] | None,
    notification_stats: dict[str, Any],
    notification_channel: str,
    notification_dry_run: bool,
    notification_status_override: str = "",
) -> AlphaRunContract:
    contract = build_alpha_run_contract(
        scan_id=scan_id,
        generated_at=generated_at,
        ranked_count=ranked_count,
        signals=signals,
        review=review,
        source_summary=source_summary,
        enrichment_summary=enrichment_summary,
        notification_stats=notification_stats,
        notification_channel=notification_channel,
        notification_dry_run=notification_dry_run,
        notification_status_override=notification_status_override,
    )
    _write_json(output_dir / "alpha_run_contract.json", contract.to_dict())
    return contract


def _alphaops_scanner_config(config: Any) -> Any:
    """Use a liquid day-trading universe instead of the legacy penny-gap profile."""

    return config.with_overrides(
        min_gap_pct=1.0,
        ideal_gap_low_pct=3.0,
        ideal_gap_high_pct=25.0,
        max_credible_gap_pct=50.0,
        min_premarket_dollar_volume=1_000_000.0,
        min_premarket_share_volume=50_000,
        min_price=1.0,
        max_price=500.0,
        top_n=20,
        wide_spread_pct=3.0,
        premarket_enrichment_max_candidates=60,
    )


def _verify_ranked_sec_safety(
    ranked: list[dict[str, Any]],
    *,
    source_config: Any,
    store: SQLiteScanStore,
    out_dir: Path,
    as_of: str,
    rehearsal_mode: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Verify SEC safety against the candidates that actually survived ranking."""

    tickers = [str(row.get("ticker") or "").upper() for row in ranked if row.get("ticker")]
    if not tickers:
        return ranked, {"status": "NO_RANKED_CANDIDATES", "checked_tickers": []}
    if rehearsal_mode:
        return ranked, {
            "status": "REHEARSAL_REUSED_COLLECTION_EVIDENCE",
            "checked_tickers": sorted(
                ticker
                for ticker, row in zip(tickers, ranked, strict=False)
                if str(row.get("sec_risk_status") or "").upper() in {"CLEAR", "BLOCKED"}
            ),
        }
    source = get_source(source_config, "sec_edgar")
    if source is None:
        return ranked, {
            "status": "DISABLED",
            "checked_tickers": [],
            "unchecked_tickers": sorted(set(tickers)),
        }
    summary = collect_sec_risk(
        source=source,
        config=source_config,
        tickers=tickers,
        out_dir=out_dir,
        store=store,
        persist=True,
    )
    enriched = enrich_rows_with_sec_risk(
        ranked,
        list(summary.get("events") or []),
        checked_tickers=list(summary.get("checked_tickers") or []),
        as_of=as_of,
    )
    return enriched, {
        "status": str(summary.get("status") or "partial").upper(),
        "checked_tickers": list(summary.get("checked_tickers") or []),
        "unchecked_tickers": list(summary.get("unchecked_tickers") or []),
        "event_count": int(summary.get("event_count") or 0),
        "ranked_candidate_count": len(tickers),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _merge_ranked_safety(
    candidates: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    safety_by_ticker = {
        str(row.get("ticker") or "").upper(): row for row in ranked if row.get("ticker")
    }
    fields = (
        "sec_risk_status",
        "corporate_action_status",
        "recent_offering",
        "reverse_split_90d",
        "sec_active_risk_events",
        "coverage_warning",
    )
    merged: list[dict[str, Any]] = []
    for candidate in candidates:
        updated = dict(candidate)
        safety = safety_by_ticker.get(str(updated.get("ticker") or "").upper())
        if safety:
            for field in fields:
                if field in safety:
                    updated[field] = safety[field]
        merged.append(updated)
    return merged


def _register_alpaca_screening_universe(
    store: SQLiteScanStore,
    *,
    source_summary: dict[str, Any],
    market_date: str,
) -> dict[str, Any]:
    attempts = list(source_summary.get("attempts") or [])
    evidence = next(
        (
            dict(row.get("universe_evidence") or {})
            for row in attempts
            if str(row.get("source_type") or "") == "alpaca_screener_api"
            and str(row.get("status") or "") == "success"
            and isinstance(row.get("universe_evidence"), dict)
        ),
        {},
    )
    members = list(evidence.get("members") or [])
    if not members:
        return {
            "status": "UNAVAILABLE",
            "reason": "authenticated_alpaca_screening_universe_missing",
            "member_count": 0,
        }
    if evidence.get("registration_approved") is not True:
        return {
            "status": "BLOCKED_CONFIGURATION",
            "reason": "alpaca_v6_registration_not_approved",
            "member_count": len(members),
        }
    contract = {
        "provider_id": "alpaca",
        "dataset_id": str(evidence.get("dataset_id") or "stocks-screener-plus-active-assets"),
        "dataset_version": str(evidence.get("dataset_version") or evidence.get("retrieved_at")),
        "terms_reference": str(evidence.get("terms_reference") or "https://docs.alpaca.markets/"),
        "entitlement_reference": str(
            evidence.get("entitlement_reference") or "configured-alpaca-account"
        ),
        "accountable_contact": str(
            evidence.get("accountable_contact") or "dawnstrikebot@gmail.com"
        ),
        "approval_status": "APPROVED",
        "critical_truth_complete": True,
        "registration_allowed": True,
    }
    contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True).encode("utf-8")).hexdigest()
    raw_hash = str(evidence.get("raw_artifact_sha256") or "")
    if len(raw_hash) != 64:
        return {
            "status": "BLOCKED_EVIDENCE",
            "reason": "alpaca_universe_artifact_hash_missing",
            "member_count": len(members),
        }
    lineage = {
        "source_id": "alpaca:stocks-screener-plus-active-assets",
        **contract,
        "source_contract_hash_sha256": contract_hash,
        "retrieved_at": str(evidence.get("retrieved_at") or utc_now_iso()),
        "raw_artifact_sha256": raw_hash,
        "configuration_hash_sha256": hashlib.sha256(
            json.dumps(
                {
                    "market_date": market_date,
                    "policy": ALPHAOPS_RADAR_VERSION,
                    "members": sorted(str(row.get("ticker") or "") for row in members),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    }
    registered = register_alpha_v6_universe(
        store,
        as_of_date=market_date,
        members=members,
        source_lineage=lineage,
    )
    return {
        "status": "REGISTERED",
        "universe_id": registered.get("universe_id"),
        "member_count": int(registered.get("membership_count") or len(members)),
        "persisted": registered.get("persisted"),
        "source_lineage_hash_sha256": registered.get("source_lineage_hash_sha256"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _research_radar(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select safe-to-study conditional setups even when official evidence is incomplete."""

    rows: list[dict[str, Any]] = []
    for signal in signals:
        hard = signal.get("hard_avoid_reasons")
        hard_reasons = (
            [str(item) for item in hard]
            if isinstance(hard, list)
            else [part for part in str(hard or "").split(";") if part]
        )
        if hard_reasons:
            continue
        if bool(signal.get("stale_data_flag")):
            continue
        if (_number(signal.get("source_confidence")) or 0.0) < 25.0:
            continue
        if str(signal.get("source_quality_status") or "").upper() not in {
            "VERIFIED",
            "LIMITED",
        }:
            continue
        if str(signal.get("halt_status") or "").upper() != "CLEAR":
            continue
        if str(signal.get("sec_risk_status") or "").upper() != "CLEAR":
            continue
        if str(signal.get("corporate_action_status") or "").upper() != "CLEAR":
            continue
        risk_text = ";".join(
            str(signal.get(field) or "")
            for field in ("risk_flags", "coverage_warning", "catalyst_risk_flags")
        ).lower()
        if "wide_spread" in risk_text or "extreme spread" in risk_text:
            continue
        trigger = _number(signal.get("entry_trigger") or signal.get("breakout_trigger"))
        stop = _number(signal.get("invalidation") or signal.get("invalidation_level"))
        dollar_volume = _number(signal.get("dollar_volume")) or 0.0
        spread = _number(signal.get("spread_pct")) or 0.0
        gap = _number(signal.get("gap_pct"))
        if (
            trigger is None
            or stop is None
            or not (trigger > stop > 0)
            or dollar_volume < 1_000_000
            or spread > 3.0
            or gap is None
            or not (1.0 <= gap <= 50.0)
        ):
            continue
        stop_distance_pct = (trigger - stop) / trigger * 100.0
        if stop_distance_pct > 8.0:
            continue
        target_options = (
            (
                "first_range_extension",
                _number(signal.get("target_1") or signal.get("first_target")),
            ),
            (
                "stretch_range_extension",
                _number(signal.get("target_2") or signal.get("stretch_target")),
            ),
        )
        chosen_target = None
        target_role = ""
        reward_risk = 0.0
        for role, target in target_options:
            if target is None or target <= trigger:
                continue
            candidate_rr = (target - trigger) / (trigger - stop)
            if candidate_rr >= 1.5:
                chosen_target = target
                target_role = role
                reward_risk = candidate_rr
                break
        if chosen_target is None:
            continue
        reasons = signal.get("alert_gate_reasons") or []
        reason_text = (
            "; ".join(str(item) for item in reasons) if isinstance(reasons, list) else str(reasons)
        )
        rows.append(
            {
                **signal,
                "cohort": ALPHAOPS_RADAR_COHORT,
                "strategy_version": ALPHAOPS_RADAR_VERSION,
                "decision_tier": "research_radar",
                "classification": "RESEARCH RADAR",
                "review_label": "CONDITIONAL PAPER WATCH",
                "reward_risk_ratio": round(reward_risk, 3),
                "radar_target": round(chosen_target, 4),
                "radar_target_role": target_role,
                "radar_stop_distance_pct": round(stop_distance_pct, 3),
                "radar_reason": reason_text or "official evidence gate incomplete",
                "research_only": True,
                "broker_execution_enabled": False,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            float(row.get("alpha_score") or 0.0),
            float(row.get("dollar_volume") or 0.0),
        ),
        reverse=True,
    )[:3]


def _annotate_research_radar(
    signals: list[dict[str, Any]],
    radar: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    radar_by_id = {str(row.get("signal_id") or row.get("signal_key") or ""): row for row in radar}
    output: list[dict[str, Any]] = []
    for signal in signals:
        signal_id = str(signal.get("signal_id") or signal.get("signal_key") or "")
        selected = radar_by_id.get(signal_id)
        if not selected:
            output.append(signal)
            continue
        output.append(
            {
                **signal,
                "research_radar_selected": True,
                "research_radar_target": selected.get("radar_target"),
                "research_radar_target_role": selected.get("radar_target_role"),
                "research_radar_reward_risk_ratio": selected.get("reward_risk_ratio"),
                "research_radar_stop_distance_pct": selected.get("radar_stop_distance_pct"),
                "research_radar_policy": ALPHAOPS_RADAR_VERSION,
            }
        )
    return output


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _signal_payload(row: dict[str, Any], scan_id: str, timestamp: str, rank: int) -> dict[str, Any]:
    return {
        **row,
        "scan_id": scan_id,
        "rank": rank,
        "timestamp": timestamp,
        "signal_key": f"{scan_id}:{rank}:{row.get('ticker')}",
        "telegram_key": f"alpha:{scan_id}:{rank}:{row.get('ticker')}",
        "alert_sent": False,
    }


def _apply_strategy_decision_receipts(
    signals: list[dict[str, Any]],
    *,
    store: SQLiteScanStore,
    config: Any,
    decision_at: str,
    source_summary: dict[str, Any],
    gap_resolver: Any | None = None,
) -> dict[str, Any]:
    """Build, resolve, re-evaluate, and persist receipts before final gates."""

    if not getattr(config, "strategy_evidence_enabled", False):
        return {"status": "disabled", "computed": 0, "persisted": 0, "reused": 0}

    from intraday_scanner.decisioning.condition_registry import (
        registry_for_strategy,
        strategy_ids,
    )
    from intraday_scanner.decisioning.contracts import ConditionCategory, ConditionStatus
    from intraday_scanner.decisioning.evidence_resolver import CONDITION_CLAIM_TYPES

    code_sha = str(os.environ.get("DAWNSTRIKE_CODE_SHA") or "").strip()
    shadow_only = bool(getattr(config, "strategy_evidence_shadow_only", True))
    max_candidates = max(1, int(getattr(config, "strategy_evidence_max_candidates", 1)))
    max_symbols = max(
        1,
        int(getattr(config, "indeterminate_research_max_symbols", max_candidates)),
    )
    resolution_budget = min(max_candidates, max_symbols)
    timeout_seconds = float(
        getattr(config, "indeterminate_research_timeout_seconds", 60.0)
    )
    max_tool_calls = int(getattr(config, "indeterminate_research_max_tool_calls", 3))
    supported = set(strategy_ids())
    eligible_rows: list[tuple[int, dict[str, Any]]] = []
    uncovered: list[dict[str, Any]] = []
    construction_records: dict[int, dict[str, Any]] = {}

    def _mark_construction_failure(index: int, row: dict[str, Any], reason: str) -> None:
        record = {
            "status": "FAIL_CLOSED",
            "rank": row.get("rank") or index + 1,
            "ticker": str(row.get("ticker") or "").upper(),
            "strategy_id": str(row.get("strategy_id") or row.get("decision_strategy_id") or ""),
            "decision_at": decision_at,
            "reason": reason,
        }
        construction_records[index] = record
        row.update(
            {
                "strategy_receipt_construction_status": "FAIL_CLOSED",
                "strategy_receipt_construction_record": record,
                "strategy_receipt_gap": reason,
            }
        )

    for index, row in enumerate(signals):
        row.update(
            {
                "strategy_receipt_enabled": True,
                "strategy_receipt_shadow_only": shadow_only,
                "strategy_receipt_legacy_can_alert": bool(row.get("can_alert")),
                "strategy_receipt_construction_status": "PENDING",
                "strategy_receipt_disagreement": [],
            }
        )
        strategy_id = str(
            row.get("strategy_id") or row.get("decision_strategy_id") or ""
        ).strip()
        if not strategy_id:
            reason = "missing strategy identity"
        elif strategy_id not in supported:
            reason = f"strategy is outside universal registry: {strategy_id}"
        else:
            if not row.get("strategy_id"):
                row["strategy_id"] = strategy_id
            eligible_rows.append((index, row))
            continue
        row["strategy_receipt_gap"] = reason
        uncovered.append(
            {
                "rank": row.get("rank") or index + 1,
                "ticker": str(row.get("ticker") or "").upper(),
                "strategy_id": strategy_id or None,
                "reason": reason,
            }
        )
        _mark_construction_failure(index, row, reason)

    source_identity = str(source_summary.get("source_identity") or "").strip()
    if not source_identity:
        source_run_id = str(source_summary.get("run_id") or "").strip()
        if source_run_id:
            source_identity = f"web_auto_collect:{source_run_id}"

    if not code_sha:
        missing_identity_reason = "missing code identity"
    elif not source_identity:
        missing_identity_reason = "missing source identity"
    else:
        missing_identity_reason = ""

    service: StrategyDecisionService | None = None
    initial_records: list[tuple[int, dict[str, Any], Any]] = []
    if not missing_identity_reason:
        service = StrategyDecisionService(
            code_sha=code_sha,
            source_identity=source_identity,
            score_threshold=float(getattr(config, "alert_score_threshold", 0.0)),
        )
        individually_built: list[tuple[int, dict[str, Any], Any]] = []
        for index, row in eligible_rows:
            try:
                receipt = service.build_receipt(row, decision_at=decision_at)
            except (KeyError, TypeError, ValueError) as exc:
                _mark_construction_failure(
                    index, row, f"receipt construction failed: {type(exc).__name__}"
                )
                continue
            individually_built.append((index, row, receipt))
        if individually_built:
            try:
                batch = service.evaluate_candidates(
                    [row for _index, row, _receipt in individually_built],
                    decision_at=decision_at,
                )
                initial_records = [
                    (item[0], item[1], receipt)
                    for item, receipt in zip(individually_built, batch, strict=True)
                ]
            except (KeyError, TypeError, ValueError):
                initial_records = individually_built
    else:
        for index, row in eligible_rows:
            _mark_construction_failure(index, row, missing_identity_reason)

    unresolved_statuses = {
        ConditionStatus.MISSING_DISCLOSED,
        ConditionStatus.STALE,
        ConditionStatus.CONFLICT,
    }
    resolvable_categories = {
        ConditionCategory.AI_RESOLVABLE,
        ConditionCategory.EXECUTION_ONLY,
        ConditionCategory.ADVISORY,
    }
    def _record_rank(item: tuple[int, dict[str, Any], Any]) -> tuple[int, str, str, int]:
        index, row, _receipt = item
        try:
            rank = int(float(row.get("rank") or index + 1))
        except (TypeError, ValueError):
            rank = index + 1
        return (
            rank,
            str(row.get("ticker") or "").upper(),
            str(row.get("strategy_id") or ""),
            index,
        )

    ranked_initial_records = sorted(initial_records, key=_record_rank)
    selected_initial_records = ranked_initial_records[:max_candidates]
    selected_indices = {item[0] for item in selected_initial_records}
    deferred_count = max(0, len(ranked_initial_records) - len(selected_initial_records))
    resolution_candidates: list[tuple[int, dict[str, Any], Any, list[str]]] = []
    resolution_overrides: dict[int, dict[str, Any]] = {}
    resolution_bundles: dict[int, dict[str, Any]] = {}
    resolution_metrics: dict[str, Any] = {
        "request_count": 0,
        "web_search_call_count": 0,
        "token_usage": {},
        "cache_hits": 0,
        "elapsed_ms": 0,
        "attempts": 0,
    }
    for index, row, receipt in initial_records:
        specs = {spec.condition_id: spec for spec in registry_for_strategy(receipt.strategy_id)}
        result_by_id = {result.condition_id: result for result in receipt.condition_results}
        unresolved: list[str] = []
        resolver_condition_ids: list[str] = []
        for spec in specs.values():
            result = result_by_id[spec.condition_id]
            if (
                result.status not in unresolved_statuses
                or spec.category not in resolvable_categories
            ):
                continue
            unresolved.append(spec.condition_id)
            if (
                spec.resolver_id == "strategy_gap_resolver"
                and spec.condition_id in CONDITION_CLAIM_TYPES
            ):
                resolver_condition_ids.append(spec.condition_id)
        row["strategy_receipt_unresolved_conditions"] = unresolved
        row["strategy_receipt_resolution_candidates"] = resolver_condition_ids
        row["strategy_evidence_resolution_status"] = (
            "not_required" if not unresolved else "not_resolver_supported"
        )
        blocking_only_contextual = True
        for failure in receipt.all_blocking_failures:
            failure_spec = specs.get(failure)
            result = result_by_id.get(failure)
            if (
                failure_spec is None
                or failure_spec.category not in resolvable_categories
                or (result is not None and result.status == ConditionStatus.FAIL)
            ):
                blocking_only_contextual = False
                break
        if (
            index in selected_indices
            and resolver_condition_ids
            and blocking_only_contextual
        ):
            resolution_candidates.append((index, row, receipt, resolver_condition_ids))

    def _rank(item: tuple[int, dict[str, Any], Any, list[str]]) -> tuple[int, str, str, int]:
        index, row, _receipt, _condition_ids = item
        try:
            rank = int(float(row.get("rank") or index + 1))
        except (TypeError, ValueError):
            rank = index + 1
        return (
            rank,
            str(row.get("ticker") or "").upper(),
            str(row.get("strategy_id") or ""),
            index,
        )

    resolution_candidates.sort(key=_rank)
    selected_resolution_candidates = resolution_candidates[:resolution_budget]
    resolution_deferred_count = max(
        0, len(resolution_candidates) - len(selected_resolution_candidates)
    )
    resolution_metrics["selected_candidates"] = len(selected_initial_records)
    resolution_metrics["resolver_candidates"] = len(selected_resolution_candidates)
    if selected_resolution_candidates:
        resolver = gap_resolver or StrategyGapResolver(
            api_key=str(getattr(config, "openai_api_key", "") or ""),
            model=str(getattr(config, "scenario_openai_model", "") or ""),
            timeout_seconds=timeout_seconds,
            max_tool_calls=max_tool_calls,
            max_symbols=max_symbols,
        )
        deadline = time.monotonic() + (timeout_seconds * len(selected_resolution_candidates))
        for index, row, _receipt, condition_ids in selected_resolution_candidates:
            if time.monotonic() >= deadline:
                row["strategy_evidence_resolution_status"] = "time_budget_exhausted"
                continue
            resolution_metrics["attempts"] += 1
            try:
                result = resolver.resolve(
                    symbol=str(row.get("ticker") or row.get("symbol") or "").upper(),
                    market_date=str(row.get("market_date") or decision_at[:10]),
                    decision_at=decision_at,
                    condition_ids=condition_ids,
                    source_identity=source_identity,
                )
            except Exception as exc:  # provider failures remain disclosed gaps
                row["strategy_evidence_resolution_status"] = (
                    f"provider_failure:{type(exc).__name__}"
                )
                continue
            row["strategy_evidence_resolution_status"] = str(
                result.get("status") or "provider_failure"
            )
            overrides: dict[str, Any] = {}
            for raw_condition in result.get("condition_results") or ():
                if not isinstance(raw_condition, dict):
                    continue
                condition_id = str(raw_condition.get("condition_id") or "")
                if condition_id in condition_ids:
                    overrides[condition_id] = dict(raw_condition)
            if overrides:
                resolution_overrides[index] = overrides
            run = result.get("run")
            claims = [claim for claim in result.get("claims") or () if isinstance(claim, dict)]
            if isinstance(run, dict):
                resolution_bundles[index] = {"claims": claims, "run": dict(run)}
                for metric in (
                    "request_count",
                    "web_search_call_count",
                    "cache_hits",
                    "elapsed_ms",
                ):
                    resolution_metrics[metric] += int(run.get(metric) or 0)
                for key, value in dict(run.get("token_usage") or {}).items():
                    resolution_metrics["token_usage"][str(key)] = (
                        int(resolution_metrics["token_usage"].get(str(key), 0)) + int(value or 0)
                    )

    final_records: list[tuple[int, dict[str, Any], Any, dict[str, Any]]] = []
    for index, row, _initial_receipt in initial_records:
        overrides = resolution_overrides.get(index, {})
        payload = dict(row)
        if overrides:
            payload["condition_results"] = overrides
        try:
            if service is None:
                _mark_construction_failure(index, row, "receipt service unavailable")
                continue
            receipt = service.build_receipt(
                payload,
                decision_at=decision_at,
                condition_overrides=overrides or None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            _mark_construction_failure(
                index, row, f"final receipt construction failed: {type(exc).__name__}"
            )
            continue
        final_records.append((index, row, receipt, overrides))

    if final_records and service is not None:
        try:
            batch = service.evaluate_candidates(
                [
                    ({**row, "condition_results": overrides} if overrides else dict(row))
                    for _index, row, _receipt, overrides in final_records
                ],
                decision_at=decision_at,
            )
            final_records = [
                (item[0], item[1], receipt, item[3])
                for item, receipt in zip(final_records, batch, strict=True)
            ]
        except (KeyError, TypeError, ValueError):
            pass

    persisted_count = 0
    reused_count = 0
    for index, row, receipt, _overrides in final_records:
        specs = {spec.condition_id: spec for spec in registry_for_strategy(receipt.strategy_id)}
        result_by_id = {result.condition_id: result for result in receipt.condition_results}
        core_categories = {
            ConditionCategory.HARD_MARKET,
            ConditionCategory.HARD_RISK,
            ConditionCategory.STRATEGY_CORE,
        }
        core_passed = [
            condition_id
            for condition_id, spec in specs.items()
            if spec.category in core_categories
            and result_by_id[condition_id].status
            in {ConditionStatus.PASS, ConditionStatus.RESOLVED_FROM_SOURCE}
        ]
        ai_evidence = [
            {
                "condition_id": result.condition_id,
                "status": getattr(result.status, "value", str(result.status)),
                "source_urls": list(result.source_urls)[:2],
                "source_hashes": list(result.source_hashes)[:2],
            }
            for result in receipt.condition_results
            if specs[result.condition_id].category == ConditionCategory.AI_RESOLVABLE
            and result.status == ConditionStatus.RESOLVED_FROM_SOURCE
        ]
        unresolved_final = [
            spec.condition_id
            for spec in specs.values()
            if spec.category in resolvable_categories
            and result_by_id[spec.condition_id].status in unresolved_statuses
        ]
        tier = getattr(receipt.pick_tier, "value", str(receipt.pick_tier))
        why = (
            "all research-blocking conditions passed"
            if receipt.research_pick_eligible and not receipt.disclosed_gaps
            else (
                "research eligible with disclosed gaps"
                if receipt.research_pick_eligible
                else f"blocked by {receipt.first_blocking_failure or 'deterministic policy'}"
            )
        )
        disagreements = list(row.get("strategy_receipt_disagreement") or [])
        if bool(row.get("strategy_receipt_legacy_can_alert")) != receipt.research_pick_eligible:
            if "legacy_vs_receipt_alert_disposition" not in disagreements:
                disagreements.append("legacy_vs_receipt_alert_disposition")
        row.update(
            {
                "receipt_id": receipt.receipt_id,
                "receipt_hash_sha256": receipt.receipt_hash_sha256,
                "pick_tier": tier,
                "research_pick_eligible": receipt.research_pick_eligible,
                "paper_entry_eligible": receipt.paper_entry_eligible,
                "disclosed_gaps": list(receipt.disclosed_gaps),
                "first_blocking_failure": receipt.first_blocking_failure,
                "all_blocking_failures": list(receipt.all_blocking_failures),
                "condition_results": [result.to_dict() for result in receipt.condition_results],
                "core_conditions_passed": core_passed,
                "ai_resolved_evidence": ai_evidence,
                "reward_risk_ratio": receipt.reward_risk_ratio,
                "strategy_receipt_tier": tier,
                "strategy_receipt_research_pick_eligible": receipt.research_pick_eligible,
                "strategy_receipt_paper_entry_eligible": receipt.paper_entry_eligible,
                "strategy_receipt_unresolved_conditions": unresolved_final,
                "strategy_receipt_construction_status": "COMPLETE",
                "strategy_receipt_gap": "",
                "strategy_receipt_disagreement": disagreements,
                "receipt_reason": why,
                "research_only": receipt.research_only,
                "broker_execution_enabled": receipt.broker_execution_enabled,
            }
        )
        bundle = resolution_bundles.get(index, {})
        run = bundle.get("run")
        if isinstance(run, dict) and str(run.get("status") or "") != "cache_hit":
            run = dict(run)
            if str(run.get("run_id") or "").startswith("unavailable-"):
                run["run_id"] = f"unavailable-{receipt.receipt_id}"
        inserted = store.persist_strategy_decision_receipt(
            receipt,
            evidence_claims=bundle.get("claims") or (),
            resolution_run=run,
        )
        if inserted:
            persisted_count += 1
        else:
            reused_count += 1

    stats = {
        "status": "shadow_only" if shadow_only else "non_shadow_research_only",
        "computed": len(final_records),
        "persisted": persisted_count,
        "reused": reused_count,
        "eligible_candidates": len(eligible_rows),
        "uncovered_candidates": len(uncovered),
        "uncovered": uncovered,
        "construction_records": list(construction_records.values()),
        "build_errors": list(construction_records.values()),
        "resolution_budget": resolution_budget,
        "resolution_candidates": len(selected_initial_records),
        "resolver_candidates": len(selected_resolution_candidates),
        "selected_candidates": len(selected_initial_records),
        "resolution_deferred": deferred_count,
        "resolver_deferred": resolution_deferred_count,
        "resolution_metrics": resolution_metrics,
        "legacy_selection_unchanged": True,
        "policy_mutation": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    source_summary["strategy_decision_receipts"] = stats
    return stats


def _apply_receipt_risk_gates(
    signals: list[dict[str, Any]], feature_vectors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    feature_by_ticker = {
        str(row.get("ticker") or "").upper(): row for row in feature_vectors
    }
    for row in signals:
        legacy_can_alert = row.get("can_alert")
        legacy_no_trade_reason = str(row.get("no_trade_reason") or "")
        decision = evaluate_risk(
            row,
            feature_by_ticker.get(str(row.get("ticker") or "").upper(), {}),
        )
        row.update(decision.to_dict())
        row["research_only"] = True
        row["broker_execution_enabled"] = False
        disagreements = list(row.get("strategy_receipt_disagreement") or [])
        for reason in decision.strategy_receipt_disagreement or []:
            if reason not in disagreements:
                disagreements.append(reason)
        row["strategy_receipt_disagreement"] = disagreements
        if bool(row.get("strategy_receipt_shadow_only")):
            row["can_alert"] = legacy_can_alert
            row["no_trade_reason"] = legacy_no_trade_reason
        elif not decision.can_alert:
            row["no_trade_reason"] = ";".join(
                dict.fromkeys(
                    [
                        item
                        for item in [legacy_no_trade_reason, *decision.hard_avoid_reasons]
                        if item
                    ]
                )
            )
    return signals


def _notification_event(
    run_id: str,
    hint: str,
    title: str,
    body: str,
    *,
    payload: dict[str, Any] | None = None,
) -> NotificationEvent:
    return NotificationEvent(
        event_key=f"alphaops:{run_id}:{hint}",
        title=title,
        body=body,
        channel_hint=hint,
        payload={
            "run_id": run_id,
            "source": "alphaops",
            "telegram_compact_message": body,
            **dict(payload or {}),
        },
    )


def _official_selection_notification_event(
    run_id: str,
    hint: str,
    title: str,
    body: str,
    *,
    selected_signals: list[dict[str, Any]],
    research_signals: list[dict[str, Any]] | None = None,
) -> NotificationEvent:
    """Keep official cohort members separate from labeled research radar rows."""

    return _notification_event(
        run_id,
        hint,
        title,
        body,
        payload={
            "signals": list(selected_signals),
            "research_radar": list(research_signals or []),
        },
    )


def _persist_official_selections(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    selected_signals: list[dict[str, Any]],
    decision: dict[str, Any],
    selected_at: str,
    event: NotificationEvent,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Freeze the exact signal identities represented by one operator message."""

    strategy_id, strategy_version = alphaops_strategy_contract(selected_at)
    strategy_stats = store.persist_strategy_versions(
        [
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "registered_at": selected_at,
                "definition_json": {
                    "name": "AlphaOps v5" if strategy_id == "alphaops_v5" else "AlphaOps v4",
                    "cohort": ALPHAOPS_OFFICIAL_COHORT,
                    "decision_source": "review.watchlist",
                    "model_version": ALPHA_MODEL_VERSION,
                    "prospective_contract": strategy_id == "alphaops_v5",
                    "research_only": True,
                    "broker_execution_enabled": False,
                },
                "payload_json": {
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "model_version": ALPHA_MODEL_VERSION,
                    "cohort": ALPHAOPS_OFFICIAL_COHORT,
                    "registered_at": selected_at,
                    "research_only": True,
                },
            }
        ]
    )
    decision_name = (
        "no_trade" if decision.get("no_trade") else str(decision.get("decision_tier") or "selected")
    )
    body_sha256 = _body_sha256(event.body)
    rows: list[dict[str, Any]] = []
    for signal in selected_signals:
        signal_id = _selection_signal_id(signal, scan_id)
        if not signal_id:
            continue
        identity = f"{strategy_id}|{strategy_version}|{ALPHAOPS_OFFICIAL_COHORT}|{signal_id}"
        selection_id = f"selection:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        row = {
            "selection_id": selection_id,
            "scan_id": scan_id,
            "signal_id": signal_id,
            "ticker": str(signal.get("ticker") or "").upper(),
            "rank": signal.get("rank"),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "cohort": ALPHAOPS_OFFICIAL_COHORT,
            "decision": decision_name,
            "selected_at": selected_at,
            "event_key": event.event_key,
            "body_sha256": body_sha256,
        }
        row["payload_json"] = {
            **row,
            "decision_payload": decision,
            "signal": signal,
            "research_only": True,
            "broker_execution_enabled": False,
        }
        rows.append(row)
    cohort_row = build_official_cohort_row(
        market_date=selected_at[:10],
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        scan_id=scan_id,
        event_key=event.event_key,
        body_sha256=body_sha256,
        claimed_at=selected_at,
        selections=rows,
    )
    cohort_stats = store.persist_official_signal_cohort(cohort_row, rows)
    inserted = int(cohort_stats["inserted_members"])
    return rows, {
        "inserted": inserted,
        "skipped": len(rows) - inserted,
        "official_cohort_claimed": bool(cohort_stats["claimed"]),
        "official_cohort_id": cohort_row["official_cohort_id"],
        "official_membership_sha256": cohort_row["membership_sha256"],
        "strategy_versions_inserted": strategy_stats["inserted"],
        "strategy_versions_skipped": strategy_stats["skipped"],
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "cohort": ALPHAOPS_OFFICIAL_COHORT,
    }


def _persist_research_radar_selections(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    radar: list[dict[str, Any]],
    selected_at: str,
    event: NotificationEvent,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Persist the exact conditional radar plans represented in Telegram."""

    if not radar:
        return [], {
            "inserted": 0,
            "skipped": 0,
            "cohort": ALPHAOPS_RADAR_COHORT,
            "strategy_version": ALPHAOPS_RADAR_VERSION,
        }
    strategy_stats = store.persist_strategy_versions(
        [
            {
                "strategy_id": ALPHAOPS_RADAR_COHORT,
                "strategy_version": ALPHAOPS_RADAR_VERSION,
                "registered_at": selected_at,
                "definition_json": {
                    "name": "Dawnstrike conditional research radar",
                    "cohort": ALPHAOPS_RADAR_COHORT,
                    "minimum_reward_risk": 1.5,
                    "maximum_stop_distance_pct": 8.0,
                    "maximum_spread_pct": 3.0,
                    "research_only": True,
                    "broker_execution_enabled": False,
                },
                "payload_json": {
                    "strategy_id": ALPHAOPS_RADAR_COHORT,
                    "strategy_version": ALPHAOPS_RADAR_VERSION,
                    "registered_at": selected_at,
                    "research_only": True,
                },
            }
        ]
    )
    body_sha256 = _body_sha256(event.body)
    rows: list[dict[str, Any]] = []
    for signal in radar:
        signal_id = _selection_signal_id(signal, scan_id)
        if not signal_id:
            continue
        identity = f"{ALPHAOPS_RADAR_COHORT}|{ALPHAOPS_RADAR_VERSION}|{scan_id}|{signal_id}"
        row = {
            "selection_id": f"selection:{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
            "scan_id": scan_id,
            "signal_id": signal_id,
            "ticker": str(signal.get("ticker") or "").upper(),
            "rank": signal.get("rank"),
            "strategy_id": ALPHAOPS_RADAR_COHORT,
            "strategy_version": ALPHAOPS_RADAR_VERSION,
            "cohort": ALPHAOPS_RADAR_COHORT,
            "decision": "conditional_paper_watch",
            "selected_at": selected_at,
            "event_key": event.event_key,
            "body_sha256": body_sha256,
        }
        row["payload_json"] = {
            **row,
            "signal": signal,
            "research_only": True,
            "broker_execution_enabled": False,
        }
        rows.append(row)
    persisted = store.persist_signal_selections(rows)
    return rows, {
        **persisted,
        "cohort": ALPHAOPS_RADAR_COHORT,
        "strategy_version": ALPHAOPS_RADAR_VERSION,
        "strategy_versions_inserted": strategy_stats["inserted"],
        "strategy_versions_skipped": strategy_stats["skipped"],
    }


def _selection_signal_id(signal: dict[str, Any], scan_id: str) -> str:
    signal_id = str(signal.get("signal_id") or signal.get("signal_key") or "").strip()
    if signal_id:
        return signal_id
    ticker = str(signal.get("ticker") or "").upper()
    rank = signal.get("rank")
    if not ticker or rank in {None, ""}:
        return ""
    return f"{scan_id}:{rank}:{ticker}"


def _notification_channels(notify: str) -> list[str]:
    channels = [channel.strip().lower() for channel in notify.split(",") if channel.strip()]
    return channels or ["console"]


def _existing_notification_keys(
    store: SQLiteScanStore,
    *,
    events: list[NotificationEvent],
    notify: str,
) -> set[str]:
    existing: set[str] = set()
    channels = set(_notification_channels(notify)) | {"console"}
    for event in events:
        for channel in channels:
            notification_key = f"{event.event_key}:{channel}"
            if store.has_notification(notification_key):
                existing.add(notification_key)
    return existing


def _persist_notification_delivery_memberships(
    store: SQLiteScanStore,
    *,
    selections: list[dict[str, Any]],
    events: list[NotificationEvent],
    notify: str,
    preexisting_notification_keys: set[str],
) -> list[dict[str, Any]]:
    """Record exact members and preserve delivery truth across deduplicated runs."""

    attempted_at = utc_now_iso()
    rows: list[dict[str, Any]] = []
    for event in events:
        event_selections = [
            row for row in selections if str(row.get("event_key") or "") == event.event_key
        ]
        for channel in _notification_channels(notify):
            notification_key = f"{event.event_key}:{channel}"
            notification = store.load_notification(notification_key)
            actual_notification_key = notification_key
            if notification is None and channel == "telegram":
                console_key = f"{event.event_key}:console"
                console_notification = store.load_notification(console_key)
                if console_notification is not None and console_notification.get("dry_run"):
                    notification = console_notification
                    actual_notification_key = console_key
            stored_body = str((notification or {}).get("body") or event.body)
            if notification is None:
                delivery_status = "failed"
                delivered_at = ""
            elif notification.get("dry_run"):
                delivery_status = "dry_run"
                delivered_at = ""
            else:
                delivery_status = "delivered"
                delivered_at = str(notification.get("sent_at") or "")
            attempt_status = (
                "deduplicated"
                if actual_notification_key in preexisting_notification_keys
                else delivery_status
            )
            body_sha256 = _body_sha256(stored_body)
            for selection in event_selections:
                identity = f"{event.event_key}|{channel}|{selection['signal_id']}"
                membership_id = f"delivery:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
                delivery_row: dict[str, Any] = {
                    "membership_id": membership_id,
                    "selection_id": str(selection["selection_id"]),
                    "scan_id": str(selection["scan_id"]),
                    "signal_id": str(selection["signal_id"]),
                    "ticker": str(selection.get("ticker") or "").upper(),
                    "strategy_id": str(selection["strategy_id"]),
                    "strategy_version": str(selection["strategy_version"]),
                    "cohort": str(selection["cohort"]),
                    "decision": str(selection["decision"]),
                    "selected_at": str(selection["selected_at"]),
                    "event_key": event.event_key,
                    "channel": channel,
                    "delivery_status": delivery_status,
                    "attempted_at": attempted_at,
                    "delivered_at": delivered_at,
                    "body_sha256": body_sha256,
                }
                delivery_row["payload_json"] = {
                    **delivery_row,
                    "notification_key": notification_key,
                    "recorded_notification_key": actual_notification_key,
                    "attempt_status": attempt_status,
                    "deduplicated": attempt_status == "deduplicated",
                    "legacy_recovery": dict(event.payload or {}).get("legacy_recovery"),
                    "body": stored_body,
                    "research_only": True,
                }
                rows.append(delivery_row)
    store.persist_notification_deliveries(rows)
    event_keys = {event.event_key for event in events}
    return [
        row
        for row in store.load_notification_deliveries(
            scan_id=str(selections[0]["scan_id"]) if selections else "",
            cohort=ALPHAOPS_OFFICIAL_COHORT,
            limit=max(100, len(rows) * 2),
        )
        if row["event_key"] in event_keys
    ]


def _body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def recover_legacy_alpha_notification_memberships(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    limit: int = 5000,
) -> dict[str, int]:
    """Conservatively recover exact legacy memberships from stored rendered bodies.

    Recovery is deliberately opt-in. A watch message is accepted only when its
    rendered pick count equals the parsed ticker lines and every ticker maps to
    exactly one historical signal in that scan. Ambiguous or truncated messages
    remain unknown rather than inheriting the old, over-broad payload membership.
    """

    store = SQLiteScanStore(db_path)
    store.initialize()
    stats = {
        "notifications_scanned": 0,
        "notifications_recovered": 0,
        "memberships_recovered": 0,
        "already_exact": 0,
        "ambiguous_or_unsupported": 0,
    }
    for notification in store.load_recent_notifications(limit=limit):
        stats["notifications_scanned"] += 1
        channel = str(notification.get("channel") or "").lower()
        qualified_event_key = str(notification.get("event_key") or "")
        channel_suffix = f":{channel}"
        if (
            channel != "telegram"
            or notification.get("dry_run")
            or not qualified_event_key.endswith(channel_suffix)
        ):
            stats["ambiguous_or_unsupported"] += 1
            continue
        event_key = qualified_event_key[: -len(channel_suffix)]
        if store.load_notification_deliveries(
            event_key=event_key,
            channel=channel,
            limit=1,
        ):
            stats["already_exact"] += 1
            continue
        run_id = str(notification.get("run_id") or "")
        body = str(notification.get("body") or "")
        hint = str(notification.get("channel_hint") or "")
        tickers = _legacy_body_tickers(body, hint)
        if not run_id or not tickers:
            stats["ambiguous_or_unsupported"] += 1
            continue
        historical = store.load_historical_signals(scan_id=run_id, limit=500)
        historical_by_ticker: dict[str, list[dict[str, Any]]] = {}
        for row in historical:
            historical_by_ticker.setdefault(str(row.get("ticker") or "").upper(), []).append(row)
        matches = [historical_by_ticker.get(ticker, []) for ticker in tickers]
        if any(len(rows) != 1 for rows in matches):
            stats["ambiguous_or_unsupported"] += 1
            continue
        matched_rows = [rows[0] for rows in matches]
        recovered_signals = [_legacy_selection_signal(row) for row in matched_rows]
        generated_at_values = [
            str(row.get("generated_at") or "")
            for row in matched_rows
            if str(row.get("generated_at") or "")
        ]
        selected_at = (
            min(generated_at_values)
            if generated_at_values
            else str(notification.get("sent_at") or utc_now_iso())
        )
        event = NotificationEvent(
            event_key=event_key,
            title=str(notification.get("title") or "Dawnstrike Alpha Watch"),
            body=body,
            channel_hint=hint,
            payload={
                "run_id": run_id,
                "source": LEGACY_ALPHAOPS_STRATEGY_ID,
                "signals": recovered_signals,
                "legacy_recovery": "exact_rendered_body",
            },
        )
        selections, _ = _persist_official_selections(
            store,
            scan_id=run_id,
            selected_signals=recovered_signals,
            decision={
                "no_trade": tickers == ["NO_TRADE"],
                "decision_tier": "legacy_body_recovered",
            },
            selected_at=selected_at,
            event=event,
        )
        deliveries = _persist_notification_delivery_memberships(
            store,
            selections=selections,
            events=[event],
            notify="telegram",
            preexisting_notification_keys=set(),
        )
        signal_ids = [str(row["signal_id"]) for row in selections]
        _link_notification_events(
            store,
            scan_id=run_id,
            events=[event],
            notify="telegram",
            dry_run=False,
            signal_ids=signal_ids,
            notification_deliveries=deliveries,
        )
        stats["notifications_recovered"] += 1
        stats["memberships_recovered"] += len(deliveries)
    return stats


def _legacy_body_tickers(body: str, hint: str) -> list[str]:
    if hint == "alpha_no_trade" and "Nothing strong enough to watch" in body:
        return ["NO_TRADE"]
    if hint != "alpha_morning_watch":
        return []
    count_match = _LEGACY_PICK_COUNT_PATTERN.search(body)
    tickers = [ticker.upper() for ticker in _LEGACY_PICK_PATTERN.findall(body)]
    if (
        count_match is None
        or int(count_match.group(1)) != len(tickers)
        or len(set(tickers)) != len(tickers)
    ):
        return []
    return tickers


def _legacy_selection_signal(row: dict[str, Any]) -> dict[str, Any]:
    raw = dict(row.get("raw_payload_json") or {})
    return {
        **raw,
        "signal_id": str(row["signal_id"]),
        "signal_key": str(row.get("alpha_signal_id") or row["signal_id"]),
        "scan_id": str(row.get("scan_id") or ""),
        "ticker": str(row.get("ticker") or "").upper(),
        "rank": row.get("rank"),
        "model_version": str(row.get("model_version") or ALPHA_MODEL_VERSION),
        "timestamp": str(row.get("generated_at") or ""),
    }


def _dispatch(
    events: list[NotificationEvent],
    *,
    notify: str,
    db_path: str | Path,
    dry_run: bool,
) -> dict[str, int]:
    channels = [channel.strip().lower() for channel in notify.split(",") if channel.strip()]
    if not channels:
        channels = ["console"]
    config = load_config(database_path=Path(db_path), notifier_channels=",".join(channels))
    notifiers: list[BaseNotifier]
    if (
        dry_run
        and "telegram" in channels
        and not (config.telegram_bot_token and config.telegram_chat_id)
    ):
        notifiers = [ConsoleNotifier()]
    else:
        notifiers = build_notifiers(config)
    return dispatch_events(events, notifiers, SQLiteScanStore(db_path), dry_run=dry_run)


def _link_notification_events(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    events: list[NotificationEvent],
    notify: str,
    dry_run: bool,
    signal_ids: list[str],
    notification_deliveries: list[dict[str, Any]],
) -> dict[str, Any]:
    channels = _notification_channels(notify)
    channel = "telegram" if "telegram" in channels else (channels[0] if channels else "console")
    links: list[dict[str, Any]] = []
    any_alerted = False
    for event in events:
        event_key = f"{event.event_key}:{channel}"
        delivered_rows = [
            row
            for row in notification_deliveries
            if row.get("event_key") == event.event_key
            and row.get("channel") == channel
            and row.get("delivery_status") in {"delivered", "delivered_legacy"}
            and str(row.get("signal_id") or "") in signal_ids
        ]
        delivered_ids = sorted({str(row["signal_id"]) for row in delivered_rows})
        if not delivered_ids and signal_ids and not dry_run:
            notification = store.load_notification(event_key)
            if notification is not None and not notification.get("dry_run"):
                delivered_ids = sorted(set(signal_ids))
        was_alerted = channel == "telegram" and bool(delivered_ids)
        any_alerted = any_alerted or was_alerted
        updated = store.link_historical_signal_notification(
            scan_id=scan_id,
            telegram_event_key=event_key,
            was_alerted=was_alerted,
            signal_ids=delivered_ids,
        )
        delivery_by_signal = {str(row["signal_id"]): row for row in delivered_rows}
        if delivered_ids:
            event_timestamp = utc_now_iso()
            store.persist_signal_events(
                [
                    {
                        "event_id": (
                            f"{signal_id}:notification:"
                            f"{hashlib.sha256(event_key.encode()).hexdigest()[:16]}"
                        ),
                        "signal_id": signal_id,
                        "event_type": "TELEGRAM_SENT",
                        "event_timestamp": str(
                            delivery_by_signal.get(signal_id, {}).get("delivered_at")
                            or event_timestamp
                        ),
                        "event_price": None,
                        "source": channel,
                        "notes": "Exact notification membership linked to historical signal.",
                        "payload_json": {
                            "event_key": event_key,
                            "channel": channel,
                            "was_alerted": was_alerted,
                            "signal_id": signal_id,
                            "strategy_id": delivery_by_signal.get(signal_id, {}).get(
                                "strategy_id", LEGACY_ALPHAOPS_STRATEGY_ID
                            ),
                            "strategy_version": delivery_by_signal.get(signal_id, {}).get(
                                "strategy_version", ALPHA_MODEL_VERSION
                            ),
                            "cohort": delivery_by_signal.get(signal_id, {}).get(
                                "cohort", ALPHAOPS_OFFICIAL_COHORT
                            ),
                            "body_sha256": delivery_by_signal.get(signal_id, {}).get("body_sha256"),
                        },
                    }
                    for signal_id in delivered_ids
                ]
            )
        links.append({"updated": updated, "signal_count": len(delivered_ids)})
    return {
        "channel": channel,
        "was_alerted": any_alerted,
        "links": links,
    }


def _edge_label(signals: list[dict[str, Any]]) -> str:
    clean = [row for row in signals if row.get("can_alert")]
    if not clean:
        return "NONE"
    top = float(clean[0].get("alpha_score") or 0.0)
    if top >= 78:
        return "HIGH"
    if top >= 58:
        return "MEDIUM"
    return "LOW"


def _trust_gate_edge_label(watchlist: list[dict[str, Any]]) -> str:
    if not watchlist:
        return "NO CLEAN EDGE"
    statuses = {str(row.get("alert_gate_status") or "").upper() for row in watchlist}
    if statuses <= {"WATCH_ONLY", "NEEDS_CONFIRMATION"}:
        return "WATCH ONLY — NEEDS CONFIRMATION"
    if "NEEDS_CONFIRMATION" in statuses:
        return "NEEDS CONFIRMATION"
    return "PROBABILITY WATCH"


def _real_days(rows: list[dict[str, Any]]) -> int:
    dates = {
        str(
            row.get("market_date")
            or row.get("recommendation_timestamp")
            or row.get("timestamp")
            or row.get("created_at")
            or ""
        )[:10]
        for row in rows
        if str(
            row.get("market_date")
            or row.get("recommendation_timestamp")
            or row.get("timestamp")
            or row.get("created_at")
            or ""
        )[:10]
    }
    return len(dates)


def _signal_market_date(row: dict[str, Any]) -> str:
    return str(
        row.get("market_date")
        or row.get("recommendation_timestamp")
        or row.get("timestamp")
        or row.get("as_of_timestamp")
        or row.get("created_at")
        or ""
    )[:10]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_markdown(path: Path, summary: dict[str, Any]) -> None:
    truth = dict(summary.get("truth_report") or {})
    status = dict(summary.get("status") or {})
    review = dict(summary.get("daily_review_metrics") or {})
    lines = [
        "# Dawnstrike AlphaOps Report",
        "",
        "Research/watchlist only. No orders are placed.",
        "",
        f"- Model version: {status.get('model_version')}",
        f"- Real days collected: {truth.get('real_days_collected', 0)}",
        f"- Enough evidence: {truth.get('enough_evidence', False)}",
        f"- Sample size: {truth.get('sample_size', 0)}",
        f"- Win rate: {truth.get('win_rate_pct', 0)}%",
        f"- Median return: {truth.get('median_return_pct', 0)}%",
        f"- Outlier dependent: {dict(truth.get('outlier') or {}).get('outlier_dependent', False)}",
        "",
        "## Day Review Backfeed",
        "",
        f"- Review days: {review.get('review_day_count', 0)}",
        f"- Caught top mover rate: {_fmt_review_pct(review.get('caught_top_mover_rate'))}",
        f"- Missed winner rate: {_fmt_review_pct(review.get('missed_winner_rate'))}",
        f"- False positive rate: {_fmt_review_pct(review.get('false_positive_rate'))}",
        f"- Learning events proposed: {review.get('learning_events_proposed', 0)}",
        f"- Learning events applied: {review.get('learning_events_applied', 0)}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _alpha_daily_review_metrics(
    runs: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    mover_count = sum(int(row.get("mover_count") or 0) for row in runs)
    signal_count = sum(int(row.get("signal_count") or 0) for row in runs)
    caught = sum(int(row.get("matched_pick_count") or 0) for row in runs)
    missed = sum(int(row.get("missed_winner_count") or 0) for row in runs)
    false_positive = sum(int(row.get("false_positive_count") or 0) for row in runs)
    return {
        "review_day_count": len(runs),
        "caught_top_mover_rate": _review_pct(caught, mover_count),
        "missed_winner_rate": _review_pct(missed, mover_count),
        "false_positive_rate": _review_pct(false_positive, signal_count),
        "learning_events_proposed": len(events),
        "learning_events_applied": sum(1 for row in events if row.get("applied")),
    }


def _review_pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 4)


def _fmt_review_pct(value: Any) -> str:
    if value in {None, ""}:
        return "Outcome Needed"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "Outcome Needed"
