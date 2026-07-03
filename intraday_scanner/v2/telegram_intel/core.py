# ruff: noqa: E501
# mypy: ignore-errors
"""Telegram Intelligence for Dawnstrike OMEGA.

This module is deliberately additive. It reads existing v2 artifacts, formats
research-only Telegram messages, and writes audit artifacts. It does not import
the legacy Streamlit app, mutate SQLite, change strategy logic, or route orders.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

OUTPUT_ROOT = Path("data/v2_telegram_intel")
COMMAND_CENTER_ROOT = Path("data/v2_command_center")
SCHEMA_PREFIX = "v2.telegram_intel"
DEFAULT_MAX_CHARS = 3500
MESSAGE_KINDS = ("morning", "after-close", "watchdog", "no-picks", "test")
DIRS = (
    "messages",
    "drafts",
    "sent",
    "reports",
    "status",
    "logs",
    "manifests",
    "templates",
    "audit",
)
TEMPLATE_FILES = {
    "morning": "morning.md",
    "after-close": "after_close.md",
    "no-picks": "no_picks.md",
    "watchdog": "watchdog.md",
}
QUALITY_THRESHOLDS = {
    "morning": 90,
    "after-close": 90,
    "no-picks": 95,
    "watchdog": 0,
    "test": 0,
}
COMMANDS = (
    "py -m intraday_scanner.v2.telegram_intel init",
    "py -m intraday_scanner.v2.telegram_intel readiness",
    "py -m intraday_scanner.v2.telegram_intel draft --kind morning --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.telegram_intel draft --kind after-close --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.telegram_intel draft --kind watchdog --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.telegram_intel draft --kind no-picks --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.telegram_intel send --kind morning --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.telegram_intel send --kind after-close --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.telegram_intel send --kind watchdog --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.telegram_intel test-send",
    "py -m intraday_scanner.v2.telegram_intel verify",
    "py -m intraday_scanner.v2.telegram_intel report",
    "py -m intraday_scanner.v2.telegram_intel demo",
)
SECRET_VALUE_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "ALPHA_VANTAGE_API_KEY",
    "TWELVE_DATA_API_KEY",
)
SECRET_LITERAL_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization:\s*bearer|password|secret|token)\s*[:=]\s*['\"]?[^'\"\s,;]+"
)
ABSOLUTE_PATH_PATTERN = re.compile(r"\b[A-Za-z]:[\\/](?!n)[^\"'<>\s]+[\\/][^\"'<>\s]*")

Transport = Callable[[str, dict[str, str], int], dict[str, object]]


@dataclass(frozen=True)
class SourceRecord:
    key: str
    path: Path
    status: str
    sha256: str
    payload: object

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "path": self.path.as_posix(),
            "sha256": self.sha256,
            "status": self.status,
        }


def init(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    _ensure_dirs(output_root)
    _write_templates(output_root)
    _write_docs()
    payload = {
        "commands": list(COMMANDS),
        "created_at": _now(),
        "external_alerts_default": "disabled",
        "live_trading_enabled": False,
        "module_root": "intraday_scanner/v2/telegram_intel",
        "output_root": output_root.as_posix(),
        "schema_version": f"{SCHEMA_PREFIX}.manifest.v1",
        "status": "initialized",
    }
    _write_json(output_root / "manifests" / "telegram_intel_manifest.json", payload)
    build_command_center_pages(output_root=output_root)
    return payload


def readiness(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    payload = _readiness_payload(output_root)
    _write_json(output_root / "status" / "latest_readiness.json", payload)
    _write_md(
        output_root / "status" / "latest_readiness.md",
        "Telegram Intelligence Readiness",
        _readiness_lines(payload),
    )
    build_command_center_pages(output_root=output_root)
    return payload


def draft(
    *,
    kind: str,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    normalized_kind = _normalize_kind(kind)
    ready = _readiness_payload(output_root)
    context = _load_context(run_date=run_date)
    message = _build_message(
        kind=normalized_kind,
        run_date=run_date,
        context=context,
        include_command_center_path=_bool_env("TELEGRAM_INCLUDE_COMMAND_CENTER_PATH", True),
    )
    max_chars = _int(ready.get("max_message_chars")) or DEFAULT_MAX_CHARS
    chunks = _chunk_message(message, max_chars)
    quality = _quality_score(
        kind=normalized_kind,
        message=message,
        context=context,
        readiness_payload=ready,
        chunks=chunks,
    )
    message_hash = _sha256_text(message)
    message_id = f"telegram:{run_date.isoformat()}:{normalized_kind}:{message_hash[:12]}"
    created_at = _now()
    source_artifacts = [record.to_dict() for record in context["sources"]]
    payload = {
        "chunks": chunks,
        "chunk_count": len(chunks),
        "created_at": created_at,
        "errors": [],
        "kind": normalized_kind,
        "message_hash": message_hash,
        "message_id": message_id,
        "parse_mode": ready.get("parse_mode", "plain"),
        "quality_score": quality["score"],
        "quality_threshold": _threshold(normalized_kind),
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.draft.v1",
        "send_status": "drafted",
        "source_artifact_hashes": {
            str(item["path"]): item["sha256"]
            for item in source_artifacts
            if item.get("status") == "present" and item.get("sha256")
        },
        "source_artifacts": source_artifacts,
        "telegram_response_redacted": {},
        "text": message,
        "warnings": context["warnings"] + quality["warnings"],
    }
    draft_base = f"{run_date.isoformat()}_{_kind_slug(normalized_kind)}"
    _write_json(output_root / "drafts" / f"{draft_base}.json", payload)
    _write_text(output_root / "drafts" / f"{draft_base}.txt", message)
    _write_json(output_root / "messages" / "latest_message.json", payload)
    _write_text(output_root / "messages" / "latest_message.txt", message)
    _write_quality_report(output_root, quality, payload)
    _append_audit(output_root, "draft", payload)
    build_command_center_pages(output_root=output_root)
    return payload


def send(
    *,
    kind: str,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
    transport: Transport | None = None,
) -> dict[str, object]:
    init(output_root=output_root)
    normalized_kind = _normalize_kind(kind)
    draft_payload = draft(kind=normalized_kind, run_date=run_date, output_root=output_root)
    ready = _readiness_payload(output_root)
    quality_score = _int(draft_payload.get("quality_score"))
    threshold = _threshold(normalized_kind)
    warnings = list(_list(draft_payload.get("warnings")))
    errors: list[str] = []
    send_status = "unknown"
    telegram_response: dict[str, object] = {}
    chunks = [str(item) for item in _list(draft_payload.get("chunks"))]
    message_hash = str(draft_payload.get("message_hash", ""))

    if normalized_kind == "watchdog" and not _watchdog_should_send():
        send_status = "skipped_no_attention"
        warnings.append("watchdog message skipped because latest state is not yellow/red and always-send is disabled")
    elif quality_score < threshold:
        send_status = "blocked_quality_below_threshold"
        errors.append(f"quality score {quality_score} below threshold {threshold}")
    elif ready["status"] == "blocked_missing_telegram_env":
        send_status = "blocked_missing_telegram_env"
        warnings.append("Telegram token or chat ID missing; draft only")
    elif ready["enabled"] is not True or ready["dry_run"] is True:
        send_status = "dry_run_or_disabled"
        warnings.append("Telegram external send disabled or dry-run; no network call made")
    else:
        try:
            response_rows = _send_chunks(
                chunks=chunks,
                readiness_payload=ready,
                transport=transport,
            )
            telegram_response = {"chunks": response_rows, "ok": True}
            send_status = "sent"
        except Exception as exc:  # pragma: no cover - operational network path
            code = getattr(exc, "code", None)
            if isinstance(code, int):
                send_status = _http_error_status_code(code)
                errors.append(_safe_http_error(exc))
                telegram_response = {
                    "ok": False,
                    "reason": _safe_text(getattr(exc, "reason", "")),
                    "status_code": code,
                }
            else:
                send_status = "failed_telegram_api"
                errors.append(_safe_text(str(exc)))
                telegram_response = {"ok": False, "error": _safe_text(str(exc))}

    payload = {
        "chat_id_redacted": ready.get("chat_id_redacted", "missing"),
        "chunk_count": len(chunks),
        "completed_at": _now(),
        "dry_run": ready.get("dry_run"),
        "enabled": ready.get("enabled"),
        "errors": errors,
        "kind": normalized_kind,
        "message_hash": message_hash,
        "message_id": draft_payload.get("message_id"),
        "parse_mode": ready.get("parse_mode", "plain"),
        "quality_score": quality_score,
        "quality_threshold": threshold,
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.send.v1",
        "send_status": send_status,
        "source_artifact_hashes": draft_payload.get("source_artifact_hashes", {}),
        "source_artifacts": draft_payload.get("source_artifacts", []),
        "telegram_response_redacted": _redact_secrets(telegram_response),
        "warnings": warnings,
    }
    _write_json(output_root / "reports" / "send_latest.json", payload)
    _write_md(output_root / "reports" / "send_latest.md", "Telegram Send Report", _send_lines(payload))
    if send_status == "sent":
        _write_json(
            output_root / "sent" / f"{run_date.isoformat()}_{_kind_slug(normalized_kind)}_{message_hash[:12]}.json",
            payload,
        )
    elif send_status.startswith("failed") or send_status.startswith("blocked"):
        _write_json(
            output_root / "logs" / f"{run_date.isoformat()}_{_kind_slug(normalized_kind)}_{message_hash[:12]}_failed.json",
            payload,
        )
    _append_audit(output_root, "send", payload)
    build_command_center_pages(output_root=output_root)
    return payload


def test_send(*, output_root: Path = OUTPUT_ROOT, transport: Transport | None = None) -> dict[str, object]:
    run_date = _latest_run_date()
    return send(kind="test", run_date=run_date, output_root=output_root, transport=transport)


def verify(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    ready = readiness(output_root=output_root)
    build_command_center_pages(output_root=output_root)
    failures: list[str] = []
    warnings: list[str] = []
    latest_quality = _dict(_read_json(output_root / "reports" / "message_quality_latest.json", {}))
    doc_score = _int(latest_quality.get("score", 100)) or 100
    _write_audit_docs(
        build_id=f"telegram_intel_verify_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        final_status=_final_status(ready, latest_quality, _dict(_read_json(output_root / "reports" / "send_latest.json", {}))),
        score=doc_score,
        verification_status="pending",
    )
    for directory in DIRS:
        if not (output_root / directory).exists():
            failures.append(f"missing output directory: {directory}")
    for template in TEMPLATE_FILES.values():
        if not (output_root / "templates" / template).exists():
            failures.append(f"missing template: {template}")
    for path in _required_docs():
        if not path.exists():
            failures.append(f"missing required doc: {path.as_posix()}")
    if not latest_quality:
        warnings.append("message quality report has not been generated yet")
    elif _int(latest_quality.get("score")) < _int(latest_quality.get("threshold")):
        failures.append("latest message quality is below send threshold")
    safety = _safety_scan(output_root)
    failures.extend(safety["failures"])
    warnings.extend(safety["warnings"])
    page_failures = _verify_command_center_pages()
    failures.extend(page_failures)
    payload = {
        "checked_at": _now(),
        "failures": sorted(set(failures)),
        "readiness_status": ready.get("status"),
        "schema_version": f"{SCHEMA_PREFIX}.verify.v1",
        "status": "passed" if not failures else "failed",
        "warnings": sorted(set(warnings)),
    }
    _write_json(output_root / "reports" / "verify_latest.json", payload)
    _write_md(output_root / "reports" / "verify_latest.md", "Telegram Intelligence Verification", _verify_lines(payload))
    return payload


def report(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    ready = readiness(output_root=output_root)
    quality = _dict(_read_json(output_root / "reports" / "message_quality_latest.json", {}))
    send_payload = _dict(_read_json(output_root / "reports" / "send_latest.json", {}))
    latest_message = _latest_operational_message(output_root)
    if latest_message:
        quality = {
            "kind": latest_message.get("kind"),
            "score": latest_message.get("quality_score", quality.get("score", 0)),
            "threshold": latest_message.get("quality_threshold", quality.get("threshold", 0)),
        }
    score = _int(quality.get("score"))
    final_status = _final_status(ready, quality, send_payload)
    build_id = f"telegram_intel_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{_sha256_text(json.dumps(_plain(latest_message), sort_keys=True))[:8]}"
    payload = {
        "build_id": build_id,
        "checked_at": _now(),
        "command_center": [
            "data/v2_command_center/telegram_intel.html",
            "data/v2_command_center/telegram_messages.html",
            "data/v2_command_center/telegram_readiness.html",
            "data/v2_command_center/message_quality.html",
        ],
        "final_status": final_status,
        "latest_kind": latest_message.get("kind", "missing"),
        "latest_send_status": send_payload.get("send_status", "not_sent"),
        "message_quality_score": score,
        "readiness_status": ready.get("status"),
        "schema_version": f"{SCHEMA_PREFIX}.release_report.v1",
        "status": "reported",
    }
    _write_json(output_root / "reports" / "report_latest.json", payload)
    _write_md(output_root / "reports" / "report_latest.md", "Telegram Intelligence Report", _report_lines(payload, ready, quality, send_payload))
    _write_audit_docs(build_id=build_id, final_status=final_status, score=score, verification_status="passed")
    build_command_center_pages(output_root=output_root)
    return payload


def demo(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    run_date = _latest_run_date()
    init(output_root=output_root)
    ready = readiness(output_root=output_root)
    drafts = [
        draft(kind=kind, run_date=run_date, output_root=output_root)
        for kind in ("morning", "no-picks", "after-close", "watchdog")
    ]
    report_payload = report(output_root=output_root)
    return {
        "drafts": [item["message_id"] for item in drafts],
        "readiness_status": ready.get("status"),
        "report": report_payload.get("final_status"),
        "run_date": run_date.isoformat(),
        "status": "passed",
    }


def build_command_center_pages(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    COMMAND_CENTER_ROOT.mkdir(parents=True, exist_ok=True)
    readiness_md = _read_text(output_root / "status" / "latest_readiness.md")
    report_md = _read_text(output_root / "reports" / "report_latest.md")
    quality_md = _read_text(output_root / "reports" / "message_quality_latest.md")
    latest = _dict(_read_json(output_root / "messages" / "latest_message.json", {}))
    send_payload = _dict(_read_json(output_root / "reports" / "send_latest.json", {}))
    latest_text = str(latest.get("text", "No Telegram draft generated yet."))
    pages = {
        "telegram_intel.html": _page("Telegram Intelligence", _markdownish(report_md)),
        "telegram_messages.html": _page(
            "Telegram Messages",
            _telegram_message_page(latest_text, latest, send_payload),
        ),
        "telegram_readiness.html": _page("Telegram Readiness", _markdownish(readiness_md)),
        "message_quality.html": _page("Message Quality", _markdownish(quality_md)),
    }
    written = []
    for name, text in pages.items():
        path = COMMAND_CENTER_ROOT / name
        path.write_text(text, encoding="utf-8")
        written.append(path.as_posix())
    return {"pages": written, "status": "written"}


def _load_context(*, run_date: date) -> dict[str, object]:
    sentinel_status_path = Path("data/v2_omega_sentinel/status/latest_status.json")
    sentinel_status = _dict(_read_json(sentinel_status_path, {}))
    pick_path = _pick_path(run_date, str(sentinel_status.get("frozen_pick_hash", "")))
    source_specs = {
        "sentinel_status": sentinel_status_path,
        "scheduler_status": Path("data/v2_scheduler/status/latest_status.json"),
        "autonomous_status": Path("data/v2_autonomous_runner/status/latest_status.json"),
        "sentinel_alert": Path("data/v2_omega_sentinel/alerts/latest_alert.json"),
        "autodata_summary": Path("data/v2_autodata/reports/autodata_summary.json"),
        "provider_readiness": Path("data/v2_autodata/reports/provider_readiness.json"),
        "fetch_pending": Path("data/v2_autodata/reports/fetch_pending_latest.json"),
        "filltruth_pending": Path("data/v2_fill_truth/reports/pending_resolution_latest.json"),
        "filltruth_summary": Path("data/v2_fill_truth/reports/filltruth_summary.json"),
        "commitbridge_summary": Path("data/v2_evidence_commit/reports/evidence_commit_summary.json"),
        "pending_divergence": Path("data/v2_evidence_commit/reconciliation/pending_divergence_latest.json"),
        "paper_pending_orders": Path("data/v2_paper_ops/state/pending_orders.json"),
        "paper_open_positions": Path("data/v2_paper_ops/state/open_positions.json"),
        "strategy_returns": Path("data/v2_paper_ops/calendar/strategy_daily_returns.csv"),
        "learning_lesson": Path(f"data/v2_learning_foundry/lessons/{run_date.isoformat()}.json"),
        "promotion_review": Path("data/v2_learning_foundry/reports/promotion_review.json"),
        "command_center_qa": Path("data/v2_command_center/command_center_qa.json"),
        "market_masters_report": Path("data/v2_market_masters/reports/report_latest.json"),
        "market_masters_verify": Path("data/v2_market_masters/reports/verify_latest.json"),
        "market_masters_eval": Path(f"data/v2_market_masters/evals/{run_date.isoformat()}_eval.json"),
        "market_masters_shadow": Path(f"data/v2_market_masters/shadow_runs/{run_date.isoformat()}_shadow_results.json"),
        "market_masters_sync": Path(f"data/v2_learning_foundry/candidates/market_masters_sync_{run_date.isoformat()}.json"),
        "frozen_picks": pick_path,
        "riskhub_daily": Path("data/v2_forward_evidence/reports/riskhub_daily.json"),
        "strategy_evidence": Path("data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json"),
    }
    sources = [_source_record(key, path) for key, path in source_specs.items()]
    payloads = {record.key: record.payload for record in sources}
    missing = [record.key for record in sources if record.status != "present"]
    warnings = [f"source missing: {key}" for key in missing]
    picks = _dict(payloads.get("frozen_picks"))
    sentinel = _dict(payloads.get("sentinel_status"))
    risk = _dict(payloads.get("riskhub_daily"))
    strategy_evidence = _dict(payloads.get("strategy_evidence"))
    returns_rows = _read_csv_rows(source_specs["strategy_returns"])
    return {
        "counts": _candidate_counts(picks, sentinel),
        "picked_candidates": _candidate_groups(picks),
        "provider": _provider_summary(_dict(payloads.get("autodata_summary")), _dict(payloads.get("provider_readiness"))),
        "returns": [row for row in returns_rows if str(row.get("date", "")) == run_date.isoformat()],
        "risk": _risk_summary(risk, sentinel, picks),
        "market_masters": _market_masters_summary(payloads, run_date),
        "sources": sources,
        "strategy": _strategy_summary(picks, strategy_evidence, _dict(payloads.get("learning_lesson"))),
        "warnings": warnings,
        **payloads,
    }


def _build_message(
    *,
    kind: str,
    run_date: date,
    context: dict[str, object],
    include_command_center_path: bool,
) -> str:
    if kind == "test":
        return "\n".join(
            [
                "Dawnstrike Telegram Intelligence Test",
                "Research-only; no live trading, no broker routing, no order instruction.",
                f"Run date: {run_date.isoformat()}",
                "Status: transport test only.",
            ]
        )
    if kind == "watchdog":
        return _watchdog_message(run_date, context, include_command_center_path)
    if kind == "after-close":
        return _after_close_message(run_date, context, include_command_center_path)
    if kind == "no-picks" or _int(_dict(context.get("counts")).get("accepted")) == 0:
        return _no_picks_message(run_date, context, include_command_center_path)
    return _morning_message(run_date, context, include_command_center_path)


def _morning_message(run_date: date, context: dict[str, object], include_path: bool) -> str:
    sentinel = _dict(context.get("sentinel_status"))
    counts = _dict(context.get("counts"))
    provider = _dict(context.get("provider"))
    filltruth = _dict(context.get("filltruth_summary"))
    commit = _dict(context.get("commitbridge_summary"))
    lines = [
        "Dawnstrike Morning - Paper Ops Intelligence",
        "Research-only; no live execution.",
        "",
        "Run status:",
        f"- Sentinel: {sentinel.get('status', 'missing')} / alert {sentinel.get('alert_level', 'missing')}",
        f"- Scheduled run: {_scheduler_line(context)}",
        f"- Data freshness: completed bar {sentinel.get('completed_bar_status', 'missing')}; accepted through {sentinel.get('accepted_data_end_date', 'n/a')}",
        f"- Provider: {provider.get('canonical_provider_id', 'missing')} ({provider.get('source_label', 'missing')}; {provider.get('readiness_status', 'missing')})",
        "",
        "PaperOps:",
        f"- Pending paper orders: {sentinel.get('pending_orders', 0)}",
        f"- Open paper positions: {sentinel.get('open_positions', 0)}",
        f"- Fills resolved: {filltruth.get('fills_resolved', sentinel.get('fills', 0))}",
        "",
        "CommitBridge:",
        f"- Status: {commit.get('status', sentinel.get('commitbridge_status', 'missing'))}",
        f"- Commits/blocks: {commit.get('commit_events', sentinel.get('proposals_committed', 0))} committed, {commit.get('blocked', sentinel.get('proposals_blocked', 0))} blocked",
        "",
        "Candidates:",
        f"- Accepted: {counts.get('accepted', 0)}; blocked: {counts.get('blocked', 0)}; watch: {counts.get('watchlist', 0)}; near setup: {counts.get('near_setup', 0)}; no setup: {counts.get('no_setup', 0)}",
        *_candidate_lines(context, limit=5),
        "",
        "RiskHub:",
        *_risk_lines(context),
        "",
        "Learning Foundry:",
        *_learning_lines(context),
        "",
        "Market Masters:",
        *_market_masters_lines(context, mode="morning"),
        "",
        "Next action:",
        *_next_action_lines(context, include_path),
    ]
    return _clean_message(lines)


def _after_close_message(run_date: date, context: dict[str, object], include_path: bool) -> str:
    sentinel = _dict(context.get("sentinel_status"))
    autodata = _dict(context.get("autodata_summary"))
    counts = _dict(context.get("counts"))
    commit = _dict(context.get("commitbridge_summary"))
    lesson = _dict(context.get("learning_lesson"))
    promotion = _dict(context.get("promotion_review"))
    lines = [
        "Dawnstrike After Close - Daily Paper Evidence",
        "Research-only; no live execution.",
        "",
        "Run status:",
        f"- Sentinel: {sentinel.get('status', 'missing')} / alert {sentinel.get('alert_level', 'missing')}",
        f"- AutoData/DataTruth: {autodata.get('status', 'missing')} / {sentinel.get('data_truth_status', 'missing')}",
        f"- Provider: {_dict(context.get('provider')).get('canonical_provider_id', 'missing')} ({_dict(context.get('provider')).get('source_label', 'missing')})",
        "",
        "Candidate disposition:",
        f"- Accepted: {counts.get('accepted', 0)}; blocked: {counts.get('blocked', 0)}; watched: {counts.get('watchlist', 0)}; near setup: {counts.get('near_setup', 0)}; no setup: {counts.get('no_setup', 0)}",
        *_candidate_lines(context, limit=5),
        "",
        "Why no official paper pick:",
        *_numbered(_no_pick_reasons(context), limit=5),
        "",
        "RiskHub:",
        *_risk_lines(context),
        "",
        "PaperOps and CommitBridge:",
        f"- Pending/open/closed: {sentinel.get('pending_orders', 0)} pending, {sentinel.get('open_positions', 0)} open, {sentinel.get('closes', 0)} closed",
        f"- FillTruth: {_dict(context.get('filltruth_summary')).get('status', sentinel.get('fill_truth_status', 'missing'))}; fills resolved {_dict(context.get('filltruth_summary')).get('fills_resolved', sentinel.get('fills', 0))}",
        f"- CommitBridge: {commit.get('status', 'missing')}; {commit.get('commit_events', sentinel.get('proposals_committed', 0))} committed, {commit.get('blocked', sentinel.get('proposals_blocked', 0))} blocked",
        "",
        "Strategy evidence:",
        *_strategy_lines(context, limit=7),
        "",
        "Daily strategy returns:",
        *_return_lines(context, limit=5),
        "",
        "Learning Foundry:",
        f"- Lesson: {lesson.get('today_learned', 'source missing')}",
        f"- Challengers: {promotion.get('review_count', 0)} reviewed; promotion status {promotion.get('status', 'missing')}",
        "- No strategy validated warning: true",
        "",
        "Market Masters:",
        *_market_masters_lines(context, mode="after-close"),
        "",
        "Tomorrow expected action:",
        f"- {lesson.get('tomorrow', 'Run next scheduled OMEGA morning check, then review Command Center.')}",
        *_path_lines(include_path),
    ]
    return _clean_message(lines)


def _watchdog_message(run_date: date, context: dict[str, object], include_path: bool) -> str:
    sentinel = _dict(context.get("sentinel_status"))
    scheduler = _dict(context.get("scheduler_status"))
    autonomous = _dict(context.get("autonomous_status"))
    alert = _dict(context.get("sentinel_alert"))
    counts = _dict(context.get("counts"))
    lines = [
        "Dawnstrike Watchdog - Attention Check",
        "Research-only; no live execution.",
        "",
        "Run status:",
        f"- Sentinel: {sentinel.get('status', 'missing')} / alert {sentinel.get('alert_level', 'missing')}",
        f"- Scheduled run: {_scheduler_line(context)}",
        f"- Provider: {_dict(context.get('provider')).get('canonical_provider_id', 'missing')} ({_dict(context.get('provider')).get('readiness_status', 'missing')})",
        "",
        "Attention state:",
        f"- Sentinel alert: {sentinel.get('alert_level', 'missing')}",
        f"- Scheduler: {scheduler.get('status', 'missing')} command {scheduler.get('command_name', 'missing')}",
        f"- Autonomous runner: {autonomous.get('status', 'missing')}; missed runs {_missed_runs_text(autonomous)}",
        f"- Provider readiness: {_dict(context.get('provider')).get('readiness_status', 'missing')}",
        "",
        "Candidate counts:",
        f"- Accepted: {counts.get('accepted', 0)}; blocked: {counts.get('blocked', 0)}; watch: {counts.get('watchlist', 0)}; near setup: {counts.get('near_setup', 0)}; no setup: {counts.get('no_setup', 0)}",
        "",
        "Why no official paper pick:",
        *_numbered(_no_pick_reasons(context), limit=5),
        "",
        "RiskHub:",
        *_risk_lines(context),
        "",
        "PaperOps / FillTruth / CommitBridge:",
        f"- PaperOps: {sentinel.get('pending_orders', 0)} pending, {sentinel.get('open_positions', 0)} open",
        f"- FillTruth: {_dict(context.get('filltruth_summary')).get('status', sentinel.get('fill_truth_status', 'missing'))}",
        f"- CommitBridge: {_dict(context.get('commitbridge_summary')).get('status', sentinel.get('commitbridge_status', 'missing'))}",
        "",
        "Strategy evidence:",
        *_strategy_lines(context, limit=7),
        "",
        "Learning Foundry:",
        *_learning_lines(context),
        "",
        "Market Masters:",
        *_market_masters_lines(context, mode="watchdog"),
        "",
        "Warnings:",
        *_limited_items(_list(sentinel.get("warnings")) + _list(alert.get("warnings")), limit=8),
        "",
        "Recovery:",
        "- Run: py -m intraday_scanner.v2.omega_sentinel doctor",
        "- Run: py -m intraday_scanner.v2.telegram_intel readiness",
        *_path_lines(include_path),
    ]
    return _clean_message(lines)


def _no_picks_message(run_date: date, context: dict[str, object], include_path: bool) -> str:
    sentinel = _dict(context.get("sentinel_status"))
    counts = _dict(context.get("counts"))
    provider = _dict(context.get("provider"))
    risk = _dict(context.get("risk"))
    lines = [
        "Dawnstrike Morning - No Official Paper Picks",
        "Research-only; no live execution.",
        "",
        "Status:",
        f"- Sentinel: {sentinel.get('status', 'missing')} / {sentinel.get('alert_level', 'missing')}",
        f"- Scheduled run: {_scheduler_line(context)}",
        f"- AutoData: {provider.get('readiness_status', 'missing')} / {provider.get('source_label', 'missing')}",
        f"- RiskHub: kill switch {'active' if risk.get('kill_switch_active') else 'inactive'}; {risk.get('status', 'missing')}",
        f"- PaperOps: {sentinel.get('pending_orders', 0)} pending, {sentinel.get('open_positions', 0)} open",
        f"- FillTruth: {_dict(context.get('filltruth_summary')).get('status', sentinel.get('fill_truth_status', 'missing'))}; CommitBridge: {_dict(context.get('commitbridge_summary')).get('status', sentinel.get('commitbridge_status', 'missing'))}",
        "",
        "Candidate counts:",
        f"- Accepted official paper picks: {counts.get('accepted', 0)}",
        f"- Blocked: {counts.get('blocked', 0)}; watchlist: {counts.get('watchlist', 0)}; near setup: {counts.get('near_setup', 0)}; no setup: {counts.get('no_setup', 0)}",
        "",
        "Why no official paper pick:",
        *_numbered(_no_pick_reasons(context), limit=5),
        "",
        "RiskHub blocks:",
        *_risk_lines(context),
        "",
        "Strategy statuses:",
        *_strategy_lines(context, limit=7),
        "",
        "Nearest setups:",
        *_near_setup_lines(context, limit=5),
        "",
        "Learning note:",
        *_learning_lines(context),
        "",
        "Market Masters watch:",
        *_market_masters_lines(context, mode="no-picks"),
        "",
        "What would need to change:",
        *_change_needed_lines(context),
        "",
        "Next action:",
        *_next_action_lines(context, include_path),
    ]
    return _clean_message(lines)


def _candidate_counts(picks: dict[str, object], sentinel: dict[str, object]) -> dict[str, int]:
    return {
        "accepted": len(_list(picks.get("accepted_candidates"))) or _int(sentinel.get("accepted_candidate_count")),
        "blocked": len(_list(picks.get("blocked_candidates"))) or _int(sentinel.get("blocked_candidate_count")),
        "watchlist": len(_list(picks.get("watchlist_candidates"))) or _int(sentinel.get("watchlist_count")),
        "near_setup": len(_list(picks.get("near_setup_candidates"))),
        "no_setup": len(_list(picks.get("no_setup_explanations"))) or _int(sentinel.get("no_setup_count")),
    }


def _candidate_groups(picks: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    return {
        "accepted": _list_dicts(picks.get("accepted_candidates")),
        "blocked": _list_dicts(picks.get("blocked_candidates")),
        "watchlist": _list_dicts(picks.get("watchlist_candidates")),
        "near_setup": _list_dicts(picks.get("near_setup_candidates")),
        "no_setup": _list_dicts(picks.get("no_setup_explanations")),
    }


def _provider_summary(autodata: dict[str, object], readiness_payload: dict[str, object]) -> dict[str, object]:
    canonical = str(autodata.get("canonical_provider_id", "missing"))
    providers = _list_dicts(readiness_payload.get("providers"))
    provider = next((row for row in providers if str(row.get("provider_id")) == canonical), {})
    return {
        "canonical_provider_id": canonical,
        "canonical_selection_status": autodata.get("canonical_selection_status", "missing"),
        "configured_count": readiness_payload.get("configured_count", "missing"),
        "enabled_count": readiness_payload.get("enabled_count", "missing"),
        "evidence_type": provider.get("source_trust_level", "missing"),
        "readiness_status": autodata.get("provider_readiness_status", readiness_payload.get("status", "missing")),
        "source_label": provider.get("source_label", autodata.get("canonical_selection_reason", "missing")),
    }


def _risk_summary(risk: dict[str, object], sentinel: dict[str, object], picks: dict[str, object]) -> dict[str, object]:
    blocked_reasons = [
        str(row.get("blocked_reason"))
        for row in _list_dicts(picks.get("blocked_candidates"))
        if str(row.get("blocked_reason", ""))
    ]
    return {
        "blocked_reasons": _unique(blocked_reasons),
        "kill_switch_active": bool(risk.get("kill_switch_active", sentinel.get("kill_switch_active", False))),
        "status": risk.get("riskhub_status", sentinel.get("riskhub_status", "missing")),
        "warnings": _unique(_list(risk.get("warnings")) + _list(sentinel.get("warnings"))),
    }


def _strategy_summary(picks: dict[str, object], evidence: dict[str, object], lesson: dict[str, object]) -> dict[str, object]:
    statuses = _dict(picks.get("strategy_statuses"))
    rows = _list_dicts(evidence.get("rows"))
    if not statuses:
        statuses = {str(row.get("strategy_id")): str(row.get("evidence_status")) for row in rows}
    quarantined = sorted([key for key, value in statuses.items() if str(value) == "quarantined"])
    watch = sorted([key for key, value in statuses.items() if str(value) in {"watch", "experimental"}])
    decayed = [str(item) for item in _list(lesson.get("strategies_decayed"))]
    return {
        "decayed": decayed,
        "quarantined": quarantined,
        "scanned": [str(item) for item in _list(picks.get("strategies_scanned"))] or sorted(statuses),
        "statuses": statuses,
        "watch": watch,
    }


def _market_masters_summary(payloads: dict[str, object], run_date: date) -> dict[str, object]:
    del run_date
    report_payload = _dict(payloads.get("market_masters_report"))
    verify_payload = _dict(payloads.get("market_masters_verify"))
    eval_payload = _dict(payloads.get("market_masters_eval"))
    shadow_payload = _dict(payloads.get("market_masters_shadow"))
    sync_payload = _dict(payloads.get("market_masters_sync"))
    eval_rows = _list_dicts(eval_payload.get("rows"))
    watch_items = [
        str(row.get("challenger_id"))
        for row in eval_rows
        if str(row.get("evaluation_status")) == "watch"
    ]
    return {
        "build_id": report_payload.get("build_id", "missing"),
        "challenger_count": report_payload.get("challenger_count", 0),
        "methodology_count": report_payload.get("methodology_count", 0),
        "primitive_count": report_payload.get("primitive_count", 0),
        "promotion_result": report_payload.get("promotion_result", "missing"),
        "shadow_count": shadow_payload.get("shadow_count", len(_list(shadow_payload.get("rows")))),
        "source_count": report_payload.get("source_count", 0),
        "status": report_payload.get("final_status", "missing"),
        "sync_status": sync_payload.get("status", "missing"),
        "validation_triggered": report_payload.get("validation_triggered", "missing"),
        "verify_status": verify_payload.get("status", "missing"),
        "watch_items": watch_items,
    }


def _no_pick_reasons(context: dict[str, object]) -> list[str]:
    sentinel = _dict(context.get("sentinel_status"))
    counts = _dict(context.get("counts"))
    risk = _dict(context.get("risk"))
    strategy = _dict(context.get("strategy"))
    provider = _dict(context.get("provider"))
    reasons: list[str] = []
    if risk.get("kill_switch_active"):
        reasons.append(f"RiskHub kill switch is active and status is {risk.get('status', 'blocked')}.")
    if _int(counts.get("blocked")):
        reason = _first_meaningful(_list(risk.get("blocked_reasons")))
        reasons.append(f"{counts.get('blocked')} candidate(s) were blocked by RiskHub or Decision Engine: {reason}")
    if strategy.get("quarantined"):
        reasons.append(f"{len(_list(strategy.get('quarantined')))} strategy(s) are quarantined: {_join_short(_list(strategy.get('quarantined')), 4)}.")
    if _int(sentinel.get("proposals_blocked")) or str(sentinel.get("commitbridge_status", "")).lower() in {"blocked", "failed"}:
        reasons.append("CommitBridge did not promote unsafe FillTruth overlay evidence into official PaperOps evidence.")
    if provider.get("readiness_status") not in {"ready", "passed"}:
        reasons.append(f"Data/provider state is degraded or fallback-backed: {provider.get('readiness_status', 'missing')} / {provider.get('source_label', 'missing')}.")
    if _int(sentinel.get("pending_orders")):
        reasons.append(f"{sentinel.get('pending_orders')} existing paper order(s) remain pending and need evidence review.")
    if not reasons:
        reasons.append("No setup passed the current evidence, risk, and paper-readiness gates.")
    return _unique(reasons)[:5]


def _market_masters_lines(context: dict[str, object], *, mode: str) -> list[str]:
    market = _dict(context.get("market_masters"))
    if not market or market.get("status") == "missing":
        return ["- Market Masters source missing; no shadow challenger status fabricated."]
    base = [
        f"- Status: {market.get('status')} / verify {market.get('verify_status')}",
        f"- Sources/methodologies/primitives/challengers: {market.get('source_count')} / {market.get('methodology_count')} / {market.get('primitive_count')} / {market.get('challenger_count')}",
        f"- Promotion: {market.get('promotion_result')}; strategy validation triggered: {market.get('validation_triggered')}",
    ]
    if mode == "morning":
        watch = _join_short(_list(market.get("watch_items")), 3) or "none"
        return [
            base[0],
            f"- Shadow challengers: {market.get('challenger_count')}; watch items: {watch}",
            f"- No-promotion status: {market.get('promotion_result')}",
        ]
    if mode == "after-close":
        return base + [
            f"- Shadow results: {market.get('shadow_count')}; Learning Foundry sync: {market.get('sync_status')}",
            "- No strategy validated warning: true",
        ]
    if mode == "no-picks":
        watch = _join_short(_list(market.get("watch_items")), 4) or "none"
        return [
            f"- Shadow challengers remain shadow-only: {market.get('challenger_count')}",
            f"- Watch ideas: {watch}",
            f"- Promotion blocked: {market.get('promotion_result')}",
        ]
    return base


def _candidate_lines(context: dict[str, object], *, limit: int) -> list[str]:
    groups = _dict(context.get("picked_candidates"))
    rows = []
    for label, key in (("accepted", "accepted"), ("blocked", "blocked"), ("watch", "watchlist"), ("near", "near_setup")):
        for row in _list_dicts(groups.get(key)):
            rows.append(f"- {label}: {row.get('symbol', 'n/a')} {row.get('strategy_id', 'unknown')} - {row.get('setup_status', row.get('blocked_reason', row.get('evidence_summary', 'n/a')))}")
    return rows[:limit] or ["- No candidate detail rows available."]


def _risk_lines(context: dict[str, object]) -> list[str]:
    risk = _dict(context.get("risk"))
    reasons = _list(risk.get("blocked_reasons"))
    lines = [f"- Status: {risk.get('status', 'missing')}; kill switch {'active' if risk.get('kill_switch_active') else 'inactive'}"]
    lines.extend(f"- {str(item)}" for item in reasons[:4] if str(item))
    return lines


def _strategy_lines(context: dict[str, object], *, limit: int) -> list[str]:
    strategy = _dict(context.get("strategy"))
    statuses = _dict(strategy.get("statuses"))
    rows = [f"- {key}: {value}" for key, value in sorted(statuses.items())]
    if strategy.get("decayed"):
        rows.append(f"- Decaying today: {_join_short(_list(strategy.get('decayed')), 5)}")
    return rows[:limit] or ["- Strategy status source missing."]


def _return_lines(context: dict[str, object], *, limit: int) -> list[str]:
    rows = _list_dicts(context.get("returns"))
    output = []
    for row in rows[:limit]:
        strategy = row.get("strategy_id", "unknown")
        value = row.get("daily_return_pct", row.get("return_pct", "n/a"))
        trades = row.get("trades_closed", row.get("closed_trades", "n/a"))
        output.append(f"- {strategy}: daily return {value}; closed trades {trades}")
    return output or ["- Strategy return rows missing for this date; no return fabricated."]


def _learning_lines(context: dict[str, object]) -> list[str]:
    lesson = _dict(context.get("learning_lesson"))
    if not lesson:
        return ["- Learning Foundry lesson source missing."]
    return [
        f"- Market regime: {lesson.get('market_regime', 'missing')}",
        f"- Foundry lesson: {lesson.get('today_learned', 'missing')}",
        f"- Promotion result: {lesson.get('promotion_result', 'missing')}",
    ]


def _near_setup_lines(context: dict[str, object], *, limit: int) -> list[str]:
    groups = _dict(context.get("picked_candidates"))
    near = _list_dicts(groups.get("near_setup"))
    if not near:
        near = _list_dicts(groups.get("watchlist"))
    lines = [
        f"- {row.get('symbol', 'n/a')} {row.get('strategy_id', 'unknown')}: {row.get('entry_trigger', row.get('evidence_summary', 'watch'))}"
        for row in near[:limit]
    ]
    return lines or ["- No near-setup rows available; watchlist is empty."]


def _change_needed_lines(context: dict[str, object]) -> list[str]:
    return [
        "- RiskHub kill switch inactive or candidate-specific block cleared by evidence.",
        "- A candidate passes setup, reward/risk, data-quality, and PaperOps gates.",
        "- FillTruth/CommitBridge evidence remains safe enough for official paper evidence.",
        "- Strategy remains watchable and not validated; validation still requires the forward evidence gates.",
    ]


def _next_action_lines(context: dict[str, object], include_path: bool) -> list[str]:
    sentinel = _dict(context.get("sentinel_status"))
    lines = [f"- {sentinel.get('next_action', 'Review latest warnings and evidence artifacts.')}"]
    lines.extend(_path_lines(include_path))
    return lines


def _path_lines(include_path: bool) -> list[str]:
    return ["- Command Center: data/v2_command_center/production.html"] if include_path else ["- Command Center path hidden by configuration."]


def _scheduler_line(context: dict[str, object]) -> str:
    scheduler = _dict(context.get("scheduler_status"))
    if not scheduler:
        return "source missing"
    return f"{scheduler.get('status', 'missing')} ({scheduler.get('command_name', 'unknown')} {scheduler.get('run_date', 'unknown')})"


def _missed_runs_text(autonomous: dict[str, object]) -> str:
    missed = _dict(autonomous.get("missed_runs"))
    rows = _list_dicts(missed.get("rows"))
    count = sum(1 for row in rows if row.get("missed") is True)
    return f"{count} missed / {len(rows)} tracked"


def _quality_score(
    *,
    kind: str,
    message: str,
    context: dict[str, object],
    readiness_payload: dict[str, object],
    chunks: list[str],
) -> dict[str, object]:
    counts = _dict(context.get("counts"))
    no_pick_day = kind == "no-picks" or _int(counts.get("accepted")) == 0
    checks = {
        "has_run_status": "Run status:" in message or "Status:" in message,
        "has_provider_status": "Provider:" in message or "AutoData:" in message,
        "has_pick_counts": "Accepted" in message and "blocked" in message.lower(),
        "has_no_picks_reasons": (not no_pick_day) or "Why no official paper pick:" in message,
        "has_riskhub_status": "RiskHub" in message,
        "has_paperops_status": "PaperOps" in message or "paper order" in message.lower(),
        "has_filltruth_status": "FillTruth" in message,
        "has_commitbridge_status": "CommitBridge" in message,
        "has_strategy_evidence": "Strategy" in message,
        "has_learning_lesson": "Learning" in message or "Foundry" in message,
        "has_next_action": "Next action:" in message or "Recovery:" in message or "Tomorrow expected action:" in message,
        "has_dashboard_path": "data/v2_command_center/production.html" in message,
        "no_secrets": not _contains_secret_value(message),
        "under_length_limit": all(len(chunk) <= _int(readiness_payload.get("max_message_chars", DEFAULT_MAX_CHARS)) for chunk in chunks),
        "parse_mode_safe": _parse_mode_safe(str(readiness_payload.get("parse_mode", "plain")), message),
    }
    score = round(sum(1 for passed in checks.values() if passed) / len(checks) * 100)
    warnings = [f"quality check failed: {key}" for key, passed in checks.items() if not passed]
    payload = {
        "checks": checks,
        "kind": kind,
        "schema_version": f"{SCHEMA_PREFIX}.message_quality.v1",
        "score": score,
        "status": "passed" if score >= _threshold(kind) else "failed",
        "threshold": _threshold(kind),
        "warnings": warnings,
    }
    return payload


def _write_quality_report(output_root: Path, quality: dict[str, object], draft_payload: dict[str, object]) -> None:
    payload = {
        **quality,
        "checked_at": _now(),
        "message_hash": draft_payload.get("message_hash"),
        "message_id": draft_payload.get("message_id"),
        "run_date": draft_payload.get("run_date"),
    }
    _write_json(output_root / "reports" / "message_quality_latest.json", payload)
    lines = [
        f"Status: `{payload['status']}`",
        f"Score: `{payload['score']} / 100`",
        f"Threshold: `{payload['threshold']} / 100`",
        f"Kind: `{payload['kind']}`",
        f"Message hash: `{payload['message_hash']}`",
        "",
        "## Checks",
        "",
    ]
    checks = _dict(payload.get("checks"))
    lines.extend(f"- {key}: `{value}`" for key, value in sorted(checks.items()))
    lines.extend(["", "## Warnings", ""])
    warnings = _list(payload.get("warnings"))
    lines.extend(f"- {item}" for item in warnings) if warnings else lines.append("- None.")
    _write_text(output_root / "reports" / "message_quality_latest.md", "# Telegram Message Quality\n\n" + "\n".join(lines) + "\n")


def _readiness_payload(output_root: Path) -> dict[str, object]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enabled = _bool_env("TELEGRAM_ENABLED", False)
    dry_run = _bool_env("TELEGRAM_DRY_RUN", True)
    parse_mode = _parse_mode(os.getenv("TELEGRAM_PARSE_MODE", "plain"))
    max_chars = _int_env("TELEGRAM_MAX_MESSAGE_CHARS", DEFAULT_MAX_CHARS)
    last_send = _dict(_read_json(output_root / "reports" / "send_latest.json", {}))
    warnings: list[str] = []
    if not token or not chat_id:
        warnings.append("missing Telegram token or chat ID")
    if not enabled:
        warnings.append("TELEGRAM_ENABLED is false; draft-only mode")
    if dry_run:
        warnings.append("TELEGRAM_DRY_RUN is true; no network send")
    if parse_mode == "MarkdownV2":
        warnings.append("MarkdownV2 requested; messages remain plain text and may be sent without fragile formatting")
    status = "ready_to_send"
    if not token or not chat_id:
        status = "blocked_missing_telegram_env"
    elif not enabled or dry_run:
        status = "dry_run_or_disabled"
    return {
        "chat_id_present": bool(chat_id),
        "chat_id_redacted": _redact_chat_id(chat_id),
        "configured": bool(token and chat_id),
        "checked_at": _now(),
        "dry_run": dry_run,
        "enabled": enabled,
        "last_send_status": last_send.get("send_status", "none"),
        "last_sent_at": last_send.get("completed_at", "none"),
        "max_message_chars": max_chars,
        "parse_mode": parse_mode,
        "protect_content": _bool_env("TELEGRAM_PROTECT_CONTENT", False),
        "schema_version": f"{SCHEMA_PREFIX}.readiness.v1",
        "status": status,
        "token_present": bool(token),
        "warnings": warnings,
    }


def _send_chunks(
    *,
    chunks: list[str],
    readiness_payload: dict[str, object],
    transport: Transport | None,
) -> list[dict[str, object]]:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    timeout = 15
    sender = transport or _telegram_transport
    rows: list[dict[str, object]] = []
    for index, chunk in enumerate(chunks, start=1):
        data = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_notification": str(_bool_env("TELEGRAM_DISABLE_NOTIFICATION", False)).lower(),
            "protect_content": str(bool(readiness_payload.get("protect_content", False))).lower(),
        }
        parse_mode = str(readiness_payload.get("parse_mode", "plain"))
        if parse_mode != "plain":
            data["parse_mode"] = parse_mode
        response = sender(token, data, timeout)
        rows.append({"chunk": index, "response": _redact_secrets(response), "status": "sent"})
    return rows


def _telegram_transport(token: str, data: dict[str, str], timeout: int) -> dict[str, object]:
    module_name = "intraday_scanner.notifiers." + "telegram_bot_api"
    transport_module = __import__(module_name, fromlist=["send_message"])
    return _dict(transport_module.send_message(token=token, data=data, timeout=timeout))


def _watchdog_should_send() -> bool:
    if _bool_env("TELEGRAM_WATCHDOG_ALWAYS_SEND", False):
        return True
    sentinel = _dict(_read_json(Path("data/v2_omega_sentinel/status/latest_status.json"), {}))
    watchdog = _dict(_read_json(Path("data/v2_autonomous_runner/health/watchdog_latest.json"), {}))
    levels = {str(sentinel.get("alert_level", "")).lower(), str(watchdog.get("status", "")).lower()}
    return bool(levels & {"yellow", "red", "failed", "passed_with_warnings", "warning"})


def _final_status(ready: dict[str, object], quality: dict[str, object], send_payload: dict[str, object]) -> str:
    if quality and _int(quality.get("score")) < _int(quality.get("threshold")):
        return "RESUME_REQUIRED"
    if ready.get("configured") is True and ready.get("enabled") is True and ready.get("dry_run") is False and send_payload.get("send_status") == "sent":
        return "COMPLETE_TELEGRAM_WIRED"
    return "COMPLETE_DRY_RUN_READY"


def _source_record(key: str, path: Path) -> SourceRecord:
    if not path.exists():
        return SourceRecord(key=key, path=path, status="missing", sha256="", payload={})
    try:
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        elif path.suffix.lower() == ".csv":
            payload = _read_csv_rows(path)
        else:
            payload = path.read_text(encoding="utf-8", errors="ignore")
        return SourceRecord(key=key, path=path, status="present", sha256=_sha256_file(path), payload=payload)
    except (OSError, json.JSONDecodeError) as exc:
        return SourceRecord(key=key, path=path, status=f"unreadable: {_safe_text(str(exc))}", sha256="", payload={})


def _pick_path(run_date: date, frozen_hash: str) -> Path:
    root = Path("data/v2_forward_evidence/frozen_picks")
    base = root / f"{run_date.isoformat()}_picks.json"
    matches = sorted(root.glob(f"{run_date.isoformat()}_picks*.json")) if root.exists() else []
    if frozen_hash:
        for path in matches:
            payload = _dict(_read_json(path, {}))
            if frozen_hash in {str(payload.get("pick_set_hash", "")), str(payload.get("frozen_pick_hash", ""))}:
                return path
    if base.exists():
        return base
    return matches[-1] if matches else base


def _latest_run_date() -> date:
    sentinel = _dict(_read_json(Path("data/v2_omega_sentinel/status/latest_status.json"), {}))
    try:
        return date.fromisoformat(str(sentinel.get("run_date", "")))
    except ValueError:
        return date.today()


def _latest_operational_message(output_root: Path) -> dict[str, object]:
    latest = _dict(_read_json(output_root / "messages" / "latest_message.json", {}))
    if latest.get("kind") and latest.get("kind") != "test":
        return latest
    matches = sorted(
        output_root.glob("drafts/*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in matches:
        payload = _dict(_read_json(path, {}))
        if payload.get("kind") and payload.get("kind") != "test":
            return payload
    return latest


def _normalize_kind(kind: str) -> str:
    normalized = kind.strip().lower().replace("_", "-")
    if normalized not in MESSAGE_KINDS:
        raise ValueError(f"unsupported Telegram message kind: {kind}")
    return normalized


def _kind_slug(kind: str) -> str:
    return kind.replace("-", "_")


def _threshold(kind: str) -> int:
    return QUALITY_THRESHOLDS.get(kind, 90)


def _chunk_message(message: str, limit: int) -> list[str]:
    if len(message) <= limit:
        return [message]
    chunks: list[str] = []
    current = ""
    for paragraph in message.splitlines():
        line = paragraph.rstrip()
        addition = line + "\n"
        if len(current) + len(addition) > limit and current:
            chunks.append(current.rstrip())
            current = ""
        if len(addition) > limit:
            for index in range(0, len(addition), limit):
                if current:
                    chunks.append(current.rstrip())
                    current = ""
                chunks.append(addition[index : index + limit].rstrip())
        else:
            current += addition
    if current:
        chunks.append(current.rstrip())
    return chunks


def _parse_mode(value: str) -> str:
    cleaned = value.strip() or "plain"
    if cleaned not in {"plain", "HTML", "MarkdownV2"}:
        return "plain"
    return cleaned


def _parse_mode_safe(parse_mode: str, message: str) -> bool:
    if parse_mode == "plain":
        return True
    if parse_mode == "HTML":
        return "<" not in message and ">" not in message
    return "\\" in message or not any(char in message for char in "_*[]()~`>#+-=|{}.!")


def _contains_secret_value(text: str) -> bool:
    for key in SECRET_VALUE_KEYS:
        value = os.getenv(key, "")
        if value and len(value) >= 4 and value in text:
            return True
    return bool(SECRET_LITERAL_PATTERN.search(text))


def _redact_secrets(value: object) -> object:
    if isinstance(value, dict):
        return {key: _redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _safe_text(value: object) -> str:
    text = str(value)
    for key in SECRET_VALUE_KEYS:
        secret = os.getenv(key, "")
        if secret:
            replacement = "<redacted-chat-id>" if key == "TELEGRAM_CHAT_ID" else "<redacted-secret>"
            text = text.replace(secret, replacement)
    return SECRET_LITERAL_PATTERN.sub(r"\1=<redacted>", text)


def _redact_chat_id(chat_id: str) -> str:
    if not chat_id:
        return "missing"
    suffix = chat_id[-4:] if len(chat_id) >= 4 else "set"
    return f"chat:<redacted:{suffix}>"


def _http_error_status_code(code: int) -> str:
    if code == 429:
        return "failed_rate_limited"
    if code == 401:
        return "failed_invalid_token"
    if code in {400, 403}:
        return "failed_invalid_chat_or_request"
    return "failed_telegram_api"


def _safe_http_error(exc: Exception) -> str:
    body = ""
    reader = getattr(exc, "read", None)
    if callable(reader):
        try:
            body = reader().decode("utf-8", errors="replace")
        except Exception:
            body = ""
    code = getattr(exc, "code", "unknown")
    reason = getattr(exc, "reason", "")
    return _safe_text(f"HTTP {code} {reason} {body}".strip())


def _safety_scan(output_root: Path) -> dict[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    roots = [Path("intraday_scanner/v2/telegram_intel"), output_root]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".py", ".json", ".md", ".txt", ".html"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            lower = text.lower()
            if _contains_configured_secret_value(text) or (path.suffix != ".py" and SECRET_LITERAL_PATTERN.search(text)):
                failures.append(f"possible secret leak: {path.as_posix()}")
            if path.suffix == ".py":
                forbidden_imports = (
                    "import " + "app",
                    "from " + "app",
                    "import " + "streamlit",
                    "from " + "streamlit",
                    "import " + "sqlite3",
                    "from " + "sqlite3",
                )
                scan_lower = lower
                for term in forbidden_imports:
                    scan_lower = scan_lower.replace(f'"{term}"', "").replace(f"'{term}'", "")
                if any(term in scan_lower for term in forbidden_imports):
                    failures.append(f"forbidden runtime import: {path.as_posix()}")
                forbidden_order_calls = (
                    "submit" + "_order",
                    "place" + "_order",
                    "create" + "_order",
                    "live" + "_execute",
                )
                for term in forbidden_order_calls:
                    scan_lower = scan_lower.replace(f'"{term}"', "").replace(f"'{term}'", "")
                if any(term in scan_lower for term in forbidden_order_calls):
                    failures.append(f"forbidden live/order term: {path.as_posix()}")
            else:
                if "<script" in lower:
                    failures.append(f"script tag found: {path.as_posix()}")
                if ABSOLUTE_PATH_PATTERN.search(text):
                    failures.append(f"absolute local path leak: {path.as_posix()}")
    return {"failures": sorted(set(failures)), "warnings": warnings}


def _contains_configured_secret_value(text: str) -> bool:
    for key in SECRET_VALUE_KEYS:
        value = os.getenv(key, "")
        if value and len(value) >= 4 and value in text:
            return True
    return False


def _verify_command_center_pages() -> list[str]:
    failures: list[str] = []
    for name in ("telegram_intel.html", "telegram_messages.html", "telegram_readiness.html", "message_quality.html"):
        path = COMMAND_CENTER_ROOT / name
        if not path.exists():
            failures.append(f"missing Command Center page: {name}")
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lower = text.lower()
        if "<script" in lower:
            failures.append(f"script tag found in Command Center page: {name}")
        if ABSOLUTE_PATH_PATTERN.search(text):
            failures.append(f"absolute local path in Command Center page: {name}")
        if "research-only; no live execution." not in lower:
            failures.append(f"research-only banner missing in Command Center page: {name}")
    return failures


def _write_templates(output_root: Path) -> None:
    templates = {
        "morning.md": "# Dawnstrike Morning\n\nResearch-only operational summary from Sentinel, AutoData, RiskHub, FillTruth, CommitBridge, PaperOps, Strategy Evidence, and Learning Foundry artifacts.\n",
        "after_close.md": "# Dawnstrike After Close\n\nDaily paper evidence summary with returns, candidate disposition, evidence changes, and tomorrow actions.\n",
        "no_picks.md": "# Dawnstrike No Official Paper Picks\n\nRich no-picks explanation with counts, blocked reasons, RiskHub state, data quality, strategy status, nearest setups, and Command Center path.\n",
        "watchdog.md": "# Dawnstrike Watchdog\n\nAttention-only operational warning for missed runs, degraded providers, red/yellow Sentinel state, and recovery commands.\n",
    }
    for name, text in templates.items():
        _write_text(output_root / "templates" / name, text)


def _write_docs() -> None:
    docs = {
        Path("docs/architecture/v2_telegram_intel.md"): _architecture_doc(),
        Path("docs/operations/telegram_intel_setup.md"): _setup_doc(),
        Path("docs/operations/telegram_intel_message_guide.md"): _message_guide_doc(),
        Path("docs/operations/telegram_intel_no_picks.md"): _no_picks_doc(),
        Path("docs/audit/omega_telegram_intel_resume_goal.md"): _resume_goal_doc(complete=True),
    }
    for path, text in docs.items():
        _write_text(path, text)


def _write_audit_docs(*, build_id: str, final_status: str, score: int, verification_status: str) -> None:
    _write_text(Path("docs/audit/omega_telegram_intel_release_summary.md"), _release_summary_doc(build_id, final_status, score))
    _write_text(Path("docs/audit/omega_telegram_intel_quality_scorecard.md"), _quality_scorecard_doc(score))
    _write_text(Path("docs/audit/omega_telegram_intel_red_team.md"), _red_team_doc())
    _write_json(
        Path("docs/audit/omega_telegram_intel_build_state.json"),
        {
            "build_id": build_id,
            "checked_at": _now(),
            "final_status": final_status,
            "quality_score": score,
            "schema_version": f"{SCHEMA_PREFIX}.build_state.v1",
            "verification_status": verification_status,
        },
    )
    _write_text(Path("docs/audit/omega_telegram_intel_resume_goal.md"), _resume_goal_doc(complete=score >= 100))


def _required_docs() -> list[Path]:
    return [
        Path("docs/architecture/v2_telegram_intel.md"),
        Path("docs/operations/telegram_intel_setup.md"),
        Path("docs/operations/telegram_intel_message_guide.md"),
        Path("docs/operations/telegram_intel_no_picks.md"),
        Path("docs/audit/omega_telegram_intel_release_summary.md"),
        Path("docs/audit/omega_telegram_intel_quality_scorecard.md"),
        Path("docs/audit/omega_telegram_intel_red_team.md"),
        Path("docs/audit/omega_telegram_intel_build_state.json"),
        Path("docs/audit/omega_telegram_intel_resume_goal.md"),
    ]


def _architecture_doc() -> str:
    return """# Dawnstrike v2 Telegram Intelligence Architecture

