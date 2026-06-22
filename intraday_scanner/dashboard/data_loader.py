"""Dashboard data loading from files or SQLite."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.dashboard.components import display_pick_from_raw
from intraday_scanner.dashboard.display_text import (
    evidence_status,
    no_trade_reason,
    source_label,
    translate_list,
)
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.reporting import read_csv_dicts, read_scan_summary
from intraday_scanner.scoring import score_universe
from intraday_scanner.services.e2e_automation_service import automation_status
from intraday_scanner.services.screener_automation import screener_automation_status
from intraday_scanner.services.web_collection_service import web_automation_status
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

CALENDAR_RETURN_POLICIES = {
    "scenario_1m": "price_1m_return",
    "scenario_5m": "price_5m_return",
    "scenario_15m": "price_15m_return",
    "lunch": "lunch_return",
    "close": "close_return",
    "monitor_exit_if_available": "monitor_exit_return",
    "recommended_exit_if_recorded": "recommended_exit_return",
}

_RESEARCH_LABELS = {
    "ENTRY WATCH",
    "WATCH",
    "WATCH ONLY",
    "BREAKOUT WATCH",
    "HIGH VOLATILITY WATCH",
    "CAUTION",
    "AVOID",
    "NO CLEAN EDGE",
    "EXIT SIGNAL",
    "INVALIDATED",
    "THESIS BROKEN",
    "OUTCOME NEEDED",
}


def load_sample_scan(snapshot_path: str | Path, config: ScannerConfig) -> dict[str, Any]:
    rows = read_snapshot_csv(snapshot_path)
    result = score_universe(rows, config)
    payload = {
        "data_source_kind": "sample_fixture",
        "summary": result.summary(),
        "ranked_candidates": [candidate.to_dict() for candidate in result.ranked_candidates],
        "top_explosive": [candidate.to_dict() for candidate in result.top_explosive],
        "avoid_list": [candidate.to_dict() for candidate in result.avoid_list],
        "scan_history": [result.summary()],
    }
    _attach_display_ready(payload)
    return payload


def load_output_dir(out_dir: str | Path) -> dict[str, Any]:
    base = Path(out_dir)
    payload = {
        "data_source_kind": "output_files",
        "summary": read_scan_summary(base / "scan_summary.json").get("summary", {}),
        "ranked_candidates": read_csv_dicts(base / "ranked_candidates.csv"),
        "top_explosive": read_csv_dicts(base / "top_explosive.csv"),
        "avoid_list": read_csv_dicts(base / "avoid_list.csv"),
        "scan_history": [],
    }
    _attach_display_ready(payload)
    return payload


def load_sqlite(db_path: str | Path) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    latest = store.load_latest_scan() or {
        "summary": {},
        "ranked_candidates": [],
        "top_explosive": [],
        "avoid_list": [],
    }
    provider_health = store.load_provider_health()
    latest["scan_history"] = store.load_scan_history()
    latest["provider_health"] = provider_health
    latest["provider_health_counts"] = _provider_health_counts(provider_health)
    latest["performance_report"] = store.load_latest_performance_report() or {}
    latest["shadow_report"] = store.load_latest_shadow_report() or {}
    latest["manual_snapshot_uploads"] = store.load_manual_snapshot_uploads()
    latest["manual_outcomes"] = store.load_manual_outcomes()
    latest["manual_audit_trades"] = store.load_manual_audit_trades()
    latest["manual_audit_summary"] = store.load_latest_manual_audit_summary() or {}
    latest["screener_automation_runs"] = store.load_screener_automation_runs()
    latest["screener_automation_status"] = screener_automation_status(store=store)
    latest["automation_runs"] = store.load_automation_runs()
    latest["recent_notifications"] = store.load_recent_notifications()
    latest["automation_status"] = automation_status(store)
    latest["web_automation_status"] = web_automation_status(store)
    latest["alpha_signals"] = store.load_alpha_signals(limit=50)
    latest["alpha_feature_vectors"] = store.load_alpha_feature_vectors(limit=100)
    latest["alpha_outcome_labels"] = store.load_alpha_outcome_labels(limit=5000)
    latest["alpha_source_reliability"] = store.load_alpha_source_reliability()
    latest["alpha_setup_memory"] = store.load_alpha_setup_memory()
    latest["alpha_learning_runs"] = store.load_alpha_learning_runs(limit=5)
    latest["historical_signals"] = store.load_historical_signals(limit=500)
    latest["signal_events"] = store.load_signal_events(limit=500)
    latest["signal_outcomes"] = store.load_signal_outcomes(limit=500)
    latest["signal_return_attribution"] = store.load_signal_return_attribution(limit=500)
    latest["daily_signal_performance"] = store.load_daily_signal_performance(limit=500)
    latest["recent_alerts"] = store.load_recent_alerts()
    latest["monitor_events"] = store.load_recent_monitor_events()
    latest["recommendation_history"] = store.load_recommendation_theses()
    latest["audit_trades"] = store.load_paper_audit_trades()
    calendar_start, calendar_end = _default_calendar_range(db_path)
    latest["calendar_start_date"] = calendar_start
    latest["calendar_end_date"] = calendar_end
    latest["calendar_days"] = load_calendar_days(db_path, calendar_start, calendar_end)
    latest["calendar_daily_returns"] = load_calendar_daily_returns(
        db_path, calendar_start, calendar_end
    )
    latest["calendar_equity_curve"] = load_calendar_equity_curve(
        db_path, calendar_start, calendar_end
    )
    latest["calendar_missing_outcomes"] = load_calendar_missing_outcomes(
        db_path, calendar_start, calendar_end
    )
    raw_config = latest.get("config")
    raw_summary = latest.get("summary")
    config = dict(raw_config) if isinstance(raw_config, dict) else {}
    summary = dict(raw_summary) if isinstance(raw_summary, dict) else {}
    latest["data_source_kind"] = (
        config.get("data_source_kind")
        or summary.get("data_source_kind")
        or "sqlite_live_or_persisted"
    )
    latest["shadow_mode"] = bool(config.get("shadow_mode") or summary.get("shadow_mode"))
    latest["live_readiness"] = _live_readiness(latest)
    _attach_display_ready(latest, db_path=db_path)
    return latest


def _attach_display_ready(payload: dict[str, Any], db_path: str | Path | None = None) -> None:
    ranked = list(payload.get("ranked_candidates") or [])
    top = list(payload.get("top_explosive") or [])
    signals = list(payload.get("alpha_signals") or [])
    clean_signals = [
        row
        for row in signals
        if row.get("can_alert", True) and not row.get("no_trade_reason")
    ]
    pick_source = top or ranked or clean_signals
    top_three = [display_pick_from_raw(row) for row in pick_source[:3]]
    main_pick = top_three[0] if top_three else None
    missing = list(payload.get("calendar_missing_outcomes") or [])
    risk_summary = _display_risk_summary(payload, missing)
    system_health = _display_system_health(payload, db_path)
    latest_status = _display_latest_status(payload, top_three, missing, system_health)
    payload["latest_status"] = latest_status
    payload["main_pick"] = main_pick
    payload["top_three"] = top_three
    payload["risk_summary"] = risk_summary
    payload["next_steps"] = _display_next_steps(payload, top_three, missing, system_health)
    payload["missing_outcomes"] = missing
    payload["performance_summary"] = _display_performance_summary(payload)
    payload["system_health"] = system_health
    payload["calendar_days_simple"] = [
        _display_calendar_day(row) for row in list(payload.get("calendar_days") or [])
    ]


def _display_latest_status(
    payload: dict[str, Any],
    top_three: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    system_health: dict[str, Any],
) -> dict[str, str]:
    if system_health.get("source_problem"):
        return {
            "kind": "source_problem",
            "variant": "red",
            "title": "🔴 Data Source Problem",
            "explanation": (
                "The latest source check failed. Verify data before reviewing watch levels."
            ),
        }
    if missing:
        return {
            "kind": "outcome_needed",
            "variant": "amber",
            "title": "📥 Outcome Data Needed",
            "explanation": (
                "Some saved picks are missing outcome rows. Returns stay pending until imported."
            ),
        }
    if top_three:
        if len(top_three) >= 3:
            return {
                "kind": "clean_watchlist",
                "variant": "green",
                "title": "🚀 Clean Watchlist Found",
                "explanation": (
                    "Dawnstrike found 3 high-volatility names. Watch the levels manually. "
                    "No orders are placed."
                ),
            }
        return {
            "kind": "watch_only",
            "variant": "yellow",
            "title": "👀 Watch Only / Needs Confirmation",
            "explanation": (
                "Dawnstrike found a short watchlist. Wait for the watch levels and verify risk."
            ),
        }
    reason = _latest_no_trade_reason(payload)
    return {
        "kind": "no_clean_edge",
        "variant": "gray",
        "title": "⚠️ No Clean Edge Today",
        "explanation": reason,
    }


def _display_risk_summary(
    payload: dict[str, Any],
    missing: list[dict[str, Any]],
) -> dict[str, Any]:
    avoid = list(payload.get("avoid_list") or [])
    warnings = 0
    for row in [*list(payload.get("ranked_candidates") or []), *avoid]:
        warning_text = str(
            row.get("coverage_warning")
            or row.get("data_warnings")
            or row.get("warning")
            or ""
        )
        if warning_text:
            warnings += 1
    return {
        "avoid_count": len(avoid),
        "top_avoid_reason": _main_risk(avoid[0]) if avoid else "No hard risk flags",
        "data_warning_count": warnings,
        "missing_outcome_count": len(missing),
    }


def _display_next_steps(
    payload: dict[str, Any],
    top_three: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    system_health: dict[str, Any],
) -> list[dict[str, Any]]:
    notifications = list(payload.get("recent_notifications") or [])
    telegram_sent = any(
        str(row.get("channel") or "").lower() == "telegram"
        or "telegram" in str(row.get("event_key") or "").lower()
        for row in notifications
    )
    steps: list[dict[str, Any]] = [
        {
            "label": "Scan ran",
            "detail": "Latest saved scan loaded" if _has_scan(payload) else "Run the scan",
            "done": _has_scan(payload),
        },
        {
            "label": "Data source worked",
            "detail": source_label(system_health.get("data_source_kind")),
            "done": not system_health.get("source_problem"),
        },
        {
            "label": "Telegram sent",
            "detail": "Latest notice found" if telegram_sent else "No Telegram notice found",
            "done": telegram_sent,
        },
        {
            "label": "Watch levels manually",
            "detail": "Use Watch Level and Exit Line" if top_three else "No watch levels today",
            "done": False,
        },
    ]
    if missing:
        first = missing[0]
        steps.append(
            {
                "label": "Add outcome file after close",
                "detail": str(first.get("expected_path") or "data\\inbox\\outcomes"),
                "done": False,
            }
        )
    return steps


def _display_performance_summary(payload: dict[str, Any]) -> dict[str, Any]:
    days = list(payload.get("calendar_days") or [])
    audited = [row for row in days if row.get("status") == "AUDITED"]
    equity = list(payload.get("calendar_equity_curve") or [])
    latest_curve = dict(equity[-1]) if equity else {}
    report = dict(payload.get("performance_report") or {})
    return {
        "real_days": sum(1 for row in days if row.get("status") != "NO DATA"),
        "audited_days": len(audited),
        "top1_return": latest_curve.get("top1_compounded_return"),
        "top3_return": latest_curve.get("top3_compounded_return"),
        "top5_return": latest_curve.get("top5_compounded_return"),
        "win_rate": report.get("hit_rate_close_pct") or _calendar_hit_rate(days),
        "worst_drawdown": report.get("max_drawdown_pct") or _calendar_worst(days),
        "evidence_status": evidence_status(len(audited)),
    }


def _display_system_health(
    payload: dict[str, Any],
    db_path: str | Path | None,
) -> dict[str, Any]:
    source_problem = _has_source_problem(payload)
    summary = dict(payload.get("summary") or {})
    config = dict(payload.get("config") or {})
    return {
        "db_path": str(db_path or ""),
        "data_source_kind": payload.get("data_source_kind") or config.get("data_source_kind"),
        "source_problem": source_problem,
        "status": "Data source problem" if source_problem else "Ready",
        "data_quality": source_label(
            payload.get("data_source_kind") or config.get("data_source_kind")
        ),
        "model_version": summary.get("model_version") or config.get("model_version") or "",
        "config_hash": summary.get("config_hash") or config.get("config_hash") or "",
        "shadow_mode": bool(payload.get("shadow_mode")),
    }


def _display_calendar_day(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "NO DATA")
    labels = {
        "NO DATA": "No data",
        "NO TRADE": "No trade",
        "PICKS PENDING OUTCOMES": "Picks pending",
        "OUTCOMES PARTIAL": "Partial outcomes",
        "AUDITED": "Audited",
        "SOURCE FAILURE": "Data problem",
    }
    top3 = _number_or_none(row.get("top3_return"))
    audited_class = (
        "audited-positive"
        if top3 is None or top3 >= 0
        else "audited-negative"
    )
    css = {
        "AUDITED": audited_class,
        "PICKS PENDING OUTCOMES": "pending",
        "OUTCOMES PARTIAL": "partial",
        "NO TRADE": "empty",
        "NO DATA": "empty",
        "SOURCE FAILURE": "empty",
    }.get(status, "empty")
    return {
        **row,
        "status_label": labels.get(status, status.title()),
        "status_class": css,
        "top_pick": row.get("top_pick") or "None",
        "top3_return_label": f"{top3:+.2f}%" if top3 is not None else "Pending",
    }


def _latest_no_trade_reason(payload: dict[str, Any]) -> str:
    signals = list(payload.get("alpha_signals") or [])
    for row in signals:
        if row.get("no_trade_reason"):
            return no_trade_reason(row.get("no_trade_reason"))
    return "No hard risk flags, but confidence was not high enough."


def _main_risk(row: dict[str, Any]) -> str:
    return (
        translate_list(row.get("risk_flags") or row.get("avoid_reasons"))
        or no_trade_reason(row.get("no_trade_reason"))
    )


def _has_scan(payload: dict[str, Any]) -> bool:
    summary = dict(payload.get("summary") or {})
    return bool(
        summary.get("created_at")
        or payload.get("ranked_candidates")
        or payload.get("top_explosive")
        or payload.get("alpha_signals")
    )


def _has_source_problem(payload: dict[str, Any]) -> bool:
    bad = {"error", "failed", "failure", "blocked", "no_data"}
    for row in [
        *list(payload.get("provider_health") or []),
        *list(payload.get("source_health") or []),
    ]:
        status = str(row.get("status") or "").lower()
        if status in bad:
            return True
    return False


def _calendar_hit_rate(days: list[dict[str, Any]]) -> float | None:
    values = [_number_or_none(row.get("hit_rate")) for row in days]
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return round(sum(usable) / len(usable), 2)


def _calendar_worst(days: list[dict[str, Any]]) -> float | None:
    values = [_number_or_none(row.get("max_drawdown")) for row in days]
    usable = [float(value) for value in values if value is not None]
    return min(usable) if usable else None


def load_calendar_days(
    db_path: str | Path,
    start_date: str | date,
    end_date: str | date,
) -> list[dict[str, Any]]:
    """Return one calendar summary row for every date in the inclusive range."""
    return [
        _calendar_day_summary(detail)
        for detail in _load_calendar_details(db_path, start_date, end_date)
    ]


def load_calendar_day_detail(db_path: str | Path, date: str | date) -> dict[str, Any]:
    """Return a safe drilldown payload for one day."""
    details = _load_calendar_details(db_path, date, date)
    if details:
        return details[0]
    return _empty_day_detail(_date_key(date), ["No calendar day loaded."])


def load_calendar_daily_returns(
    db_path: str | Path,
    start_date: str | date,
    end_date: str | date,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for detail in _load_calendar_details(db_path, start_date, end_date):
        rows.append(
            {
                "date": detail["date"],
                "status": detail["status"],
                "is_fully_audited": detail["status"] == "AUDITED",
                "outcome_coverage_pct": detail["outcome_coverage_pct"],
                **dict(detail.get("basket_returns") or {}),
            }
        )
    return rows


def load_calendar_equity_curve(
    db_path: str | Path,
    start_date: str | date,
    end_date: str | date,
) -> list[dict[str, Any]]:
    equity = {"top1": 1.0, "top3": 1.0, "top5": 1.0}
    curve: list[dict[str, Any]] = []
    for row in load_calendar_daily_returns(db_path, start_date, end_date):
        if row.get("status") != "AUDITED":
            continue
        point: dict[str, Any] = {"date": row["date"]}
        for portfolio in ("top1", "top3", "top5"):
            value = _number_or_none(row.get(f"{portfolio}_close_return"))
            if value is None:
                point[f"{portfolio}_compounded_return"] = None
                continue
            equity[portfolio] *= 1 + (value / 100.0)
            point[f"{portfolio}_compounded_return"] = round((equity[portfolio] - 1) * 100, 4)
        curve.append(point)
    return curve


def load_calendar_missing_outcomes(
    db_path: str | Path,
    start_date: str | date,
    end_date: str | date,
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for detail in _load_calendar_details(db_path, start_date, end_date):
        missing.extend(list(detail.get("missing_outcomes") or []))
    return missing


def _load_calendar_details(
    db_path: str | Path,
    start_date: str | date,
    end_date: str | date,
) -> list[dict[str, Any]]:
    start = _date_key(start_date)
    end = _date_key(end_date)
    if start > end:
        start, end = end, start
    context = _calendar_context(db_path)
    return [_calendar_detail_for_day(context, day) for day in _date_keys_between(start, end)]


def _calendar_context(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path)
    context: dict[str, Any] = {
        "db_path": str(path),
        "warnings": [],
        "runs_by_date": defaultdict(list),
        "run_by_id": {},
        "ranked_by_run": defaultdict(list),
        "top_by_run": defaultdict(list),
        "avoid_by_run": defaultdict(list),
        "theses_by_date": defaultdict(list),
        "theses_by_run": defaultdict(list),
        "signals_by_date": defaultdict(list),
        "signals_by_run": defaultdict(list),
        "signal_by_id": {},
        "features_by_key": {},
        "notifications_by_date": defaultdict(list),
        "notifications_by_run": defaultdict(list),
        "audits_by_key": defaultdict(list),
        "audits_by_date_ticker": defaultdict(list),
        "outcomes_by_key": defaultdict(list),
        "outcomes_by_date_ticker": defaultdict(list),
        "outcomes_by_signal_id": {},
        "labels_by_key": defaultdict(list),
        "labels_by_date_ticker": defaultdict(list),
        "attribution_by_signal": defaultdict(list),
        "daily_signal_performance_by_date": {},
        "monitor_events_by_key": defaultdict(list),
        "monitor_events_by_date_ticker": defaultdict(list),
        "source_health_by_date": defaultdict(list),
        "provider_health_by_date": defaultdict(list),
        "performance_by_date": defaultdict(list),
        "performance_cumulative_by_date": defaultdict(list),
        "shadow_reports_by_date": defaultdict(list),
        "alpha_reports_by_date": defaultdict(list),
        "manual_audit_summary_by_date": defaultdict(list),
        "source_reliability_by_source": {},
    }
    if not path.exists():
        context["warnings"].append(f"Database not found: {path}")
        return context
    try:
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            tables = _table_names(connection)
            context["warnings"].extend(_missing_calendar_table_warnings(tables))
            _load_scan_runs(connection, tables, context)
            _load_run_payload_table(
                connection, tables, context, "ranked_candidates", "ranked_by_run"
            )
            _load_run_payload_table(connection, tables, context, "top_explosive", "top_by_run")
            _load_run_payload_table(connection, tables, context, "avoid_list", "avoid_by_run")
            _load_recommendation_theses(connection, tables, context)
            _load_alpha_signals(connection, tables, context)
            _load_historical_signal_tables(connection, tables, context)
            _load_alpha_features(connection, tables, context)
            _load_notifications(connection, tables, context)
            _load_outcome_rows(connection, tables, context)
            _load_monitor_rows(connection, tables, context)
            _load_source_reliability_rows(connection, tables, context)
            _load_health_rows(connection, tables, context)
            _load_report_rows(connection, tables, context)
    except sqlite3.Error as exc:
        context["warnings"].append(f"Could not read calendar database: {exc}")
    return context


def _load_scan_runs(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    if "scan_runs" not in tables:
        return
    for row in _select_rows(
        connection,
        "scan_runs",
        ["id", "created_at", "source", "config_json", "summary_json"],
        order_by="created_at ASC",
    ):
        summary = _json_dict(row.get("summary_json"))
        config = _json_dict(row.get("config_json"))
        run = {
            "run_id": str(row.get("id") or ""),
            "created_at": str(row.get("created_at") or ""),
            "source": str(row.get("source") or ""),
            "summary": summary,
            "config": config,
        }
        run_date = _date_from_values(run["created_at"], summary.get("created_at"))
        if not run_date:
            continue
        context["run_by_id"][run["run_id"]] = {**run, "date": run_date}
        context["runs_by_date"][run_date].append({**run, "date": run_date})


def _load_run_payload_table(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
    table: str,
    bucket: str,
) -> None:
    if table not in tables:
        return
    for row in _payload_rows(
        connection,
        table,
        ["run_id", "rank", "ticker", "payload_json"],
        order_by="run_id ASC, rank ASC, id ASC",
    ):
        run_id = str(row.get("run_id") or row.get("scan_id") or "")
        if not run_id:
            continue
        context[bucket][run_id].append(row)


def _load_recommendation_theses(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    if "recommendation_theses" not in tables:
        return
    for row in _payload_rows(
        connection,
        "recommendation_theses",
        ["run_id", "ticker", "rank", "created_at", "payload_json"],
        order_by="created_at ASC, rank ASC, id ASC",
    ):
        run_id = str(row.get("run_id") or row.get("scan_id") or "")
        row_date = _date_from_values(row.get("created_at"), _run_date(context, run_id))
        if row_date:
            context["theses_by_date"][row_date].append(row)
        if run_id:
            context["theses_by_run"][run_id].append(row)


def _load_alpha_signals(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    if "alpha_signals" not in tables:
        return
    for row in _payload_rows(
        connection,
        "alpha_signals",
        [
            "signal_key",
            "scan_id",
            "ticker",
            "rank",
            "timestamp",
            "alpha_score",
            "edge_bucket",
            "confidence_bucket",
            "can_alert",
            "no_trade_reason",
            "payload_json",
        ],
        order_by="timestamp ASC, rank ASC, id ASC",
    ):
        scan_id = str(row.get("scan_id") or "")
        timestamp = str(row.get("timestamp") or row.get("as_of_timestamp") or "")
        signal_date = _date_from_values(timestamp, _run_date(context, scan_id))
        if not signal_date:
            continue
        row["date"] = signal_date
        row["scan_id"] = scan_id
        context["signals_by_date"][signal_date].append(row)
        if scan_id:
            context["signals_by_run"][scan_id].append(row)


def _load_historical_signal_tables(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    if "historical_signals" in tables:
        for row in _select_rows(
            connection,
            "historical_signals",
            [
                "signal_id",
                "scan_id",
                "alpha_signal_id",
                "generated_at",
                "market_date",
                "ticker",
                "company",
                "rank",
                "source",
                "source_url",
                "source_confidence",
                "data_source_kind",
                "model_version",
                "config_hash",
                "primary_setup",
                "setup_grade",
                "signal_label",
                "entry_watch_level",
                "entry_trigger_type",
                "entry_condition",
                "confirmation_condition",
                "exit_line",
                "invalidation_level",
                "target_1",
                "target_2",
                "risk_flags_json",
                "avoid_reasons_json",
                "catalyst_summary",
                "telegram_event_key",
                "was_alerted",
                "no_trade_reason",
                "raw_payload_json",
            ],
            order_by="market_date ASC, COALESCE(rank, 999999) ASC, ticker ASC",
        ):
            payload = _json_dict(row.get("raw_payload_json"))
            merged = {
                **payload,
                **{key: value for key, value in row.items() if key != "raw_payload_json"},
                "timestamp": row.get("generated_at"),
                "signal_key": row.get("signal_id"),
                "entry_trigger": row.get("entry_watch_level"),
                "breakout_trigger": row.get("entry_watch_level"),
                "invalidation": row.get("invalidation_level") or row.get("exit_line"),
                "first_target": row.get("target_1"),
                "target_2": row.get("target_2"),
                "setup_key": row.get("primary_setup") or row.get("setup_grade"),
                "risk_flags": ";".join(_json_list(row.get("risk_flags_json"))),
                "avoid_reasons": ";".join(_json_list(row.get("avoid_reasons_json"))),
                "can_alert": str(row.get("signal_label") or "").upper() != "NO CLEAN EDGE",
            }
            signal_id = str(row.get("signal_id") or "")
            scan_id = str(row.get("scan_id") or "")
            day = str(row.get("market_date") or "")[:10]
            if signal_id:
                context["signal_by_id"][signal_id] = merged
            if day:
                context["signals_by_date"][day].append(merged)
            if scan_id:
                context["signals_by_run"][scan_id].append(merged)
    if "signal_outcomes" in tables:
        for row in _payload_rows(
            connection,
            "signal_outcomes",
            [
                "signal_id",
                "market_date",
                "ticker",
                "outcome_source",
                "entry_time",
                "entry_price",
                "price_1m",
                "price_5m",
                "price_15m",
                "lunch_price",
                "close_price",
                "high_after_entry",
                "low_after_entry",
                "halted",
                "notes",
                "imported_at",
                "outcome_status",
                "payload_json",
            ],
            order_by="market_date ASC, ticker ASC",
        ):
            signal_id = str(row.get("signal_id") or "")
            signal = dict(context["signal_by_id"].get(signal_id, {}) or {})
            row.setdefault("scan_id", signal.get("scan_id") or "")
            row.setdefault("recommendation_timestamp", signal.get("generated_at") or "")
            row["date"] = row.get("market_date") or row.get("date") or ""
            row["source"] = row.get("outcome_source") or row.get("source") or ""
            if signal_id:
                context["outcomes_by_signal_id"][signal_id] = row
            _store_outcome_context(row, context, "outcomes_by_key", "outcomes_by_date_ticker")
    if "signal_return_attribution" in tables:
        for row in _payload_rows(
            connection,
            "signal_return_attribution",
            [
                "attribution_id",
                "signal_id",
                "ticker",
                "market_date",
                "entry_policy",
                "exit_policy",
                "entry_price",
                "exit_price",
                "return_pct",
                "max_favorable_excursion",
                "max_adverse_excursion",
                "drawdown_pct",
                "hit_target_1",
                "hit_target_2",
                "hit_invalidation",
                "trigger_activated",
                "audit_status",
                "scenario_or_recommended",
                "calculated_at",
                "payload_json",
            ],
            order_by="market_date ASC, ticker ASC, entry_policy ASC, exit_policy ASC",
        ):
            context["attribution_by_signal"][str(row.get("signal_id") or "")].append(row)
    if "daily_signal_performance" in tables:
        for row in _payload_rows(
            connection,
            "daily_signal_performance",
            ["market_date", "payload_json"],
            order_by="market_date ASC",
        ):
            day = str(row.get("market_date") or "")[:10]
            if day:
                context["daily_signal_performance_by_date"][day] = row


def _load_alpha_features(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    if "alpha_feature_vectors" not in tables:
        return
    for row in _payload_rows(
        connection,
        "alpha_feature_vectors",
        ["scan_id", "ticker", "timestamp", "model_version", "config_hash", "payload_json"],
        order_by="timestamp ASC, id ASC",
    ):
        key = _key(row.get("scan_id"), row.get("ticker"))
        if key:
            context["features_by_key"][key] = row


def _load_notifications(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    if "notifications_sent" not in tables:
        return
    for row in _payload_rows(
        connection,
        "notifications_sent",
        ["event_key", "run_id", "ticker", "channel", "sent_at", "payload_json"],
        order_by="sent_at ASC, id ASC",
    ):
        sent_date = _date_from_values(row.get("sent_at"), _run_date(context, row.get("run_id")))
        if sent_date:
            context["notifications_by_date"][sent_date].append(row)
        run_id = str(row.get("run_id") or "")
        if run_id:
            context["notifications_by_run"][run_id].append(row)


def _load_outcome_rows(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    if "manual_audit_trades" in tables:
        for row in _payload_rows(
            connection,
            "manual_audit_trades",
            ["scan_id", "ticker", "payload_json"],
            order_by="id ASC",
        ):
            _store_outcome_context(row, context, "audits_by_key", "audits_by_date_ticker")
    if "manual_outcomes" in tables:
        for row in _payload_rows(
            connection,
            "manual_outcomes",
            ["scan_id", "ticker", "recommendation_timestamp", "uploaded_at", "payload_json"],
            order_by="uploaded_at ASC, id ASC",
        ):
            _store_outcome_context(row, context, "outcomes_by_key", "outcomes_by_date_ticker")
    for table in ("alpha_outcome_labels", "outcome_labels"):
        if table not in tables:
            continue
        for row in _payload_rows(
            connection,
            table,
            ["scan_id", "ticker", "created_at", "payload_json"],
            order_by="created_at ASC, id ASC",
        ):
            _store_outcome_context(row, context, "labels_by_key", "labels_by_date_ticker")


def _store_outcome_context(
    row: dict[str, Any],
    context: dict[str, Any],
    by_key: str,
    by_date: str,
) -> None:
    scan_id = str(row.get("scan_id") or row.get("run_id") or "")
    ticker = str(row.get("ticker") or "").upper()
    if scan_id and ticker:
        context[by_key][(scan_id, ticker)].append(row)
    row_date = _date_from_values(
        row.get("recommendation_timestamp"),
        row.get("entry_time"),
        row.get("created_at"),
        row.get("uploaded_at"),
        row.get("date"),
        _run_date(context, scan_id),
    )
    if row_date and ticker:
        context[by_date][(row_date, ticker)].append(row)


def _load_monitor_rows(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    for table, created_column in (("monitor_events", "created_at"), ("alerts_sent", "sent_at")):
        if table not in tables:
            continue
        rows = _payload_rows(
            connection,
            table,
            [
                "run_id",
                "ticker",
                "event_type",
                "severity",
                created_column,
                "payload_json",
            ],
            order_by=f"{created_column} ASC, id ASC",
        )
        for row in rows:
            scan_id = str(row.get("run_id") or row.get("scan_id") or "")
            ticker = str(row.get("ticker") or "").upper()
            event_date = _date_from_values(
                row.get("created_at"),
                row.get("sent_at"),
                _run_date(context, scan_id),
            )
            if scan_id and ticker:
                context["monitor_events_by_key"][(scan_id, ticker)].append(row)
            if event_date and ticker:
                context["monitor_events_by_date_ticker"][(event_date, ticker)].append(row)


def _load_source_reliability_rows(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    for table in ("alpha_source_reliability", "source_reliability"):
        if table not in tables:
            continue
        for row in _merged_json_rows(
            connection,
            table,
            [
                "source",
                "updated_at",
                "runs",
                "rows_returned",
                "rows_normalized",
                "rows_rejected",
                "stale_count",
                "missing_critical_count",
                "outcome_count",
                "winner_count",
                "reliability_score",
                "summary_json",
                "payload_json",
            ],
            json_columns=("summary_json", "payload_json"),
            order_by=_safe_order_by(connection, table, ["source"]),
        ):
            source = str(row.get("source") or "").strip()
            if source:
                context["source_reliability_by_source"][source] = row


def _load_health_rows(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    if "source_health" in tables:
        for row in _payload_rows(
            connection,
            "source_health",
            ["source", "status", "checked_at", "detail", "payload_json"],
            order_by="checked_at ASC, id ASC",
        ):
            row_date = _date_from_values(row.get("checked_at"))
            if row_date:
                context["source_health_by_date"][row_date].append(row)
    if "provider_health" in tables:
        for row in _select_rows(
            connection,
            "provider_health",
            ["provider", "status", "checked_at", "detail"],
            order_by="checked_at ASC, id ASC",
        ):
            row_date = _date_from_values(row.get("checked_at"))
            if row_date:
                context["provider_health_by_date"][row_date].append(row)


def _load_report_rows(
    connection: sqlite3.Connection,
    tables: set[str],
    context: dict[str, Any],
) -> None:
    if "performance_daily" in tables:
        for row in _payload_rows(
            connection,
            "performance_daily",
            ["report_date", "run_id", "payload_json"],
            order_by="report_date ASC, id ASC",
        ):
            row_date = _date_from_values(row.get("report_date"))
            if row_date:
                context["performance_by_date"][row_date].append(row)
    if "performance_cumulative" in tables:
        for row in _payload_rows(
            connection,
            "performance_cumulative",
            ["created_at", "payload_json"],
            order_by="created_at ASC, id ASC",
        ):
            row_date = _date_from_values(row.get("created_at"))
            if row_date:
                context["performance_cumulative_by_date"][row_date].append(row)
    if "shadow_reports" in tables:
        for row in _payload_rows(
            connection,
            "shadow_reports",
            ["created_at", "payload_json"],
            order_by="created_at ASC, id ASC",
        ):
            row_date = _date_from_values(row.get("created_at"))
            if row_date:
                context["shadow_reports_by_date"][row_date].append(row)
    for table in ("alpha_reports", "alpha_report"):
        if table not in tables:
            continue
        for row in _merged_json_rows(
            connection,
            table,
            ["created_at", "report_date", "run_id", "payload_json", "summary_json"],
            json_columns=("payload_json", "summary_json"),
            order_by=_safe_order_by(connection, table, ["report_date", "created_at", "id"]),
        ):
            row_date = _date_from_values(
                row.get("report_date"),
                row.get("created_at"),
                _run_date(context, row.get("run_id")),
            )
            if row_date:
                context["alpha_reports_by_date"][row_date].append(row)
    if "manual_audit_summary" in tables:
        for row in _payload_rows(
            connection,
            "manual_audit_summary",
            ["created_at", "payload_json"],
            order_by="created_at ASC, id ASC",
        ):
            row_date = _date_from_values(row.get("created_at"))
            if row_date:
                context["manual_audit_summary_by_date"][row_date].append(row)


def _calendar_detail_for_day(context: dict[str, Any], day: str) -> dict[str, Any]:
    runs = list(context["runs_by_date"].get(day, []))
    date_theses = list(context["theses_by_date"].get(day, []))
    signals = _dedupe_rows(
        [
            *list(context["signals_by_date"].get(day, [])),
            *[
                signal
                for run in runs
                for signal in context["signals_by_run"].get(str(run.get("run_id") or ""), [])
            ],
        ],
        keys=("signal_key", "scan_id", "ticker", "rank"),
    )
    run_ids = {str(run.get("run_id") or "") for run in runs if run.get("run_id")}
    run_ids.update(str(row.get("scan_id") or "") for row in signals if row.get("scan_id"))
    run_ids.update(str(row.get("run_id") or "") for row in date_theses if row.get("run_id"))
    theses = _dedupe_rows(
        [
            *date_theses,
            *[row for run_id in run_ids for row in context["theses_by_run"].get(run_id, [])],
        ],
        keys=("run_id", "ticker", "rank", "created_at"),
    )
    ranked = [row for run_id in run_ids for row in context["ranked_by_run"].get(run_id, [])]
    top_rows = [row for run_id in run_ids for row in context["top_by_run"].get(run_id, [])]
    avoid = [row for run_id in run_ids for row in context["avoid_by_run"].get(run_id, [])]
    notifications = _dedupe_rows(
        [
            *list(context["notifications_by_date"].get(day, [])),
            *[row for run_id in run_ids for row in context["notifications_by_run"].get(run_id, [])],
        ],
        keys=("event_key", "sent_at", "channel"),
    )
    no_trade = _is_no_trade_day(signals, notifications)
    source_rows = list(context["source_health_by_date"].get(day, []))
    provider_rows = list(context["provider_health_by_date"].get(day, []))
    source_failure = _has_source_failure(source_rows, provider_rows)
    pick_source_rows = _pick_source_rows(signals, top_rows, ranked, theses, no_trade)
    return_rows = [
        _return_row_for_pick(row, day, context, no_trade=no_trade) for row in pick_source_rows
    ]
    missing = _missing_outcome_rows(day, return_rows)
    basket = _basket_returns(return_rows)
    candidate_count = _candidate_count(runs, ranked, signals, theses)
    top_pick_count = 0 if no_trade else len(pick_source_rows)
    missing_count = 0 if no_trade else len(missing)
    audited_count = sum(1 for row in return_rows if row.get("audit_status") == "audited")
    partial_count = sum(1 for row in return_rows if row.get("audit_status") == "partial")
    status = _day_status(
        has_data=bool(
            runs
            or signals
            or ranked
            or theses
            or notifications
            or source_rows
            or provider_rows
            or context["alpha_reports_by_date"].get(day)
            or context["performance_by_date"].get(day)
            or context["performance_cumulative_by_date"].get(day)
            or context["shadow_reports_by_date"].get(day)
        ),
        source_failure=source_failure,
        no_trade=no_trade,
        pick_count=top_pick_count,
        missing_count=missing_count,
        partial_count=partial_count,
        audited_count=audited_count,
    )
    overview = _day_overview(
        day=day,
        runs=runs,
        signals=signals,
        notifications=notifications,
        source_rows=source_rows,
        provider_rows=provider_rows,
        no_trade=no_trade,
        source_failure=source_failure,
    )
    coverage = (
        round(((top_pick_count - missing_count) / top_pick_count) * 100, 2)
        if top_pick_count
        else None
    )
    return {
        "date": day,
        "status": status,
        "warnings": list(context.get("warnings") or []),
        "overview": overview,
        "telegram": _telegram_rows(notifications),
        "picks": [_pick_row(row, no_trade=no_trade) for row in pick_source_rows],
        "return_rows": return_rows,
        "basket_returns": basket,
        "missing_outcomes": missing,
        "recommendation_theses": theses,
        "source_health": source_rows,
        "provider_health": provider_rows,
        "source_reliability": dict(context["source_reliability_by_source"]),
        "performance_daily": list(context["performance_by_date"].get(day, [])),
        "performance_cumulative": list(context["performance_cumulative_by_date"].get(day, [])),
        "shadow_reports": list(context["shadow_reports_by_date"].get(day, [])),
        "alpha_reports": list(context["alpha_reports_by_date"].get(day, [])),
        "manual_audit_summary": list(context["manual_audit_summary_by_date"].get(day, [])),
        "candidate_count": candidate_count,
        "signal_count": len(signals),
        "top_pick_count": top_pick_count,
        "missing_outcome_count": missing_count,
        "partial_outcome_count": partial_count,
        "avoided_count": len(avoid),
        "top1_return": basket.get("top1_close_return"),
        "top3_return": basket.get("top3_close_return"),
        "top5_return": basket.get("top5_close_return"),
        "best_pick_return": _extreme_return(return_rows, "close_return", best=True),
        "worst_pick_return": _extreme_return(return_rows, "close_return", best=False),
        "max_drawdown": _extreme_return(return_rows, "low_after_entry_drawdown", best=False),
        "hit_rate": _hit_rate(return_rows, "close_return"),
        "outcome_coverage_pct": coverage,
        "required_outcome_path": f"data\\inbox\\outcomes\\outcomes_{day}.csv",
        "required_outcome_columns": [
            "date",
            "ticker",
            "entry_time",
            "entry_price",
            "price_1m",
            "price_5m",
            "price_15m",
            "lunch_price",
            "close_price",
            "high_after_entry",
            "low_after_entry",
            "source",
            "notes",
        ],
        "import_commands": _outcome_commands(day),
    }


def _calendar_day_summary(detail: dict[str, Any]) -> dict[str, Any]:
    overview = dict(detail.get("overview") or {})
    return {
        "date": detail["date"],
        "status": detail["status"],
        "candidate_count": detail.get("candidate_count", 0),
        "signal_count": detail.get("signal_count", 0),
        "top_pick_count": detail.get("top_pick_count", 0),
        "missing_outcome_count": detail.get("missing_outcome_count", 0),
        "avoided_count": detail.get("avoided_count", 0),
        "top_pick": str(dict((detail.get("picks") or [{}])[0]).get("ticker") or "None")
        if detail.get("picks")
        else "None",
        "top1_return": detail.get("top1_return"),
        "top3_return": detail.get("top3_return"),
        "top5_return": detail.get("top5_return"),
        "best_pick_return": detail.get("best_pick_return"),
        "worst_pick_return": detail.get("worst_pick_return"),
        "max_drawdown": detail.get("max_drawdown"),
        "hit_rate": detail.get("hit_rate"),
        "outcome_coverage_pct": detail.get("outcome_coverage_pct"),
        "alphaops_decision": overview.get("alphaops_decision", ""),
        "data_source_kind": overview.get("data_source_kind", ""),
        "source_label": overview.get("source_label", ""),
        "source_status": overview.get("source_status", ""),
        "setups": ";".join(
            sorted(
                {
                    str(row.get("primary_setup") or "")
                    for row in detail["picks"]
                    if row.get("primary_setup")
                }
            )
        ),
        "warnings": "; ".join(str(item) for item in detail.get("warnings") or []),
    }


def _empty_day_detail(day: str, warnings: list[str]) -> dict[str, Any]:
    return {
        "date": day,
        "status": "NO DATA",
        "warnings": warnings,
        "overview": {
            "date": day,
            "source_status": "No data",
            "alphaops_decision": "NO DATA",
            "source_label": "No persisted AlphaOps data",
        },
        "telegram": [],
        "picks": [],
        "return_rows": [],
        "basket_returns": _basket_returns([]),
        "missing_outcomes": [],
        "candidate_count": 0,
        "signal_count": 0,
        "top_pick_count": 0,
        "missing_outcome_count": 0,
        "avoided_count": 0,
        "outcome_coverage_pct": None,
        "required_outcome_path": f"data\\inbox\\outcomes\\outcomes_{day}.csv",
        "required_outcome_columns": [],
        "import_commands": _outcome_commands(day),
    }


def _pick_source_rows(
    signals: list[dict[str, Any]],
    top_rows: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    theses: list[dict[str, Any]],
    no_trade: bool,
) -> list[dict[str, Any]]:
    if signals:
        rows = sorted(signals, key=lambda row: (_int(row.get("rank"), 999), str(row.get("ticker"))))
        if no_trade:
            return rows[:5]
        clean = [row for row in rows if row.get("can_alert") and not row.get("no_trade_reason")]
        return (clean or rows)[:5]
    rows = top_rows or theses or ranked
    return sorted(rows, key=lambda row: (_int(row.get("rank"), 999), str(row.get("ticker"))))[:5]


def _return_row_for_pick(
    pick: dict[str, Any],
    day: str,
    context: dict[str, Any],
    *,
    no_trade: bool,
) -> dict[str, Any]:
    if not no_trade:
        attributed = _return_row_from_attribution(pick, context)
        if attributed:
            return attributed
    ticker = str(pick.get("ticker") or "").upper()
    scan_id = str(pick.get("scan_id") or pick.get("run_id") or "")
    signal_time = str(pick.get("timestamp") or pick.get("as_of_timestamp") or "")
    audit = _first_valid_outcome(
        context["audits_by_key"].get((scan_id, ticker), []),
        context["audits_by_date_ticker"].get((day, ticker), []),
        signal_time=signal_time,
    )
    label = _first_valid_outcome(
        context["labels_by_key"].get((scan_id, ticker), []),
        context["labels_by_date_ticker"].get((day, ticker), []),
        signal_time=signal_time,
    )
    imported = _first_valid_outcome(
        context["outcomes_by_key"].get((scan_id, ticker), []),
        context["outcomes_by_date_ticker"].get((day, ticker), []),
        signal_time=signal_time,
    )
    entry = _number_or_none(
        _first_non_empty(
            audit.get("entry_price") if audit else None,
            label.get("entry_price") if label else None,
            imported.get("entry_price") if imported else None,
        )
    )
    row = {
        "rank": _int(pick.get("rank"), 0) or "",
        "ticker": ticker,
        "entry_price": entry,
        "entry_time": _first_non_empty(
            audit.get("entry_time") if audit else None,
            imported.get("entry_time") if imported else None,
            signal_time,
        ),
        "signal_time": signal_time,
        "recommended_exit_policy": "not_recorded",
        "recommended_exit_price": None,
        "recommended_exit_return": None,
        "price_1m_return": _number_or_none(_pick_return(audit, label, "return_1m_pct")),
        "price_5m_return": _number_or_none(_pick_return(audit, label, "return_5m_pct")),
        "price_15m_return": _number_or_none(_pick_return(audit, label, "return_15m_pct")),
        "lunch_return": _number_or_none(_pick_return(audit, label, "lunch_return_pct")),
        "close_return": _number_or_none(_pick_return(audit, label, "close_return_pct")),
        "open_entry_return": _number_or_none(_pick_return(audit, label, "open_entry_return_pct")),
        "breakout_entry_return": _number_or_none(
            _pick_return(audit, label, "breakout_entry_return_pct")
        ),
        "monitor_exit_return": None,
        "high_after_entry_return": _number_or_none(
            _first_non_empty(
                _pick_return(label, audit, "high_after_entry_return"),
                _pick_return(audit, label, "high_return_pct"),
            )
        ),
        "low_after_entry_drawdown": _number_or_none(
            _first_non_empty(
                _pick_return(label, audit, "low_after_entry_drawdown"),
                _pick_return(audit, label, "low_drawdown_pct"),
            )
        ),
        "return_kind": "scenario returns",
        "high_after_entry_label": "opportunity, not realized",
        "audit_status": "Outcome needed",
        "outcome_source": "none",
        "notes": "",
    }
    if no_trade:
        row["audit_status"] = "NO TRADE"
        row["notes"] = "No-trade decision saved for this day."
        return row
    if audit:
        row["audit_status"] = str(audit.get("audit_status") or "audited").lower()
        row["outcome_source"] = "manual_audit_trades"
        row["notes"] = str(audit.get("notes") or audit.get("audit_reason") or "")
    elif label:
        row["audit_status"] = "partial"
        row["outcome_source"] = "alpha_outcome_labels"
        row["notes"] = "Alpha outcome label has limited persisted return fields."
    elif imported:
        row["audit_status"] = "Outcome imported, audit needed"
        row["outcome_source"] = "manual_outcomes"
        row["notes"] = "Run audit-manual-outcomes to calculate scenario returns."
    if not audit and not label and not imported:
        return row
    explicit_exit = _explicit_exit(audit or label or imported or {})
    monitor_exit = _monitor_exit(pick, day, context, entry)
    if explicit_exit:
        row.update(explicit_exit)
    elif monitor_exit:
        row.update(monitor_exit)
    if row["audit_status"] not in {"audited", "partial"} and _has_any_return(row):
        row["audit_status"] = "partial"
    return row


def _return_row_from_attribution(
    pick: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    signal_id = str(pick.get("signal_id") or pick.get("signal_key") or "")
    if not signal_id:
        return {}
    rows = list(context["attribution_by_signal"].get(signal_id, []))
    if not rows:
        return {}
    first_available = [
        row for row in rows if row.get("entry_policy") == "first_available_after_signal"
    ]
    by_policy = {str(row.get("exit_policy") or ""): row for row in first_available}
    outcome = dict(context["outcomes_by_signal_id"].get(signal_id, {}) or {})
    close = by_policy.get("close", {})
    high = by_policy.get("high_opportunity", {})
    low = close or by_policy.get("invalidation", {})
    missing: dict[str, Any] = by_policy.get("outcome_needed") or next(
        (row for row in rows if row.get("audit_status") == "missing_outcome"),
        {},
    )
    recommended: dict[str, Any] = next(
        (
            row
            for row in rows
            if row.get("scenario_or_recommended") == "recommended"
            and row.get("audit_status") == "audited"
        ),
        {},
    )
    any_return = any(_number_or_none(row.get("return_pct")) is not None for row in rows)
    audit_status = "audited" if _number_or_none(close.get("return_pct")) is not None else (
        "partial" if any_return else "Outcome needed"
    )
    if missing and not any_return:
        audit_status = "Outcome needed"
    return {
        "rank": _int(pick.get("rank"), 0) or "",
        "ticker": str(pick.get("ticker") or "").upper(),
        "entry_price": _number_or_none(
            _first_non_empty(close.get("entry_price"), outcome.get("entry_price"))
        ),
        "entry_time": _first_non_empty(outcome.get("entry_time"), pick.get("generated_at")),
        "signal_time": _first_non_empty(pick.get("generated_at"), pick.get("timestamp")),
        "recommended_exit_policy": (
            str(recommended.get("exit_policy") or "not_recorded") if recommended else "not_recorded"
        ),
        "recommended_exit_price": _number_or_none(recommended.get("exit_price")),
        "recommended_exit_return": _number_or_none(recommended.get("return_pct")),
        "price_1m_return": _number_or_none(dict(by_policy.get("one_min") or {}).get("return_pct")),
        "price_5m_return": _number_or_none(dict(by_policy.get("five_min") or {}).get("return_pct")),
        "price_15m_return": _number_or_none(
            dict(by_policy.get("fifteen_min") or {}).get("return_pct")
        ),
        "lunch_return": _number_or_none(dict(by_policy.get("lunch") or {}).get("return_pct")),
        "close_return": _number_or_none(close.get("return_pct")),
        "open_entry_return": None,
        "breakout_entry_return": _trigger_return(rows, "close"),
        "monitor_exit_return": _number_or_none(
            dict(by_policy.get("monitor_exit_signal") or recommended).get("return_pct")
        ),
        "high_after_entry_return": _number_or_none(high.get("return_pct")),
        "low_after_entry_drawdown": _number_or_none(
            _first_non_empty(low.get("drawdown_pct"), close.get("drawdown_pct"))
        ),
        "return_kind": "scenario returns",
        "high_after_entry_label": "opportunity, not realized",
        "audit_status": audit_status,
        "outcome_source": (
            "signal_return_attribution" if audit_status != "Outcome needed" else "none"
        ),
        "notes": (
            "Scenario return from imported historical outcome."
            if audit_status != "Outcome needed"
            else "Outcome needed before return can be counted."
        ),
    }


def _trigger_return(rows: list[dict[str, Any]], exit_policy: str) -> float | None:
    for row in rows:
        if row.get("entry_policy") == "trigger_touch" and row.get("exit_policy") == exit_policy:
            return _number_or_none(row.get("return_pct"))
    return None


def _pick_row(row: dict[str, Any], *, no_trade: bool) -> dict[str, Any]:
    label = _research_label(row, no_trade=no_trade)
    return {
        "rank": _int(row.get("rank"), 0) or "",
        "ticker": str(row.get("ticker") or "").upper(),
        "company": str(row.get("company") or row.get("name") or ""),
        "primary_setup": str(row.get("setup_key") or row.get("setup_grade") or ""),
        "alpha_score": _number_or_none(row.get("alpha_score")),
        "total_score": _number_or_none(row.get("total_score") or row.get("score")),
        "edge_bucket": str(row.get("edge_bucket") or ""),
        "confidence_bucket": str(row.get("confidence_bucket") or ""),
        "risk_score": _number_or_none(row.get("risk_score")),
        "source_confidence": _number_or_none(row.get("source_confidence")),
        "gap_pct": _number_or_none(row.get("gap_pct")),
        "premarket_price": _number_or_none(row.get("premarket_price")),
        "trigger": _first_non_empty(row.get("entry_trigger"), row.get("breakout_trigger")),
        "invalidation": _first_non_empty(row.get("invalidation"), row.get("invalidation_level")),
        "target": _first_non_empty(row.get("target_1"), row.get("first_target")),
        "catalyst": _first_non_empty(row.get("catalyst_summary"), row.get("catalyst_headline")),
        "risk_flags": str(row.get("risk_flags") or ""),
        "avoid_reasons": str(row.get("avoid_reasons") or row.get("no_trade_reason") or ""),
        "label/action": label,
        "source": str(row.get("preferred_source") or row.get("source") or ""),
        "data_source_kind": str(row.get("data_source_kind") or ""),
        "catalyst_category": str(row.get("catalyst_category") or ""),
        "setup_key": str(row.get("setup_key") or ""),
    }


def _day_overview(
    *,
    day: str,
    runs: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    notifications: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    provider_rows: list[dict[str, Any]],
    no_trade: bool,
    source_failure: bool,
) -> dict[str, Any]:
    top_signal = signals[0] if signals else {}
    latest_run = runs[-1] if runs else {}
    summary = dict(latest_run.get("summary") or {})
    config = dict(latest_run.get("config") or {})
    source_kind = str(
        top_signal.get("data_source_kind")
        or config.get("data_source_kind")
        or summary.get("data_source_kind")
        or ""
    )
    source_status = "SOURCE FAILURE" if source_failure else "ok" if runs or signals else "No data"
    return {
        "date": day,
        "source_status": source_status,
        "source_confidence": _number_or_none(
            _first_non_empty(
                top_signal.get("source_confidence"),
                summary.get("source_confidence"),
            )
        ),
        "alphaops_decision": "NO TRADE" if no_trade else "WATCHLIST" if signals else "NO DATA",
        "no_trade_reason": _no_trade_reason(signals, notifications) if no_trade else "",
        "model_version": str(top_signal.get("model_version") or ""),
        "config_hash": str(top_signal.get("config_hash") or ""),
        "data_source_kind": source_kind,
        "source_label": _source_label(top_signal, config, source_kind),
        "public_free_label": _source_label(top_signal, config, source_kind),
        "notification_count": len(notifications),
        "source_health_count": len(source_rows),
        "provider_health_count": len(provider_rows),
    }


def _telegram_rows(notifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in notifications:
        channel = str(row.get("channel") or "")
        event_key = str(row.get("event_key") or "")
        message = _notification_message(row)
        if channel.lower() != "telegram" and "telegram" not in event_key.lower() and not message:
            continue
        rows.append({
            "event_key": event_key,
            "channel": channel,
            "sent_at": str(row.get("sent_at") or ""),
            "message": message,
        })
    return rows


def _notification_message(row: dict[str, Any]) -> str:
    text = _first_non_empty(
        row.get("telegram_compact_message"),
        row.get("body"),
        row.get("message"),
        row.get("text"),
        row.get("title"),
    )
    if not text:
        return ""
    lowered = str(text).lower()
    if any(marker in lowered for marker in ("telegram_bot_token", "telegram_chat_id", "secret")):
        return "[redacted notification text]"
    return str(text)


def _missing_outcome_rows(day: str, return_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in return_rows:
        if row.get("audit_status") != "Outcome needed":
            continue
        rows.append({
            "date": day,
            "ticker": row.get("ticker"),
            "rank": row.get("rank"),
            "audit_status": "Outcome needed",
            "expected_path": f"data\\inbox\\outcomes\\outcomes_{day}.csv",
        })
    return rows


def _basket_returns(return_rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for count in (1, 3, 5):
        for policy, field in CALENDAR_RETURN_POLICIES.items():
            result[f"top{count}_{policy}_return"] = _equal_weight_return(
                return_rows, field, count
            )
        result[f"top{count}_open_entry_return"] = _equal_weight_return(
            return_rows, "open_entry_return", count
        )
        result[f"top{count}_breakout_entry_return"] = _equal_weight_return(
            return_rows, "breakout_entry_return", count
        )
    result["return_note"] = (
        "Scenario returns are not recommended returns. Recommended returns require "
        "an explicit saved exit or monitor exit signal."
    )
    return result


def _equal_weight_return(
    rows: list[dict[str, Any]],
    field: str,
    count: int,
) -> float | None:
    selected = rows[:count]
    if not selected:
        return None
    values = [_number_or_none(row.get(field)) for row in selected]
    if any(value is None for value in values):
        return None
    usable = [float(value) for value in values if value is not None]
    return round(sum(usable) / len(usable), 4) if usable else None


def _candidate_count(
    runs: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    theses: list[dict[str, Any]],
) -> int:
    counts: list[int] = [len(ranked), len(signals), len(theses)]
    for run in runs:
        summary = dict(run.get("summary") or {})
        for key in ("candidate_count", "ranked_count", "snapshot_row_count"):
            value = _int(summary.get(key), 0)
            if value:
                counts.append(value)
    return max(counts) if counts else 0


def _day_status(
    *,
    has_data: bool,
    source_failure: bool,
    no_trade: bool,
    pick_count: int,
    missing_count: int,
    partial_count: int,
    audited_count: int,
) -> str:
    if not has_data:
        return "NO DATA"
    if source_failure and not pick_count:
        return "SOURCE FAILURE"
    if no_trade:
        return "NO TRADE"
    if pick_count and missing_count == pick_count:
        return "PICKS PENDING OUTCOMES"
    if pick_count and (missing_count or partial_count):
        return "OUTCOMES PARTIAL"
    if pick_count and audited_count == pick_count:
        return "AUDITED"
    return "PICKS PENDING OUTCOMES" if pick_count else "NO DATA"


def _is_no_trade_day(signals: list[dict[str, Any]], notifications: list[dict[str, Any]]) -> bool:
    for row in notifications:
        text = " ".join(
            str(row.get(key) or "")
            for key in ("event_key", "title", "body", "telegram_compact_message", "message")
        ).lower()
        if "alpha_no_trade" in text or "no clean edge" in text:
            return True
    if signals and all(not row.get("can_alert") or row.get("no_trade_reason") for row in signals):
        return True
    return False


def _no_trade_reason(signals: list[dict[str, Any]], notifications: list[dict[str, Any]]) -> str:
    for row in notifications:
        message = _notification_message(row)
        for line in message.splitlines():
            if line.lower().startswith("reason:"):
                return line.split(":", 1)[1].strip()
    reasons = [
        str(row.get("no_trade_reason") or "")
        for row in signals
        if row.get("no_trade_reason")
    ]
    return "; ".join(sorted(set(reasons)))


def _has_source_failure(
    source_rows: list[dict[str, Any]],
    provider_rows: list[dict[str, Any]],
) -> bool:
    failure_statuses = {"error", "failed", "failure", "blocked", "no_data"}
    for row in [*source_rows, *provider_rows]:
        status = str(row.get("status") or "").lower()
        if status in failure_statuses:
            return True
    return False


def _source_label(row: dict[str, Any], config: dict[str, Any], source_kind: str) -> str:
    if (
        row.get("paid_data")
        or config.get("paid_data")
        or source_kind in {"paid", "api", "paid/api"}
    ):
        return "paid/api data"
    if row.get("manual_uploaded_data") or source_kind == "manual":
        return "manual shadow data"
    if (
        source_kind in {"web_url", "public_table_url"}
        or row.get("shadow_mode")
        or config.get("shadow_mode")
    ):
        return "public/free unverified shadow data"
    if source_kind == "local_inbox":
        return "local inbox shadow data"
    return source_kind or "unknown data source"


def _research_label(row: dict[str, Any], *, no_trade: bool) -> str:
    raw = str(
        row.get("signal_label")
        or row.get("label")
        or row.get("classification")
        or row.get("action")
        or ""
    ).upper()
    if raw in _RESEARCH_LABELS:
        return raw
    if no_trade:
        return "NO CLEAN EDGE"
    if row.get("no_trade_reason") or row.get("avoid_reasons"):
        return "AVOID"
    if row.get("can_alert"):
        return "WATCH"
    return "CAUTION"


def _explicit_exit(row: dict[str, Any]) -> dict[str, Any] | None:
    policy = _first_non_empty(row.get("recommended_exit_policy"), row.get("exit_policy"))
    price = _number_or_none(
        _first_non_empty(row.get("recommended_exit_price"), row.get("exit_price"))
    )
    ret = _number_or_none(
        _first_non_empty(row.get("recommended_exit_return"), row.get("recommended_exit_return_pct"))
    )
    if not policy and price is None and ret is None:
        return None
    return {
        "recommended_exit_policy": str(policy or "explicit_exit_recorded"),
        "recommended_exit_price": price,
        "recommended_exit_return": ret,
    }


def _monitor_exit(
    pick: dict[str, Any],
    day: str,
    context: dict[str, Any],
    entry: float | None,
) -> dict[str, Any] | None:
    ticker = str(pick.get("ticker") or "").upper()
    scan_id = str(pick.get("scan_id") or pick.get("run_id") or "")
    events = [
        *context["monitor_events_by_key"].get((scan_id, ticker), []),
        *context["monitor_events_by_date_ticker"].get((day, ticker), []),
    ]
    signal_time = str(pick.get("timestamp") or pick.get("as_of_timestamp") or "")
    candidates = []
    for event in events:
        if not _is_monitor_exit_event(event):
            continue
        if not _outcome_after_signal(event, signal_time):
            continue
        candidates.append(event)
    if not candidates:
        return None
    event = sorted(
        candidates,
        key=lambda row: str(row.get("created_at") or row.get("sent_at") or ""),
    )[0]
    price = _number_or_none(
        _first_non_empty(event.get("current_price"), event.get("exit_price"), event.get("price"))
    )
    return_value = _return_from_prices(price, entry) if price is not None else None
    return {
        "recommended_exit_policy": "monitor_exit_signal",
        "recommended_exit_price": price,
        "recommended_exit_return": return_value,
        "monitor_exit_return": return_value,
        "notes": "Monitor exit signal saved."
        if price is not None
        else "Monitor exit signal saved without a price.",
    }


def _is_monitor_exit_event(event: dict[str, Any]) -> bool:
    text = " ".join(
        str(event.get(key) or "")
        for key in ("event_type", "status", "label", "suggested_action", "severity")
    ).upper()
    return "INVALIDATED" in text or "THESIS BROKEN" in text or "INVALIDATED" in text


def _first_valid_outcome(
    keyed: list[dict[str, Any]],
    dated: list[dict[str, Any]],
    *,
    signal_time: str,
) -> dict[str, Any]:
    rows = _dedupe_rows([*keyed, *dated], keys=("scan_id", "ticker", "entry_time", "created_at"))
    for row in rows:
        if _outcome_after_signal(row, signal_time):
            return row
    return {}


def _outcome_after_signal(row: dict[str, Any], signal_time: str) -> bool:
    if not signal_time:
        return True
    outcome_time = _first_non_empty(
        row.get("entry_time"),
        row.get("recommendation_timestamp"),
        row.get("created_at"),
        row.get("uploaded_at"),
        row.get("sent_at"),
    )
    if not outcome_time:
        return True
    signal_dt = _parse_datetime(signal_time)
    outcome_dt = _parse_datetime(str(outcome_time))
    if signal_dt is None or outcome_dt is None:
        return True
    return outcome_dt >= signal_dt


def _has_any_return(row: dict[str, Any]) -> bool:
    return any(
        _number_or_none(row.get(field)) is not None
        for field in CALENDAR_RETURN_POLICIES.values()
    )


def _pick_return(
    primary: dict[str, Any] | None,
    fallback: dict[str, Any] | None,
    key: str,
) -> Any:
    return _first_non_empty(
        dict(primary or {}).get(key),
        dict(primary or {}).get(key.replace("_pct", "")),
        dict(fallback or {}).get(key),
        dict(fallback or {}).get(key.replace("_pct", "")),
    )


def _extreme_return(
    rows: list[dict[str, Any]],
    field: str,
    *,
    best: bool,
) -> float | None:
    values = [_number_or_none(row.get(field)) for row in rows]
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return round(max(usable) if best else min(usable), 4)


def _hit_rate(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [_number_or_none(row.get(field)) for row in rows]
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return round((sum(1 for value in usable if value > 0) / len(usable)) * 100, 2)


def _outcome_commands(day: str) -> list[str]:
    path = f"data\\inbox\\outcomes\\outcomes_{day}.csv"
    return [
        "py -m intraday_scanner.cli import-manual-outcomes "
        f"--input {path} --db-path data\\shadow_real.sqlite --persist",
        "py -m intraday_scanner.cli audit-manual-outcomes "
        "--db-path data\\shadow_real.sqlite --out-dir outputs\\manual_audit --persist",
        "py -m intraday_scanner.cli alpha-learn --db-path data\\shadow_real.sqlite",
        "py -m intraday_scanner.cli alpha-report "
        "--db-path data\\shadow_real.sqlite --out-dir outputs\\alpha_report",
    ]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()  # noqa: S608
    return {str(row[1]) for row in rows}


def _select_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: list[str],
    *,
    order_by: str = "",
) -> list[dict[str, Any]]:
    existing = _table_columns(connection, table)
    selected = [column for column in columns if column in existing]
    if not selected:
        return []
    query = f"SELECT {', '.join(selected)} FROM {table}"  # noqa: S608
    if order_by:
        query += f" ORDER BY {order_by}"
    rows = connection.execute(query).fetchall()
    return [
        {column: row[column] if column in row.keys() else None for column in selected}
        for row in rows
    ]


def _payload_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: list[str],
    *,
    order_by: str = "",
) -> list[dict[str, Any]]:
    rows = []
    for row in _select_rows(connection, table, columns, order_by=order_by):
        payload = _json_dict(row.get("payload_json"))
        merged = {key: value for key, value in row.items() if key != "payload_json"}
        merged.update(payload)
        rows.append(merged)
    return rows


def _merged_json_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: list[str],
    *,
    json_columns: tuple[str, ...],
    order_by: str = "",
) -> list[dict[str, Any]]:
    rows = []
    for row in _select_rows(connection, table, columns, order_by=order_by):
        merged = {key: value for key, value in row.items() if key not in json_columns}
        for column in json_columns:
            merged.update(_json_dict(row.get(column)))
        rows.append(merged)
    return rows


def _safe_order_by(
    connection: sqlite3.Connection,
    table: str,
    candidates: list[str],
) -> str:
    existing = _table_columns(connection, table)
    selected = [column for column in candidates if column in existing]
    return ", ".join(selected)


def _json_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = str(value).replace(",", ";").split(";")
    if isinstance(decoded, list):
        return [str(item) for item in decoded if str(item).strip()]
    return [str(decoded)] if str(decoded).strip() else []


def _missing_calendar_table_warnings(tables: set[str]) -> list[str]:
    expected = {
        "scan_runs",
        "alpha_signals",
        "historical_signals",
        "signal_events",
        "signal_outcomes",
        "signal_return_attribution",
        "daily_signal_performance",
        "alpha_feature_vectors",
        "ranked_candidates",
        "top_explosive",
        "recommendation_theses",
        "notifications_sent",
        "manual_outcomes",
        "manual_audit_trades",
        "manual_audit_summary",
        "source_health",
        "provider_health",
        "shadow_reports",
        "performance_daily",
        "performance_cumulative",
    }
    missing = sorted(table for table in expected if table not in tables)
    if "alpha_outcome_labels" not in tables and "outcome_labels" not in tables:
        missing.append("alpha_outcome_labels/outcome_labels")
    if "alpha_source_reliability" not in tables and "source_reliability" not in tables:
        missing.append("alpha_source_reliability/source_reliability")
    if "alpha_reports" not in tables and "alpha_report" not in tables:
        missing.append("alpha_reports/alpha_report")
    if not missing:
        return []
    preview = ", ".join(missing[:8])
    suffix = "..." if len(missing) > 8 else ""
    return [f"Missing optional calendar tables: {preview}{suffix}"]


def _dedupe_rows(rows: list[dict[str, Any]], *, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    deduped = []
    for row in rows:
        identity = tuple(str(row.get(key) or "") for key in keys)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(row)
    return deduped


def _run_date(context: dict[str, Any], run_id: Any) -> str:
    run = dict(context.get("run_by_id", {}).get(str(run_id or ""), {}) or {})
    return str(run.get("date") or "")


def _key(scan_id: Any, ticker: Any) -> tuple[str, str] | None:
    scan = str(scan_id or "")
    symbol = str(ticker or "").upper()
    return (scan, symbol) if scan and symbol else None


def _date_keys_between(start: str, end: str) -> list[str]:
    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    days = []
    current = start_dt
    while current <= end_dt:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _date_key(value: str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if len(text) >= 10:
        return text[:10]
    return date.today().isoformat()


def _date_from_values(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if len(text) >= 10:
            return text[:10]
    return ""


def _parse_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text or len(text) < 10:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _default_calendar_range(db_path: str | Path) -> tuple[str, str]:
    dates: list[str] = []
    path = Path(db_path)
    if path.exists():
        try:
            with sqlite3.connect(path) as connection:
                connection.row_factory = sqlite3.Row
                tables = _table_names(connection)
                for table, column in (
                    ("scan_runs", "created_at"),
                    ("alpha_signals", "timestamp"),
                    ("historical_signals", "market_date"),
                    ("signal_outcomes", "market_date"),
                    ("daily_signal_performance", "market_date"),
                    ("notifications_sent", "sent_at"),
                    ("manual_audit_trades", ""),
                ):
                    if table not in tables:
                        continue
                    if table == "manual_audit_trades":
                        for row in _payload_rows(connection, table, ["payload_json"]):
                            row_date = _date_from_values(
                                row.get("recommendation_timestamp"),
                                row.get("entry_time"),
                                row.get("date"),
                            )
                            if row_date:
                                dates.append(row_date)
                        continue
                    for row in _select_rows(connection, table, [column]):
                        row_date = _date_from_values(row.get(column))
                        if row_date:
                            dates.append(row_date)
        except sqlite3.Error:
            dates = []
    if dates:
        anchor = datetime.strptime(max(dates), "%Y-%m-%d").date()
    else:
        anchor = date.today()
    start = anchor.replace(day=1)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _number_or_none(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value).replace("$", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _return_from_prices(exit_price: float | None, entry_price: float | None) -> float | None:
    if exit_price is None or entry_price is None or entry_price == 0:
        return None
    return round(((exit_price - float(entry_price)) / float(entry_price)) * 100.0, 4)


def _int(value: Any, default: int = 0) -> int:
    number = _number_or_none(value)
    return int(number) if number is not None else default


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in {None, ""}:
            return value
    return ""


def _provider_health_counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        provider = str(row.get("provider", ""))
        if provider.endswith(":counts"):
            try:
                return json.loads(str(row.get("detail") or "{}"))
            except json.JSONDecodeError:
                return {}
    return {}


def _live_readiness(data: dict[str, Any]) -> list[dict[str, str]]:
    health = list(data.get("provider_health") or [])
    counts = dict(data.get("provider_health_counts") or {})
    ranked = list(data.get("ranked_candidates") or [])
    checks = [
        ("database", "pass" if data.get("summary") else "blocked", "Persisted scan exists"),
        ("provider_health", "pass" if health else "blocked", "Provider health rows exist"),
        (
            "universe_counts",
            "pass" if counts.get("symbols_requested") else "blocked",
            "Universe/provider count telemetry exists",
        ),
        ("ranked_candidates", "pass" if ranked else "blocked", "Ranked candidates exist"),
        (
            "alerts",
            "pass" if data.get("recent_alerts") or data.get("monitor_events") else "partial",
            "Monitor alert history exists",
        ),
        (
            "shadow_mode",
            "pass" if data.get("shadow_mode") or data.get("manual_outcomes") else "partial",
            "Manual/free shadow records exist",
        ),
    ]
    return [{"check": name, "status": status, "detail": detail} for name, status, detail in checks]
