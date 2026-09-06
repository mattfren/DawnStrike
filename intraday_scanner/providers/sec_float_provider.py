"""Automatic share-count enrichment from SEC XBRL company facts.

``formula._float_rotation_score`` contributes up to 14 of the 100 available
score points, but returns a hard 0.0 whenever ``float_shares`` is absent.  With
no float source wired in, every live candidate silently forfeited those points
and could never reach an A/B setup grade - the top live candidate scored 69.03
against a B threshold of 70.

This module supplies that input automatically from SEC XBRL, which the project
already reaches: ``sec.gov`` is allowlisted and the ``sec_edgar`` source is
enabled, so no new credential, vendor or domain is introduced.

Two honesty constraints are deliberately enforced:

* **Shares outstanding is not free float.**  Float excludes insider and
  restricted holdings, so outstanding >= float.  Using it therefore
  *understates* float rotation and *understates* the score.  That is the safe
  direction - it can never inflate a setup grade - and the proxy is labelled as
  such on every enriched row.
* **Stale filings are refused, not used.**  Some issuers stopped tagging the
  cover-page concept years ago; a 2015 share count would badly misstate today's
  rotation.  Anything filed more than ``MAX_FILING_AGE_DAYS`` ago is discarded
  and the ticker keeps ``unknown_float`` rather than receiving a wrong number.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.providers.sec_edgar_provider import fetch_company_ticker_map
from intraday_scanner.providers.web_source_base import (
    WebCollectionConfig,
    WebSourceConfig,
    fetch_text,
    write_json,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

COMPANY_CONCEPT_URL = (
    "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{tag}.json"
)

# Ordered by preference.  The cover-page dei concept is the most current, and
# the us-gaap fallbacks cover issuers that do not tag it.
SHARE_COUNT_CONCEPTS: tuple[tuple[str, str], ...] = (
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesIssued"),
)

# An annual filer legitimately reports once a year; beyond that the count is not
# a usable proxy for today's tradable supply.
MAX_FILING_AGE_DAYS = 400

FLOAT_SOURCE_KIND = "sec_xbrl_shares_outstanding"
FLOAT_PROXY_NOTE = "shares_outstanding_proxy_not_free_float"


def _parse_day(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _latest_share_count(payload_text: str) -> dict[str, Any] | None:
    """Most recently *filed* share count in the concept payload.

    Recency is taken from ``filed`` rather than ``end``: the period end can be
    older than a later amended filing, and some entries omit it entirely.
    """

    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    best: dict[str, Any] | None = None
    for unit, entries in dict(payload.get("units") or {}).items():
        if str(unit).lower() != "shares":
            continue
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            value = entry.get("val")
            filed = _parse_day(entry.get("filed"))
            if value is None or filed is None:
                continue
            try:
                shares = float(value)
            except (TypeError, ValueError):
                continue
            if shares <= 0:
                continue
            if best is None or filed > best["filed_date"]:
                best = {
                    "shares": shares,
                    "filed": filed.isoformat(),
                    "filed_date": filed,
                    "period_end": (_parse_day(entry.get("end")) or filed).isoformat(),
                    "form": str(entry.get("form") or ""),
                }
    return best


def collect_share_counts(
    *,
    source: WebSourceConfig,
    config: WebCollectionConfig,
    tickers: list[str],
    out_dir: str | Path,
    store: SQLiteScanStore | None = None,
    persist: bool = False,
    as_of: str | None = None,
    max_filing_age_days: int = MAX_FILING_AGE_DAYS,
) -> dict[str, Any]:
    """Resolve a current share count per ticker, refusing stale evidence."""

    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    clean = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    reference = _parse_day(as_of) or datetime.now(timezone.utc).date()
    cutoff = reference - timedelta(days=max(1, int(max_filing_age_days)))

    summary: dict[str, Any] = {
        "status": "no_tickers",
        "as_of": reference.isoformat(),
        "cutoff": cutoff.isoformat(),
        "source_kind": FLOAT_SOURCE_KIND,
        "proxy_note": FLOAT_PROXY_NOTE,
        "shares_by_ticker": {},
        "resolved_tickers": [],
        "stale_tickers": [],
        "unavailable_tickers": [],
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if not clean:
        write_json(output_dir / "sec_share_counts.json", summary)
        return summary

    cik_map = fetch_company_ticker_map(source, config, store=store, persist=persist)
    if not cik_map:
        summary["status"] = "cik_map_unavailable"
        summary["unavailable_tickers"] = clean
        write_json(output_dir / "sec_share_counts.json", summary)
        return summary

    resolved: dict[str, Any] = {}
    stale: list[str] = []
    unavailable: list[str] = []

    for ticker in clean:
        cik = cik_map.get(ticker)
        if cik is None:
            unavailable.append(ticker)
            continue
        best: dict[str, Any] | None = None
        used_concept = ""
        for taxonomy, tag in SHARE_COUNT_CONCEPTS:
            url = COMPANY_CONCEPT_URL.format(cik=int(cik), taxonomy=taxonomy, tag=tag)
            fetch = fetch_text(source, config, url=url, allow_unlisted_url=True)
            if persist and store is not None:
                store.persist_web_fetch_run(fetch.payload())
            if fetch.status != "success":
                continue
            candidate = _latest_share_count(fetch.content)
            if candidate:
                best = candidate
                used_concept = f"{taxonomy}:{tag}"
                break
        if best is None:
            unavailable.append(ticker)
            continue
        if best["filed_date"] < cutoff:
            # Refuse rather than enrich with a share count that predates the cutoff.
            stale.append(ticker)
            continue
        resolved[ticker] = {
            "ticker": ticker,
            "cik": int(cik),
            "shares_outstanding": best["shares"],
            "filed": best["filed"],
            "period_end": best["period_end"],
            "form": best["form"],
            "concept": used_concept,
            "source_kind": FLOAT_SOURCE_KIND,
            "proxy_note": FLOAT_PROXY_NOTE,
        }

    summary["shares_by_ticker"] = resolved
    summary["resolved_tickers"] = sorted(resolved)
    summary["stale_tickers"] = sorted(stale)
    summary["unavailable_tickers"] = sorted(unavailable)
    summary["status"] = "success" if resolved else "no_share_counts"
    summary["resolved_count"] = len(resolved)
    summary["requested_count"] = len(clean)
    write_json(output_dir / "sec_share_counts.json", summary)
    return summary


def enrich_rows_with_float(
    rows: list[dict[str, Any]],
    share_counts: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach ``float_shares`` where a current share count was resolved.

    Rows without a resolved count are returned unchanged and keep their
    ``unknown_float`` risk flag, so a missing float stays visibly missing.
    """

    resolved = dict(share_counts.get("shares_by_ticker") or {})
    if not resolved:
        return [dict(row) for row in rows]

    enriched: list[dict[str, Any]] = []
    for row in rows:
        output = dict(row)
        ticker = str(output.get("ticker") or output.get("symbol") or "").upper()
        record = resolved.get(ticker)
        if not record:
            enriched.append(output)
            continue
        existing = output.get("float_shares")
        try:
            has_existing = existing is not None and float(existing) > 0
        except (TypeError, ValueError):
            has_existing = False
        if has_existing:
            # A caller-supplied float is more precise than an outstanding-share
            # proxy; never overwrite it.
            enriched.append(output)
            continue
        output["float_shares"] = record["shares_outstanding"]
        output["float_source"] = record["source_kind"]
        output["float_source_concept"] = record["concept"]
        output["float_as_of"] = record["filed"]
        output["float_is_outstanding_proxy"] = True
        output["float_proxy_note"] = record["proxy_note"]
        for field in ("risk_flags", "avoid_reasons", "data_warnings"):
            text = str(output.get(field) or "")
            if "unknown_float" in text:
                parts = [
                    part
                    for part in text.replace(",", ";").split(";")
                    if part.strip() and part.strip() != "unknown_float"
                ]
                output[field] = ";".join(parts)
        enriched.append(output)
    return enriched


__all__ = [
    "COMPANY_CONCEPT_URL",
    "FLOAT_PROXY_NOTE",
    "FLOAT_SOURCE_KIND",
    "MAX_FILING_AGE_DAYS",
    "SHARE_COUNT_CONCEPTS",
    "collect_share_counts",
    "enrich_rows_with_float",
]