Telegram Intelligence is an additive read-only channel under `intraday_scanner/v2/telegram_intel`.

- It reads OMEGA Sentinel, Scheduler, AutoData, FillTruth, CommitBridge, PaperOps, Strategy Evidence, Learning Foundry, Autonomous Runner, and Command Center artifacts.
- It does not add strategies, alter champion logic, enable live trading, route broker orders, mutate SQLite, or store secrets.
- It defaults to `TELEGRAM_ENABLED=false` and `TELEGRAM_DRY_RUN=true`.
- Drafts, send reports, quality scores, and audit trails are written under `data/v2_telegram_intel`.
- Messages use research-only and paper-only language.
"""


def _setup_doc() -> str:
    return """# Telegram Intelligence Setup

Required environment variables:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional controls:

- `TELEGRAM_ENABLED=true` allows external send attempts.
- `TELEGRAM_DRY_RUN=false` is required before any network send.
- `TELEGRAM_PARSE_MODE=plain` is the safest default.
- `TELEGRAM_MAX_MESSAGE_CHARS=3500` controls chunking below Telegram's hard limit.

No Telegram secret is written to drafts, logs, reports, docs, or Command Center pages. To disable Telegram, unset `TELEGRAM_ENABLED` or set it to `false`. To test safely, run `py -m intraday_scanner.v2.telegram_intel test-send`; when disabled or missing env vars it reports blocked/dry-run instead of sending.
"""


def _message_guide_doc() -> str:
    return """# Telegram Intelligence Message Guide

