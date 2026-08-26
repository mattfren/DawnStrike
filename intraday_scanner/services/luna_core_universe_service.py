"""Point-in-time Luna core-universe contract.

The core universe is deliberately source-backed.  This module accepts governed
manifest artifacts (JSON objects or paths), unions the S&P 500 and Nasdaq-100
membership sets, and fails closed when the source is absent, incomplete, or
stale.  It does not turn a broad Nasdaq listing file into Nasdaq-100 membership.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SNAPSHOT_COLUMNS
from intraday_scanner.providers.alpaca_provider import AlpacaProvider

SCHEMA_VERSION = "dawnstrike.luna.core_universe.v1"
CORE_INDEXES = ("S&P 500", "Nasdaq-100")
DEFAULT_MAX_AGE_DAYS = 31


def canonical_symbol(value: Any) -> str:
    """Return a conservative ticker spelling used for membership deduplication."""

    return str(value or "").strip().upper()


def build_core_universe_contract(
    manifests: Iterable[dict[str, Any] | str | Path] | dict[str, Any] | str | Path | None,
    *,
    observed_at: str | datetime | None = None,
    effective_date: str | None = None,
    market_date: str | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Build an auditable union contract without inventing missing membership.

    A manifest may contain ``members`` or ``records`` and each member may use
    ``symbol``/``ticker`` plus ``index_memberships`` (or a single ``index``).
    The source identity and dates are copied into every member's metadata.
    """

    now = _timestamp(observed_at) if observed_at is not None else datetime.now(timezone.utc)
    candidates = _manifest_list(manifests)
    loaded: list[dict[str, Any]] = []
    source_errors: list[str] = []
    for candidate in candidates:
        try:
            item = _read_manifest(candidate)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            source_errors.append(f"manifest_unreadable:{exc}")
            continue
        if item:
            loaded.append(item)
    source_ids = sorted({str(m.get("source_id") or m.get("id") or "").strip() for m in loaded if str(m.get("source_id") or m.get("id") or "").strip()})
    source_uris = sorted({str(m.get("source_uri") or m.get("uri") or m.get("url") or "").strip() for m in loaded if str(m.get("source_uri") or m.get("uri") or m.get("url") or "").strip()})
    requested_date = _date_text(market_date or effective_date)
    members_by_symbol: dict[str, dict[str, Any]] = {}
    index_symbols: dict[str, set[str]] = {index: set() for index in CORE_INDEXES}
    index_expected: dict[str, int | None] = {index: None for index in CORE_INDEXES}
    index_declared_complete: set[str] = set()
    index_dates_ok: dict[str, bool] = {index: True for index in CORE_INDEXES}
    index_seen: set[str] = set()
    observed_dates: list[datetime] = []
    effective_dates: list[str] = []
    complete = bool(loaded) and not source_errors
    stale = False
    for manifest in loaded:
        source_id = str(manifest.get("source_id") or manifest.get("id") or "").strip()
        source_uri = str(manifest.get("source_uri") or manifest.get("uri") or manifest.get("url") or "").strip()
        observed = _parse_datetime(manifest.get("observed_at") or manifest.get("retrieved_at"))
        effective = _date_text(manifest.get("effective_date") or manifest.get("as_of_date"))
        if not source_id or not source_uri or observed is None or effective is None:
            complete = False
        if observed is not None:
            observed_dates.append(observed)
            stale = stale or (now - observed).total_seconds() > max(0, max_age_days) * 86400
        if effective:
            effective_dates.append(effective)
        if requested_date and (effective is None or effective > requested_date):
            complete = False
        explicit = str(manifest.get("completeness_verdict") or "").upper()
        if explicit and explicit not in {"COMPLETE", "PASS", "READY"}:
            complete = False
        records = manifest.get("members")
        if not isinstance(records, list):
            records = manifest.get("records")
        if not isinstance(records, list) and isinstance(manifest.get("index_memberships"), dict):
            records = []
            for index, symbols in manifest["index_memberships"].items():
                if isinstance(symbols, (list, tuple, set)):
                    records.extend(
                        {"symbol": symbol, "index_memberships": [index]}
                        for symbol in symbols
                    )
        if not isinstance(records, list):
            complete = False
            continue
        manifest_index = manifest.get("index_name") or manifest.get("index")
        declared = manifest.get("expected_counts")
        if isinstance(declared, dict):
            for raw_index, count in declared.items():
                index = _index_name(raw_index)
                parsed_count = _positive_int(count)
                if index and parsed_count is not None:
                    index_expected[index] = parsed_count
                    index_seen.add(index)
        manifest_expected = _positive_int(manifest.get("expected_count"))
        manifest_index_name = _index_name(manifest_index)
        equivalent = manifest.get("completeness_by_index") or manifest.get("index_completeness")
        if isinstance(equivalent, dict):
            for raw_index, verdict in equivalent.items():
                if _index_name(raw_index) and str(verdict).upper() in {"COMPLETE", "PASS", "READY"}:
                    index_declared_complete.add(_index_name(raw_index))
        if manifest_index_name and explicit in {"COMPLETE", "PASS", "READY"}:
            index_declared_complete.add(manifest_index_name)
        if manifest_index_name and manifest_expected is not None:
            index_expected[manifest_index_name] = manifest_expected
            index_seen.add(manifest_index_name)
        for record in records:
            if not isinstance(record, dict):
                complete = False
                continue
            symbol = canonical_symbol(record.get("symbol") or record.get("ticker"))
            indexes = record.get("index_memberships")
            if isinstance(indexes, str):
                indexes = [indexes]
            if not isinstance(indexes, list):
                indexes = [record.get("index") or manifest_index] if (record.get("index") or manifest_index) else []
            normalized_indexes = sorted({_index_name(item) for item in indexes if _index_name(item)})
            if not symbol or not normalized_indexes:
                complete = False
                continue
            normalized_indexes = [item for item in normalized_indexes if item in CORE_INDEXES]
            if not normalized_indexes:
                continue
            row = members_by_symbol.setdefault(symbol, {"symbol": symbol, "index_memberships": [], "sources": []})
            if requested_date:
                valid_from = _date_text(record.get("valid_from") or effective)
                valid_to = _date_text(record.get("valid_to"))
                if valid_from is None or valid_from > requested_date or (valid_to and requested_date > valid_to):
                    complete = False
                    for index in normalized_indexes:
                        index_dates_ok[index] = False
                    continue
            row["index_memberships"] = sorted(set(row["index_memberships"]) | set(normalized_indexes))
            row["sources"] = sorted(set(row["sources"]) | {value for value in (source_id, source_uri) if value})
            row["observed_at"] = max(str(row.get("observed_at") or ""), observed.isoformat() if observed else "")
            row["effective_date"] = max(str(row.get("effective_date") or ""), effective or "")
            for index in normalized_indexes:
                index_symbols[index].add(symbol)
                index_seen.add(index)
    if effective_date:
        requested_effective = _date_text(effective_date)
        if requested_effective is None:
            complete = False
        else:
            effective_dates.append(requested_effective)
    index_verdicts: dict[str, dict[str, Any]] = {}
    for index in CORE_INDEXES:
        expected = index_expected[index]
        observed = len(index_symbols[index])
        has_index = index in index_seen or observed > 0
        count_complete = (
            expected is not None and observed == expected
        ) or (expected is None and index in index_declared_complete and observed > 0)
        freshness = "STALE" if stale else "FRESH" if observed_dates else "UNKNOWN"
        index_complete = has_index and count_complete and index_dates_ok[index] and freshness == "FRESH"
        index_verdicts[index] = {
            "status": "READY" if index_complete else "DATA_UNAVAILABLE",
            "expected_count": expected,
            "observed_unique_count": observed,
            "count_verdict": "PASS" if count_complete else "FAIL",
            "completeness_basis": "declared_source_complete" if expected is None and count_complete else "expected_count",
            "freshness_verdict": freshness,
            "effective_date_verdict": "PASS" if index_dates_ok[index] and requested_date else "UNKNOWN",
            "completeness_verdict": "COMPLETE" if index_complete else "INCOMPLETE",
        }
    observed_text = max((item.isoformat() for item in observed_dates), default=None)
    effective_text = max(effective_dates, default=None)
    freshness_verdict = "STALE" if stale else "FRESH" if observed_dates else "UNKNOWN"
    all_indexes_complete = all(row["status"] == "READY" for row in index_verdicts.values())
    completeness_verdict = "COMPLETE" if complete and members_by_symbol and all_indexes_complete else "INCOMPLETE"
    status = "READY" if completeness_verdict == "COMPLETE" and freshness_verdict == "FRESH" else "DATA_UNAVAILABLE"
    content = {
        "schema_version": SCHEMA_VERSION,
        "effective_date": effective_text,
        "observed_at": observed_text,
        "source_ids": source_ids,
        "source_uris": source_uris,
        "source_id": source_ids[0] if len(source_ids) == 1 else None,
        "source_uri": source_uris[0] if len(source_uris) == 1 else None,
        "members": sorted(members_by_symbol.values(), key=lambda row: row["symbol"]),
        "membership_count": len(members_by_symbol),
        "index_verdicts": index_verdicts,
        "requested_market_date": requested_date,
        "completeness_verdict": completeness_verdict,
        "freshness_verdict": freshness_verdict,
        "completeness": completeness_verdict,
        "freshness": freshness_verdict,
        "observed_date": observed_text[:10] if observed_text else None,
        "status": status,
        "reason": (
            ""
            if status == "READY"
            else "DATA_UNAVAILABLE: " + ";".join(source_errors or _unavailable_reasons(loaded, complete, stale, bool(members_by_symbol)))
        ),
        "research_only": True,
        "broker_execution": "disabled",
        "missing_truth_is_zero": False,
    }
    content["content_hash_sha256"] = _hash(content)
    content["content_hash"] = content["content_hash_sha256"]
    content["contract_id"] = "luna-core-" + content["content_hash_sha256"][:24]
    content["universe_id"] = content["contract_id"]
    return content


