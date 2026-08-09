"""SEC EDGAR JSON/RSS risk event collection."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    checked_tickers: list[str] = []
    unchecked_tickers: list[str] = []
    if source.fixture_path:
        events.extend(_events_from_fixture(source, config, clean_tickers, fetches, store, persist))
        if any(str(row.get("status") or "") == "success" for row in fetches):
            checked_tickers.extend(clean_tickers)
        else:
            unchecked_tickers.extend(clean_tickers)
    else:
        cik_map = fetch_company_ticker_map(source, config, store=store, persist=persist)
        for ticker in clean_tickers:
            cik = cik_map.get(ticker)
            if cik is None:
                unchecked_tickers.append(ticker)
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
                unchecked_tickers.append(ticker)
                continue
            checked_tickers.append(ticker)
            events.extend(parse_submissions_json(fetch.content, ticker=ticker))
    summary = {
        "status": "success" if checked_tickers else "partial",
        "source": source.name,
        "tickers": clean_tickers,
        "checked_tickers": sorted(set(checked_tickers)),
        "unchecked_tickers": sorted(set(unchecked_tickers)),
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
    today = datetime.now(timezone.utc).date()
    for index, form in enumerate(forms):
        form_type = str(form or "").upper()
        filed_at = _value_at(filed, index)
        doc = _value_at(primary, index)
        description = _value_at(descriptions, index)
        text_blob = f"{form_type} {description} {doc}".lower()
        labels = _risk_labels(form_type, text_blob)
        if not labels:
            continue
        if not _retain_event(filed_at, labels, today):
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


def parse_filing_evidence(
    text: str,
    *,
    ticker: str,
    fetched_at: str | None = None,
    first_seen_at: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize SEC submission metadata without treating title as document terms."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    recent = dict(dict(payload.get("filings") or {}).get("recent") or {})
    forms = _as_list(recent.get("form"))
    filed = _as_list(recent.get("filingDate"))
    accession = _as_list(recent.get("accessionNumber"))
    primary = _as_list(recent.get("primaryDocument"))
    acceptance = _as_list(recent.get("acceptanceDateTime"))
    items = _as_list(recent.get("items"))
    descriptions = _as_list(recent.get("primaryDocDescription"))
    observed_at = fetched_at or datetime.now(timezone.utc).isoformat()
    first_seen = first_seen_at or observed_at
    records: list[dict[str, Any]] = []
    for index, raw_form in enumerate(forms):
        form = str(raw_form or "").upper()
        accession_number = _value_at(accession, index)
        primary_document = _value_at(primary, index)
        filing_date = _value_at(filed, index)
        acceptance_timestamp = _value_at(acceptance, index)
        records.append(
            {
                "ticker": ticker.upper(),
                "cik": str(payload.get("cik") or ""),
                "accession_number": accession_number,
                "form": form.rstrip("/A") if form.endswith("/A") else form,
                "amendment_status": "amended" if form.endswith("/A") else "original",
                "sec_acceptance_timestamp": acceptance_timestamp,
                "filing_date": filing_date,
                "eight_k_items": (
                    str(_value_at(items, index) or "") if form.startswith("8-K") else ""
                ),
                "primary_document": primary_document,
                "primary_document_url": _filing_url(payload, accession_number, primary_document),
                "primary_doc_description": _value_at(descriptions, index),
                "fetched_at": observed_at,
                "first_seen_at": first_seen,
                "source": "sec_edgar",
                "content_hash_sha256": "",
            }
        )
    return records


def fetch_primary_filing_document(
    *,
    filing: dict[str, Any],
    source: WebSourceConfig,
    config: WebCollectionConfig,
    out_dir: str | Path,
) -> dict[str, Any]:
    """Fetch one primary SEC document under the existing fair-access transport."""

    url = str(filing.get("primary_document_url") or "")
    if not url:
        return {"status": "missing_primary_document_url", **filing}
    fetch = fetch_text(source, config, url=url, allow_unlisted_url=True)
    payload = {**filing, **fetch.payload()}
    if fetch.status != "success":
        payload.update({"status": "provider_failed", "content_hash_sha256": ""})
        return payload
    content = fetch.content.encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    directory = Path(out_dir) / "sec" / str(filing.get("ticker") or "UNKNOWN").upper()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}.html"
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise ValueError("SEC primary document hash conflict")
    if not destination.exists():
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    payload.update(
        {
            "status": "success",
            "raw_artifact_path": str(destination),
            "content_hash_sha256": digest,
            "content_length": len(content),
        }
    )
    return payload


def normalize_filing_facts(
    filing: dict[str, Any],
    *,
    document_text: str = "",
) -> dict[str, Any]:
    """Extract explicit facts and verify amount/price/share arithmetic."""

    text_blob = " ".join(
        str(filing.get(key) or "") for key in ("form", "primary_doc_description")
    ) + " " + document_text
    normalized = text_blob.lower()
    security_type = (
        "common_stock"
        if "common stock" in normalized or "common shares" in normalized
        else "preferred_stock"
        if "preferred stock" in normalized
        else "debt"
        if "debt" in normalized or "notes" in normalized
        else "unknown"
    )
    gross_amount = _amount_match(
        normalized,
        ("gross proceeds", "aggregate offering amount", "offering amount"),
    )
    price = _price_match(normalized)
    share_count = _share_count_match(normalized)
    atm_capacity = _amount_match(normalized, ("at-the-market", "atm program", "aggregate sales") )
    remaining_amount = _amount_match(normalized, ("remaining capacity", "remaining amount"))
    warrant_count = _number_match(normalized, ("warrants", "warrant"))
    strike = _number_match(normalized, ("exercise price", "strike price"))
    reverse_split = bool(re.search(r"reverse split|reverse stock split", normalized))
    arithmetic_status = "UNKNOWN"
    if gross_amount is not None and price is not None and share_count is not None:
        arithmetic_status = (
            "PASS"
            if abs(gross_amount - price * share_count)
            <= max(1.0, gross_amount * 0.05)
            else "CONFLICT"
        )
    return {
        "security_type": security_type,
        "gross_amount": gross_amount,
        "price": price,
        "share_count": share_count,
        "atm_capacity": atm_capacity,
        "atm_remaining_amount": remaining_amount,
        "warrant_count": warrant_count,
        "warrant_strike": strike,
        "warrant_expiry": _date_match(normalized, "expiry"),
        "reverse_split": reverse_split,
        "relevant_offering_terms": _offering_terms(normalized),
        "arithmetic_status": arithmetic_status,
        "facts_status": (
            "PARTIAL"
            if "unknown" in {security_type} or arithmetic_status == "UNKNOWN"
            else "NORMALIZED"
        ),
    }


def classify_filing_research_feature(
    filing: dict[str, Any],
    facts: dict[str, Any],
    *,
    decision_at: str,
) -> dict[str, Any]:
    """Register the avoid-long feature without creating a fade/short route."""

    form = str(filing.get("form") or "").upper()
    filed = str(filing.get("filing_date") or "")[:10]
    decision_date = str(decision_at or "")[:10]
    try:
        age_hours = (
            (
                datetime.fromisoformat(decision_at.replace("Z", "+00:00"))
                - datetime.fromisoformat(f"{filed}T00:00:00+00:00")
            ).total_seconds()
            / 3600
            if filed
            else None
        )
    except ValueError:
        age_hours = None
    terms = str(facts.get("relevant_offering_terms") or "")
    avoid_long = (
        form in {"S-3", "424B5"}
        and age_hours is not None
        and 0 <= age_hours <= 72 * 1.0
        and bool(terms)
    )
    return {
        "feature_id": "avoid_long_s3_424b5_inside_72h",
        "symbol": filing.get("ticker", ""),
        "security_type": facts.get("security_type", "unknown"),
        "actual_takedown_terms": terms or "unknown",
        "avoid_long": avoid_long,
        "route": "none",
        "status": "VERIFIED" if avoid_long else "NO_ACTION_OR_UNKNOWN",
        "decision_at": decision_at,
        "decision_date": decision_date,
    }


def _amount_match(text: str, labels: tuple[str, ...]) -> float | None:
    if not any(label in text for label in labels):
        return None
    match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(million|billion|m|b)?", text)
    if not match:
        return None
    value = float(match.group(1))
    multiplier = {
        "million": 1_000_000.0,
        "m": 1_000_000.0,
        "billion": 1_000_000_000.0,
        "b": 1_000_000_000.0,
    }.get(match.group(2) or "", 1.0)
    return value * multiplier


def _number_match(text: str, labels: tuple[str, ...]) -> float | None:
    if not any(label in text for label in labels):
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:million|m|billion|b)?", text)
    return float(match.group(1)) if match else None


def _price_match(text: str) -> float | None:
    match = re.search(
        r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:price per share|offering price)",
        text,
    )
    return (
        float(match.group(1))
        if match
        else _number_match(text, ("price per share", "offering price"))
    )


def _share_count_match(text: str) -> float | None:
    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*(million|billion|m|b)?\s+shares",
        text,
    )
    if not match:
        return _number_match(
            text, ("shares offered", "shares of common stock", "common shares")
        )
    multiplier = {
        "million": 1_000_000.0,
        "m": 1_000_000.0,
        "billion": 1_000_000_000.0,
        "b": 1_000_000_000.0,
    }.get(match.group(2) or "", 1.0)
    return float(match.group(1)) * multiplier


def _date_match(text: str, label: str) -> str | None:
    match = re.search(rf"{label}[^0-9]*(20[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}})", text)
    return match.group(1) if match else None


def _offering_terms(text: str) -> str:
    markers = [
        marker
        for marker in ("takedown", "at-the-market", "shelf", "warrant")
        if marker in text
    ]
    return ";".join(markers) if markers else ""


def enrich_rows_with_sec_risk(
    rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    checked_tickers: list[str] | None = None,
    as_of: str | datetime | None = None,
) -> list[dict[str, Any]]:
    """Attach only time-bounded SEC risk, preserving unknown checks as unknown."""

    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_ticker.setdefault(str(event.get("ticker") or "").upper(), []).append(event)
    checked = {str(ticker).upper() for ticker in list(checked_tickers or [])}
    reference_date = _as_date(as_of)
    enriched = []
    for row in rows:
        updated = dict(row)
        ticker = str(updated.get("ticker") or "").upper()
        matches = by_ticker.get(ticker, [])
        if matches:
            updated["sec_risk_events"] = matches
        active_matches = [
            event for event in matches if _event_is_active(event, reference_date)
        ]
        if active_matches:
            labels = sorted(
                {
                    label
                    for event in active_matches
                    for label in list(event.get("risk_labels") or [])
                }
            )
            updated["sec_active_risk_events"] = active_matches
            updated["recent_offering"] = any("dilution" in label for label in labels)
            updated["reverse_split_90d"] = any(
                "reverse_split" in label for label in labels
            )
            flags = [part for part in str(updated.get("coverage_warning") or "").split(";") if part]
            flags.extend(label for label in labels if label != "filing_watch")
            updated["coverage_warning"] = ";".join(dict.fromkeys(flags))
        checked_ok = ticker in checked
        if checked_ok and not active_matches:
            updated["recent_offering"] = False
            updated["reverse_split_90d"] = False
        has_active_risk = bool(
            updated.get("recent_offering") or updated.get("reverse_split_90d")
        )
        updated["sec_risk_status"] = (
            "BLOCKED" if has_active_risk else "CLEAR" if checked_ok else "UNKNOWN"
        )
        updated["corporate_action_status"] = (
            "BLOCKED"
            if updated.get("reverse_split_90d")
            else "CLEAR"
            if checked_ok
            else "UNKNOWN"
        )
        enriched.append(updated)
    return enriched


def _as_date(value: str | datetime | None) -> date:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    raw = str(value or "")[:10]
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def _event_is_active(event: dict[str, Any], as_of: date) -> bool:
    labels = {str(label) for label in list(event.get("risk_labels") or [])}
    material = labels - {"filing_watch"}
    if not material:
        return False
    filed_raw = str(event.get("filed_at") or "")[:10]
    if not filed_raw:
        return True
    try:
        filed = date.fromisoformat(filed_raw)
    except ValueError:
        return True
    age_days = (as_of - filed).days
    if age_days < 0:
        return False
    window = 90 if material & {"dilution_risk", "reverse_split_risk", "warrant_risk"} else 180
    return age_days <= window


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
    # 424B2 is commonly a bank/debt pricing supplement and is not, by itself,
    # evidence of common-stock dilution. Equity-oriented registration and
    # prospectus forms remain conservative risk signals.
    if form_type.startswith(("424B3", "424B4", "424B5")) or form_type in {"S-1", "S-3"}:
        labels.append("dilution_risk")
    return sorted(set(labels))


def _retain_event(filed_at: str, labels: list[str], today: date) -> bool:
    """Bound the feed to decision-relevant history instead of all SEC history."""

    try:
        filed = date.fromisoformat(str(filed_at or "")[:10])
    except ValueError:
        return True
    age_days = (today - filed).days
    if age_days < 0:
        return False
    material = set(labels) - {"filing_watch"}
    return age_days <= (365 if material else 45)


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