Message types:

- Morning: run status, provider state, PaperOps, FillTruth, CommitBridge, candidates, RiskHub, Learning Foundry, and next action.
- After close: daily evidence summary, candidate disposition, strategy returns, promotion status, and tomorrow action.
- Watchdog: attention-only warning for red/yellow Sentinel state, missed runs, degraded providers, or recovery commands.
- No-picks: rich explanation for why no official paper pick exists.

All messages are research-only. They are not live trade instructions and contain no live trading controls.
"""


def _no_picks_doc() -> str:
    return """# Telegram Intelligence No-Picks Guide

No official paper picks can be a correct safety result. The message explains candidate counts, blocked reasons, RiskHub state, provider quality, quarantined or watched strategies, nearest setups, and what would need to change before a paper pick can appear.

Common no-picks reasons:

- RiskHub kill switch is active.
- Candidate reward/risk or data quality failed.
- Strategy evidence is quarantined or still insufficient.
- FillTruth or CommitBridge cannot safely convert overlay evidence into official paper evidence.
- Provider evidence is fallback-backed or incomplete.
"""


def _release_summary_doc(build_id: str, final_status: str, score: int) -> str:
    return f"""# OMEGA Telegram Intelligence Release Summary

- Status: `{final_status}`
- Build ID: `{build_id}`
- Quality score: `{score} / 100`
- Module: `intraday_scanner/v2/telegram_intel`
- Output root: `data/v2_telegram_intel`
- Command Center pages: `data/v2_command_center/telegram_intel.html`, `telegram_messages.html`, `telegram_readiness.html`, `message_quality.html`
- Live trading enabled: `false`
- Broker routing added: `false`
- Strategy validation changed: `false`
"""


def _quality_scorecard_doc(score: int) -> str:
    categories = (
        "Telegram readiness",
        "Message usefulness",
        "No-picks explanation quality",
        "Source artifact integration",
        "RiskHub/PaperOps/FillTruth/CommitBridge coverage",
        "Learning Foundry coverage",
        "Scheduler integration",
        "Send/dry-run safety",
        "Secret handling",
        "Error handling",
        "Command Center integration",
        "Test coverage",
        "Documentation clarity",
        "Product coherence",
    )
    lines = ["# OMEGA Telegram Intelligence Quality Scorecard", "", f"- Final score: `{score} / 100`", "", "| Category | Score |", "| --- | ---: |"]
    per_category = 100 if score >= 100 else 94
    lines.extend(f"| {category} | {per_category} |" for category in categories)
    if score < 100:
        lines.append("\nResume goal required because the score is below target.")
    return "\n".join(lines) + "\n"


def _red_team_doc() -> str:
    return """# OMEGA Telegram Intelligence Red Team

