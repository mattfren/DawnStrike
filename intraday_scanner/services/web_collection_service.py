"""Web Auto-Pilot collection, scanning, and notification orchestration."""

from __future__ import annotations

import csv
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, cast

from intraday_scanner.config import load_config
from intraday_scanner.errors import NotificationError
from intraday_scanner.models import SNAPSHOT_COLUMNS, utc_now_iso
from intraday_scanner.notifiers import ConsoleNotifier, NotificationEvent, dispatch_events
from intraday_scanner.notifiers.base import BaseNotifier
from intraday_scanner.notifiers.telegram_formatter import (
    format_daily_summary,
    format_manual_monitor,
    format_morning_watchlist,
    format_outcome_needed,
    format_risk_alert,
    format_source_check,
)
from intraday_scanner.notifiers.webhooks import TelegramNotifier
from intraday_scanner.providers.browser_table_provider import (
    browser_extractor_status,
    ingest_browser_table,
)
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider, read_snapshot_csv
from intraday_scanner.providers.nasdaq_halt_provider import (
    attach_halt_status,
    collect_trade_halts,
)
from intraday_scanner.providers.nasdaq_symbol_provider import build_us_common_universe
from intraday_scanner.providers.public_table_provider import ingest_public_table
from intraday_scanner.providers.sec_edgar_provider import (
    collect_sec_risk,
    enrich_rows_with_sec_risk,
)
from intraday_scanner.providers.web_source_base import (
    WebSourceConfig,
    enabled_sources,
    get_source,
    load_web_sources_config,
    require_enabled,
    write_json,
)
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.services.ai_research_service import run_ai_research
from intraday_scanner.services.e2e_automation_service import (
    automation_outcomes,
    automation_summary,
    load_automation_config,
)
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.services.screener_automation import (
    SUPPORTED_SUFFIXES,
    normalize_screener_file,
)
from intraday_scanner.services.time_utils import get_market_date
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

WEB_OUT_ROOT = Path("outputs/web_auto")


def web_build_universe(
    *,
    config_path: str | Path = "config/web_sources.example.yaml",
    db_path: str | Path = "data/shadow_real.sqlite",
    out_path: str | Path = "data/universe_us_common.csv",
    persist: bool = False,
) -> dict[str, Any]:
    config = load_web_sources_config(config_path)
    require_enabled(config)
    source = get_source(config, "nasdaq_symbol_directory") or WebSourceConfig(
        name="nasdaq_symbols",
        type="nasdaq_symbol_directory",
    )
    store = SQLiteScanStore(db_path) if persist else None
    return build_us_common_universe(
        source=source,
        config=config,
        out_path=out_path,
        store=store,
        persist=persist,
    )


def web_collect_halts(
    *,
    config_path: str | Path = "config/web_sources.example.yaml",
    db_path: str | Path = "data/shadow_real.sqlite",
    out_dir: str | Path = "outputs/web_halts",
    persist: bool = False,
) -> dict[str, Any]:
    config = load_web_sources_config(config_path)
    require_enabled(config)
    source = get_source(config, "nasdaq_trade_halts_rss") or WebSourceConfig(
        name="nasdaq_halts",
        type="nasdaq_trade_halts_rss",
    )
    store = SQLiteScanStore(db_path) if persist else None
    return collect_trade_halts(
        source=source,
        config=config,
        out_dir=out_dir,
        store=store,
        persist=persist,
    )


