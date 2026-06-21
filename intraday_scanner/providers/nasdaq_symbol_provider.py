"""Nasdaq Trader symbol directory universe builder."""

from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
from typing import Any

from intraday_scanner.models import utc_now_iso
from intraday_scanner.providers.web_source_base import (
    WebCollectionConfig,
    WebSourceConfig,
    fetch_text,
    write_json,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

REJECT_NAME_TERMS = (
    " warrant",
    " warrants",
    " right",
    " rights",
    " unit",
    " units",
    " preferred",
    " preference",
    " note",
    " notes",
    " etn",
    " fund",
    " trust",
)


def build_us_common_universe(
    *,
    source: WebSourceConfig,
    config: WebCollectionConfig,
    out_path: str | Path = "data/universe_us_common.csv",
    rejected_path: str | Path | None = None,
    store: SQLiteScanStore | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    out = Path(out_path)
    rejected = Path(rejected_path) if rejected_path else out.with_name("universe_us_rejected.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    rejected.parent.mkdir(parents=True, exist_ok=True)
    urls = _directory_urls(source)
    accepted: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    fetch_payloads = []
    for url in urls:
        fetch = fetch_text(source, config, url=url)
        fetch_payloads.append(fetch.payload())
        if persist and store is not None:
            store.persist_web_fetch_run(fetch.payload())
        if fetch.status != "success":
            if persist and store is not None:
                store.record_source_health(
                    source.name,
                    "failed",
                    utc_now_iso(),
                    fetch.failure_reason,
                    fetch.payload(),
                )
            continue
        for row in parse_symbol_directory(fetch.content):
            normalized = normalize_symbol_row(row)
            reason = rejection_reason(normalized)
            if reason:
                normalized["reject_reason"] = reason
                rejected_rows.append(normalized)
            else:
                accepted.append(normalized)
    accepted = _dedupe_symbols(accepted)
    rejected_rows = _dedupe_symbols(rejected_rows)
    _write_csv(
        out,
        accepted,
        ["symbol", "security_name", "exchange", "etf", "test_issue", "source"],
    )
    _write_csv(
        rejected,
        rejected_rows,
        ["symbol", "security_name", "exchange", "etf", "test_issue", "source", "reject_reason"],
    )
    summary = {
        "status": "success" if accepted else "no_symbols",
        "built_at": utc_now_iso(),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected_rows),
        "paths": {"universe": str(out), "rejected": str(rejected)},
        "fetches": fetch_payloads,
    }
    write_json(out.with_suffix(".summary.json"), summary)
    if persist and store is not None:
        store.record_source_health(
            source.name,
            "ok" if accepted else "failed",
            utc_now_iso(),
            f"accepted={len(accepted)} rejected={len(rejected_rows)}",
            summary,
        )
    return summary


def parse_symbol_directory(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    filtered_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("file creation time"):
            continue
        filtered_lines.append(line)
    if not filtered_lines:
        return rows
    reader = csv.DictReader(StringIO("\n".join(filtered_lines)), delimiter="|")
    for row in reader:
        if not row:
            continue
        symbol = row.get("Symbol") or row.get("ACT Symbol") or row.get("Nasdaq Symbol")
        if not symbol or symbol == "Symbol":
            continue
        rows.append({str(key): str(value or "") for key, value in row.items() if key is not None})
    return rows


def normalize_symbol_row(row: dict[str, str]) -> dict[str, Any]:
    symbol = (row.get("Symbol") or row.get("ACT Symbol") or row.get("Nasdaq Symbol") or "").strip()
    return {
        "symbol": symbol.upper(),
        "security_name": (
            row.get("Security Name")
            or row.get("Company Name")
            or row.get("Issuer Name")
            or symbol
        ).strip(),
        "exchange": (row.get("Exchange") or row.get("Market Category") or "").strip(),
        "etf": (row.get("ETF") or "").strip().upper(),
        "test_issue": (row.get("Test Issue") or "").strip().upper(),
        "source": "nasdaq_symbol_directory",
    }


def rejection_reason(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "")
    name = f" {str(row.get('security_name') or '').lower()} "
    if not symbol:
        return "missing_symbol"
    if row.get("etf") == "Y":
        return "etf"
    if row.get("test_issue") == "Y":
        return "test_issue"
    if any(term in name for term in REJECT_NAME_TERMS):
        return "non_common_security_name"
    if any(symbol.endswith(suffix) for suffix in ("W", "WS", "R", "RT", "U", "P", "PR")):
        return "non_common_symbol_suffix"
    return ""


def _directory_urls(source: WebSourceConfig) -> list[str]:
    if source.url:
        return [source.url]
    return [NASDAQ_LISTED_URL, OTHER_LISTED_URL]


def _dedupe_symbols(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if symbol in seen:
            continue
        seen.add(symbol)
        output.append(row)
    return sorted(output, key=lambda row: str(row.get("symbol") or ""))


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
