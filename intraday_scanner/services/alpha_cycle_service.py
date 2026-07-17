"""AlphaOps v4 orchestration services."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION, AlphaModel
from intraday_scanner.alpha.feature_factory import build_feature_vector
from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.regime_detector import detect_regime
from intraday_scanner.config import load_config
from intraday_scanner.errors import StorageError
from intraday_scanner.market_calendar import (
    MARKET_TIMEZONE,
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
    format_telegram_event,
    select_alpha_watch_rows,
)
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.services.alpha_paper_service import (
    ALPHAOPS_COHORT,
    ALPHAOPS_STRATEGY_ID,
    ALPHAOPS_STRATEGY_VERSION,
    alpha_reconciliation_gate,
    alpha_telegram_delivery_proof,
    freeze_alpha_telegram_cohort,
    load_alpha_official_delivered_cohort,
    persist_alpha_telegram_delivery,
)
from intraday_scanner.services.learning_service import run_alpha_learning
from intraday_scanner.services.return_attribution_service import (
    link_historical_notification,
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
DEFAULT_WEB_CONFIG = "config/web_sources.example.yaml"


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
    if _uses_external_notifications(notify):
        session = session_for_timestamp(as_of)
        phase = core_session_phase(as_of)
        if not session.is_trading_day:
            return _write_alpha_session_skip(
                output_dir=output_dir,
                cycle_name=cycle_name,
                status="skipped_market_closed",
                phase=phase,
                session=session.to_dict(),
                as_of=as_of,
            )
        if phase != "before_core_session":
            return _write_alpha_session_skip(
                output_dir=output_dir,
                cycle_name=cycle_name,
                status="skipped_outside_premarket_session",
                phase=phase,
                session=session.to_dict(),
                as_of=as_of,
            )
    store = SQLiteScanStore(db_path)
    store.initialize()
    run_market_date = _alpha_market_date(as_of)
    if _has_channel(notify, "telegram"):
        official = store.load_official_strategy_cohort(
            market_date=run_market_date.isoformat(),
            strategy_id=ALPHAOPS_STRATEGY_ID,
            strategy_version=ALPHAOPS_STRATEGY_VERSION,
            cohort=ALPHAOPS_COHORT,
        )
        if official is not None:
            return _official_cohort_already_frozen_result(
                official=official,
                cycle_name=cycle_name,
                output_dir=output_dir,
            )
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
        outcomes=_canonical_alpha_labels(store),
        previous=store.load_alpha_source_reliability(),
    )
    if source_reliability:
        store.persist_alpha_source_reliability(source_reliability)

    if collection.get("status") != "success":
        review = review_alpha_signals([], source_summary=source_summary)
        generated_at = (as_of.isoformat() if as_of is not None else utc_now_iso())
        no_data_scan_id = f"{cycle_name}:source_failure:{run_market_date.isoformat()}"
        source_failure_no_trade_row = record_no_trade_historical_signal(
            store,
            scan_id=no_data_scan_id,
            generated_at=generated_at,
            reason=str(review["decision"]["reason"]),
            source_summary=source_summary,
            candidate_count=int(source_summary.get("candidate_count") or 0),
        )
        message = format_alpha_no_trade(
            reason=str(review["decision"]["reason"]),
            next_action=str(review["decision"]["next_action"]),
        )
        events = [
            _notification_event(
                no_data_scan_id,
                "alpha_no_trade",
                "Dawnstrike Alpha Check",
                message,
            )
        ]
        source_failure_selections: list[dict[str, Any]] = []
        exact_telegram_body = ""
        if _has_channel(notify, "telegram"):
            exact_telegram_body = _expected_telegram_transmission(events[0], db_path=db_path)
            try:
                source_failure_selections = freeze_alpha_telegram_cohort(
                    store,
                    scan_id=no_data_scan_id,
                    selected_at=generated_at,
                    event_key=events[0].event_key,
                    body=exact_telegram_body,
                    rendered_rows=[],
                    no_trade_row=source_failure_no_trade_row,
                )
            except StorageError as exc:
                if "already frozen for this market date" in str(exc):
                    official = store.load_official_strategy_cohort(
                        market_date=run_market_date.isoformat(),
                        strategy_id=ALPHAOPS_STRATEGY_ID,
                        strategy_version=ALPHAOPS_STRATEGY_VERSION,
                        cohort=ALPHAOPS_COHORT,
                    )
                    if official is not None:
                        return _official_cohort_already_frozen_result(
                            official=official,
                            cycle_name=cycle_name,
                            output_dir=output_dir,
                        )
                raise
        notification_stats = _dispatch(
            events,
            notify=notify,
            db_path=db_path,
            dry_run=dry_run,
        )
        delivery_status = "not_telegram"
        source_failure_receipt: Mapping[str, Any] | None = None
        if source_failure_selections:
            proof = alpha_telegram_delivery_proof(
                store,
                event_key=events[0].event_key,
                body=exact_telegram_body,
                dry_run=dry_run,
                notification_stats=notification_stats,
            )
            delivery_status = str(proof["status"])
            raw_receipt = proof.get("transport_receipt")
            source_failure_receipt = (
                raw_receipt if isinstance(raw_receipt, Mapping) else None
            )
            persist_alpha_telegram_delivery(
                store,
                selections=source_failure_selections,
                delivery_status=delivery_status,
                transport_receipt=source_failure_receipt,
            )
        notification_link = _link_notification_events(
            store,
            scan_id=no_data_scan_id,
            events=events,
            notification_stats=notification_stats,
            notify=notify,
            dry_run=dry_run,
            signal_ids=[str(row["signal_id"]) for row in source_failure_selections],
            proven_delivery=delivery_status == "delivered",
        )
        no_data_result: dict[str, Any] = {
            "status": "no_trade",
            "run_type": cycle_name,
            "scan_id": no_data_scan_id,
            "source_summary": source_summary,
            "review": review,
            "notification_stats": notification_stats,
            "historical_notification_link": notification_link,
            "official_telegram_delivery_status": delivery_status,
            "official_telegram_selection_count": len(source_failure_selections),
            "official_telegram_transport_receipt": source_failure_receipt,
            "out_dir": str(output_dir),
        }
        _write_json(output_dir / "alpha_cycle.json", no_data_result)
        return no_data_result

    scanner_config = load_config(
        provider="csv",
        output_dir=output_dir / "scan",
        database_path=Path(db_path),
    )
    scan_result = ScanService(
        CsvSnapshotProvider(str(collection["snapshot_path"])),
        store=store,
    ).run(scanner_config, persist=True)
    scan_paths = write_scan_outputs(scan_result, scanner_config.output_dir)
    ranked = [candidate.to_dict() for candidate in scan_result.ranked_candidates]
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
        for row in ranked
    ]
    store.persist_alpha_feature_vectors(feature_vectors)
    historical_labels = _canonical_alpha_labels(store)
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
    store.persist_alpha_signals(signals)
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
    if decision.get("no_trade"):
        message = format_alpha_no_trade(
            reason=str(decision.get("reason") or ""),
            next_action=str(decision.get("next_action") or ""),
        )
        hint = "alpha_no_trade"
        title = "Dawnstrike Alpha Check"
    else:
        edge_label = (
            "PROBABILITY WATCH"
            if str(decision.get("decision_tier") or "") == "probability_fallback"
            else _edge_label(signals)
        )
        message = format_alpha_watch(
            signals=list(review["watchlist"]),
            edge_label=edge_label,
            source_summary=source_summary,
        )
        hint = "alpha_morning_watch"
        title = "Dawnstrike Alpha Watch"
    rendered_rows = (
        []
        if decision.get("no_trade")
        else select_alpha_watch_rows(list(review["watchlist"]))
    )
    events = [
        _notification_event(
            scan_result.run_id,
            hint,
            title,
            message,
            payload={
                "signals": rendered_rows,
                "rendered_signal_count": len(rendered_rows),
                "official_cohort": "official_telegram",
            },
        )
    ]
    delivery_selections: list[dict[str, Any]] = []
    exact_telegram_body = ""
    if _has_channel(notify, "telegram"):
        exact_telegram_body = _expected_telegram_transmission(events[0], db_path=db_path)
        try:
            delivery_selections = freeze_alpha_telegram_cohort(
                store,
                scan_id=scan_result.run_id,
                selected_at=timestamp,
                event_key=events[0].event_key,
                body=exact_telegram_body,
                rendered_rows=rendered_rows,
                no_trade_row=no_trade_row,
            )
        except StorageError as exc:
            if "already frozen for this market date" in str(exc):
                official = store.load_official_strategy_cohort(
                    market_date=run_market_date.isoformat(),
                    strategy_id=ALPHAOPS_STRATEGY_ID,
                    strategy_version=ALPHAOPS_STRATEGY_VERSION,
                    cohort=ALPHAOPS_COHORT,
                )
                if official is not None:
                    return _official_cohort_already_frozen_result(
                        official=official,
                        cycle_name=cycle_name,
                        output_dir=output_dir,
                    )
            raise
    try:
        notification_stats = _dispatch(
            events,
            notify=notify,
            db_path=db_path,
            dry_run=dry_run,
        )
    except Exception:
        if delivery_selections:
            persist_alpha_telegram_delivery(
                store,
                selections=delivery_selections,
                delivery_status="failed",
            )
        raise
    delivery_status = "not_telegram"
    delivery_receipt: Mapping[str, Any] | None = None
    delivery_stats: dict[str, int] = {"inserted": 0, "updated": 0, "skipped": 0}
    if delivery_selections:
        proof = alpha_telegram_delivery_proof(
            store,
            event_key=events[0].event_key,
            body=exact_telegram_body,
            dry_run=dry_run,
            notification_stats=notification_stats,
        )
        delivery_status = str(proof["status"])
        raw_receipt = proof.get("transport_receipt")
        delivery_receipt = raw_receipt if isinstance(raw_receipt, Mapping) else None
        delivery_stats = persist_alpha_telegram_delivery(
            store,
            selections=delivery_selections,
            delivery_status=delivery_status,
            transport_receipt=delivery_receipt,
        )
    notification_link = _link_notification_events(
        store,
        scan_id=scan_result.run_id,
        events=events,
        notification_stats=notification_stats,
        notify=notify,
        dry_run=dry_run,
        signal_ids=[str(row["signal_id"]) for row in delivery_selections],
        proven_delivery=delivery_status == "delivered",
    )
    regime = detect_regime(signals, source_summary)
    result: dict[str, Any] = {
        "status": "complete" if not decision.get("no_trade") else "no_trade",
        "run_type": cycle_name,
        "scan_id": scan_result.run_id,
        "model_version": ALPHA_MODEL_VERSION,
        "source_summary": source_summary,
        "source_reliability": source_reliability,
        "regime": regime,
        "feature_vector_count": len(feature_vectors),
        "signal_count": len(signals),
        "historical_signal_count": len(historical_rows),
        "historical_notification_link": notification_link,
        "official_telegram_delivery_status": delivery_status,
        "official_telegram_selection_count": len(delivery_selections),
        "official_telegram_delivery_stats": delivery_stats,
        "official_telegram_transport_receipt": delivery_receipt,
        "top_signal": signals[0] if signals else None,
        "review": review,
        "notification_stats": notification_stats,
        "scan_paths": {key: str(value) for key, value in scan_paths.items()},
        "out_dir": str(output_dir),
    }
    _write_json(output_dir / "alpha_cycle.json", result)
    _write_json(output_dir / "alpha_signals.json", signals)
    _write_json(output_dir / "alpha_features.json", feature_vectors)
    return result


def alpha_monitor(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    notify: str = "console",
    dry_run: bool = False,
    current_prices: dict[str, float | Mapping[str, Any]] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    if _uses_external_notifications(notify):
        session = session_for_timestamp(as_of)
        phase = core_session_phase(as_of)
        if phase != "core_session_open":
            return {
                "status": "skipped_outside_core_session",
                "phase": phase,
                "session_gate": session.to_dict(),
                "as_of": as_of.isoformat() if as_of is not None else utc_now_iso(),
                "notification_stats": {"sent": 0, "skipped": 0, "failed": 0},
                "research_only": True,
                "order_execution_enabled": False,
            }
    run_date = _alpha_market_date(as_of)
    store = SQLiteScanStore(db_path)
    store.initialize()
    selections, cohort_blockers = load_alpha_official_delivered_cohort(
        store,
        market_date=run_date,
    )
    official = store.load_official_strategy_cohort(
        market_date=run_date.isoformat(),
        strategy_id=ALPHAOPS_STRATEGY_ID,
        strategy_version=ALPHAOPS_STRATEGY_VERSION,
        cohort=ALPHAOPS_COHORT,
    )
    if cohort_blockers or official is None:
        return {
            "status": "blocked_incomplete",
            "market_date": run_date.isoformat(),
            "blocked_reasons": cohort_blockers or ["official cohort lock is absent"],
            "notification_stats": {"sent": 0, "skipped": 0, "deliveries": []},
            "research_only": True,
            "order_execution_enabled": False,
        }
    if len(selections) == 1 and selections[0].get("decision") == "no_trade":
        return {
            "status": "no_trade",
            "market_date": run_date.isoformat(),
            "official_cohort_id": official["official_cohort_id"],
            "message": "The proven official cohort is an explicit NO_TRADE day.",
            "notification_stats": {"sent": 0, "skipped": 0, "deliveries": []},
            "research_only": True,
            "order_execution_enabled": False,
        }
    signals = [_selection_signal_snapshot(row) for row in selections]
    prices, quote_refs, quote_blockers = _proven_monitor_prices(
        store,
        selections=selections,
        market_date=run_date,
        supplied=current_prices,
    )
    cohort_key = str(official["official_cohort_id"])
    if quote_blockers:
        blocked_digest = hashlib.sha256(
            "\x1f".join(sorted(quote_blockers)).encode("utf-8")
        ).hexdigest()[:16]
        message = "AlphaOps monitor blocked: " + "; ".join(quote_blockers)
        event = NotificationEvent(
            event_key=(
                f"alphaops-monitor:{run_date.isoformat()}:{cohort_key.split(':')[-1][:12]}:"
                f"blocked:{blocked_digest}"
            ),
            title="Dawnstrike Alpha Monitor Blocked",
            body=message,
            channel_hint="alpha_monitor_blocked",
            payload={
                "run_id": cohort_key,
                "market_date": run_date.isoformat(),
                "official_cohort_id": cohort_key,
                "research_only": True,
            },
        )
        return {
            "status": "blocked_incomplete",
            "market_date": run_date.isoformat(),
            "official_cohort_id": cohort_key,
            "blocked_reasons": quote_blockers,
            "blocked_symbols": sorted(
                str(row.get("ticker") or "")
                for row in selections
                if str(row.get("ticker") or "") not in prices
            ),
            "notification_stats": _dispatch(
                [event], notify=notify, db_path=db_path, dry_run=dry_run
            ),
            "research_only": True,
            "order_execution_enabled": False,
        }
    result = monitor_alpha_signals(signals, current_prices=prices)
    result["market_date"] = run_date.isoformat()
    result["official_cohort_id"] = cohort_key
    result["quote_source_refs"] = quote_refs
    result["historical_event_stats"] = record_monitor_signal_events(
        store,
        signals=signals,
        monitor_events=list(result.get("events") or []),
    )
    message = format_alpha_monitor(result)
    event_digest = hashlib.sha256(
        json.dumps(
            {
                "events": result.get("events") or [],
                "quote_source_refs": quote_refs,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    events = [
        NotificationEvent(
            event_key=(
                f"alphaops-monitor:{run_date.isoformat()}:{cohort_key.split(':')[-1][:12]}:"
                f"{event_digest}"
            ),
            title="Dawnstrike Alpha Monitor",
            body=message,
            channel_hint="alpha_monitor",
            payload={
                "run_id": cohort_key,
                "market_date": run_date.isoformat(),
                "official_cohort_id": cohort_key,
                "quote_source_refs": quote_refs,
                "research_only": True,
            },
        )
    ]
    result["notification_stats"] = _dispatch(
        events,
        notify=notify,
        db_path=db_path,
        dry_run=dry_run,
    )
    return result


def _uses_external_notifications(notify: str) -> bool:
    channels = {channel.strip().lower() for channel in notify.split(",") if channel.strip()}
    return bool(channels - {"console"})


def _has_channel(notify: str, channel: str) -> bool:
    return channel.lower() in {
        value.strip().lower() for value in notify.split(",") if value.strip()
    }


def _alpha_market_date(as_of: datetime | None) -> date:
    timestamp = as_of or datetime.now(MARKET_TIMEZONE)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("AlphaOps as_of must include a timezone offset")
    return timestamp.astimezone(MARKET_TIMEZONE).date()


def _official_cohort_already_frozen_result(
    *,
    official: Mapping[str, Any],
    cycle_name: str,
    output_dir: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "official_cohort_already_frozen",
        "run_type": cycle_name,
        "market_date": str(official.get("market_date") or ""),
        "scan_id": str(official.get("scan_id") or ""),
        "official_cohort_id": str(official.get("official_cohort_id") or ""),
        "event_key": str(official.get("event_key") or ""),
        "membership_sha256": str(official.get("membership_sha256") or ""),
        "notification_stats": {"sent": 0, "skipped": 1, "deliveries": []},
        "message": (
            "One official AlphaOps cohort is already frozen for this market date; "
            "the retry did not rescan, replace membership, or send another message."
        ),
        "out_dir": str(output_dir),
        "research_only": True,
        "order_execution_enabled": False,
    }
    _write_json(output_dir / "alpha_cycle.json", result)
    return result


def _write_alpha_session_skip(
    *,
    output_dir: Path,
    cycle_name: str,
    status: str,
    phase: str,
    session: dict[str, object],
    as_of: datetime | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "run_type": cycle_name,
        "phase": phase,
        "session_gate": session,
        "as_of": as_of.isoformat() if as_of is not None else utc_now_iso(),
        "notification_stats": {"sent": 0, "skipped": 0, "failed": 0},
        "out_dir": str(output_dir),
        "research_only": True,
        "order_execution_enabled": False,
    }
    _write_json(output_dir / "alpha_session_gate.json", payload)
    return payload


def alpha_outcomes(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    return {
        "status": "blocked_incomplete",
        "reason": (
            "alpha-outcomes is a disabled legacy alias; use alpha-paper-reconcile "
            "with complete sourced RTH bars, then alpha-learn"
        ),
        "db_path": str(db_path),
        "research_only": True,
        "order_execution_enabled": False,
    }


def alpha_learn(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    market_date: str | None = None,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    store.initialize()
    target_date = market_date or _latest_real_alpha_delivery_date(store)
    if not target_date:
        return {
            "status": "blocked_incomplete",
            "reason": "no proven real official Telegram delivery is available to learn from",
            "market_date": None,
            "research_only": True,
            "order_execution_enabled": False,
        }
    allowed, reason = alpha_reconciliation_gate(store, market_date=target_date)
    if not allowed:
        return {
            "status": "blocked_incomplete",
            "reason": reason,
            "market_date": target_date,
            "research_only": True,
            "order_execution_enabled": False,
        }
    return run_alpha_learning(store)


def _latest_real_alpha_delivery_date(store: SQLiteScanStore) -> str | None:
    rows = store.load_notification_deliveries(
        channel="telegram",
        cohort=ALPHAOPS_COHORT,
        limit=50_000,
    )
    dates: list[str] = []
    for row in rows:
        if row.get("delivery_status") != "delivered":
            continue
        try:
            parsed = datetime.fromisoformat(
                str(row.get("selected_at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        dates.append(parsed.astimezone(MARKET_TIMEZONE).date().isoformat())
    return max(dates) if dates else None


def alpha_status(*, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    store.initialize()
    latest_scan = store.load_latest_scan()
    signals = store.load_alpha_signals(limit=20)
    labels = _canonical_alpha_labels(store)
    learning = store.load_alpha_learning_runs(limit=1)
    reliability = store.load_alpha_source_reliability()
    setup_memory = store.load_alpha_setup_memory()
    real_days = _real_days(labels)
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
        "enough_evidence": real_days >= 20,
        "last_learning_run": learning[0] if learning else None,
        "research_only": True,
        "order_execution_enabled": False,
    }


def alpha_doctor(
    *,
    config_path: str | Path = DEFAULT_WEB_CONFIG,
    out_dir: str | Path = "outputs/alpha_doctor",
) -> dict[str, Any]:
    result = web_source_doctor(config_path=config_path, out_dir=out_dir, print_rows=False)
    result["alphaops_checks"] = {
        "research_only": True,
        "order_execution": "not implemented",
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
    labels = _canonical_alpha_labels(store)
    real_days = _real_days(labels)
    truth = build_truth_report(labels, real_days_collected=real_days)
    status = alpha_status(db_path=db_path)
    summary = {
        "created_at": utc_now_iso(),
        "status": status,
        "truth_report": truth,
        "source_reliability": store.load_alpha_source_reliability(),
        "setup_memory": store.load_alpha_setup_memory(),
        "alpha_summary_message": format_alpha_summary({"truth_report": truth}),
    }
    _write_json(output_dir / "alpha_report.json", summary)
    _write_markdown(output_dir / "alpha_report.md", summary)
    return {**summary, "out_dir": str(output_dir)}


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


def _dispatch(
    events: list[NotificationEvent],
    *,
    notify: str,
    db_path: str | Path,
    dry_run: bool,
) -> dict[str, Any]:
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
    return dispatch_events(
        events,
        notifiers,
        SQLiteScanStore(db_path),
        dry_run=dry_run,
        capture_transport_receipts=True,
    )


def _expected_telegram_transmission(
    event: NotificationEvent,
    *,
    db_path: str | Path,
) -> str:
    config = load_config(database_path=Path(db_path), notifier_channels="telegram")
    if config.telegram_message_style == "legacy":
        return f"{event.title}\n{event.body}"
    return format_telegram_event(
        event,
        max_morning_chars=config.telegram_max_morning_chars,
        max_alert_chars=config.telegram_max_alert_chars,
        max_summary_chars=config.telegram_max_summary_chars,
        include_debug_fields=config.telegram_include_debug_fields,
    )


def _selection_signal_snapshot(selection: Mapping[str, Any]) -> dict[str, Any]:
    payload = selection.get("payload_json")
    snapshot = payload.get("signal_snapshot") if isinstance(payload, Mapping) else None
    if not isinstance(snapshot, Mapping):
        raise StorageError(
            f"{selection.get('ticker')}: official cohort has no frozen signal snapshot"
        )
    return {
        **dict(snapshot),
        "signal_id": str(selection.get("signal_id") or ""),
        "signal_key": str(selection.get("signal_id") or ""),
        "ticker": str(selection.get("ticker") or ""),
        "rank": selection.get("rank"),
    }


def _proven_monitor_prices(
    store: SQLiteScanStore,
    *,
    selections: list[dict[str, Any]],
    market_date: date,
    supplied: dict[str, float | Mapping[str, Any]] | None,
) -> tuple[dict[str, float], list[dict[str, Any]], list[str]]:
    prices: dict[str, float] = {}
    source_refs: list[dict[str, Any]] = []
    blockers: list[str] = []
    supplied_rows = {str(key).upper(): value for key, value in (supplied or {}).items()}
    for selection in selections:
        ticker = str(selection.get("ticker") or "").upper()
        signal_id = str(selection.get("signal_id") or "")
        quote: Mapping[str, Any] | None = None
        raw_supplied = supplied_rows.get(ticker)
        if raw_supplied is not None:
            if not isinstance(raw_supplied, Mapping):
                blockers.append(f"{ticker}: supplied current price has no source lineage")
                continue
            quote = raw_supplied
        else:
            observations = store.load_price_observations(
                market_date=market_date.isoformat(),
                signal_id=signal_id,
                usable_only=True,
                limit=10,
            )
            if not observations:
                observations = store.load_price_observations(
                    market_date=market_date.isoformat(),
                    ticker=ticker,
                    usable_only=True,
                    limit=10,
                )
            quote = observations[0] if observations else None
        if quote is None:
            blockers.append(f"{ticker}: no usable real sourced current-day quote")
            continue
        observed_at = str(quote.get("observed_at") or "")
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            blockers.append(f"{ticker}: sourced quote observed_at is invalid")
            continue
        if observed.tzinfo is None or observed.utcoffset() is None:
            blockers.append(f"{ticker}: sourced quote observed_at has no timezone")
            continue
        if observed.astimezone(MARKET_TIMEZONE).date() != market_date:
            blockers.append(f"{ticker}: sourced quote is not from the current market date")
            continue
        source = str(quote.get("source") or quote.get("provider") or "").strip()
        if not source or quote.get("is_usable") is not True:
            blockers.append(f"{ticker}: quote source/usable proof is incomplete")
            continue
        raw_price = quote.get("price")
        if raw_price is None:
            blockers.append(f"{ticker}: sourced quote price is invalid")
            continue
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            blockers.append(f"{ticker}: sourced quote price is invalid")
            continue
        if price <= 0:
            blockers.append(f"{ticker}: sourced quote price is non-positive")
            continue
        prices[ticker] = price
        source_refs.append(
            {
                "ticker": ticker,
                "signal_id": signal_id,
                "observation_id": str(quote.get("observation_id") or ""),
                "source": source,
                "provider": str(quote.get("provider") or ""),
                "observed_at": observed.isoformat(),
                "price_type": str(quote.get("price_type") or ""),
                "price": price,
            }
        )
    return prices, source_refs, blockers


def _link_notification_events(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    events: list[NotificationEvent],
    notification_stats: Mapping[str, Any],
    notify: str,
    dry_run: bool,
    signal_ids: list[str] | None = None,
    proven_delivery: bool | None = None,
) -> dict[str, Any]:
    channels = [channel.strip().lower() for channel in notify.split(",") if channel.strip()]
    channel = "telegram" if "telegram" in channels else (channels[0] if channels else "console")
    if channel != "telegram":
        # The official learning cohort is Telegram-only, but other transports
        # still need their legacy audit linkage.  Let the scan id select those
        # historical rows instead of passing the intentionally empty official
        # Telegram selection set.
        if not signal_ids:
            signal_ids = None
        proven_delivery = None
    was_alerted = (
        proven_delivery
        if proven_delivery is not None
        else (not dry_run)
        and (
            int(notification_stats.get("sent") or 0) > 0
            or int(notification_stats.get("skipped") or 0) > 0
        )
    )
    links = [
        link_historical_notification(
            store,
            scan_id=scan_id,
            event_key=f"{event.event_key}:{channel}",
            was_alerted=was_alerted,
            channel=channel,
            signal_ids=signal_ids,
        )
        for event in events
    ]
    return {
        "channel": channel,
        "was_alerted": was_alerted,
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


def _real_days(rows: list[dict[str, Any]]) -> int:
    dates = {
        str(row.get("created_at") or row.get("timestamp") or "")[:10]
        for row in rows
        if str(row.get("created_at") or row.get("timestamp") or "")[:10]
    }
    return len(dates)


def _canonical_alpha_labels(store: SQLiteScanStore) -> list[dict[str, Any]]:
    return [
        row
        for row in store.load_alpha_outcome_labels(limit=50_000)
        if row.get("label_source") == "strategy_learning"
        and row.get("forward_observation") is True
        and row.get("after_cost") is True
    ]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_markdown(path: Path, summary: dict[str, Any]) -> None:
    truth = dict(summary.get("truth_report") or {})
    status = dict(summary.get("status") or {})
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
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