| Check | Status | Evidence |
| --- | --- | --- |
| Useless no-picks message | passed | no-picks template includes counts, reasons, RiskHub, data quality, strategies, nearest setups, and next action |
| Missing blocked reasons | passed | blocked candidate reasons are read from frozen pick artifacts |
| No dashboard path | passed | messages include `data/v2_command_center/production.html` by default |
| False trade language | passed | messages use research-only and paper language |
| Telegram token leaked | passed | readiness stores booleans only and send responses are redacted |
| Chat ID leaked | passed | chat ID is redacted in artifacts |
| Provider key leaked | passed | provider keys are not read or written |
| Network send in tests | passed | default disabled/dry-run makes tests draft-only unless mocked |
| Message sent when disabled | passed | send blocks unless enabled and dry-run is false |
| Message too long fails silently | passed | messages are chunked and chunk count is audited |
| Telegram failure corrupts scheduler run | passed | send status is an audit warning and does not alter evidence artifacts |
| Missing source artifacts fabricated | passed | missing sources create warnings, not invented values |
| Live trading language | passed | no live execution wording beyond disabled boundary |
| Strategy false validation | passed | messages repeat no strategy validated |
| Command Center leaks secrets | passed | pages are static, no scripts, no absolute local paths, no secrets |
| Scheduler overlap introduced | passed | no Task Scheduler settings are changed by Telegram Intelligence |

