"""Dashboard data loading from files or SQLite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.reporting import read_csv_dicts, read_scan_summary
from intraday_scanner.scoring import score_universe
from intraday_scanner.services.screener_automation import screener_automation_status
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def load_sample_scan(snapshot_path: str | Path, config: ScannerConfig) -> dict[str, Any]:
    rows = read_snapshot_csv(snapshot_path)
    result = score_universe(rows, config)
    return {
        "data_source_kind": "sample_fixture",
        "summary": result.summary(),
        "ranked_candidates": [candidate.to_dict() for candidate in result.ranked_candidates],
        "top_explosive": [candidate.to_dict() for candidate in result.top_explosive],
        "avoid_list": [candidate.to_dict() for candidate in result.avoid_list],
        "scan_history": [result.summary()],
    }


def load_output_dir(out_dir: str | Path) -> dict[str, Any]:
    base = Path(out_dir)
    return {
        "data_source_kind": "output_files",
        "summary": read_scan_summary(base / "scan_summary.json").get("summary", {}),
        "ranked_candidates": read_csv_dicts(base / "ranked_candidates.csv"),
        "top_explosive": read_csv_dicts(base / "top_explosive.csv"),
        "avoid_list": read_csv_dicts(base / "avoid_list.csv"),
        "scan_history": [],
    }


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
    latest["recent_alerts"] = store.load_recent_alerts()
    latest["monitor_events"] = store.load_recent_monitor_events()
    latest["recommendation_history"] = store.load_recommendation_theses()
    latest["audit_trades"] = store.load_paper_audit_trades()
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
    return latest


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
