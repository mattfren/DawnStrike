from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path
from typing import Any, cast

import altair as alt
import pandas as pd
import streamlit as st

from intraday_scanner.config import load_config
from intraday_scanner.dashboard.components import filter_candidates
from intraday_scanner.dashboard.data_loader import load_output_dir, load_sample_scan, load_sqlite
from intraday_scanner.errors import IntradayScannerError
from intraday_scanner.expectancy import estimate_expectancy
from intraday_scanner.notifiers import scan_events_from_payload
from intraday_scanner.notifiers.telegram_formatter import format_morning_watchlist
from intraday_scanner.providers import CSVProvider
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.reporting import read_csv_dicts, write_scan_outputs
from intraday_scanner.services.audit_service import run_paper_audit_rows
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.services.setup_monitor import run_setup_monitor
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

WATCHLIST_COLUMNS = [
    "rank",
    "ticker",
    "score",
    "setup_grade",
    "gap_pct",
    "dollar_volume",
    "breakout_trigger",
    "invalidation_level",
    "first_target",
]

MONITOR_COLUMNS = [
    "rank",
    "ticker",
    "status",
    "monitor_confidence_pct",
    "current_price",
    "breakout_trigger",
    "invalidation_level",
    "first_target",
    "path_progress_pct",
]

DASHBOARD_COLUMNS = [
    "rank",
    "ticker",
    "decision",
    "alpha_score",
    "edge_bucket",
    "no_trade_reason",
    "total_score",
    "expected_return_bucket",
    "confidence_bucket",
    "breakout_trigger",
    "invalidation_level",
    "first_target",
    "source_confidence",
    "model_version",
    "config_hash",
]

AUDIT_COLUMNS = [
    "ticker",
    "triggered",
    "entry_price",
    "lunch_return_pct",
    "close_return_pct",
    "close_return_status",
    "high_return_pct",
    "low_drawdown_pct",
]

EXPECTANCY_COLUMNS = [
    "ticker",
    "expected_return_pct",
    "confidence_pct",
    "lower_return_pct",
    "upper_return_pct",
    "risk_adjusted_return_pct",
    "explanation",
]

AVOID_COLUMNS = [
    "rank",
    "ticker",
    "score",
    "gap_pct",
    "dollar_volume",
    "risk_flags",
    "avoid_reasons",
]

MANUAL_OUTCOME_COLUMNS = [
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
]

SHADOW_AUDIT_COLUMNS = [
    "audit_status",
    "ticker",
    "rank",
    "return_1m_pct",
    "return_5m_pct",
    "return_15m_pct",
    "lunch_return_pct",
    "close_return_pct",
    "high_return_pct",
    "low_drawdown_pct",
]

FRIENDLY = {
    "confirming": "Confirming",
    "watching": "Watching",
    "extended": "Extended",
    "fading": "Fading",
    "invalidated": "Invalidated",
    "missing": "Missing",
    "high_liquidity": "High liquidity",
    "watchable_liquidity": "Watchable liquidity",
    "thin_liquidity": "Thin liquidity",
    "illiquid": "Illiquid",
    "current_halt": "Current halt",
    "recent_offering": "Recent offering",
    "wide_spread": "Wide spread",
    "reverse_split_90d": "Reverse split",
    "extreme_gap_above_300_pct": "Extreme gap",
    "low_share_volume": "Low share volume",
    "low_dollar_volume": "Low dollar volume",
}


def main() -> None:
    st.set_page_config(page_title="Dawnstrike", layout="wide", initial_sidebar_state="collapsed")
    _theme()
    config = load_config()
    _init_defaults(config)

    settings = _sidebar(config)
    data = _load_data(settings, config)
    if data is None:
        return

    state = _build_state(data, settings, config)
    _header(state)

    overview, run_flow, watchlist, monitor, audit, history, settings_tab = st.tabs(
        ["Dashboard", "Run", "Picks", "5-Min Check", "Backtest", "History", "Settings"]
    )

    with overview:
        _today(state)

    with run_flow:
        _run_flow(state, config)

    with watchlist:
        _watchlist(state)

    with monitor:
        _monitor(state)

    with audit:
        _audit(state)

    with history:
        _history(state)

    with settings_tab:
        _settings(state, config)

    if settings["refresh"]:
        st.rerun()