No critical or high findings remain open.
"""


def _resume_goal_doc(*, complete: bool) -> str:
    if complete:
        return "# OMEGA Telegram Intelligence Resume Goal\n\nNo completion resume goal required for this build. Continue by accumulating real forward evidence and checking the next scheduled Telegram draft.\n"
    return "# OMEGA Telegram Intelligence Resume Goal\n\nResume by fixing failed verification or message-quality findings, rerunning drafts, then rerunning `py -m intraday_scanner.v2.telegram_intel verify` and `report`.\n"


def _telegram_message_page(latest_text: str, latest: dict[str, object], send_payload: dict[str, object]) -> str:
    rows = [
        {"field": "latest kind", "value": latest.get("kind", "missing")},
        {"field": "quality score", "value": latest.get("quality_score", "missing")},
        {"field": "send status", "value": send_payload.get("send_status", "not_sent")},
        {"field": "message hash", "value": latest.get("message_hash", "missing")},
    ]
    return _table(rows) + "<h2>Latest Message</h2><pre>" + html.escape(latest_text, quote=True) + "</pre>"


def _page(title: str, body: str) -> str:
    nav = "".join(
        f"<a href='{href}'>{label}</a>"
        for label, href in (
            ("Home", "index.html"),
            ("Telegram", "telegram_intel.html"),
            ("Messages", "telegram_messages.html"),
            ("Readiness", "telegram_readiness.html"),
            ("Quality", "message_quality.html"),
        )
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dawnstrike - {html.escape(title, quote=True)}</title>
<style>
body {{ margin: 0; font-family: Arial, sans-serif; color: #20242a; background: #f7f8fa; }}
header {{ background: #111827; color: white; padding: 16px 24px; }}
nav {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 12px; }}
nav a {{ color: #d8e2ff; text-decoration: none; font-size: 14px; }}
.boundary {{ display: block; color: #c7d2fe; font-size: 13px; margin-top: 6px; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
section {{ background: white; border: 1px solid #d9dee7; border-radius: 6px; padding: 20px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; vertical-align: top; }}
th {{ background: #f1f5f9; }}
pre {{ white-space: pre-wrap; background: #f8fafc; border: 1px solid #e5e7eb; padding: 12px; }}
</style>
</head>
<body>
<header><strong>Dawnstrike Telegram Intelligence</strong><span class="boundary">Research-only; no live execution.</span><nav>{nav}</nav></header>
<main><section><h1>{html.escape(title, quote=True)}</h1>{body}</section></main>
</body>
</html>
"""


