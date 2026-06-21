"""End-to-end notification-only automation for Free Shadow Mode."""

from __future__ import annotations

import csv
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from intraday_scanner.config import load_config
from intraday_scanner.errors import DataProviderError, NotificationError
from intraday_scanner.models import SNAPSHOT_COLUMNS, utc_now_iso
from intraday_scanner.notifiers import ConsoleNotifier, NotificationEvent, dispatch_events
from intraday_scanner.notifiers.email import EmailNotifier
from intraday_scanner.notifiers.telegram_formatter import (
    format_daily_summary,
    format_outcome_needed,
)
from intraday_scanner.notifiers.webhooks import DiscordWebhookNotifier, TelegramNotifier
from intraday_scanner.notifiers.windows import WindowsLocalNotifier
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider, read_snapshot_csv
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.services.alert_service import alerts_from_monitor_rows, persist_deduped_alerts
from intraday_scanner.services.free_shadow_mode import (
    OUTCOME_COLUMNS,
    audit_manual_outcomes,
    build_free_shadow_report,
    import_manual_outcomes,
)
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.services.screener_automation import (
    SUPPORTED_SUFFIXES,
    file_sha256,
    normalize_screener_file,
)
from intraday_scanner.services.setup_monitor import run_setup_monitor
from intraday_scanner.services.time_utils import get_market_date
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

DEFAULT_CONFIG_PATH = Path("config/automation.example.yaml")
OUTCOME_INBOX = Path("data/inbox/outcomes")
OUTCOME_PROCESSED = Path("data/processed/outcomes")
OUTCOME_FAILED = Path("data/failed/outcomes")

AUTOMATION_EVENT_TYPES = {
    "automation_started",
    "source_found",
    "source_failed",
    "normalization_failed",
    "scan_completed",
    "top_picks",
    "avoid_warning",
    "monitor_started",
    "monitor_alert",
    "outcome_missing",
    "lunch_reminder",
    "close_reminder",
    "audit_completed",
    "daily_summary",
    "automation_failed",
}


@dataclass(frozen=True)
class ScreenerSourceConfig:
    name: str
    type: str
    path: str = ""
    url: str = ""
    enabled: bool = False
    allowed_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutomationConfig:
    timezone: str
    market_timezone: str
    db_path: Path
    out_root: Path
    notification_channels: tuple[str, ...]
    screener_sources: tuple[ScreenerSourceConfig, ...]
    normalizer_preferred: str
    normalizer_fallback: str
    schedule: dict[str, Any]
    monitor: dict[str, Any]
    outcomes: dict[str, Any]
    notifications: dict[str, Any]


@dataclass(frozen=True)
class SourceResult:
    status: str
    name: str = ""
    kind: str = ""
    path: Path | None = None
    url: str = ""
    file_hash: str = ""
    message: str = ""


def load_automation_config(
    config_path: str | Path | None = None,
    *,
    db_path: str | Path | None = None,
    out_root: str | Path | None = None,
) -> AutomationConfig:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists() and path != DEFAULT_CONFIG_PATH:
        path = DEFAULT_CONFIG_PATH
    if not path.exists():
        data = _default_config_data()
    else:
        data = _load_simple_yaml(path)
    sources = tuple(
        ScreenerSourceConfig(
            name=str(row.get("name") or ""),
            type=str(row.get("type") or ""),
            path=str(row.get("path") or ""),
            url=str(row.get("url") or ""),
            enabled=_bool(row.get("enabled")),
            allowed_domains=tuple(str(item) for item in row.get("allowed_domains", []) or []),
        )
        for row in list(data.get("screener_sources") or [])
    )
    normalizer = dict(data.get("normalizer") or {})
    return AutomationConfig(
        timezone=str(data.get("timezone") or "America/Chicago"),
        market_timezone=str(data.get("market_timezone") or "America/New_York"),
        db_path=Path(db_path or data.get("db_path") or "data/shadow_real.sqlite"),
        out_root=Path(out_root or data.get("out_root") or "outputs/automation"),
        notification_channels=tuple(
            str(channel).lower()
            for channel in list(data.get("notification_channels") or ["console"])
        ),
        screener_sources=sources,
        normalizer_preferred=str(normalizer.get("preferred") or "deterministic"),
        normalizer_fallback=str(normalizer.get("fallback") or "none"),
        schedule=dict(data.get("schedule") or {}),
        monitor=dict(data.get("monitor") or {}),
        outcomes=dict(data.get("outcomes") or {}),
        notifications=dict(data.get("notifications") or {}),
    )