def _init_defaults(config: Any) -> None:
    defaults = {
        "data_source": "SQLite",
        "snapshot_path": "sample_data/premarket_snapshot_sample.csv",
        "minute_bars_path": "sample_data/minute_bars/2026-06-18.csv",
        "scan_output_dir": str(config.output_dir),
        "audit_output_dir": "outputs/latest_audit",
        "monitor_output_dir": "outputs/latest_monitor",
        "db_path": str(config.database_path),
        "rows_to_show": int(config.top_n),
        "minimum_score": 0,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _sidebar(config: Any) -> dict[str, Any]:
    return {
        "data_source": str(st.session_state.get("data_source", "SQLite")),
        "rows_to_show": int(st.session_state.get("rows_to_show", config.top_n)),
        "minimum_score": float(st.session_state.get("minimum_score", 0)),
        "refresh": False,
        "snapshot_path": str(st.session_state.get("snapshot_path", "")),
        "minute_bars_path": str(st.session_state.get("minute_bars_path", "")),
        "db_path": str(st.session_state.get("db_path", "")),
        "scan_output_dir": str(st.session_state.get("scan_output_dir", "")),
        "audit_output_dir": str(st.session_state.get("audit_output_dir", "")),
        "monitor_output_dir": str(st.session_state.get("monitor_output_dir", "")),
    }


def _load_data(settings: dict[str, Any], config: Any) -> dict[str, Any] | None:
    try:
        if settings["data_source"] == "sample CSV":
            return load_sample_scan(settings["snapshot_path"], config)
        if settings["data_source"] == "latest output":
            return load_output_dir(settings["scan_output_dir"])
        return load_sqlite(settings["db_path"])
    except (IntradayScannerError, OSError, ValueError) as exc:
        st.error(str(exc))
        return None


def _build_state(
    data: dict[str, Any],
    settings: dict[str, Any],
    config: Any,
) -> dict[str, Any]:
    summary = dict(data.get("summary", {}) or {})
    ranked_all = list(data.get("ranked_candidates", []) or [])
    ranked = filter_candidates(
        ranked_all,
        float(settings["minimum_score"]),
        int(settings["rows_to_show"]),
    )
    top = list(data.get("top_explosive", []) or [])
    avoid = list(data.get("avoid_list", []) or [])
    history = list(data.get("scan_history", []) or [])
    provider_health = list(data.get("provider_health", []) or [])
    provider_health_counts = dict(data.get("provider_health_counts", {}) or {})
    live_readiness = list(data.get("live_readiness", []) or [])
    data_source_kind = str(data.get("data_source_kind", settings["data_source"]))
    performance_report = dict(data.get("performance_report", {}) or {})
    shadow_report = dict(data.get("shadow_report", {}) or {})
    manual_snapshot_uploads = list(data.get("manual_snapshot_uploads", []) or [])
    manual_outcomes = list(data.get("manual_outcomes", []) or [])
    manual_audit_trades = list(data.get("manual_audit_trades", []) or [])
    manual_audit_summary = dict(data.get("manual_audit_summary", {}) or {})
    screener_automation_status = dict(data.get("screener_automation_status", {}) or {})
    screener_automation_runs = list(data.get("screener_automation_runs", []) or [])
    automation_status = dict(data.get("automation_status", {}) or {})
    web_automation_status = dict(data.get("web_automation_status", {}) or {})
    automation_runs = list(data.get("automation_runs", []) or [])
    recent_notifications = list(data.get("recent_notifications", []) or [])
    recent_alerts = list(data.get("recent_alerts", []) or [])
    monitor_events = list(data.get("monitor_events", []) or [])
    recommendation_history = list(data.get("recommendation_history", []) or [])
    audit_trades_from_db = list(data.get("audit_trades", []) or [])
    alpha_signals = list(data.get("alpha_signals", []) or [])
    alpha_feature_vectors = list(data.get("alpha_feature_vectors", []) or [])
    alpha_outcome_labels = list(data.get("alpha_outcome_labels", []) or [])
    alpha_source_reliability = dict(data.get("alpha_source_reliability", {}) or {})
    alpha_setup_memory = dict(data.get("alpha_setup_memory", {}) or {})
    alpha_learning_runs = list(data.get("alpha_learning_runs", []) or [])
    audit_rows, audit_summary = _load_audit(settings["audit_output_dir"])
    monitor_rows = _load_monitor_rows(settings["db_path"], settings["monitor_output_dir"])
    expectancy = [
        estimate.to_dict()
        for estimate in estimate_expectancy(ranked, audit_rows)
    ]
    return {
        "settings": settings,
        "config": config,
        "summary": summary,
        "ranked": ranked,
        "ranked_all": ranked_all,
        "top": top,
        "avoid": avoid,
        "history": history,
        "provider_health": provider_health,
        "provider_health_counts": provider_health_counts,
        "live_readiness": live_readiness,
        "data_source_kind": data_source_kind,
        "performance_report": performance_report,
        "shadow_report": shadow_report,
        "manual_snapshot_uploads": manual_snapshot_uploads,
        "manual_outcomes": manual_outcomes,
        "manual_audit_trades": manual_audit_trades,
        "manual_audit_summary": manual_audit_summary,
        "screener_automation_status": screener_automation_status,
        "screener_automation_runs": screener_automation_runs,
        "automation_status": automation_status,
        "web_automation_status": web_automation_status,
        "automation_runs": automation_runs,
        "recent_notifications": recent_notifications,
        "shadow_mode": bool(data.get("shadow_mode") or shadow_report or manual_outcomes),
        "recent_alerts": recent_alerts,
        "monitor_events": monitor_events,
        "recommendation_history": recommendation_history,
        "audit_trades_from_db": audit_trades_from_db,
        "alpha_signals": alpha_signals,
        "alpha_feature_vectors": alpha_feature_vectors,
        "alpha_outcome_labels": alpha_outcome_labels,
        "alpha_source_reliability": alpha_source_reliability,
        "alpha_setup_memory": alpha_setup_memory,
        "alpha_learning_runs": alpha_learning_runs,
        "audit_rows": audit_rows,
        "audit_summary": audit_summary,
        "monitor_rows": monitor_rows,
        "expectancy": expectancy,
    }


def _load_audit(audit_output_dir: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = Path(audit_output_dir)
    trades = _read_csv(base / "paper_audit_trades.csv")
    summary = _read_json(base / "paper_audit_summary.json")
    return trades, summary


def _load_monitor_rows(db_path: str, monitor_output_dir: str) -> list[dict[str, Any]]:
    try:
        rows = SQLiteScanStore(db_path).load_latest_monitor_checks()
        if rows:
            return rows
    except (IntradayScannerError, OSError):
        pass
    return _read_csv(Path(monitor_output_dir) / "setup_monitor_checks.csv")


def _read_csv(path: str | Path) -> list[dict[str, Any]]:
    csv_path = Path(path)
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: str | Path) -> dict[str, Any]:
    json_path = Path(path)
    if not json_path.exists():
        return {}
    return json.loads(json_path.read_text(encoding="utf-8"))


def _theme() -> None:
    st.markdown(
        """
        <style>
        :root {
            --bg: #070b12;
            --surface: #0f1724;
            --surface-soft: #141d2c;
            --line: #263246;
            --line-strong: #3a4a63;
            --text: #eef4ff;
            --muted: #94a3b8;
            --blue: #6ea8fe;
            --green: #2dd4bf;
            --amber: #fbbf24;
            --red: #fb7185;
            --copy: #cbd5e1;
        }
        .stApp {
            background: var(--bg);
            color: var(--text);
        }
        .block-container {
            max-width: 1120px;
            padding-top: 2.2rem;
            padding-bottom: 2.4rem;
        }
        section[data-testid="stSidebar"] {
            display: none;
        }
        [data-testid="collapsedControl"] {
            display: none;
        }
        header[data-testid="stHeader"],
        div[data-testid="stToolbar"] {
            display: none;
        }
        footer {
            display: none;
        }
        h1, h2, h3, p, label {
            letter-spacing: 0;
        }
        input,
        textarea,
        [data-baseweb="input"],
        [data-baseweb="base-input"],
        [data-baseweb="select"] > div {
            background: var(--surface) !important;
            border-color: var(--line) !important;
            color: var(--text) !important;
        }
        [data-baseweb="select"] span,
        [data-baseweb="select"] div {
            color: var(--text) !important;
        }
        [data-baseweb="slider"] [role="slider"] {
            background: var(--blue) !important;
            border-color: var(--blue) !important;
        }
        [data-baseweb="slider"] div {
            color: var(--text) !important;
        }
        div[data-testid="stMetric"] {
            background: var(--surface);
            border: 1px solid var(--line);
            border-radius: 8px;
            padding: 0.78rem 0.9rem;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
        }
        [data-testid="stMetricLabel"] {
            color: var(--muted);
        }
        [data-testid="stMetricValue"] {
            color: var(--text);
            font-size: 1.42rem;
        }
        div[data-testid="stDataFrame"],
        div[data-testid="stVegaLiteChart"] {
            background: var(--surface);
            border: 1px solid var(--line);
            border-radius: 10px;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
        }
        div[data-testid="stVegaLiteChart"] {
            padding: 0.65rem;
        }
        .stTabs [data-baseweb="tab-list"] {
            gap: 0.25rem;
            border-bottom: 1px solid var(--line);
        }
        .stTabs [data-baseweb="tab"] {
            color: var(--muted);
            padding: 0.6rem 0.85rem;
        }
        .stTabs [aria-selected="true"] {
            color: var(--text);
            border-bottom: 2px solid var(--blue);
        }
        div.stButton > button {
            background: var(--surface);
            border: 1px solid var(--line-strong);
            border-radius: 8px;
            color: var(--text);
            font-weight: 650;
        }
        div.stButton > button:hover {
            border-color: var(--blue);
            color: var(--blue);
        }
        .ds-hero {
            align-items: flex-start;
            border-bottom: 1px solid var(--line);
            display: flex;
            justify-content: space-between;
            gap: 1.2rem;
            margin-bottom: 1.1rem;
            padding-bottom: 1rem;
        }
        .ds-title {
            color: var(--text);
            font-size: 2rem;
            font-weight: 800;
            line-height: 1.08;
        }
        .ds-sub {
            color: var(--muted);
            font-size: 0.95rem;
            margin-top: 0.35rem;
        }
        .ds-chip-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            justify-content: flex-end;
        }
        .ds-chip {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 999px;
            color: var(--muted);
            font-size: 0.76rem;
            padding: 0.28rem 0.62rem;
        }
        .ds-strip {
            display: grid;
            gap: 0.85rem;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            margin: 0.55rem 0 1.05rem;
        }
        .ds-step {
            background: var(--surface);
            border: 1px solid var(--line);
            border-radius: 10px;
            min-height: 5.4rem;
            padding: 0.9rem;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
        }
        .ds-step-label {
            color: var(--muted);
            font-size: 0.72rem;
            font-weight: 700;
            text-transform: uppercase;
        }
        .ds-step-value {
            color: var(--text);
            font-size: 1.12rem;
            font-weight: 760;
            line-height: 1.25;
            margin-top: 0.18rem;
        }
        .ds-step-note {
            color: var(--muted);
            font-size: 0.78rem;
            line-height: 1.32;
            margin-top: 0.24rem;
        }
        .ds-section {
            color: var(--muted);
            font-size: 0.78rem;
            font-weight: 750;
            margin: 0.2rem 0 0.55rem;
            text-transform: uppercase;
        }
        .ds-read {
            background: #ffffff;
            border: 1px solid var(--line);
            border-left: 3px solid var(--blue);
            border-radius: 10px;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
            margin: 0.25rem 0 1.05rem;
            padding: 1rem 1.05rem;
        }
        .ds-read-title {
            color: var(--blue);
            font-size: 0.76rem;
            font-weight: 760;
            margin-bottom: 0.32rem;
            text-transform: uppercase;
        }
        .ds-read-body {
            color: var(--text);
            font-size: 1rem;
            line-height: 1.45;
        }
        .ds-small {
            color: var(--muted);
            font-size: 0.82rem;
            line-height: 1.35;
            margin: -0.2rem 0 0.55rem;
        }
        .ds-path {
            display: grid;
            gap: 0.75rem;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            margin: 0.4rem 0 1rem;
        }
        .ds-path-card {
            background: var(--surface);
            border: 1px solid var(--line);
            border-radius: 10px;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
            padding: 0.9rem;
        }
        .ds-path-num {
            color: var(--blue);
            font-size: 0.75rem;
            font-weight: 750;
            margin-bottom: 0.2rem;
        }
        .ds-path-title {
            color: var(--text);
            font-size: 1rem;
            font-weight: 750;
        }
        .ds-path-note {
            color: var(--muted);
            font-size: 0.78rem;
            line-height: 1.32;
            margin-top: 0.28rem;
        }
        .ds-next {
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            border-radius: 10px;
            color: #1e3a8a;
            margin: 0.5rem 0 1rem;
            padding: 0.85rem 1rem;
        }
        .ds-brief {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 16px;
            box-shadow: 0 12px 30px rgba(16, 24, 40, 0.08);
            margin: 0.8rem 0 1rem;
            padding: 1.35rem 1.45rem;
        }
        .ds-brief-kicker {
            color: var(--blue);
            font-size: 0.78rem;
            font-weight: 800;
            margin-bottom: 0.3rem;
            text-transform: uppercase;
        }
        .ds-brief-title {
            color: var(--text);
            font-size: 2.15rem;
            font-weight: 850;
            line-height: 1;
            margin-bottom: 0.5rem;
        }
        .ds-brief-copy {
            color: #344054;
            font-size: 1.02rem;
            line-height: 1.45;
            max-width: 760px;
        }
        .ds-brief-grid {
            display: grid;
            gap: 0.8rem;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            margin-top: 1.05rem;
        }
        .ds-brief-label,
        .ds-level-label {
            color: var(--muted);
            font-size: 0.72rem;
            font-weight: 750;
            text-transform: uppercase;
        }
        .ds-brief-value {
            color: var(--text);
            font-size: 1.14rem;
            font-weight: 760;
            margin-top: 0.18rem;
        }
        .ds-brief-note {
            color: var(--muted);
            font-size: 0.88rem;
            line-height: 1.35;
            margin-top: 0.85rem;
        }
        .ds-level-grid {
            display: grid;
            gap: 0.75rem;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            margin: 0.5rem 0 1.25rem;
        }
        .ds-level-card {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 0.9rem;
        }
        .ds-level-value {
            color: var(--text);
            font-size: 1.2rem;
            font-weight: 800;
            margin-top: 0.16rem;
        }
        .ds-level-note {
            color: var(--muted);
            font-size: 0.8rem;
            line-height: 1.3;
            margin-top: 0.28rem;
        }
        .ds-card-grid {
            display: grid;
            gap: 0.85rem;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            margin: 0.45rem 0 1.2rem;
        }
        .ds-candidate-card {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 14px;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
            padding: 1rem;
        }
        .ds-candidate-top {
            align-items: baseline;
            display: flex;
            justify-content: space-between;
            gap: 0.7rem;
        }
        .ds-candidate-ticker {
            color: var(--text);
            font-size: 1.35rem;
            font-weight: 850;
            line-height: 1;
        }
        .ds-candidate-rank {
            color: var(--muted);
            font-size: 0.78rem;
            font-weight: 750;
        }
        .ds-candidate-score {
            color: var(--blue);
            font-size: 0.92rem;
            font-weight: 800;
            margin-top: 0.4rem;
        }
        .ds-candidate-levels {
            border-top: 1px solid var(--line);
            margin-top: 0.8rem;
            padding-top: 0.75rem;
        }
        .ds-candidate-row {
            display: flex;
            justify-content: space-between;
            gap: 0.6rem;
            margin: 0.24rem 0;
        }
        .ds-candidate-row span:first-child {
            color: var(--muted);
            font-size: 0.78rem;
        }
        .ds-candidate-row span:last-child {
            color: var(--text);
            font-size: 0.82rem;
            font-weight: 750;
        }
        .ds-dashboard-hero {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 18px;
            box-shadow: 0 14px 34px rgba(16, 24, 40, 0.08);
            margin: 0.65rem 0 1rem;
            padding: 1.4rem;
        }
        .ds-dashboard-top {
            align-items: flex-start;
            display: flex;
            gap: 1rem;
            justify-content: space-between;
        }
        .ds-dashboard-kicker {
            color: var(--blue);
            font-size: 0.78rem;
            font-weight: 850;
            text-transform: uppercase;
        }
        .ds-dashboard-title {
            color: var(--text);
            font-size: 2.35rem;
            font-weight: 880;
            letter-spacing: 0;
            line-height: 1;
            margin-top: 0.25rem;
        }
        .ds-dashboard-read {
            color: #344054;
            font-size: 1.04rem;
            line-height: 1.45;
            margin-top: 0.75rem;
            max-width: 820px;
        }
        .ds-decision-pill {
            border-radius: 999px;
            font-size: 0.8rem;
            font-weight: 850;
            padding: 0.4rem 0.75rem;
            white-space: nowrap;
        }
        .ds-decision--go {
            background: #ecfdf3;
            color: var(--green);
        }
        .ds-decision--wait {
            background: #eff6ff;
            color: var(--blue);
        }
        .ds-decision--caution {
            background: #fffbeb;
            color: var(--amber);
        }
        .ds-decision--exit {
            background: #fef3f2;
            color: var(--red);
        }
        .ds-decision--neutral {
            background: #f2f4f7;
            color: var(--muted);
        }
        .ds-dashboard-grid {
            display: grid;
            gap: 0.85rem;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            margin: 1rem 0 1.1rem;
        }
        .ds-dashboard-panel {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 1rem;
        }
        .ds-panel-title {
            color: var(--muted);
            font-size: 0.74rem;
            font-weight: 850;
            text-transform: uppercase;
        }
        .ds-panel-main {
            color: var(--text);
            font-size: 1.22rem;
            font-weight: 850;
            margin-top: 0.28rem;
        }
        .ds-panel-copy {
            color: #475467;
            font-size: 0.88rem;
            line-height: 1.38;
            margin-top: 0.35rem;
        }
        .ds-simple-levels {
            display: grid;
            gap: 0.6rem;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            margin-top: 1rem;
        }
        .ds-simple-level {
            background: var(--surface-soft);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 0.8rem;
        }
        .ds-simple-level-label {
            color: var(--muted);
            font-size: 0.72rem;
            font-weight: 800;
            text-transform: uppercase;
        }
        .ds-simple-level-value {
            color: var(--text);
            font-size: 1.16rem;
            font-weight: 850;
            margin-top: 0.18rem;
        }
        .ds-broker-flow {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 14px;
            margin: 0.35rem 0 1rem;
            padding: 1rem;
        }
        .ds-flow-row {
            display: grid;
            gap: 0.75rem;
            grid-template-columns: repeat(4, minmax(0, 1fr));
        }
        .ds-flow-step {
            border-left: 3px solid var(--blue);
            padding-left: 0.75rem;
        }
        .ds-flow-num {
            color: var(--blue);
            font-size: 0.72rem;
            font-weight: 850;
            text-transform: uppercase;
        }
        .ds-flow-title {
            color: var(--text);
            font-size: 0.98rem;
            font-weight: 820;
            margin-top: 0.16rem;
        }
        .ds-flow-copy {
            color: var(--muted);
            font-size: 0.8rem;
            line-height: 1.32;
            margin-top: 0.22rem;
        }
        .ds-monitor-brief {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 16px;
            box-shadow: 0 10px 24px rgba(16, 24, 40, 0.07);
            margin: 0.45rem 0 1rem;
            padding: 1.2rem;
        }
        .ds-monitor-top {
            align-items: flex-start;
            display: flex;
            gap: 1rem;
            justify-content: space-between;
        }
        .ds-monitor-kicker {
            color: var(--blue);
            font-size: 0.76rem;
            font-weight: 800;
            text-transform: uppercase;
        }
        .ds-monitor-title {
            color: var(--text);
            font-size: 2rem;
            font-weight: 850;
            line-height: 1;
            margin-top: 0.2rem;
        }
        .ds-monitor-copy {
            color: #344054;
            font-size: 1rem;
            line-height: 1.45;
            margin-top: 0.65rem;
            max-width: 780px;
        }
        .ds-status-pill {
            border-radius: 999px;
            font-size: 0.78rem;
            font-weight: 800;
            padding: 0.36rem 0.7rem;
            white-space: nowrap;
        }
        .ds-status--confirming {
            background: #ecfdf3;
            color: var(--green);
        }
        .ds-status--watching {
            background: #eff6ff;
            color: var(--blue);
        }
        .ds-status--extended,
        .ds-status--fading {
            background: #fffbeb;
            color: var(--amber);
        }
        .ds-status--invalidated,
        .ds-status--missing {
            background: #fef3f2;
            color: var(--red);
        }
        .ds-status--default {
            background: #f2f4f7;
            color: var(--muted);
        }
        .ds-monitor-grid {
            display: grid;
            gap: 0.85rem;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            margin-top: 1rem;
        }
        .ds-monitor-stat {
            background: var(--surface-soft);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 0.85rem;
        }
        .ds-monitor-label {
            color: var(--muted);
            font-size: 0.72rem;
            font-weight: 760;
            text-transform: uppercase;
        }
        .ds-monitor-value {
            color: var(--text);
            font-size: 1.1rem;
            font-weight: 820;
            margin-top: 0.18rem;
        }
        .ds-price-lane {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 14px;
            margin: 0.45rem 0 1.15rem;
            padding: 1rem;
        }
        .ds-price-lane-row {
            display: grid;
            gap: 0.75rem;
            grid-template-columns: repeat(4, minmax(0, 1fr));
        }
        .ds-price-point {
            background: var(--surface-soft);
            border: 1px solid var(--line);
            border-top: 4px solid var(--line-strong);
            border-radius: 12px;
            min-height: 5.2rem;
            padding: 0.8rem;
        }
        .ds-price-point--risk {
            border-top-color: var(--red);
        }
        .ds-price-point--current {
            border-top-color: #98a2b3;
        }
        .ds-price-point--trigger {
            border-top-color: var(--blue);
        }
        .ds-price-point--target {
            border-top-color: var(--green);
        }
        .ds-price-label {
            color: var(--muted);
            font-size: 0.72rem;
            font-weight: 780;
            text-transform: uppercase;
        }
        .ds-price-value {
            color: var(--text);
            font-size: 1.16rem;
            font-weight: 850;
            margin-top: 0.18rem;
        }
        .ds-price-note {
            color: var(--muted);
            font-size: 0.78rem;
            line-height: 1.3;
            margin-top: 0.25rem;
        }
        .ds-monitor-cards {
            display: grid;
            gap: 0.85rem;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            margin: 0.45rem 0 1.1rem;
        }
        .ds-monitor-card {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 14px;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
            padding: 1rem;
        }
        .ds-monitor-card-head {
            align-items: center;
            display: flex;
            gap: 0.75rem;
            justify-content: space-between;
        }
        .ds-monitor-name {
            color: var(--text);
            font-size: 1.2rem;
            font-weight: 850;
        }
        .ds-monitor-meta {
            color: var(--muted);
            font-size: 0.78rem;
            margin-top: 0.2rem;
        }
        .ds-progress {
            margin-top: 0.85rem;
        }
        .ds-progress-track {
            background: #eef2f6;
            border-radius: 999px;
            height: 0.58rem;
            overflow: hidden;
        }
        .ds-progress-fill {
            background: var(--blue);
            border-radius: 999px;
            height: 100%;
        }
        .ds-progress-fill--confirming {
            background: var(--green);
        }
        .ds-progress-fill--extended,
        .ds-progress-fill--fading {
            background: var(--amber);
        }
        .ds-progress-fill--invalidated,
        .ds-progress-fill--missing {
            background: var(--red);
        }
        .ds-progress-caption {
            align-items: center;
            color: var(--muted);
            display: flex;
            font-size: 0.78rem;
            justify-content: space-between;
            margin-top: 0.35rem;
        }
        .ds-monitor-reason {
            color: #344054;
            font-size: 0.88rem;
            line-height: 1.38;
            margin-top: 0.7rem;
        }
        .ds-empty-state {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 16px;
            padding: 1.5rem;
        }
        .ds-empty-title {
            color: var(--text);
            font-size: 1.35rem;
            font-weight: 800;
        }
        .ds-empty-copy {
            color: var(--muted);
            margin-top: 0.35rem;
        }
        .ds-table-wrap {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 10px;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
            margin-bottom: 1rem;
            overflow-x: auto;
        }
        .ds-table {
            border-collapse: collapse;
            min-width: 100%;
            width: 100%;
        }
        .ds-table th {
            background: #f9fafb;
            border-bottom: 1px solid var(--line);
            color: var(--muted);
            font-size: 0.76rem;
            font-weight: 750;
            padding: 0.72rem 0.78rem;
            text-align: left;
            text-transform: uppercase;
            white-space: nowrap;
        }
        .ds-table td {
            border-bottom: 1px solid #f1f5f9;
            color: var(--text);
            font-size: 0.88rem;
            max-width: 260px;
            padding: 0.72rem 0.78rem;
            overflow-wrap: anywhere;
            white-space: normal;
        }
        .ds-table tr:last-child td {
            border-bottom: 0;
        }
        .ds-table .ds-num {
            font-variant-numeric: tabular-nums;
            text-align: right;
        }
        .ds-empty {
            background: var(--surface);
            border: 1px solid var(--line);
            border-radius: 10px;
            color: var(--muted);
            padding: 1rem;
        }
        .ds-chip,
        .ds-read,
        .ds-brief,
        .ds-level-card,
        .ds-candidate-card,
        .ds-dashboard-hero,
        .ds-dashboard-panel,
        .ds-broker-flow,
        .ds-monitor-brief,
        .ds-price-lane,
        .ds-monitor-card,
        .ds-empty-state,
        .ds-table-wrap {
            background: var(--surface);
            border-color: var(--line);
            box-shadow: 0 14px 36px rgba(0, 0, 0, 0.22);
        }
        .ds-next {
            background: #0b1b33;
            border-color: #1d4ed8;
            color: #bfdbfe;
        }
        .ds-brief-copy,
        .ds-dashboard-read,
        .ds-panel-copy,
        .ds-monitor-copy,
        .ds-monitor-reason {
            color: var(--copy);
        }
        .ds-decision--go,
        .ds-status--confirming {
            background: rgba(45, 212, 191, 0.14);
        }
        .ds-decision--wait,
        .ds-status--watching {
            background: rgba(110, 168, 254, 0.16);
        }
        .ds-decision--caution,
        .ds-status--extended,
        .ds-status--fading {
            background: rgba(251, 191, 36, 0.14);
        }
        .ds-decision--exit,
        .ds-status--invalidated,
        .ds-status--missing {
            background: rgba(251, 113, 133, 0.14);
        }
        .ds-decision--neutral,
        .ds-status--default {
            background: rgba(148, 163, 184, 0.14);
        }
        .ds-progress-track {
            background: #1e293b;
        }
        .ds-table th {
            background: #111827;
        }
        .ds-table td {
            border-bottom-color: #1f2937;
        }
        @media (max-width: 900px) {
            .ds-hero {
                display: block;
            }
            .ds-chip-row {
                justify-content: flex-start;
                margin-top: 0.65rem;
            }
            .ds-strip,
            .ds-path,
            .ds-brief-grid,
            .ds-level-grid,
            .ds-card-grid,
            .ds-dashboard-grid,
            .ds-simple-levels,
            .ds-flow-row,
            .ds-monitor-grid,
            .ds-price-lane-row,
            .ds-monitor-cards {
                grid-template-columns: 1fr;
            }
            .ds-dashboard-top {
                display: block;
            }
            .ds-decision-pill {
                display: inline-block;
                margin-top: 0.75rem;
            }
            .ds-monitor-top {
                display: block;
            }
            .ds-status-pill {
                display: inline-block;
                margin-top: 0.7rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _header(state: dict[str, Any]) -> None:
    summary = state["summary"]
    top = _first(state["ranked"])
    top_text = str(top.get("ticker", "No setup")) if top else "No setup"
    timestamp = str(summary.get("created_at", "No saved run"))
    mode_text = "Shadow: manual/free" if state.get("shadow_mode") else "Research only"
    st.markdown(
        f"""
        <div class="ds-hero">
            <div>
                <div class="ds-title">Dawnstrike</div>
                <div class="ds-sub">
                    Run the scan. Review picks. Monitor exits every five minutes.
                </div>
            </div>
            <div class="ds-chip-row">
                <span class="ds-chip">Top setup: {_html(top_text)}</span>
                <span class="ds-chip">{_html(mode_text)}</span>
                <span class="ds-chip">Updated {_html(timestamp)}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _mission_control(state: dict[str, Any]) -> None:
    ranked = state["ranked"]
    avoid = state["avoid"]
    monitor_rows = state["monitor_rows"]
    audit_summary = _audit_summary(state)
    web_status = dict(state.get("web_automation_status") or {})
    latest_web = dict(web_status.get("latest_source_summary") or {})
    top = _first(ranked)
    monitor_top = _first(monitor_rows)
    cards = [
        (
            "Top setup",
            str(top.get("ticker", "None")) if top else "None",
            f"Score {_format_score(top.get('score'))}" if top else "Run a scan",
        ),
        (
            "Candidates",
            str(len(ranked)),
            "Ready to review",
        ),
        (
            "Monitor",
            _friendly(str(monitor_top.get("status", "No check"))) if monitor_top else "No check",
            "Latest monitor result" if monitor_top else "Run Monitor",
        ),
        (
            "Backtest",
            _signed_pct(audit_summary.get("avg_close_return_pct")),
            f"{int(_number(audit_summary.get('trade_count')))} test rows",
        ),
        (
            "Source confidence",
            f"{_format_number(latest_web.get('source_confidence'))}%",
            str(latest_web.get("stale_data_status") or "No source run"),
        ),
    ]
    st.markdown(_step_strip(cards), unsafe_allow_html=True)
    if avoid:
        st.caption(f"Risk list: {len(avoid)} symbols did not pass the safety filters.")


def _today(state: dict[str, Any]) -> None:
    config = state["config"]
    top = _first(state["ranked"])
    if not top:
        st.markdown(
            """
            <div class="ds-empty-state">
                <div class="ds-empty-title">No scan loaded</div>
                <div class="ds-empty-copy">Open Run and press Full Test.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    ticker = str(top.get("ticker", "n/a"))
    estimate = _expectancy_for(state, ticker)
    monitor = _row_by_ticker(state["monitor_rows"], ticker)
    st.markdown(_dashboard_hero(top, estimate, monitor), unsafe_allow_html=True)
    _free_shadow_panel(state)
    _dashboard_actions(state, config)
    _action_result()
    _dashboard_alphaops(state)
    _dashboard_performance(state)
    _dashboard_activity(state)
    st.markdown('<div class="ds-section">Broker Flow</div>', unsafe_allow_html=True)
    st.markdown(_broker_flow(), unsafe_allow_html=True)
    st.markdown('<div class="ds-section">Today\'s Picks</div>', unsafe_allow_html=True)
    _table(_dashboard_rows(state["ranked"][:6], state), DASHBOARD_COLUMNS)
    if state["avoid"]:
        with st.expander("Risk list", expanded=False):
            _table(state["avoid"][:8], AVOID_COLUMNS)
    st.caption(
        "Research only. The app does not place orders, hold broker credentials, or execute trades."
    )


def _dashboard_actions(state: dict[str, Any], config: Any) -> None:
    settings = state["settings"]
    run_col, check_col, auto_col, status_col = st.columns(4)
    _action_button(
        run_col,
        "Run Full Test",
        lambda: _run_full_web_backtest(
            settings["snapshot_path"],
            settings["scan_output_dir"],
            settings["db_path"],
            settings["minute_bars_path"],
            settings["audit_output_dir"],
            settings["monitor_output_dir"],
            int(settings["rows_to_show"]),
            3,
            float(config.slippage_bps),
        ),
        primary=True,
        key="dashboard_run_full_test",
    )
    _action_button(
        check_col,
        "Check Now",
        lambda: _run_web_monitor(
            settings["snapshot_path"],
            settings["monitor_output_dir"],
            settings["db_path"],
            int(settings["rows_to_show"]),
        ),
        key="dashboard_check_now",
    )
    _action_button(
        auto_col,
        "5-Min Monitor",
        lambda: _register_web_tasks(
            settings["snapshot_path"],
            settings["db_path"],
            settings["scan_output_dir"],
            settings["monitor_output_dir"],
        ),
        key="dashboard_register_monitor",
    )
    _action_button(status_col, "Task Status", _scheduled_task_status, key="dashboard_task_status")


def _dashboard_alphaops(state: dict[str, Any]) -> None:
    signals = list(state.get("alpha_signals") or [])
    if not signals:
        return
    top = dict(signals[0])
    labels = list(state.get("alpha_outcome_labels") or [])
    reliability = dict(state.get("alpha_source_reliability") or {})
    setup_memory = dict(state.get("alpha_setup_memory") or {})
    real_days = _distinct_dates(labels)
    enough = "Yes" if real_days >= 20 else "Not yet"
    source = str(top.get("preferred_source") or top.get("source") or "unknown")
    source_score = dict(reliability.get(source) or {}).get("reliability_score")
    memory = dict(setup_memory.get(str(top.get("setup_key") or "")) or {})
    missing_high = sum(1 for row in labels if row.get("missing_outcome_high") is True)
    missing_rate = (missing_high / len(labels)) * 100 if labels else 0
    cards = [
        (
            "Alpha score",
            _format_number(top.get("alpha_score")),
            str(top.get("edge_bucket") or "n/a"),
        ),
        (
            "No-trade reason",
            str(top.get("no_trade_reason") or "Clean"),
            "Risk gate result",
        ),
        ("Source reliability", _format_number(source_score), source),
        (
            "Setup memory",
            _format_number(memory.get("sample_size")),
            f"{_format_number(memory.get('win_rate_pct'))}% win rate",
        ),
        ("Evidence", enough, f"{real_days} real days"),
        ("Missing outcomes", f"{_format_number(missing_rate)}%", "High-after-entry fields"),
    ]
    st.markdown('<div class="ds-section">AlphaOps</div>', unsafe_allow_html=True)
    st.markdown(_step_strip(cards), unsafe_allow_html=True)
    st.caption(
        "Shows Alpha score, edge bucket, no-trade reason, setup memory, source reliability, "
        "score decile, risk impact, outlier dependency, missing outcome rate, and "
        "evidence sufficiency."
    )
    _table(
        signals[:5],
        [
            "rank",
            "ticker",
            "alpha_score",
            "edge_bucket",
            "score_decile",
            "confidence_bucket",
            "expected_return_bucket",
            "no_trade_reason",
        ],
    )


def _dashboard_performance(state: dict[str, Any]) -> None:
    report = dict(state.get("performance_report") or {})
    if not report:
        return
    cards = [
        (
            "Historical avg",
            _signed_pct(report.get("avg_close_return_pct")),
            "Close-exit paper return",
        ),
        (
            "Hit rate",
            f"{_format_number(report.get('hit_rate_close_pct'))}%",
            "Closed positive",
        ),
        (
            "Max drawdown",
            _signed_pct(report.get("max_drawdown_pct")),
            "Worst low-after-entry",
        ),
        (
            "Audit rows",
            _format_number(report.get("trade_count")),
            "Persisted paper trades",
        ),
    ]
    st.markdown('<div class="ds-section">Historical Edge</div>', unsafe_allow_html=True)
    st.markdown(_step_strip(cards), unsafe_allow_html=True)


def _dashboard_activity(state: dict[str, Any]) -> None:
    alerts = list(state.get("recent_alerts") or [])
    provider_health = list(state.get("provider_health") or [])
    provider_counts = dict(state.get("provider_health_counts") or {})
    live_readiness = list(state.get("live_readiness") or [])
    recommendation_history = list(state.get("recommendation_history") or [])
    audit_rows = list(
        state.get("manual_audit_trades")
        or state.get("audit_trades_from_db")
        or state.get("audit_rows")
        or []
    )
    st.markdown('<div class="ds-section">Readiness</div>', unsafe_allow_html=True)
    st.caption(f"Data source: {state.get('data_source_kind', 'unknown')}")
    if state.get("shadow_mode"):
        st.caption(
            "Free Shadow Mode: manual/free validation only. Not paid live data and not a "
            "performance claim."
        )
    if provider_counts:
        _table(
            [provider_counts],
            [
                "symbols_requested",
                "symbols_returned",
                "symbols_with_premarket_volume",
                "symbols_passing_filters",
                "snapshot_row_count",
                "candidate_count",
                "top_explosive_count",
            ],
        )
    if live_readiness:
        _table(live_readiness, ["check", "status", "detail"])
    if alerts:
        st.markdown('<div class="ds-section">Recent Alerts</div>', unsafe_allow_html=True)
        _table(
            alerts[:6],
            [
                "sent_at",
                "ticker",
                "event_type",
                "severity",
                "suggested_action",
                "reason",
                "source_link",
            ],
        )
    if provider_health:
        st.markdown('<div class="ds-section">Provider Health</div>', unsafe_allow_html=True)
        _table(provider_health[:6], ["checked_at", "provider", "status", "detail"])
    if recommendation_history:
        with st.expander("Historical calls", expanded=False):
            _table(
                recommendation_history[:12],
                [
                    "timestamp",
                    "rank",
                    "ticker",
                    "score",
                    "breakout_trigger",
                    "invalidation_level",
                    "first_target",
                    "exit_bias",
                    "confidence_level",
                    "catalyst_url",
                ],
            )
    if audit_rows:
        with st.expander("Actual return rows", expanded=False):
            _table(
                audit_rows[:12],
                [
                    "audit_status",
                    "entry_mode",
                    "ticker",
                    "return_1m_pct",
                    "return_5m_pct",
                    "return_15m_pct",
                    "lunch_return_pct",
                    "close_return_pct",
                    "high_return_pct",
                    "low_drawdown_pct",
                ],
            )


def _free_shadow_panel(state: dict[str, Any]) -> None:
    report = dict(state.get("shadow_report") or {})
    uploads = list(state.get("manual_snapshot_uploads") or [])
    outcomes = list(state.get("manual_outcomes") or [])
    manual_audit = list(state.get("manual_audit_trades") or [])
    automation = dict(state.get("screener_automation_status") or {})
    automation_runs = list(state.get("screener_automation_runs") or [])
    e2e_status = dict(state.get("automation_status") or {})
    web_status = dict(state.get("web_automation_status") or {})
    e2e_runs = list(state.get("automation_runs") or [])
    latest_e2e = dict(e2e_status.get("latest_run") or {})
    latest_notification = dict(e2e_status.get("latest_notification") or {})
    latest_web = dict(web_status.get("latest_source_summary") or {})
    latest_run = dict(automation.get("latest_auto_shadow_run") or {})
    run_summary = dict(latest_run.get("scan_summary") or {})
    normalization = dict(latest_run.get("normalization") or {})
    if not (
        report
        or uploads
        or outcomes
        or automation
        or e2e_status
        or web_status
        or state.get("shadow_mode")
    ):
        st.markdown('<div class="ds-section">Free Shadow Mode</div>', unsafe_allow_html=True)
        st.caption(
            "Drop an exported screener CSV or text table into `data\\inbox\\screener`. "
            "The automation normalizes it, runs a paper-only shadow scan, and archives "
            "the raw file."
        )
        return
    status_text = str(automation.get("normalization_status") or latest_run.get("status") or "Ready")
    cards = [
        (
            "Inbox",
            _format_number(automation.get("inbox_count", 0)),
            str(automation.get("inbox_path") or "data\\inbox\\screener"),
        ),
        (
            "Last status",
            _friendly(status_text),
            "Latest automation run",
        ),
        (
            "Processed",
            _format_number(automation.get("processed_count", 0)),
            "Raw files archived after a run",
        ),
        (
            "Failed",
            _format_number(automation.get("failed_count", 0)),
            "Needs review if above zero",
        ),
    ]
    st.markdown('<div class="ds-section">Free Shadow Mode</div>', unsafe_allow_html=True)
    st.markdown(_step_strip(cards), unsafe_allow_html=True)
    if automation:
        st.markdown(
            _callout(
                "Simple flow",
                "Export the screener, drop the file in the inbox, run the watcher or daily task, "
                "then review these paper-only picks before entering anything manually at "
                "the broker.",
            ),
            unsafe_allow_html=True,
        )
        latest_raw_path = automation.get("latest_raw_screener_file") or latest_run.get("input_path")
        source_cards = [
            (
                "Latest raw file",
                _short_path(latest_raw_path),
                "Unprocessed file in inbox or last input",
            ),
            (
                "Normalized snapshot",
                _short_path(automation.get("latest_normalized_snapshot")),
                "Canonical rows Dawnstrike scored",
            ),
            (
                "Last scan",
                str(run_summary.get("top_ticker") or "No pick yet"),
                f"{_format_number(run_summary.get('ranked_count', 0))} ranked",
            ),
        ]
        st.markdown(_step_strip(source_cards), unsafe_allow_html=True)
    if e2e_status:
        st.markdown('<div class="ds-section">Notification automation</div>', unsafe_allow_html=True)
        e2e_cards = [
            (
                "Latest run",
                _friendly(str(latest_e2e.get("status") or "No run")),
                str(latest_e2e.get("run_type") or "automation"),
            ),
            (
                "Latest notice",
                str(latest_notification.get("title") or "None"),
                str(latest_notification.get("channel_hint") or "notification"),
            ),
            (
                "Missing outcomes",
                _format_number(len(list(e2e_status.get("missing_outcomes") or []))),
                "Reminder triggers when files are absent",
            ),
            (
                "Logs",
                _short_path(e2e_status.get("logs_path") or "logs"),
                "Automation log folder",
            ),
        ]
        st.markdown(_step_strip(e2e_cards), unsafe_allow_html=True)
        health = list(e2e_status.get("health") or [])
        if health:
            with st.expander("Automation health checklist", expanded=False):
                _table(health, ["check", "status", "detail"])
    if web_status:
        st.markdown('<div class="ds-section">Web Auto-Pilot</div>', unsafe_allow_html=True)
        counts = dict(web_status.get("counts") or {})
        telegram = dict(web_status.get("telegram_status") or {})
        candidate_count = latest_web.get(
            "candidate_count",
            counts.get("latest_candidate_count", 0),
        )
        web_cards = [
            (
                "Source status",
                _friendly(str(latest_web.get("status") or "No run")),
                f"{_format_number(candidate_count)} candidates",
            ),
            (
                "Confidence",
                f"{_format_number(latest_web.get('source_confidence'))}%",
                str(latest_web.get("stale_data_status") or "unknown"),
            ),
            (
                "Failures",
                _format_number(counts.get("source_failures", 0)),
                "Blocked or unavailable sources are logged",
            ),
            (
                "SEC / halts",
                (
                    f"{_format_number(counts.get('sec_risk_events', 0))} / "
                    f"{_format_number(counts.get('halt_events', 0))}"
                ),
                "Risk enrichment events",
            ),
            (
                "Telegram",
                _format_number(telegram.get("telegram_notifications", 0)),
                "Persisted Telegram notifications",
            ),
        ]
        st.markdown(_step_strip(web_cards), unsafe_allow_html=True)
        if latest_web.get("snapshot_path"):
            st.caption(f"Latest web snapshot: `{_short_path(latest_web.get('snapshot_path'))}`")
        source_operability = dict(web_status.get("source_operability") or {})
        browser = dict(web_status.get("browser_extractor") or {})
        enabled_candidates = list(source_operability.get("enabled_candidate_sources") or [])
        st.markdown(
            _callout(
                "Enabled candidate sources",
                ", ".join(enabled_candidates) if enabled_candidates else "None enabled",
            ),
            unsafe_allow_html=True,
        )
        if source_operability.get("only_universe_or_enrichment_enabled"):
            st.warning("Only universe/enrichment sources are enabled. Add a candidate source.")
        if latest_web.get("status") == "no_data":
            attempts = list(latest_web.get("attempts") or [])
            reason = "; ".join(
                str(item.get("failure_reason") or item.get("reason") or item.get("status"))
                for item in attempts[:3]
            )
            st.info(f"Latest no-data reason: {reason or 'No usable rows found.'}")
        st.caption(
            "Browser extractor: "
            + (
                "available"
                if browser.get("available")
                else str(browser.get("install_hint") or "not installed")
            )
        )
        preview_ranked = list(state.get("ranked") or [])
        preview_avoid = list(state.get("avoid") or [])
        if preview_ranked:
            st.markdown('<div class="ds-section">Telegram preview</div>', unsafe_allow_html=True)
            st.code(
                format_morning_watchlist(
                    ranked=preview_ranked,
                    avoid=preview_avoid,
                    source_summary=latest_web,
                ),
                language="text",
            )
        warnings = list((web_status.get("ai_data_warnings") or [])[:3])
        if warnings:
            st.warning(
                "AI/data warnings: "
                + "; ".join(str(item.get("warning") or item) for item in warnings)
            )
        with st.expander("Web source details", expanded=False):
            _table(
                list(latest_web.get("attempts") or [])[:8],
                [
                    "source",
                    "source_type",
                    "status",
                    "rows_extracted",
                    "rows_normalized",
                    "failure_reason",
                ],
            )
            _table(
                list(web_status.get("source_health") or [])[:8],
                ["checked_at", "source", "status", "detail"],
            )
            _table(
                list(web_status.get("fetch_results") or [])[:8],
                ["source", "status", "row_count", "failure_reason", "artifact_path"],
            )
    if latest_run:
        warnings = normalization.get("warnings") or []
        if warnings:
            st.warning("Data warnings: " + "; ".join(str(item) for item in warnings[:5]))
        summary_path = Path(str(latest_run.get("out_dir", ""))) / "run_summary.json"
        st.caption(f"Latest run summary: `{_short_path(summary_path)}`")
    ranked = list(state.get("ranked") or [])
    avoid = list(state.get("avoid") or [])
    if ranked:
        st.markdown('<div class="ds-section">Current picks</div>', unsafe_allow_html=True)
        _table(
            ranked[:3],
                [
                    "rank",
                    "ticker",
                    "total_score",
                    "expected_return_bucket",
                    "confidence_bucket",
                    "setup_grade",
                    "premarket_price",
                    "breakout_trigger",
                    "invalidation_level",
                    "first_target",
                    "source_confidence",
                    "model_version",
                    "coverage_warning",
                ],
            )
    if avoid:
        with st.expander("Avoid list", expanded=False):
            _table(
                avoid[:6],
                [
                    "rank",
                    "ticker",
                    "score",
                    "gap_pct",
                    "dollar_volume",
                    "risk_flags",
                    "avoid_reasons",
                ],
            )
    if report:
        report_cards = [
            ("Top 1", _signed_pct(report.get("top_1_close_return_pct")), "Close basket"),
            ("Top 3", _signed_pct(report.get("top_3_close_return_pct")), "Equal-weight"),
            ("Top 5", _signed_pct(report.get("top_5_close_return_pct")), "Equal-weight"),
            ("Hit rate", f"{_format_number(report.get('hit_rate_close_pct'))}%", "Manual closes"),
        ]
        st.markdown(_step_strip(report_cards), unsafe_allow_html=True)
    if outcomes:
        with st.expander("Uploaded manual outcomes", expanded=False):
            _table(outcomes[:12], MANUAL_OUTCOME_COLUMNS)
    if manual_audit:
        with st.expander("Manual audit status", expanded=False):
            _table(manual_audit[:12], SHADOW_AUDIT_COLUMNS)
    if uploads:
        with st.expander("Manual snapshot uploads", expanded=False):
            _table(
                uploads[:8],
                [
                    "created_at",
                    "row_count",
                    "data_source_kind",
                    "shadow_mode",
                    "avg_data_quality_score",
                    "missing_enrichment_count",
                    "output_path",
                ],
            )
    if automation_runs:
        with st.expander("Automation run history", expanded=False):
            _table(
                automation_runs[:8],
                [
                    "started_at",
                    "status",
                    "input_path",
                    "normalized_path",
                    "scan_run_id",
                    "out_dir",
                ],
            )
    if e2e_runs:
        with st.expander("Notification automation history", expanded=False):
            _table(
                e2e_runs[:8],
                [
                    "run_type",
                    "status",
                    "started_at",
                    "completed_at",
                    "out_dir",
                ],
            )
    notifications = list(e2e_status.get("notifications") or state.get("recent_notifications") or [])
    if notifications:
        with st.expander("Latest notifications", expanded=False):
            _table(
                notifications[:8],
                [
                    "sent_at",
                    "channel",
                    "ticker",
                    "title",
                    "channel_hint",
                ],
            )


def _dashboard_hero(
    row: dict[str, Any],
    estimate: dict[str, Any] | None,
    monitor: dict[str, Any] | None,
) -> str:
    ticker = str(row.get("ticker", "n/a"))
    decision, tone = _decision_label(row, monitor)
    expected = _signed_pct(estimate.get("expected_return_pct")) if estimate else "Run backtest"
    confidence = f"{_format_number(estimate.get('confidence_pct'))}%" if estimate else "n/a"
    current = _format_price(monitor.get("current_price")) if monitor else _format_price(
        row.get("premarket_price")
    )
    monitor_text = _monitor_sentence(monitor) if monitor else (
        "No five-minute check is loaded yet. Press Check Now after running the scan."
    )
    levels = [
        ("Current", current),
        ("Broker watch", _format_price(row.get("breakout_trigger"))),
        ("Exit line", _format_price(row.get("invalidation_level"))),
        ("First target", _format_price(row.get("first_target"))),
    ]
    level_html = []
    for label, value in levels:
        level_html.append(
            '<div class="ds-simple-level">'
            f'<div class="ds-simple-level-label">{_html(label)}</div>'
            f'<div class="ds-simple-level-value">{_html(value)}</div>'
            "</div>"
        )
    panels = [
        (
            "Expected return",
            expected,
            f"{confidence} confidence from the current paper-test sample.",
        ),
        (
            "5-minute check",
            _friendly(str(monitor.get("status", "No check"))) if monitor else "No check",
            monitor_text,
        ),
        (
            "Exit rule",
            _format_price(row.get("invalidation_level")),
            "If the monitor marks invalidated or price loses this line, the setup failed.",
        ),
    ]
    panel_html = []
    for title, value, copy in panels:
        panel_html.append(
            '<div class="ds-dashboard-panel">'
            f'<div class="ds-panel-title">{_html(title)}</div>'
            f'<div class="ds-panel-main">{_html(value)}</div>'
            f'<div class="ds-panel-copy">{_html(copy)}</div>'
            "</div>"
        )
    return (
        '<div class="ds-dashboard-hero">'
        '<div class="ds-dashboard-top">'
        "<div>"
        '<div class="ds-dashboard-kicker">Today\'s simple read</div>'
        f'<div class="ds-dashboard-title">{_html(ticker)}</div>'
        "</div>"
        f'<div class="ds-decision-pill ds-decision--{tone}">{_html(decision)}</div>'
        "</div>"
        f'<div class="ds-dashboard-read">{_html(_dashboard_sentence(row, estimate, monitor))}</div>'
        f'<div class="ds-simple-levels">{"".join(level_html)}</div>'
        f'<div class="ds-dashboard-grid">{"".join(panel_html)}</div>'
        "</div>"
    )


def _dashboard_sentence(
    row: dict[str, Any],
    estimate: dict[str, Any] | None,
    monitor: dict[str, Any] | None,
) -> str:
    ticker = str(row.get("ticker", "This setup"))
    expected = _signed_pct(estimate.get("expected_return_pct")) if estimate else "not tested yet"
    confidence = f"{_format_number(estimate.get('confidence_pct'))}%" if estimate else "n/a"
    decision, _tone = _decision_label(row, monitor)
    return (
        f"{ticker} is the lead pick. Watch for strength through "
        f"{_format_price(row.get('breakout_trigger'))}. The exit line is "
        f"{_format_price(row.get('invalidation_level'))}; first target is "
        f"{_format_price(row.get('first_target'))}. Expected paper return is {expected} "
        f"with {confidence} confidence. Current decision: {decision}."
    )


def _broker_flow() -> str:
    steps = [
        ("1", "Run", "Run Full Test before the session or when new data lands."),
        ("2", "Review", "Use the pick list and copy the watch, exit, and target levels."),
        ("3", "Trade", "Place any order yourself in the broker. Dawnstrike does not trade."),
        ("4", "Monitor", "Use Check Now or the 5-minute monitor for exit/invalidated reads."),
    ]
    html = []
    for num, title, copy in steps:
        html.append(
            '<div class="ds-flow-step">'
            f'<div class="ds-flow-num">Step {_html(num)}</div>'
            f'<div class="ds-flow-title">{_html(title)}</div>'
            f'<div class="ds-flow-copy">{_html(copy)}</div>'
            "</div>"
        )
    return f'<div class="ds-broker-flow"><div class="ds-flow-row">{"".join(html)}</div></div>'


def _dashboard_rows(rows: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        ticker = str(row.get("ticker", ""))
        estimate = _expectancy_for(state, ticker)
        monitor = _row_by_ticker(state["monitor_rows"], ticker)
        decision, _tone = _decision_label(row, monitor)
        output.append(
            {
                **row,
                "decision": decision,
                "expected_return_pct": (
                    estimate.get("expected_return_pct") if estimate else None
                ),
                "confidence_pct": estimate.get("confidence_pct") if estimate else None,
            }
        )
    return output


def _decision_label(
    row: dict[str, Any],
    monitor: dict[str, Any] | None,
) -> tuple[str, str]:
    if not monitor:
        return ("Run 5-minute check", "neutral")
    status = str(monitor.get("status", "")).lower()
    if status == "confirming":
        return ("Active: monitor target and exit line", "go")
    if status == "watching":
        return ("Wait: not through watch price", "wait")
    if status == "extended":
        return ("Caution: already stretched", "caution")
    if status == "fading":
        return ("Caution: weakening", "caution")
    if status == "invalidated":
        return ("Exit signal: setup failed", "exit")
    if status == "missing":
        return ("No current price read", "neutral")
    if _number(row.get("score")) <= 0:
        return ("Skip", "exit")
    return (_friendly(status), "neutral")


def _decision_brief(
    row: dict[str, Any],
    estimate: dict[str, Any] | None,
    monitor: dict[str, Any] | None,
) -> None:
    ticker = str(row.get("ticker", "n/a"))
    expected = _signed_pct(estimate.get("expected_return_pct")) if estimate else "not tested"
    confidence = f"{_format_number(estimate.get('confidence_pct'))}%" if estimate else "n/a"
    status = _friendly(str(monitor.get("status", "No monitor"))) if monitor else "Not checked"
    status_note = (
        str(monitor.get("reason", "Run Monitor Now to confirm the setup.")) if monitor else ""
    )
    st.markdown(
        f"""
        <div class="ds-brief">
            <div class="ds-brief-kicker">Current read</div>
            <div class="ds-brief-title">{_html(ticker)}</div>
            <div class="ds-brief-copy">
                Watch {_html(_format_price(row.get('breakout_trigger')))}.
                Risk line {_html(_format_price(row.get('invalidation_level')))}.
                First paper target {_html(_format_price(row.get('first_target')))}.
            </div>
            <div class="ds-brief-grid">
                <div>
                    <div class="ds-brief-label">Score</div>
                    <div class="ds-brief-value">{_html(_format_score(row.get('score')))}</div>
                </div>
                <div>
                    <div class="ds-brief-label">Backtest estimate</div>
                    <div class="ds-brief-value">{_html(expected)}</div>
                </div>
                <div>
                    <div class="ds-brief-label">Confidence</div>
                    <div class="ds-brief-value">{_html(confidence)}</div>
                </div>
                <div>
                    <div class="ds-brief-label">Monitor</div>
                    <div class="ds-brief-value">{_html(status)}</div>
                </div>
            </div>
            <div class="ds-brief-note">{_html(status_note)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _level_summary(row: dict[str, Any], monitor: dict[str, Any] | None) -> None:
    current = monitor.get("current_price") if monitor else row.get("premarket_price")
    levels = [
        ("Current", _format_price(current), "Where it is now"),
        ("Watch", _format_price(row.get("breakout_trigger")), "Needs strength above this"),
        ("Risk line", _format_price(row.get("invalidation_level")), "Setup fails below this"),
        ("Target", _format_price(row.get("first_target")), "First paper objective"),
    ]
    st.markdown(_level_cards(levels), unsafe_allow_html=True)


def _plain_read(state: dict[str, Any]) -> None:
    ranked = state["ranked"]
    monitor_rows = state["monitor_rows"]
    if not ranked:
        body = "No setup is loaded yet. Open Run and press Full Test."
    else:
        top = ranked[0]
        ticker = str(top.get("ticker", "Top setup"))
        score = _format_score(top.get("score"))
        breakout = _format_price(top.get("breakout_trigger"))
        invalid = _format_price(top.get("invalidation_level"))
        target = _format_price(top.get("first_target"))
        monitor = _row_by_ticker(monitor_rows, ticker)
        monitor_text = (
            f" Latest monitor status: {_friendly(str(monitor.get('status', ''))).lower()}."
            if monitor
            else " Run Monitor to see whether it still matches the plan."
        )
        body = (
            f"{ticker} is first on the list. Price to watch: {breakout}. Risk line: "
            f"{invalid}. First paper target: {target}. Score: {score}.{monitor_text}"
        )
    st.markdown(
        f"""
        <div class="ds-read">
            <div class="ds-read-title">Next Step</div>
            <div class="ds-read-body">{_html(body)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _run_flow(state: dict[str, Any], config: Any) -> None:
    settings = state["settings"]
    st.markdown('<div class="ds-section">Start Here</div>', unsafe_allow_html=True)
    st.markdown(
        """
        <div class="ds-path">
            <div class="ds-path-card">
                <div class="ds-path-num">1</div>
                <div class="ds-path-title">Find names</div>
                <div class="ds-path-note">Build today's review list from the snapshot.</div>
            </div>
            <div class="ds-path-card">
                <div class="ds-path-num">2</div>
                <div class="ds-path-title">Test them</div>
                <div class="ds-path-note">Replay historical minute bars as a paper test.</div>
            </div>
            <div class="ds-path-card">
                <div class="ds-path-num">3</div>
                <div class="ds-path-title">Check status</div>
                <div class="ds-path-note">See whether the names still match the original plan.</div>
            </div>
            <div class="ds-path-card">
                <div class="ds-path-num">4</div>
                <div class="ds-path-title">Repeat</div>
                <div class="ds-path-note">Let Windows rerun the monitor every five minutes.</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="ds-next">
            <strong>Best first click:</strong> Full Test runs the scan, paper test,
            and monitor check in one pass.
        </div>
        """,
        unsafe_allow_html=True,
    )

    primary_left, primary_right = st.columns([1, 1])
    _action_button(
        primary_left,
        "Full Test",
        lambda: _run_full_web_backtest(
            settings["snapshot_path"],
            settings["scan_output_dir"],
            settings["db_path"],
            settings["minute_bars_path"],
            settings["audit_output_dir"],
            settings["monitor_output_dir"],
            int(settings["rows_to_show"]),
            3,
            float(config.slippage_bps),
        ),
        primary=True,
        key="run_full_test",
    )
    _action_button(
        primary_right,
        "Monitor Now",
        lambda: _run_web_monitor(
            settings["snapshot_path"],
            settings["monitor_output_dir"],
            settings["db_path"],
            int(settings["rows_to_show"]),
        ),
        key="run_monitor_now",
    )

    with st.expander("Manual steps and automation", expanded=False):
        cols = st.columns(4)
        _action_button(
            cols[0],
            "Initialize DB",
            lambda: _initialize_web_db(settings["db_path"]),
            key="run_initialize_db",
        )
        _action_button(
            cols[1],
            "Run Scan",
            lambda: _run_web_scan(
                settings["snapshot_path"],
                settings["scan_output_dir"],
                settings["db_path"],
                int(settings["rows_to_show"]),
            ),
            key="run_scan",
        )
        _action_button(
            cols[2],
            "Paper Test",
            lambda: _run_web_audit_latest(
                settings["db_path"],
                settings["minute_bars_path"],
                settings["audit_output_dir"],
                3,
                float(config.slippage_bps),
            ),
            key="run_paper_test",
        )
        _action_button(
            cols[3],
            "Preview Alerts",
            lambda: _preview_web_alerts(settings["db_path"], config),
            key="run_preview_alerts",
        )

        auto_left, auto_right = st.columns(2)
        _action_button(
            auto_left,
            "Register 5m Monitor",
            lambda: _register_web_tasks(
                settings["snapshot_path"],
                settings["db_path"],
                settings["scan_output_dir"],
                settings["monitor_output_dir"],
            ),
            key="run_register_monitor",
        )
        _action_button(auto_right, "Task Status", _scheduled_task_status, key="run_task_status")
    _action_result()


def _action_button(
    column: Any,
    label: str,
    callback: Any,
    *,
    key: str,
    primary: bool = False,
) -> None:
    if column.button(
        label,
        key=key,
        type="primary" if primary else "secondary",
        width="stretch",
    ):
        try:
            with st.spinner(f"{label}..."):
                st.session_state["action_result"] = callback()
            st.rerun()
        except (
            IntradayScannerError,
            OSError,
            ValueError,
            json.JSONDecodeError,
            subprocess.TimeoutExpired,
        ) as exc:
            st.session_state["action_result"] = {
                "status": "error",
                "message": str(exc),
            }
            st.rerun()


def _action_result() -> None:
    result = st.session_state.get("action_result")
    if not isinstance(result, dict):
        return
    status = str(result.get("status", ""))
    message = str(result.get("message", "Action complete."))
    if status == "error":
        st.error(message)
        return
    st.success(message)
    summary = result.get("summary")
    if isinstance(summary, dict) and summary:
        _summary_cards(summary)
    events = result.get("events")
    if isinstance(events, list) and events:
        _table(events, ["title", "ticker", "body"])
    paths = result.get("paths")
    if isinstance(paths, dict) and paths:
        st.caption(" | ".join(f"{key}: {value}" for key, value in paths.items()))


def _summary_cards(summary: dict[str, Any]) -> None:
    if "setup_count" in summary:
        cards = [
            ("Checked", str(summary.get("setup_count", 0)), "Monitor rows"),
            ("Confirming", str(summary.get("confirming_count", 0)), "Above trigger"),
            ("Watching", str(summary.get("watching_count", 0)), "Still intact"),
            ("Warnings", str(summary.get("warning_count", 0)), "Needs attention"),
        ]
    elif "avg_close_return_pct" in summary:
        cards = [
            ("Trades", str(summary.get("trade_count", 0)), "Paper rows"),
            ("Lunch avg", _signed_pct(summary.get("avg_lunch_return_pct")), "Midday exit"),
            ("Close avg", _signed_pct(summary.get("avg_close_return_pct")), "Close exit"),
            ("Drawdown", _signed_pct(summary.get("max_drawdown_pct")), "Worst low"),
        ]
    else:
        cards = [
            ("Top", str(summary.get("top_ticker", "n/a")), "Lead setup"),
            ("Watchlist", str(summary.get("ranked_count", 0)), "Clean names"),
            ("Top picks", str(summary.get("top_explosive_count", 0)), "Strongest"),
            ("Blocked", str(summary.get("avoid_count", 0)), "Risk list"),
        ]
    st.markdown(_step_strip(cards), unsafe_allow_html=True)


def _watchlist(state: dict[str, Any]) -> None:
    ranked = state["ranked"]
    if not ranked:
        st.info("Run Scan to create a watchlist.")
        return
    st.markdown('<div class="ds-section">Top Candidates</div>', unsafe_allow_html=True)
    st.markdown(_setup_card_grid(ranked[:4], state), unsafe_allow_html=True)
    st.markdown('<div class="ds-section">Review List</div>', unsafe_allow_html=True)
    _table(ranked, WATCHLIST_COLUMNS)
    with st.expander("Risk and exit notes", expanded=False):
        _table(ranked, ["ticker", "best_exit_bias", "risk_flags"])


def _setup_card(row: dict[str, Any], estimate: dict[str, Any] | None) -> None:
    ticker = str(row.get("ticker", "n/a"))
    expected = _signed_pct(estimate.get("expected_return_pct")) if estimate else "No estimate"
    confidence = (
        f"{_format_number(estimate.get('confidence_pct'))}% confidence"
        if estimate
        else "Run audit for confidence"
    )
    body = (
        f"{ticker} is ranked highest by the current scan. Price to watch: "
        f"{_format_price(row.get('breakout_trigger'))}. Risk line: "
        f"{_format_price(row.get('invalidation_level'))}. First paper target: "
        f"{_format_price(row.get('first_target'))}. Backtest estimate: {expected} "
        f"with {confidence}."
    )
    st.markdown(
        f"""
        <div class="ds-read">
            <div class="ds-read-title">Plain Read</div>
            <div class="ds-read-body">{_html(body)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _monitor(state: dict[str, Any]) -> None:
    rows = _sorted_monitor_rows(state["monitor_rows"])
    if not rows:
        st.info("Run Monitor in Run Flow to check the saved watchlist.")
        return
    top = rows[0]
    st.markdown(_monitor_brief(top, rows), unsafe_allow_html=True)
    st.markdown('<div class="ds-section">Price Plan</div>', unsafe_allow_html=True)
    st.markdown(_monitor_price_lane(top), unsafe_allow_html=True)
    st.markdown('<div class="ds-section">Setup Checks</div>', unsafe_allow_html=True)
    st.markdown(_monitor_card_grid(rows), unsafe_allow_html=True)
    st.markdown('<div class="ds-section">Monitor Board</div>', unsafe_allow_html=True)
    _table(rows, MONITOR_COLUMNS)
    with st.expander("Why each status was assigned", expanded=False):
        _table(rows, ["ticker", "reason", "expected_path", "risk_flags", "checked_at"])


def _sorted_monitor_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(row: dict[str, Any]) -> tuple[float, float, str]:
        rank = _number(row.get("rank"))
        confidence = _number(row.get("monitor_confidence_pct"))
        ticker = str(row.get("ticker", ""))
        return (rank if rank > 0 else 999, -confidence, ticker)

    return sorted(rows, key=sort_key)


def _monitor_brief(row: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    ticker = str(row.get("ticker", "Top setup"))
    status = str(row.get("status", "")).lower()
    checked = _short_timestamp(row.get("checked_at"))
    cards = [
        ("Current", _format_price(row.get("current_price")), "Last price checked"),
        ("Watch price", _format_price(row.get("breakout_trigger")), "Confirmation level"),
        (
            "Confidence",
            f"{_format_number(row.get('monitor_confidence_pct'))}%",
            "Monitor confidence",
        ),
        ("Checked", checked or "n/a", f"{len(rows)} saved names"),
    ]
    card_html = []
    for label, value, note in cards:
        card_html.append(
            '<div class="ds-monitor-stat">'
            f'<div class="ds-monitor-label">{_html(label)}</div>'
            f'<div class="ds-monitor-value">{_html(value)}</div>'
            f'<div class="ds-price-note">{_html(note)}</div>'
            "</div>"
        )
    return (
        '<div class="ds-monitor-brief">'
        '<div class="ds-monitor-top">'
        "<div>"
        '<div class="ds-monitor-kicker">Latest monitor read</div>'
        f'<div class="ds-monitor-title">{_html(ticker)}</div>'
        "</div>"
        f'<div class="ds-status-pill {_status_class(status)}">{_html(_friendly(status))}</div>'
        "</div>"
        f'<div class="ds-monitor-copy">{_html(_monitor_sentence(row))}</div>'
        f'<div class="ds-monitor-grid">{"".join(card_html)}</div>'
        "</div>"
    )


def _monitor_sentence(row: dict[str, Any]) -> str:
    ticker = str(row.get("ticker", "This setup"))
    status = str(row.get("status", "")).lower()
    current = _format_price(row.get("current_price"))
    trigger = _format_price(row.get("breakout_trigger"))
    risk = _format_price(row.get("invalidation_level"))
    target = _format_price(row.get("first_target"))
    if status == "confirming":
        return (
            f"{ticker} is confirming the watch price. Current price is {current}; "
            f"the first paper target is {target}. Keep the risk line at {risk}."
        )
    if status == "watching":
        return (
            f"{ticker} is still valid, but it has not reclaimed {trigger}. "
            f"Current price is {current}. The setup fails below {risk}; "
            f"first paper target is {target}."
        )
    if status == "extended":
        return (
            f"{ticker} has already stretched away from the watch price. Current price is "
            f"{current}; first paper target is {target}. Do not chase the move blindly."
        )
    if status == "fading":
        return (
            f"{ticker} is fading away from the expected path. Current price is {current}; "
            f"the risk line is {risk}."
        )
    if status == "invalidated":
        return (
            f"{ticker} broke the risk line. Current price is {current}; the original setup "
            f"is no longer intact."
        )
    reason = str(row.get("reason", "No status reason was saved."))
    return f"{ticker} is marked {_friendly(status).lower()}. {reason}"


def _monitor_price_lane(row: dict[str, Any]) -> str:
    levels = [
        ("Risk line", row.get("invalidation_level"), "risk", "Setup fails below this"),
        ("Current", row.get("current_price"), "current", "Latest monitor price"),
        ("Watch price", row.get("breakout_trigger"), "trigger", "Needs strength above this"),
        ("Target", row.get("first_target"), "target", "First paper objective"),
    ]
    cards = []
    for label, value, kind, note in levels:
        cards.append(
            f'<div class="ds-price-point ds-price-point--{kind}">'
            f'<div class="ds-price-label">{_html(label)}</div>'
            f'<div class="ds-price-value">{_html(_format_price(value))}</div>'
            f'<div class="ds-price-note">{_html(note)}</div>'
            "</div>"
        )
    return (
        '<div class="ds-price-lane">'
        f'<div class="ds-price-lane-row">{"".join(cards)}</div>'
        "</div>"
    )


def _monitor_card_grid(rows: list[dict[str, Any]]) -> str:
    cards = []
    for row in rows:
        status = str(row.get("status", "")).lower()
        progress = _clamp(_number(row.get("path_progress_pct")), 0, 100)
        progress_label = f"{_format_number(row.get('path_progress_pct'))}%"
        flags = _friendly_list(row.get("risk_flags"))
        reason = str(row.get("reason", "No reason saved."))
        cards.append(
            '<div class="ds-monitor-card">'
            '<div class="ds-monitor-card-head">'
            "<div>"
            f'<div class="ds-monitor-name">{_html(row.get("ticker", "n/a"))}</div>'
            f'<div class="ds-monitor-meta">Rank #{_html(_format_number(row.get("rank")))}'
            f' | confidence {_html(_format_number(row.get("monitor_confidence_pct")))}%</div>'
            "</div>"
            f'<div class="ds-status-pill {_status_class(status)}">{_html(_friendly(status))}</div>'
            "</div>"
            '<div class="ds-progress">'
            '<div class="ds-progress-track">'
            f'<div class="ds-progress-fill {_progress_class(status)}" style="width: {progress}%">'
            "</div>"
            "</div>"
            '<div class="ds-progress-caption">'
            f"<span>{_html(progress_label)} to target</span>"
            f"<span>{_html(_format_price(row.get('current_price')))}</span>"
            "</div>"
            "</div>"
            f'<div class="ds-monitor-reason">{_html(reason)}</div>'
            f'<div class="ds-monitor-meta">Risk flags: {_html(flags)}</div>'
            "</div>"
        )
    return f'<div class="ds-monitor-cards">{"".join(cards)}</div>'


def _status_class(status: str) -> str:
    normalized = status.lower()
    if normalized in {"confirming", "watching", "extended", "fading", "invalidated", "missing"}:
        return f"ds-status--{normalized}"
    return "ds-status--default"


def _progress_class(status: str) -> str:
    normalized = status.lower()
    if normalized in {"confirming", "extended", "fading", "invalidated", "missing"}:
        return f"ds-progress-fill--{normalized}"
    return ""


def _audit(state: dict[str, Any]) -> None:
    audit_rows = state["manual_audit_trades"] or state["audit_rows"]
    expectancy = state["expectancy"]
    summary = _audit_summary(state)
    cards = [
        ("Tests", str(len(audit_rows)), "Paper-trade rows"),
        ("Average", _signed_pct(summary.get("avg_close_return_pct")), "Close-exit return"),
        ("Win rate", f"{_format_number(summary.get('win_rate_close_pct'))}%", "Closed positive"),
        ("Worst dip", _signed_pct(summary.get("max_drawdown_pct")), "Lowest drawdown"),
    ]
    st.markdown(_step_strip(cards), unsafe_allow_html=True)
    left, right = st.columns([1, 1])
    with left:
        st.markdown('<div class="ds-section">Expected Paper Return</div>', unsafe_allow_html=True)
        exp_frame = _expectancy_frame(expectancy)
        _render_chart(
            _expectancy_chart(exp_frame) if not exp_frame.empty else _empty_chart("No estimates"),
        )
    with right:
        st.markdown('<div class="ds-section">Backtest Results</div>', unsafe_allow_html=True)
        audit_frame = _audit_frame(audit_rows)
        _render_chart(
            _audit_chart(audit_frame) if not audit_frame.empty else _empty_chart("No audit rows"),
        )
    _table(expectancy, EXPECTANCY_COLUMNS)
    _table(audit_rows, SHADOW_AUDIT_COLUMNS if state["manual_audit_trades"] else AUDIT_COLUMNS)


def _history(state: dict[str, Any]) -> None:
    history = state["history"]
    shadow_report = dict(state.get("shadow_report") or {})
    if shadow_report:
        st.markdown('<div class="ds-section">Free Shadow Report</div>', unsafe_allow_html=True)
        cards = [
            ("Shadow days", _format_number(shadow_report.get("scan_day_count")), "Saved scans"),
            (
                "Recommendations",
                _format_number(shadow_report.get("recommendation_count")),
                "Saved calls",
            ),
            ("Top 3", _signed_pct(shadow_report.get("top_3_close_return_pct")), "Close basket"),
            ("Drawdown", _signed_pct(shadow_report.get("max_drawdown_pct")), "Worst low"),
        ]
        st.markdown(_step_strip(cards), unsafe_allow_html=True)
    frame = _history_frame(history)
    if frame.empty:
        st.info("No saved run history is available yet.")
        return
    _render_chart(_history_chart(frame))
    _table(
        history,
        [
            "created_at",
            "top_ticker",
            "ranked_count",
            "top_explosive_count",
            "avoid_count",
            "run_id",
        ],
    )


def _settings(state: dict[str, Any], config: Any) -> None:
    settings = state["settings"]
    st.markdown('<div class="ds-section">Display</div>', unsafe_allow_html=True)
    control_left, control_mid, control_right = st.columns(3)
    with control_left:
        st.selectbox(
            "Data source",
            ["SQLite", "latest output", "sample CSV"],
            key="data_source",
        )
    with control_mid:
        st.number_input(
            "Rows to show",
            min_value=1,
            max_value=50,
            key="rows_to_show",
        )
    with control_right:
        st.slider(
            "Score floor",
            min_value=0,
            max_value=100,
            key="minimum_score",
        )
    if st.button("Apply settings", width="stretch"):
        st.rerun()

    st.markdown('<div class="ds-section">Files</div>', unsafe_allow_html=True)
    file_left, file_right = st.columns(2)
    with file_left:
        st.text_input("Snapshot", key="snapshot_path")
        st.text_input("Minute bars", key="minute_bars_path")
        st.text_input("SQLite", key="db_path")
    with file_right:
        st.text_input("Scan output", key="scan_output_dir")
        st.text_input("Audit output", key="audit_output_dir")
        st.text_input("Monitor output", key="monitor_output_dir")

    cards = [
        ("Database", settings["db_path"], "SQLite store"),
        ("Snapshot", settings["snapshot_path"], "Current scan input"),
        ("Scan output", settings["scan_output_dir"], "Ranked CSV files"),
        ("Monitor output", settings["monitor_output_dir"], "Latest setup checks"),
    ]
    st.markdown(_step_strip(cards), unsafe_allow_html=True)
    st.markdown('<div class="ds-section">Current Parameters</div>', unsafe_allow_html=True)
    config_rows = [
        {"setting": "Top names", "value": config.top_n},
        {"setting": "Minimum gap", "value": f"{config.min_gap_pct}%"},
        {
            "setting": "Minimum dollar volume",
            "value": _format_money(config.min_premarket_dollar_volume),
        },
        {
            "setting": "Price range",
            "value": f"{_format_price(config.min_price)} to {_format_price(config.max_price)}",
        },
        {"setting": "Slippage", "value": f"{config.slippage_bps} bps"},
    ]
    _table(config_rows, ["setting", "value"])


def _initialize_web_db(db_path: str) -> dict[str, Any]:
    SQLiteScanStore(db_path).initialize()
    return {"status": "ok", "message": f"Initialized SQLite database at {db_path}."}


def _run_web_scan(
    snapshot_path: str,
    scan_output_dir: str,
    db_path: str,
    top_n: int,
) -> dict[str, Any]:
    scan_config = load_config(
        provider="csv",
        output_dir=Path(scan_output_dir),
        database_path=Path(db_path),
        top_n=top_n,
    )
    store = SQLiteScanStore(scan_config.database_path)
    result = ScanService(CSVProvider(snapshot_path), store=store).run(scan_config, persist=True)
    paths = write_scan_outputs(result, scan_config.output_dir)
    return {
        "status": "ok",
        "message": f"Scan complete. Top setup: {result.summary().get('top_ticker')}.",
        "summary": result.summary(),
        "paths": {key: str(value) for key, value in paths.items()},
    }


def _run_web_audit_latest(
    db_path: str,
    minute_bars_path: str,
    audit_output_dir: str,
    top_n: int,
    slippage_bps: float,
) -> dict[str, Any]:
    audit_config = load_config(database_path=Path(db_path), slippage_bps=slippage_bps)
    store = SQLiteScanStore(audit_config.database_path)
    latest = store.load_latest_scan()
    if latest is None:
        raise ValueError("No persisted scan exists yet. Run Scan first.")
    ranked_rows = cast(list[dict[str, Any]], latest.get("ranked_candidates") or [])
    minute_rows = read_csv_dicts(minute_bars_path)
    if not minute_rows:
        raise ValueError(f"No minute bars found at {minute_bars_path}.")
    paths = run_paper_audit_rows(
        ranked_rows,
        minute_rows,
        audit_output_dir,
        audit_config,
        top_n=top_n,
    )
    trades = _read_csv(paths["trades"])
    summary = _read_json(paths["summary"])
    store.persist_paper_audit(summary, trades)
    return {
        "status": "ok",
        "message": "Paper audit complete.",
        "summary": summary,
        "paths": {key: str(value) for key, value in paths.items()},
    }


def _run_web_monitor(
    snapshot_path: str,
    monitor_output_dir: str,
    db_path: str,
    top_n: int,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    latest = store.load_latest_scan()
    if latest is None:
        raise ValueError("No persisted scan exists yet. Run Scan first.")
    ranked_rows = cast(list[dict[str, Any]], latest.get("ranked_candidates") or [])
    if not ranked_rows:
        raise ValueError("Latest persisted scan has no ranked candidates.")
    summary = cast(dict[str, Any], latest.get("summary") or {})
    source_run_id = str(summary.get("run_id") or latest.get("run_id") or "")
    result = run_setup_monitor(
        candidates=ranked_rows,
        snapshots=read_snapshot_csv(snapshot_path),
        out_dir=monitor_output_dir,
        store=store,
        persist=True,
        source_run_id=source_run_id or None,
        top_n=top_n,
    )
    paths = dict(result.get("paths") or {})
    return {
        "status": "ok",
        "message": "Setup monitor complete.",
        "summary": result.get("summary", {}),
        "paths": {key: str(value) for key, value in paths.items()},
    }


def _run_full_web_backtest(
    snapshot_path: str,
    scan_output_dir: str,
    db_path: str,
    minute_bars_path: str,
    audit_output_dir: str,
    monitor_output_dir: str,
    scan_top_n: int,
    audit_top_n: int,
    slippage_bps: float,
) -> dict[str, Any]:
    _initialize_web_db(db_path)
    scan_result = _run_web_scan(snapshot_path, scan_output_dir, db_path, scan_top_n)
    audit_result = _run_web_audit_latest(
        db_path,
        minute_bars_path,
        audit_output_dir,
        audit_top_n,
        slippage_bps,
    )
    monitor_result = _run_web_monitor(
        snapshot_path,
        monitor_output_dir,
        db_path,
        scan_top_n,
    )
    return {
        "status": "ok",
        "message": "Full test complete.",
        "summary": monitor_result.get("summary", {}),
        "paths": {
            **dict(scan_result.get("paths") or {}),
            **dict(audit_result.get("paths") or {}),
            **dict(monitor_result.get("paths") or {}),
        },
    }


def _preview_web_alerts(db_path: str, config: Any) -> dict[str, Any]:
    latest = SQLiteScanStore(db_path).load_latest_scan()
    if latest is None:
        raise ValueError("No persisted scan exists yet. Run Scan first.")
    events = scan_events_from_payload(latest, config)
    return {
        "status": "ok",
        "message": f"Found {len(events)} alert preview(s).",
        "events": [
            {"title": event.title, "ticker": event.ticker or "", "body": event.body}
            for event in events
        ],
    }


def _register_web_tasks(
    snapshot_path: str,
    db_path: str,
    scan_output_dir: str,
    monitor_output_dir: str,
) -> dict[str, Any]:
    script = Path("scripts/register_dawnstrike_tasks.ps1").resolve()
    if not script.exists():
        raise ValueError(f"Task registration script is missing: {script}")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Repo",
        str(Path.cwd()),
        "-Snapshot",
        snapshot_path,
        "-DbPath",
        db_path,
        "-ScanOut",
        scan_output_dir,
        "-MonitorOut",
        monitor_output_dir,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if completed.returncode != 0:
        raise ValueError((completed.stderr or completed.stdout).strip())
    return {
        "status": "ok",
        "message": (completed.stdout or "Scheduled tasks registered.").strip(),
    }


def _scheduled_task_status() -> dict[str, Any]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        (
            "Get-ScheduledTask -TaskName 'Dawnstrike*' -ErrorAction SilentlyContinue | "
            "Select-Object TaskName,State | ConvertTo-Json -Compress"
        ),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode != 0:
        raise ValueError((completed.stderr or completed.stdout).strip())
    rows = _task_status_rows(completed.stdout)
    return {
        "status": "ok",
        "message": f"Found {len(rows)} Dawnstrike task(s).",
        "events": rows,
    }


def _task_status_rows(raw_json: str) -> list[dict[str, str]]:
    raw_json = raw_json.strip()
    if not raw_json:
        return []
    payload = json.loads(raw_json)
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    return [
        {
            "title": str(row.get("TaskName", "")),
            "ticker": "",
            "body": str(row.get("State", "")),
        }
        for row in payload
        if isinstance(row, dict)
    ]


def _audit_summary(state: dict[str, Any]) -> dict[str, Any]:
    rows = state["manual_audit_trades"] or state["audit_rows"]
    summary = dict(state["manual_audit_summary"] or state["audit_summary"] or {})
    if not rows:
        return summary
    fallback = {
        "trade_count": len(rows),
        "avg_lunch_return_pct": _average(rows, "lunch_return_pct"),
        "avg_close_return_pct": _average(rows, "close_return_pct"),
        "max_drawdown_pct": _minimum(rows, "low_drawdown_pct"),
        "win_rate_close_pct": _win_rate(rows, "close_return_pct"),
    }
    return {**fallback, **summary}


def _scan_frame(ranked: list[dict[str, Any]], avoid: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for label, source_rows in [("Watchlist", ranked), ("Risk list", avoid)]:
        for row in source_rows:
            rows.append(
                {
                    "ticker": str(row.get("ticker", "")),
                    "status": label,
                    "score": _number(row.get("score")),
                    "gap_pct": _number(row.get("gap_pct")),
                    "dollar_volume": max(_number(row.get("dollar_volume")), 1.0),
                    "range_position_pct": _number(row.get("range_position_pct")),
                    "float_rotation_pct": _number(row.get("float_rotation_pct")),
                }
            )
    return pd.DataFrame(rows)


def _monitor_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    order = {
        "confirming": 0,
        "watching": 1,
        "extended": 2,
        "fading": 3,
        "invalidated": 4,
        "missing": 5,
    }
    frame = pd.DataFrame(
        [
            {
                "ticker": str(row.get("ticker", "")),
                "status": str(row.get("status", "")),
                "order": order.get(str(row.get("status", "")), 99),
                "monitor_confidence_pct": _number(row.get("monitor_confidence_pct")),
                "current_price": _number(row.get("current_price")),
                "breakout_trigger": _number(row.get("breakout_trigger")),
                "first_target": _number(row.get("first_target")),
                "path_progress_pct": _number(row.get("path_progress_pct")),
                "distance_to_breakout_pct": _number(row.get("distance_to_breakout_pct")),
                "reason": str(row.get("reason", "")),
            }
            for row in rows
        ]
    )
    if frame.empty:
        return frame
    return frame.sort_values(["order", "monitor_confidence_pct"], ascending=[True, False])


def _expectancy_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": row.get("ticker"),
                "expected_return_pct": _number(row.get("expected_return_pct")),
                "confidence_pct": _number(row.get("confidence_pct")),
                "lower_return_pct": _number(row.get("lower_return_pct")),
                "upper_return_pct": _number(row.get("upper_return_pct")),
            }
            for row in rows
        ]
    )


def _audit_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    records = []
    for row in rows:
        ticker = str(row.get("ticker", ""))
        records.extend(
            [
                {
                    "ticker": ticker,
                    "exit": "Lunch",
                    "return_pct": _number(row.get("lunch_return_pct")),
                },
                {
                    "ticker": ticker,
                    "exit": "Close",
                    "return_pct": _number(row.get("close_return_pct")),
                },
                {
                    "ticker": ticker,
                    "exit": "High",
                    "return_pct": _number(row.get("high_return_pct")),
                },
            ]
        )
    return pd.DataFrame(records)


def _history_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "created_at": str(row.get("created_at", "")),
                "top_ticker": row.get("top_ticker") or "n/a",
                "ranked_count": _number(row.get("ranked_count")),
                "top_explosive_count": _number(row.get("top_explosive_count")),
                "avoid_count": _number(row.get("avoid_count")),
            }
            for row in rows
        ]
    )


def _render_chart(chart: alt.Chart) -> None:
    st.altair_chart(chart, width="stretch", theme=None)


def _opportunity_chart(frame: pd.DataFrame) -> alt.Chart:
    return (
        alt.Chart(frame)
        .mark_circle(opacity=0.86, stroke="#ffffff", strokeWidth=1)
        .encode(
            x=alt.X("dollar_volume:Q", scale=alt.Scale(type="log"), title="Premarket dollars"),
            y=alt.Y("score:Q", scale=alt.Scale(domain=[0, 100]), title="Score"),
            size=alt.Size(
                "float_rotation_pct:Q",
                title="Float rotation",
                scale=alt.Scale(range=[80, 780]),
            ),
            color=alt.Color(
                "status:N",
                scale=alt.Scale(domain=["Watchlist", "Risk list"], range=["#0f766e", "#b42318"]),
                legend=None,
            ),
            tooltip=[
                "ticker:N",
                "status:N",
                alt.Tooltip("score:Q", format=".2f"),
                alt.Tooltip("gap_pct:Q", title="Gap %", format=".2f"),
                alt.Tooltip("dollar_volume:Q", title="$ volume", format="$,.0f"),
            ],
        )
        .properties(height=320)
        .configure_axis(gridColor="#263246", labelColor="#94a3b8", titleColor="#cbd5e1")
        .configure_view(stroke=None, fill="#0f1724")
        .configure(background="#0f1724")
        .interactive()
    )


def _score_chart(frame: pd.DataFrame) -> alt.Chart:
    if frame.empty:
        return _empty_chart("No scores")
    ordered = frame.sort_values("score", ascending=True)
    return (
        alt.Chart(ordered)
        .mark_bar(cornerRadiusEnd=3, color="#1d4ed8")
        .encode(
            x=alt.X("score:Q", scale=alt.Scale(domain=[0, 100]), title="Score"),
            y=alt.Y("ticker:N", sort=None, title=None),
            tooltip=["ticker:N", alt.Tooltip("score:Q", format=".2f")],
        )
        .properties(height=280)
        .configure_axis(gridColor="#263246", labelColor="#94a3b8", titleColor="#cbd5e1")
        .configure_view(stroke=None, fill="#0f1724")
        .configure(background="#0f1724")
    )


def _level_chart(row: dict[str, Any]) -> alt.Chart:
    levels = [
        ("Fail line", "invalidation_level", "risk"),
        ("Watched", "premarket_price", "context"),
        ("Breakout", "breakout_trigger", "trigger"),
        ("First target", "first_target", "target"),
        ("Stretch", "stretch_target", "target"),
    ]
    frame = pd.DataFrame(
        [
            {"level": label, "price": _number(row.get(key)), "kind": kind}
            for label, key, kind in levels
            if _number(row.get(key)) > 0
        ]
    )
    if frame.empty:
        return _empty_chart("No levels")
    level_order = [label for label, _, _ in levels]
    base = alt.Chart(frame).encode(
        x=alt.X("price:Q", title="Price"),
        y=alt.Y("level:N", sort=level_order, title=None),
        tooltip=["level:N", alt.Tooltip("price:Q", format="$,.4f")],
    )
    ticks = base.mark_tick(thickness=4, size=32).encode(
        color=alt.Color(
            "kind:N",
            scale=alt.Scale(
                domain=["risk", "context", "trigger", "target"],
                range=["#b42318", "#98a2b3", "#1d4ed8", "#0f766e"],
            ),
            legend=None,
        )
    )
    text = base.mark_text(align="left", dx=10, color="#eef4ff").encode(
        text=alt.Text("price:Q", format="$,.4f")
    )
    return (
        (ticks + text)
        .properties(height=250)
        .configure_axis(gridColor="#263246", labelColor="#94a3b8", titleColor="#cbd5e1")
        .configure_view(stroke=None, fill="#0f1724")
        .configure(background="#0f1724")
    )


def _monitor_status_chart(frame: pd.DataFrame) -> alt.Chart:
    counts = frame.groupby("status", as_index=False).size().rename(columns={"size": "count"})
    return (
        alt.Chart(counts)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            x=alt.X("count:Q", title="Names"),
            y=alt.Y("status:N", sort=None, title=None),
            color=alt.Color(
                "status:N",
                scale=alt.Scale(
                    domain=["confirming", "watching", "extended", "fading", "invalidated"],
                    range=["#0f766e", "#1d4ed8", "#b45309", "#c2410c", "#b42318"],
                ),
                legend=None,
            ),
            tooltip=["status:N", "count:Q"],
        )
        .properties(height=270)
        .configure_axis(gridColor="#263246", labelColor="#94a3b8", titleColor="#cbd5e1")
        .configure_view(stroke=None, fill="#0f1724")
        .configure(background="#0f1724")
    )


def _monitor_progress_chart(frame: pd.DataFrame) -> alt.Chart:
    ordered = frame.sort_values("path_progress_pct", ascending=True)
    bars = (
        alt.Chart(ordered)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            x=alt.X("path_progress_pct:Q", title="Progress to first target %"),
            y=alt.Y("ticker:N", sort=None, title=None),
            color=alt.Color(
                "status:N",
                scale=alt.Scale(
                    domain=["confirming", "watching", "extended", "fading", "invalidated"],
                    range=["#0f766e", "#1d4ed8", "#b45309", "#c2410c", "#b42318"],
                ),
                legend=None,
            ),
            tooltip=[
                "ticker:N",
                "status:N",
                alt.Tooltip("path_progress_pct:Q", title="Progress %", format=".2f"),
                alt.Tooltip("current_price:Q", title="Current", format="$,.4f"),
                alt.Tooltip("breakout_trigger:Q", title="Breakout", format="$,.4f"),
            ],
        )
    )
    target = alt.Chart(pd.DataFrame([{"x": 100}])).mark_rule(color="#0f766e").encode(x="x:Q")
    fail = alt.Chart(pd.DataFrame([{"x": 0}])).mark_rule(color="#b42318").encode(x="x:Q")
    return (
        (bars + target + fail)
        .properties(height=300)
        .configure_axis(gridColor="#263246", labelColor="#94a3b8", titleColor="#cbd5e1")
        .configure_view(stroke=None, fill="#0f1724")
        .configure(background="#0f1724")
    )


def _expectancy_chart(frame: pd.DataFrame) -> alt.Chart:
    base = alt.Chart(frame).encode(
        x=alt.X("ticker:N", sort="-y", title=None),
        tooltip=[
            "ticker:N",
            alt.Tooltip("expected_return_pct:Q", title="Expected %", format=".2f"),
            alt.Tooltip("confidence_pct:Q", title="Confidence %", format=".1f"),
            alt.Tooltip("lower_return_pct:Q", title="Low %", format=".2f"),
            alt.Tooltip("upper_return_pct:Q", title="High %", format=".2f"),
        ],
    )
    interval = base.mark_rule(color="#98a2b3", strokeWidth=2).encode(
        y=alt.Y("lower_return_pct:Q", title="Expected paper return %"),
        y2="upper_return_pct:Q",
    )
    bars = base.mark_bar(cornerRadiusEnd=3).encode(
        y=alt.Y("expected_return_pct:Q", title="Expected paper return %"),
        color=alt.Color("confidence_pct:Q", scale=alt.Scale(range=["#b45309", "#0f766e"])),
    )
    zero = alt.Chart(pd.DataFrame([{"y": 0}])).mark_rule(color="#b42318").encode(y="y:Q")
    return (
        (interval + bars + zero)
        .properties(height=310)
        .configure_axis(gridColor="#263246", labelColor="#94a3b8", titleColor="#cbd5e1")
        .configure_view(stroke=None, fill="#0f1724")
        .configure(background="#0f1724")
    )


def _audit_chart(frame: pd.DataFrame) -> alt.Chart:
    return (
        alt.Chart(frame.sort_values("return_pct", ascending=True))
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            x=alt.X("return_pct:Q", title="Paper return %"),
            y=alt.Y("ticker:N", sort=None, title=None),
            color=alt.Color(
                "exit:N",
                scale=alt.Scale(
                    domain=["Lunch", "Close", "High"],
                    range=["#b45309", "#1d4ed8", "#0f766e"],
                ),
            ),
            tooltip=["ticker:N", "exit:N", alt.Tooltip("return_pct:Q", format=".2f")],
        )
        .properties(height=310)
        .configure_axis(gridColor="#263246", labelColor="#94a3b8", titleColor="#cbd5e1")
        .configure_view(stroke=None, fill="#0f1724")
        .configure(background="#0f1724")
    )