def read_core_universe_manifest(path: str | Path) -> dict[str, Any]:
    """Read one governed JSON manifest; callers still receive DATA_UNAVAILABLE."""

    return _read_manifest(path)


def write_core_universe_contract(contract: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def discover_core_universe_rows(
    contract: dict[str, Any],
    *,
    config: ScannerConfig,
    provider: Any | None = None,
    max_symbols: int = 600,
) -> dict[str, Any]:
    """Collect current read-only snapshots for READY core members.

    Discovery failure is returned as a lane-local blocker; callers retain the
    existing mover rows.  No row is synthesized from membership alone.
    """

    if str(contract.get("status") or "") != "READY":
        return {"status": "DATA_UNAVAILABLE", "rows": [], "reason": str(contract.get("reason") or "core universe contract unavailable"), "requested_count": 0, "returned_count": 0}
    members = list(contract.get("members") or [])[: max(int(max_symbols), 0)]
    symbols = [canonical_symbol(row.get("symbol") or row.get("ticker")) for row in members]
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
        return {"status": "DATA_UNAVAILABLE", "rows": [], "reason": "core universe has no members", "requested_count": 0, "returned_count": 0}
    active_provider = provider or AlpacaProvider(config)
    try:
        if hasattr(active_provider, "validate_credentials"):
            active_provider.validate_credentials()
        snapshots = active_provider.get_premarket_snapshot(symbols, config)
    except (DataProviderError, OSError, TypeError, ValueError) as exc:
        return {"status": "BLOCKED_EXTERNAL", "rows": [], "reason": str(exc), "requested_count": len(symbols), "returned_count": 0}
    memberships = {canonical_symbol(row.get("symbol") or row.get("ticker")): list(row.get("index_memberships") or []) for row in members}
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots or []:
        row = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
        ticker = canonical_symbol(row.get("ticker"))
        if ticker not in memberships:
            continue
        row["discovery_context"] = "luna_core:" + ",".join(memberships[ticker])
        row["universe_lane"] = "core"
        row["core_universe_memberships"] = memberships[ticker]
        rows.append(row)
    return {
        "status": "READY" if rows else "DATA_UNAVAILABLE",
        "rows": rows,
        "reason": "" if rows else "core snapshot provider returned no usable rows",
        "requested_count": len(symbols),
        "returned_count": len(rows),
    }


def merge_core_universe_rows(
    mover_rows: Iterable[dict[str, Any]], core_rows: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge lanes by ticker while retaining mover precedence and core metadata."""

    merged: dict[str, dict[str, Any]] = {}
    for raw in [*(mover_rows or []), *(core_rows or [])]:
        row = dict(raw)
        ticker = canonical_symbol(row.get("ticker") or row.get("symbol"))
        if not ticker:
            continue
        row["ticker"] = ticker
        if ticker not in merged:
            merged[ticker] = row
            continue
        current = merged[ticker]
        if row.get("core_universe_memberships"):
            current["core_universe_memberships"] = sorted(set(current.get("core_universe_memberships") or []) | set(row["core_universe_memberships"]))
            current["universe_lane"] = "mover+core"
            current["discovery_context"] = ";".join(filter(None, {str(current.get("discovery_context") or ""), "luna_core:" + ",".join(current["core_universe_memberships"])}))
    return list(merged.values())


def rank_core_universe_rows(
    rows: Iterable[dict[str, Any]], *, max_rows: int = 100
) -> list[dict[str, Any]]:
    """Cheap core-lane eligibility/rank independent of mover gap predicates."""

    eligible: list[dict[str, Any]] = []
    for raw in rows or []:
        row = dict(raw)
        try:
            price = float(row.get("premarket_price") or 0)
            volume = float(row.get("premarket_volume") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or volume <= 0 or row.get("stale_data_flag"):
            continue
        row["universe_lane"] = "core"
        row["core_lane_eligible"] = True
        row["core_lane_score"] = round(float(row.get("dollar_volume") or price * volume), 2)
        eligible.append(row)
    return sorted(
        eligible,
        key=lambda row: (float(row.get("core_lane_score") or 0), canonical_symbol(row.get("ticker"))),
        reverse=True,
    )[: max(int(max_rows), 0)]


def write_snapshot_rows(rows: Iterable[dict[str, Any]], output_path: str | Path) -> Path:
    import csv

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in SNAPSHOT_COLUMNS})
    return path


def _manifest_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, Path, dict)):
        return [value]
    return list(value) if isinstance(value, Iterable) else []


def _read_manifest(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    raw = Path(value).read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("universe manifest must be a JSON object")
    return parsed


def _index_name(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "").replace("&", "and")
    if text in {"s&p500", "sp500", "sandp500"}:
        return "S&P 500"
    if text in {"nasdaq-100", "nasdaq 100", "ndx", "nasdaq100"}:
        return "Nasdaq-100"
    return ""


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _timestamp(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else _parse_datetime(value)
    return (parsed or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _date_text(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat() if value else None
    except ValueError:
        return None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _unavailable_reasons(loaded: list[dict[str, Any]], complete: bool, stale: bool, has_members: bool) -> list[str]:
    reasons: list[str] = []
    if not loaded:
        reasons.append("manifest_absent")
    if not has_members:
        reasons.append("membership_absent")
    if not complete:
        reasons.append("completeness_failed")
    if stale:
        reasons.append("stale_manifest")
    return reasons or ["unavailable"]


def _hash(value: dict[str, Any]) -> str:
    payload = {key: value[key] for key in sorted(value) if key != "content_hash_sha256"}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


__all__ = ["CORE_INDEXES", "SCHEMA_VERSION", "build_core_universe_contract", "canonical_symbol", "discover_core_universe_rows", "merge_core_universe_rows", "rank_core_universe_rows", "read_core_universe_manifest", "write_core_universe_contract", "write_snapshot_rows"]