def ensure_automation_directories(config: AutomationConfig, run_date: str | None = None) -> None:
    paths = [
        Path("data/inbox/screener"),
        Path("data/processed/screener"),
        Path("data/failed/screener"),
        OUTCOME_INBOX,
        OUTCOME_PROCESSED,
        OUTCOME_FAILED,
        Path("logs"),
        config.out_root,
    ]
    if run_date:
        paths.extend(
            [
                config.out_root / run_date,
                config.out_root / run_date / "morning",
                config.out_root / run_date / "monitor",
                config.out_root / run_date / "outcomes",
                config.out_root / run_date / "summary",
            ]
        )
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def discover_screener_source(config: AutomationConfig, run_date: str | None = None) -> SourceResult:
    ensure_automation_directories(config, run_date)
    for source in config.screener_sources:
        if not source.enabled:
            continue
        if source.type == "inbox":
            inbox = Path(source.path or "data/inbox/screener")
            latest = _latest_file(_pending_files(inbox))
            if latest is not None:
                return SourceResult(
                    status="found",
                    name=source.name,
                    kind="inbox",
                    path=latest,
                    file_hash=file_sha256(latest),
                )
        elif source.type == "url" and source.url:
            return SourceResult(
                status="found_url",
                name=source.name,
                kind="url",
                url=source.url,
            )
    return SourceResult(
        status="missing",
        message="No enabled local screener file or configured URL source was available.",
    )


