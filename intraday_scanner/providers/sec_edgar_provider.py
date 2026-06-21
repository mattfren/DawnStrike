"""SEC EDGAR JSON/RSS risk event collection."""

from __future__ import annotations

import json
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

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

RISK_FORMS = {"S-1", "S-3", "424B", "424B3", "424B4", "424B5", "8-K", "6-K"}
RISK_TERMS = {
    "atm": "dilution_risk",
    "at-the-market": "dilution_risk",
    "shelf": "dilution_risk",
    "offering": "dilution_risk",
    "warrant": "warrant_risk",
    "reverse split": "reverse_split_risk",
    "going concern": "going_concern_risk",
    "delisting": "listing_risk",
    "nasdaq deficiency": "listing_risk",
}


def collect_sec_risk(
    *,
    source: WebSourceConfig,
    config: WebCollectionConfig,
    tickers: list[str],
    out_dir: str | Path,
    store: SQLiteScanStore | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    clean_tickers = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    if not clean_tickers:
        summary = {"status": "no_tickers", "event_count": 0, "events": []}
        write_json(output_dir / "sec_risk_summary.json", summary)
        return summary
    events: list[dict[str, Any]] = []
    fetches: list[dict[str, Any]] = []
    if source.fixture_path:
        events.extend(_events_from_fixture(source, config, clean_tickers, fetches, store, persist))
    else:
        cik_map = fetch_company_ticker_map(source, config, store=store, persist=persist)
        for ticker in clean_tickers:
            cik = cik_map.get(ticker)
            if cik is None:
                continue
            fetch = fetch_text(
                source,
                config,
                url=SUBMISSIONS_URL.format(cik=int(cik)),
                allow_unlisted_url=True,
            )
            fetches.append(fetch.payload())
            if persist and store is not None:
                store.persist_web_fetch_run(fetch.payload())
            if fetch.status != "success":
                continue
            events.extend(parse_submissions_json(fetch.content, ticker=ticker))
    summary = {
        "status": "success",
        "source": source.name,
        "tickers": clean_tickers,
        "event_count": len(events),
        "events": events,
        "fetches": fetches,
    }
    write_json(output_dir / "sec_risk_summary.json", summary)
    if persist and store is not None:
        counts = store.persist_sec_risk_events(events)
        store.record_source_health(
            source.name,
            "ok" if events else "partial",
            utc_now_iso(),
            f"sec_risk_events={len(events)}",
            {**summary, "persist_counts": counts},
        )
    return summary


def fetch_company_ticker_map(
    source: WebSourceConfig,
    config: WebCollectionConfig,
    *,
    store: SQLiteScanStore | None = None,
    persist: bool = False,
) -> dict[str, int]:
    fetch = fetch_text(source, config, url=COMPANY_TICKERS_URL, allow_unlisted_url=True)
    if persist and store is not None:
        store.persist_web_fetch_run(fetch.payload())
    if fetch.status != "success":
        return {}
    try:
        payload = json.loads(fetch.content)
    except json.JSONDecodeError:
        return {}
    mapping: dict[str, int] = {}
    rows = payload.values() if isinstance(payload, dict) else payload
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        cik = row.get("cik_str")
        if ticker and cik is not None:
            mapping[ticker] = int(cik)
    return mapping


def parse_submissions_json(text: str, *, ticker: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    recent = dict(dict(payload.get("filings") or {}).get("recent") or {})
    forms = _as_list(recent.get("form"))
    filed = _as_list(recent.get("filingDate"))
    accession = _as_list(recent.get("accessionNumber"))
    primary = _as_list(recent.get("primaryDocument"))
    descriptions = _as_list(recent.get("primaryDocDescription"))
    events = []
    for index, form in enumerate(forms):
        form_type = str(form or "").upper()
        filed_at = _value_at(filed, index)
        doc = _value_at(primary, index)
        description = _value_at(descriptions, index)
        text_blob = f"{form_type} {description} {doc}".lower()
        labels = _risk_labels(form_type, text_blob)
        if not labels:
            continue
        accession_number = _value_at(accession, index) or f"{filed_at}:{index}"
        url = _filing_url(payload, accession_number, doc)
        events.append(
            {
                "event_key": f"{ticker}:{accession_number}:{form_type}",
                "ticker": ticker.upper(),
                "filed_at": filed_at,
                "form_type": form_type,
                "severity": "high" if "dilution_risk" in labels else "medium",
                "risk_labels": labels,
                "headline": description or form_type,
                "url": url,
                "source": "sec_edgar",
            }
        )
    return events


def enrich_rows_with_sec_risk(
    rows: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_ticker.setdefault(str(event.get("ticker") or "").upper(), []).append(event)
    enriched = []
    for row in rows:
        updated = dict(row)
        matches = by_ticker.get(str(updated.get("ticker") or "").upper(), [])
        if matches:
            labels = sorted(
                {
                    label
                    for event in matches
                    for label in list(event.get("risk_labels") or [])
                }
            )
            updated["sec_risk_events"] = matches
            updated["recent_offering"] = any("dilution" in label for label in labels)
            flags = [part for part in str(updated.get("coverage_warning") or "").split(";") if part]
            flags.extend(labels)
            updated["coverage_warning"] = ";".join(dict.fromkeys(flags))
        enriched.append(updated)
    return enriched


def _events_from_fixture(
    source: WebSourceConfig,
    config: WebCollectionConfig,
    tickers: list[str],
    fetches: list[dict[str, Any]],
    store: SQLiteScanStore | None,
    persist: bool,
) -> list[dict[str, Any]]:
    fetch = fetch_text(source, config)
    fetches.append(fetch.payload())
    if persist and store is not None:
        store.persist_web_fetch_run(fetch.payload())
    if fetch.status != "success":
        return []
    try:
        payload = json.loads(fetch.content)
    except json.JSONDecodeError:
        return []
    if "filings" in payload:
        ticker = str(payload.get("ticker") or tickers[0]).upper()
        if ticker not in {item.upper() for item in tickers}:
            return []
        return parse_submissions_json(fetch.content, ticker=ticker)
    events = []
    for ticker, submissions in dict(payload.get("submissions") or {}).items():
        if ticker.upper() in tickers:
            events.extend(parse_submissions_json(json.dumps(submissions), ticker=ticker))
    return events


def _risk_labels(form_type: str, text_blob: str) -> list[str]:
    labels = []
    if form_type in RISK_FORMS:
        labels.append("filing_watch")
    for term, label in RISK_TERMS.items():
        if term in text_blob:
            labels.append(label)
    if form_type.startswith("424B") or form_type in {"S-1", "S-3"}:
        labels.append("dilution_risk")
    return sorted(set(labels))


def _filing_url(payload: dict[str, Any], accession: str, doc: str) -> str:
    cik = str(payload.get("cik") or "").lstrip("0")
    clean_accession = accession.replace("-", "")
    if not cik or not clean_accession or not doc:
        return ""
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{clean_accession}/{doc}"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _value_at(values: list[Any], index: int) -> str:
    if index >= len(values):
        return ""
    return str(values[index] or "")