def _history_chart(frame: pd.DataFrame) -> alt.Chart:
    melted = frame.melt(
        id_vars=["created_at", "top_ticker"],
        value_vars=["ranked_count", "top_explosive_count", "avoid_count"],
        var_name="metric",
        value_name="count",
    )
    return (
        alt.Chart(melted)
        .mark_line(point=True)
        .encode(
            x=alt.X("created_at:N", title="Run"),
            y=alt.Y("count:Q", title="Count"),
            color=alt.Color("metric:N", legend=None),
            tooltip=["created_at:N", "top_ticker:N", "metric:N", "count:Q"],
        )
        .properties(height=330)
        .configure_axis(gridColor="#263246", labelColor="#94a3b8", titleColor="#cbd5e1")
        .configure_view(stroke=None, fill="#0f1724")
        .configure(background="#0f1724")
    )


def _empty_chart(message: str) -> alt.Chart:
    return (
        alt.Chart(pd.DataFrame([{"message": message}]))
        .mark_text(color="#94a3b8", fontSize=14)
        .encode(text="message:N")
        .properties(height=240)
        .configure_view(stroke=None, fill="#0f1724")
        .configure(background="#0f1724")
    )


def _table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        st.markdown('<div class="ds-empty">No rows to show.</div>', unsafe_allow_html=True)
        return
    visible = [column for column in columns if column in rows[0]] or list(rows[0].keys())
    header = "".join(f"<th>{_html(_column_label(column))}</th>" for column in visible)
    body_rows = []
    for row in rows:
        cells = []
        for column in visible:
            value = _format_cell(column, row.get(column))
            css_class = "ds-num" if _is_numeric_column(column) else ""
            cells.append(f'<td class="{css_class}">{_html(value)}</td>')
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    st.markdown(
        f"""
        <div class="ds-table-wrap">
            <table class="ds-table">
                <thead><tr>{header}</tr></thead>
                <tbody>{''.join(body_rows)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _step_strip(cards: list[tuple[str, str, str]]) -> str:
    html = []
    for label, value, note in cards:
        html.append(
            '<div class="ds-step">'
            f'<div class="ds-step-label">{_html(label)}</div>'
            f'<div class="ds-step-value">{_html(value)}</div>'
            f'<div class="ds-step-note">{_html(note)}</div>'
            "</div>"
        )
    return f'<div class="ds-strip">{"".join(html)}</div>'


def _callout(title: str, body: str) -> str:
    return (
        '<div class="ds-read-card">'
        f'<div class="ds-read-label">{_html(title)}</div>'
        f'<div class="ds-read-body">{_html(body)}</div>'
        "</div>"
    )


def _level_cards(cards: list[tuple[str, str, str]]) -> str:
    html = []
    for label, value, note in cards:
        html.append(
            '<div class="ds-level-card">'
            f'<div class="ds-level-label">{_html(label)}</div>'
            f'<div class="ds-level-value">{_html(value)}</div>'
            f'<div class="ds-level-note">{_html(note)}</div>'
            "</div>"
        )
    return f'<div class="ds-level-grid">{"".join(html)}</div>'


def _setup_card_grid(rows: list[dict[str, Any]], state: dict[str, Any]) -> str:
    cards = []
    for row in rows:
        ticker = str(row.get("ticker", "n/a"))
        estimate = _expectancy_for(state, ticker)
        expected = _signed_pct(estimate.get("expected_return_pct")) if estimate else "n/a"
        cards.append(
            '<div class="ds-candidate-card">'
            '<div class="ds-candidate-top">'
            f'<div class="ds-candidate-ticker">{_html(ticker)}</div>'
            f'<div class="ds-candidate-rank">#{_html(_format_number(row.get("rank")))}</div>'
            "</div>"
            f'<div class="ds-candidate-score">Score {_html(_format_score(row.get("score")))}</div>'
            '<div class="ds-candidate-levels">'
            '<div class="ds-candidate-row">'
            f'<span>Watch</span><span>{_html(_format_price(row.get("breakout_trigger")))}</span>'
            "</div>"
            '<div class="ds-candidate-row">'
            f'<span>Risk</span><span>{_html(_format_price(row.get("invalidation_level")))}</span>'
            "</div>"
            '<div class="ds-candidate-row">'
            f'<span>Target</span><span>{_html(_format_price(row.get("first_target")))}</span>'
            "</div>"
            '<div class="ds-candidate-row">'
            f'<span>Expected</span><span>{_html(expected)}</span>'
            "</div>"
            "</div>"
            "</div>"
        )
    return f'<div class="ds-card-grid">{"".join(cards)}</div>'


def _column_label(column: str) -> str:
    labels = {
        "rank": "Rank",
        "ticker": "Ticker",
        "score": "Score",
        "total_score": "Score",
        "explosive_score": "Explosive",
        "tradability_score": "Tradability",
        "catalyst_score": "Catalyst",
        "risk_score": "Risk",
        "expected_return_bucket": "Expected",
        "confidence_bucket": "Confidence",
        "source_confidence": "Source",
        "model_version": "Model",
        "config_hash": "Config",
        "setup_grade": "Grade",
        "gap_pct": "Gap",
        "dollar_volume": "Dollar volume",
        "range_position_pct": "Range",
        "breakout_trigger": "Watch price",
        "invalidation_level": "Risk line",
        "first_target": "Target",
        "best_exit_bias": "Exit",
        "risk_flags": "Risk flags",
        "avoid_reasons": "Reason",
        "status": "Status",
        "monitor_confidence_pct": "Confidence",
        "current_price": "Current",
        "path_progress_pct": "Progress",
        "checked_at": "Checked",
        "expected_return_pct": "Expected",
        "confidence_pct": "Confidence",
        "lower_return_pct": "Low",
        "upper_return_pct": "High",
        "risk_adjusted_return_pct": "Risk adjusted",
        "entry_price": "Entry",
        "lunch_return_pct": "Lunch",
        "close_return_pct": "Close",
        "high_return_pct": "High",
        "low_drawdown_pct": "Drawdown",
        "sent_at": "Sent",
        "event_type": "Event",
        "severity": "Severity",
        "suggested_action": "Action",
        "reason": "Reason",
        "provider": "Provider",
        "detail": "Detail",
        "timestamp": "Timestamp",
        "exit_bias": "Exit",
        "confidence_level": "Confidence",
        "return_1m_pct": "1 min",
        "return_5m_pct": "5 min",
        "return_15m_pct": "15 min",
    }
    return labels.get(column, column.replace("_", " ").title())


def _format_cell(column: str, value: Any) -> str:
    if value in {None, ""}:
        return ""
    if column in {
        "breakout_trigger",
        "invalidation_level",
        "first_target",
        "current_price",
        "entry_price",
        "price_1m",
        "price_5m",
        "price_15m",
        "lunch_price",
        "close_price",
        "high_after_entry",
        "low_after_entry",
    }:
        return _format_price(value)
    if column == "dollar_volume":
        return _format_money(value)
    if column.endswith("_pct") or column in {
        "gap_pct",
        "range_position_pct",
        "monitor_confidence_pct",
        "path_progress_pct",
        "expected_return_pct",
        "confidence_pct",
        "lower_return_pct",
        "upper_return_pct",
        "risk_adjusted_return_pct",
        "lunch_return_pct",
        "close_return_pct",
        "high_return_pct",
        "low_drawdown_pct",
    }:
        return f"{_format_number(value)}%"
    if column in {
        "score",
        "total_score",
        "explosive_score",
        "tradability_score",
        "catalyst_score",
        "risk_score",
        "source_confidence",
    }:
        return _format_number(value)
    if column == "status":
        return _friendly(str(value))
    if column == "risk_flags":
        return _friendly_list(value)
    if column == "best_exit_bias":
        return _friendly(str(value))
    if column == "checked_at":
        return _short_timestamp(value)
    return str(value)


def _is_numeric_column(column: str) -> bool:
    return column in {
        "rank",
        "score",
        "gap_pct",
        "dollar_volume",
        "range_position_pct",
        "breakout_trigger",
        "invalidation_level",
        "first_target",
        "monitor_confidence_pct",
        "current_price",
        "path_progress_pct",
        "expected_return_pct",
        "confidence_pct",
        "lower_return_pct",
        "upper_return_pct",
        "risk_adjusted_return_pct",
        "entry_price",
        "price_1m",
        "price_5m",
        "price_15m",
        "lunch_price",
        "close_price",
        "high_after_entry",
        "low_after_entry",
        "lunch_return_pct",
        "close_return_pct",
        "high_return_pct",
        "low_drawdown_pct",
    }


def _expectancy_for(state: dict[str, Any], ticker: str) -> dict[str, Any] | None:
    return _row_by_ticker(state["expectancy"], ticker)


def _row_by_ticker(rows: list[dict[str, Any]], ticker: str) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("ticker", "")).upper() == ticker.upper():
            return row
    return None


def _first(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[0] if rows else None


def _distinct_dates(rows: list[dict[str, Any]]) -> int:
    dates = {
        str(row.get("created_at") or row.get("timestamp") or "")[:10]
        for row in rows
        if str(row.get("created_at") or row.get("timestamp") or "")[:10]
    }
    return len(dates)


def _average(rows: list[dict[str, Any]], key: str) -> float:
    values = [_number(row.get(key)) for row in rows if row.get(key) not in {None, ""}]
    return round(sum(values) / len(values), 2) if values else 0.0


def _minimum(rows: list[dict[str, Any]], key: str) -> float:
    values = [_number(row.get(key)) for row in rows if row.get(key) not in {None, ""}]
    return round(min(values), 2) if values else 0.0


def _win_rate(rows: list[dict[str, Any]], key: str) -> float:
    values = [_number(row.get(key)) for row in rows if row.get(key) not in {None, ""}]
    if not values:
        return 0.0
    return round((sum(1 for value in values if value > 0) / len(values)) * 100, 2)


def _number(value: Any) -> float:
    try:
        if value in {None, ""}:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _format_number(value: Any) -> str:
    number = _number(value)
    if abs(number) >= 100:
        return f"{number:.0f}"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _format_score(value: Any) -> str:
    return _format_number(value)


def _format_money(value: Any) -> str:
    number = _number(value)
    if number >= 1_000_000:
        return f"${number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:.0f}"


def _format_price(value: Any) -> str:
    if value in {None, ""}:
        return "n/a"
    number = _number(value)
    if number == 0 and str(value).strip() not in {"0", "0.0", "0.00"}:
        return "n/a"
    return f"${number:.4f}".rstrip("0").rstrip(".")


def _signed_pct(value: Any) -> str:
    number = _number(value)
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.2f}%"


def _friendly(value: str) -> str:
    normalized = value.strip()
    return FRIENDLY.get(normalized, normalized.replace("_", " ").title())


def _friendly_list(value: Any) -> str:
    if value in {None, ""}:
        return "None"
    parts = [
        part.strip()
        for chunk in str(value).split(";")
        for part in chunk.split(",")
        if part.strip()
    ]
    return ", ".join(_friendly(part) for part in parts) if parts else "None"


def _short_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    cleaned = text.replace("T", " ")
    for suffix in ("+00:00", "-04:00", "-05:00", "Z"):
        cleaned = cleaned.replace(suffix, "")
    return cleaned


def _short_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "None"
    path = Path(text)
    parts = path.parts
    if len(parts) <= 3:
        return text
    return str(Path(parts[-3]) / parts[-2] / parts[-1])


def _html(value: Any) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


if __name__ == "__main__":
    main()