def automation_morning(
    *,
    config_path: str | Path | None = None,
    db_path: str | Path | None = None,
    out_root: str | Path | None = None,
    run_date: str | None = None,
    notify: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    config = load_automation_config(config_path, db_path=db_path, out_root=out_root)
    run_date = run_date or get_market_date(config.timezone)
    ensure_automation_directories(config, run_date)
    store = SQLiteScanStore(config.db_path)
    started_at = utc_now_iso()
    run_id = str(uuid.uuid4())
    out_dir = config.out_root / run_date / "morning"
    source_hash = ""
    _send(
        [
            _event(
                run_date,
                "automation_started",
                "Morning scan started",
                "Dawnstrike automation started.",
            )
        ],
        config,
        store,
        notify=notify,
        dry_run=dry_run,
    )
    try:
        source = discover_screener_source(config, run_date)
        if source.status == "missing":
            body = "No screener file/source was available. Drop a CSV into data\\inbox\\screener."
            _send(
                [
                    _event(run_date, "source_failed", "No screener source", body, severity="high"),
                    _event(
                        run_date,
                        "automation_failed",
                        "Morning scan failed",
                        body,
                        severity="high",
                    ),
                ],
                config,
                store,
                notify=notify,
                dry_run=dry_run,
            )
            result = _run_payload(
                run_id,
                "morning",
                "no_data",
                started_at,
                out_dir,
                {"message": body},
            )
            _persist_run(store, result)
            store.record_provider_health("automation:source", "failed", utc_now_iso(), body)
            return result
        source_path = _materialize_source(source, config, run_date)
        source_hash = file_sha256(source_path)
        _send(
            [
                _event(
                    run_date,
                    "source_found",
                    "Screener source found",
                    f"Using {source.kind} source {source_path}.",
                    source_hash=source_hash,
                )
            ],
            config,
            store,
            notify=notify,
            dry_run=dry_run,
        )
        ai_normalizer = _ai_normalizer(config)
        normalized = normalize_screener_file(
            input_path=source_path,
            out_dir=out_dir,
            ai_normalizer=ai_normalizer,
            store=store,
        )
        snapshot_path = Path(normalized["paths"]["snapshot"])
        if source.kind == "url":
            _relabel_url_snapshot(snapshot_path, source.url)
        scan_config = load_config(
            provider="csv",
            output_dir=out_dir,
            database_path=config.db_path,
            notifier_channels=",".join(config.notification_channels),
        )
        scan_result = ScanService(CsvSnapshotProvider(snapshot_path), store=store).run(
            scan_config, persist=False
        )
        scan_result.config.update(
            {
                "data_source_kind": "url_ingest" if source.kind == "url" else "manual",
                "shadow_mode": True,
                "manual_uploaded_data": source.kind != "url",
                "paid_data": False,
                "fixture_only": False,
                "automation_run_id": run_id,
                "automation_source": source.name,
                "source_file_hash": source_hash,
            }
        )
        store.persist_scan_result(scan_result)
        paths = write_scan_outputs(scan_result, out_dir)
        summary = {
            "run_id": run_id,
            "run_type": "morning",
            "status": "success",
            "started_at": started_at,
            "completed_at": utc_now_iso(),
            "date": run_date,
            "out_dir": str(out_dir),
            "source": _source_payload(source),
            "source_hash": source_hash,
            "official_call_timestamp": scan_result.created_at,
            "snapshot_path": str(snapshot_path),
            "paths": {key: str(value) for key, value in paths.items()},
            "scan_summary": scan_result.summary(),
            "normalization": normalized["summary"],
        }
        _write_json(out_dir / "run_summary.json", summary)
        _persist_run(store, summary)
        _archive_source(source_path, Path("data/processed/screener"), "processed")
        store.record_provider_health(
            "automation:morning",
            "ok",
            utc_now_iso(),
            "morning scan complete",
        )
        _send(
            _morning_events(run_date, scan_result.summary(), paths, source_hash),
            config,
            store,
            notify=notify,
            dry_run=dry_run,
        )
        return summary
    except Exception as exc:
        failure = _run_payload(
            run_id,
            "morning",
            "failed",
            started_at,
            out_dir,
            {"error": str(exc), "source_hash": source_hash},
        )
        _write_json(out_dir / "error_report.json", failure)
        _persist_run(store, failure)
        store.record_provider_health("automation:morning", "failed", utc_now_iso(), str(exc))
        _send(
            [
                _event(
                    run_date,
                    "automation_failed",
                    "Morning automation failed",
                    str(exc),
                    severity="critical",
                )
            ],
            config,
            store,
            notify=notify,
            dry_run=dry_run,
        )
        raise


def automation_monitor_open(
    *,
    config_path: str | Path | None = None,
    db_path: str | Path | None = None,
    out_root: str | Path | None = None,
    run_date: str | None = None,
    snapshot: str | Path | None = None,
    max_iterations: int = 1,
    notify: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    config = load_automation_config(config_path, db_path=db_path, out_root=out_root)
    run_date = run_date or get_market_date(config.timezone)
    ensure_automation_directories(config, run_date)
    store = SQLiteScanStore(config.db_path)
    started_at = utc_now_iso()
    run_id = str(uuid.uuid4())
    out_dir = config.out_root / run_date / "monitor"
    latest = store.load_latest_scan()
    _send(
        [
            _event(
                run_date,
                "monitor_started",
                "Market-open monitor started",
                "Checking saved official calls.",
            )
        ],
        config,
        store,
        notify=notify,
        dry_run=dry_run,
    )
    if not latest:
        return _monitor_manual_required(
            store,
            config,
            run_date,
            run_id,
            started_at,
            out_dir,
            "No official calls exist yet.",
            notify,
            dry_run,
        )
    if snapshot is None:
        return _monitor_manual_required(
            store,
            config,
            run_date,
            run_id,
            started_at,
            out_dir,
            "No reliable current-price source is configured; manual monitor required.",
            notify,
            dry_run,
        )
    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for _ in range(max(1, max_iterations)):
        rows_snapshot = read_snapshot_csv(snapshot)
        ranked_rows = cast(list[dict[str, Any]], latest.get("ranked_candidates") or [])
        summary_payload = cast(dict[str, Any], latest.get("summary") or {})
        source_run_id = str(summary_payload.get("run_id") or latest.get("run_id") or "")
        result = run_setup_monitor(
            candidates=ranked_rows,
            snapshots=rows_snapshot,
            out_dir=out_dir,
            store=store,
            persist=True,
            source_run_id=source_run_id,
            top_n=int(config.monitor.get("max_symbols") or 5),
        )
        rows = list(result.get("rows") or [])
        summary = dict(result.get("summary") or {})
        alerts = alerts_from_monitor_rows(rows, run_id=source_run_id)
        persist_deduped_alerts(store, alerts, run_id=source_run_id)
        _send(
            [
                _event(
                    run_date,
                    "monitor_alert",
                    alert.title,
                    alert.body,
                    ticker=alert.ticker,
                    severity=alert.severity,
                )
                for alert in alerts
            ],
            config,
            store,
            notify=notify,
            dry_run=dry_run,
        )
    payload = _run_payload(
        run_id,
        "monitor_open",
        "success",
        started_at,
        out_dir,
        {"summary": summary, "row_count": len(rows)},
    )
    _persist_run(store, payload)
    return payload


def automation_outcomes(
    *,
    config_path: str | Path | None = None,
    db_path: str | Path | None = None,
    out_root: str | Path | None = None,
    run_date: str | None = None,
    notify: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    config = load_automation_config(config_path, db_path=db_path, out_root=out_root)
    run_date = run_date or get_market_date(config.timezone)
    ensure_automation_directories(config, run_date)
    store = SQLiteScanStore(config.db_path)
    started_at = utc_now_iso()
    run_id = str(uuid.uuid4())
    out_dir = config.out_root / run_date / "outcomes"
    source = _latest_outcome_file(Path(config.outcomes.get("inbox") or OUTCOME_INBOX))
    if source is None:
        return _send_outcome_reminder(
            store,
            config,
            run_date,
            run_id,
            started_at,
            out_dir,
            notify,
            dry_run,
        )
    try:
        imported = import_manual_outcomes(input_path=source, store=store, persist=True)
        audit = audit_manual_outcomes(store=store, out_dir=out_dir / "audit", persist=True)
        report = build_free_shadow_report(
            store=store,
            out_dir=out_dir / "shadow_report",
            persist=True,
        )
        archive = _archive_source(source, OUTCOME_PROCESSED, "processed")
        payload = _run_payload(
            run_id,
            "outcomes",
            "success",
            started_at,
            out_dir,
            {
                "input_path": str(source),
                "archive_path": str(archive),
                "imported": {key: value for key, value in imported.items() if key != "rows"},
                "audit_summary": audit["summary"],
                "shadow_report": report["report"],
            },
        )
        _write_json(out_dir / "outcome_summary.json", payload)
        _persist_run(store, payload)
        _send(
            [
                _event(
                    run_date,
                    "audit_completed",
                    "Outcome audit completed",
                    _audit_body(audit["summary"]),
                ),
                _event(
                    run_date,
                    "daily_summary",
                    "Daily shadow report updated",
                    _summary_body(report["report"]),
                    source_hash=str(report["report"].get("created_at") or "outcomes"),
                ),
            ],
            config,
            store,
            notify=notify,
            dry_run=dry_run,
        )
        return payload
    except Exception as exc:
        archive_path = (
            str(_archive_source(source, OUTCOME_FAILED, "failed")) if source.exists() else ""
        )
        payload = _run_payload(
            run_id,
            "outcomes",
            "failed",
            started_at,
            out_dir,
            {"error": str(exc), "archive_path": archive_path},
        )
        _write_json(out_dir / "error_report.json", payload)
        _persist_run(store, payload)
        _send(
            [
                _event(
                    run_date,
                    "automation_failed",
                    "Outcome automation failed",
                    str(exc),
                    severity="critical",
                )
            ],
            config,
            store,
            notify=notify,
            dry_run=dry_run,
        )
        raise


def automation_summary(
    *,
    config_path: str | Path | None = None,
    db_path: str | Path | None = None,
    out_root: str | Path | None = None,
    run_date: str | None = None,
    notify: bool = False,
    dry_run: bool = False,
    dashboard_url: str = "http://127.0.0.1:8502/",
) -> dict[str, Any]:
    config = load_automation_config(config_path, db_path=db_path, out_root=out_root)
    run_date = run_date or get_market_date(config.timezone)
    ensure_automation_directories(config, run_date)
    store = SQLiteScanStore(config.db_path)
    started_at = utc_now_iso()
    latest = store.load_latest_scan() or {}
    shadow_report = store.load_latest_shadow_report() or {}
    manual_outcomes = store.load_manual_outcomes()
    ranked = cast(list[dict[str, Any]], latest.get("ranked_candidates") or [])
    out_dir = config.out_root / run_date / "summary"
    payload = _run_payload(
        str(uuid.uuid4()),
        "summary",
        "success",
        started_at,
        out_dir,
        {
            "date": run_date,
            "source_kind": _source_kind(latest),
            "top_1": [row.get("ticker") for row in ranked[:1]],
            "top_3": [row.get("ticker") for row in ranked[:3]],
            "top_5": [row.get("ticker") for row in ranked[:5]],
            "outcomes_available": bool(manual_outcomes),
            "missing_outcome_count": max(0, len(ranked) - len(manual_outcomes)),
            "shadow_report": shadow_report,
            "dashboard_url": dashboard_url,
            "output_paths": {
                "root": str(config.out_root / run_date),
                "morning": str(config.out_root / run_date / "morning"),
                "outcomes": str(config.out_root / run_date / "outcomes"),
            },
        },
    )
    _write_json(out_dir / "daily_summary.json", payload)
    _persist_run(store, payload)
    _send(
        [
            _event(
                run_date,
                "daily_summary",
                "Dawnstrike daily summary",
                _daily_body(payload),
                source_hash="outcomes" if manual_outcomes else "missing_outcomes",
            )
        ],
        config,
        store,
        notify=notify,
        dry_run=dry_run,
    )
    return payload


def automation_run(
    *,
    mode: str,
    config_path: str | Path | None = None,
    db_path: str | Path | None = None,
    out_root: str | Path | None = None,
    run_date: str | None = None,
    notify: bool = False,
    max_cycles: int | None = None,
    poll_seconds: int = 60,
) -> dict[str, Any]:
    if mode == "once":
        morning = automation_morning(
            config_path=config_path,
            db_path=db_path,
            out_root=out_root,
            run_date=run_date,
            notify=notify,
        )
        monitor = automation_monitor_open(
            config_path=config_path,
            db_path=db_path,
            out_root=out_root,
            run_date=run_date,
            notify=notify,
        )
        outcomes = automation_outcomes(
            config_path=config_path,
            db_path=db_path,
            out_root=out_root,
            run_date=run_date,
            notify=notify,
        )
        summary = automation_summary(
            config_path=config_path,
            db_path=db_path,
            out_root=out_root,
            run_date=run_date,
            notify=notify,
        )
        return {
            "status": "complete",
            "mode": mode,
            "morning": morning,
            "monitor": monitor,
            "outcomes": outcomes,
            "summary": summary,
        }
    if mode == "dry-run":
        return automation_daemon(
            config_path=config_path,
            db_path=db_path,
            out_root=out_root,
            run_date=run_date,
            notify=notify,
            dry_run=True,
            max_cycles=max_cycles or 1,
            poll_seconds=poll_seconds,
        )
    if mode == "daemon":
        return automation_daemon(
            config_path=config_path,
            db_path=db_path,
            out_root=out_root,
            run_date=run_date,
            notify=notify,
            max_cycles=max_cycles,
            poll_seconds=poll_seconds,
        )
    raise DataProviderError(f"Unsupported automation-run mode: {mode}")


def automation_daemon(
    *,
    config_path: str | Path | None = None,
    db_path: str | Path | None = None,
    out_root: str | Path | None = None,
    run_date: str | None = None,
    notify: bool = False,
    dry_run: bool = False,
    max_cycles: int | None = None,
    poll_seconds: int = 60,
) -> dict[str, Any]:
    config = load_automation_config(config_path, db_path=db_path, out_root=out_root)
    run_date = run_date or get_market_date(config.timezone)
    ensure_automation_directories(config, run_date)
    store = SQLiteScanStore(config.db_path)
    log_path = Path("logs") / f"automation_{run_date}.log"
    cycles = 0
    attempts: list[dict[str, Any]] = []
    while True:
        cycles += 1
        if dry_run:
            payload = {
                "status": "dry_run",
                "date": run_date,
                "market_day": _is_market_day(run_date),
                "planned": ["morning", "monitor_open", "outcomes", "summary"],
            }
            attempts.append(payload)
            _append_log(log_path, payload)
        elif not _is_market_day(run_date):
            payload = {"status": "skipped_non_market_day", "date": run_date}
            attempts.append(payload)
            store.record_provider_health(
                "automation:daemon",
                "skipped",
                utc_now_iso(),
                "non-market day",
            )
            _append_log(log_path, payload)
        else:
            try:
                morning = automation_morning(
                    config_path=config_path,
                    db_path=config.db_path,
                    out_root=config.out_root,
                    run_date=run_date,
                    notify=notify,
                )
                monitor = automation_monitor_open(
                    config_path=config_path,
                    db_path=config.db_path,
                    out_root=config.out_root,
                    run_date=run_date,
                    notify=notify,
                )
                outcomes = automation_outcomes(
                    config_path=config_path,
                    db_path=config.db_path,
                    out_root=config.out_root,
                    run_date=run_date,
                    notify=notify,
                )
                summary = automation_summary(
                    config_path=config_path,
                    db_path=config.db_path,
                    out_root=config.out_root,
                    run_date=run_date,
                    notify=notify,
                )
                attempts.append(
                    {
                        "status": "success",
                        "morning": morning.get("status"),
                        "monitor": monitor.get("status"),
                        "outcomes": outcomes.get("status"),
                        "summary": summary.get("status"),
                    }
                )
            except Exception as exc:
                attempts.append({"status": "failed", "error": str(exc)})
                _send(
                    [
                        _event(
                            run_date,
                            "automation_failed",
                            "Daemon cycle failed",
                            str(exc),
                            severity="critical",
                        )
                    ],
                    config,
                    store,
                    notify=notify,
                    dry_run=dry_run,
                )
        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(max(1, poll_seconds))
    result = {
        "status": "complete",
        "run_type": "daemon",
        "dry_run": dry_run,
        "cycles": cycles,
        "log_path": str(log_path),
        "attempts": attempts,
    }
    _append_log(log_path, result)
    return result


def safe_url_ingest_screener(
    *,
    url: str,
    out_dir: str | Path,
    allowed_domains: tuple[str, ...],
    timeout_seconds: float = 10.0,
) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise DataProviderError("URL screener ingestion only allows http/https URLs.")
    host = parsed.hostname or ""
    allowed = set(allowed_domains) or {host}
    if host not in allowed:
        raise DataProviderError(f"URL host {host} is not in configured allowed_domains.")
    request = Request(url, headers={"User-Agent": "DawnstrikeResearchBot/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        html = response.read().decode("utf-8", errors="replace")
    parser = _TableParser()
    parser.feed(html)
    rows = parser.rows
    if len(rows) < 2:
        raise DataProviderError("URL ingestion found no parseable HTML table.")
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "url_screener_raw.csv"
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)
    return output_path


def automation_status(
    store: SQLiteScanStore, out_root: str | Path = "outputs/automation"
) -> dict[str, Any]:
    runs = store.load_automation_runs(limit=20)
    notifications = store.load_recent_notifications(limit=20)
    latest = runs[0] if runs else {}
    return {
        "latest_run": latest,
        "runs": runs,
        "latest_notification": notifications[0] if notifications else {},
        "notifications": notifications,
        "out_root": str(out_root),
        "logs_path": "logs",
        "missing_outcomes": _missing_outcome_tickers(store),
        "health": _automation_health(store, runs, notifications),
    }


def _materialize_source(source: SourceResult, config: AutomationConfig, run_date: str) -> Path:
    if source.kind == "inbox" and source.path is not None:
        return source.path
    if source.kind == "url":
        matching = next(
            (row for row in config.screener_sources if row.name == source.name),
            None,
        )
        allowed = matching.allowed_domains if matching else ()
        return safe_url_ingest_screener(
            url=source.url,
            out_dir=config.out_root / run_date / "source",
            allowed_domains=allowed,
        )
    raise DataProviderError("No materialized screener source was available.")


def _source_payload(source: SourceResult) -> dict[str, Any]:
    return {
        "status": source.status,
        "name": source.name,
        "kind": source.kind,
        "path": str(source.path) if source.path else "",
        "url": source.url,
        "file_hash": source.file_hash,
        "message": source.message,
    }


def _monitor_manual_required(
    store: SQLiteScanStore,
    config: AutomationConfig,
    run_date: str,
    run_id: str,
    started_at: str,
    out_dir: Path,
    reason: str,
    notify: bool,
    dry_run: bool,
) -> dict[str, Any]:
    event = {
        "ticker": "ALL",
        "event_type": "manual_monitor_required",
        "severity": "medium",
        "created_at": utc_now_iso(),
        "reason": reason,
    }
    store.persist_monitor_events([event], run_id=run_id)
    _send(
        [_event(run_date, "monitor_alert", "Manual monitor required", reason, severity="medium")],
        config,
        store,
        notify=notify,
        dry_run=dry_run,
    )
    payload = _run_payload(
        run_id,
        "monitor_open",
        "manual_required",
        started_at,
        out_dir,
        {"reason": reason},
    )
    _persist_run(store, payload)
    return payload


def _send_outcome_reminder(
    store: SQLiteScanStore,
    config: AutomationConfig,
    run_date: str,
    run_id: str,
    started_at: str,
    out_dir: Path,
    notify: bool,
    dry_run: bool,
) -> dict[str, Any]:
    ticker_limit = int(config.outcomes.get("reminder_top_n") or 3)
    tickers = _missing_outcome_tickers(store, limit=ticker_limit)
    path = Path(config.outcomes.get("inbox") or OUTCOME_INBOX) / f"outcomes_{run_date}.csv"
    body = format_outcome_needed(run_date=run_date, reminder_path=str(path), tickers=tickers)
    _write_outcome_template(path, tickers)
    events = [_event(run_date, "outcome_missing", "Outcome file missing", body, severity="medium")]
    if config.notifications.get("send_lunch_reminder", True):
        events.append(_event(run_date, "lunch_reminder", "Lunch outcome reminder", body))
    if config.notifications.get("send_close_reminder", True):
        events.append(_event(run_date, "close_reminder", "Close outcome reminder", body))
    _send(events, config, store, notify=notify, dry_run=dry_run)
    payload = _run_payload(
        run_id,
        "outcomes",
        "missing",
        started_at,
        out_dir,
        {
            "reminder_path": str(path),
            "tickers": tickers,
            "missing_tickers": tickers,
            "required_fields": OUTCOME_COLUMNS,
            "no_saved_picks": not bool(tickers),
        },
    )
    _write_json(out_dir / "outcome_reminder.json", payload)
    _persist_run(store, payload)
    return payload


def _morning_events(
    run_date: str,
    summary: dict[str, Any],
    paths: dict[str, Path],
    source_hash: str,
) -> list[NotificationEvent]:
    top = str(summary.get("top_ticker") or "none")
    ranked = int(summary.get("ranked_count") or 0)
    avoid = int(summary.get("avoid_count") or 0)
    return [
        _event(
            run_date,
            "scan_completed",
            "Morning scan completed",
            f"Official call timestamp {summary.get('created_at')}; ranked={ranked}; avoid={avoid}.",
            source_hash=source_hash,
        ),
        _event(
            run_date,
            "top_picks",
            "Top explosive picks ready",
            f"Top setup {top}. Top 10 path: {paths['ranked_candidates']}. Research/watchlist only.",
            ticker=top if top != "none" else None,
            source_hash=source_hash,
        ),
        _event(
            run_date,
            "avoid_warning",
            "Avoid list updated",
            f"{avoid} avoid/do-not-touch row(s). Path: {paths['avoid_list']}.",
            severity="medium" if avoid else "info",
            source_hash=source_hash,
        ),
    ]


def _send(
    events: list[NotificationEvent],
    config: AutomationConfig,
    store: SQLiteScanStore,
    *,
    notify: bool,
    dry_run: bool = False,
) -> dict[str, int]:
    if not events:
        return {"sent": 0, "skipped": 0}
    notifiers = _build_automation_notifiers(config, store)
    if not notify and not dry_run:
        return {"sent": 0, "skipped": 0}
    return dispatch_events(events, notifiers, store, dry_run=dry_run)


def _build_automation_notifiers(config: AutomationConfig, store: SQLiteScanStore) -> list[Any]:
    scanner_config = load_config(
        database_path=config.db_path,
        output_dir=config.out_root,
        notifier_channels=",".join(config.notification_channels),
    )
    notifiers: list[Any] = []
    channels = set(config.notification_channels) or {"console"}
    if "console" in channels:
        notifiers.append(ConsoleNotifier())
    if "discord" in channels:
        if scanner_config.discord_webhook_url:
            notifiers.append(DiscordWebhookNotifier(scanner_config))
        else:
            store.record_provider_health(
                "notify:discord",
                "not_configured",
                utc_now_iso(),
                "missing webhook URL",
            )
    if "telegram" in channels:
        if scanner_config.telegram_bot_token and scanner_config.telegram_chat_id:
            notifiers.append(TelegramNotifier(scanner_config))
        else:
            store.record_provider_health(
                "notify:telegram",
                "not_configured",
                utc_now_iso(),
                "missing bot token/chat id",
            )
    if "email" in channels:
        if scanner_config.email_smtp_host and scanner_config.email_from and scanner_config.email_to:
            notifiers.append(EmailNotifier(scanner_config))
        else:
            store.record_provider_health(
                "notify:email",
                "not_configured",
                utc_now_iso(),
                "missing SMTP settings",
            )
    if "windows" in channels:
        notifiers.append(WindowsLocalNotifier())
    if not notifiers:
        notifiers.append(ConsoleNotifier())
    return notifiers


def _event(
    run_date: str,
    event_type: str,
    title: str,
    body: str,
    *,
    ticker: str | None = None,
    severity: str = "info",
    source_hash: str = "",
) -> NotificationEvent:
    if event_type not in AUTOMATION_EVENT_TYPES:
        raise NotificationError(f"Unknown automation event type: {event_type}")
    key_ticker = (ticker or "ALL").upper()
    event_key = f"{run_date}:{event_type}:{key_ticker}:{severity}:{source_hash}"
    return NotificationEvent(
        event_key=event_key,
        title=title,
        body=body,
        channel_hint=event_type,
        ticker=ticker,
        payload={
            "event_type": event_type,
            "severity": severity,
            "date": run_date,
            "source_hash": source_hash,
            "run_id": event_key,
        },
    )


def _run_payload(
    run_id: str,
    run_type: str,
    status: str,
    started_at: str,
    out_dir: Path,
    extra: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "run_type": run_type,
        "status": status,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "out_dir": str(out_dir),
        **extra,
    }


def _persist_run(store: SQLiteScanStore, payload: dict[str, Any]) -> None:
    store.persist_automation_run(payload)


def _archive_source(path: Path, target_dir: Path, label: str) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return target_dir / f"{label}_missing"
    digest = file_sha256(path)[:12]
    destination = target_dir / f"{path.stem}_{label}_{digest}{path.suffix}"
    counter = 1
    while destination.exists():
        destination = target_dir / f"{path.stem}_{label}_{digest}_{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(destination))
    return destination


def _pending_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(
        [
            item
            for item in path.iterdir()
            if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
        ],
        key=lambda item: item.stat().st_mtime,
    )


def _latest_file(paths: list[Path]) -> Path | None:
    if not paths:
        return None
    return max(paths, key=lambda item: item.stat().st_mtime)


def _latest_outcome_file(path: Path) -> Path | None:
    files = [
        item
        for item in _pending_files(path)
        if _csv_has_data_rows(item)
    ]
    return _latest_file(files)


def _csv_has_data_rows(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return False
    return any(any(str(value or "").strip() for value in row.values()) for row in rows)


def _ai_normalizer(config: AutomationConfig) -> str:
    fallback = config.normalizer_fallback.lower()
    return fallback if fallback in {"codex-cli", "openai-api"} else "none"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _append_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _write_outcome_template(path: Path, tickers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTCOME_COLUMNS)
        writer.writeheader()
        for ticker in tickers:
            writer.writerow({"ticker": ticker, "source": "manual_outcome_upload"})


def _missing_outcome_tickers(store: SQLiteScanStore, *, limit: int | None = None) -> list[str]:
    latest = store.load_latest_scan() or {}
    top_rows = cast(list[dict[str, Any]], latest.get("top_explosive") or [])
    ranked_rows = cast(list[dict[str, Any]], latest.get("ranked_candidates") or [])
    ordered: list[str] = []
    for row in [*top_rows, *ranked_rows]:
        ticker = str(row.get("ticker", "")).upper()
        if ticker and ticker not in ordered:
            ordered.append(ticker)
    outcomes = {
        str(row.get("ticker", "")).upper()
        for row in store.load_manual_outcomes(limit=5000)
    }
    missing = [ticker for ticker in ordered if ticker and ticker not in outcomes]
    return missing[:limit] if limit else missing


def _automation_health(
    store: SQLiteScanStore, runs: list[dict[str, Any]], notifications: list[dict[str, Any]]
) -> list[dict[str, str]]:
    return [
        {
            "check": "automation_run",
            "status": "pass" if runs else "missing",
            "detail": "At least one automation run is persisted.",
        },
        {
            "check": "notification",
            "status": "pass" if notifications else "missing",
            "detail": "At least one automation notification is persisted.",
        },
        {
            "check": "official_calls",
            "status": "pass" if store.load_latest_scan() else "missing",
            "detail": "Latest official scan exists before outcomes.",
        },
    ]


def _source_kind(latest: dict[str, Any]) -> str:
    config = latest.get("config") if isinstance(latest, dict) else {}
    summary = latest.get("summary") if isinstance(latest, dict) else {}
    if isinstance(config, dict) and config.get("data_source_kind"):
        return str(config["data_source_kind"])
    if isinstance(summary, dict) and summary.get("data_source_kind"):
        return str(summary["data_source_kind"])
    return "unknown"


def _audit_body(summary: dict[str, Any]) -> str:
    return (
        f"Audited {summary.get('trade_count', 0)} rows. "
        f"Lunch {summary.get('avg_lunch_return_pct', 'n/a')}%, "
        f"close {summary.get('avg_close_return_pct', 'n/a')}%."
    )


def _summary_body(report: dict[str, Any]) -> str:
    return (
        f"Top3 close {report.get('top_3_close_return_pct', 'n/a')}%; "
        f"missing/manual rows {report.get('manual_outcome_count', 0)}."
    )


def _daily_body(payload: dict[str, Any]) -> str:
    return format_daily_summary(payload)


def _relabel_url_snapshot(snapshot_path: Path, url: str) -> None:
    rows = []
    host = urlparse(url).hostname or "unknown"
    with snapshot_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            row["data_source_kind"] = "url_ingest"
            row["manual_uploaded_data"] = "false"
            row["paid_data"] = "false"
            row["source"] = f"url_ingest:{host}"
            warning = str(row.get("coverage_warning") or "")
            row["coverage_warning"] = ";".join(
                part for part in [warning, "url_ingest_unverified"] if part
            )
            rows.append(row)
    with snapshot_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _is_market_day(value: str) -> bool:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    return parsed.weekday() < 5


def _today() -> str:
    return get_market_date()


def _default_config_data() -> dict[str, Any]:
    return {
        "timezone": "America/Chicago",
        "market_timezone": "America/New_York",
        "db_path": "data/shadow_real.sqlite",
        "out_root": "outputs/automation",
        "notification_channels": ["console"],
        "screener_sources": [
            {"name": "local_inbox", "type": "inbox", "path": "data/inbox/screener", "enabled": True}
        ],
        "normalizer": {"preferred": "deterministic", "fallback": "codex-cli"},
        "schedule": {},
        "monitor": {"enabled": True, "interval_seconds": 60, "max_symbols": 5},
        "outcomes": {"inbox": str(OUTCOME_INBOX), "reminder_if_missing": True},
        "notifications": {
            "dedupe": True,
            "send_top_picks": True,
            "send_failures": True,
            "send_lunch_reminder": True,
            "send_close_reminder": True,
            "send_daily_summary": True,
        },
    }


def _load_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key = ""
    current_item: dict[str, Any] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 0:
            key, value = _split_yaml(line)
            current_key = key
            current_item = None
            if value == "" and key in {"notification_channels", "screener_sources"}:
                data[key] = []
            elif value == "":
                data[key] = {}
            else:
                data[key] = _scalar(value)
        elif indent == 2 and line.startswith("- "):
            value = line[2:].strip()
            if current_key == "notification_channels":
                data.setdefault(current_key, []).append(_scalar(value))
            else:
                item: dict[str, Any] = {}
                data.setdefault(current_key, []).append(item)
                current_item = item
                if ":" in value:
                    key, scalar = _split_yaml(value)
                    item[key] = _scalar(scalar)
        elif indent == 2:
            key, value = _split_yaml(line)
            section = data.setdefault(current_key, {})
            if isinstance(section, dict):
                section[key] = _scalar(value)
        elif indent == 4 and current_item is not None:
            key, value = _split_yaml(line)
            current_item[key] = _scalar(value)
    return data


def _split_yaml(line: str) -> tuple[str, str]:
    if ":" not in line:
        return line.strip(), ""
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _scalar(value: str) -> Any:
    cleaned = value.strip().strip('"').strip("'")
    if cleaned.lower() in {"true", "false"}:
        return cleaned.lower() == "true"
    if cleaned == "":
        return ""
    try:
        if "." in cleaned:
            return float(cleaned)
        return int(cleaned)
    except ValueError:
        return cleaned


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_cell = False
        self._cell = ""
        self._row: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"td", "th"}:
            self._in_cell = True
            self._cell = ""
        elif tag == "tr":
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell += data

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._in_cell:
            self._row.append(" ".join(self._cell.split()))
            self._in_cell = False
        elif tag == "tr" and self._row:
            self.rows.append(self._row)
