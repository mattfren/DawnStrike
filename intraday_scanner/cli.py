"""Command-line interface for scanner operations."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, cast

from intraday_scanner.ai.headline_classifier import RuleBasedHeadlineClassifier
from intraday_scanner.config import ConfigError, load_config
from intraday_scanner.errors import (
    DataProviderError,
    IntradayScannerError,
    SnapshotValidationError,
    StorageError,
)
from intraday_scanner.logging_config import configure_logging
from intraday_scanner.notifiers import (
    audit_summary_events,
    build_notifiers,
    dispatch_events,
    scan_events_from_payload,
)
from intraday_scanner.notifiers.base import NotificationEvent
from intraday_scanner.notifiers.console import ConsoleNotifier
from intraday_scanner.paper_audit import main as paper_audit_main
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.providers.csv_enrichment_provider import CsvEnrichmentProvider
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider, read_snapshot_csv
from intraday_scanner.providers.news_provider import (
    FinnhubNewsProvider,
    NewsAPIProvider,
    build_news_provider,
)
from intraday_scanner.providers.sec_provider import SECRSSProvider
from intraday_scanner.reporting import read_csv_dicts, write_scan_outputs
from intraday_scanner.scheduler import schedule_as_rows
from intraday_scanner.services.alert_service import (
    alerts_from_monitor_rows,
    alerts_from_news_and_filings,
    persist_deduped_alerts,
)
from intraday_scanner.services.alpha_cycle_service import (
    alpha_cycle,
    alpha_doctor,
    alpha_learn,
    alpha_monitor,
    alpha_morning,
    alpha_outcomes,
    alpha_report,
    alpha_status,
)
from intraday_scanner.services.audit_service import run_paper_audit, run_paper_audit_rows
from intraday_scanner.services.calendar_report_service import calendar_report
from intraday_scanner.services.e2e_automation_service import (
    automation_daemon,
    automation_monitor_open,
    automation_morning,
    automation_outcomes,
    automation_run,
    automation_summary,
    safe_url_ingest_screener,
)
from intraday_scanner.services.free_shadow_mode import (
    audit_manual_outcomes,
    build_free_shadow_report,
    build_free_universe,
    import_manual_outcomes,
    import_manual_snapshot,
    print_upload_prompt,
)
from intraday_scanner.services.historical_ingestion_service import (
    backfill_snapshot_runs,
    ingest_minute_bars,
)
from intraday_scanner.services.mover_discovery_service import (
    provider_count_payload,
    record_provider_counts,
    require_universe,
    resolve_universe,
)
from intraday_scanner.services.performance_service import (
    build_performance_report,
    format_performance_report,
)
from intraday_scanner.services.premarket_intelligence import (
    evaluate_intelligence_outcomes,
    write_intelligence_outcome_outputs,
)
from intraday_scanner.services.provider_health_service import (
    record_health_check,
    record_health_status,
)
from intraday_scanner.services.return_attribution_service import (
    attribute_returns,
    historical_report,
)
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.services.screener_automation import (
    auto_shadow_daily,
    auto_shadow_from_screener,
    normalize_screener_file,
    watch_screener_inbox,
)
from intraday_scanner.services.setup_monitor import run_setup_monitor
from intraday_scanner.services.tuning_service import run_strategy_tuning, write_tuning_outputs
from intraday_scanner.services.universe_service import load_symbols_file, parse_symbols
from intraday_scanner.services.web_collection_service import (
    telegram_test,
    web_auto_collect,
    web_build_universe,
    web_collect_halts,
    web_collect_sec_risk,
    web_ingest_public_table,
    web_source_doctor,
    web_telegram_daemon,
)
from intraday_scanner.snapshot_builder import main as snapshot_builder_main
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="intraday-scan")
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Run an offline CSV snapshot scan")
    scan.add_argument("--snapshot", required=True)
    scan.add_argument("--out-dir", default=None)
    scan.add_argument("--db-path", default=None)
    scan.add_argument("--persist", action="store_true")
    scan.add_argument("--print", action="store_true", dest="print_rows")
    scan.add_argument("--top-n", type=int, default=None)
    scan.add_argument("--min-gap-pct", type=float, default=None)
    scan.add_argument("--min-dollar-volume", type=float, default=None)
    scan.add_argument("--min-share-volume", type=int, default=None)
    scan.add_argument("--min-price", type=float, default=None)
    scan.add_argument("--max-price", type=float, default=None)
    scan.add_argument("--enrichment-file", default=None)

    prompt = subparsers.add_parser(
        "print-upload-prompt", help="Print the ChatGPT screener-normalization prompt"
    )
    prompt.set_defaults(command="print-upload-prompt")

    manual_snapshot = subparsers.add_parser(
        "import-manual-snapshot", help="Normalize a manual screener CSV for shadow mode"
    )
    manual_snapshot.add_argument("--input", required=True)
    manual_snapshot.add_argument("--out", required=True)
    manual_snapshot.add_argument("--db-path", default=None)
    manual_snapshot.add_argument("--persist", action="store_true")

    shadow_scan = subparsers.add_parser(
        "free-shadow-scan", help="Run a labeled manual/free shadow scan"
    )
    shadow_scan.add_argument("--snapshot", required=True)
    shadow_scan.add_argument("--db-path", default=None)
    shadow_scan.add_argument("--out-dir", required=True)
    shadow_scan.add_argument("--persist", action="store_true")
    shadow_scan.add_argument("--print", action="store_true", dest="print_rows")
    shadow_scan.add_argument("--top-n", type=int, default=None)

    manual_outcomes = subparsers.add_parser(
        "import-manual-outcomes", help="Import manual outcome prices for saved shadow calls"
    )
    manual_outcomes.add_argument("--input", required=True)
    manual_outcomes.add_argument("--db-path", default=None)
    manual_outcomes.add_argument("--persist", action="store_true")
    manual_outcomes.add_argument("--replace", action="store_true")

    manual_audit = subparsers.add_parser(
        "audit-manual-outcomes", help="Audit manually uploaded shadow outcomes"
    )
    manual_audit.add_argument("--db-path", default=None)
    manual_audit.add_argument("--out-dir", required=True)
    manual_audit.add_argument("--persist", action="store_true")

    intelligence_outcomes = subparsers.add_parser(
        "evaluate-intelligence-outcomes",
        help="Evaluate intelligence classifications against saved outcome prices",
    )
    intelligence_outcomes.add_argument("--db-path", default=None)
    intelligence_outcomes.add_argument("--out-dir", default="outputs/intelligence_outcomes")
    intelligence_outcomes.add_argument("--run-id", default=None)
    intelligence_outcomes.add_argument("--min-samples", type=int, default=20)
    intelligence_outcomes.add_argument("--persist", action="store_true")

    shadow_report = subparsers.add_parser(
        "free-shadow-report", help="Build the cumulative Free Shadow Mode report"
    )
    shadow_report.add_argument("--db-path", default=None)
    shadow_report.add_argument("--out-dir", required=True)
    shadow_report.add_argument("--persist", action="store_true")

    universe = subparsers.add_parser(
        "build-free-universe", help="Build an offline/free starter universe file"
    )
    universe.add_argument("--out", required=True)

    normalize_screener = subparsers.add_parser(
        "normalize-screener-file",
        help="Normalize a raw exported screener file into a canonical manual snapshot",
    )
    normalize_screener.add_argument("--input", required=True)
    normalize_screener.add_argument("--out", required=True)
    normalize_screener.add_argument("--db-path", default=None)
    normalize_screener.add_argument(
        "--ai-normalizer",
        choices=["none", "codex-cli", "openai-api"],
        default="none",
    )
    normalize_screener.add_argument("--scan", action="store_true")
    normalize_screener.add_argument("--persist", action="store_true")
    normalize_screener.add_argument("--print", action="store_true", dest="print_rows")

    auto_shadow = subparsers.add_parser(
        "auto-shadow-from-screener",
        help="Normalize a screener export, run the Free Shadow scan, and archive the raw file",
    )
    auto_shadow.add_argument("--input", required=True)
    auto_shadow.add_argument("--db-path", required=True)
    auto_shadow.add_argument("--out-dir", required=True)
    auto_shadow.add_argument(
        "--ai-normalizer",
        choices=["none", "codex-cli", "openai-api"],
        default="none",
    )
    auto_shadow.add_argument("--persist", action="store_true")
    auto_shadow.add_argument("--print", action="store_true", dest="print_rows")

    watch_screener = subparsers.add_parser(
        "watch-screener-inbox",
        help="Watch a screener inbox and run Free Shadow scans for new raw exports",
    )
    watch_screener.add_argument("--inbox", required=True)
    watch_screener.add_argument("--db-path", required=True)
    watch_screener.add_argument("--out-root", required=True)
    watch_screener.add_argument(
        "--ai-normalizer",
        choices=["none", "codex-cli", "openai-api"],
        default="none",
    )
    watch_screener.add_argument("--poll-seconds", type=int, default=10)
    watch_screener.add_argument("--max-files", type=int, default=None)
    watch_screener.add_argument("--max-minutes", type=float, default=None)

    daily_shadow = subparsers.add_parser(
        "auto-shadow-daily",
        help="Run the daily Free Shadow automation for the latest screener export",
    )
    daily_shadow.add_argument("--date", required=True)
    daily_shadow.add_argument("--db-path", required=True)
    daily_shadow.add_argument(
        "--ai-normalizer",
        choices=["none", "codex-cli", "openai-api"],
        default="none",
    )

    url_ingest = subparsers.add_parser(
        "url-ingest-screener",
        help="Safely ingest a public allowed HTML table into a raw screener CSV",
    )
    url_ingest.add_argument("--url", required=True)
    url_ingest.add_argument("--out", required=True)
    url_ingest.add_argument("--allowed-domain", action="append", dest="allowed_domains")
    url_ingest.add_argument("--timeout-seconds", type=float, default=10.0)

    web_build_universe_parser = subparsers.add_parser(
        "web-build-universe", help="Build a filtered free U.S. common-stock universe"
    )
    web_build_universe_parser.add_argument("--config", default="config/web_sources.example.yaml")
    web_build_universe_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    web_build_universe_parser.add_argument("--out", default="data/universe_us_common.csv")
    web_build_universe_parser.add_argument("--persist", action="store_true")

    web_halts = subparsers.add_parser(
        "web-collect-halts", help="Collect Nasdaq Trader trade halt events"
    )
    web_halts.add_argument("--config", default="config/web_sources.example.yaml")
    web_halts.add_argument("--db-path", default="data/shadow_real.sqlite")
    web_halts.add_argument("--out-dir", default="outputs/web_halts")
    web_halts.add_argument("--persist", action="store_true")

    web_sec = subparsers.add_parser(
        "web-collect-sec-risk", help="Collect SEC filing risk events for candidates"
    )
    web_sec.add_argument("--config", default="config/web_sources.example.yaml")
    web_sec.add_argument("--db-path", default="data/shadow_real.sqlite")
    web_sec.add_argument("--out-dir", default="outputs/web_sec")
    web_sec.add_argument("--tickers", default=None)
    web_sec.add_argument("--persist", action="store_true")

    web_table = subparsers.add_parser(
        "web-ingest-public-table",
        help="Safely ingest an allowed public table into a canonical snapshot",
    )
    web_table.add_argument("--url", required=True)
    web_table.add_argument("--config", default="config/web_sources.example.yaml")
    web_table.add_argument("--db-path", default="data/shadow_real.sqlite")
    web_table.add_argument("--out-dir", required=True)
    web_table.add_argument("--persist", action="store_true")
    web_table.add_argument("--print", action="store_true", dest="print_rows")
    web_table.add_argument("--allow-unlisted-url", action="store_true")

    web_auto = subparsers.add_parser(
        "web-auto-collect", help="Collect local/web candidates and produce a snapshot"
    )
    web_auto.add_argument("--config", default="config/web_sources.example.yaml")
    web_auto.add_argument("--db-path", default="data/shadow_real.sqlite")
    web_auto.add_argument("--out-dir", default="outputs/web_auto")
    web_auto.add_argument("--persist", action="store_true")
    web_auto.add_argument("--print", action="store_true", dest="print_rows")

    telegram = subparsers.add_parser("telegram-test", help="Send or dry-run a Telegram test")
    telegram.add_argument("--db-path", default="data/shadow_real.sqlite")
    telegram.add_argument("--dry-run", action="store_true")
    telegram.add_argument(
        "--force",
        action="store_true",
        help="Bypass dedupe for this test event only",
    )

    source_doctor = subparsers.add_parser(
        "web-source-doctor", help="Diagnose configured web candidate sources"
    )
    source_doctor.add_argument("--config", default="config/web_sources.example.yaml")
    source_doctor.add_argument("--out-dir", default="outputs/source_doctor")
    source_doctor.add_argument("--print", action="store_true", dest="print_rows")

    web_daemon = subparsers.add_parser(
        "web-telegram-daemon", help="Run the web auto-pilot notification daemon"
    )
    web_daemon.add_argument("--config", default="config/web_sources.example.yaml")
    web_daemon.add_argument("--automation-config", default="config/automation.example.yaml")
    web_daemon.add_argument("--db-path", default="data/shadow_real.sqlite")
    web_daemon.add_argument("--out-root", default="outputs/web_telegram")
    web_daemon.add_argument(
        "--ai-mode",
        choices=["none", "codex-cli", "openai-api"],
        default="none",
    )
    web_daemon.add_argument("--notify", default="console")
    web_daemon.add_argument("--dry-run", action="store_true")
    web_daemon.add_argument("--max-cycles", type=int, default=None)
    web_daemon.add_argument("--poll-seconds", type=int, default=60)
    web_daemon.add_argument("--date", default=None)

    alpha_morning_parser = subparsers.add_parser(
        "alpha-morning", help="Run the AlphaOps morning research cycle"
    )
    alpha_morning_parser.add_argument("--config", default="config/web_sources.example.yaml")
    alpha_morning_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_morning_parser.add_argument("--out-dir", default="outputs/alpha_morning")
    alpha_morning_parser.add_argument("--notify", default="console")
    alpha_morning_parser.add_argument("--dry-run", action="store_true")

    alpha_cycle_parser = subparsers.add_parser(
        "alpha-cycle", help="Run one AlphaOps collect-score-notify cycle"
    )
    alpha_cycle_parser.add_argument("--config", default="config/web_sources.example.yaml")
    alpha_cycle_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_cycle_parser.add_argument("--out-dir", default="outputs/alpha_cycle")
    alpha_cycle_parser.add_argument("--notify", default="console")
    alpha_cycle_parser.add_argument("--dry-run", action="store_true")

    alpha_monitor_parser = subparsers.add_parser(
        "alpha-monitor", help="Check latest AlphaOps signals against current prices"
    )
    alpha_monitor_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_monitor_parser.add_argument("--notify", default="console")
    alpha_monitor_parser.add_argument("--dry-run", action="store_true")

    alpha_outcomes_parser = subparsers.add_parser(
        "alpha-outcomes", help="Label saved AlphaOps signals from manual outcomes"
    )
    alpha_outcomes_parser.add_argument("--db-path", default="data/shadow_real.sqlite")

    alpha_learn_parser = subparsers.add_parser(
        "alpha-learn", help="Update AlphaOps setup memory and performance truth"
    )
    alpha_learn_parser.add_argument("--db-path", default="data/shadow_real.sqlite")

    alpha_status_parser = subparsers.add_parser(
        "alpha-status", help="Print AlphaOps persistence and evidence status"
    )
    alpha_status_parser.add_argument("--db-path", default="data/shadow_real.sqlite")

    alpha_doctor_parser = subparsers.add_parser(
        "alpha-doctor", help="Diagnose AlphaOps source and safety readiness"
    )
    alpha_doctor_parser.add_argument("--config", default="config/web_sources.example.yaml")
    alpha_doctor_parser.add_argument("--out-dir", default="outputs/alpha_doctor")

    alpha_report_parser = subparsers.add_parser(
        "alpha-report", help="Write AlphaOps performance and evidence report"
    )
    alpha_report_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_report_parser.add_argument("--out-dir", default="outputs/alpha_report")

    attribute_parser = subparsers.add_parser(
        "attribute-returns",
        help="Calculate historical paper/scenario return attribution",
    )
    attribute_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    attribute_parser.add_argument("--out-dir", default="outputs/return_attribution")
    attribute_parser.add_argument("--persist", action="store_true")
    attribute_parser.add_argument("--notify", default="")

    historical_report_parser = subparsers.add_parser(
        "historical-report",
        help="Write historical signal ledger and accuracy report files",
    )
    historical_report_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    historical_report_parser.add_argument("--out-dir", default="outputs/historical_report")
    historical_report_parser.add_argument("--start", default=None)
    historical_report_parser.add_argument("--end", default=None)

    calendar_report_parser = subparsers.add_parser(
        "calendar-report", help="Write Historical Alpha Calendar review files"
    )
    calendar_report_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    calendar_report_parser.add_argument("--out-dir", default="outputs/calendar_report")
    calendar_report_parser.add_argument("--start", default=None)
    calendar_report_parser.add_argument("--end", default=None)
    calendar_report_parser.add_argument("--month", default=None)

    def add_automation_common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--config", default="config/automation.example.yaml")
        command.add_argument("--db-path", default=None)
        command.add_argument("--out-root", default=None)
        command.add_argument("--date", default=None)

    automation_run_parser = subparsers.add_parser(
        "automation-run",
        help="Run the notification-only automation orchestrator",
    )
    automation_run_parser.add_argument(
        "--mode",
        choices=["once", "daemon", "dry-run"],
        required=True,
    )
    add_automation_common(automation_run_parser)
    automation_run_parser.add_argument("--notify", action="store_true")
    automation_run_parser.add_argument("--max-cycles", type=int, default=None)
    automation_run_parser.add_argument("--poll-seconds", type=int, default=60)

    automation_morning_parser = subparsers.add_parser(
        "automation-morning", help="Run the automated morning Free Shadow scan"
    )
    add_automation_common(automation_morning_parser)
    automation_morning_parser.add_argument("--notify", action="store_true")

    automation_monitor = subparsers.add_parser(
        "automation-monitor-open", help="Run market-open monitor automation"
    )
    add_automation_common(automation_monitor)
    automation_monitor.add_argument("--snapshot", default=None)
    automation_monitor.add_argument("--max-iterations", type=int, default=1)
    automation_monitor.add_argument("--notify", action="store_true")

    automation_outcomes_parser = subparsers.add_parser(
        "automation-outcomes", help="Import/audit outcomes or send outcome reminders"
    )
    add_automation_common(automation_outcomes_parser)
    automation_outcomes_parser.add_argument("--notify", action="store_true")

    automation_summary_parser = subparsers.add_parser(
        "automation-summary", help="Send the daily automation summary notification"
    )
    add_automation_common(automation_summary_parser)
    automation_summary_parser.add_argument("--notify", action="store_true")

    automation_daemon_parser = subparsers.add_parser(
        "automation-daemon", help="Run or dry-run the automation daemon loop"
    )
    add_automation_common(automation_daemon_parser)
    automation_daemon_parser.add_argument("--dry-run", action="store_true")
    automation_daemon_parser.add_argument("--max-cycles", type=int, default=None)
    automation_daemon_parser.add_argument("--poll-seconds", type=int, default=60)
    automation_daemon_parser.add_argument("--notify", action="store_true")

    live = subparsers.add_parser("live-scan", help="Run a provider-backed live scan")
    live.add_argument("--provider", choices=["alpaca"], default="alpaca")
    live.add_argument("--symbols", default=None, help="Comma-separated symbols")
    live.add_argument("--symbols-file", default=None)
    live.add_argument("--universe-file", default=None)
    live.add_argument("--enrichment-file", default=None)
    live.add_argument("--out-dir", default=None)
    live.add_argument("--db-path", default=None)
    live.add_argument("--persist", action="store_true")
    live.add_argument("--print", action="store_true", dest="print_rows")
    live.add_argument("--top-n", type=int, default=None)

    morning = subparsers.add_parser("morning-run", help="Run the morning scan workflow")
    morning.add_argument("--snapshot", default="sample_data/premarket_snapshot_sample.csv")
    morning.add_argument("--out-dir", default=None)
    morning.add_argument("--db-path", default=None)
    morning.add_argument("--top-n", type=int, default=None)
    morning.add_argument("--notify", action="store_true")
    morning.add_argument("--print", action="store_true", dest="print_rows")

    build_snapshot = subparsers.add_parser("build-snapshot", help="Build canonical snapshot CSV")
    build_snapshot.add_argument("--minute-bars", required=True)
    build_snapshot.add_argument("--previous-close", required=True)
    build_snapshot.add_argument("--metadata", required=True)
    build_snapshot.add_argument("--out", required=True)

    audit = subparsers.add_parser("paper-audit", help="Run paper-audit from ranked candidates")
    audit.add_argument("--ranked", required=True)
    audit.add_argument("--minute-bars", required=True)
    audit.add_argument("--out-dir", required=True)
    audit.add_argument("--top-n", type=int, default=3)
    audit.add_argument("--slippage-bps", type=float, default=None)
    audit.add_argument("--entry-mode", choices=["open", "breakout"], default="open")
    audit.add_argument("--db-path", default=None)
    audit.add_argument("--persist", action="store_true")

    init_db = subparsers.add_parser("init-db", help="Initialize SQLite tables")
    init_db.add_argument("--db-path", default=None)

    notify = subparsers.add_parser("notify", help="Send deduped research alerts")
    notify.add_argument("--db-path", default=None)
    notify.add_argument("--audit-summary", default=None)
    notify.add_argument("--dry-run", action="store_true")

    audit_latest = subparsers.add_parser(
        "audit-latest", help="Paper-audit the latest persisted ranked candidates"
    )
    audit_latest.add_argument("--minute-bars", default="sample_data/minute_bars/2026-06-18.csv")
    audit_latest.add_argument("--out-dir", default="outputs/latest_audit")
    audit_latest.add_argument("--db-path", default=None)
    audit_latest.add_argument("--top-n", type=int, default=3)
    audit_latest.add_argument("--slippage-bps", type=float, default=None)
    audit_latest.add_argument("--entry-mode", choices=["open", "breakout"], default="open")
    audit_latest.add_argument("--persist", action="store_true")

    backfill = subparsers.add_parser("backfill-audit", help="Audit a historical ranked CSV")
    backfill.add_argument("--ranked", required=True)
    backfill.add_argument("--minute-bars", required=True)
    backfill.add_argument("--out-dir", required=True)
    backfill.add_argument("--db-path", default=None)
    backfill.add_argument("--top-n", type=int, default=3)
    backfill.add_argument("--slippage-bps", type=float, default=None)
    backfill.add_argument("--entry-mode", choices=["open", "breakout"], default="open")
    backfill.add_argument("--persist", action="store_true")

    monitor = subparsers.add_parser(
        "monitor-setups", help="Check latest ranked setups against a fresh snapshot"
    )
    monitor.add_argument("--snapshot", default="sample_data/premarket_snapshot_sample.csv")
    monitor.add_argument("--provider", choices=["csv", "alpaca"], default="csv")
    monitor.add_argument("--db-path", default=None)
    monitor.add_argument("--out-dir", default="outputs/latest_monitor")
    monitor.add_argument("--persist", action="store_true")
    monitor.add_argument("--top-n", type=int, default=None)
    monitor.add_argument("--symbols", default=None, help="Comma-separated symbols")
    monitor.add_argument("--universe-file", default=None)
    monitor.add_argument(
        "--news-provider", choices=["none", "auto", "newsapi", "finnhub"], default="none"
    )
    monitor.add_argument("--sec-rss", action="store_true")

    monitor_loop = subparsers.add_parser(
        "monitor-loop", help="Repeat setup monitoring until stopped"
    )
    monitor_loop.add_argument("--snapshot", default="sample_data/premarket_snapshot_sample.csv")
    monitor_loop.add_argument("--provider", choices=["csv", "alpaca"], default="csv")
    monitor_loop.add_argument("--db-path", default=None)
    monitor_loop.add_argument("--out-dir", default="outputs/latest_monitor")
    monitor_loop.add_argument("--persist", action="store_true")
    monitor_loop.add_argument("--top-n", type=int, default=None)
    monitor_loop.add_argument("--symbols", default=None, help="Comma-separated symbols")
    monitor_loop.add_argument("--universe-file", default=None)
    monitor_loop.add_argument("--interval-seconds", type=int, default=300)
    monitor_loop.add_argument("--max-iterations", type=int, default=None)
    monitor_loop.add_argument(
        "--news-provider", choices=["none", "auto", "newsapi", "finnhub"], default="none"
    )
    monitor_loop.add_argument("--sec-rss", action="store_true")

    monitor_open = subparsers.add_parser(
        "monitor-open", help="Run 1-minute market-open monitoring"
    )
    monitor_open.add_argument("--snapshot", default="sample_data/premarket_snapshot_sample.csv")
    monitor_open.add_argument("--provider", choices=["csv", "alpaca"], default="csv")
    monitor_open.add_argument("--db-path", default=None)
    monitor_open.add_argument("--out-dir", default="outputs/latest_monitor")
    monitor_open.add_argument("--persist", action="store_true")
    monitor_open.add_argument("--top-n", type=int, default=None)
    monitor_open.add_argument("--symbols", default=None, help="Comma-separated symbols")
    monitor_open.add_argument("--universe-file", default=None)
    monitor_open.add_argument("--interval-seconds", type=int, default=60)
    monitor_open.add_argument("--max-iterations", type=int, default=1)
    monitor_open.add_argument("--continuous", action="store_true")
    monitor_open.add_argument(
        "--news-provider", choices=["none", "auto", "newsapi", "finnhub"], default="none"
    )
    monitor_open.add_argument("--sec-rss", action="store_true")

    notify_test = subparsers.add_parser("notify-test", help="Send a console test alert")
    notify_test.add_argument("--db-path", default=None)

    performance = subparsers.add_parser(
        "performance-report", help="Print historical paper-audit performance"
    )
    performance.add_argument("--db-path", default=None)
    performance.add_argument("--persist", action="store_true")

    ingest = subparsers.add_parser("ingest-minute-bars", help="Validate/copy local minute bars")
    ingest.add_argument("--input", required=True)
    ingest.add_argument("--out-dir", required=True)
    ingest.add_argument("--date", default=None)
    ingest.add_argument("--format", choices=["csv", "parquet"], default="csv")

    backfill_snapshots = subparsers.add_parser(
        "backfill-snapshots", help="Build historical snapshots and optional scan runs"
    )
    backfill_snapshots.add_argument("--minute-bars", required=True)
    backfill_snapshots.add_argument("--previous-close", required=True)
    backfill_snapshots.add_argument("--metadata", required=True)
    backfill_snapshots.add_argument("--out-dir", required=True)
    backfill_snapshots.add_argument("--db-path", default=None)
    backfill_snapshots.add_argument("--persist", action="store_true")
    backfill_snapshots.add_argument("--signal-time", default=None)

    tune = subparsers.add_parser("tune-strategy", help="Tune scoring weights on fixture data")
    tune.add_argument("--snapshot", default="sample_data/premarket_snapshot_sample.csv")
    tune.add_argument("--minute-bars", default="sample_data/minute_bars/2026-06-18.csv")
    tune.add_argument("--out-dir", default="outputs/tuning")
    tune.add_argument("--top-n", type=int, default=5)
    tune.add_argument("--fixture-only", action="store_true", default=True)

    schedule = subparsers.add_parser("scheduler", help="Print the local production schedule")
    schedule.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    try:
        if args.command == "scan":
            return _run_scan(args)
        if args.command == "print-upload-prompt":
            return _run_print_upload_prompt(args)
        if args.command == "import-manual-snapshot":
            return _run_import_manual_snapshot(args)
        if args.command == "free-shadow-scan":
            return _run_free_shadow_scan(args)
        if args.command == "import-manual-outcomes":
            return _run_import_manual_outcomes(args)
        if args.command == "audit-manual-outcomes":
            return _run_audit_manual_outcomes(args)
        if args.command == "evaluate-intelligence-outcomes":
            return _run_evaluate_intelligence_outcomes(args)
        if args.command == "free-shadow-report":
            return _run_free_shadow_report(args)
        if args.command == "build-free-universe":
            return _run_build_free_universe(args)
        if args.command == "normalize-screener-file":
            return _run_normalize_screener_file(args)
        if args.command == "auto-shadow-from-screener":
            return _run_auto_shadow_from_screener(args)
        if args.command == "watch-screener-inbox":
            return _run_watch_screener_inbox(args)
        if args.command == "auto-shadow-daily":
            return _run_auto_shadow_daily(args)
        if args.command == "url-ingest-screener":
            return _run_url_ingest_screener(args)
        if args.command == "web-build-universe":
            return _run_web_build_universe(args)
        if args.command == "web-collect-halts":
            return _run_web_collect_halts(args)
        if args.command == "web-collect-sec-risk":
            return _run_web_collect_sec_risk(args)
        if args.command == "web-ingest-public-table":
            return _run_web_ingest_public_table(args)
        if args.command == "web-auto-collect":
            return _run_web_auto_collect(args)
        if args.command == "telegram-test":
            return _run_telegram_test(args)
        if args.command == "web-source-doctor":
            return _run_web_source_doctor(args)
        if args.command == "web-telegram-daemon":
            return _run_web_telegram_daemon(args)
        if args.command == "alpha-morning":
            return _run_alpha_morning(args)
        if args.command == "alpha-cycle":
            return _run_alpha_cycle(args)
        if args.command == "alpha-monitor":
            return _run_alpha_monitor(args)
        if args.command == "alpha-outcomes":
            return _run_alpha_outcomes(args)
        if args.command == "alpha-learn":
            return _run_alpha_learn(args)
        if args.command == "alpha-status":
            return _run_alpha_status(args)
        if args.command == "alpha-doctor":
            return _run_alpha_doctor(args)
        if args.command == "alpha-report":
            return _run_alpha_report(args)
        if args.command == "attribute-returns":
            return _run_attribute_returns(args)
        if args.command == "historical-report":
            return _run_historical_report(args)
        if args.command == "calendar-report":
            return _run_calendar_report(args)
        if args.command == "automation-run":
            return _run_automation_run(args)
        if args.command == "automation-morning":
            return _run_automation_morning(args)
        if args.command == "automation-monitor-open":
            return _run_automation_monitor_open(args)
        if args.command == "automation-outcomes":
            return _run_automation_outcomes(args)
        if args.command == "automation-summary":
            return _run_automation_summary(args)
        if args.command == "automation-daemon":
            return _run_automation_daemon(args)
        if args.command == "live-scan":
            return _run_live_scan(args)
        if args.command == "morning-run":
            return _run_morning_run(args)
        if args.command == "build-snapshot":
            return snapshot_builder_main(
                [
                    "--minute-bars",
                    args.minute_bars,
                    "--previous-close",
                    args.previous_close,
                    "--metadata",
                    args.metadata,
                    "--out",
                    args.out,
                ]
            )
        if args.command == "paper-audit":
            if args.persist:
                return _run_backfill_audit(args)
            paper_args = [
                "--ranked",
                args.ranked,
                "--minute-bars",
                args.minute_bars,
                "--out-dir",
                args.out_dir,
                "--top-n",
                str(args.top_n),
            ]
            if args.slippage_bps is not None:
                paper_args.extend(["--slippage-bps", str(args.slippage_bps)])
            paper_args.extend(["--entry-mode", args.entry_mode])
            return paper_audit_main(paper_args)
        if args.command == "init-db":
            return _run_init_db(args)
        if args.command == "notify":
            return _run_notify(args)
        if args.command == "audit-latest":
            return _run_audit_latest(args)
        if args.command == "backfill-audit":
            return _run_backfill_audit(args)
        if args.command == "monitor-setups":
            return _run_monitor_setups(args)
        if args.command == "monitor-loop":
            return _run_monitor_loop(args)
        if args.command == "monitor-open":
            return _run_monitor_open(args)
        if args.command == "notify-test":
            return _run_notify_test(args)
        if args.command == "performance-report":
            return _run_performance_report(args)
        if args.command == "ingest-minute-bars":
            return _run_ingest_minute_bars(args)
        if args.command == "backfill-snapshots":
            return _run_backfill_snapshots(args)
        if args.command == "tune-strategy":
            return _run_tune_strategy(args)
        if args.command == "scheduler":
            return _run_scheduler(args)
        parser.error("Unknown command")
        return 2
    except (ConfigError, DataProviderError, SnapshotValidationError, StorageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except IntradayScannerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run_scan(args: argparse.Namespace) -> int:
    config = load_config(
        provider="csv",
        output_dir=Path(args.out_dir) if args.out_dir else None,
        database_path=Path(args.db_path) if args.db_path else None,
        top_n=args.top_n,
        min_gap_pct=args.min_gap_pct,
        min_premarket_dollar_volume=args.min_dollar_volume,
        min_premarket_share_volume=args.min_share_volume,
        min_price=args.min_price,
        max_price=args.max_price,
    )
    provider = CsvSnapshotProvider(args.snapshot)
    store = SQLiteScanStore(config.database_path) if args.persist else None
    if store is not None:
        record_health_check(store, provider="csv", check=provider.validate_credentials)
    result = ScanService(
        provider, store=store, enrichment_providers=_enrichment_providers(args)
    ).run(config, persist=args.persist)
    paths = write_scan_outputs(result, config.output_dir)
    _print_scan_done(paths, result.summary(), args.print_rows)
    return 0


def _run_print_upload_prompt(args: argparse.Namespace) -> int:
    _ = args
    print(print_upload_prompt())
    return 0


def _run_import_manual_snapshot(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    store = SQLiteScanStore(config.database_path) if args.persist else None
    result = import_manual_snapshot(
        input_path=args.input,
        out_dir=args.out,
        store=store,
        persist=args.persist,
    )
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    print(f"Wrote normalized manual snapshot to {result['path']}")
    return 0


def _run_free_shadow_scan(args: argparse.Namespace) -> int:
    config = load_config(
        provider="csv",
        output_dir=Path(args.out_dir),
        database_path=Path(args.db_path) if args.db_path else None,
        top_n=args.top_n,
    )
    store = SQLiteScanStore(config.database_path)
    provider = CsvSnapshotProvider(args.snapshot)
    record_health_check(store, provider="manual_shadow_csv", check=provider.validate_credentials)
    result = ScanService(provider, store=store).run(config, persist=False)
    result.config.update(
        {
            "data_source_kind": "manual",
            "shadow_mode": True,
            "manual_uploaded_data": True,
            "paid_data": False,
            "fixture_only": _is_fixture_path(args.snapshot),
        }
    )
    if args.persist:
        store.persist_scan_result(result)
    paths = write_scan_outputs(result, config.output_dir)
    _print_scan_done(paths, result.summary(), args.print_rows)
    return 0


def _run_import_manual_outcomes(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    result = import_manual_outcomes(
        input_path=args.input,
        store=SQLiteScanStore(config.database_path),
        persist=args.persist,
        replace=args.replace,
    )
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}, indent=2))
    return 0


def _run_audit_manual_outcomes(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    result = audit_manual_outcomes(
        store=SQLiteScanStore(config.database_path),
        out_dir=args.out_dir,
        persist=args.persist,
    )
    paths = cast(dict[str, Path], result.get("paths") or {})
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    print(f"Wrote manual audit trades to {paths['trades']}")
    print(f"Wrote manual audit summary to {paths['summary']}")
    return 0


def _run_evaluate_intelligence_outcomes(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    result = evaluate_intelligence_outcomes(
        store=SQLiteScanStore(config.database_path),
        run_id=args.run_id,
        min_samples=args.min_samples,
        persist=args.persist,
    )
    paths = write_intelligence_outcome_outputs(result, args.out_dir)
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    print(f"Wrote intelligence outcomes to {paths['rows']}")
    print(f"Wrote intelligence summary to {paths['summary']}")
    return 0


def _run_free_shadow_report(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    result = build_free_shadow_report(
        store=SQLiteScanStore(config.database_path),
        out_dir=args.out_dir,
        persist=args.persist,
    )
    paths = cast(dict[str, Path], result.get("paths") or {})
    print(json.dumps(result["report"], indent=2, sort_keys=True))
    print(f"Wrote Free Shadow Mode report to {paths['report']}")
    return 0


def _run_build_free_universe(args: argparse.Namespace) -> int:
    result = build_free_universe(out_path=args.out)
    paths = cast(dict[str, Path], result.get("paths") or {})
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    print(f"Wrote free universe to {paths['universe']}")
    return 0


def _run_normalize_screener_file(args: argparse.Namespace) -> int:
    config = load_config(
        provider="csv",
        output_dir=Path(args.out),
        database_path=Path(args.db_path) if args.db_path else None,
    )
    store = SQLiteScanStore(config.database_path) if args.db_path else None
    result = normalize_screener_file(
        input_path=args.input,
        out_dir=args.out,
        ai_normalizer=args.ai_normalizer,
        store=store,
    )
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    print(f"Wrote normalized screener snapshot to {result['paths']['snapshot']}")
    if not args.scan:
        return 0
    snapshot_path = Path(result["paths"]["snapshot"])
    scan_store = SQLiteScanStore(config.database_path) if args.persist and args.db_path else None
    scan_result = ScanService(CsvSnapshotProvider(snapshot_path), store=scan_store).run(
        config, persist=False
    )
    scan_result.config.update(
        {
            "data_source_kind": "manual",
            "shadow_mode": True,
            "manual_uploaded_data": True,
            "paid_data": False,
            "fixture_only": False,
        }
    )
    if scan_store is not None:
        scan_store.persist_scan_result(scan_result)
    paths = write_scan_outputs(scan_result, Path(args.out) / "scan")
    _print_scan_done(paths, scan_result.summary(), args.print_rows)
    return 0


def _run_auto_shadow_from_screener(args: argparse.Namespace) -> int:
    result = auto_shadow_from_screener(
        input_path=args.input,
        db_path=args.db_path,
        out_dir=args.out_dir,
        ai_normalizer=args.ai_normalizer,
        persist=args.persist,
        print_rows=args.print_rows,
    )
    print(json.dumps(_printable_auto_shadow_result(result), indent=2, sort_keys=True))
    if result.get("status") == "success":
        scan_summary = cast(dict[str, Any], result.get("scan_summary") or {})
        paths = {
            key: Path(value)
            for key, value in cast(dict[str, str], result.get("paths") or {}).items()
        }
        if paths:
            _print_scan_done(paths, scan_summary, args.print_rows)
    return 0


def _run_watch_screener_inbox(args: argparse.Namespace) -> int:
    result = watch_screener_inbox(
        inbox=args.inbox,
        db_path=args.db_path,
        out_root=args.out_root,
        ai_normalizer=args.ai_normalizer,
        poll_seconds=args.poll_seconds,
        max_files=args.max_files,
        max_minutes=args.max_minutes,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_auto_shadow_daily(args: argparse.Namespace) -> int:
    result = auto_shadow_daily(
        date=args.date,
        db_path=args.db_path,
        ai_normalizer=args.ai_normalizer,
    )
    print(json.dumps(_printable_auto_shadow_result(result), indent=2, sort_keys=True))
    return 0


def _run_url_ingest_screener(args: argparse.Namespace) -> int:
    path = safe_url_ingest_screener(
        url=args.url,
        out_dir=args.out,
        allowed_domains=tuple(args.allowed_domains or []),
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps({"status": "success", "path": str(path)}, indent=2, sort_keys=True))
    return 0


def _run_web_build_universe(args: argparse.Namespace) -> int:
    result = web_build_universe(
        config_path=args.config,
        db_path=args.db_path,
        out_path=args.out,
        persist=args.persist,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_web_collect_halts(args: argparse.Namespace) -> int:
    result = web_collect_halts(
        config_path=args.config,
        db_path=args.db_path,
        out_dir=args.out_dir,
        persist=args.persist,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_web_collect_sec_risk(args: argparse.Namespace) -> int:
    tickers = [ticker.strip() for ticker in str(args.tickers or "").split(",") if ticker.strip()]
    result = web_collect_sec_risk(
        config_path=args.config,
        db_path=args.db_path,
        out_dir=args.out_dir,
        tickers=tickers or None,
        persist=args.persist,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_web_ingest_public_table(args: argparse.Namespace) -> int:
    result = web_ingest_public_table(
        url=args.url,
        config_path=args.config,
        db_path=args.db_path,
        out_dir=args.out_dir,
        persist=args.persist,
        print_rows=args.print_rows,
        allow_unlisted_url=args.allow_unlisted_url,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_web_auto_collect(args: argparse.Namespace) -> int:
    result = web_auto_collect(
        config_path=args.config,
        db_path=args.db_path,
        out_dir=args.out_dir,
        persist=args.persist,
        print_rows=args.print_rows,
    )
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}, indent=2))
    return 0


def _run_telegram_test(args: argparse.Namespace) -> int:
    result = telegram_test(db_path=args.db_path, dry_run=args.dry_run, force=args.force)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_web_source_doctor(args: argparse.Namespace) -> int:
    result = web_source_doctor(
        config_path=args.config,
        out_dir=args.out_dir,
        print_rows=args.print_rows,
    )
    if not args.print_rows:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_web_telegram_daemon(args: argparse.Namespace) -> int:
    result = web_telegram_daemon(
        config_path=args.config,
        automation_config_path=args.automation_config,
        db_path=args.db_path,
        out_root=args.out_root,
        ai_mode=args.ai_mode,
        notify=args.notify,
        dry_run=args.dry_run,
        max_cycles=args.max_cycles,
        poll_seconds=args.poll_seconds,
        run_date=args.date,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_morning(args: argparse.Namespace) -> int:
    result = alpha_morning(
        config_path=args.config,
        db_path=args.db_path,
        out_dir=args.out_dir,
        notify=args.notify,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_cycle(args: argparse.Namespace) -> int:
    result = alpha_cycle(
        config_path=args.config,
        db_path=args.db_path,
        out_dir=args.out_dir,
        notify=args.notify,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_monitor(args: argparse.Namespace) -> int:
    result = alpha_monitor(
        db_path=args.db_path,
        notify=args.notify,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_outcomes(args: argparse.Namespace) -> int:
    result = alpha_outcomes(db_path=args.db_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_learn(args: argparse.Namespace) -> int:
    result = alpha_learn(db_path=args.db_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_status(args: argparse.Namespace) -> int:
    result = alpha_status(db_path=args.db_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_doctor(args: argparse.Namespace) -> int:
    result = alpha_doctor(config_path=args.config, out_dir=args.out_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_report(args: argparse.Namespace) -> int:
    result = alpha_report(db_path=args.db_path, out_dir=args.out_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_attribute_returns(args: argparse.Namespace) -> int:
    result = attribute_returns(
        db_path=args.db_path,
        out_dir=args.out_dir,
        persist=args.persist,
        notify=args.notify,
    )
    print(json.dumps({key: value for key, value in result.items() if key != "summary"}, indent=2))
    return 0


def _run_historical_report(args: argparse.Namespace) -> int:
    result = historical_report(
        db_path=args.db_path,
        out_dir=args.out_dir,
        start=args.start,
        end=args.end,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_calendar_report(args: argparse.Namespace) -> int:
    result = calendar_report(
        db_path=args.db_path,
        out_dir=args.out_dir,
        start=args.start,
        end=args.end,
        month=args.month,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_automation_run(args: argparse.Namespace) -> int:
    result = automation_run(
        mode=args.mode,
        config_path=args.config,
        db_path=args.db_path,
        out_root=args.out_root,
        run_date=args.date,
        notify=args.notify,
        max_cycles=args.max_cycles,
        poll_seconds=args.poll_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_automation_morning(args: argparse.Namespace) -> int:
    result = automation_morning(
        config_path=args.config,
        db_path=args.db_path,
        out_root=args.out_root,
        run_date=args.date,
        notify=args.notify,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_automation_monitor_open(args: argparse.Namespace) -> int:
    result = automation_monitor_open(
        config_path=args.config,
        db_path=args.db_path,
        out_root=args.out_root,
        run_date=args.date,
        snapshot=args.snapshot,
        max_iterations=args.max_iterations,
        notify=args.notify,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_automation_outcomes(args: argparse.Namespace) -> int:
    result = automation_outcomes(
        config_path=args.config,
        db_path=args.db_path,
        out_root=args.out_root,
        run_date=args.date,
        notify=args.notify,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_automation_summary(args: argparse.Namespace) -> int:
    result = automation_summary(
        config_path=args.config,
        db_path=args.db_path,
        out_root=args.out_root,
        run_date=args.date,
        notify=args.notify,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_automation_daemon(args: argparse.Namespace) -> int:
    result = automation_daemon(
        config_path=args.config,
        db_path=args.db_path,
        out_root=args.out_root,
        run_date=args.date,
        notify=args.notify,
        dry_run=args.dry_run,
        max_cycles=args.max_cycles,
        poll_seconds=args.poll_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_live_scan(args: argparse.Namespace) -> int:
    config = load_config(
        provider=args.provider,
        output_dir=Path(args.out_dir) if args.out_dir else None,
        database_path=Path(args.db_path) if args.db_path else None,
        top_n=args.top_n,
    )
    explicit_symbols = parse_symbols(args.symbols) + load_symbols_file(args.symbols_file)
    selection = resolve_universe(
        provider_name=args.provider,
        config=config,
        explicit_symbols=explicit_symbols,
        universe_file=args.universe_file,
    )
    require_universe(selection.symbols, args.provider)
    provider = AlpacaProvider(config)
    store = SQLiteScanStore(config.database_path)
    record_health_check(store, provider="alpaca", check=provider.validate_credentials)
    result = ScanService(
        provider, store=store, enrichment_providers=_enrichment_providers(args)
    ).run(config, symbols=selection.symbols, persist=args.persist)
    record_provider_counts(
        store,
        args.provider,
        provider_count_payload(
            symbols_requested=selection.symbols,
            snapshots=[candidate.snapshot for candidate in result.all_candidates],
            result=result,
        ),
    )
    paths = write_scan_outputs(result, config.output_dir)
    _print_scan_done(paths, result.summary(), args.print_rows)
    return 0


def _run_morning_run(args: argparse.Namespace) -> int:
    config = load_config(
        provider="csv",
        output_dir=Path(args.out_dir) if args.out_dir else None,
        database_path=Path(args.db_path) if args.db_path else None,
        top_n=args.top_n,
    )
    store = SQLiteScanStore(config.database_path)
    provider = CsvSnapshotProvider(args.snapshot)
    record_health_check(store, provider="csv", check=provider.validate_credentials)
    result = ScanService(provider, store=store).run(config, persist=True)
    paths = write_scan_outputs(result, config.output_dir)
    _print_scan_done(paths, result.summary(), args.print_rows)
    print(f"morning-run saved recommendations for run_id={result.run_id}")
    if args.notify:
        payload = {
            "summary": result.summary(),
            "ranked_candidates": [row.to_dict() for row in result.ranked_candidates],
            "top_explosive": [row.to_dict() for row in result.top_explosive],
            "avoid_list": [row.to_dict() for row in result.avoid_list],
        }
        stats = dispatch_events(
            scan_events_from_payload(payload, config), build_notifiers(config), store
        )
        print(f"morning-run alerts sent={stats['sent']} skipped={stats['skipped']}")
    return 0


def _run_init_db(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    SQLiteScanStore(config.database_path).initialize()
    print(f"Initialized SQLite database at {config.database_path}")
    return 0


def _run_notify(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    store = SQLiteScanStore(config.database_path)
    notifiers = build_notifiers(config)
    if args.audit_summary:
        summary = json.loads(Path(args.audit_summary).read_text(encoding="utf-8"))
        events = audit_summary_events(summary)
    else:
        scan = store.load_latest_scan()
        if scan is None:
            print("No persisted scan is available to notify.")
            return 0
        events = scan_events_from_payload(scan, config)
    stats = dispatch_events(events, notifiers, store, dry_run=args.dry_run)
    print(f"Notification events sent={stats['sent']} skipped={stats['skipped']}")
    return 0


def _run_audit_latest(args: argparse.Namespace) -> int:
    config = load_config(
        database_path=Path(args.db_path) if args.db_path else None,
        slippage_bps=args.slippage_bps,
        entry_mode=args.entry_mode,
    )
    store = SQLiteScanStore(config.database_path)
    latest = store.load_latest_scan()
    if latest is None:
        print("No persisted scan is available to audit.", file=sys.stderr)
        return 1
    ranked_rows = cast(list[dict[str, Any]], latest.get("ranked_candidates") or [])
    minute_rows = read_csv_dicts(args.minute_bars)
    paths = run_paper_audit_rows(
        ranked_rows,
        minute_rows,
        args.out_dir,
        config,
        top_n=args.top_n,
        fixture_only=_is_fixture_path(args.minute_bars),
    )
    if args.persist:
        _persist_audit_paths(store, paths)
    _print_audit_done(paths)
    return 0


def _run_backfill_audit(args: argparse.Namespace) -> int:
    config = load_config(
        database_path=Path(args.db_path) if args.db_path else None,
        slippage_bps=args.slippage_bps,
        entry_mode=args.entry_mode,
    )
    paths = run_paper_audit(
        args.ranked,
        args.minute_bars,
        args.out_dir,
        config,
        top_n=args.top_n,
        fixture_only=_is_fixture_path(args.minute_bars),
    )
    if args.persist:
        _persist_audit_paths(SQLiteScanStore(config.database_path), paths)
    _print_audit_done(paths)
    return 0


def _run_monitor_setups(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    store = SQLiteScanStore(config.database_path)
    latest = store.load_latest_scan()
    if latest is None:
        print("No persisted scan is available to monitor.", file=sys.stderr)
        return 1
    ranked_rows = cast(list[dict[str, Any]], latest.get("ranked_candidates") or [])
    if not ranked_rows:
        print("Latest persisted scan has no ranked candidates to monitor.", file=sys.stderr)
        return 1
    summary = cast(dict[str, Any], latest.get("summary") or {})
    source_run_id = str(summary.get("run_id") or latest.get("run_id") or "")
    snapshots = _load_monitor_snapshots(args, config, store, ranked_rows)
    result = run_setup_monitor(
        candidates=ranked_rows,
        snapshots=snapshots,
        out_dir=args.out_dir,
        store=store,
        persist=args.persist,
        source_run_id=source_run_id or None,
        top_n=args.top_n,
        symbols=parse_symbols(args.symbols),
        config=config,
    )
    if args.persist:
        alerts = alerts_from_monitor_rows(
            list(result.get("rows") or []), run_id=source_run_id or None
        )
        alerts.extend(
            _external_risk_alerts(
                args=args,
                config=config,
                store=store,
                ranked_rows=ranked_rows,
                source_run_id=source_run_id or None,
            )
        )
        sent = persist_deduped_alerts(store, alerts, run_id=source_run_id or None)
        if alerts:
            print(f"monitor alerts saved={sent} generated={len(alerts)}")
    _print_monitor_done(result)
    return 0


def _run_monitor_loop(args: argparse.Namespace) -> int:
    interval_seconds = max(1, int(args.interval_seconds))
    iterations = 0
    try:
        while True:
            status = _run_monitor_setups(args)
            if status != 0:
                return status
            iterations += 1
            if args.max_iterations is not None and iterations >= int(args.max_iterations):
                return 0
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("Monitor loop stopped.")
        return 0


def _run_monitor_open(args: argparse.Namespace) -> int:
    if args.continuous:
        args.max_iterations = None
    return _run_monitor_loop(args)


def _load_monitor_snapshots(
    args: argparse.Namespace,
    config: Any,
    store: SQLiteScanStore,
    ranked_rows: list[dict[str, Any]],
) -> list[Any]:
    provider_name = str(getattr(args, "provider", "csv"))
    symbols = _monitor_symbols(ranked_rows, args, config)
    if provider_name == "alpaca":
        provider = AlpacaProvider(config)
        record_health_check(store, provider="alpaca", check=provider.validate_credentials)
        snapshots = provider.get_premarket_snapshot(symbols, config)
        record_health_status(
            store,
            provider="alpaca",
            status="ok",
            detail=f"loaded live monitor snapshot rows={len(snapshots)}",
        )
        return snapshots
    snapshots = read_snapshot_csv(args.snapshot)
    record_health_status(
        store,
        provider="csv",
        status="ok",
        detail=f"loaded monitor snapshot rows={len(snapshots)}",
    )
    return snapshots


def _monitor_symbols(
    ranked_rows: list[dict[str, Any]], args: argparse.Namespace, config: Any
) -> list[str]:
    explicit = parse_symbols(getattr(args, "symbols", None))
    if explicit:
        return explicit
    universe_file = getattr(args, "universe_file", None)
    if universe_file:
        return resolve_universe(
            provider_name=str(getattr(args, "provider", "csv")),
            config=config,
            universe_file=universe_file,
        ).symbols
    limit = int(getattr(args, "top_n", None) or 10)
    return [str(row.get("ticker", "")).upper() for row in ranked_rows[:limit] if row.get("ticker")]


def _external_risk_alerts(
    *,
    args: argparse.Namespace,
    config: Any,
    store: SQLiteScanStore,
    ranked_rows: list[dict[str, Any]],
    source_run_id: str | None,
) -> list[Any]:
    symbols = [str(row.get("ticker", "")).upper() for row in ranked_rows[: int(args.top_n or 10)]]
    theses = {
        str(row.get("ticker", "")).upper(): str(row.get("catalyst_headline") or "")
        for row in ranked_rows
    }
    news_items = []
    filing_items = []
    news_provider_name = str(getattr(args, "news_provider", "none"))
    if news_provider_name != "none":
        news_provider = _news_provider(news_provider_name, config)
        record_health_check(
            store,
            provider=f"news:{news_provider_name}",
            check=news_provider.validate_credentials,
        )
        news_items = news_provider.get_news(symbols)
    if bool(getattr(args, "sec_rss", False)):
        sec_provider = SECRSSProvider(config)
        record_health_check(store, provider="sec_rss", check=sec_provider.validate_credentials)
        filing_items = sec_provider.get_filings(symbols)
    if not news_items and not filing_items:
        return []
    return alerts_from_news_and_filings(
        news_items=news_items,
        filing_items=filing_items,
        original_theses=theses,
        classifier=RuleBasedHeadlineClassifier(),
        run_id=source_run_id,
    )


def _news_provider(name: str, config: Any) -> Any:
    if name == "auto":
        return build_news_provider(config)
    if name == "newsapi":
        return NewsAPIProvider(config)
    if name == "finnhub":
        return FinnhubNewsProvider(config)
    raise DataProviderError(f"Unsupported news provider: {name}")


def _run_notify_test(args: argparse.Namespace) -> int:
    event = NotificationEvent(
        event_key="notify-test",
        title="Dawnstrike notification test",
        body="Console notifier is wired. Research/watchlist only; no orders are placed.",
        channel_hint="test",
        ticker="TEST",
        payload={"source": "notify-test"},
    )
    if args.db_path:
        config = load_config(database_path=Path(args.db_path))
        stats = dispatch_events(
            [event],
            [ConsoleNotifier()],
            SQLiteScanStore(config.database_path),
        )
        print(f"notify-test sent={stats['sent']} skipped={stats['skipped']}")
        return 0
    ConsoleNotifier().send(event)
    return 0


def _run_ingest_minute_bars(args: argparse.Namespace) -> int:
    result = ingest_minute_bars(
        input_path=args.input,
        out_dir=args.out_dir,
        source_date=args.date,
        file_format=args.format,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_backfill_snapshots(args: argparse.Namespace) -> int:
    config = load_config(
        database_path=Path(args.db_path) if args.db_path else None,
        signal_time=args.signal_time,
    )
    result = backfill_snapshot_runs(
        minute_bars=args.minute_bars,
        previous_close=args.previous_close,
        metadata=args.metadata,
        out_dir=args.out_dir,
        config=config,
        persist=args.persist,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_performance_report(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    store = SQLiteScanStore(config.database_path)
    trades = store.load_paper_audit_trades()
    summary = store.load_latest_paper_audit_summary()
    if not trades:
        print("No persisted paper-audit trades are available.", file=sys.stderr)
        return 1
    report = build_performance_report(trades, summary)
    if args.persist:
        store.persist_performance_report(report)
    print(format_performance_report(report))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _enrichment_providers(args: argparse.Namespace) -> list[Any]:
    enrichment_file = getattr(args, "enrichment_file", None)
    if not enrichment_file:
        return []
    return [CsvEnrichmentProvider(enrichment_file)]


def _is_fixture_path(value: str) -> bool:
    return "sample_data" in value.replace("/", "\\").lower()


def _run_tune_strategy(args: argparse.Namespace) -> int:
    config = load_config()
    snapshots = read_snapshot_csv(args.snapshot)
    minute_bars = read_csv_dicts(args.minute_bars)
    report = run_strategy_tuning(
        snapshots=snapshots,
        minute_bars=minute_bars,
        base_config=config,
        fixture_only=bool(args.fixture_only),
        top_n=args.top_n,
    )
    paths = write_tuning_outputs(report, args.out_dir)
    best = dict(report.get("best") or {})
    label = "fixture-only" if report.get("fixture_only") else "historical"
    print(
        f"tune-strategy ({label}): best={best.get('scenario', 'n/a')} "
        f"top3_close={best.get('top_3_close_return_pct', 0)}% "
        f"hit_rate={best.get('hit_rate_close_pct', 0)}%"
    )
    print(f"Wrote tuning CSV to {paths['csv']}")
    print(f"Wrote tuning summary to {paths['summary']}")
    return 0


def _run_scheduler(args: argparse.Namespace) -> int:
    rows = schedule_as_rows()
    if args.as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for row in rows:
        print(
            f"{row['time_ct']} CT | {row['name']} | {row['command']} | {row['description']}"
        )
    return 0


def _persist_audit_paths(store: SQLiteScanStore, paths: dict[str, Path]) -> None:
    trades = read_csv_dicts(paths["trades"])
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    store.persist_paper_audit(summary, trades)


def _print_audit_done(paths: dict[str, Path]) -> None:
    print(f"Wrote paper audit trades to {paths['trades']}")
    print(f"Wrote paper audit summary to {paths['summary']}")


def _print_monitor_done(result: dict[str, Any]) -> None:
    paths = cast(dict[str, Path], result.get("paths") or {})
    summary = cast(dict[str, Any], result.get("summary") or {})
    print(f"Wrote setup monitor checks to {paths['checks']}")
    print(f"Wrote setup monitor summary to {paths['summary']}")
    print(
        "monitor: "
        f"confirming={summary.get('confirming_count', 0)} "
        f"watching={summary.get('watching_count', 0)} "
        f"extended={summary.get('extended_count', 0)} "
        f"fading={summary.get('fading_count', 0)} "
        f"invalidated={summary.get('invalidated_count', 0)}"
    )


def _printable_auto_shadow_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result)
    if "normalization" in normalized:
        normalization = dict(cast(dict[str, Any], normalized["normalization"]))
        normalization.pop("warnings", None)
        normalized["normalization"] = normalization
    return normalized


def _print_scan_done(paths: dict[str, Path], summary: dict[str, object], print_rows: bool) -> None:
    print(f"Wrote ranked candidates to {paths['ranked_candidates']}")
    print(f"Wrote top explosive names to {paths['top_explosive']}")
    print(f"Wrote avoid list to {paths['avoid_list']}")
    print(f"Wrote summary to {paths['summary']}")
    if print_rows:
        print(
            "summary: "
            f"ranked={summary['ranked_count']} "
            f"avoid={summary['avoid_count']} "
            f"top={summary['top_ticker']}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
