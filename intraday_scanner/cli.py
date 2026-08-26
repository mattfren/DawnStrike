"""Command-line interface for scanner operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
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
from intraday_scanner.performance.cli import main as performance_reconcile_main
from intraday_scanner.performance.strategy_miss_attribution import (
    attribute_strategy_misses,
    load_portfolio_performance_rows_readonly,
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
    MappingEvidenceAnalyzer,
    StrategyEvidenceAnalyzer,
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
from intraday_scanner.services.setup_monitor import run_setup_monitor
from intraday_scanner.services.strategy_challenger_backtest_service import (
    run_strategy_challenger_backtest,
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
        "--core-universe-manifest",
        default=None,
        help="Governed JSON S&P 500/Nasdaq-100 manifest (absent remains DATA_UNAVAILABLE)",
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
        "--core-universe-manifest",
        default=None,
        help="Governed JSON S&P 500/Nasdaq-100 manifest (absent remains DATA_UNAVAILABLE)",
    )

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
        help="Optional JSON mapping keyed by strategy ID for injected evidence/proposals",
    )
    daily_strategy_evidence.add_argument(
        "--db-path",
        default=None,
        help=(
            "Optional SQLite database read with mode=ro and PRAGMA query_only; "
            "attributes retained portfolio performance rows through market-date"
        ),
    )

    strategy_challenger_backtest_parser = subparsers.add_parser(
        "strategy-challenger-backtest",
        help="Compare all catalog strategies and research challengers on verified DataTruth",
    )
    strategy_challenger_backtest_parser.add_argument("--data-truth-root", required=True)
    strategy_challenger_backtest_parser.add_argument("--snapshot-id", default=None)
    strategy_challenger_backtest_parser.add_argument("--code-sha", required=True)
    strategy_challenger_backtest_parser.add_argument("--out", required=True)

    alpha_v6_train_weekly_parser = subparsers.add_parser(
        "alpha-v6-train-weekly",
        help="Run the separately scheduled V6 refit and all-family OOF evaluation",
    )
    alpha_v6_train_weekly_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    alpha_v6_train_weekly_parser.add_argument("--code-sha", default="unresolved-local-sha")
    alpha_v6_train_weekly_parser.add_argument("--market-date", default=None)

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

    daily_status_parser = subparsers.add_parser(
        "daily-orchestrator-status",
        help="Report stale heartbeats and missing/failed daily DAG stages",
    )
    daily_status_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    daily_status_parser.add_argument("--state-root", required=True)
    daily_status_parser.add_argument("--market-date", required=True)
    daily_status_parser.add_argument("--heartbeat-ttl-minutes", type=int, default=30)

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
    monitor_loop.add_argument("--max-iterations", type=int, default=None)
    monitor_loop.add_argument(
        "--news-provider", choices=["none", "auto", "newsapi", "finnhub"], default="none"
    )
    monitor_loop.add_argument("--sec-rss", action="store_true")

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
        if args.command == "strategy-challenger-backtest":
            return _run_strategy_challenger_backtest(args)
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
    except (ConfigError, DataProviderError, SnapshotValidationError, StorageError) as exc:
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
    result = run_alpha_v6_daily_monitor(SQLiteScanStore(args.db_path), market_date=args.market_date)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_strategy_learning_daily(args: argparse.Namespace) -> int:
    analyzer: StrategyEvidenceAnalyzer | None = None
    input_hash_sha256: str | None = None
    if args.evidence_file:
        payload = json.loads(Path(args.evidence_file).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SnapshotValidationError("strategy learning evidence must be a JSON object")
        analyzer = MappingEvidenceAnalyzer(payload)
        input_hash_sha256 = _hash_strategy_learning_inputs(
            evidence_payload=payload,
        )
    elif args.db_path:
        rows = load_portfolio_performance_rows_readonly(
            args.db_path,
            date_cutoff=args.market_date,
        )
        paper_ops_rows = None
        if args.paper_ops_root:
            from intraday_scanner.v2.paper_ops.trade_blotter import load_trade_blotter_readonly

            paper_ops_rows = load_trade_blotter_readonly(
                output_root=Path(args.paper_ops_root),
                mode="forward",
            )
        input_hash_sha256 = _hash_strategy_learning_inputs(
            database_rows=rows,
            paper_ops_rows=paper_ops_rows,
            paper_ops_root=Path(args.paper_ops_root) if args.paper_ops_root else None,
        )
        analyzer = AttributionReportAnalyzer(
            attribute_strategy_misses(
                rows,
                date_cutoff=args.cutoff,
                paper_ops_rows=paper_ops_rows,
            )
        )
    result = run_daily_strategy_learning(
        market_date=args.market_date,
        cutoff=args.cutoff,
        source_identity=args.source_identity,
        source_hash_sha256=args.source_hash_sha256,
        code_sha=args.code_sha,
        out_dir=args.out_dir,
        input_hash_sha256=input_hash_sha256,
        analyzer=analyzer,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "complete" else 1


def _hash_strategy_learning_inputs(
    *,
    database_rows: Sequence[Mapping[str, Any]] | None = None,
    paper_ops_rows: Sequence[Mapping[str, Any]] | None = None,
    paper_ops_root: Path | None = None,
    evidence_payload: Mapping[str, Any] | None = None,
) -> str | None:
    """Bind reuse to the exact evidence objects consumed by the analyzer.

    Hashing the in-memory rows avoids a database/WAL time-of-check/time-of-use
    gap and excludes mutable storage layout from the immutable receipt.  The
    read-only PaperOps materializer hash is carried on each row and included
    here, so registry/config/manifest bytes still participate in the identity.
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

        paper_hash = hash_trade_blotter_readonly_inputs(paper_ops_root)
        parts.append(("paper_ops_materializer_inputs", paper_hash.encode("ascii")))
    if evidence_payload is not None:
        parts.append(("evidence_payload", _canonical_input_bytes(evidence_payload)))
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
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _run_strategy_challenger_backtest(args: argparse.Namespace) -> int:
    result = run_strategy_challenger_backtest(
        data_truth_root=args.data_truth_root,
        snapshot_id=args.snapshot_id,
        code_sha=args.code_sha,
        out_path=args.out,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "complete" else 1


def _run_alpha_v6_train_weekly(args: argparse.Namespace) -> int:
    result = run_alpha_v6_weekly_training(
        SQLiteScanStore(args.db_path), code_sha=args.code_sha, market_date=args.market_date
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
    return 0 if result.get("status") == "HEALTHY" else 2


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
