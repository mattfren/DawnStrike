"""Public HTML table extraction and snapshot normalization."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SNAPSHOT_COLUMNS, SnapshotRow, utc_now_iso
from intraday_scanner.providers.web_source_base import (
    FetchResult,
    WebCollectionConfig,
    WebSourceConfig,
    artifact_payload,
    fetch_text,
    write_json,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

TABLE_COLUMN_SIGNALS = {
    "symbol",
    "ticker",
    "name",
    "company name",
    "last",
    "price",
    "premarket price",
    "premkt. price",
    "premkt price",
    "pre-market price",
    "pre-mkt price",
    "pre mkt price",
    "pre-mkt gap %",
    "pre mkt gap %",
    "pre-mkt vol",
    "pre mkt vol",
    "pre. volume",
    "pre volume",
    "change %",
    "% change",
    "gap %",
    "chg %",
    "volume",
    "premarket volume",
    "market cap",
    "mkt cap",
    "float",
    "news",
    "headline",
    "high",
    "low",
    "previous close",
    "prev close",
    "relative volume",
    "rel volume",
    "rel vol",
}

ALIASES = {
    "ticker": ["ticker", "symbol"],
    "company": ["company", "company name", "name", "security", "security name"],
    "premarket_price": [
        "premarket_price",
        "premarket price",
        "premkt. price",
        "premkt price",
        "pre-market price",
        "pre-mkt price",
        "pre mkt price",
        "price",
        "last",
    ],
    "previous_close": ["previous_close", "previous close", "prev close", "prev_close", "close"],
    "premarket_high": ["premarket_high", "premarket high", "pre-market high", "high"],
    "premarket_low": ["premarket_low", "premarket low", "pre-market low", "low"],
    "premarket_volume": [
        "premarket_volume",
        "premarket volume",
        "pre-market volume",
        "pre-mkt vol",
        "pre mkt vol",
        "pre. volume",
        "pre volume",
        "volume",
        "vol",
    ],
    "gap_pct": [
        "gap_pct",
        "gap %",
        "pre-mkt gap %",
        "pre mkt gap %",
        "premarket change %",
        "pre-market change %",
        "change %",
        "% change",
        "chg %",
        "pre-mkt chg %",
        "pre mkt chg %",
    ],
    "float_shares": ["float_shares", "float shares", "float"],
    "market_cap": ["market_cap", "market cap", "mkt cap"],
    "spread_pct": ["spread_pct", "spread %", "spread"],
    "short_float_pct": ["short_float_pct", "short float", "short float %"],
    "catalyst_headline": ["catalyst_headline", "headline", "news", "catalyst"],
    "catalyst_url": ["catalyst_url", "url", "link", "source url", "source_url"],
    "relative_volume": ["relative volume", "rel volume", "rel vol", "relative_volume"],
}


@dataclass(frozen=True)
class ExtractedTable:
    index: int
    headers: list[str]
    rows: list[dict[str, str]]
    score: int


class _RowRejected(DataProviderError):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def ingest_public_table(
    *,
    url: str,
    source: WebSourceConfig,
    config: WebCollectionConfig,
    out_dir: str | Path,
    store: SQLiteScanStore | None = None,
    persist: bool = False,
    print_rows: bool = False,
    allow_unlisted_url: bool = False,
) -> dict[str, Any]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fetch = fetch_text(source, config, url=url, allow_unlisted_url=allow_unlisted_url)
    if persist and store is not None:
        store.persist_web_fetch_run(fetch.payload())
    if fetch.status != "success":
        failure = _failure(fetch, output_dir, "fetch_failed")
        if persist and store is not None:
            _persist_failure(store, fetch, failure)
        return failure

    raw_path = output_dir / "raw_source.html"
    if config.save_raw:
        raw_path.write_text(fetch.content, encoding="utf-8")
        if persist and store is not None:
            store.persist_raw_source_artifact(
                artifact_payload(
                    run_id=fetch.run_id,
                    source=fetch.source,
                    artifact_kind="html",
                    path=raw_path,
                    content_type=fetch.content_type,
                    metadata={"url": fetch.url, "from_fixture": fetch.from_fixture},
                )
            )
    tables = extract_html_tables(fetch.content)
    best = select_best_table(tables)
    extracted_path = output_dir / "extracted_tables.csv"
    write_extracted_tables(extracted_path, tables)
    if best is None:
        failure = _failure(fetch, output_dir, "no_candidate_table")
        if persist and store is not None:
            _persist_failure(store, fetch, failure)
        return failure

    rows, warnings, normalization_debug = normalize_public_table_rows_with_debug(
        best,
        source_name=source.name,
        source_url=fetch.url,
        raw_file_path=str(raw_path if config.save_raw else ""),
    )
    snapshot_path = output_dir / "premarket_snapshot.csv"
    extracted_rows_path = output_dir / "extracted_rows.csv"
    rejected_rows_path = output_dir / "rejected_rows.csv"
    debug_path = output_dir / "normalization_debug.json"
    _write_snapshot(snapshot_path, rows)
    _write_dynamic_csv(extracted_rows_path, normalization_debug["extracted_rows"])
    _write_dynamic_csv(rejected_rows_path, normalization_debug["rejected_rows"])
    write_json(debug_path, normalization_debug)
    summary = {
        "status": "success" if rows else "no_valid_rows",
        "run_id": fetch.run_id,
        "source": source.name,
        "source_type": source.type,
        "url": fetch.url,
        "table_count": len(tables),
        "selected_table_index": best.index,
        "selected_table_score": best.score,
        "rows_extracted": sum(len(table.rows) for table in tables),
        "rows_normalized": len(rows),
        "rows_rejected": len(normalization_debug["rejected_rows"]),
        "rejection_reason_counts": normalization_debug["rejection_reason_counts"],
        "warnings": warnings,
        "paths": {
            "extracted_tables": str(extracted_path),
            "extracted_rows": str(extracted_rows_path),
            "rejected_rows": str(rejected_rows_path),
            "normalization_debug": str(debug_path),
            "premarket_snapshot": str(snapshot_path),
            "raw_source": str(raw_path if config.save_raw else ""),
        },
        "data_source_kind": "web_url",
        "shadow_mode": True,
        "paid_data": False,
        "coverage_warning": "url_table_unverified",
    }
    write_json(output_dir / "extraction_summary.json", summary)
    if persist and store is not None:
        store.persist_web_fetch_result(
            {
                "run_id": fetch.run_id,
                "source": source.name,
                "status": summary["status"],
                "row_count": len(rows),
                "artifact_path": str(snapshot_path),
                "failure_reason": "",
                "summary": summary,
            }
        )
        store.record_source_health(
            source.name,
            "ok" if rows else "partial",
            utc_now_iso(),
            f"tables={len(tables)} rows={len(rows)}",
            summary,
        )
        store.persist_normalized_source_rows(fetch.run_id, source.name, rows)
    if print_rows:
        print_rows_preview(rows)
    return summary


def extract_html_tables(html: str) -> list[ExtractedTable]:
    parser = _TableParser()
    parser.feed(html)
    tables: list[ExtractedTable] = []
    for index, raw_rows in enumerate(parser.tables):
        if len(raw_rows) < 2:
            continue
        headers = [_clean_header(cell) for cell in raw_rows[0]]
        rows = []
        for raw in raw_rows[1:]:
            if not any(cell.strip() for cell in raw):
                continue
            values = raw + [""] * max(0, len(headers) - len(raw))
            rows.append(dict(zip(headers, values[: len(headers)], strict=False)))
        score = score_table(headers)
        tables.append(ExtractedTable(index=index, headers=headers, rows=rows, score=score))
    return tables


def select_best_table(tables: list[ExtractedTable]) -> ExtractedTable | None:
    candidates = [table for table in tables if table.score > 0 and table.rows]
    if not candidates:
        return None
    return sorted(candidates, key=lambda table: (table.score, len(table.rows)), reverse=True)[0]


def score_table(headers: list[str]) -> int:
    normalized = {_clean_header(header) for header in headers}
    score = 0
    for header in normalized:
        if header in TABLE_COLUMN_SIGNALS:
            score += 2
        if "symbol" in header or "ticker" in header:
            score += 4
        if "volume" in header:
            score += 2
        if "price" in header or header == "last":
            score += 2
    return score


def normalize_public_table_rows(
    table: ExtractedTable,
    *,
    source_name: str,
    source_url: str,
    raw_file_path: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    rows, warnings, _debug = normalize_public_table_rows_with_debug(
        table,
        source_name=source_name,
        source_url=source_url,
        raw_file_path=raw_file_path,
    )
    return rows, warnings


def normalize_public_table_rows_with_debug(
    table: ExtractedTable,
    *,
    source_name: str,
    source_url: str,
    raw_file_path: str = "",
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    imported_at = utc_now_iso()
    normalized: list[dict[str, Any]] = []
    warnings: list[str] = []
    rejected_rows: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    extracted_rows = _flatten_extracted_rows(table, source_name)
    for index, raw in enumerate(table.rows):
        try:
            row, row_warnings = _normalize_row(
                raw,
                source_name=source_name,
                source_url=source_url,
                raw_file_path=raw_file_path,
                imported_at=imported_at,
            )
            SnapshotRow.from_mapping(row, source=source_name)
            normalized.append(row)
            warnings.extend(f"{row['ticker']}: {warning}" for warning in row_warnings)
        except _RowRejected as exc:
            ticker = _text(_alias(raw, "ticker")) or "unknown"
            reason_counts[exc.reason] = reason_counts.get(exc.reason, 0) + 1
            warnings.append(f"{ticker}: {exc.detail}")
            rejected_rows.append(
                _rejected_row(
                    source_name=source_name,
                    row_index=index,
                    reason=exc.reason,
                    detail=exc.detail,
                    raw=raw,
                )
            )
        except (DataProviderError, ValueError) as exc:
            ticker = _text(_alias(raw, "ticker")) or "unknown"
            reason = _classify_rejection(str(exc))
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            warnings.append(f"{ticker}: {exc}")
            rejected_rows.append(
                _rejected_row(
                    source_name=source_name,
                    row_index=index,
                    reason=reason,
                    detail=str(exc),
                    raw=raw,
                )
            )
    debug = {
        "source": source_name,
        "table_index": table.index,
        "headers": table.headers,
        "rows_extracted": len(table.rows),
        "rows_normalized": len(normalized),
        "rows_rejected": len(rejected_rows),
        "rejection_reason_counts": reason_counts,
        "extracted_rows": extracted_rows,
        "rejected_rows": rejected_rows,
        "warnings": warnings,
    }
    return normalized, warnings, debug


def write_extracted_tables(path: str | Path, tables: list[ExtractedTable]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["table_index", "table_score", "row_index", "headers_json", "row_json"]
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for table in tables:
            for row_index, row in enumerate(table.rows):
                writer.writerow(
                    {
                        "table_index": table.index,
                        "table_score": table.score,
                        "row_index": row_index,
                        "headers_json": repr(table.headers),
                        "row_json": repr(row),
                    }
                )


def print_rows_preview(rows: list[dict[str, Any]]) -> None:
    for row in rows[:10]:
        print(
            f"{row.get('ticker')} price={row.get('premarket_price')} "
            f"gap={row.get('gap_pct')} volume={row.get('premarket_volume')}"
        )


def _normalize_row(
    row: dict[str, Any],
    *,
    source_name: str,
    source_url: str,
    raw_file_path: str,
    imported_at: str,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    ticker, company_hint = _normalize_ticker_and_company(
        _alias(row, "ticker"),
        _alias(row, "company"),
        source_name,
    )
    if not ticker:
        raise _RowRejected("missing_ticker", "ticker/symbol is required")
    price = _required_number(_alias(row, "premarket_price"), "price/last/premarket_price")
    volume = int(_required_number(_alias(row, "premarket_volume"), "volume"))
    gap_pct = _optional_number(_alias(row, "gap_pct"))
    previous_close = _optional_number(_alias(row, "previous_close"))
    if gap_pct is None and previous_close is None:
        raise _RowRejected(
            "missing_gap_or_previous_close",
            "gap/change percent or previous close is required",
        )
    high = _optional_number(_alias(row, "premarket_high"))
    low = _optional_number(_alias(row, "premarket_low"))
    if previous_close is None:
        warnings.append("previous_close_unavailable")
    previous_close_value = "" if previous_close is None else round(float(previous_close), 6)
    if high is None or low is None:
        warnings.append("premarket_range_unavailable_price_used")
    high_value = float(high) if high is not None else price
    low_value = float(low) if low is not None else price
    headline = _text(_alias(row, "catalyst_headline"))
    coverage = [
        "url_table_unverified",
        "halt_status_unverified",
        "sec_risk_unverified",
        *warnings,
    ]
    float_shares = _optional_number(_alias(row, "float_shares"))
    market_cap = _optional_number(_alias(row, "market_cap"))
    short_float = _optional_number(_alias(row, "short_float_pct"))
    relative_volume = _optional_number(_alias(row, "relative_volume"))
    optional_missing = sum(
        value is None for value in (float_shares, market_cap, short_float)
    ) + 3
    normalized = {
        "ticker": ticker,
        "company": company_hint or ticker,
        "previous_close": previous_close_value,
        "premarket_price": price,
        "premarket_high": high_value,
        "premarket_low": low_value,
        "premarket_volume": volume,
        "dollar_volume": round(price * volume, 2),
        "gap_pct": round(
            gap_pct if gap_pct is not None else _gap_pct(price, float(previous_close or 0)),
            4,
        ),
        "float_shares": "" if float_shares is None else float_shares,
        "market_cap": "" if market_cap is None else market_cap,
        "spread_pct": _optional_number(_alias(row, "spread_pct")) or 0,
        "short_float_pct": "" if short_float is None else short_float,
        "has_news": bool(headline),
        "catalyst_headline": headline,
        "catalyst_url": _text(_alias(row, "catalyst_url")) or source_url,
        "current_halt": False,
        "recent_offering": False,
        "reverse_split_90d": False,
        "source": source_name,
        "as_of_timestamp": imported_at,
        "data_source_kind": "web_url",
        "shadow_mode": True,
        "paid_data": False,
        "fixture_only": False,
        "manual_uploaded_data": False,
        "data_quality_score": max(20, 100 - optional_missing * 10),
        "coverage_warning": ";".join(coverage),
        "missing_enrichment_count": optional_missing,
        "raw_file_path": raw_file_path,
        "imported_at": imported_at,
        "source_url": source_url,
    }
    if relative_volume is not None:
        normalized["relative_volume"] = relative_volume
    return normalized, warnings


def _failure(fetch: FetchResult, out_dir: Path, reason: str) -> dict[str, Any]:
    failure = {
        "status": "failed",
        "run_id": fetch.run_id,
        "source": fetch.source,
        "source_type": fetch.source_type,
        "url": fetch.url,
        "reason": reason,
        "failure_reason": fetch.failure_reason,
        "started_at": fetch.started_at,
        "completed_at": fetch.completed_at,
    }
    write_json(out_dir / "failure_report.json", failure)
    return failure


def _persist_failure(store: SQLiteScanStore, fetch: FetchResult, failure: dict[str, Any]) -> None:
    store.persist_web_fetch_result(
        {
            "run_id": fetch.run_id,
            "source": fetch.source,
            "status": "failed",
            "row_count": 0,
            "artifact_path": "",
            "failure_reason": str(failure.get("failure_reason") or failure.get("reason") or ""),
            "summary": failure,
        }
    )
    store.record_source_health(
        fetch.source,
        "failed",
        utc_now_iso(),
        str(failure.get("failure_reason") or failure.get("reason") or ""),
        failure,
    )


def _write_snapshot(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_dynamic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or ["empty"])
        writer.writeheader()
        writer.writerows(rows)


def _flatten_extracted_rows(table: ExtractedTable, source_name: str) -> list[dict[str, Any]]:
    rows = []
    for index, raw in enumerate(table.rows):
        flattened = {
            "source": source_name,
            "table_index": table.index,
            "table_score": table.score,
            "row_index": index,
            "headers": json.dumps(table.headers),
        }
        flattened.update({f"raw_{key}": value for key, value in raw.items()})
        rows.append(flattened)
    return rows


def _rejected_row(
    *,
    source_name: str,
    row_index: int,
    reason: str,
    detail: str,
    raw: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source": source_name,
        "row_index": row_index,
        "reason": reason,
        "detail": detail,
        "ticker": _text(_alias(raw, "ticker")),
        "raw_json": json.dumps(raw, sort_keys=True),
    }


def _alias(row: dict[str, Any], key: str) -> Any:
    lookup = {_clean_header(raw_key): value for raw_key, value in row.items()}
    for alias in ALIASES[key]:
        if alias in lookup:
            return lookup[alias]
    return ""


def _clean_header(value: Any) -> str:
    text = str(value or "").replace("\xa0", " ")
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _required_number(value: Any, column: str) -> float:
    parsed = _optional_number(value)
    if parsed is None:
        if "price" in column or "last" in column:
            raise _RowRejected("missing_price", f"{column} is required")
        if "volume" in column:
            raise _RowRejected("missing_volume", f"{column} is required")
        raise _RowRejected("invalid_numeric_format", f"{column} is required")
    return parsed


def _optional_number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    text = (
        str(value)
        .strip()
        .replace("\xa0", " ")
        .replace("−", "-")
        .replace("$", "")
        .replace(",", "")
        .replace("%", "")
    )
    text = re.sub(r"\b(usd|us\$|eur|gbp|cad)\b", "", text, flags=re.IGNORECASE).strip()
    if not text or text in {"-", "—", "N/A", "n/a"}:
        return None
    multiplier = 1.0
    suffix = text[-1:].lower()
    if suffix in {"k", "m", "b"}:
        multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix]
        text = text[:-1]
    elif suffix == "t":
        multiplier = 1_000_000_000_000
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError as exc:
        raise _RowRejected(
            "invalid_numeric_format", f"could not parse number {value!r}"
        ) from exc


def _gap_pct(price: float, previous_close: float) -> float:
    if previous_close <= 0:
        return 0.0
    return ((price - previous_close) / previous_close) * 100


def _normalize_ticker_and_company(
    raw_ticker: Any,
    raw_company: Any,
    source_name: str,
) -> tuple[str, str]:
    ticker_text = _text(raw_ticker)
    company_text = _text(raw_company)
    if ":" in ticker_text and " " not in ticker_text:
        ticker_text = ticker_text.rsplit(":", 1)[-1]
    if "tradingview" in source_name.lower() and ticker_text:
        parsed_ticker, parsed_company = _split_tradingview_symbol(ticker_text)
        ticker_text = parsed_ticker
        company_text = company_text or parsed_company
    ticker = re.sub(r"[^A-Za-z0-9.\-]", "", ticker_text).upper()
    return ticker, company_text.strip()


def _split_tradingview_symbol(value: str) -> tuple[str, str]:
    text = " ".join(value.split())
    if not text:
        return "", ""
    first_token = text.split(" ", 1)[0]
    match = re.match(r"^([A-Z][A-Z0-9.]{0,5})(?=[A-Z][a-z])(.+)$", first_token)
    if match:
        ticker = match.group(1)
        company = match.group(2)
        if " " in text:
            company = f"{company} {text.split(' ', 1)[1]}"
        return ticker, company.strip()
    return first_token, text.split(" ", 1)[1] if " " in text else ""


def _classify_rejection(detail: str) -> str:
    lowered = detail.lower()
    if "ticker" in lowered or "symbol" in lowered:
        return "missing_ticker"
    if "price" in lowered or "last" in lowered:
        return "missing_price"
    if "volume" in lowered:
        return "missing_volume"
    if "gap" in lowered or "previous close" in lowered:
        return "missing_gap_or_previous_close"
    if "parse number" in lowered or "numeric" in lowered:
        return "invalid_numeric_format"
    if "login" in lowered or "captcha" in lowered or "anti-bot" in lowered:
        return "blocked_or_login_required"
    if "no_candidate_table" in lowered or "no visible candidate" in lowered:
        return "no_candidate_table"
    return "unknown_columns"


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._in_cell = False
        self._cell = ""
        self._row: list[str] = []
        self._table: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        if tag == "table":
            self._in_table = True
            self._table = []
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in {"td", "th"} and self._in_table:
            self._in_cell = True
            self._cell = ""

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell += data

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._in_cell:
            self._row.append(" ".join(self._cell.split()))
            self._in_cell = False
        elif tag == "tr" and self._in_table and self._row:
            self._table.append(self._row)
        elif tag == "table" and self._in_table:
            if self._table:
                self.tables.append(self._table)
            self._in_table = False
