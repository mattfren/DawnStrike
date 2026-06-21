"""Zero-dollar manual shadow-mode workflow helpers."""

from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from intraday_scanner.errors import DataProviderError, SnapshotValidationError
from intraday_scanner.models import SNAPSHOT_COLUMNS, utc_now_iso, validate_required_columns
from intraday_scanner.reporting import read_csv_dicts
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

OUTCOME_COLUMNS = [
    "date",
    "ticker",
    "entry_time",
    "entry_price",
    "price_1m",
    "price_5m",
    "price_15m",
    "lunch_price",
    "close_price",
    "high_after_entry",
    "low_after_entry",
    "halted",
    "source",
    "notes",
]

MANUAL_AUDIT_COLUMNS = [
    "audit_status",
    "audit_reason",
    "manual_uploaded_data",
    "paid_data",
    "scan_id",
    "rank",
    "ticker",
    "recommendation_timestamp",
    "entry_time",
    "entry_price",
    "return_1m_pct",
    "return_1m_status",
    "return_5m_pct",
    "return_5m_status",
    "return_15m_pct",
    "return_15m_status",
    "lunch_return_pct",
    "lunch_return_status",
    "close_return_pct",
    "close_return_status",
    "high_return_pct",
    "high_return_status",
    "low_drawdown_pct",
    "low_drawdown_status",
    "max_favorable_excursion_pct",
    "max_adverse_excursion_pct",
    "source",
    "notes",
]

UNIVERSE_COLUMNS = [
    "ticker",
    "company",
    "exchange",
    "source",
    "security_type",
    "cik",
    "is_etf",
    "is_test_issue",
    "include_reason",
    "exclude_reason",
]

ENRICHMENT_FIELDS = [
    "float_shares",
    "market_cap",
    "short_float_pct",
    "catalyst_headline",
    "catalyst_url",
    "current_halt",
    "recent_offering",
    "reverse_split_90d",
]


def print_upload_prompt() -> str:
    return Path("templates/chatgpt_screener_to_snapshot_prompt.md").read_text(encoding="utf-8")


