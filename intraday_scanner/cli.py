"""Command-line interface for scanner operations."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from intraday_scanner.ai.headline_classifier import RuleBasedHeadlineClassifier
from intraday_scanner.alpha.v6.registry import register_experiment
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
from intraday_scanner.performance.account_session_reporting import (
    build_account_session_report,
)
from intraday_scanner.performance.cli import main as performance_reconcile_main
from intraday_scanner.performance.strategy_miss_attribution import (
    attribute_strategy_misses,
    load_strategy_learning_database_snapshot_readonly,
)
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
from intraday_scanner.services.alpha_alert_replay_service import (
    write_alpha_alert_replay_report,
)
from intraday_scanner.services.alpha_attribution_service import (
    generate_alpha_attribution_report,
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
from intraday_scanner.services.alpha_eod_gate_service import evaluate_alpha_eod_gate
from intraday_scanner.services.alpha_outcome_capture_service import (
    capture_sourced_alpha_outcomes,
)
from intraday_scanner.services.alpha_paper_reconciliation_service import (
    reconcile_alpha_paper_trades,
)
from intraday_scanner.services.alpha_v6_holdout_service import (
    evaluate_registered_holdout,
)
from intraday_scanner.services.alpha_v6_learning_service import (
    run_alpha_v6_daily_monitor,
    run_alpha_v6_learning,
    run_alpha_v6_weekly_training,
)
from intraday_scanner.services.alpha_v6_research_service import (
    write_alpha_v6_research_packet,
)
from intraday_scanner.services.alpha_v6_universe_adapter_service import (
    build_alpha_v6_universe_candidate,
    validate_alpha_v6_universe_candidate,
    write_alpha_v6_universe_candidate,
)
from intraday_scanner.services.alpha_v6_universe_service import (
    preview_alpha_v6_universe,
    register_alpha_v6_universe,
    restore_alpha_v6_universe,
)
from intraday_scanner.services.audit_service import run_paper_audit, run_paper_audit_rows
from intraday_scanner.services.calendar_report_service import calendar_report
from intraday_scanner.services.daily_account_session_reconciliation import (
    reconcile_daily_account_sessions,
)
from intraday_scanner.services.daily_orchestrator_service import (
    daily_orchestration_status,
    write_heartbeat,
)
from intraday_scanner.services.daily_run_service import (
    resolve_release_sha,
    shared_daily_run_id,
)
from intraday_scanner.services.daily_strategy_learning_service import (
    AttributionReportAnalyzer,
    DailyLearningContext,
    MappingEvidenceAnalyzer,
    StrategyEvidenceAnalyzer,
    _authenticated_no_evidence_receipts,
    _build_daily_strategy_catalog,
    run_daily_strategy_learning,
)
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
from intraday_scanner.services.indeterminate_research_service import (
    run_indeterminate_research,
)
from intraday_scanner.services.managed_learning_queue_service import (
    LearningQueuePolicy,
    LearningQueueValidationError,
    produce_managed_learning_queue,
)
from intraday_scanner.services.mover_discovery_service import (
    provider_count_payload,
    record_provider_counts,
    require_universe,
    resolve_universe,
)
from intraday_scanner.services.opportunity_research_service import (
    LocalResearchStatus,
    OpportunityResearchMode,
    run_local_opportunity_research,
)
from intraday_scanner.services.outcome_gap_service import outcome_gap_report
from intraday_scanner.services.performance_service import (
    build_performance_report,
    format_performance_report,
)
from intraday_scanner.services.premarket_intelligence import (
    evaluate_intelligence_outcomes,
    write_intelligence_outcome_outputs,
)
from intraday_scanner.services.price_observation_service import (
    collect_price_observations,
)
from intraday_scanner.services.provider_health_service import (
    record_health_check,
    record_health_status,
)
from intraday_scanner.services.release_doctor_service import (
    dashboard_doctor,
    probability_doctor,
    scheduler_doctor,
)
from intraday_scanner.services.return_attribution_service import (
    attribute_returns,
    historical_report,
)
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.services.scenario_intelligence_service import (
    close_open_scenario_positions,
    finalize_scenario_performance,
    run_scenario_cycle,
    run_scenario_historical_replay,
    scenario_doctor,
    scenario_public_snapshot,
)
from intraday_scanner.services.screener_automation import (
    auto_shadow_daily,
    auto_shadow_from_screener,
    normalize_screener_file,
    watch_screener_inbox,
)
from intraday_scanner.services.setup_monitor import (
    monitor_interval_gap_receipt,
    run_setup_monitor,
)
from intraday_scanner.services.strategy_challenger_backtest_service import (
    run_strategy_challenger_backtest,
)
from intraday_scanner.services.strategy_challenger_evaluation_service import (
    StrategyChallengerEvidenceError,
    run_strategy_challenger_weekly_adapter,
)
from intraday_scanner.services.trade_watcher_service import run_trade_watcher
from intraday_scanner.services.tuning_service import run_strategy_tuning, write_tuning_outputs
from intraday_scanner.services.universe_service import load_symbols_file, parse_symbols
from intraday_scanner.services.v6_learning_service import (
    build_v6_failure_attribution,
)
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
    web_build_universe_parser.add_argument("--config", default="config/web_sources.yaml")
    web_build_universe_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    web_build_universe_parser.add_argument("--out", default="data/universe_us_common.csv")
    web_build_universe_parser.add_argument("--persist", action="store_true")

    web_halts = subparsers.add_parser(
        "web-collect-halts", help="Collect Nasdaq Trader trade halt events"
    )
    web_halts.add_argument("--config", default="config/web_sources.yaml")
    web_halts.add_argument("--db-path", default="data/shadow_real.sqlite")
    web_halts.add_argument("--out-dir", default="outputs/web_halts")
    web_halts.add_argument("--persist", action="store_true")

    web_sec = subparsers.add_parser(
        "web-collect-sec-risk", help="Collect SEC filing risk events for candidates"
    )
    web_sec.add_argument("--config", default="config/web_sources.yaml")
    web_sec.add_argument("--db-path", default="data/shadow_real.sqlite")
    web_sec.add_argument("--out-dir", default="outputs/web_sec")
    web_sec.add_argument("--tickers", default=None)
    web_sec.add_argument("--persist", action="store_true")

    web_table = subparsers.add_parser(
        "web-ingest-public-table",
        help="Safely ingest an allowed public table into a canonical snapshot",
    )
    web_table.add_argument("--url", required=True)
    web_table.add_argument("--config", default="config/web_sources.yaml")
    web_table.add_argument("--db-path", default="data/shadow_real.sqlite")
    web_table.add_argument("--out-dir", required=True)
    web_table.add_argument("--persist", action="store_true")
    web_table.add_argument("--print", action="store_true", dest="print_rows")
    web_table.add_argument("--allow-unlisted-url", action="store_true")

    web_auto = subparsers.add_parser(
        "web-auto-collect", help="Collect local/web candidates and produce a snapshot"
    )
    web_auto.add_argument("--config", default="config/web_sources.yaml")
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
    source_doctor.add_argument("--config", default="config/web_sources.yaml")
    source_doctor.add_argument("--out-dir", default="outputs/source_doctor")
    source_doctor.add_argument("--print", action="store_true", dest="print_rows")

    web_daemon = subparsers.add_parser(
        "web-telegram-daemon", help="Run the web auto-pilot notification daemon"
    )
    web_daemon.add_argument("--config", default="config/web_sources.yaml")
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
    alpha_morning_parser.add_argument("--config", default="config/web_sources.yaml")
    alpha_morning_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_morning_parser.add_argument("--out-dir", default="outputs/alpha_morning")
    alpha_morning_parser.add_argument("--notify", default="console")
    alpha_morning_parser.add_argument("--dry-run", action="store_true")
    alpha_morning_parser.add_argument("--market-date", default=None)
    alpha_morning_parser.add_argument(
        "--as-of",
        default=None,
        help="UTC cycle observation timestamp (defaults to the current time)",
    )
    alpha_morning_parser.add_argument(
        "--core-universe-manifest",
        default=None,
        help="Governed JSON S&P 500/Nasdaq-100 manifest (absent remains DATA_UNAVAILABLE)",
    )
    alpha_morning_parser.add_argument(
        "--paper-ops-root",
        default="data/v2_paper_ops_live",
        help="Read-only governed prior-session PaperOps root for strategy research lineage",
    )
    alpha_morning_parser.add_argument(
        "--code-sha",
        default=None,
        help="Exact full Git SHA used to build strategy receipts",
    )

    alpha_cycle_parser = subparsers.add_parser(
        "alpha-cycle", help="Run one AlphaOps collect-score-notify cycle"
    )
    alpha_cycle_parser.add_argument("--config", default="config/web_sources.yaml")
    alpha_cycle_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_cycle_parser.add_argument("--out-dir", default="outputs/alpha_cycle")
    alpha_cycle_parser.add_argument("--notify", default="console")
    alpha_cycle_parser.add_argument("--dry-run", action="store_true")
    alpha_cycle_parser.add_argument("--market-date", default=None)
    alpha_cycle_parser.add_argument(
        "--as-of",
        default=None,
        help="UTC cycle observation timestamp (defaults to the current time)",
    )
    alpha_cycle_parser.add_argument(
        "--core-universe-manifest",
        default=None,
        help="Governed JSON S&P 500/Nasdaq-100 manifest (absent remains DATA_UNAVAILABLE)",
    )
    alpha_cycle_parser.add_argument(
        "--paper-ops-root",
        default="data/v2_paper_ops_live",
        help="Read-only governed prior-session PaperOps root for strategy research lineage",
    )
    alpha_cycle_parser.add_argument(
        "--code-sha",
        default=None,
        help="Exact full Git SHA used to build strategy receipts",
    )

    alpha_monitor_parser = subparsers.add_parser(
        "alpha-monitor", help="Check latest AlphaOps signals against current prices"
    )
    alpha_monitor_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_monitor_parser.add_argument("--notify", default="console")
    alpha_monitor_parser.add_argument("--dry-run", action="store_true")
    alpha_monitor_parser.add_argument(
        "--market-date",
        default=None,
        help="Scheduled market date used for notification-preflight evidence",
    )
    alpha_monitor_parser.add_argument(
        "--observation-bundle",
        default=None,
        help="Immutable five-minute bars/quotes bundle shared with trade-watch",
    )
    alpha_monitor_parser.add_argument("--cycle-id", default=None)

    alpha_outcomes_parser = subparsers.add_parser(
        "alpha-outcomes", help="Label saved AlphaOps signals from manual outcomes"
    )
    alpha_outcomes_parser.add_argument("--db-path", default="data/shadow_real.sqlite")

    alpha_alert_replay_parser = subparsers.add_parser(
        "alpha-alert-replay",
        help="Read-only replay of historical AlphaOps alert-gate decisions",
    )
    alpha_alert_replay_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_alert_replay_parser.add_argument("--out", required=True)

    alpha_capture_parser = subparsers.add_parser(
        "alpha-capture-outcomes",
        help="Capture sourced regular-session outcomes for saved AlphaOps signals",
    )
    alpha_capture_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_capture_parser.add_argument("--market-date", default=None)
    alpha_capture_parser.add_argument("--at", default=None)
    alpha_capture_parser.add_argument("--out-dir", default="outputs/alpha_outcomes")
    alpha_capture_parser.add_argument("--persist", action="store_true")
    alpha_capture_parser.add_argument("--replace", action="store_true")
    alpha_capture_parser.add_argument(
        "--max-close-staleness-seconds",
        type=int,
        default=90,
    )

    alpha_eod_gate_parser = subparsers.add_parser(
        "alpha-eod-gate",
        help="Fail closed unless exact official outcome truth permits EOD continuation",
    )
    alpha_eod_gate_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_eod_gate_parser.add_argument("--market-date", required=True)
    alpha_eod_gate_parser.add_argument("--capture-exit-code", required=True, type=int)
    alpha_eod_gate_parser.add_argument("--capture-result", required=True)
    alpha_eod_gate_parser.add_argument("--outcome-gap", required=True)
    alpha_eod_gate_parser.add_argument("--out", required=True)

    alpha_paper_reconcile_parser = subparsers.add_parser(
        "alpha-paper-reconcile",
        help="Reconcile exact AlphaOps selections into sourced paper trades",
    )
    alpha_paper_reconcile_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_paper_reconcile_parser.add_argument("--market-date", default=None)
    alpha_paper_reconcile_parser.add_argument(
        "--out-dir", default="outputs/strategy_reconciliation"
    )
    alpha_paper_reconcile_parser.add_argument("--persist", action="store_true")
    alpha_paper_reconcile_parser.add_argument("--notional-per-trade", type=float, default=1000.0)
    alpha_paper_reconcile_parser.add_argument("--fee-bps", type=float, default=1.0)

    alpha_learn_parser = subparsers.add_parser(
        "alpha-learn", help="Update AlphaOps setup memory and performance truth"
    )
    alpha_learn_parser.add_argument("--db-path", default="data/shadow_real.sqlite")

    alpha_v6_learn_parser = subparsers.add_parser(
        "alpha-v6-learn",
        help="Append sourced V6 shadow labels and strict walk-forward evidence",
    )
    alpha_v6_learn_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_v6_learn_parser.add_argument("--code-sha", default="unresolved-local-sha")

    alpha_v6_daily_monitor_parser = subparsers.add_parser(
        "alpha-v6-daily-monitor",
        help="Append V6 outcomes, labels, dataset and drift evidence without refitting",
    )
    alpha_v6_daily_monitor_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_v6_daily_monitor_parser.add_argument("--market-date", default=None)
    alpha_v6_daily_monitor_parser.add_argument(
        "--reference-window", default=None,
        help="Optional JSON object or path containing a frozen reference drift window",
    )
    alpha_v6_daily_monitor_parser.add_argument(
        "--recent-window", default=None,
        help="Optional JSON object or path containing a frozen recent drift window",
    )

    daily_strategy_learning_parser = subparsers.add_parser(
        "strategy-learning-daily",
        help="Inventory strategies and write research-only daily miss-learning artifacts",
    )
    daily_strategy_learning_parser.add_argument("--market-date", required=True)
    daily_strategy_learning_parser.add_argument("--cutoff", required=True)
    daily_strategy_learning_parser.add_argument("--source-identity", required=True)
    daily_strategy_learning_parser.add_argument("--source-hash-sha256", default=None)
    daily_strategy_learning_parser.add_argument("--code-sha", required=True)
    daily_strategy_learning_parser.add_argument("--out-dir", required=True)
    daily_strategy_learning_parser.add_argument(
        "--paper-ops-root",
        default=None,
        help="Optional governed PaperOps root read through the immutable blotter materializer",
    )
    daily_strategy_evidence = daily_strategy_learning_parser.add_mutually_exclusive_group()
    daily_strategy_evidence.add_argument(
        "--evidence-file",
        default=None,
        help=(
            "Optional JSON mapping keyed by strategy ID for injected evidence/proposals; "
            "decision_receipts, when present, must be exact persisted receipts"
        ),
    )
    daily_strategy_evidence.add_argument(
        "--db-path",
        default=None,
        help=(
            "Optional SQLite database held under a non-mutating reserved source lock, "
            "then read with PRAGMA query_only; attributes retained portfolio performance "
            "rows through market-date"
        ),
    )

    managed_learning_queue_parser = subparsers.add_parser(
        "managed-learning-queue",
        help="Produce the private post-commit managed learning queue",
    )
    managed_learning_queue_parser.add_argument(
        "--approved-root",
        required=True,
        help="Explicit root containing committed daily-learning receipt/proposal pairs",
    )
    managed_learning_queue_parser.add_argument("--out-root", required=True)
    managed_learning_queue_parser.add_argument("--calendar", required=True)
    managed_learning_queue_parser.add_argument("--as-of-market-date", default=None)

    strategy_challenger_backtest_parser = subparsers.add_parser(
        "strategy-challenger-backtest",
        help="Compare all catalog strategies and research challengers on verified DataTruth",
    )
    strategy_challenger_backtest_parser.add_argument("--data-truth-root", required=True)
    strategy_challenger_backtest_parser.add_argument("--snapshot-id", default=None)
    strategy_challenger_backtest_parser.add_argument("--code-sha", required=True)
    strategy_challenger_backtest_parser.add_argument("--out", required=True)

    strategy_challenger_weekly_parser = subparsers.add_parser(
        "strategy-challenger-evaluate-weekly",
        help="Write one immutable, evidence-bound weekly challenger receipt",
    )
    strategy_challenger_weekly_parser.add_argument("--db-path", required=True)
    strategy_challenger_weekly_parser.add_argument("--state-root", required=True)
    strategy_challenger_weekly_parser.add_argument("--market-date", required=True)
    strategy_challenger_weekly_parser.add_argument("--code-sha", required=True)
    strategy_challenger_weekly_parser.add_argument(
        "--out-root",
        default=None,
        help="Optional approved output root; evidence is always read from --state-root",
    )

    alpha_v6_train_weekly_parser = subparsers.add_parser(
        "alpha-v6-train-weekly",
        help="Run the separately scheduled V6 refit and all-family OOF evaluation",
    )
    alpha_v6_train_weekly_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_v6_train_weekly_parser.add_argument("--code-sha", default="unresolved-local-sha")
    alpha_v6_train_weekly_parser.add_argument("--market-date", default=None)
    alpha_v6_train_weekly_parser.add_argument("--attempt-id", default=None)
    alpha_v6_train_weekly_parser.add_argument("--reference-window", default=None)
    alpha_v6_train_weekly_parser.add_argument("--recent-window", default=None)

    alpha_v6_register_experiment_parser = subparsers.add_parser(
        "alpha-v6-register-experiment",
        help="Register one forward-only V6 experiment from an operator JSON contract",
    )
    alpha_v6_register_experiment_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_v6_register_experiment_parser.add_argument("--input", required=True)

    alpha_v6_holdout_parser = subparsers.add_parser(
        "alpha-v6-evaluate-holdout",
        help="Evaluate one immutable, tagged V6 holdout only after its frozen start",
    )
    alpha_v6_holdout_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_v6_holdout_parser.add_argument("--experiment-id", required=True)
    alpha_v6_holdout_parser.add_argument("--as-of", required=True)
    alpha_v6_holdout_parser.add_argument(
        "--model-run-id",
        default=None,
        help="Exact frozen model run to bind into the immutable holdout receipt",
    )

    alpha_v6_attribution_parser = subparsers.add_parser(
        "alpha-v6-attribution",
        help="Explain V6 shadow outcomes and propose holdout-only experiments",
    )
    alpha_v6_attribution_parser.add_argument("--db-path", default="data/shadow_real.sqlite")

    alpha_v6_packet_parser = subparsers.add_parser(
        "alpha-v6-research-packet",
        help="Write V6 failure attribution and experiment registry research artifacts",
    )
    alpha_v6_packet_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_v6_packet_parser.add_argument("--code-sha", default="unresolved-local-sha")
    alpha_v6_packet_parser.add_argument("--out-dir", default="outputs/alpha_v6_research")

    alpha_v6_universe_parser = subparsers.add_parser(
        "alpha-v6-register-universe",
        help="Append a sourced, versioned AlphaOps V6 universe snapshot",
    )
    alpha_v6_universe_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_v6_universe_parser.add_argument("--input", required=True)
    alpha_v6_universe_parser.add_argument(
        "--source-contract",
        required=True,
        help="The exact approved source contract used to build --input.",
    )
    alpha_v6_universe_parser.add_argument(
        "--raw-artifact",
        required=True,
        help="The exact raw source artifact used to build --input.",
    )
    alpha_v6_universe_parser.add_argument(
        "--confirm-preview-hash",
        required=True,
        help="Exact preview_hash_sha256 from alpha-v6-preview-universe.",
    )

    alpha_v6_universe_preview_parser = subparsers.add_parser(
        "alpha-v6-preview-universe",
        help="Diff a sourced AlphaOps V6 universe without mutating durable state",
    )
    alpha_v6_universe_preview_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_v6_universe_preview_parser.add_argument("--input", required=True)

    alpha_v6_universe_build_parser = subparsers.add_parser(
        "alpha-v6-build-universe",
        help="Validate a recorded point-in-time source artifact into a V6 preview candidate",
    )
    alpha_v6_universe_build_parser.add_argument("--source-contract", required=True)
    alpha_v6_universe_build_parser.add_argument("--raw-artifact", required=True)
    alpha_v6_universe_build_parser.add_argument("--out", required=True)

    alpha_v6_universe_restore_parser = subparsers.add_parser(
        "alpha-v6-restore-universe",
        help="Append an audited forward restore from an immutable V6 universe version",
    )
    alpha_v6_universe_restore_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_v6_universe_restore_parser.add_argument("--universe-id", required=True)
    alpha_v6_universe_restore_parser.add_argument("--as-of", required=True)
    alpha_v6_universe_restore_parser.add_argument("--operator", required=True)
    alpha_v6_universe_restore_parser.add_argument("--reason", required=True)

    daily_heartbeat_parser = subparsers.add_parser(
        "daily-heartbeat", help="Write durable daily-DAG heartbeat evidence"
    )
    daily_heartbeat_parser.add_argument("--state-root", required=True)
    daily_heartbeat_parser.add_argument("--runtime-root", default=".")
    daily_heartbeat_parser.add_argument("--market-date", required=True)
    daily_heartbeat_parser.add_argument("--stage", required=True)
    daily_heartbeat_parser.add_argument("--status", default="RUNNING")
    daily_heartbeat_parser.add_argument("--release-sha", default="")

    daily_status_parser = subparsers.add_parser(
        "daily-orchestrator-status",
        help="Report stale heartbeats and missing/failed daily DAG stages",
    )
    daily_status_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    daily_status_parser.add_argument("--state-root", required=True)
    daily_status_parser.add_argument("--market-date", required=True)
    daily_status_parser.add_argument("--heartbeat-ttl-minutes", type=int, default=30)

    account_session_parser = subparsers.add_parser(
        "account-session-report",
        help="Report canonical account/session evidence and target status",
    )
    account_session_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    account_session_parser.add_argument("--market-date", default=None)
    account_session_parser.add_argument("--account-id", default=None)
    account_session_parser.add_argument("--window-days", type=int, default=30)
    account_session_parser.add_argument("--code-sha", default=None)
    account_session_parser.add_argument("--experiment-id", default=None)
    account_session_parser.add_argument("--arm-id", default=None)

    account_session_reconcile_parser = subparsers.add_parser(
        "account-session-reconcile",
        help="Produce one bounded canonical paper account/session ledger slice",
    )
    account_session_reconcile_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    account_session_reconcile_parser.add_argument("--market-date", required=True)
    account_session_reconcile_parser.add_argument("--account-id", default=None)
    account_session_reconcile_parser.add_argument("--release-sha", required=True)
    account_session_reconcile_parser.add_argument("--now", default=None)
    account_session_reconcile_parser.add_argument(
        "--evidence-mode",
        choices=("forward_observed", "retrospective_research"),
        default="forward_observed",
    )

    alpha_status_parser = subparsers.add_parser(
        "alpha-status", help="Print AlphaOps persistence and evidence status"
    )
    alpha_status_parser.add_argument("--db-path", default="data/shadow_real.sqlite")

    alpha_doctor_parser = subparsers.add_parser(
        "alpha-doctor", help="Diagnose AlphaOps source and safety readiness"
    )
    alpha_doctor_parser.add_argument("--config", default="config/web_sources.yaml")
    alpha_doctor_parser.add_argument("--out-dir", default="outputs/alpha_doctor")

    alpha_report_parser = subparsers.add_parser(
        "alpha-report", help="Write AlphaOps performance and evidence report"
    )
    alpha_report_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_report_parser.add_argument("--out-dir", default="outputs/alpha_report")

    alpha_attribution_parser = subparsers.add_parser(
        "alpha-attribution",
        help="Write causal AlphaOps daily and cumulative attribution",
    )
    alpha_attribution_parser.add_argument(
        "--db-path",
        default="data/shadow_real.sqlite",
    )
    alpha_attribution_parser.add_argument(
        "--out-dir",
        default="outputs/alpha_attribution",
    )
    alpha_attribution_parser.add_argument("--start", default=None)
    alpha_attribution_parser.add_argument("--end", default=None)
    alpha_attribution_parser.add_argument(
        "--paper-ops-root",
        default=None,
        help="Optional bounded PaperOps root for cross-version attribution.",
    )

    scenario_doctor_parser = subparsers.add_parser(
        "scenario-doctor", help="Check Scenario Intelligence readiness without calling providers"
    )
    scenario_doctor_parser.add_argument("--db-path", default="data/shadow_real.sqlite")

    indeterminate_research_parser = subparsers.add_parser(
        "indeterminate-research",
        help="Collect cited OpenAI web research for a data-ineligible AlphaOps universe",
    )
    indeterminate_research_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    indeterminate_research_parser.add_argument("--symbols", required=True)
    indeterminate_research_parser.add_argument("--selection-outcome", required=True)
    indeterminate_research_parser.add_argument("--market-date", required=True)
    indeterminate_research_parser.add_argument("--out", required=True)
    indeterminate_research_parser.add_argument("--notify", default="console")
    indeterminate_research_parser.add_argument("--dry-run", action="store_true")

    scenario_cycle_parser = subparsers.add_parser(
        "scenario-cycle", help="Fetch Alpaca news and create research-only scenario candidates"
    )
    scenario_cycle_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    scenario_cycle_parser.add_argument("--symbols", default=None)
    scenario_cycle_parser.add_argument("--since", default=None)
    scenario_cycle_parser.add_argument("--until", default=None)
    scenario_cycle_parser.add_argument("--dry-run", action="store_true")
    scenario_cycle_parser.add_argument("--notify", default="console")

    scenario_monitor_parser = subparsers.add_parser(
        "scenario-monitor",
        help="Run the deduplicated Scenario Intelligence monitor and paper lifecycle check",
    )
    scenario_monitor_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    scenario_monitor_parser.add_argument("--symbols", default=None)
    scenario_monitor_parser.add_argument("--since", default=None)
    scenario_monitor_parser.add_argument("--until", default=None)
    scenario_monitor_parser.add_argument("--dry-run", action="store_true")
    scenario_monitor_parser.add_argument("--notify", default="console")

    scenario_replay_parser = subparsers.add_parser(
        "scenario-replay", help="Record a separately labeled historical scenario research cohort"
    )
    scenario_replay_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    scenario_replay_parser.add_argument("--symbols", required=True)
    scenario_replay_parser.add_argument("--start", required=True)
    scenario_replay_parser.add_argument("--end", required=True)

    scenario_finalize_parser = subparsers.add_parser(
        "scenario-finalize", help="Reconcile scenario-linked paper return records"
    )
    scenario_finalize_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    scenario_finalize_parser.add_argument("--market-date", default=None)

    scenario_close_parser = subparsers.add_parser(
        "scenario-close", help="Close open Scenario Intelligence paper positions at EOD"
    )
    scenario_close_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    scenario_close_parser.add_argument("--market-date", default=None)
    scenario_close_parser.add_argument("--at", default="16:00")
    scenario_close_parser.add_argument("--source", default="alpaca")
    scenario_close_parser.add_argument("--notify", default="console")

    scenario_report_parser = subparsers.add_parser(
        "scenario-report", help="Print the safe static Scenario Intelligence projection"
    )
    scenario_report_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    scenario_report_parser.add_argument("--limit", type=int, default=250)

    outcome_gap_parser = subparsers.add_parser(
        "outcome-gap",
        help="Report unresolved outcome truth without converting gaps to zero",
    )
    outcome_gap_parser.add_argument(
        "--db-path",
        default="data/shadow_real.sqlite",
    )
    outcome_gap_parser.add_argument("--market-date", default=None)
    outcome_gap_parser.add_argument("--out", default=None)

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
    monitor_loop.add_argument(
        "--market-date", default=None, help="Explicit America/New_York market date for gap receipts"
    )
    monitor_loop.add_argument("--schedule-id", default="alphaops-monitor-5m")
    monitor_loop.add_argument(
        "--persist-interval-gaps",
        action="store_true",
        help="Opt in to release-bound scheduled interval-gap receipts",
    )
    monitor_loop.add_argument(
        "--release-sha", default=None, help="Exact lowercase runtime SHA for persisted gap receipts"
    )
    monitor_loop.add_argument("--max-iterations", type=int, default=None)
    monitor_loop.add_argument(
        "--news-provider", choices=["none", "auto", "newsapi", "finnhub"], default="none"
    )
    monitor_loop.add_argument("--sec-rss", action="store_true")

    monitor_gap = subparsers.add_parser(
        "monitor-gap", help="Persist one idempotent missed monitor interval receipt"
    )
    monitor_gap.add_argument("--db-path", default=None)
    monitor_gap.add_argument("--expected-at", required=True)
    monitor_gap.add_argument("--observed-at", required=True)
    monitor_gap.add_argument("--interval-seconds", type=int, required=True)
    monitor_gap.add_argument("--market-date", required=True)
    monitor_gap.add_argument("--run-id", default=None)
    monitor_gap.add_argument("--schedule-id", default="alphaops-monitor-5m")
    monitor_gap.add_argument("--release-sha", required=True)

    monitor_open = subparsers.add_parser("monitor-open", help="Run 1-minute market-open monitoring")
    monitor_open.add_argument("--snapshot", default="sample_data/premarket_snapshot_sample.csv")
    monitor_open.add_argument("--provider", choices=["csv", "alpaca"], default="csv")
    monitor_open.add_argument("--db-path", default=None)
    monitor_open.add_argument("--out-dir", default="outputs/latest_monitor")
    monitor_open.add_argument("--persist", action="store_true")
    monitor_open.add_argument("--top-n", type=int, default=None)
    monitor_open.add_argument("--symbols", default=None, help="Comma-separated symbols")
    monitor_open.add_argument("--universe-file", default=None)
    monitor_open.add_argument("--interval-seconds", type=int, default=60)
    monitor_open.add_argument(
        "--market-date", default=None, help="Explicit America/New_York market date for gap receipts"
    )
    monitor_open.add_argument("--schedule-id", default="monitor-open-1m")
    monitor_open.add_argument(
        "--persist-interval-gaps",
        action="store_true",
        help="Opt in to release-bound scheduled interval-gap receipts",
    )
    monitor_open.add_argument(
        "--release-sha", default=None, help="Exact lowercase runtime SHA for persisted gap receipts"
    )
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

    canonical_performance = subparsers.add_parser(
        "performance-reconcile", help="Reconcile the canonical performance read model"
    )
    canonical_performance.add_argument("--db-path", default="data/shadow_real.sqlite")
    canonical_performance.add_argument("--paper-ops-root", default="data/v2_paper_ops_live")
    canonical_performance.add_argument("--as-of", dest="as_of", default=None)
    canonical_performance.add_argument("--persist", action="store_true")
    canonical_performance.add_argument("--print", action="store_true", dest="print_result")

    ingest = subparsers.add_parser("ingest-minute-bars", help="Validate/copy local minute bars")
    ingest.add_argument("--input", required=True)
    ingest.add_argument("--out-dir", required=True)
    ingest.add_argument("--date", default=None)
    ingest.add_argument("--format", choices=["csv", "parquet"], default="csv")

    price_observe = subparsers.add_parser(
        "price-observe",
        help="Persist auditable time-specific prices for saved picks or supplied tickers",
    )
    price_observe.add_argument("--db-path", default="data/shadow_real.sqlite")
    price_observe.add_argument(
        "--source",
        choices=["auto", "csv", "alpaca", "yahoo"],
        default="auto",
    )
    price_observe.add_argument("--minute-bars", default=None)
    price_observe.add_argument("--tickers", default=None, help="Comma-separated symbols")
    price_observe.add_argument("--market-date", default=None)
    price_observe.add_argument(
        "--at",
        default=None,
        help="ISO timestamp or HH:MM with --market-date",
    )
    price_observe.add_argument("--max-age-seconds", type=int, default=360)
    price_observe.add_argument("--no-persist", action="store_true")

    trade_watch = subparsers.add_parser(
        "trade-watch",
        help="Evaluate saved selections and simulate governed paper fills",
    )
    trade_watch.add_argument("--db-path", default="data/shadow_real.sqlite")
    trade_watch.add_argument(
        "--mode",
        choices=["observe_only", "paper_execute", "live_execute"],
        default="paper_execute",
    )
    trade_watch.add_argument(
        "--source",
        choices=["auto", "csv", "alpaca", "yahoo"],
        default="auto",
    )
    trade_watch.add_argument("--minute-bars", default=None)
    trade_watch.add_argument("--tickers", default=None)
    trade_watch.add_argument("--market-date", default=None)
    trade_watch.add_argument("--at", default=None)
    trade_watch.add_argument("--max-age-seconds", type=int, default=360)
    trade_watch.add_argument("--expected-code-sha", default=None)
    trade_watch.add_argument("--observation-bundle", default=None)
    trade_watch.add_argument("--cycle-id", default=None)
    trade_watch.add_argument("--notify", default="console")
    trade_watch.add_argument("--dry-run", action="store_true")
    trade_watch.add_argument("--notional-per-trade", type=float, default=1000.0)
    trade_watch.add_argument("--simulated-equity", type=float, default=100_000.0)
    trade_watch.add_argument("--max-open-positions", type=int, default=3)
    trade_watch.add_argument("--max-daily-entries", type=int, default=10)
    trade_watch.add_argument("--min-reward-risk", type=float, default=1.5)
    trade_watch.add_argument("--notify-blocked", action="store_true")
    trade_watch.add_argument(
        "--include-scenarios",
        action="store_true",
        help=(
            "Include bounded Scenario Intelligence paper candidates alongside AlphaOps selections."
        ),
    )

    trade_watch_loop = subparsers.add_parser(
        "trade-watch-loop",
        help="Continuously evaluate governed paper selections",
    )
    trade_watch_loop.add_argument("--db-path", default="data/shadow_real.sqlite")
    trade_watch_loop.add_argument(
        "--mode",
        choices=["observe_only", "paper_execute", "live_execute"],
        default="paper_execute",
    )
    trade_watch_loop.add_argument(
        "--source",
        choices=["auto", "csv", "alpaca", "yahoo"],
        default="auto",
    )
    trade_watch_loop.add_argument("--minute-bars", default=None)
    trade_watch_loop.add_argument("--tickers", default=None)
    trade_watch_loop.add_argument("--market-date", default=None)
    trade_watch_loop.add_argument("--at", default=None)
    trade_watch_loop.add_argument("--max-age-seconds", type=int, default=360)
    trade_watch_loop.add_argument("--expected-code-sha", default=None)
    trade_watch_loop.add_argument("--observation-bundle", default=None)
    trade_watch_loop.add_argument("--cycle-id", default=None)
    trade_watch_loop.add_argument("--notify", default="console")
    trade_watch_loop.add_argument("--dry-run", action="store_true")
    trade_watch_loop.add_argument("--notional-per-trade", type=float, default=1000.0)
    trade_watch_loop.add_argument("--simulated-equity", type=float, default=100_000.0)
    trade_watch_loop.add_argument("--max-open-positions", type=int, default=3)
    trade_watch_loop.add_argument("--max-daily-entries", type=int, default=10)
    trade_watch_loop.add_argument("--min-reward-risk", type=float, default=1.5)
    trade_watch_loop.add_argument("--notify-blocked", action="store_true")
    trade_watch_loop.add_argument("--include-scenarios", action="store_true")
    trade_watch_loop.add_argument("--interval-seconds", type=float, default=60.0)
    trade_watch_loop.add_argument("--max-iterations", type=int, default=0)

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

    probability_doctor_parser = subparsers.add_parser(
        "probability-doctor", help="Report calibration evidence without claiming calibration"
    )
    probability_doctor_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    probability_doctor_parser.add_argument("--print", action="store_true", dest="print_result")

    scheduler_doctor_parser = subparsers.add_parser(
        "scheduler-doctor", help="Check daily publication scheduler artifacts"
    )
    scheduler_doctor_parser.add_argument("--root", default=".")
    scheduler_doctor_parser.add_argument(
        "--state-root",
        default=r"C:\r\dawnstrike-state",
    )
    scheduler_doctor_parser.add_argument("--print", action="store_true", dest="print_result")

    dashboard_doctor_parser = subparsers.add_parser(
        "dashboard-doctor", help="Check canonical public dashboard publication state"
    )
    dashboard_doctor_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    dashboard_doctor_parser.add_argument("--root", default=".")
    dashboard_doctor_parser.add_argument("--print", action="store_true", dest="print_result")

    opportunity_research = subparsers.add_parser(
        "opportunity-research",
        help="Run disabled-by-default research over retained local evidence only",
    )
    opportunity_research.add_argument("--enable-research", action="store_true")
    opportunity_research.add_argument(
        "--mode",
        choices=[item.value for item in OpportunityResearchMode],
        default=OpportunityResearchMode.CURRENT.value,
    )
    opportunity_research.add_argument("--data-truth-root", default=None)
    opportunity_research.add_argument("--snapshot-id", default=None)
    opportunity_research.add_argument("--database-path", default=None)
    opportunity_research.add_argument("--decision-at", default=None)
    opportunity_research.add_argument("--recorded-at", default=None)
    opportunity_research.add_argument("--universe-evidence", default=None)
    opportunity_research.add_argument("--catalyst-database", default=None)
    opportunity_research.add_argument("--alphaops-v5-candidates", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    try:
        if args.command == "scan":
            return _run_scan(args)
        if args.command == "opportunity-research":
            return _run_opportunity_research(args)
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
        if args.command == "alpha-alert-replay":
            return _run_alpha_alert_replay(args)
        if args.command == "alpha-capture-outcomes":
            return _run_alpha_capture_outcomes(args)
        if args.command == "alpha-eod-gate":
            return _run_alpha_eod_gate(args)
        if args.command == "alpha-paper-reconcile":
            return _run_alpha_paper_reconcile(args)
        if args.command == "alpha-learn":
            return _run_alpha_learn(args)
        if args.command == "alpha-v6-learn":
            return _run_alpha_v6_learn(args)
        if args.command == "alpha-v6-daily-monitor":
            return _run_alpha_v6_daily_monitor(args)
        if args.command == "strategy-learning-daily":
            return _run_strategy_learning_daily(args)
        if args.command == "managed-learning-queue":
            return _run_managed_learning_queue(args)
        if args.command == "strategy-challenger-backtest":
            return _run_strategy_challenger_backtest(args)
        if args.command == "strategy-challenger-evaluate-weekly":
            return _run_strategy_challenger_weekly(args)
        if args.command == "alpha-v6-train-weekly":
            return _run_alpha_v6_train_weekly(args)
        if args.command == "alpha-v6-register-experiment":
            return _run_alpha_v6_register_experiment(args)
        if args.command == "alpha-v6-evaluate-holdout":
            return _run_alpha_v6_evaluate_holdout(args)
        if args.command == "alpha-v6-attribution":
            return _run_alpha_v6_attribution(args)
        if args.command == "alpha-v6-research-packet":
            return _run_alpha_v6_research_packet(args)
        if args.command == "alpha-v6-build-universe":
            return _run_alpha_v6_build_universe(args)
        if args.command == "alpha-v6-register-universe":
            return _run_alpha_v6_register_universe(args)
        if args.command == "alpha-v6-preview-universe":
            return _run_alpha_v6_preview_universe(args)
        if args.command == "alpha-v6-restore-universe":
            return _run_alpha_v6_restore_universe(args)
        if args.command == "daily-heartbeat":
            return _run_daily_heartbeat(args)
        if args.command == "daily-orchestrator-status":
            return _run_daily_orchestrator_status(args)
        if args.command == "account-session-report":
            return _run_account_session_report(args)
        if args.command == "account-session-reconcile":
            return _run_account_session_reconcile(args)
        if args.command == "alpha-status":
            return _run_alpha_status(args)
        if args.command == "alpha-doctor":
            return _run_alpha_doctor(args)
        if args.command == "alpha-report":
            return _run_alpha_report(args)
        if args.command == "alpha-attribution":
            return _run_alpha_attribution(args)
        if args.command == "scenario-doctor":
            return _run_scenario_doctor(args)
        if args.command == "indeterminate-research":
            return _run_indeterminate_research(args)
        if args.command == "scenario-cycle":
            return _run_scenario_cycle(args)
        if args.command == "scenario-monitor":
            return _run_scenario_cycle(args)
        if args.command == "scenario-replay":
            return _run_scenario_replay(args)
        if args.command == "scenario-finalize":
            return _run_scenario_finalize(args)
        if args.command == "scenario-close":
            return _run_scenario_close(args)
        if args.command == "scenario-report":
            return _run_scenario_report(args)
        if args.command == "outcome-gap":
            return _run_outcome_gap(args)
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
        if args.command == "monitor-gap":
            return _run_monitor_gap(args)
        if args.command == "monitor-open":
            return _run_monitor_open(args)
        if args.command == "notify-test":
            return _run_notify_test(args)
        if args.command == "performance-report":
            return _run_performance_report(args)
        if args.command == "performance-reconcile":
            return _run_canonical_performance_reconcile(args)
        if args.command == "ingest-minute-bars":
            return _run_ingest_minute_bars(args)
        if args.command == "price-observe":
            return _run_price_observe(args)
        if args.command == "trade-watch":
            return _run_trade_watch(args)
        if args.command == "trade-watch-loop":
            return _run_trade_watch_loop(args)
        if args.command == "backfill-snapshots":
            return _run_backfill_snapshots(args)
        if args.command == "tune-strategy":
            return _run_tune_strategy(args)
        if args.command == "scheduler":
            return _run_scheduler(args)
        if args.command == "probability-doctor":
            return _run_release_doctor(probability_doctor(args.db_path))
        if args.command == "scheduler-doctor":
            return _run_release_doctor(
                scheduler_doctor(args.root, state_root=args.state_root),
                require_local_verification=True,
            )
        if args.command == "dashboard-doctor":
            return _run_release_doctor(dashboard_doctor(args.db_path, args.root))
        parser.error("Unknown command")
        return 2
    except (
        ConfigError,
        DataProviderError,
        SnapshotValidationError,
        StorageError,
        StrategyChallengerEvidenceError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except IntradayScannerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run_opportunity_research(args: argparse.Namespace) -> int:
    try:
        decision_at = _optional_iso_datetime(args.decision_at)
        recorded_at = _optional_iso_datetime(args.recorded_at)
    except ValueError:
        decision_at = None
        recorded_at = None
    report = run_local_opportunity_research(
        enabled=bool(args.enable_research),
        mode=OpportunityResearchMode(args.mode),
        data_truth_root=Path(args.data_truth_root) if args.data_truth_root else None,
        snapshot_id=args.snapshot_id,
        database_path=Path(args.database_path) if args.database_path else None,
        decision_at=decision_at,
        recorded_at=recorded_at,
        universe_evidence_path=(Path(args.universe_evidence) if args.universe_evidence else None),
        catalyst_database_path=(Path(args.catalyst_database) if args.catalyst_database else None),
        alphaops_v5_candidates_path=(
            Path(args.alphaops_v5_candidates) if args.alphaops_v5_candidates else None
        ),
    )
    print(json.dumps(report.deterministic_payload(), sort_keys=True))
    return 1 if report.status is LocalResearchStatus.FAILED else 0


def _optional_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


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
    store = SQLiteScanStore(config.database_path) if args.persist else None
    provider = CsvSnapshotProvider(args.snapshot)
    if store is not None:
        record_health_check(
            store,
            provider="manual_shadow_csv",
            check=provider.validate_credentials,
        )
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
        assert store is not None
        store.persist_scan_result(result)
    paths = write_scan_outputs(result, config.output_dir)
    _print_scan_done(paths, result.summary(), args.print_rows)
    return 0


def _run_import_manual_outcomes(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    result = import_manual_outcomes(
        input_path=args.input,
        store=SQLiteScanStore(config.database_path, read_only=not args.persist),
        persist=args.persist,
        replace=args.replace,
    )
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}, indent=2))
    return 0


def _run_audit_manual_outcomes(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    result = audit_manual_outcomes(
        store=SQLiteScanStore(config.database_path, read_only=not args.persist),
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
        store=SQLiteScanStore(config.database_path, read_only=not args.persist),
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
        store=SQLiteScanStore(config.database_path, read_only=not args.persist),
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
    store = SQLiteScanStore(config.database_path) if args.persist and args.db_path else None
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
        core_universe_manifest=args.core_universe_manifest,
        market_date=args.market_date,
        as_of=_optional_iso_datetime(args.as_of),
        paper_ops_root=getattr(args, "paper_ops_root", None),
        code_sha=getattr(args, "code_sha", None),
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
        core_universe_manifest=args.core_universe_manifest,
        market_date=args.market_date,
        as_of=_optional_iso_datetime(args.as_of),
        paper_ops_root=getattr(args, "paper_ops_root", None),
        code_sha=getattr(args, "code_sha", None),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_monitor(args: argparse.Namespace) -> int:
    result = alpha_monitor(
        db_path=args.db_path,
        notify=args.notify,
        dry_run=args.dry_run,
        market_date=getattr(args, "market_date", None),
        observation_bundle_path=getattr(args, "observation_bundle", None),
        cycle_id=getattr(args, "cycle_id", None),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_outcomes(args: argparse.Namespace) -> int:
    result = alpha_outcomes(db_path=args.db_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_capture_outcomes(args: argparse.Namespace) -> int:
    result = capture_sourced_alpha_outcomes(
        db_path=args.db_path,
        market_date=args.market_date,
        requested_at=args.at,
        out_dir=args.out_dir,
        persist=args.persist,
        replace=args.replace,
        max_close_staleness_seconds=args.max_close_staleness_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") in {"session_incomplete", "partial"} else 0


def _run_alpha_paper_reconcile(args: argparse.Namespace) -> int:
    result = reconcile_alpha_paper_trades(
        db_path=args.db_path,
        market_date=args.market_date,
        out_dir=args.out_dir,
        persist=args.persist,
        notional_per_trade=args.notional_per_trade,
        fee_bps=args.fee_bps,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "complete" else 1


def _run_alpha_learn(args: argparse.Namespace) -> int:
    result = alpha_learn(db_path=args.db_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "complete" else 2


def _run_alpha_v6_learn(args: argparse.Namespace) -> int:
    store = SQLiteScanStore(args.db_path)
    result = run_alpha_v6_learning(store, code_sha=args.code_sha)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_v6_daily_monitor(args: argparse.Namespace) -> int:
    result = run_alpha_v6_daily_monitor(
        SQLiteScanStore(args.db_path),
        market_date=args.market_date,
        reference_window=_read_v6_window(getattr(args, "reference_window", None)),
        recent_window=_read_v6_window(getattr(args, "recent_window", None)),
        attempt_id=getattr(args, "attempt_id", None),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _read_v6_window(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        candidate = Path(value)
        raw = candidate.read_text(encoding="utf-8") if candidate.is_file() else value
    except OSError:
        raw = value
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SnapshotValidationError("V6 drift window must be valid JSON or a JSON file") from exc
    if not isinstance(parsed, dict):
        raise SnapshotValidationError("V6 drift window must be a JSON object")
    return parsed


def _run_strategy_learning_daily(args: argparse.Namespace) -> int:
    """Run daily learning with a reservation-before-read source lock."""

    connection: sqlite3.Connection | None = None
    if args.db_path:
        path = Path(args.db_path).resolve()
        if not path.is_file():
            raise SnapshotValidationError(f"strategy-learning database is missing: {path}")
        try:
            connection = sqlite3.connect(path)
            # BEGIN IMMEDIATE takes a reserved writer-exclusion lock without
            # changing rows.  This is deliberately not a mode=ro connection:
            # SQLite cannot acquire the reservation from a read-only URI.  The
            # transaction is switched to query_only before any source SELECT.
            # No SELECT occurs until the phase-1 reservation is durably
            # installed, so an insert/backdate cannot slip into the cohort.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA query_only = ON")
            args._learning_db_connection = connection
            return _run_strategy_learning_daily_unlocked(args)
        except sqlite3.Error as exc:
            raise SnapshotValidationError(
                "strategy-learning database cannot establish a non-mutating "
                f"reserved snapshot: {path}"
            ) from exc
        except ValueError as exc:
            raise SnapshotValidationError(f"strategy-learning validation failed: {exc}") from exc
        except OSError as exc:
            raise SnapshotValidationError(
                f"strategy-learning source cannot be read: {exc}"
            ) from exc
        finally:
            args.__dict__.pop("_learning_db_connection", None)
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()
    try:
        return _run_strategy_learning_daily_unlocked(args)
    except ValueError as exc:
        raise SnapshotValidationError(f"strategy-learning validation failed: {exc}") from exc
    except OSError as exc:
        raise SnapshotValidationError(f"strategy-learning source cannot be read: {exc}") from exc


def _run_managed_learning_queue(args: argparse.Namespace) -> int:
    calendar_path = Path(args.calendar)
    if calendar_path.is_symlink() or not calendar_path.is_file():
        raise SnapshotValidationError("managed-learning calendar must be a regular file")
    try:
        calendar = json.loads(calendar_path.read_text(encoding="utf-8"))
        result = produce_managed_learning_queue(
            args.approved_root,
            args.out_root,
            calendar=calendar,
            as_of_market_date=args.as_of_market_date,
            policy=LearningQueuePolicy(),
        )
    except (OSError, json.JSONDecodeError, LearningQueueValidationError) as exc:
        raise SnapshotValidationError(f"managed-learning queue failed closed: {exc}") from exc
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result.get("status") == "COMPLETE" else 2


def _run_strategy_learning_daily_unlocked(args: argparse.Namespace) -> int:
    analyzer: StrategyEvidenceAnalyzer | None
    input_hash_sha256: str | None
    decision_receipts: Sequence[Mapping[str, Any]] | None
    v6_decisions: Sequence[Mapping[str, Any]] | None
    no_evidence_receipts: Sequence[Mapping[str, Any]] | None
    research_episode_outcomes: Sequence[Mapping[str, Any]] | None
    if args.paper_ops_root and not args.db_path:
        raise SnapshotValidationError("--paper-ops-root requires --db-path")
    # Phase 1 is deliberately before the first database, PaperOps, or
    # evidence-file read.  Retries validate this reservation and retain its
    # original cutoff/source identity.
    reservation = _reserve_learning_invocation(args)
    if reservation.get("reservation_phase") in {1, 2}:
        # Preserve the original requested boundary if a process crashed after
        # reservation but before acquisition completed.
        args.cutoff = str(reservation.get("cutoff") or args.cutoff)
        args.source_identity = str(reservation.get("source_identity") or args.source_identity)
        args.source_hash_sha256 = str(
            reservation.get("source_hash_sha256") or args.source_hash_sha256 or ""
        )
    if args.evidence_file or args.db_path:
        snapshot = _load_or_freeze_strategy_learning_evidence(args)
        (
            analyzer,
            input_hash_sha256,
            decision_receipts,
            v6_decisions,
            no_evidence_receipts,
            research_episode_outcomes,
        ) = _restore_strategy_learning_evidence(snapshot)
        frozen_cutoff = str(snapshot["cutoff"])
        frozen_source_identity = str(snapshot["source_identity"])
        frozen_source_hash = str(snapshot["source_hash_sha256"])
    else:
        analyzer = None
        input_hash_sha256 = None
        decision_receipts = None
        v6_decisions = None
        no_evidence_receipts = None
        research_episode_outcomes = None
        frozen_cutoff = str(args.cutoff)
        frozen_source_identity = str(args.source_identity)
        frozen_source_hash = args.source_hash_sha256
    if not input_hash_sha256 and reservation.get("input_hash_sha256"):
        input_hash_sha256 = str(reservation["input_hash_sha256"])
    result = run_daily_strategy_learning(
        market_date=args.market_date,
        cutoff=frozen_cutoff,
        source_identity=frozen_source_identity,
        source_hash_sha256=frozen_source_hash,
        code_sha=args.code_sha,
        out_dir=args.out_dir,
        input_hash_sha256=input_hash_sha256,
        analyzer=analyzer,
        decision_receipts=decision_receipts,
        v6_decisions=v6_decisions,
        no_evidence_receipts=no_evidence_receipts,
        research_episode_outcomes=research_episode_outcomes,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "complete" else 1


def _hash_strategy_learning_inputs(
    *,
    database_rows: Sequence[Mapping[str, Any]] | None = None,
    paper_ops_rows: Sequence[Mapping[str, Any]] | None = None,
    paper_ops_root: Path | None = None,
    evidence_payload: Mapping[str, Any] | None = None,
    decision_receipts: Sequence[Mapping[str, Any]] | None = None,
    v6_decisions: Sequence[Mapping[str, Any]] | None = None,
    research_episode_outcomes: Sequence[Mapping[str, Any]] | None = None,
) -> str | None:
    """Bind reuse to the exact evidence objects consumed by the analyzer.

    Hashing the in-memory rows avoids a database/WAL time-of-check/time-of-use
    gap and excludes mutable storage layout from the immutable receipt.  The
    read-only PaperOps materializer hash is independently recomputed from the
    requested root and compared with the adapter declaration, so a caller
    cannot inject a self-selected hash and widen the signed boundary.
    """

    parts: list[tuple[str, bytes]] = []
    if database_rows is not None:
        parts.append(("portfolio_performance_rows", _canonical_input_bytes(database_rows)))
    if paper_ops_rows is not None:
        parts.append(("paper_ops_lifecycle_rows", _canonical_input_bytes(paper_ops_rows)))
    if paper_ops_root is not None:
        from intraday_scanner.v2.paper_ops.trade_blotter import (
            hash_trade_blotter_readonly_inputs,
        )

        paper_hash = str(getattr(paper_ops_rows, "read_only_input_hash_sha256", "") or "")
        actual_paper_hash = hash_trade_blotter_readonly_inputs(paper_ops_root)
        if paper_hash and paper_hash != actual_paper_hash:
            raise SnapshotValidationError(
                "PaperOps read-only materializer hash conflicts with immutable input bytes"
            )
        paper_hash = actual_paper_hash
        parts.append(("paper_ops_materializer_inputs", paper_hash.encode("ascii")))
    if evidence_payload is not None:
        parts.append(("evidence_payload", _canonical_input_bytes(evidence_payload)))
    if decision_receipts is not None:
        parts.append(("strategy_decision_receipts", _canonical_input_bytes(decision_receipts)))
    if v6_decisions is not None:
        parts.append(("alpha_v6_decisions", _canonical_input_bytes(v6_decisions)))
    if research_episode_outcomes is not None:
        parts.append(
            (
                "research_episode_outcomes",
                _canonical_input_bytes(research_episode_outcomes),
            )
        )
    if not parts:
        return None
    digest = hashlib.sha256()
    for label, payload in sorted(parts, key=lambda item: item[0]):
        encoded_label = label.encode("utf-8")
        digest.update(len(encoded_label).to_bytes(8, "big"))
        digest.update(encoded_label)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _canonical_input_bytes(value: Any) -> bytes:
    if (
        hasattr(value, "invalid_identities")
        or hasattr(value, "invalid_reasons")
        or hasattr(value, "expected_selection_count")
        or hasattr(value, "expected_contributor_count")
    ):
        identity: dict[str, Any] = {
            "accepted": list(value),
            "invalid_identities": list(getattr(value, "invalid_identities", ())),
            "invalid_reasons": dict(getattr(value, "invalid_reasons", {})),
        }
        if hasattr(value, "expected_selection_count"):
            identity["expected_selection_count"] = int(
                getattr(value, "expected_selection_count", 0) or 0
            )
        if hasattr(value, "expected_contributor_count"):
            identity["expected_contributor_count"] = int(
                getattr(value, "expected_contributor_count", 0) or 0
            )
        value = identity
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


_STRATEGY_LEARNING_EVIDENCE_SNAPSHOT_SCHEMA = "dawnstrike.strategy_learning_evidence_snapshot.v1"
_STRATEGY_LEARNING_ACQUISITION_MANIFEST_SCHEMA = (
    "dawnstrike.strategy_learning_acquisition_manifest.v1"
)
_STRATEGY_LEARNING_COMMIT_MANIFEST_SCHEMA = "dawnstrike.strategy_learning_commit_manifest.v1"
_LEARNING_MANIFEST_DOMAIN = b"dawnstrike/strategy-learning/acquisition-manifest/v1\0"
_LEARNING_RESERVATION_DOMAIN = b"dawnstrike/strategy-learning/invocation-reservation/v1\0"
_LEARNING_BOUNDARY_DOMAIN = b"dawnstrike/strategy-learning/source-boundary/v1\0"
_LEARNING_MANIFEST_KEY_ENV = "DAWNSTRIKE_DAILY_LEARNING_HMAC_KEY"
_LEARNING_MANIFEST_KEY_FILE_ENV = "DAWNSTRIKE_DAILY_LEARNING_HMAC_KEY_FILE"


class _AuthenticatedLearningSnapshot(dict[str, Any]):
    """Snapshot accepted only after its external acquisition manifest verifies."""

    _TOKEN = object()

    def __init__(self, payload: Mapping[str, Any], *, token: object) -> None:
        if token is not self._TOKEN:
            raise TypeError("learning snapshot authentication is private")
        super().__init__(payload)


def _learning_root(args: argparse.Namespace) -> Path:
    return Path(args.out_dir) / str(args.market_date)


def _learning_manifest_key(root: Path) -> bytes:
    configured = os.environ.get(_LEARNING_MANIFEST_KEY_ENV) or os.environ.get(
        "DAWNSTRIKE_FORWARD_GAP_HMAC_KEY"
    )
    if configured:
        key = configured.encode("utf-8")
        if len(key) < 32:
            raise SnapshotValidationError("daily-learning HMAC key is too short")
        return key
    configured_path = os.environ.get(_LEARNING_MANIFEST_KEY_FILE_ENV)
    if configured_path:
        key_path = Path(configured_path).expanduser().resolve()
        try:
            key_path.relative_to(root.parent.resolve())
        except ValueError:
            pass
        else:
            raise SnapshotValidationError(
                "daily-learning HMAC key must be outside the complete output tree"
            )
        if not key_path.is_file():
            raise SnapshotValidationError("daily-learning HMAC key file is missing")
        key = key_path.read_bytes()
        if len(key) < 32:
            raise SnapshotValidationError("daily-learning HMAC key is too short")
        return key
    else:
        raise SnapshotValidationError(
            f"{_LEARNING_MANIFEST_KEY_ENV}, DAWNSTRIKE_FORWARD_GAP_HMAC_KEY, or "
            f"{_LEARNING_MANIFEST_KEY_FILE_ENV} is required"
        )


def _signed_learning_manifest(body: Mapping[str, Any], *, key: bytes) -> dict[str, Any]:
    canonical = _canonical_input_bytes(body)
    domain = (
        _LEARNING_BOUNDARY_DOMAIN
        if body.get("boundary_phase") == 1
        else _LEARNING_RESERVATION_DOMAIN
        if body.get("reservation_phase") == 1
        else _LEARNING_MANIFEST_DOMAIN
    )
    signature = hmac.new(key, domain + canonical, hashlib.sha256).hexdigest()
    return {**dict(body), "signature_hmac_sha256": signature}


def _verify_learning_manifest(payload: Any, *, key: bytes) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise SnapshotValidationError("daily-learning acquisition manifest is malformed")
    signature = str(payload.get("signature_hmac_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise SnapshotValidationError("daily-learning acquisition manifest signature is missing")
    body = {
        key: value
        for key, value in payload.items()
        if key != "signature_hmac_sha256"
        and not (payload.get("reservation_phase") == 1 and key == "reservation_sha256")
        and not (payload.get("boundary_phase") == 1 and key == "boundary_sha256")
    }
    domain = (
        _LEARNING_BOUNDARY_DOMAIN
        if body.get("boundary_phase") == 1
        else _LEARNING_RESERVATION_DOMAIN
        if body.get("reservation_phase") == 1
        else _LEARNING_MANIFEST_DOMAIN
    )
    expected = hmac.new(key, domain + _canonical_input_bytes(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise SnapshotValidationError("daily-learning acquisition manifest signature mismatch")
    return dict(body)


def _atomic_install_bytes(path: Path, encoded: bytes) -> bool:
    """Install bytes exactly once, durably, without replacing a winner."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SnapshotValidationError(f"daily-learning immutable path is a symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            return False
        except FileExistsError:
            if path.is_symlink():
                raise SnapshotValidationError(
                    f"daily-learning immutable path is a symlink: {path}"
                ) from None
            if path.read_bytes() != encoded:
                raise SnapshotValidationError(
                    f"immutable daily-learning file conflict: {path}"
                ) from None
            return True
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_replace_bytes(path: Path, encoded: bytes) -> None:
    """Durably replace one authenticated mutable-phase file.

    Daily-learning reservation files are write-once during phase 1.  After
    acquisition, the same invocation is extended with the aggregate input
    hash so the final artifacts can bind to the exact cohort.  This helper is
    only used for that signed phase-2 extension; all snapshot/manifest/output
    artifacts remain install-once.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SnapshotValidationError(f"daily-learning immutable path is a symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise SnapshotValidationError(f"daily-learning immutable path is a symlink: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _learning_invocation_lock(args: argparse.Namespace):
    """Serialize reservation phase transitions for one output/date pair."""

    root = _learning_root(args)
    if root.is_symlink() or root.parent.is_symlink():
        raise SnapshotValidationError(f"daily-learning output root is a symlink: {root}")
    lock_path = root / ".daily_learning_invocation.lock"
    if lock_path.is_symlink():
        raise SnapshotValidationError(f"daily-learning invocation lock is a symlink: {lock_path}")
    from intraday_scanner.v2.paper_ops.storage import exclusive_file_lock

    return exclusive_file_lock(lock_path)


def _read_stable_json(path: Path) -> tuple[Any, dict[str, Any]]:
    """Read one source file and prove it did not change during acquisition."""

    if path.is_symlink() or path.parent.is_symlink():
        raise SnapshotValidationError(f"daily-learning evidence source is a symlink: {path}")
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise SnapshotValidationError(
            f"daily-learning evidence source is unreadable: {path}"
        ) from exc
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise SnapshotValidationError(f"daily-learning evidence source changed during read: {path}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(
            f"daily-learning evidence source is invalid JSON: {path}"
        ) from exc
    return payload, {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "mtime_ns": int(after.st_mtime_ns),
    }


def _parse_learning_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


class _FrozenLearningBatch(tuple):
    """Restored immutable input batch with retained rejection diagnostics."""

    invalid_reasons: dict[str, int]
    invalid_count: int
    invalid_identities: tuple[str, ...]
    expected_selection_count: int
    expected_contributor_count: int

    def __new__(
        cls,
        values: Sequence[Mapping[str, Any]],
        *,
        invalid_reasons: Mapping[str, Any],
        invalid_identities: Sequence[Any],
        expected_selection_count: int = 0,
        expected_contributor_count: int = 0,
    ):
        result = super().__new__(cls, values)
        result.invalid_reasons = {
            str(key): int(value) for key, value in sorted(invalid_reasons.items())
        }
        result.invalid_count = sum(result.invalid_reasons.values())
        result.invalid_identities = tuple(str(value) for value in invalid_identities)
        result.expected_selection_count = int(expected_selection_count)
        result.expected_contributor_count = int(expected_contributor_count)
        return result


def _strategy_learning_snapshot_request(args: argparse.Namespace) -> dict[str, Any]:
    mode = "evidence_file" if args.evidence_file else "database"
    return {
        "mode": mode,
        "database_path": str(Path(args.db_path).resolve()) if args.db_path else None,
        "evidence_file": (str(Path(args.evidence_file).resolve()) if args.evidence_file else None),
        "paper_ops_root": (
            str(Path(args.paper_ops_root).resolve()) if args.paper_ops_root else None
        ),
    }


def _strategy_learning_source_hash(args: argparse.Namespace) -> str:
    source_hash = str(
        args.source_hash_sha256
        or hashlib.sha256(str(args.source_identity).encode("utf-8")).hexdigest()
    )
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise SnapshotValidationError(
            "daily-learning source hash must be a canonical lowercase SHA-256 hex digest"
        )
    return source_hash


def _strategy_learning_snapshot_path(args: argparse.Namespace) -> Path:
    return Path(args.out_dir) / str(args.market_date) / "daily_learning_evidence_snapshot.json"


def _strategy_learning_reservation_path(args: argparse.Namespace) -> Path:
    return _learning_root(args) / "daily_learning_invocation.json"


def _strategy_learning_source_boundary_path(args: argparse.Namespace) -> Path:
    return _learning_root(args) / "daily_learning_source_boundary.json"


# Keep the table-name allowlist and every identifier-bearing statement as
# immutable literals. Besides making the boundary explicit, this prevents a
# future refactor from turning this read-only snapshot into identifier SQL.
_DATABASE_SOURCE_BOUNDARY_QUERY_CONTRACT: tuple[
    tuple[str, str, str, str], ...
] = (
    (
        "portfolio_performance_rows",
        "PRAGMA table_info(portfolio_performance_rows)",
        "SELECT rowid, * FROM portfolio_performance_rows ORDER BY rowid",
        "SELECT * FROM portfolio_performance_rows",
    ),
    (
        "strategy_decision_receipts",
        "PRAGMA table_info(strategy_decision_receipts)",
        "SELECT rowid, * FROM strategy_decision_receipts ORDER BY rowid",
        "SELECT * FROM strategy_decision_receipts",
    ),
    (
        "alpha_v6_decisions",
        "PRAGMA table_info(alpha_v6_decisions)",
        "SELECT rowid, * FROM alpha_v6_decisions ORDER BY rowid",
        "SELECT * FROM alpha_v6_decisions",
    ),
)


def _database_source_boundary(
    connection: sqlite3.Connection,
    database_path: Path,
) -> dict[str, Any]:
    """Capture immutable row bounds and row bytes under the held DB lock."""

    connection.row_factory = sqlite3.Row
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    result: dict[str, Any] = {}
    for table, schema_query, ordered_rows_query, fallback_rows_query in (
        _DATABASE_SOURCE_BOUNDARY_QUERY_CONTRACT
    ):
        if table not in tables:
            result[table] = {
                "exists": False,
                "row_count": 0,
                "max_rowid": None,
                "schema_sha256": None,
                "rows_sha256": None,
            }
            continue
        schema_rows = [
            dict(row) for row in connection.execute(schema_query).fetchall()
        ]
        try:
            rows = connection.execute(ordered_rows_query).fetchall()
            max_rowid = int(rows[-1][0]) if rows else None
        except sqlite3.DatabaseError:
            rows = connection.execute(fallback_rows_query).fetchall()
            max_rowid = None
        row_payload = [dict(row) for row in rows]
        result[table] = {
            "exists": True,
            "row_count": len(row_payload),
            "max_rowid": max_rowid,
            "schema_sha256": hashlib.sha256(_canonical_input_bytes(schema_rows)).hexdigest(),
            "rows_sha256": hashlib.sha256(_canonical_input_bytes(row_payload)).hexdigest(),
        }
    return {"database_path": str(database_path.resolve()), "tables": result}


def _paper_ops_source_boundary(root: Path) -> dict[str, Any]:
    from intraday_scanner.v2.paper_ops.trade_blotter import (
        describe_trade_blotter_readonly_inputs,
    )

    return describe_trade_blotter_readonly_inputs(root)


def _source_boundary_for_invocation(args: argparse.Namespace) -> dict[str, Any]:
    """Read source metadata only after the signed phase-1 reservation exists."""

    boundary: dict[str, Any] = {
        "mode": "database" if args.db_path else "evidence_file",
    }
    connection = getattr(args, "_learning_db_connection", None)
    if args.db_path:
        if connection is None:
            connection = sqlite3.connect(Path(args.db_path).resolve())
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA query_only = ON")
            owns_connection = True
        else:
            owns_connection = False
        try:
            boundary["database"] = _database_source_boundary(connection, Path(args.db_path))
        finally:
            if owns_connection:
                connection.rollback()
                connection.close()
    if args.paper_ops_root:
        boundary["paper_ops"] = _paper_ops_source_boundary(Path(args.paper_ops_root))
    if args.evidence_file:
        _payload, generation = _read_stable_json(Path(args.evidence_file))
        boundary["evidence_file"] = generation
    return boundary


def _verify_source_boundary_current(args: argparse.Namespace, boundary: Mapping[str, Any]) -> None:
    """Reject any source drift before an orphaned phase-1 run can widen."""

    expected_db = boundary.get("database")
    if expected_db is not None:
        connection = getattr(args, "_learning_db_connection", None)
        if connection is None:
            raise SnapshotValidationError("daily-learning source-boundary database lock is missing")
        actual_db = _database_source_boundary(connection, Path(args.db_path))
        if actual_db != expected_db:
            raise SnapshotValidationError("daily-learning phase-1 database source boundary changed")
    expected_paper = boundary.get("paper_ops")
    if expected_paper is not None:
        actual_paper = _paper_ops_source_boundary(Path(args.paper_ops_root))
        expected_files = (
            expected_paper.get("files") if isinstance(expected_paper, Mapping) else None
        )
        actual_files = actual_paper.get("files") if isinstance(actual_paper, Mapping) else None
        if expected_files != actual_files:
            raise SnapshotValidationError("daily-learning phase-1 PaperOps source boundary changed")
    expected_evidence = boundary.get("evidence_file")
    if expected_evidence is not None:
        _payload, actual_evidence = _read_stable_json(Path(args.evidence_file))
        if actual_evidence != expected_evidence:
            raise SnapshotValidationError(
                "daily-learning phase-1 evidence-file source boundary changed"
            )


def _strategy_learning_reservation_body(
    args: argparse.Namespace, *, input_hash_sha256: str | None = None
) -> dict[str, Any]:
    return {
        "schema_version": _STRATEGY_LEARNING_EVIDENCE_SNAPSHOT_SCHEMA,
        "reservation_phase": 1,
        "reserved_at": datetime.now(UTC).isoformat(),
        "market_date": str(args.market_date),
        "cutoff": str(args.cutoff),
        "source_identity": str(args.source_identity),
        "source_hash_sha256": _strategy_learning_source_hash(args),
        "code_sha": str(args.code_sha),
        "request": _strategy_learning_snapshot_request(args),
        # The aggregate source hash is unknown until acquisition.  It is
        # bound once, after phase 2, and can never be changed on retry.
        "input_hash_sha256": str(input_hash_sha256 or ""),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _bind_learning_invocation_input_hash_unlocked(
    args: argparse.Namespace,
    input_hash_sha256: str,
) -> dict[str, Any]:
    """Durably extend the signed phase-1 reservation with the cohort hash."""

    if not re.fullmatch(r"[0-9a-f]{64}", str(input_hash_sha256)):
        raise SnapshotValidationError("daily-learning input hash is malformed")
    path = _strategy_learning_reservation_path(args)
    if not path.is_file() or path.is_symlink():
        raise SnapshotValidationError("daily-learning invocation reservation is missing")
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(
            "daily-learning invocation reservation is unreadable"
        ) from exc
    if not isinstance(stored, Mapping):
        raise SnapshotValidationError("daily-learning invocation reservation is malformed")
    stored_hash = str(stored.get("reservation_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", stored_hash):
        raise SnapshotValidationError("daily-learning invocation reservation hash is missing")
    stored_body = _verify_learning_manifest(stored, key=_learning_manifest_key(path.parent))
    if stored_hash != hashlib.sha256(_canonical_input_bytes(stored_body)).hexdigest():
        raise SnapshotValidationError("daily-learning invocation reservation hash mismatch")
    current_input_hash = str(stored_body.get("input_hash_sha256") or "")
    if current_input_hash:
        if current_input_hash != input_hash_sha256:
            raise SnapshotValidationError(
                "daily-learning invocation identity conflict: input_hash_sha256"
            )
        return {**stored_body, "reservation_sha256": stored_hash}

    # Preserve the original reservation hash as the source-boundary identity;
    # the phase-2 replacement gets a new content hash and HMAC while retaining
    # that immutable phase-1 link.  This leaves crash recovery unambiguous if
    # the process stops between this update and snapshot publication.
    updated_body = {
        **stored_body,
        "phase1_reservation_sha256": stored_hash,
        "input_hash_sha256": input_hash_sha256,
    }
    updated = {
        **_signed_learning_manifest(updated_body, key=_learning_manifest_key(path.parent)),
        "reservation_sha256": hashlib.sha256(_canonical_input_bytes(updated_body)).hexdigest(),
    }
    encoded = (
        json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    _atomic_replace_bytes(path, encoded)
    return {**updated_body, "reservation_sha256": updated["reservation_sha256"]}


def _bind_learning_invocation_input_hash(
    args: argparse.Namespace,
    input_hash_sha256: str,
) -> dict[str, Any]:
    """Bind the first aggregate input hash without a lost update race."""

    with _learning_invocation_lock(args):
        # Re-read while holding the stable lock file.  If another process won
        # phase 2, the unlocked implementation observes its hash and either
        # accepts the same winner or rejects a conflicting cohort.
        return _bind_learning_invocation_input_hash_unlocked(args, input_hash_sha256)


def _reserve_learning_invocation_unlocked(
    args: argparse.Namespace, *, input_hash_sha256: str | None = None
) -> dict[str, Any]:
    """Atomically reserve the requested invocation before reading any source."""

    path = _strategy_learning_reservation_path(args)
    if path.is_symlink():
        raise SnapshotValidationError("daily-learning invocation reservation is a symlink")
    body = _strategy_learning_reservation_body(args, input_hash_sha256=input_hash_sha256)
    # A retry must never rewrite phase 1 (including its reserved_at boundary).
    # Only an absent path may be installed; an existing immutable reservation
    # is read and authenticated below.  The atomic link still closes the
    # concurrent first-writer race when two processes observe an absent path.
    installed_phase1 = not path.exists()
    if installed_phase1:
        payload = {
            **_signed_learning_manifest(body, key=_learning_manifest_key(path.parent)),
            "reservation_sha256": hashlib.sha256(_canonical_input_bytes(body)).hexdigest(),
        }
        try:
            _atomic_install_bytes(
                path,
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8"),
            )
        except SnapshotValidationError:
            # Another invocation may have won the atomic first-writer race
            # with a different reserved_at.  Authenticate and compare that
            # winner below; conflicting identity is rejected there.
            if not path.is_file():
                raise
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(
            "daily-learning invocation reservation is unreadable"
        ) from exc
    if not isinstance(stored, dict):
        raise SnapshotValidationError("daily-learning invocation reservation is malformed")
    stored_hash = stored.get("reservation_sha256")
    stored_body = {
        key: value
        for key, value in stored.items()
        if key not in {"reservation_sha256", "signature_hmac_sha256"}
    }
    if stored_hash != hashlib.sha256(_canonical_input_bytes(stored_body)).hexdigest():
        raise SnapshotValidationError("daily-learning invocation reservation hash mismatch")
    stored_body = _verify_learning_manifest(stored, key=_learning_manifest_key(path.parent))
    snapshot_complete = (
        _strategy_learning_snapshot_path(args).is_file()
        and _strategy_learning_snapshot_path(args)
        .with_name("daily_learning_acquisition_manifest.json")
        .is_file()
    )
    # Once the signed phase-2 snapshot and acquisition manifest are complete,
    # they are the authority for a retry.  A caller may re-run with a stale
    # scheduler argument (for example, a later wall-clock cutoff or a new
    # descriptive source label), but cannot change the request scope, market,
    # code, or research-only boundary.  _run_strategy_learning_daily_unlocked
    # replaces the mutable cutoff/source fields with the signed reservation
    # before the snapshot is consumed.
    retry_frozen_boundary = snapshot_complete
    for field in (
        "schema_version",
        "market_date",
        "cutoff",
        "source_identity",
        "source_hash_sha256",
        "code_sha",
        "request",
        "research_only",
        "broker_execution_enabled",
    ):
        if retry_frozen_boundary and field in {
            "cutoff",
            "source_identity",
            "source_hash_sha256",
        }:
            continue
        if stored.get(field) != body.get(field):
            raise SnapshotValidationError(f"daily-learning invocation identity conflict: {field}")
    requested_input_hash = str(body.get("input_hash_sha256") or "")
    stored_input_hash = str(stored.get("input_hash_sha256") or "")
    if requested_input_hash and stored_input_hash not in {"", requested_input_hash}:
        raise SnapshotValidationError(
            "daily-learning invocation identity conflict: input_hash_sha256"
        )
    reservation = {**stored_body, "reservation_sha256": stored["reservation_sha256"]}
    boundary_path = _strategy_learning_source_boundary_path(args)
    if boundary_path.is_symlink():
        raise SnapshotValidationError("daily-learning phase-1 source boundary is a symlink")
    if not boundary_path.exists():
        if not installed_phase1:
            raise SnapshotValidationError(
                "daily-learning phase-1 source boundary is missing after reservation"
            )
        boundary_body = {
            "schema_version": "dawnstrike.strategy_learning_source_boundary.v1",
            "boundary_phase": 1,
            "reservation_sha256": reservation["reservation_sha256"],
            "market_date": reservation["market_date"],
            "cutoff": reservation["cutoff"],
            "source_identity": reservation["source_identity"],
            "source_hash_sha256": reservation["source_hash_sha256"],
            "code_sha": reservation["code_sha"],
            "request": reservation["request"],
            "source_boundary": _source_boundary_for_invocation(args),
            "research_only": True,
            "broker_execution_enabled": False,
        }
        boundary_payload = {
            **_signed_learning_manifest(boundary_body, key=_learning_manifest_key(path.parent)),
            "boundary_sha256": hashlib.sha256(_canonical_input_bytes(boundary_body)).hexdigest(),
        }
        _atomic_install_bytes(
            boundary_path,
            (
                json.dumps(
                    boundary_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        )
    try:
        boundary_payload = json.loads(boundary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(
            "daily-learning phase-1 source boundary is unreadable"
        ) from exc
    boundary_hash = (
        boundary_payload.get("boundary_sha256") if isinstance(boundary_payload, Mapping) else None
    )
    boundary_body = _verify_learning_manifest(
        boundary_payload, key=_learning_manifest_key(path.parent)
    )
    if (
        boundary_body.get("schema_version") != "dawnstrike.strategy_learning_source_boundary.v1"
        or boundary_body.get("boundary_phase") != 1
        or boundary_body.get("reservation_sha256")
        != str(reservation.get("phase1_reservation_sha256") or reservation["reservation_sha256"])
        or boundary_hash != hashlib.sha256(_canonical_input_bytes(boundary_body)).hexdigest()
    ):
        raise SnapshotValidationError("daily-learning phase-1 source boundary binding mismatch")
    if not snapshot_complete:
        _verify_source_boundary_current(args, boundary_body.get("source_boundary") or {})
    return reservation


def _reserve_learning_invocation(
    args: argparse.Namespace, *, input_hash_sha256: str | None = None
) -> dict[str, Any]:
    """Reserve and validate one invocation while serializing phase changes."""

    with _learning_invocation_lock(args):
        return _reserve_learning_invocation_unlocked(args, input_hash_sha256=input_hash_sha256)


def _load_or_freeze_strategy_learning_evidence(
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Freeze the analyzer cohort before the daily-learning reservation.

    PaperOps has immutable append-only events but legacy events do not carry a
    trustworthy append timestamp. Re-reading a larger ledger on a retry can
    therefore never reproduce the first invocation exactly. This snapshot is
    the point-in-time boundary: subsequent attempts validate and consume its
    exact analyzer payload and authenticated receipt envelopes without touching
    the now-larger database or ledger.
    """

    path = _strategy_learning_snapshot_path(args)
    manifest_path = path.with_name("daily_learning_acquisition_manifest.json")
    reservation_path = _strategy_learning_reservation_path(args)
    if path.is_symlink() or manifest_path.is_symlink() or reservation_path.is_symlink():
        raise SnapshotValidationError("daily-learning frozen evidence boundary contains a symlink")
    if not reservation_path.is_file():
        raise SnapshotValidationError("daily-learning phase-1 invocation reservation is missing")
    if path.is_file() and manifest_path.is_file():
        return _read_strategy_learning_evidence_snapshot(path, args)
    # A crash may leave only one phase-2 file.  It is an orphan, not
    # authority: reacquire the exact phase-1 source boundary and atomically
    # complete the missing file, with immutable-install conflicts failing
    # closed if an orphan was altered.
    body = _acquire_strategy_learning_evidence(args)
    # SQLite rows are protected by the held transaction, while PaperOps and
    # evidence files are external mutable inputs. Re-check the signed
    # boundary after materialization and immediately before publishing phase
    # 2, so a file that changed between preflight and the read cannot be
    # frozen as if it belonged to the phase-1 cohort.
    _reserve_learning_invocation(args)
    _bind_learning_invocation_input_hash(args, str(body["input_hash_sha256"]))
    reservation_payload = json.loads(reservation_path.read_text(encoding="utf-8"))
    if not isinstance(reservation_payload, Mapping):
        raise SnapshotValidationError("daily-learning invocation reservation is malformed")
    reservation_hash = str(reservation_payload.get("reservation_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", reservation_hash):
        raise SnapshotValidationError("daily-learning invocation reservation hash is missing")
    candidates = body.pop("no_evidence_candidates", [])
    if not isinstance(candidates, list):
        raise SnapshotValidationError("daily-learning no-evidence candidates are malformed")
    manifest_core = {
        "schema_version": _STRATEGY_LEARNING_ACQUISITION_MANIFEST_SCHEMA,
        "market_date": str(body["market_date"]),
        "cutoff": str(body["cutoff"]),
        "source_identity": str(body["source_identity"]),
        "source_hash_sha256": str(body["source_hash_sha256"]),
        "code_sha": str(body["code_sha"]),
        "request": body["request"],
        "reservation_sha256": reservation_hash,
        "snapshot_path": path.name,
        "source_generation": body.get("source_generation") or {},
        "component_hashes": body.get("component_hashes") or {},
        "input_hash_sha256": body["input_hash_sha256"],
        "input_components_hash_sha256": hashlib.sha256(
            _canonical_input_bytes(body.get("input_components") or {})
        ).hexdigest(),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    acquisition_hash = hashlib.sha256(_canonical_input_bytes(manifest_core)).hexdigest()
    bound_receipts: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise SnapshotValidationError("daily-learning no-evidence candidate is malformed")
        candidate_body = {
            **dict(candidate),
            "reservation_sha256": reservation_hash,
            "acquisition_manifest_sha256": acquisition_hash,
        }
        candidate_body["receipt_sha256"] = hashlib.sha256(
            _canonical_input_bytes(candidate_body)
        ).hexdigest()
        bound_receipts.append(candidate_body)
    body["no_evidence_receipts"] = bound_receipts
    payload = {
        **body,
        "snapshot_sha256": hashlib.sha256(_canonical_input_bytes(body)).hexdigest(),
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    manifest_body = {
        **manifest_core,
        "snapshot_sha256": payload["snapshot_sha256"],
        "acquisition_manifest_sha256": acquisition_hash,
        "no_evidence_receipts": bound_receipts,
    }
    manifest = _signed_learning_manifest(manifest_body, key=_learning_manifest_key(path.parent))
    _atomic_install_bytes(path, encoded)
    _atomic_install_bytes(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    return _read_strategy_learning_evidence_snapshot(path, args)


def _external_input_identity(value: Sequence[Mapping[str, Any]] | None) -> Any:
    if value is None:
        return None
    identity: dict[str, Any] = {
        "accepted": [dict(item) for item in value],
        "invalid_identities": list(getattr(value, "invalid_identities", ())),
        "invalid_reasons": dict(getattr(value, "invalid_reasons", {})),
    }
    if hasattr(value, "expected_selection_count"):
        identity["expected_selection_count"] = int(
            getattr(value, "expected_selection_count", 0) or 0
        )
    if hasattr(value, "expected_contributor_count"):
        identity["expected_contributor_count"] = int(
            getattr(value, "expected_contributor_count", 0) or 0
        )
    return identity


def _recompute_snapshot_input_hash(snapshot: Mapping[str, Any]) -> str:
    """Recompute aggregate input identity solely from frozen source inputs."""

    components = snapshot.get("input_components")
    if not isinstance(components, Mapping):
        raise SnapshotValidationError("daily-learning frozen input components are malformed")
    parts: list[tuple[str, bytes]] = []
    if components.get("portfolio_rows") is not None:
        parts.append(
            ("portfolio_performance_rows", _canonical_input_bytes(components["portfolio_rows"]))
        )
    if components.get("paper_ops_rows") is not None:
        parts.append(
            ("paper_ops_lifecycle_rows", _canonical_input_bytes(components["paper_ops_rows"]))
        )
    paper_hash = str(components.get("paper_ops_materializer_inputs") or "")
    if paper_hash:
        if not re.fullmatch(r"[0-9a-f]{64}", paper_hash):
            raise SnapshotValidationError("daily-learning PaperOps input hash is malformed")
        parts.append(("paper_ops_materializer_inputs", paper_hash.encode("ascii")))
    if components.get("evidence_payload") is not None:
        parts.append(("evidence_payload", _canonical_input_bytes(components["evidence_payload"])))
    if components.get("decision_receipts") is not None:
        parts.append(
            ("strategy_decision_receipts", _canonical_input_bytes(components["decision_receipts"]))
        )
    if components.get("v6_decisions") is not None:
        parts.append(("alpha_v6_decisions", _canonical_input_bytes(components["v6_decisions"])))
    if components.get("research_episode_outcomes") is not None:
        parts.append(
            (
                "research_episode_outcomes",
                _canonical_input_bytes(components["research_episode_outcomes"]),
            )
        )
    if not parts:
        raise SnapshotValidationError("daily-learning frozen input components are empty")
    digest = hashlib.sha256()
    for label, payload in sorted(parts, key=lambda item: item[0]):
        encoded_label = label.encode("utf-8")
        digest.update(len(encoded_label).to_bytes(8, "big"))
        digest.update(encoded_label)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _acquire_strategy_learning_evidence(args: argparse.Namespace) -> dict[str, Any]:
    decision_receipts: Sequence[Mapping[str, Any]] | None = None
    v6_decisions: Sequence[Mapping[str, Any]] | None = None
    research_episode_outcomes: Sequence[Mapping[str, Any]] | None = None
    component_hashes: dict[str, str] = {}
    source_generation: dict[str, Any] = {"mode": "none"}
    input_components: dict[str, Any] = {}
    no_evidence_candidates: list[dict[str, Any]] = []
    if args.evidence_file:
        payload, evidence_generation = _read_stable_json(Path(args.evidence_file))
        if not isinstance(payload, dict):
            raise SnapshotValidationError("strategy learning evidence must be a JSON object")
        receipt_key = (
            "decision_receipts"
            if "decision_receipts" in payload
            else "strategy_decision_receipts"
            if "strategy_decision_receipts" in payload
            else None
        )
        if receipt_key is not None:
            raw_receipts = payload[receipt_key]
            if not isinstance(raw_receipts, list) or any(
                not isinstance(item, Mapping) for item in raw_receipts
            ):
                raise SnapshotValidationError(
                    "strategy learning decision_receipts must be a list of objects"
                )
            decision_receipts = tuple(raw_receipts)
        reservation_payload = json.loads(
            _strategy_learning_reservation_path(args).read_text(encoding="utf-8")
        )
        reserved_at = _parse_learning_timestamp(
            reservation_payload.get("reserved_at")
            if isinstance(reservation_payload, Mapping)
            else None
        )
        if reserved_at is None:
            raise SnapshotValidationError("daily-learning reservation timestamp is malformed")
        if evidence_generation.get("mtime_ns") not in (None, "") and int(
            evidence_generation["mtime_ns"]
        ) > int(reserved_at.timestamp() * 1_000_000_000):
            raise SnapshotValidationError(
                "daily-learning evidence file changed after phase-1 reservation"
            )
        input_hash_sha256 = _hash_strategy_learning_inputs(
            evidence_payload=payload,
            decision_receipts=decision_receipts,
        )
        analysis_kind = "mapping_evidence"
        analysis_payload: Mapping[str, Any] = payload
        serialized_receipts = _serialize_learning_batch(
            decision_receipts, provenance="untrusted_external"
        )
        serialized_v6 = _serialize_learning_batch(None, provenance="not_provided")
        serialized_bridges = _serialize_learning_batch(None, provenance="not_provided")
        component_hashes["evidence_payload"] = hashlib.sha256(
            _canonical_input_bytes(payload)
        ).hexdigest()
        source_generation = {
            "mode": "evidence_file",
            "evidence_file": evidence_generation,
        }
        input_components = {
            "evidence_payload": payload,
            "decision_receipts": list(decision_receipts) if decision_receipts is not None else None,
            "v6_decisions": None,
            "research_episode_outcomes": None,
        }
        component_hashes["strategy_decision_receipts"] = hashlib.sha256(
            _canonical_input_bytes(input_components["decision_receipts"])
        ).hexdigest()
    else:
        database_snapshot = load_strategy_learning_database_snapshot_readonly(
            args.db_path,
            market_date=args.market_date,
            date_cutoff=args.cutoff,
            _connection=getattr(args, "_learning_db_connection", None),
        )
        rows = database_snapshot["portfolio_rows"]
        decision_receipts = database_snapshot["decision_receipts"]
        v6_decisions = database_snapshot["v6_decisions"]
        research_episode_outcomes = database_snapshot["research_episode_outcomes"]
        source_generation = {"mode": "database", **database_snapshot["generation"]}
        paper_ops_rows = None
        paper_generation: dict[str, Any] | None = None
        paper_input_hash: str | None = None
        if args.paper_ops_root:
            from intraday_scanner.v2.paper_ops.observer_safety import PaperOpsObserverBlocked
            from intraday_scanner.v2.paper_ops.trade_blotter import (
                describe_trade_blotter_readonly_inputs,
                hash_trade_blotter_readonly_inputs,
                load_trade_blotter_readonly,
            )

            paper_root = Path(args.paper_ops_root)
            try:
                paper_ops_rows = load_trade_blotter_readonly(
                    output_root=paper_root,
                    mode="forward",
                )
            except PaperOpsObserverBlocked as exc:
                raise SnapshotValidationError(str(exc)) from exc
            declared_paper_hash = str(
                getattr(paper_ops_rows, "read_only_input_hash_sha256", "") or ""
            )
            paper_input_hash = hash_trade_blotter_readonly_inputs(paper_root)
            if declared_paper_hash != paper_input_hash:
                raise SnapshotValidationError(
                    "PaperOps read-only materializer hash conflicts with immutable input bytes"
                )
            adapter_generation = getattr(paper_ops_rows, "read_only_input_generation", None)
            paper_generation = dict(describe_trade_blotter_readonly_inputs(paper_root))
            if (
                isinstance(adapter_generation, Mapping)
                and "files" in adapter_generation
                and adapter_generation.get("files") != paper_generation.get("files")
            ):
                raise SnapshotValidationError(
                    "PaperOps read-only materializer generation changed during acquisition"
                )
            if paper_generation:
                reservation_payload = json.loads(
                    _strategy_learning_reservation_path(args).read_text(encoding="utf-8")
                )
                reserved_at = _parse_learning_timestamp(
                    reservation_payload.get("reserved_at")
                    if isinstance(reservation_payload, Mapping)
                    else None
                )
                if reserved_at is None:
                    raise SnapshotValidationError(
                        "daily-learning reservation timestamp is malformed"
                    )
                reserved_ns = int(reserved_at.timestamp() * 1_000_000_000)
                for item in paper_generation.get("files", []):
                    if isinstance(item, Mapping) and item.get("mtime_ns") not in (None, ""):
                        if int(item["mtime_ns"]) > reserved_ns:
                            raise SnapshotValidationError(
                                "PaperOps immutable input grew after phase-1 reservation"
                            )
                source_generation["paper_ops"] = {
                    **paper_generation,
                    "materialization_warnings": list(
                        getattr(paper_ops_rows, "blotter_warnings", ())
                    ),
                }
        input_hash_sha256 = _hash_strategy_learning_inputs(
            database_rows=rows,
            paper_ops_rows=paper_ops_rows,
            paper_ops_root=Path(args.paper_ops_root) if args.paper_ops_root else None,
            decision_receipts=decision_receipts,
            v6_decisions=v6_decisions,
            research_episode_outcomes=research_episode_outcomes,
        )
        report = attribute_strategy_misses(
            rows,
            date_cutoff=args.cutoff,
            paper_ops_rows=paper_ops_rows,
        )
        analysis_kind = "attribution_report"
        analysis_payload = report.to_dict()
        serialized_receipts = _serialize_learning_batch(
            decision_receipts, provenance="persisted_v5"
        )
        serialized_v6 = _serialize_learning_batch(v6_decisions, provenance="persisted_v6")
        serialized_bridges = _serialize_learning_batch(
            research_episode_outcomes,
            provenance="persisted_research_bridge",
        )
        component_hashes["portfolio_performance_rows"] = hashlib.sha256(
            _canonical_input_bytes(rows)
        ).hexdigest()
        if paper_ops_rows is not None:
            component_hashes["paper_ops_lifecycle_rows"] = hashlib.sha256(
                _canonical_input_bytes(paper_ops_rows)
            ).hexdigest()
            component_hashes["paper_ops_materializer_inputs"] = str(
                paper_input_hash or getattr(paper_ops_rows, "read_only_input_hash_sha256", "")
            )
            component_hashes["paper_ops_ledger"] = str(
                getattr(paper_ops_rows, "ledger_source_hash_sha256", "")
            )
        input_components = {
            "portfolio_rows": list(rows),
            "paper_ops_rows": list(paper_ops_rows) if paper_ops_rows is not None else None,
            "decision_receipts": _external_input_identity(decision_receipts),
            "v6_decisions": _external_input_identity(v6_decisions),
            "research_episode_outcomes": _external_input_identity(
                research_episode_outcomes
            ),
            "paper_ops_materializer_inputs": paper_input_hash
            if paper_ops_rows is not None
            else None,
        }
        if input_components["decision_receipts"] is not None:
            component_hashes["strategy_decision_receipts"] = hashlib.sha256(
                _canonical_input_bytes(input_components["decision_receipts"])
            ).hexdigest()
        if input_components["v6_decisions"] is not None:
            component_hashes["alpha_v6_decisions"] = hashlib.sha256(
                _canonical_input_bytes(input_components["v6_decisions"])
            ).hexdigest()
        component_hashes["research_episode_outcomes"] = hashlib.sha256(
            _canonical_input_bytes(input_components["research_episode_outcomes"])
        ).hexdigest()
        no_evidence_candidates = _no_evidence_candidates(
            args,
            rows=rows,
            decision_receipts=decision_receipts,
            v6_decisions=v6_decisions,
            paper_ops_rows=paper_ops_rows,
            source_generation=source_generation,
            component_hashes=component_hashes,
        )
    if not isinstance(input_hash_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", input_hash_sha256
    ):
        raise SnapshotValidationError("strategy learning input hash is unavailable")
    component_hashes["analysis_payload"] = hashlib.sha256(
        _canonical_input_bytes(analysis_payload)
    ).hexdigest()
    return {
        "schema_version": _STRATEGY_LEARNING_EVIDENCE_SNAPSHOT_SCHEMA,
        "market_date": str(args.market_date),
        "cutoff": str(args.cutoff),
        "source_identity": str(args.source_identity),
        "source_hash_sha256": _strategy_learning_source_hash(args),
        "code_sha": str(args.code_sha),
        "request": _strategy_learning_snapshot_request(args),
        "analysis_kind": analysis_kind,
        "analysis_payload": analysis_payload,
        "decision_receipts": serialized_receipts,
        "v6_decisions": serialized_v6,
        "research_episode_outcomes": serialized_bridges,
        "input_hash_sha256": input_hash_sha256,
        "component_hashes": dict(sorted(component_hashes.items())),
        "source_generation": source_generation,
        "input_components": input_components,
        "no_evidence_candidates": no_evidence_candidates,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _serialize_learning_batch(
    values: Sequence[Mapping[str, Any]] | None,
    *,
    provenance: str,
) -> dict[str, Any]:
    if values is None:
        return {
            "provided": False,
            "provenance": "not_provided",
            "items": [],
            "invalid_reasons": {},
            "invalid_identities": [],
            "expected_selection_count": 0,
            "expected_contributor_count": 0,
        }
    items: list[dict[str, Any]] = []
    for value in values:
        item: dict[str, Any] = {"payload": dict(value)}
        if provenance == "persisted_v5":
            envelope = getattr(value, "_envelope", None)
            if not isinstance(envelope, Mapping):
                raise SnapshotValidationError("daily-learning v5 receipt lacks persisted envelope")
            item["envelope"] = dict(envelope)
        elif provenance == "persisted_v6":
            envelope = getattr(value, "_envelope", None)
            if not isinstance(envelope, Mapping) or not envelope.get("stored_at"):
                raise SnapshotValidationError("daily-learning V6 decision lacks persisted envelope")
            item["envelope"] = dict(envelope)
        elif provenance == "persisted_research_bridge":
            envelope = getattr(value, "_envelope", None)
            if not isinstance(envelope, Mapping):
                raise SnapshotValidationError(
                    "daily-learning research bridge lacks persisted envelope"
                )
            item["envelope"] = dict(envelope)
        items.append(item)
    return {
        "provided": True,
        "provenance": provenance,
        "items": items,
        "invalid_reasons": dict(getattr(values, "invalid_reasons", {})),
        "invalid_identities": list(getattr(values, "invalid_identities", ())),
        "expected_selection_count": int(
            getattr(values, "expected_selection_count", 0) or 0
        ),
        "expected_contributor_count": int(
            getattr(values, "expected_contributor_count", 0) or 0
        ),
    }


def _no_evidence_candidates(
    args: argparse.Namespace,
    *,
    rows: Sequence[Mapping[str, Any]],
    decision_receipts: Sequence[Mapping[str, Any]] | None,
    v6_decisions: Sequence[Mapping[str, Any]] | None,
    paper_ops_rows: Sequence[Mapping[str, Any]] | None,
    source_generation: Mapping[str, Any],
    component_hashes: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Describe zero cohorts before binding them to the signed manifest."""

    from intraday_scanner.v2.paper_ops.trade_blotter import ReadOnlyBlotterRows

    generation = dict(source_generation)
    table_bounds = generation.get("table_bounds")
    if not isinstance(table_bounds, Mapping):
        table_bounds = {}
    generation_hash = hashlib.sha256(_canonical_input_bytes(generation)).hexdigest()
    candidates: list[dict[str, Any]] = []

    def empty_table(table: str) -> bool:
        bound = table_bounds.get(table)
        return (
            isinstance(bound, Mapping)
            and bound.get("exists") is True
            and int(bound.get("row_count") or 0) == 0
            and table in table_bounds
        )

    portfolio_empty = (
        paper_ops_rows is None and empty_table("portfolio_performance_rows") and not rows
    )
    paper_warnings = getattr(paper_ops_rows, "blotter_warnings", None)
    paper_input_generation = getattr(paper_ops_rows, "read_only_input_generation", None)
    paper_files = (
        {
            str(item.get("path")): item
            for item in paper_input_generation.get("files", [])
            if isinstance(item, Mapping)
        }
        if isinstance(paper_input_generation, Mapping)
        and isinstance(paper_input_generation.get("files"), list)
        else {}
    )
    required_paper_files = {
        "ledger/paper_ledger.jsonl",
        "state/paper_ops_config.json",
        "state/strategy_registry.json",
        "state/execution_policy_manifest.json",
    }
    paper_materialization_complete = (
        isinstance(paper_ops_rows, ReadOnlyBlotterRows)
        and isinstance(paper_warnings, tuple)
        and not paper_warnings
        and isinstance(paper_input_generation, Mapping)
        and isinstance(paper_input_generation.get("files"), list)
        and bool(paper_input_generation["files"])
        and required_paper_files.issubset(paper_files)
        and all(
            re.fullmatch(r"[0-9a-f]{64}", str(paper_files[path].get("sha256") or ""))
            and int(paper_files[path].get("size") or -1) >= 0
            for path in required_paper_files
        )
    )
    paper_empty = paper_materialization_complete and not paper_ops_rows
    if portfolio_empty or (paper_empty and not rows):
        if portfolio_empty:
            table = "portfolio_performance_rows"
            source_hash = component_hashes.get("portfolio_performance_rows", "")
            table_generation = table_bounds.get(table)
        else:
            table = "paper_ops_materialized_rows"
            source_hash = component_hashes.get("paper_ops_lifecycle_rows", "")
            table_generation = generation.get("paper_ops")
        if re.fullmatch(r"[0-9a-f]{64}", str(source_hash)):
            for strategy in _build_daily_strategy_catalog():
                candidates.append(
                    {
                        "schema_version": "dawnstrike.strategy_learning_no_evidence.v1",
                        "receipt_type": "no_evidence",
                        "lane": "strategy",
                        "strategy_id": strategy.strategy_id,
                        "strategy_version": strategy.version,
                        "market_date": str(args.market_date),
                        "cutoff": str(args.cutoff),
                        "zero_count": 0,
                        "no_trade": True,
                        "source_component_hash_sha256": str(source_hash),
                        "source_generation_hash_sha256": generation_hash,
                        "query": {
                            "kind": "point_in_time_zero_query",
                            "table": table,
                            "market_date": str(args.market_date),
                            "cutoff": str(args.cutoff),
                            "strategy_id": strategy.strategy_id,
                            "strategy_version": strategy.version,
                            "table_generation": table_generation,
                        },
                    }
                )

    def lane_empty(batch: Sequence[Mapping[str, Any]] | None, table: str) -> bool:
        return (
            batch is not None
            and not batch
            and int(getattr(batch, "invalid_count", 0) or 0) == 0
            and empty_table(table)
        )

    if lane_empty(decision_receipts, "strategy_decision_receipts"):
        source_hash = component_hashes.get("strategy_decision_receipts", "")
        if re.fullmatch(r"[0-9a-f]{64}", str(source_hash)):
            strategy_id, strategy_version = ("alphaops_v5", "dawnstrike-alphaops-v5.0.0")
            candidates.append(
                {
                    "schema_version": "dawnstrike.strategy_learning_no_evidence.v1",
                    "receipt_type": "no_evidence",
                    "lane": "v5",
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "market_date": str(args.market_date),
                    "cutoff": str(args.cutoff),
                    "zero_count": 0,
                    "no_trade": True,
                    "source_component_hash_sha256": str(source_hash),
                    "source_generation_hash_sha256": generation_hash,
                    "query": {
                        "kind": "point_in_time_zero_query",
                        "table": "strategy_decision_receipts",
                        "market_date": str(args.market_date),
                        "cutoff": str(args.cutoff),
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "table_generation": table_bounds.get("strategy_decision_receipts"),
                    },
                }
            )

    if lane_empty(v6_decisions, "alpha_v6_decisions"):
        source_hash = component_hashes.get("alpha_v6_decisions", "")
        if re.fullmatch(r"[0-9a-f]{64}", str(source_hash)):
            strategy_id, strategy_version = ("alphaops_v6", "dawnstrike-alphaops-v6-shadow")
            candidates.append(
                {
                    "schema_version": "dawnstrike.strategy_learning_no_evidence.v1",
                    "receipt_type": "no_evidence",
                    "lane": "v6",
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "market_date": str(args.market_date),
                    "cutoff": str(args.cutoff),
                    "zero_count": 0,
                    "no_trade": True,
                    "source_component_hash_sha256": str(source_hash),
                    "source_generation_hash_sha256": generation_hash,
                    "query": {
                        "kind": "point_in_time_zero_query",
                        "table": "alpha_v6_decisions",
                        "market_date": str(args.market_date),
                        "cutoff": str(args.cutoff),
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "table_generation": table_bounds.get("alpha_v6_decisions"),
                    },
                }
            )
    return candidates


def _read_strategy_learning_evidence_snapshot(
    path: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    manifest_path = path.with_name("daily_learning_acquisition_manifest.json")
    reservation_path = _strategy_learning_reservation_path(args)
    if path.is_symlink() or manifest_path.is_symlink() or reservation_path.is_symlink():
        raise SnapshotValidationError("daily-learning frozen evidence boundary contains a symlink")
    if not path.is_file():
        raise SnapshotValidationError("daily-learning frozen evidence path is not a file")
    if not manifest_path.is_file():
        raise SnapshotValidationError(
            "daily-learning frozen evidence has no authenticated acquisition manifest"
        )
    if not reservation_path.is_file():
        raise SnapshotValidationError("daily-learning phase-1 invocation reservation is missing")
    try:
        reservation_payload = json.loads(reservation_path.read_text(encoding="utf-8"))
        reservation = _verify_learning_manifest(
            reservation_payload, key=_learning_manifest_key(path.parent)
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(
            "daily-learning invocation reservation is unreadable"
        ) from exc
    if reservation.get("reservation_phase") != 1:
        raise SnapshotValidationError("daily-learning phase-1 reservation is not immutable")
    for field in (
        "market_date",
        "code_sha",
        "request",
        "research_only",
        "broker_execution_enabled",
    ):
        expected_value = (
            str(args.market_date)
            if field == "market_date"
            else str(args.code_sha)
            if field == "code_sha"
            else _strategy_learning_snapshot_request(args)
            if field == "request"
            else True
            if field == "research_only"
            else False
        )
        if reservation.get(field) != expected_value:
            raise SnapshotValidationError(
                f"daily-learning invocation reservation identity conflict: {field}"
            )
    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = _verify_learning_manifest(
            manifest_payload, key=_learning_manifest_key(path.parent)
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError("daily-learning acquisition manifest is unreadable") from exc
    if manifest.get("schema_version") != _STRATEGY_LEARNING_ACQUISITION_MANIFEST_SCHEMA:
        raise SnapshotValidationError("daily-learning acquisition manifest schema mismatch")
    if manifest.get("reservation_sha256") != reservation_payload.get("reservation_sha256"):
        raise SnapshotValidationError(
            "daily-learning acquisition manifest reservation binding mismatch"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError("daily-learning frozen evidence is unreadable") from exc
    if not isinstance(payload, dict):
        raise SnapshotValidationError("daily-learning frozen evidence is not an object")
    snapshot_hash = payload.get("snapshot_sha256")
    body = {key: value for key, value in payload.items() if key != "snapshot_sha256"}
    expected_hash = hashlib.sha256(_canonical_input_bytes(body)).hexdigest()
    if snapshot_hash != expected_hash:
        raise SnapshotValidationError("daily-learning frozen evidence hash mismatch")
    expected = {
        "schema_version": _STRATEGY_LEARNING_EVIDENCE_SNAPSHOT_SCHEMA,
        "market_date": str(args.market_date),
        "code_sha": str(args.code_sha),
        "request": _strategy_learning_snapshot_request(args),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    for field, value in expected.items():
        if body.get(field) != value:
            raise SnapshotValidationError(
                f"daily-learning frozen evidence identity conflict: {field}"
            )
    for field in (
        "market_date",
        "cutoff",
        "source_identity",
        "source_hash_sha256",
        "code_sha",
        "request",
        "input_hash_sha256",
        "research_only",
        "broker_execution_enabled",
    ):
        if manifest.get(field) != body.get(field):
            raise SnapshotValidationError(
                f"daily-learning acquisition manifest identity conflict: {field}"
            )
    for field in ("market_date", "cutoff", "source_identity", "source_hash_sha256", "code_sha"):
        if reservation.get(field) != body.get(field):
            raise SnapshotValidationError(
                f"daily-learning reservation/acquisition identity conflict: {field}"
            )
    if reservation.get("input_hash_sha256") != body.get("input_hash_sha256"):
        raise SnapshotValidationError(
            "daily-learning reservation/acquisition input hash binding mismatch"
        )
    if (
        manifest.get("snapshot_path") != path.name
        or manifest.get("snapshot_sha256") != snapshot_hash
    ):
        raise SnapshotValidationError("daily-learning acquisition manifest snapshot mismatch")
    acquisition_hash = str(manifest.get("acquisition_manifest_sha256") or "")
    manifest_core = {
        key: value
        for key, value in manifest.items()
        if key not in {"snapshot_sha256", "acquisition_manifest_sha256", "no_evidence_receipts"}
    }
    if (
        not re.fullmatch(r"[0-9a-f]{64}", acquisition_hash)
        or acquisition_hash != hashlib.sha256(_canonical_input_bytes(manifest_core)).hexdigest()
    ):
        raise SnapshotValidationError("daily-learning acquisition manifest hash mismatch")
    no_evidence_receipts = manifest.get("no_evidence_receipts")
    if (
        not isinstance(no_evidence_receipts, list)
        or body.get("no_evidence_receipts") != no_evidence_receipts
    ):
        raise SnapshotValidationError("daily-learning no-evidence receipt binding mismatch")
    source_generation = manifest.get("source_generation")
    if not isinstance(source_generation, Mapping):
        raise SnapshotValidationError("daily-learning source generation is malformed")
    table_bounds = source_generation.get("table_bounds")
    if not isinstance(table_bounds, Mapping):
        table_bounds = {}
    paper_generation = source_generation.get("paper_ops")
    seen_zero_identities: set[tuple[str, str, str]] = set()
    for receipt in no_evidence_receipts:
        if not isinstance(receipt, Mapping):
            raise SnapshotValidationError("daily-learning no-evidence receipt is malformed")
        receipt_body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        receipt_hash = str(receipt.get("receipt_sha256") or "")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", receipt_hash)
            or receipt_hash != hashlib.sha256(_canonical_input_bytes(receipt_body)).hexdigest()
            or receipt.get("reservation_sha256") != reservation_payload.get("reservation_sha256")
            or receipt.get("acquisition_manifest_sha256") != acquisition_hash
            or receipt.get("zero_count") != 0
            or receipt.get("no_trade") is not True
            or not isinstance(receipt.get("query"), Mapping)
        ):
            raise SnapshotValidationError("daily-learning no-evidence receipt validation failed")
        lane = str(receipt.get("lane") or "")
        query = receipt.get("query")
        query_table = query.get("table") if isinstance(query, Mapping) else None
        component_label = (
            "portfolio_performance_rows"
            if lane == "strategy" and query_table == "portfolio_performance_rows"
            else "paper_ops_lifecycle_rows"
            if lane == "strategy" and query_table == "paper_ops_materialized_rows"
            else "strategy_decision_receipts"
            if lane == "v5"
            else "alpha_v6_decisions"
            if lane == "v6"
            else None
        )
        expected_table = {
            "strategy": {"portfolio_performance_rows", "paper_ops_materialized_rows"},
            "v5": {"strategy_decision_receipts"},
            "v6": {"alpha_v6_decisions"},
        }.get(lane, set())
        table_generation = (
            table_bounds.get(query_table)
            if query_table
            in {"portfolio_performance_rows", "strategy_decision_receipts", "alpha_v6_decisions"}
            else paper_generation
        )
        table_zero_is_authenticated = (
            isinstance(table_generation, Mapping)
            and table_generation.get("exists") is True
            and int(table_generation.get("row_count") or 0) == 0
            if query_table != "paper_ops_materialized_rows"
            else isinstance(table_generation, Mapping)
            and table_generation.get("materialization_warnings") == []
        )
        if (
            component_label is None
            or receipt.get("source_component_hash_sha256")
            != (manifest.get("component_hashes") or {}).get(component_label)
            or receipt.get("source_generation_hash_sha256")
            != hashlib.sha256(
                _canonical_input_bytes(manifest.get("source_generation") or {})
            ).hexdigest()
            or not isinstance(query, Mapping)
            or query.get("table") not in expected_table
            or query.get("kind") != "point_in_time_zero_query"
            or query.get("table_generation") != table_generation
            or not table_zero_is_authenticated
            or query.get("market_date") != body.get("market_date")
            or query.get("cutoff") != body.get("cutoff")
            or query.get("strategy_id") != receipt.get("strategy_id")
            or query.get("strategy_version") != receipt.get("strategy_version")
        ):
            raise SnapshotValidationError(
                "daily-learning no-evidence receipt source binding failed"
            )
        identity = (
            str(receipt.get("lane") or ""),
            str(receipt.get("strategy_id") or ""),
            str(receipt.get("strategy_version") or ""),
        )
        if identity in seen_zero_identities:
            raise SnapshotValidationError("daily-learning duplicate no-evidence receipt")
        seen_zero_identities.add(identity)
    if not re.fullmatch(r"[0-9a-f]{64}", str(body.get("input_hash_sha256") or "")):
        raise SnapshotValidationError("daily-learning frozen input hash is malformed")
    analysis_payload = body.get("analysis_payload")
    component_hashes = body.get("component_hashes")
    if not isinstance(analysis_payload, Mapping) or not isinstance(component_hashes, Mapping):
        raise SnapshotValidationError("daily-learning frozen analyzer payload is malformed")
    if (
        component_hashes.get("analysis_payload")
        != hashlib.sha256(_canonical_input_bytes(analysis_payload)).hexdigest()
    ):
        raise SnapshotValidationError("daily-learning frozen analyzer hash mismatch")
    if manifest.get("component_hashes") != component_hashes:
        raise SnapshotValidationError("daily-learning acquisition component manifest mismatch")
    input_components = body.get("input_components")
    if not isinstance(input_components, Mapping):
        raise SnapshotValidationError("daily-learning frozen input components are malformed")
    if (
        manifest.get("input_components_hash_sha256")
        != hashlib.sha256(_canonical_input_bytes(input_components)).hexdigest()
    ):
        raise SnapshotValidationError("daily-learning frozen input component hash mismatch")
    expected_components: dict[str, str] = {
        "analysis_payload": hashlib.sha256(_canonical_input_bytes(analysis_payload)).hexdigest()
    }
    component_labels = {
        "evidence_payload": "evidence_payload",
        "portfolio_rows": "portfolio_performance_rows",
        "paper_ops_rows": "paper_ops_lifecycle_rows",
        "decision_receipts": "strategy_decision_receipts",
        "v6_decisions": "alpha_v6_decisions",
        "research_episode_outcomes": "research_episode_outcomes",
    }
    for component_key, component_label in component_labels.items():
        if input_components.get(component_key) is not None:
            expected_components[component_label] = hashlib.sha256(
                _canonical_input_bytes(input_components[component_key])
            ).hexdigest()
    if input_components.get("paper_ops_materializer_inputs"):
        expected_components["paper_ops_materializer_inputs"] = str(
            input_components["paper_ops_materializer_inputs"]
        )
    for key, value in expected_components.items():
        if component_hashes.get(key) != value:
            raise SnapshotValidationError(f"daily-learning frozen component hash mismatch: {key}")
    recomputed = _recompute_snapshot_input_hash(body)
    if recomputed != body.get("input_hash_sha256"):
        raise SnapshotValidationError("daily-learning frozen aggregate input hash mismatch")
    return _AuthenticatedLearningSnapshot(body, token=_AuthenticatedLearningSnapshot._TOKEN)


def _restore_strategy_learning_evidence(
    snapshot: Mapping[str, Any],
) -> tuple[
    StrategyEvidenceAnalyzer,
    str,
    Sequence[Mapping[str, Any]] | None,
    Sequence[Mapping[str, Any]] | None,
    Sequence[Mapping[str, Any]],
    Sequence[Mapping[str, Any]] | None,
]:
    if not isinstance(snapshot, _AuthenticatedLearningSnapshot):
        raise SnapshotValidationError(
            "daily-learning snapshot must be authenticated by its acquisition manifest"
        )
    analysis_kind = snapshot.get("analysis_kind")
    analysis_payload = snapshot.get("analysis_payload")
    if not isinstance(analysis_payload, Mapping):
        raise SnapshotValidationError("daily-learning frozen analyzer payload is malformed")
    if analysis_kind == "mapping_evidence":
        analyzer: StrategyEvidenceAnalyzer = MappingEvidenceAnalyzer(analysis_payload)
        allowed_receipt_provenance = "untrusted_external"
    elif analysis_kind == "attribution_report":
        analyzer = AttributionReportAnalyzer(analysis_payload)
        allowed_receipt_provenance = "persisted_v5"
    else:
        raise SnapshotValidationError("daily-learning frozen analyzer kind is unsupported")
    decision_receipts = _restore_learning_batch(
        snapshot.get("decision_receipts"),
        allowed_provenance=allowed_receipt_provenance,
        authenticated=True,
        market_date=str(snapshot["market_date"]),
        cutoff=str(snapshot["cutoff"]),
    )
    v6_decisions = _restore_learning_batch(
        snapshot.get("v6_decisions"),
        allowed_provenance="persisted_v6",
        authenticated=True,
        market_date=str(snapshot["market_date"]),
        cutoff=str(snapshot["cutoff"]),
    )
    research_episode_outcomes = _restore_learning_batch(
        snapshot.get("research_episode_outcomes"),
        allowed_provenance="persisted_research_bridge",
        authenticated=True,
        market_date=str(snapshot["market_date"]),
        cutoff=str(snapshot["cutoff"]),
    )
    raw_no_evidence = snapshot.get("no_evidence_receipts")
    if not isinstance(raw_no_evidence, list):
        raise SnapshotValidationError("daily-learning no-evidence receipts are malformed")
    no_evidence_receipts = _authenticated_no_evidence_receipts(raw_no_evidence)
    return (
        analyzer,
        str(snapshot["input_hash_sha256"]),
        decision_receipts,
        v6_decisions,
        no_evidence_receipts,
        research_episode_outcomes,
    )


def _restore_learning_batch(
    value: Any,
    *,
    allowed_provenance: str,
    authenticated: bool = False,
    market_date: str | None = None,
    cutoff: str | None = None,
) -> Sequence[Mapping[str, Any]] | None:
    if not isinstance(value, Mapping):
        raise SnapshotValidationError("daily-learning frozen batch is malformed")
    provided = value.get("provided")
    provenance = value.get("provenance")
    items = value.get("items")
    invalid_reasons = value.get("invalid_reasons")
    invalid_identities = value.get("invalid_identities")
    expected_selection_count = value.get("expected_selection_count", 0)
    expected_contributor_count = value.get("expected_contributor_count", 0)
    if (
        not isinstance(provided, bool)
        or not isinstance(items, list)
        or not isinstance(invalid_reasons, Mapping)
        or not isinstance(invalid_identities, list)
        or isinstance(expected_selection_count, bool)
        or not isinstance(expected_selection_count, int)
        or expected_selection_count < 0
        or isinstance(expected_contributor_count, bool)
        or not isinstance(expected_contributor_count, int)
        or expected_contributor_count < 0
    ):
        raise SnapshotValidationError("daily-learning frozen batch fields are malformed")
    if not provided:
        if (
            provenance != "not_provided"
            or items
            or invalid_reasons
            or invalid_identities
            or expected_selection_count
            or expected_contributor_count
        ):
            raise SnapshotValidationError("daily-learning absent batch has evidence")
        return None
    if provenance != allowed_provenance:
        raise SnapshotValidationError("daily-learning frozen batch provenance mismatch")
    if provenance in {
        "persisted_v5",
        "persisted_v6",
        "persisted_research_bridge",
    } and not authenticated:
        raise SnapshotValidationError(
            "daily-learning persisted provenance requires authenticated acquisition"
        )
    restored: list[Mapping[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("payload"), Mapping):
            raise SnapshotValidationError("daily-learning frozen batch item is malformed")
        payload = dict(item["payload"])
        if provenance == "persisted_v5":
            envelope = item.get("envelope")
            if not isinstance(envelope, Mapping):
                raise SnapshotValidationError("daily-learning frozen v5 envelope is malformed")
            from intraday_scanner.services.daily_strategy_learning_service import (
                _persisted_receipt,
            )

            restored_item = _persisted_receipt(
                payload,
                envelope=dict(envelope),
                schema_validated=True,
            )
            if market_date is None or cutoff is None:
                raise SnapshotValidationError("daily-learning V5 restore context is missing")
            from intraday_scanner.services.daily_strategy_learning_service import (
                _cutoff_datetime,
                _validate_persisted_decision_receipt,
            )

            cutoff_at = _cutoff_datetime(
                DailyLearningContext(
                    market_date=market_date,
                    cutoff=cutoff,
                    source_identity="snapshot-restore",
                    code_sha="snapshot-restore",
                    source_hash_sha256="0" * 64,
                )
            )
            valid, reason = _validate_persisted_decision_receipt(
                restored_item, market_date=market_date, cutoff=cutoff_at
            )
            if not valid:
                raise SnapshotValidationError(
                    f"daily-learning V5 restore validation failed: {reason}"
                )
            restored.append(restored_item)
        elif provenance == "persisted_v6":
            from intraday_scanner.services.daily_strategy_learning_service import (
                _persisted_v6_decision,
            )

            envelope = item.get("envelope")
            if not isinstance(envelope, Mapping):
                raise SnapshotValidationError("daily-learning V6 envelope is malformed")
            _validate_restored_v6_item(
                payload,
                envelope,
                market_date=market_date,
                cutoff=cutoff,
            )
            restored.append(_persisted_v6_decision(payload, envelope=dict(envelope)))
        elif provenance == "persisted_research_bridge":
            envelope = item.get("envelope")
            if not isinstance(envelope, Mapping):
                raise SnapshotValidationError(
                    "daily-learning frozen research bridge envelope is malformed"
                )
            if market_date is None or cutoff is None:
                raise SnapshotValidationError(
                    "daily-learning research bridge restore context is missing"
                )
            from intraday_scanner.services.daily_strategy_learning_service import (
                _persisted_research_bridge,
                _validate_persisted_research_bridge,
            )

            restored_bridge = _persisted_research_bridge(payload, envelope=dict(envelope))
            bridge_cutoff_at = _parse_learning_timestamp(cutoff)
            if bridge_cutoff_at is None:
                raise SnapshotValidationError(
                    "daily-learning research bridge restore cutoff is malformed"
                )
            valid, reason = _validate_persisted_research_bridge(
                restored_bridge,
                market_date=market_date,
                cutoff=bridge_cutoff_at,
            )
            if not valid:
                raise SnapshotValidationError(
                    f"daily-learning research bridge restore failed: {reason}"
                )
            restored.append(restored_bridge)
        else:
            restored.append(payload)
    return _FrozenLearningBatch(
        restored,
        invalid_reasons=invalid_reasons,
        invalid_identities=invalid_identities,
        expected_selection_count=expected_selection_count,
        expected_contributor_count=expected_contributor_count,
    )


def _validate_restored_v6_item(
    payload: Mapping[str, Any],
    envelope: Mapping[str, Any],
    *,
    market_date: str | None,
    cutoff: str | None,
) -> None:
    """Re-run the complete V6 canonical, row-envelope, and stored_at gates."""

    if market_date is None or cutoff is None:
        raise SnapshotValidationError("daily-learning V6 restore context is missing")
    from intraday_scanner.alpha.v6.decision_ledger import validate_decision_batch

    if not validate_decision_batch([dict(payload)])["valid"]:
        raise SnapshotValidationError("daily-learning V6 restore canonical validation failed")
    cutoff_at = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    if cutoff_at.tzinfo is None:
        raise SnapshotValidationError("daily-learning V6 restore cutoff is not aware")
    for field in (
        "decision_id",
        "scan_id",
        "source_signal_id",
        "shadow_signal_id",
        "market_date",
        "decision_at",
        "ticker",
        "strategy_version",
        "model_version",
        "action",
        "setup_key",
        "regime_key",
        "input_hash_sha256",
        "source_lineage_hash_sha256",
    ):
        if not isinstance(envelope.get(field), str) or envelope[field] != str(
            payload.get(field) or ""
        ):
            raise SnapshotValidationError(f"daily-learning V6 restore envelope mismatch: {field}")
    if payload.get("market_date") != market_date:
        raise SnapshotValidationError("daily-learning V6 restore market date mismatch")
    decision_at = datetime.fromisoformat(str(payload.get("decision_at")).replace("Z", "+00:00"))
    if decision_at.tzinfo is None or decision_at > cutoff_at:
        raise SnapshotValidationError("daily-learning V6 restore decision cutoff mismatch")
    stored_at = str(envelope.get("stored_at") or "")
    try:
        stored = datetime.fromisoformat(stored_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotValidationError("daily-learning V6 restore stored_at is malformed") from exc
    if stored.tzinfo is None or stored > cutoff_at:
        raise SnapshotValidationError("daily-learning V6 restore stored_at cutoff mismatch")
    if envelope.get("safety_vetoes") != payload.get("safety_vetoes"):
        raise SnapshotValidationError("daily-learning V6 restore safety_vetoes mismatch")
    expected_payload_hash = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if envelope.get("payload_hash_sha256") != expected_payload_hash:
        raise SnapshotValidationError("daily-learning V6 restore payload hash mismatch")


def _receipts_at_or_before_cutoff(
    receipts: Sequence[Mapping[str, Any]], cutoff: str
) -> tuple[Mapping[str, Any], ...]:
    """Keep only aware, exact-date decision receipts known at the cutoff."""

    try:
        cutoff_at = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotValidationError("strategy learning cutoff must be an ISO datetime") from exc
    if cutoff_at.tzinfo is None:
        raise SnapshotValidationError("strategy learning cutoff must include a timezone")
    result: list[Mapping[str, Any]] = []
    for receipt in receipts:
        try:
            decision_at = datetime.fromisoformat(
                str(receipt.get("decision_at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if decision_at.tzinfo is not None and decision_at <= cutoff_at:
            result.append(receipt)
    return tuple(result)


def _run_strategy_challenger_backtest(args: argparse.Namespace) -> int:
    result = run_strategy_challenger_backtest(
        data_truth_root=args.data_truth_root,
        snapshot_id=args.snapshot_id,
        code_sha=args.code_sha,
        out_path=args.out,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "complete" else 1


def _run_strategy_challenger_weekly(args: argparse.Namespace) -> int:
    result = run_strategy_challenger_weekly_adapter(
        db_path=args.db_path,
        state_root=args.state_root,
        market_date=args.market_date,
        code_sha=args.code_sha,
        out_root=args.out_root,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    # Honest evidence absence is a completed, explicitly non-evaluable result.
    return 0 if str(result.get("status") or "").startswith("NOT_EVALUABLE") else 1


def _run_alpha_v6_train_weekly(args: argparse.Namespace) -> int:
    result = run_alpha_v6_weekly_training(
        SQLiteScanStore(args.db_path),
        code_sha=args.code_sha,
        market_date=args.market_date,
        reference_window=_read_v6_window(getattr(args, "reference_window", None)),
        recent_window=_read_v6_window(getattr(args, "recent_window", None)),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_v6_register_experiment(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SnapshotValidationError("V6 experiment input must be a JSON object.")
    required = (
        "hypothesis",
        "training_cutoff",
        "baseline_config",
        "candidate_config",
        "validation_start",
        "holdout_start",
        "stop_condition",
        "promotion_requirements",
    )
    missing = [field for field in required if field not in payload]
    if missing:
        raise SnapshotValidationError("V6 experiment input is missing: " + ", ".join(missing))
    experiment = register_experiment(
        hypothesis=str(payload["hypothesis"]),
        training_cutoff=str(payload["training_cutoff"]),
        baseline_config=dict(payload["baseline_config"]),
        candidate_config=dict(payload["candidate_config"]),
        validation_start=str(payload["validation_start"]),
        holdout_start=str(payload["holdout_start"]),
        stop_condition=str(payload["stop_condition"]),
        promotion_requirements=list(payload["promotion_requirements"]),
        training_dates=payload.get("training_dates"),
        validation_dates=payload.get("validation_dates"),
        holdout_dates=payload.get("holdout_dates"),
        holdout_end=payload.get("holdout_end"),
        validation_end=payload.get("validation_end"),
        data_hash_sha256=payload.get("data_hash_sha256"),
        source_hash_sha256=payload.get("source_hash_sha256"),
        code_sha=payload.get("code_sha"),
        window_hash_sha256=payload.get("window_hash_sha256"),
        v5_comparison_hash_sha256=payload.get("v5_comparison_hash_sha256"),
        input_hash_sha256=payload.get("input_hash_sha256"),
    )
    persisted = SQLiteScanStore(args.db_path).persist_alpha_v6_experiments([experiment])
    result = {
        "status": "REGISTERED_NOT_APPLIED" if persisted["inserted"] else "ALREADY_REGISTERED",
        "persisted": persisted,
        "experiment": experiment,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_v6_evaluate_holdout(args: argparse.Namespace) -> int:
    result = evaluate_registered_holdout(
        SQLiteScanStore(args.db_path),
        experiment_id=args.experiment_id,
        as_of_date=args.as_of,
        model_run_id=args.model_run_id,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"HOLDOUT_RECORDED", "ALREADY_EVALUATED_IMMUTABLE"} else 2


def _run_alpha_v6_attribution(args: argparse.Namespace) -> int:
    result = build_v6_failure_attribution(
        SQLiteScanStore(args.db_path, read_only=True), persist=False
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_v6_research_packet(args: argparse.Namespace) -> int:
    result = write_alpha_v6_research_packet(
        SQLiteScanStore(args.db_path, read_only=True),
        code_sha=args.code_sha,
        out_dir=args.out_dir,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_v6_register_universe(args: argparse.Namespace) -> int:
    reviewed = _read_alpha_v6_universe_candidate(args.input)
    payload = build_alpha_v6_universe_candidate(
        source_contract_path=args.source_contract,
        raw_artifact_path=args.raw_artifact,
    )
    if reviewed["candidate_hash_sha256"] != payload["candidate_hash_sha256"]:
        raise SnapshotValidationError(
            "V6 universe registration source inputs do not reproduce the reviewed candidate."
        )
    members = payload["members"]
    source_lineage = payload["source_lineage"]
    store = SQLiteScanStore(args.db_path)
    preview = preview_alpha_v6_universe(
        store,
        as_of_date=str(payload.get("as_of_date") or ""),
        members=members,
        source_lineage=source_lineage,
    )
    if args.confirm_preview_hash != preview["preview_hash_sha256"]:
        raise SnapshotValidationError(
            "V6 universe registration requires the exact current preview_hash_sha256."
        )
    result = register_alpha_v6_universe(
        store,
        as_of_date=str(payload.get("as_of_date") or ""),
        members=members,
        source_lineage=source_lineage,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_v6_build_universe(args: argparse.Namespace) -> int:
    result = build_alpha_v6_universe_candidate(
        source_contract_path=args.source_contract,
        raw_artifact_path=args.raw_artifact,
    )
    output_path = write_alpha_v6_universe_candidate(result, output_path=args.out)
    print(json.dumps({**result, "output_path": str(output_path)}, indent=2, sort_keys=True))
    return 0 if result["registration_allowed"] is True else 2


def _run_alpha_v6_preview_universe(args: argparse.Namespace) -> int:
    payload = _read_alpha_v6_universe_candidate(args.input)
    result = preview_alpha_v6_universe(
        SQLiteScanStore(args.db_path, read_only=True),
        as_of_date=str(payload.get("as_of_date") or ""),
        members=payload["members"],
        source_lineage=payload["source_lineage"],
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_alpha_v6_restore_universe(args: argparse.Namespace) -> int:
    result = restore_alpha_v6_universe(
        SQLiteScanStore(args.db_path),
        universe_id=args.universe_id,
        as_of_date=args.as_of,
        operator=args.operator,
        reason=args.reason,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _read_alpha_v6_universe_candidate(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SnapshotValidationError("V6 universe candidate input must be a JSON object.")
    return validate_alpha_v6_universe_candidate(payload)


def _run_daily_heartbeat(args: argparse.Namespace) -> int:
    explicit_release_sha = str(getattr(args, "release_sha", "") or "").strip()
    if explicit_release_sha:
        if not re.fullmatch(r"[0-9a-f]{40}", explicit_release_sha):
            raise SnapshotValidationError(
                "Daily heartbeat release SHA must be one full lowercase Git SHA."
            )
        release_sha = explicit_release_sha
    else:
        release_sha = resolve_release_sha(args.runtime_root)
    result = write_heartbeat(
        state_root=args.state_root,
        market_date=args.market_date,
        stage=args.stage,
        run_id=shared_daily_run_id(args.market_date, release_sha),
        status=args.status,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_daily_orchestrator_status(args: argparse.Namespace) -> int:
    result = daily_orchestration_status(
        SQLiteScanStore(args.db_path, read_only=True),
        market_date=args.market_date,
        state_root=args.state_root,
        heartbeat_ttl_minutes=args.heartbeat_ttl_minutes,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"HEALTHY", "SKIPPED_NOT_APPLICABLE"} else 2


def _run_account_session_report(args: argparse.Namespace) -> int:
    result = build_account_session_report(
        args.db_path,
        market_date=args.market_date,
        account_id=args.account_id,
        window_days=args.window_days,
        code_sha=args.code_sha,
        experiment_id=args.experiment_id,
        arm_id=args.arm_id,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "COMPLETE" else 2


def _run_account_session_reconcile(args: argparse.Namespace) -> int:
    result = reconcile_daily_account_sessions(
        args.db_path,
        market_date=args.market_date,
        account_id=args.account_id,
        release_sha=args.release_sha,
        now=args.now,
        evidence_mode=args.evidence_mode,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "COMPLETE" else 2


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


def _run_alpha_attribution(args: argparse.Namespace) -> int:
    result = generate_alpha_attribution_report(
        db_path=args.db_path,
        out_dir=args.out_dir,
        start=args.start,
        end=args.end,
        paper_ops_root=args.paper_ops_root,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"complete", "no_evidence"} else 1


def _run_scenario_doctor(args: argparse.Namespace) -> int:
    result = scenario_doctor(db_path=args.db_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "READY" else 2


def _run_indeterminate_research(args: argparse.Namespace) -> int:
    symbols = [item.strip().upper() for item in str(args.symbols).split(",") if item.strip()]
    result = run_indeterminate_research(
        db_path=args.db_path,
        symbols=symbols,
        selection_outcome=args.selection_outcome,
        market_date=args.market_date,
        out_path=args.out,
        notify=args.notify,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") == "failed" else 0


def _run_scenario_cycle(args: argparse.Namespace) -> int:
    symbols = [item.strip().upper() for item in str(args.symbols or "").split(",") if item.strip()]
    result = run_scenario_cycle(
        db_path=args.db_path,
        symbols=symbols or None,
        since=args.since,
        until=args.until,
        dry_run=args.dry_run,
        notify=args.notify,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_scenario_replay(args: argparse.Namespace) -> int:
    symbols = [item.strip().upper() for item in str(args.symbols).split(",") if item.strip()]
    result = run_scenario_historical_replay(
        db_path=args.db_path,
        symbols=symbols,
        start=args.start,
        end=args.end,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_scenario_finalize(args: argparse.Namespace) -> int:
    result = finalize_scenario_performance(db_path=args.db_path, market_date=args.market_date)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_scenario_close(args: argparse.Namespace) -> int:
    result = close_open_scenario_positions(
        db_path=args.db_path,
        market_date=args.market_date,
        requested_at=args.at,
        source=args.source,
        notify=args.notify,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_scenario_report(args: argparse.Namespace) -> int:
    print(json.dumps(scenario_public_snapshot(db_path=args.db_path, limit=args.limit), indent=2))
    return 0


def _run_alpha_alert_replay(args: argparse.Namespace) -> int:
    result = write_alpha_alert_replay_report(db_path=args.db_path, out_path=args.out)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 2


def _run_outcome_gap(args: argparse.Namespace) -> int:
    result = outcome_gap_report(
        db_path=args.db_path,
        market_date=args.market_date,
        out_path=args.out,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"COMPLETE", "NO_ELIGIBLE"} else 2


def _run_alpha_eod_gate(args: argparse.Namespace) -> int:
    result = evaluate_alpha_eod_gate(
        db_path=args.db_path,
        market_date=args.market_date,
        capture_exit_code=args.capture_exit_code,
        capture_result_path=args.capture_result,
        outcome_gap_path=args.outcome_gap,
        out_path=args.out,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"COMPLETE", "NO_ELIGIBLE"} else 2


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
    store = SQLiteScanStore(config.database_path) if args.persist else None
    if store is not None:
        record_health_check(store, provider="alpaca", check=provider.validate_credentials)
    result = ScanService(
        provider, store=store, enrichment_providers=_enrichment_providers(args)
    ).run(config, symbols=selection.symbols, persist=args.persist)
    if store is not None:
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
    store = SQLiteScanStore(config.database_path, read_only=not args.persist)
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
    store = SQLiteScanStore(config.database_path, read_only=not args.persist)
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
    snapshots = _load_monitor_snapshots(
        args,
        config,
        store,
        ranked_rows,
        persist=args.persist,
    )
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
    # Keep a wall-clock schedule separate from the work duration.  If a
    # provider or process stalls, every elapsed slot is recorded explicitly;
    # no missing slot is turned into a market-data result.
    next_due = datetime.now(UTC)
    try:
        while True:
            status = _run_monitor_setups(args)
            if status != 0:
                return status
            iterations += 1
            next_due += timedelta(seconds=interval_seconds)
            observed_at = datetime.now(UTC)
            _persist_monitor_interval_gaps(args, next_due, observed_at, interval_seconds)
            # Keep the newest overdue slot as the next work item.  Older
            # slots are receipts only once a complete interval has elapsed;
            # the newest slot is run immediately instead of being skipped.
            while next_due + timedelta(seconds=interval_seconds) <= observed_at:
                next_due += timedelta(seconds=interval_seconds)
            if args.max_iterations is not None and iterations >= int(args.max_iterations):
                return 0
            time.sleep(max(0.0, (next_due - datetime.now(UTC)).total_seconds()))
    except KeyboardInterrupt:
        print("Monitor loop stopped.")
        return 0


def _run_monitor_gap(args: argparse.Namespace) -> int:
    if int(args.interval_seconds) <= 0:
        raise SnapshotValidationError("monitor gap interval must be positive")
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    store = SQLiteScanStore(config.database_path)
    receipt = monitor_interval_gap_receipt(
        expected_at=args.expected_at,
        observed_at=args.observed_at,
        interval_seconds=int(args.interval_seconds),
        market_date=args.market_date,
        run_id=args.run_id,
        schedule_id=args.schedule_id,
        release_sha=args.release_sha,
    )
    stats = store.persist_monitor_interval_gap_receipts([receipt])
    print(json.dumps({"receipt": receipt, "persistence": stats}, sort_keys=True))
    return 0


def _persist_monitor_interval_gaps(
    args: argparse.Namespace,
    first_due: datetime,
    observed_at: datetime,
    interval_seconds: int,
) -> int:
    """Persist all slots missed before ``observed_at`` with stable identities."""

    if not bool(getattr(args, "persist", False)):
        return 0
    # Manual/sample monitor runs persist their normal monitor output but do
    # not claim schedule coverage.  Gap receipts are an explicit lineage mode
    # and require all release/date/schedule fields below.
    if not bool(getattr(args, "persist_interval_gaps", False)):
        return 0
    release_sha = str(getattr(args, "release_sha", "") or "")
    if not release_sha:
        raise SnapshotValidationError(
            "persisted monitor interval gap persistence requires --release-sha "
            "with the exact runtime HEAD"
        )
    market_date = str(getattr(args, "market_date", "") or "")
    if not market_date:
        raise SnapshotValidationError(
            "persisted monitor interval gap persistence requires explicit --market-date"
        )
    schedule_id = str(getattr(args, "schedule_id", "") or "")
    if not schedule_id:
        raise SnapshotValidationError("persisted monitor interval gaps require --schedule-id")
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    store = SQLiteScanStore(config.database_path)
    receipts: list[dict[str, Any]] = []
    due = first_due
    while due + timedelta(seconds=interval_seconds) <= observed_at:
        receipts.append(
            monitor_interval_gap_receipt(
                expected_at=due.isoformat(),
                observed_at=observed_at.isoformat(),
                interval_seconds=interval_seconds,
                release_sha=release_sha,
                market_date=market_date,
                schedule_id=schedule_id,
            )
        )
        due += timedelta(seconds=interval_seconds)
    if not receipts:
        return 0
    return int(store.persist_monitor_interval_gap_receipts(receipts)["inserted"])


def _run_monitor_open(args: argparse.Namespace) -> int:
    if args.continuous:
        args.max_iterations = None
    return _run_monitor_loop(args)


def _load_monitor_snapshots(
    args: argparse.Namespace,
    config: Any,
    store: SQLiteScanStore,
    ranked_rows: list[dict[str, Any]],
    *,
    persist: bool,
) -> list[Any]:
    provider_name = str(getattr(args, "provider", "csv"))
    symbols = _monitor_symbols(ranked_rows, args, config)
    if provider_name == "alpaca":
        provider = AlpacaProvider(config)
        if persist:
            record_health_check(store, provider="alpaca", check=provider.validate_credentials)
        snapshots = provider.get_premarket_snapshot(symbols, config)
        if persist:
            record_health_status(
                store,
                provider="alpaca",
                status="ok",
                detail=f"loaded live monitor snapshot rows={len(snapshots)}",
            )
        return snapshots
    snapshots = read_snapshot_csv(args.snapshot)
    if persist:
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


def _run_price_observe(args: argparse.Namespace) -> int:
    tickers = [item.strip().upper() for item in str(args.tickers or "").split(",") if item.strip()]
    config = load_config(database_path=Path(args.db_path))
    result = collect_price_observations(
        db_path=args.db_path,
        source=args.source,
        tickers=tickers or None,
        market_date=args.market_date,
        requested_at=args.at,
        minute_bars=args.minute_bars,
        max_age_seconds=args.max_age_seconds,
        persist=not args.no_persist,
        config=config,
    )
    printable = dict(result)
    printable["observations"] = [
        {
            "ticker": row.get("ticker"),
            "price": row.get("price"),
            "observed_at": row.get("observed_at"),
            "provider_status": row.get("provider_status"),
            "freshness_seconds": row.get("freshness_seconds"),
            "is_usable": row.get("is_usable"),
        }
        for row in list(result.get("observations") or [])
    ]
    print(json.dumps(printable, indent=2, sort_keys=True))
    return 0


def _run_trade_watch(args: argparse.Namespace) -> int:
    result = run_trade_watcher(**_trade_watch_kwargs(args))
    printable = dict(result)
    printable["states"] = list(result.get("states") or [])[:20]
    printable["intents"] = list(result.get("intents") or [])[:20]
    printable["paper_positions"] = list(result.get("paper_positions") or [])[:20]
    printable["paper_fills"] = list(result.get("paper_fills") or [])[:20]
    print(json.dumps(printable, indent=2, sort_keys=True))
    return 0


def _run_trade_watch_loop(args: argparse.Namespace) -> int:
    if args.interval_seconds <= 0:
        raise SnapshotValidationError("--interval-seconds must be positive.")
    iteration = 0
    while True:
        iteration += 1
        kwargs = _trade_watch_kwargs(args)
        if args.at is None:
            kwargs["requested_at"] = None
        result = run_trade_watcher(**kwargs)
        summary = {
            "iteration": iteration,
            "status": result.get("status"),
            "mode": result.get("mode"),
            "market_date": result.get("market_date"),
            "requested_at": result.get("requested_at"),
            "usable_prices": dict(result.get("price_observation") or {}).get("usable_count"),
            "intent_inserted": dict(result.get("intent_stats") or {}).get("inserted", 0),
            "paper_fills": dict(result.get("paper_fill_stats") or {}).get("inserted", 0),
            "notifications": result.get("notification_stats"),
        }
        print(json.dumps(summary, sort_keys=True))
        if args.max_iterations and iteration >= args.max_iterations:
            break
        time.sleep(float(args.interval_seconds))
    return 0


def _trade_watch_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    tickers = [item.strip().upper() for item in str(args.tickers or "").split(",") if item.strip()]
    return {
        "db_path": args.db_path,
        "mode": args.mode,
        "source": args.source,
        "tickers": tickers or None,
        "market_date": args.market_date,
        "requested_at": args.at,
        "minute_bars": args.minute_bars,
        "max_age_seconds": args.max_age_seconds,
        "notify": args.notify,
        "dry_run": args.dry_run,
        "notional_per_trade": args.notional_per_trade,
        "simulated_equity": args.simulated_equity,
        "max_open_positions": args.max_open_positions,
        "max_daily_entries": args.max_daily_entries,
        "min_reward_risk": args.min_reward_risk,
        "notify_blocked": args.notify_blocked,
        "include_scenarios": args.include_scenarios,
        "expected_code_sha": getattr(args, "expected_code_sha", None),
        "observation_bundle_path": getattr(args, "observation_bundle", None),
        "cycle_id": getattr(args, "cycle_id", None),
    }


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


def _run_canonical_performance_reconcile(args: argparse.Namespace) -> int:
    argv = [
        "--db-path",
        args.db_path,
        "--paper-ops-root",
        args.paper_ops_root,
    ]
    if args.as_of:
        argv.extend(["--as-of", args.as_of])
    if args.persist:
        argv.append("--persist")
    if args.print_result:
        argv.append("--print")
    return performance_reconcile_main(argv)


def _run_release_doctor(result: dict[str, Any], *, require_local_verification: bool = False) -> int:
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if require_local_verification:
        return 0 if result.get("status") == "LOCAL_VERIFIED" else 2
    return 0 if result.get("status") not in {"FAILED"} else 2


def _run_performance_report(args: argparse.Namespace) -> int:
    config = load_config(database_path=Path(args.db_path) if args.db_path else None)
    store = SQLiteScanStore(config.database_path, read_only=not args.persist)
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
        print(f"{row['time_ct']} CT | {row['name']} | {row['command']} | {row['description']}")
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