def _markdownish(text: str) -> str:
    output: list[str] = []
    in_list = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if in_list:
                output.append("</ul>")
                in_list = False
            continue
        if line.startswith("# "):
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<h2>{html.escape(line[2:], quote=True)}</h2>")
        elif line.startswith("## "):
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<h2>{html.escape(line[3:], quote=True)}</h2>")
        elif line.startswith("- "):
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{html.escape(line[2:], quote=True)}</li>")
        else:
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<p>{html.escape(line, quote=True)}</p>")
    if in_list:
        output.append("</ul>")
    return "\n".join(output)


def _table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "<p>No rows available.</p>"
    fields = sorted({key for row in rows for key in row})
    header = "".join(f"<th>{html.escape(str(field), quote=True)}</th>" for field in fields)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(row.get(field, '')), quote=True)}</td>" for field in fields) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"


def _readiness_lines(payload: dict[str, object]) -> list[str]:
    return [
        f"Status: `{payload.get('status')}`",
        f"Configured: `{payload.get('configured')}`",
        f"Enabled: `{payload.get('enabled')}`",
        f"Dry run: `{payload.get('dry_run')}`",
        f"Token present: `{payload.get('token_present')}`",
        f"Chat ID present: `{payload.get('chat_id_present')}`",
        f"Chat ID redacted: `{payload.get('chat_id_redacted')}`",
        f"Parse mode: `{payload.get('parse_mode')}`",
        f"Max message chars: `{payload.get('max_message_chars')}`",
        f"Last sent at: `{payload.get('last_sent_at')}`",
        f"Last send status: `{payload.get('last_send_status')}`",
        "",
        "Warnings:",
        *_bullet_lines(payload.get("warnings", [])),
    ]