def import_manual_snapshot(
    *,
    input_path: str | Path,
    out_dir: str | Path,
    store: SQLiteScanStore | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    raw_rows = _read_csv(input_path)
    normalized = [_normalize_snapshot_row(row) for row in raw_rows]
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "premarket_snapshot.csv"
    _write_csv(output_path, normalized, SNAPSHOT_COLUMNS)
    upload_id = str(uuid.uuid4())
    created_at = utc_now_iso()
    summary = _snapshot_summary(
        upload_id=upload_id,
        created_at=created_at,
        input_path=str(input_path),
        output_path=str(output_path),
        rows=normalized,
    )
    if persist:
        if store is None:
            raise ValueError("persist=True requires a SQLite store")
        store.persist_manual_snapshot_upload(
            upload_id=upload_id,
            created_at=created_at,
            input_path=str(input_path),
            output_path=str(output_path),
            raw_rows=raw_rows,
            normalized_rows=normalized,
            summary=summary,
        )
    return {"summary": summary, "path": str(output_path), "rows": normalized}


def import_manual_outcomes(
    *,
    input_path: str | Path,
    store: SQLiteScanStore,
    persist: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    rows = _read_csv(input_path)
    if not rows:
        raise SnapshotValidationError(f"{input_path} has no outcome rows")
    validate_required_columns(set(rows[0]), OUTCOME_COLUMNS, str(input_path))
    recommendations = store.load_recommendation_theses(limit=5000)
    normalized = [
        _normalize_outcome(row, _match_recommendation(row, recommendations)) for row in rows
    ]
    stats = {"inserted": 0, "skipped": 0}
    if persist:
        stats = store.persist_manual_outcomes(normalized, replace=replace)
    return {
        "created_at": utc_now_iso(),
        "manual_uploaded_data": True,
        "paid_data": False,
        "input_path": str(input_path),
        "row_count": len(normalized),
        **stats,
        "rows": normalized,
    }


def audit_manual_outcomes(
    *,
    store: SQLiteScanStore,
    out_dir: str | Path,
    persist: bool = False,
) -> dict[str, Any]:
    outcomes = list(reversed(store.load_manual_outcomes(limit=5000)))
    if not outcomes:
        raise SnapshotValidationError("No manual outcomes are available to audit.")
    trades = [_manual_trade(row) for row in outcomes]
    summary = _manual_audit_summary(trades)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / "manual_audit_trades.csv"
    summary_path = output_dir / "manual_audit_summary.json"
    _write_csv(trades_path, trades, MANUAL_AUDIT_COLUMNS)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    if persist:
        store.persist_manual_audit(summary, trades)
    return {
        "summary": summary,
        "trades": trades,
        "paths": {"trades": trades_path, "summary": summary_path},
    }


def build_free_shadow_report(
    *,
    store: SQLiteScanStore,
    out_dir: str | Path,
    persist: bool = False,
) -> dict[str, Any]:
    outcomes = list(reversed(store.load_manual_outcomes(limit=5000)))
    trades = [_manual_trade(row) for row in outcomes] if outcomes else list(
        reversed(store.load_manual_audit_trades(limit=5000))
    )
    if not trades:
        raise SnapshotValidationError("No manual outcomes are available for shadow reporting.")
    latest_scan = store.load_latest_scan() or {}
    scan_history = store.load_scan_history(limit=5000)
    uploads = store.load_manual_snapshot_uploads(limit=5000)
    report = {
        **_manual_audit_summary(trades),
        "created_at": utc_now_iso(),
        "manual_uploaded_data": True,
        "shadow_mode": True,
        "paid_data": False,
        "provider_validated": False,
        "scan_day_count": len({str(row.get("created_at", ""))[:10] for row in scan_history}),
        "recommendation_count": sum(int(row.get("ranked_count", 0) or 0) for row in scan_history),
        "manual_outcome_count": len(outcomes),
        "manual_snapshot_upload_count": len(uploads),
        "source_mix": _source_mix(scan_history, outcomes),
        "data_quality_summary": _data_quality_summary(latest_scan),
        "disclaimer": (
            "Manual/free shadow results are for validation only. They are not paid/live "
            "provider validation, trading advice, or proof of future returns."
        ),
    }
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "free_shadow_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    equity_path = output_dir / "free_shadow_equity_curve.csv"
    _write_csv(equity_path, report["compounded_close_equity_curve"], [])
    if persist:
        store.persist_shadow_report(report)
    return {
        "report": report,
        "paths": {"report": report_path, "equity_curve": equity_path},
    }


def build_free_universe(
    *,
    out_path: str | Path,
    rejected_path: str | Path = "outputs/universe_rejected.csv",
    summary_path: str | Path = "outputs/universe_build_summary.json",
    fixture_path: str | Path = "sample_data/universe_sample.csv",
) -> dict[str, Any]:
    symbols = _read_universe_symbols(fixture_path)
    accepted = [
        {
            "ticker": symbol,
            "company": "",
            "exchange": "",
            "source": "bundled_fixture_free",
            "security_type": "common_stock",
            "cik": "",
            "is_etf": "false",
            "is_test_issue": "false",
            "include_reason": "fixture_common_stock_candidate",
            "exclude_reason": "",
        }
        for symbol in symbols
        if _include_symbol(symbol)
    ]
    rejected = [
        {
            "ticker": symbol,
            "company": "",
            "exchange": "",
            "source": "bundled_fixture_free",
            "security_type": "unknown",
            "cik": "",
            "is_etf": "false",
            "is_test_issue": "false",
            "include_reason": "",
            "exclude_reason": "excluded_by_symbol_filter",
        }
        for symbol in symbols
        if not _include_symbol(symbol)
    ]
    out = Path(out_path)
    rejected_out = Path(rejected_path)
    summary_out = Path(summary_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rejected_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(out, accepted, UNIVERSE_COLUMNS)
    _write_csv(rejected_out, rejected, UNIVERSE_COLUMNS)
    summary = {
        "created_at": utc_now_iso(),
        "source": "bundled_fixture_free",
        "paid_data": False,
        "full_paid_data": False,
        "fixture_mode": True,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "output_path": str(out),
        "rejected_path": str(rejected_out),
        "warning": (
            "Bundled fixture universe is only a free/offline starter list. Replace it "
            "with a broad U.S. common-stock universe before serious live validation."
        ),
    }
    summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "summary": summary,
        "paths": {"universe": out, "rejected": rejected_out, "summary": summary_out},
    }


def _normalize_snapshot_row(row: dict[str, Any]) -> dict[str, Any]:
    ticker = _text(_get(row, "ticker", "symbol")).upper()
    if not ticker:
        raise SnapshotValidationError("ticker is required")
    price = _required_float(_get(row, "premarket_price", "last", "price"), "premarket_price")
    volume = int(_required_float(_get(row, "premarket_volume", "volume"), "premarket_volume"))
    previous_close = _optional_float(_get(row, "previous_close", "prev_close", "close"))
    high = _optional_float(_get(row, "premarket_high", "high")) or price
    low = _optional_float(_get(row, "premarket_low", "low")) or price
    dollar_volume = _optional_float(_get(row, "dollar_volume", "premarket_dollar_volume"))
    if dollar_volume is None:
        dollar_volume = price * volume
    gap_pct = _optional_float(_get(row, "gap_pct", "gap_percent", "gap"))
    if gap_pct is None and previous_close and previous_close > 0:
        gap_pct = ((price - previous_close) / previous_close) * 100
    normalized = {
        "ticker": ticker,
        "company": _text(_get(row, "company", "name")) or ticker,
        "previous_close": _format_optional(previous_close),
        "premarket_price": _round(price),
        "premarket_high": _round(high),
        "premarket_low": _round(low),
        "premarket_volume": volume,
        "dollar_volume": _round(dollar_volume),
        "gap_pct": _round(gap_pct or 0.0),
        "float_shares": _format_optional(_optional_float(_get(row, "float_shares", "float"))),
        "market_cap": _format_optional(_optional_float(_get(row, "market_cap"))),
        "spread_pct": _round(_optional_float(_get(row, "spread_pct", "spread")) or 0.0),
        "short_float_pct": _format_optional(
            _optional_float(_get(row, "short_float_pct", "short_float"))
        ),
        "has_news": _bool_text(_get(row, "has_news", "news")),
        "catalyst_headline": _text(_get(row, "catalyst_headline", "headline", "catalyst")),
        "catalyst_url": _text(_get(row, "catalyst_url", "url")),
        "current_halt": _bool_text(_get(row, "current_halt", "halted")),
        "recent_offering": _bool_text(_get(row, "recent_offering", "offering")),
        "reverse_split_90d": _bool_text(_get(row, "reverse_split_90d", "reverse_split")),
        "source": _text(_get(row, "source")) or "manual_upload",
        "as_of_timestamp": _text(_get(row, "as_of_timestamp", "timestamp")) or utc_now_iso(),
        "data_source_kind": "manual",
        "shadow_mode": "true",
        "paid_data": "false",
        "fixture_only": "false",
        "manual_uploaded_data": "true",
    }
    missing = _missing_fields(normalized)
    normalized["coverage_warning"] = ";".join(missing) if missing else "complete"
    normalized["missing_enrichment_count"] = sum(
        1 for field in ENRICHMENT_FIELDS if not normalized.get(field)
    )
    normalized["data_quality_score"] = _manual_data_quality_score(normalized)
    return normalized


def _normalize_outcome(row: dict[str, Any], recommendation: dict[str, Any]) -> dict[str, Any]:
    ticker = _text(row.get("ticker")).upper()
    date_value = _text(row.get("date"))
    entry_time = _normalize_entry_time(date_value, _text(row.get("entry_time")))
    recommendation_time = str(recommendation.get("timestamp") or "")
    if _parse_time(entry_time) < _parse_time(recommendation_time):
        raise SnapshotValidationError(
            f"{ticker} outcome entry_time {entry_time} is before "
            f"recommendation {recommendation_time}"
        )
    scan_id = str(recommendation.get("scan_id") or "")
    return {
        "outcome_key": f"{scan_id}:{ticker}:{date_value}:{entry_time}",
        "date": date_value,
        "ticker": ticker,
        "scan_id": scan_id,
        "rank": recommendation.get("rank"),
        "recommendation_timestamp": recommendation_time,
        "uploaded_at": utc_now_iso(),
        "entry_time": entry_time,
        "entry_price": _format_optional(_optional_float(row.get("entry_price"))),
        "price_1m": _format_optional(_optional_float(row.get("price_1m"))),
        "price_5m": _format_optional(_optional_float(row.get("price_5m"))),
        "price_15m": _format_optional(_optional_float(row.get("price_15m"))),
        "lunch_price": _format_optional(_optional_float(row.get("lunch_price"))),
        "close_price": _format_optional(_optional_float(row.get("close_price"))),
        "high_after_entry": _format_optional(_optional_float(row.get("high_after_entry"))),
        "low_after_entry": _format_optional(_optional_float(row.get("low_after_entry"))),
        "halted": _bool_text(row.get("halted")),
        "source": _text(row.get("source")) or "manual_outcome_upload",
        "notes": _text(row.get("notes")),
        "manual_uploaded_data": True,
        "paid_data": False,
    }


def _manual_trade(row: dict[str, Any]) -> dict[str, Any]:
    entry = _optional_float(row.get("entry_price"))
    if entry is None or entry <= 0:
        return {
            "audit_status": "unavailable",
            "audit_reason": "entry_price is missing",
            **_trade_base(row),
        }
    returns = {
        "return_1m_pct": _return_pct(row.get("price_1m"), entry),
        "return_5m_pct": _return_pct(row.get("price_5m"), entry),
        "return_15m_pct": _return_pct(row.get("price_15m"), entry),
        "lunch_return_pct": _return_pct(row.get("lunch_price"), entry),
        "close_return_pct": _return_pct(row.get("close_price"), entry),
        "high_return_pct": _return_pct(row.get("high_after_entry"), entry),
        "low_drawdown_pct": _return_pct(row.get("low_after_entry"), entry),
    }
    statuses = {
        key.replace("_pct", "_status"): "unavailable" if value == "" else "audited"
        for key, value in returns.items()
    }
    available = [value for value in returns.values() if value != ""]
    available_float = [float(value) for value in available]
    status = "audited" if available and len(available) == len(returns) else "partial"
    if not available:
        status = "unavailable"
    reason = "" if status == "audited" else "One or more outcome price fields are unavailable."
    favorable = max(available_float) if available_float else ""
    adverse = min(available_float) if available_float else ""
    return {
        "audit_status": status,
        "audit_reason": reason,
        **_trade_base(row),
        **returns,
        **statuses,
        "max_favorable_excursion_pct": (
            favorable if favorable == "" else round(float(favorable), 2)
        ),
        "max_adverse_excursion_pct": adverse if adverse == "" else round(float(adverse), 2),
    }


def _manual_audit_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    audited = [row for row in trades if row.get("audit_status") in {"audited", "partial"}]
    close = _values(audited, "close_return_pct")
    lunch = _values(audited, "lunch_return_pct")
    high = _values(audited, "high_return_pct")
    low = _values(audited, "low_drawdown_pct")
    return {
        "created_at": utc_now_iso(),
        "manual_uploaded_data": True,
        "shadow_mode": True,
        "paid_data": False,
        "provider_validated": False,
        "trade_count": len(audited),
        "partial_count": sum(1 for row in trades if row.get("audit_status") == "partial"),
        "unavailable_count": sum(1 for row in trades if row.get("audit_status") == "unavailable"),
        "avg_close_return_pct": _avg(close),
        "avg_lunch_return_pct": _avg(lunch),
        "avg_high_return_pct": _avg(high),
        "median_close_return_pct": round(median(close), 2) if close else 0.0,
        "hit_rate_close_pct": _hit_rate(close),
        "max_drawdown_pct": min(low) if low else 0.0,
        "best_pick": _extreme(audited, "close_return_pct", best=True),
        "worst_pick": _extreme(audited, "close_return_pct", best=False),
        "best_day": _best_day(audited, best=True),
        "worst_day": _best_day(audited, best=False),
        "top_1_close_return_pct": _portfolio_latest(audited, "close_return_pct", 1),
        "top_3_close_return_pct": _portfolio_latest(audited, "close_return_pct", 3),
        "top_5_close_return_pct": _portfolio_latest(audited, "close_return_pct", 5),
        "compounded_close_equity_curve": _compounded_curve(close),
        "compounded_top_1_equity_curve": _compounded_portfolio_curve(audited, 1),
        "compounded_top_3_equity_curve": _compounded_portfolio_curve(audited, 3),
        "compounded_top_5_equity_curve": _compounded_portfolio_curve(audited, 5),
        "disclaimer": "Manual uploaded outcomes are for shadow validation only.",
    }


def _snapshot_summary(
    *,
    upload_id: str,
    created_at: str,
    input_path: str,
    output_path: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    missing_counts = [int(row.get("missing_enrichment_count") or 0) for row in rows]
    return {
        "upload_id": upload_id,
        "created_at": created_at,
        "input_path": input_path,
        "output_path": output_path,
        "row_count": len(rows),
        "data_source_kind": "manual",
        "shadow_mode": True,
        "manual_uploaded_data": True,
        "paid_data": False,
        "fixture_only": False,
        "avg_data_quality_score": _avg(
            [float(row.get("data_quality_score") or 0) for row in rows]
        ),
        "missing_enrichment_count": sum(missing_counts),
        "coverage_warning": (
            "manual fields may be incomplete" if sum(missing_counts) else "complete"
        ),
    }


def _match_recommendation(
    row: dict[str, Any], recommendations: list[dict[str, Any]]
) -> dict[str, Any]:
    ticker = _text(row.get("ticker")).upper()
    date_value = _text(row.get("date"))
    entry_time = _normalize_entry_time(date_value, _text(row.get("entry_time")))
    same_symbol_day = []
    for rec in recommendations:
        if _text(rec.get("ticker")).upper() != ticker:
            continue
        recommendation_time = _recommendation_time_for_date(rec, date_value)
        if recommendation_time:
            same_symbol_day.append((rec, recommendation_time))
    if same_symbol_day and all(
        _parse_time(recommendation_time) > _parse_time(entry_time)
        for _rec, recommendation_time in same_symbol_day
    ):
        first_time = min(recommendation_time for _rec, recommendation_time in same_symbol_day)
        raise SnapshotValidationError(
            f"{ticker} outcome entry_time {entry_time} is before recommendation {first_time}"
        )
    candidates = [
        (rec, recommendation_time)
        for rec, recommendation_time in same_symbol_day
        if _parse_time(recommendation_time) <= _parse_time(entry_time)
    ]
    if not candidates:
        raise SnapshotValidationError(
            f"No saved recommendation exists for {ticker} on {date_value} before {entry_time}"
        )
    return sorted(candidates, key=lambda item: item[1], reverse=True)[0][0]


def _trade_base(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "manual_uploaded_data": True,
        "paid_data": False,
        "scan_id": row.get("scan_id", ""),
        "rank": row.get("rank", ""),
        "ticker": row.get("ticker", ""),
        "recommendation_timestamp": row.get("recommendation_timestamp", ""),
        "entry_time": row.get("entry_time", ""),
        "entry_price": row.get("entry_price", ""),
        "source": row.get("source", "manual_outcome_upload"),
        "notes": row.get("notes", ""),
    }


def _return_pct(value: Any, entry: float) -> float | str:
    price = _optional_float(value)
    if price is None:
        return ""
    return round(((price - entry) / entry) * 100, 2)


def _source_mix(
    scan_history: list[dict[str, Any]], outcomes: list[dict[str, Any]]
) -> dict[str, int]:
    mix: dict[str, int] = {"manual": 0, "sample": 0, "free_api": 0, "paid": 0}
    for row in scan_history:
        kind = str(row.get("data_source_kind") or row.get("source") or "").lower()
        if "manual" in kind:
            mix["manual"] += 1
        elif "sample" in kind or "fixture" in kind:
            mix["sample"] += 1
        elif "paid" in kind:
            mix["paid"] += 1
        else:
            mix["free_api"] += 1 if kind else 0
    if outcomes:
        mix["manual"] += len(outcomes)
    return mix


def _data_quality_summary(latest_scan: dict[str, Any]) -> dict[str, Any]:
    ranked = list(latest_scan.get("ranked_candidates") or [])
    all_rows = ranked + list(latest_scan.get("avoid_list") or [])
    return {
        "row_count": len(all_rows),
        "avg_data_quality_score": _avg([_num(row.get("data_quality_score")) for row in all_rows]),
        "missing_enrichment_count": sum(
            int(_num(row.get("missing_enrichment_count"))) for row in all_rows
        ),
        "coverage_warnings": sorted(
            {
                str(row.get("coverage_warning", ""))
                for row in all_rows
                if row.get("coverage_warning")
            }
        ),
    }


def _read_csv(path: str | Path) -> list[dict[str, Any]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise DataProviderError(f"CSV file does not exist: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SnapshotValidationError(f"{csv_path} is empty or missing a header row")
        return list(reader)


def _write_csv(path: str | Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not fieldnames:
        keys: list[str] = []
        for row in rows:
            keys.extend(key for key in row if key not in keys)
        fieldnames = keys
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_universe_symbols(path: str | Path) -> list[str]:
    rows = read_csv_dicts(path)
    return [str(row.get("ticker") or row.get("symbol") or "").upper() for row in rows if row]


def _include_symbol(symbol: str) -> bool:
    upper = symbol.upper()
    blocked_suffixes = ("W", "WS", "WT", "U", "R", "P", "PR", "F")
    blocked_terms = ("ETF", "ETN", "FUND", "UNIT", "RIGHT", "WARRANT", "PREFERRED", "NOTE")
    return bool(upper) and not upper.endswith(blocked_suffixes) and all(
        term not in upper for term in blocked_terms
    )


def _missing_fields(row: dict[str, Any]) -> list[str]:
    missing = [field for field in ENRICHMENT_FIELDS if row.get(field) in {None, ""}]
    if row.get("previous_close") in {None, ""}:
        missing.append("previous_close")
    return [f"{field}_unknown" for field in missing]


def _manual_data_quality_score(row: dict[str, Any]) -> float:
    checks = [
        row.get("previous_close") not in {None, ""},
        _num(row.get("premarket_price")) > 0,
        _num(row.get("premarket_volume")) > 0,
        _num(row.get("dollar_volume")) > 0,
        row.get("float_shares") not in {None, ""},
        row.get("market_cap") not in {None, ""},
        row.get("short_float_pct") not in {None, ""},
        bool(row.get("as_of_timestamp")),
    ]
    return round((sum(1 for check in checks if check) / len(checks)) * 100, 2)


def _values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [_num(row.get(key)) for row in rows if row.get(key) not in {None, ""}]


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _hit_rate(values: list[float]) -> float:
    if not values:
        return 0.0
    return round((sum(1 for value in values if value > 0) / len(values)) * 100, 2)


def _portfolio_latest(rows: list[dict[str, Any]], key: str, count: int) -> float:
    selected = sorted(rows, key=lambda row: int(_num(row.get("rank"))))[:count]
    values = _values(selected, key)
    return _avg(values)


def _compounded_curve(values: list[float]) -> list[dict[str, Any]]:
    equity = 1.0
    curve = []
    for step, value in enumerate(values, start=1):
        equity *= 1 + value / 100
        curve.append(
            {
                "step": step,
                "equity": round(equity, 6),
                "compounded_return_pct": round((equity - 1) * 100, 2),
            }
        )
    return curve


def _compounded_portfolio_curve(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    equity = 1.0
    curve = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("scan_id") or row.get("entry_time") or ""), []).append(row)
    for step, key in enumerate(sorted(grouped), start=1):
        selected = sorted(grouped[key], key=lambda row: int(_num(row.get("rank"))))[:count]
        values = _values(selected, "close_return_pct")
        if not values:
            continue
        basket = sum(values) / len(values)
        equity *= 1 + basket / 100
        curve.append(
            {
                "step": step,
                "basket_size": len(values),
                "basket_return_pct": round(basket, 2),
                "equity": round(equity, 6),
                "compounded_return_pct": round((equity - 1) * 100, 2),
            }
        )
    return curve


def _best_day(rows: list[dict[str, Any]], *, best: bool) -> dict[str, Any] | None:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        date = str(row.get("entry_time", ""))[:10]
        value = row.get("close_return_pct")
        if value not in {None, ""}:
            grouped.setdefault(date, []).append(_num(value))
    if not grouped:
        return None
    day_returns = [
        {"date": date, "avg_close_return_pct": _avg(values)} for date, values in grouped.items()
    ]
    return max(day_returns, key=lambda row: _num(row["avg_close_return_pct"])) if best else min(
        day_returns, key=lambda row: _num(row["avg_close_return_pct"])
    )


def _extreme(rows: list[dict[str, Any]], key: str, *, best: bool) -> dict[str, Any] | None:
    candidates = [row for row in rows if row.get(key) not in {None, ""}]
    if not candidates:
        return None
    return max(candidates, key=lambda row: _num(row.get(key))) if best else min(
        candidates, key=lambda row: _num(row.get(key))
    )


def _normalize_entry_time(date_value: str, raw: str) -> str:
    if "T" in raw:
        return raw
    if len(raw) == 5 and ":" in raw:
        return f"{date_value}T{raw}:00"
    if raw:
        return raw
    raise SnapshotValidationError("entry_time is required")


def _recommendation_time_for_date(rec: dict[str, Any], date_value: str) -> str:
    for key in ("timestamp", "recorded_at", "source_as_of_timestamp"):
        value = str(rec.get(key) or "")
        if value.startswith(date_value):
            return value
    return ""


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotValidationError(f"Invalid timestamp: {value}") from exc
    return parsed.replace(tzinfo=None)


def _get(row: dict[str, Any], *names: str) -> Any:
    lowered = {key.strip().lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _required_float(value: Any, name: str) -> float:
    parsed = _optional_float(value)
    if parsed is None:
        raise SnapshotValidationError(f"{name} is required")
    return parsed


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    text = str(value).replace("$", "").replace(",", "").replace("%", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise SnapshotValidationError(f"Expected numeric value, got {value!r}") from exc


def _format_optional(value: float | None) -> str:
    return "" if value is None else str(_round(value))


def _round(value: float) -> float:
    return round(float(value), 6)


def _bool_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return "true"
    if text in {"false", "f", "0", "no", "n"}:
        return "false"
    return ""


def _num(value: Any) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
