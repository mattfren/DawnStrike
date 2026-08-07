"""Fail-closed OpenAI research for data-ineligible AlphaOps runs."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from intraday_scanner.ai.indeterminate_researcher import research_symbol
from intraday_scanner.config import ScannerConfig, load_config
from intraday_scanner.notifiers import build_notifiers, dispatch_events
from intraday_scanner.notifiers.base import NotificationEvent
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

SCHEMA_VERSION = "dawnstrike.indeterminate_research.v1"
ELIGIBLE_OUTCOME = "data_ineligible"
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


def run_indeterminate_research(
    *,
    db_path: str | Path,
    symbols: list[str],
    selection_outcome: str,
    market_date: str,
    out_path: str | Path,
    notify: str = "console",
    dry_run: bool = False,
    config: ScannerConfig | None = None,
    researcher: Any = research_symbol,
) -> dict[str, Any]:
    """Collect cited public research without changing AlphaOps pick truth."""

    config = config or load_config(database_path=Path(db_path))
    normalized = _normalize_symbols(symbols)
    selected = normalized[: config.indeterminate_research_max_symbols]
    deferred = normalized[config.indeterminate_research_max_symbols :]
    run_id = "indeterminate_research_" + uuid.uuid4().hex
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "market_date": market_date,
        "selection_outcome": selection_outcome,
        "trigger": ELIGIBLE_OUTCOME,
        "symbols_requested": normalized,
        "symbols_researched": selected,
        "symbols_deferred": deferred,
        "requested_model": config.scenario_openai_model,
        "research_only": True,
        "broker_execution_enabled": False,
        "market_data_substitute": False,
        "can_create_pick": False,
        "model_request_count": 0,
        "web_search_call_count": 0,
        "citation_count": 0,
        "source_count": 0,
        "dossiers": [],
        "notification_stats": {"sent": 0, "skipped": 0},
    }
    if not config.indeterminate_research_enabled:
        result = {**base, "status": "skipped_disabled"}
        _write_artifact(out_path, result)
        return result
    if selection_outcome != ELIGIBLE_OUTCOME:
        result = {**base, "status": "skipped_not_indeterminate"}
        _write_artifact(out_path, result)
        return result
    if not selected:
        result = {**base, "status": "failed", "error_code": "no_research_symbols"}
        _write_artifact(out_path, result)
        return result

    dossiers: list[dict[str, Any]] = []
    for symbol in selected:
        try:
            dossier = researcher(
                symbol=symbol,
                market_date=market_date,
                api_key=config.openai_api_key,
                model=config.scenario_openai_model,
                timeout_seconds=config.indeterminate_research_timeout_seconds,
                max_tool_calls=config.indeterminate_research_max_tool_calls,
            )
        except Exception as exc:
            dossier = {
                "symbol": symbol,
                "status": "provider_error",
                "error_code": type(exc).__name__,
                "brief": "",
                "sources": [],
                "citation_count": 0,
                "source_count": 0,
                "web_search_call_count": 0,
                "market_data_substitute": False,
                "can_create_pick": False,
            }
        dossiers.append(dossier)
        interim = _finalize_counts({**base, "status": "running", "dossiers": dossiers})
        _write_artifact(out_path, interim)

    result = _finalize_counts({**base, "dossiers": dossiers})
    sourced_count = sum(row.get("status") == "sourced" for row in dossiers)
    result["status"] = (
        "completed"
        if sourced_count == len(dossiers)
        else "partial"
        if sourced_count > 0
        else "failed"
    )
    result["research_summary"] = {
        "sourced_symbol_count": sourced_count,
        "insufficient_symbol_count": len(dossiers) - sourced_count,
        "all_market_data_gaps_remain": True,
    }
    result["artifact_hash_sha256"] = _artifact_hash(result)
    _write_artifact(out_path, result)

    if notify.strip().lower() not in {"", "none", "off"}:
        store = SQLiteScanStore(db_path)
        store.initialize()
        channels = ",".join(
            channel.strip().lower() for channel in notify.split(",") if channel.strip()
        )
        notify_config = config.with_overrides(notifier_channels=channels or "console")
        event = _notification_event(result)
        try:
            result["notification_stats"] = dispatch_events(
                [event], build_notifiers(notify_config), store, dry_run=dry_run
            )
        except Exception as exc:
            result["notification_error_code"] = type(exc).__name__
            result["artifact_hash_sha256"] = _artifact_hash(result)
            _write_artifact(out_path, result)
            raise
        result["artifact_hash_sha256"] = _artifact_hash(result)
        _write_artifact(out_path, result)
    return result


def _normalize_symbols(symbols: list[str]) -> list[str]:
    output: list[str] = []
    for value in symbols:
        symbol = str(value).upper().strip()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"Invalid research symbol: {symbol!r}")
        if symbol not in output:
            output.append(symbol)
    return output


def _finalize_counts(result: dict[str, Any]) -> dict[str, Any]:
    dossiers = list(result.get("dossiers") or [])
    result["model_request_count"] = len(dossiers)
    result["web_search_call_count"] = sum(
        int(row.get("web_search_call_count") or 0) for row in dossiers
    )
    result["citation_count"] = sum(int(row.get("citation_count") or 0) for row in dossiers)
    result["source_count"] = sum(int(row.get("source_count") or 0) for row in dossiers)
    return result


def _notification_event(result: dict[str, Any]) -> NotificationEvent:
    summary = dict(result.get("research_summary") or {})
    sourced = int(summary.get("sourced_symbol_count") or 0)
    total = len(result.get("symbols_researched") or [])
    body = "\n".join(
        [
            "🔎 Dawnstrike AI Research — Not Picks",
            f"AlphaOps result: {result.get('selection_outcome')}",
            f"OpenAI web research: {sourced}/{total} symbols with cited sources",
            f"Citations captured: {int(result.get('citation_count') or 0)}",
            "Alpaca price/volume proof is still missing and was not invented.",
            "No pick was created. No order was placed.",
        ]
    )
    return NotificationEvent(
        event_key=f"dawnstrike:indeterminate-research:{result.get('market_date')}",
        title="Dawnstrike AI research receipt",
        body=body,
        channel_hint="research_summary",
        payload={
            "run_id": result.get("run_id"),
            "telegram_compact_message": body,
            "status": result.get("status"),
            "selection_outcome": result.get("selection_outcome"),
            "sourced_symbol_count": sourced,
            "researched_symbol_count": total,
            "citation_count": result.get("citation_count"),
            "research_only": True,
            "broker_execution_enabled": False,
        },
    )


def _artifact_hash(result: dict[str, Any]) -> str:
    payload = {key: value for key, value in result.items() if key != "artifact_hash_sha256"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_artifact(path: str | Path, result: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)


__all__ = ["run_indeterminate_research"]
