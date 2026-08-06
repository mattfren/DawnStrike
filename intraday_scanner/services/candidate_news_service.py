"""Attach point-in-time Alpaca news facts to premarket candidates."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SNAPSHOT_COLUMNS, utc_now_iso
from intraday_scanner.providers.base import NewsItem
from intraday_scanner.providers.news_provider import (
    AlpacaNewsProvider,
    headline_has_dilution_risk,
)
from intraday_scanner.services.premarket_intelligence import classify_catalyst


def enrich_candidate_news(
    rows: list[dict[str, Any]],
    *,
    config: ScannerConfig,
    requested_at: datetime | None = None,
    max_symbols: int = 60,
    rehearsal_mode: bool = False,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Enrich candidate rows; provider failure stays explicit and non-fatal."""

    at = requested_at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    at = at.astimezone(timezone.utc)
    tickers = [
        str(row.get("ticker") or "").upper()
        for row in rows[: max(1, max_symbols)]
        if row.get("ticker")
    ]
    started_at = utc_now_iso()
    items = []
    if rehearsal_mode:
        status = "rehearsal_not_applicable"
    elif not tickers:
        status = "not_applicable"
    else:
        status = "success"
    failure_reason = ""
    if tickers and not rehearsal_mode:
        try:
            items = AlpacaNewsProvider(config).get_news(
                tickers,
                since=(at - timedelta(hours=18)).isoformat(),
            )
        except (DataProviderError, OSError, TypeError, ValueError) as exc:
            status = "failed"
            failure_reason = str(exc)
    latest: dict[str, NewsItem] = {}
    for item in items:
        ticker = str(item.ticker or "").upper()
        prior = latest.get(ticker)
        if prior is None or str(item.published_at) > str(prior.published_at):
            latest[ticker] = item
    enriched: list[dict[str, Any]] = []
    matched = 0
    for row in rows:
        updated = dict(row)
        ticker = str(updated.get("ticker") or "").upper()
        news_item = latest.get(ticker)
        if news_item is not None:
            matched += 1
            assessment = classify_catalyst(news_item.headline, has_news=True)
            updated.update(
                {
                    "has_news": True,
                    "catalyst_headline": news_item.headline,
                    "catalyst_url": news_item.url,
                    "catalyst_summary": assessment.catalyst_summary,
                    "catalyst_tier": assessment.catalyst_tier,
                    "catalyst_category": assessment.catalyst_category,
                    "catalyst_confidence": assessment.catalyst_confidence,
                    "catalyst_status": "VERIFIED",
                    "catalyst_risk_flags": ";".join(assessment.catalyst_risk_flags),
                }
            )
            if headline_has_dilution_risk(news_item.headline):
                updated["recent_offering"] = True
                warnings = [
                    part
                    for part in str(updated.get("coverage_warning") or "").split(";")
                    if part
                ]
                warnings.append("news_dilution_language")
                updated["coverage_warning"] = ";".join(dict.fromkeys(warnings))
        elif ticker in tickers:
            if not str(updated.get("catalyst_status") or "").strip():
                updated["catalyst_status"] = "MISSING"
            if updated.get("catalyst_confidence") in {None, ""}:
                updated["catalyst_confidence"] = 0.2
        enriched.append(updated)
    summary = {
        "schema_version": "dawnstrike.candidate_news.v1",
        "status": status,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "requested_at": at.isoformat(),
        "provider": "alpaca_news",
        "queried_symbol_count": 0 if rehearsal_mode else len(tickers),
        "article_count": len(items),
        "matched_symbol_count": matched,
        "failure_reason": failure_reason,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    result: dict[str, Any] = {"rows": enriched, "summary": summary}
    if out_dir is not None:
        output = Path(out_dir)
        output.mkdir(parents=True, exist_ok=True)
        snapshot = output / "premarket_snapshot_with_news.csv"
        with snapshot.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(enriched)
        (output / "candidate_news_summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result["snapshot_path"] = str(snapshot)
    return result


__all__ = ["enrich_candidate_news"]
