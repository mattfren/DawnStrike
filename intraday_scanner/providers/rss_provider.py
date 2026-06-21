"""Generic RSS feed parser for optional headline/catalyst enrichment."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from intraday_scanner.models import utc_now_iso
from intraday_scanner.providers.web_source_base import (
    WebCollectionConfig,
    WebSourceConfig,
    fetch_text,
    write_json,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def collect_rss_feed(
    *,
    source: WebSourceConfig,
    config: WebCollectionConfig,
    out_dir: str | Path,
    store: SQLiteScanStore | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fetch = fetch_text(source, config)
    if persist and store is not None:
        store.persist_web_fetch_run(fetch.payload())
    if fetch.status != "success":
        failure = {
            "status": "failed",
            "source": source.name,
            "failure_reason": fetch.failure_reason,
            "run_id": fetch.run_id,
        }
        write_json(output_dir / f"{source.name}_rss_failure.json", failure)
        if persist and store is not None:
            store.record_source_health(
                source.name,
                "failed",
                utc_now_iso(),
                fetch.failure_reason,
                failure,
            )
        return failure
    items = parse_rss_items(fetch.content, source_name=source.name)
    summary = {
        "status": "success",
        "run_id": fetch.run_id,
        "source": source.name,
        "item_count": len(items),
        "items": items,
    }
    write_json(output_dir / f"{source.name}_rss_summary.json", summary)
    if persist and store is not None:
        store.persist_web_fetch_result(
            {
                "run_id": fetch.run_id,
                "source": source.name,
                "status": "success",
                "row_count": len(items),
                "artifact_path": "",
                "failure_reason": "",
                "summary": summary,
            }
        )
        store.record_source_health(
            source.name,
            "ok",
            utc_now_iso(),
            f"items={len(items)}",
            summary,
        )
    return summary


def parse_rss_items(text: str, *, source_name: str) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []
    items = []
    for item in [node for node in root.iter() if node.tag.lower().endswith("item")]:
        items.append(
            {
                "source": source_name,
                "title": _child_text(item, "title"),
                "url": _child_text(item, "link"),
                "published_at": _child_text(item, "pubDate") or utc_now_iso(),
                "summary": _child_text(item, "description"),
            }
        )
    return items


def _child_text(item: ElementTree.Element, suffix: str) -> str:
    for child in item:
        if child.tag.lower().endswith(suffix.lower()) and child.text:
            return child.text.strip()
    return ""
