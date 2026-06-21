"""Nasdaq Trader trade halt RSS collection."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from intraday_scanner.models import utc_now_iso
from intraday_scanner.providers.web_source_base import (
    WebCollectionConfig,
    WebSourceConfig,
    artifact_payload,
    fetch_text,
    write_json,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

HALT_RSS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"


def collect_trade_halts(
    *,
    source: WebSourceConfig,
    config: WebCollectionConfig,
    out_dir: str | Path,
    store: SQLiteScanStore | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fetch = fetch_text(source, config, url=source.url or HALT_RSS_URL)
    if persist and store is not None:
        store.persist_web_fetch_run(fetch.payload())
    if fetch.status != "success":
        failure = {
            "status": "failed",
            "source": source.name,
            "failure_reason": fetch.failure_reason,
            "run_id": fetch.run_id,
        }
        write_json(output_dir / "halt_failure.json", failure)
        if persist and store is not None:
            store.record_source_health(
                source.name,
                "failed",
                utc_now_iso(),
                fetch.failure_reason,
                failure,
            )
        return failure
    raw_path = output_dir / "nasdaq_trade_halts.rss"
    if config.save_raw:
        raw_path.write_text(fetch.content, encoding="utf-8")
        if persist and store is not None:
            store.persist_raw_source_artifact(
                artifact_payload(
                    run_id=fetch.run_id,
                    source=source.name,
                    artifact_kind="rss",
                    path=raw_path,
                    content_type=fetch.content_type,
                    metadata={"url": fetch.url, "from_fixture": fetch.from_fixture},
                )
            )
    events = parse_halt_rss(fetch.content)
    summary = {
        "status": "success",
        "run_id": fetch.run_id,
        "source": source.name,
        "event_count": len(events),
        "events": events,
        "raw_path": str(raw_path if config.save_raw else ""),
    }
    write_json(output_dir / "halt_summary.json", summary)
    if persist and store is not None:
        counts = store.persist_halt_events(events)
        store.persist_web_fetch_result(
            {
                "run_id": fetch.run_id,
                "source": source.name,
                "status": "success",
                "row_count": len(events),
                "artifact_path": str(raw_path if config.save_raw else ""),
                "failure_reason": "",
                "summary": summary,
                "persist_counts": counts,
            }
        )
        store.record_source_health(
            source.name,
            "ok",
            utc_now_iso(),
            f"halt_events={len(events)}",
            summary,
        )
    return summary


def parse_halt_rss(text: str) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []
    events = []
    for item in [node for node in root.iter() if node.tag.lower().endswith("item")]:
        title = _child_text(item, "title")
        description = _child_text(item, "description")
        published = _child_text(item, "pubDate") or _child_text(item, "date") or utc_now_iso()
        ticker = _extract_symbol(title, description)
        if not ticker:
            continue
        code = _extract_field(description, ["Reason Code", "Code", "Halt Code"])
        status = _extract_field(description, ["Status", "Market Category"]) or "halt_feed"
        key = f"{ticker}:{published}:{code or title}"
        events.append(
            {
                "event_key": key,
                "ticker": ticker,
                "event_time": published,
                "status": status,
                "halt_code": code,
                "headline": title,
                "description": " ".join(description.split()),
                "source": "nasdaq_trade_halts_rss",
            }
        )
    return events


def attach_halt_status(
    rows: list[dict[str, Any]], halt_events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    halted = {str(event.get("ticker") or "").upper(): event for event in halt_events}
    output = []
    for row in rows:
        updated = dict(row)
        ticker = str(updated.get("ticker") or "").upper()
        event = halted.get(ticker)
        if event:
            updated["current_halt"] = True
            flags = [part for part in str(updated.get("coverage_warning") or "").split(";") if part]
            flags.append("nasdaq_halt_feed_match")
            updated["coverage_warning"] = ";".join(dict.fromkeys(flags))
            updated["halt_event"] = event
        output.append(updated)
    return output


def _child_text(item: ElementTree.Element, suffix: str) -> str:
    for child in item:
        if child.tag.lower().endswith(suffix.lower()) and child.text:
            return child.text.strip()
    return ""


def _extract_symbol(title: str, description: str) -> str:
    for pattern in (
        r"(?:symbol|ticker)\s*[:\-]\s*([A-Z][A-Z0-9.\-]{0,9})",
        r"\b([A-Z]{1,5})\b",
    ):
        match = re.search(pattern, f"{title} {description}", flags=re.IGNORECASE)
        if match:
            return match.group(1).upper().replace(".", "-")
    return ""


def _extract_field(description: str, names: list[str]) -> str:
    for name in names:
        match = re.search(rf"{re.escape(name)}\s*[:\-]\s*([^;,\n]+)", description, re.I)
        if match:
            return match.group(1).strip()
    return ""