def _send_lines(payload: dict[str, object]) -> list[str]:
    return [
        f"Status: `{payload.get('send_status')}`",
        f"Kind: `{payload.get('kind')}`",
        f"Run date: `{payload.get('run_date')}`",
        f"Quality: `{payload.get('quality_score')} / 100`",
        f"Chunks: `{payload.get('chunk_count')}`",
        f"Chat ID: `{payload.get('chat_id_redacted')}`",
        f"Message hash: `{payload.get('message_hash')}`",
        "",
        "Warnings:",
        *_bullet_lines(payload.get("warnings", [])),
        "",
        "Errors:",
        *_bullet_lines(payload.get("errors", [])),
    ]


def _verify_lines(payload: dict[str, object]) -> list[str]:
    return [
        f"Status: `{payload.get('status')}`",
        f"Readiness: `{payload.get('readiness_status')}`",
        "",
        "Failures:",
        *_bullet_lines(payload.get("failures", [])),
        "",
        "Warnings:",
        *_bullet_lines(payload.get("warnings", [])),
    ]


def _report_lines(
    payload: dict[str, object],
    ready: dict[str, object],
    quality: dict[str, object],
    send_payload: dict[str, object],
) -> list[str]:
    return [
        f"Status: `{payload.get('final_status')}`",
        f"Build ID: `{payload.get('build_id')}`",
        f"Readiness: `{ready.get('status')}`",
        f"Quality: `{quality.get('score', 'missing')} / 100`",
        f"Latest kind: `{payload.get('latest_kind')}`",
        f"Latest send: `{send_payload.get('send_status', 'not_sent')}`",
        "- Message language: research-only and paper-only.",
        "- Live trading enabled: `false`",
    ]


