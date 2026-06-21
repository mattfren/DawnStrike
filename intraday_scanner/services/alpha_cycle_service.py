"""AlphaOps v4 orchestration services."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION, AlphaModel
from intraday_scanner.alpha.feature_factory import build_feature_vector
from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.regime_detector import detect_regime
from intraday_scanner.config import load_config
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
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.services.learning_service import run_alpha_learning
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
) -> dict[str, Any]:
    return alpha_cycle(
        config_path=config_path,
        db_path=db_path,
        out_dir=out_dir,
        notify=notify,
        dry_run=dry_run,
        cycle_name="alpha_morning",
    )


def alpha_cycle(
    *,
    config_path: str | Path = DEFAULT_WEB_CONFIG,
    db_path: str | Path = DEFAULT_DB_PATH,
    out_dir: str | Path = "outputs/alpha_cycle",
    notify: str = "console",
    dry_run: bool = False,
    cycle_name: str = "alpha_cycle",
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    store.initialize()
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
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
        outcomes=store.load_alpha_outcome_labels(limit=5000),
        previous=store.load_alpha_source_reliability(),
    )
    if source_reliability:
        store.persist_alpha_source_reliability(source_reliability)

    if collection.get("status") != "success":
        review = review_alpha_signals([], source_summary=source_summary)
        message = format_alpha_no_trade(
            reason=str(review["decision"]["reason"]),
            next_action=str(review["decision"]["next_action"]),
        )
        events = [
            _notification_event(cycle_name, "alpha_no_trade", "Dawnstrike Alpha Check", message)
        ]
        notification_stats = _dispatch(events, notify=notify, db_path=db_path, dry_run=dry_run)
        no_data_result: dict[str, Any] = {
            "status": "no_trade",
            "run_type": cycle_name,
            "source_summary": source_summary,
            "review": review,
            "notification_stats": notification_stats,
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
    historical_labels = store.load_alpha_outcome_labels(limit=5000)
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
    if decision.get("no_trade"):
        message = format_alpha_no_trade(
            reason=str(decision.get("reason") or ""),
            next_action=str(decision.get("next_action") or ""),
        )
        hint = "alpha_no_trade"
        title = "Dawnstrike Alpha Check"
    else:
        edge_label = _edge_label(signals)
        message = format_alpha_watch(
            signals=list(review["watchlist"]),
            edge_label=edge_label,
            source_summary=source_summary,
        )
        hint = "alpha_morning_watch"
        title = "Dawnstrike Alpha Watch"
    events = [
        _notification_event(
            scan_result.run_id,
            hint,
            title,
            message,
            payload={"signals": signals[:5]},
        )
    ]
    notification_stats = _dispatch(events, notify=notify, db_path=db_path, dry_run=dry_run)
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
    current_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    signals = store.load_alpha_signals(limit=25)
    result = monitor_alpha_signals(signals, current_prices=current_prices)
    message = format_alpha_monitor(result)
    event_key = (
        "manual"
        if result.get("status") == "manual_monitor_required"
        else uuid.uuid4().hex[:12]
    )
    events = [_notification_event("alpha_monitor", event_key, "Dawnstrike Alpha Monitor", message)]
    result["notification_stats"] = _dispatch(
        events,
        notify=notify,
        db_path=db_path,
        dry_run=dry_run,
    )
    return result


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
    labels = store.load_alpha_outcome_labels(limit=5000)
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
        "orders_enabled": False,
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
    labels = store.load_alpha_outcome_labels(limit=5000)
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