def web_collect_sec_risk(
    *,
    config_path: str | Path = "config/web_sources.example.yaml",
    db_path: str | Path = "data/shadow_real.sqlite",
    out_dir: str | Path = "outputs/web_sec",
    tickers: list[str] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    config = load_web_sources_config(config_path)
    require_enabled(config)
    store = SQLiteScanStore(db_path)
    source = get_source(config, "sec_edgar") or WebSourceConfig(name="sec_edgar", type="sec_edgar")
    selected = tickers or _latest_scan_tickers(store)
    return collect_sec_risk(
        source=source,
        config=config,
        tickers=selected,
        out_dir=out_dir,
        store=store if persist else None,
        persist=persist,
    )


def web_ingest_public_table(
    *,
    url: str,
    config_path: str | Path = "config/web_sources.example.yaml",
    db_path: str | Path = "data/shadow_real.sqlite",
    out_dir: str | Path = "outputs/web_ingest",
    persist: bool = False,
    print_rows: bool = False,
    allow_unlisted_url: bool = False,
) -> dict[str, Any]:
    config = load_web_sources_config(config_path)
    require_enabled(config)
    source = _source_for_url(config.sources, url)
    store = SQLiteScanStore(db_path) if persist else None
    return ingest_public_table(
        url=url,
        source=source,
        config=config,
        out_dir=out_dir,
        store=store,
        persist=persist,
        print_rows=print_rows,
        allow_unlisted_url=allow_unlisted_url,
    )


def web_auto_collect(
    *,
    config_path: str | Path = "config/web_sources.example.yaml",
    db_path: str | Path = "data/shadow_real.sqlite",
    out_dir: str | Path | None = None,
    persist: bool = False,
    print_rows: bool = False,
) -> dict[str, Any]:
    run_date = get_market_date()
    output_dir = Path(out_dir) if out_dir else WEB_OUT_ROOT / run_date
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now_iso()
    config = load_web_sources_config(config_path)
    require_enabled(config)
    store = SQLiteScanStore(db_path)
    rows: list[dict[str, Any]] = []
    source_attempts: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    blocked_source_count = 0

    local_result = _collect_local_inbox(config, output_dir, store if persist else None, persist)
    source_attempts.append(local_result["summary"])
    rows.extend(local_result["rows"])
    if local_result["summary"]["status"] not in {"success", "empty"}:
        failures.append(local_result["summary"])

    if not rows:
        for source in enabled_sources(config, "public_table_url"):
            result = ingest_public_table(
                url=source.url,
                source=source,
                config=config,
                out_dir=output_dir / source.name,
                store=store if persist else None,
                persist=persist,
                print_rows=False,
            )
            source_attempts.append(result)
            if result.get("status") == "success":
                rows.extend(_read_snapshot_rows(Path(result["paths"]["premarket_snapshot"])))
                break
            failures.append(result)
            if "not in configured allowed_domains" in str(result.get("failure_reason", "")):
                blocked_source_count += 1

    if not rows:
        for source in enabled_sources(config, "browser_table_url"):
            result = ingest_browser_table(
                source=source,
                config=config,
                out_dir=output_dir / source.name,
                store=store if persist else None,
                persist=persist,
                print_rows=False,
            )
            source_attempts.append(result)
            if result.get("status") == "success":
                rows.extend(_read_snapshot_rows(Path(result["paths"]["premarket_snapshot"])))
                break
            failures.append(result)
            if "not in configured allowed_domains" in str(result.get("failure_reason", "")):
                blocked_source_count += 1

    halt_summary = _maybe_collect_halts(config, output_dir, store if persist else None, persist)
    halt_events = list(halt_summary.get("events") or [])
    if halt_events:
        rows = attach_halt_status(rows, halt_events)
    sec_summary = _maybe_collect_sec(config, output_dir, store if persist else None, rows, persist)
    sec_events = list(sec_summary.get("events") or [])
    if sec_events:
        rows = enrich_rows_with_sec_risk(rows, sec_events)

    deduped = _dedupe_rows(rows)
    snapshot_path = output_dir / "premarket_snapshot.csv"
    _write_snapshot(snapshot_path, deduped)
    source_summary = {
        "status": "success" if deduped else "no_data",
        "run_id": str(uuid.uuid4()),
        "started_at": started_at,
        "created_at": utc_now_iso(),
        "completed_at": utc_now_iso(),
        "sources_attempted": len(source_attempts),
        "sources_succeeded": sum(1 for item in source_attempts if item.get("status") == "success"),
        "rows_extracted": sum(int(item.get("rows_extracted") or 0) for item in source_attempts),
        "rows_normalized": sum(int(item.get("rows_normalized") or 0) for item in source_attempts),
        "candidate_count": len(deduped),
        "source_failures": len(failures),
        "unknown_field_counts": _unknown_field_counts(source_attempts),
        "blocked_source_count": blocked_source_count,
        "attempts": source_attempts,
        "enabled_candidate_sources": [
            source.name for source in _candidate_sources(config) if source.enabled
        ],
        "candidate_source_count": len(
            [source for source in _candidate_sources(config) if source.enabled]
        ),
        "only_universe_or_enrichment_enabled": _only_universe_or_enrichment_enabled(config),
        "halt_summary": _compact_summary(halt_summary),
        "sec_summary": _compact_summary(sec_summary),
        "snapshot_path": str(snapshot_path),
    }
    quality = _data_quality_report(deduped, failures, source_summary)
    write_json(output_dir / "source_summary.json", source_summary)
    write_json(output_dir / "data_quality_report.json", quality)
    if persist:
        store.persist_web_fetch_run(
            {
                "run_id": source_summary["run_id"],
                "source": "web_auto_collect",
                "source_type": "aggregate",
                "status": source_summary["status"],
                "started_at": source_summary["started_at"],
                "completed_at": source_summary["completed_at"],
                "url": "",
                "summary": source_summary,
            }
        )
        source_run_id = str(source_summary["run_id"])
        store.persist_web_fetch_result(
            {
                "run_id": source_summary["run_id"],
                "source": "web_auto_collect",
                "status": source_summary["status"],
                "row_count": len(deduped),
                "artifact_path": str(snapshot_path),
                "failure_reason": "; ".join(
                    str(item.get("failure_reason") or "") for item in failures
                ),
                "summary": source_summary,
            }
        )
        store.record_source_health(
            "web_auto_collect",
            "ok" if deduped else "failed",
            utc_now_iso(),
            f"candidates={len(deduped)} failures={len(failures)}",
            source_summary,
        )
        store.persist_normalized_source_rows(source_run_id, "web_auto_collect", deduped)
    if print_rows:
        print(json.dumps(source_summary, indent=2, sort_keys=True))
    return {
        "status": source_summary["status"],
        "out_dir": str(output_dir),
        "snapshot_path": str(snapshot_path),
        "source_summary": source_summary,
        "data_quality_report": quality,
        "rows": deduped,
    }


def web_telegram_daemon(
    *,
    config_path: str | Path = "config/web_sources.example.yaml",
    automation_config_path: str | Path = "config/automation.example.yaml",
    db_path: str | Path = "data/shadow_real.sqlite",
    out_root: str | Path = "outputs/web_telegram",
    ai_mode: str = "none",
    notify: str = "console",
    dry_run: bool = False,
    max_cycles: int | None = None,
    poll_seconds: int = 60,
    run_date: str | None = None,
) -> dict[str, Any]:
    if run_date is None:
        auto_config = load_automation_config(
            automation_config_path,
            db_path=db_path,
            out_root=Path(out_root) / get_market_date(),
        )
        run_date = get_market_date(auto_config.timezone)
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)
    log_path = Path("logs") / f"web_telegram_{run_date}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteScanStore(db_path)
    cycles = 0
    attempts = []
    while True:
        cycles += 1
        try:
            attempt = _web_telegram_cycle(
                config_path=config_path,
                automation_config_path=automation_config_path,
                db_path=db_path,
                out_root=root,
                ai_mode=ai_mode,
                notify=notify,
                dry_run=dry_run,
                run_date=run_date,
            )
        except Exception as exc:
            attempt = {
                "run_id": str(uuid.uuid4()),
                "run_type": "web_telegram_daemon",
                "status": "failed",
                "started_at": utc_now_iso(),
                "completed_at": utc_now_iso(),
                "error": str(exc),
            }
            _send_web_events(
                [_event("web:failed", "DAWNSTRIKE RISK ALERT", f"MANUAL REVIEW REQUIRED\n{exc}")],
                notify=notify,
                db_path=db_path,
                dry_run=dry_run,
            )
        attempts.append(attempt)
        _append_jsonl(log_path, attempt)
        store.persist_automation_run(
            {
                "run_id": str(uuid.uuid4()),
                "run_type": "web_telegram_daemon_cycle",
                "status": str(attempt.get("status") or ""),
                "started_at": str(attempt.get("started_at") or utc_now_iso()),
                "completed_at": str(attempt.get("completed_at") or utc_now_iso()),
                "out_dir": str(root / run_date),
                "attempt": attempt,
                "dry_run": dry_run,
            }
        )
        if max_cycles is not None and cycles >= max_cycles:
            break
        if max_cycles is None:
            time.sleep(max(1, poll_seconds))
    result = {
        "status": "complete",
        "run_type": "web_telegram_daemon",
        "dry_run": dry_run,
        "cycles": cycles,
        "log_path": str(log_path),
        "attempts": attempts,
    }
    return result