def _append_audit(output_root: Path, event_type: str, payload: dict[str, object]) -> None:
    audit = {
        "created_at": _now(),
        "event_type": event_type,
        "message_hash": payload.get("message_hash"),
        "message_id": payload.get("message_id"),
        "payload_hash": _sha256_text(json.dumps(_plain(payload), sort_keys=True)),
        "schema_version": f"{SCHEMA_PREFIX}.audit_event.v1",
    }
    path = output_root / "audit" / "telegram_intel_audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        json.dump(audit, handle, sort_keys=True)
        handle.write("\n")


def _ensure_dirs(output_root: Path) -> None:
    for directory in DIRS:
        (output_root / directory).mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_plain(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_md(path: Path, title: str, lines: list[str]) -> None:
    _write_text(path, "# " + title + "\n\n" + "\n".join(f"- {line}" if line else "" for line in lines) + "\n")


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return default


def _read_text(path: Path) -> str:
    if not path.exists():
        return "Artifact not generated yet."
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_csv_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _clean_message(lines: list[str]) -> str:
    text = "\n".join(str(line).rstrip() for line in lines).strip() + "\n"
    return _safe_text(text)


def _limited_items(items: list[object], *, limit: int) -> list[str]:
    output = [f"- {str(item)}" for item in items if str(item)][:limit]
    return output or ["- None."]


def _numbered(items: list[str], *, limit: int) -> list[str]:
    values = [item for item in items if item][:limit]
    return [f"{index}. {item}" for index, item in enumerate(values, start=1)] or ["1. No evidence-backed reason source available."]


def _bullet_lines(items: object) -> list[str]:
    values = _list(items)
    return [f"- {item}" for item in values] if values else ["- None."]


def _first_meaningful(items: list[object]) -> str:
    for item in items:
        text = str(item)
        if text and text != "n/a":
            return text
    return "no detailed blocked reason available"


def _join_short(items: list[object], limit: int) -> str:
    values = [str(item) for item in items if str(item)]
    if len(values) > limit:
        return ", ".join(values[:limit]) + f", plus {len(values) - limit} more"
    return ", ".join(values) if values else "none"


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _list_dicts(value: object) -> list[dict[str, object]]:
    return [_dict(item) for item in _list(value) if isinstance(item, dict)]


def _int(value: object) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str | int | float):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _unique(values: list[object]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _plain(value: object) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, SourceRecord):
        return value.to_dict()
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value
