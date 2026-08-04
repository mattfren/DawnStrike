"""AlphaOps v4 orchestration services."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.alert_gate import apply_alert_gates
from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION, AlphaModel
from intraday_scanner.alpha.feature_factory import build_feature_vector
from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.regime_detector import detect_regime
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
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.providers.web_source_base import load_web_sources_config
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.services.alpha_official_cohort_service import (
    build_official_cohort_row,
)
from intraday_scanner.services.alpha_v6_universe_service import (
    active_alpha_v6_membership_by_ticker,
)
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
from intraday_scanner.services.web_collection_service import web_auto_collect, web_source_doctor
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

DEFAULT_DB_PATH = "data/shadow_real.sqlite"
DEFAULT_WEB_CONFIG = "config/web_sources.yaml"
ALPHAOPS_STRATEGY_ID = "alphaops_v4"
ALPHAOPS_OFFICIAL_COHORT = "official_telegram"
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
        source.enabled and bool(source.fixture_path)
        for source in source_config.sources
    )
    enrichment = enrich_premarket_rows(
        list(collection.get("rows") or []),
        config=scanner_config,
        source=("yahoo" if fixture_mode else "alpaca"),
        allow_yahoo_fallback=not fixture_mode,
        rehearsal_mode=fixture_mode,
        out_dir=output_dir / "premarket_enrichment",
    )
    source_summary["premarket_enrichment"] = enrichment["summary"]
    enriched_snapshot_path = str(
        dict(enrichment.get("paths") or {}).get("snapshot") or collection["snapshot_path"]
    )
    scan_result = ScanService(
        CsvSnapshotProvider(enriched_snapshot_path),
        store=store,
    ).run(scanner_config, persist=True)
    scan_paths = write_scan_outputs(scan_result, scanner_config.output_dir)
    ranked = [candidate.to_dict() for candidate in scan_result.ranked_candidates]
    all_candidates = [candidate.to_dict() for candidate in scan_result.all_candidates]
    timestamp = scan_result.created_at
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
    signals = apply_alert_gates(signals)
    store.persist_alpha_signals(signals)
    regime = detect_regime(signals, source_summary)
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
    review = review_alpha_signals(signals, source_summary=source_summary)
    decision = dict(review["decision"])
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
    selected_signal_ids = [str(row["signal_id"]) for row in selected_rows]
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
            selections=selected_rows,
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
        selections=selected_rows,
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
            "tracked_count": sum(
                1 for row in v6_decisions if row.get("action") == "SHADOW_TRACK"
            ),
            "persistence": v6_decision_stats,
            "versioned_universe_membership_count": len(universe_memberships),
            "missing_versioned_universe_memberships": missing_v6_universe_memberships,
            "research_only": True,
            "broker_execution_enabled": False,
        },
        "signal_count": len(signals),
        "historical_signal_count": len(historical_rows),
        "historical_notification_link": notification_link,
        "top_signal": signals[0] if signals else None,
        "review": review,
        "selection_stats": selection_stats,
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
    if exact_selections:
        selected_signal_ids = {
            str(row.get("signal_id") or "")
            for row in exact_selections
            if str(row.get("decision") or "").lower() != "no_trade"
            and str(row.get("ticker") or "").upper() != "NO_TRADE"
        }
        signals = [
            row
            for row in signals
            if str(row.get("signal_id") or row.get("signal_key") or "")
            in selected_signal_ids
        ]
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
        if bool(row.get("can_alert")) and not str(row.get("no_trade_reason") or "").strip()
    ]
    price_observation: dict[str, Any] | None = None
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
    if not active_signals:
        result: dict[str, Any] = {
            "status": "no_active_watchlist",
            "label": "NO ACTIVE WATCH",
            "message": "The latest AlphaOps run has no alertable research watchlist to monitor.",
            "tickers": [],
            "events": [],
        }
    else:
        result = monitor_alpha_signals(active_signals, current_prices=current_prices)
    if price_observation is not None:
        result["price_observation"] = {
            key: value
            for key, value in price_observation.items()
            if key != "observations"
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
    result["selection_evidence_status"] = (
        "exact_official_cohort" if exact_selections else "legacy_manual_fallback"
    )
    return result


def _monitor_event_key(scan_id: str, result: dict[str, Any]) -> str:
    states = sorted(
        f"{row.get('ticker')}:{row.get('status') or row.get('label')}"
        for row in result.get("events", [])
    )
    state_text = ";".join(states) or str(result.get("status") or "unknown")
    digest = hashlib.sha256(f"{scan_id}|{state_text}".encode()).hexdigest()[:16]
    return f"{scan_id or 'no-scan'}:{digest}"


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
    store = SQLiteScanStore(db_path)
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
    store = SQLiteScanStore(db_path)
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
            "source": "alphaops_v4",
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
) -> NotificationEvent:
    """Build an event whose structured members exactly match the rendered cohort."""

    return _notification_event(
        run_id,
        hint,
        title,
        body,
        payload={"signals": list(selected_signals)},
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
        "no_trade"
        if decision.get("no_trade")
        else str(decision.get("decision_tier") or "selected")
    )
    body_sha256 = _body_sha256(event.body)
    rows: list[dict[str, Any]] = []
    for signal in selected_signals:
        signal_id = _selection_signal_id(signal, scan_id)
        if not signal_id:
            continue
        identity = (
            f"{strategy_id}|{strategy_version}|"
            f"{ALPHAOPS_OFFICIAL_COHORT}|{signal_id}"
        )
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
                identity = (
                    f"{event.event_key}|{channel}|{selection['signal_id']}"
                )
                membership_id = (
                    f"delivery:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
                )
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
        selected_at = min(generated_at_values) if generated_at_values else str(
            notification.get("sent_at") or utc_now_iso()
        )
        event = NotificationEvent(
            event_key=event_key,
            title=str(notification.get("title") or "Dawnstrike Alpha Watch"),
            body=body,
            channel_hint=hint,
            payload={
                "run_id": run_id,
                "source": ALPHAOPS_STRATEGY_ID,
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
    if dry_run and "telegram" in channels and not (
        config.telegram_bot_token and config.telegram_chat_id
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
        delivery_by_signal = {
            str(row["signal_id"]): row for row in delivered_rows
        }
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
                            "strategy_id": ALPHAOPS_STRATEGY_ID,
                            "strategy_version": ALPHA_MODEL_VERSION,
                            "cohort": ALPHAOPS_OFFICIAL_COHORT,
                            "body_sha256": delivery_by_signal.get(signal_id, {}).get(
                                "body_sha256"
                            ),
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