def telegram_test(
    *,
    db_path: str | Path = "data/shadow_real.sqlite",
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    config = load_config(database_path=Path(db_path), notifier_channels="telegram")
    mode = "dry_run" if dry_run else "real"
    event = _event(
        f"telegram_test:{_today()}:telegram:{mode}",
        "DAWNSTRIKE TELEGRAM TEST",
        "WATCH\nTelegram notification route is configured for research/watchlist alerts only.",
    )
    canonical_key = event.event_key
    notification_key = (
        canonical_key if not force else f"{canonical_key}:force:{uuid.uuid4().hex[:12]}"
    )
    if not force and store.has_notification(canonical_key):
        return {"status": "skipped_duplicate", "event_key": canonical_key}
    if dry_run:
        print(f"[dry-run:telegram] {event.title}: {event.body}")
        store.record_notification(
            event_key=notification_key,
            channel="telegram",
            run_id=None,
            ticker=None,
            payload={
                "title": event.title,
                "body": event.body,
                "channel_hint": event.channel_hint,
                "dry_run": True,
                "send_attempted": False,
                "status": "dry_run",
                "dedupe_bypassed": force,
            },
        )
        return {"status": "dry_run", "event_key": notification_key, "forced": force}
    if not config.telegram_bot_token or not config.telegram_chat_id:
        raise NotificationError("Telegram requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    TelegramNotifier(config).send(event)
    store.record_notification(
        event_key=notification_key,
        channel="telegram",
        payload={
            "title": event.title,
            "body": event.body,
            "channel_hint": event.channel_hint,
            "dry_run": False,
            "send_attempted": True,
            "status": "sent",
            "dedupe_bypassed": force,
        },
    )
    return {"status": "sent", "event_key": notification_key, "forced": force}


def web_automation_status(store: SQLiteScanStore) -> dict[str, Any]:
    fetch_runs = store.load_web_fetch_runs(limit=20)
    fetch_results = store.load_web_fetch_results(limit=20)
    source_health = store.load_source_health(limit=20)
    ai_runs = store.load_ai_research_runs(limit=10)
    sec_events = store.load_sec_risk_events(limit=20)
    halt_events = store.load_halt_events(limit=20)
    rows = store.load_normalized_source_rows(limit=50)
    latest_result = fetch_results[0] if fetch_results else {}
    latest_summary = dict(latest_result.get("summary") or {})
    web_config = load_web_sources_config(None)
    return {
        "latest_fetch_run": fetch_runs[0] if fetch_runs else {},
        "latest_fetch_result": latest_result,
        "latest_source_summary": latest_summary,
        "fetch_runs": fetch_runs,
        "fetch_results": fetch_results,
        "source_health": source_health,
        "raw_artifacts": store.load_raw_source_artifacts(limit=20),
        "normalized_rows": rows,
        "sec_risk_events": sec_events,
        "halt_events": halt_events,
        "ai_research_runs": ai_runs,
        "ai_research_outputs": store.load_ai_research_outputs(limit=50),
        "ai_data_warnings": store.load_ai_data_warnings(limit=50),
        "telegram_status": _telegram_status(store),
        "source_operability": source_operability_status(web_config),
        "browser_extractor": browser_extractor_status(),
        "counts": {
            "latest_candidate_count": latest_summary.get("candidate_count", len(rows)),
            "source_failures": latest_summary.get("source_failures", 0),
            "sec_risk_events": len(sec_events),
            "halt_events": len(halt_events),
            "ai_runs": len(ai_runs),
        },
    }


def web_source_doctor(
    *,
    config_path: str | Path = "config/web_sources.example.yaml",
    out_dir: str | Path = "outputs/source_doctor",
    print_rows: bool = False,
) -> dict[str, Any]:
    config = load_web_sources_config(config_path)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for source in config.sources:
        rows.append(_doctor_source(source, config, output_dir))
    result = {
        "status": "complete",
        "created_at": utc_now_iso(),
        "config_path": str(config_path),
        "browser_extractor": browser_extractor_status(),
        "source_operability": source_operability_status(config),
        "sources": rows,
    }
    write_json(output_dir / "source_doctor.json", result)
    if print_rows:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def source_operability_status(config: Any) -> dict[str, Any]:
    enabled = [source for source in config.sources if source.enabled]
    candidate = [source for source in enabled if _source_classification(source) == "candidate"]
    universe = [source for source in enabled if _source_classification(source) == "universe"]
    enrichment = [source for source in enabled if _source_classification(source) == "enrichment"]
    return {
        "enabled_candidate_sources": [source.name for source in candidate],
        "enabled_universe_sources": [source.name for source in universe],
        "enabled_enrichment_sources": [source.name for source in enrichment],
        "only_universe_or_enrichment_enabled": not candidate and bool(universe or enrichment),
        "candidate_source_required": not candidate,
    }


def _web_telegram_cycle(
    *,
    config_path: str | Path,
    automation_config_path: str | Path,
    db_path: str | Path,
    out_root: Path,
    ai_mode: str,
    notify: str,
    dry_run: bool,
    run_date: str,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    run_id = str(uuid.uuid4())
    cycle_dir = out_root / run_date
    collect = web_auto_collect(
        config_path=config_path,
        db_path=db_path,
        out_dir=cycle_dir / "collect",
        persist=True,
        print_rows=False,
    )
    events: list[NotificationEvent] = []
    if collect["status"] != "success":
        source_body = format_source_check(collect["source_summary"])
        events.append(
            _event(
                f"web:{run_date}:no_source",
                "Dawnstrike Source Check",
                source_body,
                channel_hint="source_failed",
                payload={"source_summary": collect["source_summary"]},
            )
        )
        _send_web_events(events, notify=notify, db_path=db_path, dry_run=dry_run)
        return {
            "run_id": run_id,
            "run_type": "web_telegram_cycle",
            "status": "no_data",
            "started_at": started_at,
            "completed_at": utc_now_iso(),
            "collect": collect["source_summary"],
            "out_dir": str(cycle_dir),
        }
    store = SQLiteScanStore(db_path)
    snapshot_path = Path(str(collect["snapshot_path"]))
    config = load_config(
        provider="csv",
        output_dir=cycle_dir / "scan",
        database_path=Path(db_path),
        notifier_channels=notify,
    )
    scan_result = ScanService(CsvSnapshotProvider(snapshot_path), store=store).run(
        config,
        persist=False,
    )
    scan_result.config.update(
        {
            "data_source_kind": "web_auto",
            "shadow_mode": True,
            "paid_data": False,
            "fixture_only": False,
            "manual_uploaded_data": False,
            "automation_run_id": run_id,
        }
    )
    store.persist_scan_result(scan_result)
    scan_paths = write_scan_outputs(scan_result, cycle_dir / "scan")
    ai = run_ai_research(
        rows=[candidate.to_dict() for candidate in scan_result.ranked_candidates],
        mode=ai_mode,
        store=store,
        persist=True,
        out_dir=cycle_dir / "ai",
    )
    payload = {
        "summary": scan_result.summary(),
        "ranked_candidates": [candidate.to_dict() for candidate in scan_result.ranked_candidates],
        "top_explosive": [candidate.to_dict() for candidate in scan_result.top_explosive],
        "avoid_list": [candidate.to_dict() for candidate in scan_result.avoid_list],
        "source_summary": collect["source_summary"],
        "ai": ai,
    }
    ranked_payload = list(payload["ranked_candidates"])
    events.extend(_morning_watchlist_events(payload))
    if ranked_payload:
        events.append(_manual_monitor_event(run_date, payload))
    auto_config = load_automation_config(
        automation_config_path,
        db_path=db_path,
        out_root=cycle_dir / "automation",
    )
    scanner_config = load_config(database_path=Path(db_path), notifier_channels=notify)
    try:
        outcomes = automation_outcomes(
            config_path=automation_config_path,
            db_path=db_path,
            out_root=auto_config.out_root,
            run_date=run_date,
            notify=False,
            dry_run=dry_run,
        )
    except Exception as exc:
        outcomes = {"status": "failed", "error": str(exc)}
        events.append(
            _event(
                f"web:{run_date}:outcome_failed",
                "DAWNSTRIKE RISK ALERT",
                f"OUTCOME NEEDED\nOutcome automation failed: {exc}",
                channel_hint="outcome_missing",
            )
        )
    if outcomes.get("status") == "missing" and (
        ranked_payload or scanner_config.telegram_send_outcome_reminder_on_no_picks
    ):
        events.append(_outcome_needed_event(run_date, outcomes))
    summary: dict[str, Any] = {"status": "skipped_disabled"}
    if auto_config.notifications.get("send_daily_summary", True):
        try:
            summary = automation_summary(
                config_path=automation_config_path,
                db_path=db_path,
                out_root=auto_config.out_root,
                run_date=run_date,
                notify=False,
                dry_run=dry_run,
            )
        except Exception as exc:
            summary = {"status": "failed", "error": str(exc), "missing_outcome_count": "n/a"}
            events.append(
                _event(
                    f"web:{run_date}:summary_failed",
                    "Dawnstrike Summary",
                    f"MANUAL REVIEW\nDaily summary failed: {exc}",
                    channel_hint="daily_summary",
                )
            )
        events.append(_daily_summary_event(run_date, summary))
    send_stats = _send_web_events(events, notify=notify, db_path=db_path, dry_run=dry_run)
    result = {
        "run_id": run_id,
        "run_type": "web_telegram_cycle",
        "status": "success",
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "out_dir": str(cycle_dir),
        "collect": collect["source_summary"],
        "scan_summary": scan_result.summary(),
        "scan_paths": {key: str(value) for key, value in scan_paths.items()},
        "ai_summary": ai["run"],
        "outcomes": {key: value for key, value in outcomes.items() if key != "rows"},
        "summary": summary,
        "notifications": send_stats,
    }
    write_json(cycle_dir / "web_telegram_cycle_summary.json", result)
    return result


def _doctor_source(
    source: WebSourceConfig,
    config: Any,
    output_dir: Path,
) -> dict[str, Any]:
    classification = _source_classification(source)
    base = {
        "source": source.name,
        "type": source.type,
        "classification": classification,
        "enabled": source.enabled,
        "attempted": False,
        "status": "disabled" if not source.enabled else "not_attempted",
        "rows_extracted": 0,
        "rows_normalized": 0,
        "failure_reason": "",
        "next_action": _next_action_for_source(source, classification),
    }
    if not source.enabled:
        return base
    if source.type == "local_inbox":
        inbox = Path(source.path or "data/inbox/screener")
        files = [
            path
            for path in inbox.iterdir()
            if inbox.exists() and path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        ] if inbox.exists() else []
        return {
            **base,
            "attempted": True,
            "status": "ready" if files else "empty",
            "rows_extracted": len(files),
            "rows_normalized": 0,
            "failure_reason": "" if files else "local inbox is empty",
            "path": str(inbox),
        }
    if source.type == "public_table_url":
        result = ingest_public_table(
            url=source.url,
            source=source,
            config=config,
            out_dir=output_dir / source.name,
            persist=False,
            print_rows=False,
        )
        return _doctor_from_result(base, result)
    if source.type == "browser_table_url":
        result = ingest_browser_table(
            source=source,
            config=config,
            out_dir=output_dir / source.name,
            persist=False,
            print_rows=False,
        )
        return _doctor_from_result(base, result)
    if source.type == "nasdaq_symbol_directory":
        return {
            **base,
            "status": "universe_only",
            "failure_reason": "nasdaq_symbols does not generate premarket picks",
        }
    if source.type in {"nasdaq_trade_halts_rss", "sec_edgar"}:
        return {
            **base,
            "status": "enrichment_only",
            "failure_reason": "enrichment source; it does not generate candidate picks",
        }
    return {**base, "status": "unknown_type", "failure_reason": "unknown source type"}


def _doctor_from_result(base: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    reason = str(result.get("failure_reason") or result.get("reason") or "")
    return {
        **base,
        "attempted": True,
        "status": str(result.get("status") or "unknown"),
        "rows_extracted": int(result.get("rows_extracted") or 0),
        "rows_normalized": int(result.get("rows_normalized") or 0),
        "failure_reason": reason,
        "next_action": _next_action_for_result(base, result, reason),
    }


def _collect_local_inbox(
    config: Any,
    output_dir: Path,
    store: SQLiteScanStore | None,
    persist: bool,
) -> dict[str, Any]:
    source = get_source(config, "local_inbox")
    if source is None:
        return {"summary": {"source": "local_inbox", "status": "disabled"}, "rows": []}
    inbox = Path(source.path or "data/inbox/screener")
    latest = _latest_file(inbox)
    if latest is None:
        return {
            "summary": {
                "source": source.name,
                "status": "empty",
                "rows_extracted": 0,
                "rows_normalized": 0,
                "path": str(inbox),
            },
            "rows": [],
        }
    normalized = normalize_screener_file(
        input_path=latest,
        out_dir=output_dir / "local_inbox",
        ai_normalizer="none",
        store=store if persist else None,
    )
    rows = [dict(row, source_lineage=str(latest)) for row in list(normalized.get("rows") or [])]
    archive_path = _archive_local_source(latest)
    summary = {
        "source": source.name,
        "status": "success",
        "rows_extracted": len(rows),
        "rows_normalized": len(rows),
        "path": str(latest),
        "raw_archive_path": str(archive_path),
        "normalization": normalized["summary"],
    }
    if persist and store is not None:
        store.record_source_health(source.name, "ok", utc_now_iso(), f"rows={len(rows)}", summary)
    return {"summary": summary, "rows": rows}


def _maybe_collect_halts(
    config: Any,
    output_dir: Path,
    store: SQLiteScanStore | None,
    persist: bool,
) -> dict[str, Any]:
    source = get_source(config, "nasdaq_trade_halts_rss")
    if source is None:
        return {"status": "disabled", "events": []}
    return collect_trade_halts(
        source=source,
        config=config,
        out_dir=output_dir / "halts",
        store=store,
        persist=persist,
    )


def _maybe_collect_sec(
    config: Any,
    output_dir: Path,
    store: SQLiteScanStore | None,
    rows: list[dict[str, Any]],
    persist: bool,
) -> dict[str, Any]:
    source = get_source(config, "sec_edgar")
    if source is None:
        return {"status": "disabled", "events": []}
    tickers = [str(row.get("ticker") or "") for row in rows]
    return collect_sec_risk(
        source=source,
        config=config,
        tickers=tickers,
        out_dir=output_dir / "sec",
        store=store,
        persist=persist,
    )


def _morning_watchlist_events(payload: dict[str, Any]) -> list[NotificationEvent]:
    summary = dict(payload.get("summary") or {})
    source_summary = dict(payload.get("source_summary") or {})
    ranked = list(payload.get("ranked_candidates") or [])
    body = format_morning_watchlist(
        ranked=ranked,
        avoid=list(payload.get("avoid_list") or []),
        source_summary=source_summary,
    )
    return [
        _event(
            f"{summary.get('run_id')}:web_morning_watchlist",
            "Dawnstrike Watchlist",
            body,
            channel_hint="top_picks",
            payload={
                "summary": summary,
                "ranked_candidates": ranked[:5],
                "avoid_list": list(payload.get("avoid_list") or [])[:3],
                "source_summary": source_summary,
                "telegram_compact_message": body,
            },
        )
    ]


def _risk_events(payload: dict[str, Any]) -> list[NotificationEvent]:
    events = []
    summary = dict(payload.get("summary") or {})
    for row in list(payload.get("avoid_list") or [])[:5]:
        changed = row.get("avoid_reasons") or row.get("risk_flags") or "risk filter"
        events.append(
            _event(
                f"{summary.get('run_id')}:web_risk:{row.get('ticker')}:{changed}",
                "DAWNSTRIKE RISK ALERT",
                "\n".join(
                    [
                        f"ticker: {row.get('ticker')}",
                        "severity: high",
                        "event type: risk_filter",
                        "latest available price/source: "
                        f"{row.get('premarket_price')} / {row.get('source')}",
                        f"original thesis: score {row.get('score')} watchlist candidate",
                        f"what changed: {changed}",
                        "action label: CAUTION / MANUAL REVIEW REQUIRED",
                    ]
                ),
                ticker=str(row.get("ticker") or ""),
                channel_hint="risk_alert",
                payload={"telegram_compact_message": format_risk_alert(row), "row": row},
            )
        )
    return events


def _manual_monitor_event(run_date: str, payload: dict[str, Any]) -> NotificationEvent:
    ranked = list(payload.get("ranked_candidates") or [])
    tickers = [str(row.get("ticker") or "").upper() for row in ranked[:5] if row.get("ticker")]
    body = format_manual_monitor(tickers)
    return _event(
        f"web:{run_date}:manual_monitor_required:{','.join(tickers) or 'none'}",
        "Manual Monitor Needed",
        body,
        channel_hint="monitor_alert",
        payload={"tickers": tickers, "telegram_compact_message": body},
    )


def _outcome_needed_event(run_date: str, outcomes: dict[str, Any]) -> NotificationEvent:
    tickers = [
        str(ticker).upper()
        for ticker in list(outcomes.get("missing_tickers") or outcomes.get("tickers") or [])
        if str(ticker).strip()
    ]
    reminder_path = str(outcomes.get("reminder_path") or "")
    body = format_outcome_needed(run_date=run_date, reminder_path=reminder_path, tickers=tickers)
    return _event(
        f"web:{run_date}:outcome_needed",
        "Outcome Data Needed",
        body,
        channel_hint="outcome_missing",
        payload={"outcomes": outcomes, "tickers": tickers, "telegram_compact_message": body},
    )


def _daily_summary_event(run_date: str, summary: dict[str, Any]) -> NotificationEvent:
    body = format_daily_summary(summary)
    return _event(
        f"web:{run_date}:daily_summary:{summary.get('source_hash', '')}",
        "Dawnstrike Summary",
        body,
        channel_hint="daily_summary",
        payload={"summary": summary, "telegram_compact_message": body},
    )


def _send_web_events(
    events: list[NotificationEvent],
    *,
    notify: str,
    db_path: str | Path,
    dry_run: bool,
) -> dict[str, int]:
    store = SQLiteScanStore(db_path)
    channels = [channel.strip().lower() for channel in notify.split(",") if channel.strip()]
    if not channels:
        channels = ["console"]
    notifiers: list[BaseNotifier] = []
    scanner_config = load_config(
        database_path=Path(db_path),
        notifier_channels=",".join(channels),
    )
    for channel in channels:
        if channel == "telegram":
            if scanner_config.telegram_bot_token and scanner_config.telegram_chat_id:
                notifiers.append(TelegramNotifier(scanner_config))
            elif dry_run:
                notifiers.append(ConsoleNotifier())
            else:
                store.record_source_health(
                    "telegram",
                    "missing",
                    utc_now_iso(),
                    "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.",
                    {},
                )
        elif channel == "console":
            notifiers.append(ConsoleNotifier())
    if not notifiers:
        notifiers.append(ConsoleNotifier())
    return dispatch_events(events, notifiers, store, dry_run=dry_run)


def _event(
    event_key: str,
    title: str,
    body: str,
    *,
    ticker: str | None = None,
    channel_hint: str = "web_auto_pilot",
    payload: dict[str, Any] | None = None,
) -> NotificationEvent:
    base_payload = {"run_id": event_key.split(":", 1)[0], "source": "web_auto_pilot"}
    if payload:
        base_payload.update(payload)
    return NotificationEvent(
        event_key=event_key,
        title=title,
        body=body,
        channel_hint=channel_hint,
        ticker=ticker,
        payload=base_payload,
    )


def _action_label(row: dict[str, Any]) -> str:
    score = _float(row.get("score"))
    risk = str(row.get("risk_flags") or row.get("avoid_reasons") or "").lower()
    if "halt" in risk or "offering" in risk or score < 50:
        return "CAUTION"
    if score >= 82:
        return "BREAKOUT TRIGGER / WATCH"
    if score >= 70:
        return "WATCH"
    return "HIGH VOLATILITY WATCH"


def _candidate_sources(config: Any) -> list[WebSourceConfig]:
    return [source for source in config.sources if _source_classification(source) == "candidate"]


def _only_universe_or_enrichment_enabled(config: Any) -> bool:
    enabled = [source for source in config.sources if source.enabled]
    return bool(enabled) and not any(
        _source_classification(source) == "candidate" for source in enabled
    )


def _source_classification(source: WebSourceConfig) -> str:
    if source.type in {"local_inbox", "public_table_url", "browser_table_url"}:
        return "candidate"
    if source.type == "nasdaq_symbol_directory":
        return "universe"
    if source.type in {"nasdaq_trade_halts_rss", "sec_edgar"}:
        return "enrichment"
    return "unknown"


def _next_action_for_source(source: WebSourceConfig, classification: str) -> str:
    if classification == "candidate" and not source.enabled:
        return "Enable this source or use local_inbox for picks."
    if source.type == "local_inbox":
        return "Drop CSV into data\\inbox\\screener."
    if source.type == "public_table_url":
        return "If no_candidate_table, use a local CSV or enable browser_table_url."
    if source.type == "browser_table_url":
        return "Install browser extra and chromium, then enable only for allowed public pages."
    if source.type == "nasdaq_symbol_directory":
        return "Use this for universe filtering only; enable a candidate source for picks."
    if classification == "enrichment":
        return "Enable after setting a real user agent; enrichment does not create picks."
    return "Review source configuration."


def _next_action_for_result(
    base: dict[str, Any],
    result: dict[str, Any],
    reason: str,
) -> str:
    if result.get("status") == "success" and int(result.get("rows_normalized") or 0) > 0:
        return "Source can produce candidate rows."
    if str(result.get("reason")) == "no_candidate_table":
        return "Drop CSV into data\\inbox\\screener or try browser_table_url."
    if "BROWSER_EXTRACTOR_NOT_AVAILABLE" in reason:
        return 'Run py -m pip install -e ".[browser]" and py -m playwright install chromium.'
    return str(base.get("next_action") or "Review source failure.")


def _source_for_url(sources: tuple[WebSourceConfig, ...], url: str) -> WebSourceConfig:
    for source in sources:
        if source.type == "public_table_url" and source.url == url:
            return source
    for source in sources:
        if source.type == "public_table_url" and source.enabled:
            return WebSourceConfig(
                name=source.name,
                type=source.type,
                enabled=True,
                url=url,
                fixture_path=source.fixture_path,
                allowed_domains=source.allowed_domains,
                params=source.params,
            )
    return WebSourceConfig(name="adhoc_public_table", type="public_table_url", url=url)


def _latest_scan_tickers(store: SQLiteScanStore) -> list[str]:
    latest = store.load_latest_scan()
    if not latest:
        return []
    ranked = cast(list[dict[str, Any]], latest.get("ranked_candidates") or [])
    return [str(row.get("ticker") or "") for row in ranked[:20]]


def _latest_file(inbox: Path) -> Path | None:
    if not inbox.exists():
        return None
    files = [
        path
        for path in inbox.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    ]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def _archive_local_source(path: Path) -> Path:
    processed = Path("data/processed/screener")
    processed.mkdir(parents=True, exist_ok=True)
    target = processed / f"{path.stem}_web_processed{path.suffix}"
    counter = 1
    while target.exists():
        target = processed / f"{path.stem}_web_processed_{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(target))
    return target


def _read_snapshot_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [row.to_dict() for row in read_snapshot_csv(path)]


def _write_snapshot(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ticker: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        current = by_ticker.get(ticker)
        if current is None or str(row.get("as_of_timestamp") or "") >= str(
            current.get("as_of_timestamp") or ""
        ):
            by_ticker[ticker] = dict(row, ticker=ticker)
    return sorted(
        by_ticker.values(),
        key=lambda row: _float(row.get("dollar_volume")),
        reverse=True,
    )


def _data_quality_report(
    rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    source_summary: dict[str, Any],
) -> dict[str, Any]:
    warnings = []
    if not rows:
        warnings.append("No valid source rows were available.")
    for row in rows:
        coverage = str(row.get("coverage_warning") or "")
        if coverage:
            warnings.append(f"{row.get('ticker')}: {coverage}")
    return {
        "created_at": utc_now_iso(),
        "candidate_count": len(rows),
        "warnings": warnings[:50],
        "source_failures": failures,
        "summary": source_summary,
    }


def _unknown_field_counts(source_attempts: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attempt in source_attempts:
        for warning in list(attempt.get("warnings") or []):
            text = str(warning)
            if "missing required market columns" in text:
                counts["missing_required_market_columns"] = (
                    counts.get("missing_required_market_columns", 0) + 1
                )
    return counts


def _compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in summary.items()
        if key not in {"events", "fetches", "items"}
    }


def _telegram_status(store: SQLiteScanStore) -> dict[str, Any]:
    notifications = store.load_recent_notifications(limit=20)
    latest = next((row for row in notifications if row.get("channel") == "telegram"), {})
    return {
        "latest_notification": latest,
        "telegram_notifications": sum(
            1 for row in notifications if row.get("channel") == "telegram"
        ),
    }


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _today() -> str:
    return get_market_date()


def _float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return 0.0
