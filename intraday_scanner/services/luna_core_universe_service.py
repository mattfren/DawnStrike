"""Point-in-time Luna core-universe contract.

The core universe is deliberately source-backed.  This module accepts governed
manifest artifacts (JSON objects or paths), unions the S&P 500 and Nasdaq-100
membership sets, and fails closed when the source is absent, incomplete, or
stale.  It does not turn a broad Nasdaq listing file into Nasdaq-100 membership.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError
from intraday_scanner.models import SNAPSHOT_COLUMNS
from intraday_scanner.providers.alpaca_provider import AlpacaProvider

SCHEMA_VERSION = "dawnstrike.luna.core_universe.v1"
CORE_INDEXES = ("S&P 500", "Nasdaq-100")
DEFAULT_MAX_AGE_DAYS = 31
MIN_PRODUCTION_COUNTS = {"S&P 500": 503, "Nasdaq-100": 100}
_SYMBOL_PATTERN = __import__("re").compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


def build_core_universe_contract(
    manifests: Iterable[dict[str, Any] | str | Path] | dict[str, Any] | str | Path | None,
    *,
    observed_at: str | datetime | None = None,
    effective_date: str | None = None,
    market_date: str | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    allow_test_override: bool = False,
) -> dict[str, Any]:
    """Build a source- and point-in-time-verifiable core universe.

    Production manifests must carry a digest of the raw provider artifact and
    a digest of their canonical member set.  Completeness is derived from
    records and expected counts; a provider's self-declared ``READY`` value is
    never sufficient.  ``allow_test_override`` is intentionally explicit and
    is for tests only; manifest content cannot opt itself out of production
    validation.
    """

    now = _timestamp(observed_at) if observed_at is not None else datetime.now(timezone.utc)
    candidates = _manifest_list(manifests)
    loaded: list[dict[str, Any]] = []
    errors: list[str] = []
    for candidate in candidates:
        try:
            items = _read_manifest_entries(candidate)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"manifest_unreadable:{exc}")
            continue
        if items:
            loaded.extend(items)
        else:
            errors.append("manifest_empty")

    requested_date = _date_text(market_date or effective_date)
    if (market_date or effective_date) and requested_date is None:
        errors.append("invalid_market_date")
    members: dict[str, dict[str, Any]] = {}
    per_index: dict[str, set[str]] = {index: set() for index in CORE_INDEXES}
    expected: dict[str, int | None] = {index: None for index in CORE_INDEXES}
    expected_conflicts: set[str] = set()
    index_seen: set[str] = set()
    index_date_failures: set[str] = set()
    observed_values: list[datetime] = []
    effective_values: list[str] = []
    source_artifacts: list[dict[str, Any]] = []
    provider_mapping: dict[str, tuple[str, str]] = {}
    production = not allow_test_override

    for manifest_number, manifest in enumerate(loaded, start=1):
        source_id = str(manifest.get("source_id") or manifest.get("id") or "").strip()
        source_uri = str(
            manifest.get("source_uri") or manifest.get("uri") or manifest.get("url") or ""
        ).strip()
        manifest_errors: list[str] = []
        if not source_id:
            manifest_errors.append("source_id_missing")
        if not source_uri:
            manifest_errors.append("source_uri_missing")
        observed = _parse_datetime(manifest.get("observed_at") or manifest.get("retrieved_at"))
        effective = _date_text(manifest.get("effective_date") or manifest.get("as_of_date"))
        if observed is None:
            manifest_errors.append("observed_at_missing_or_invalid")
        else:
            observed_values.append(observed)
            if observed > now:
                manifest_errors.append("future_observed_at")
            if (now - observed).total_seconds() > max(0, max_age_days) * 86400:
                manifest_errors.append("stale_manifest")
        if effective is None:
            manifest_errors.append("effective_date_missing_or_invalid")
        else:
            effective_values.append(effective)
            if requested_date and effective > requested_date:
                manifest_errors.append("effective_date_after_market_date")

        records = manifest.get("members")
        if not isinstance(records, list):
            records = manifest.get("records")
        manifest_index = manifest.get("index_name") or manifest.get("index")
        if not isinstance(records, list) and isinstance(manifest.get("index_memberships"), dict):
            records = [
                {"symbol": symbol, "index_memberships": [index]}
                for index, symbols in manifest["index_memberships"].items()
                if isinstance(symbols, (list, tuple, set))
                for symbol in symbols
            ]
        if not isinstance(records, list):
            manifest_errors.append("members_missing_or_invalid")
            records = []

        # Expected counts are mandatory, positive, and stable across sources.
        declared_counts = manifest.get("expected_counts")
        count_pairs: list[tuple[Any, Any]] = (
            list(declared_counts.items()) if isinstance(declared_counts, dict) else []
        )
        if manifest_index:
            count_pairs.append((manifest_index, manifest.get("expected_count")))
        for raw_index, raw_count in count_pairs:
            index = _index_name(raw_index)
            count = _strict_positive_int(raw_count)
            if not index:
                manifest_errors.append(f"unknown_index:{raw_index}")
                continue
            index_seen.add(index)
            if count is None:
                manifest_errors.append(f"expected_count_invalid:{index}")
                continue
            if expected[index] is not None and expected[index] != count:
                expected_conflicts.add(index)
            expected[index] = count
            if production and count < MIN_PRODUCTION_COUNTS[index]:
                manifest_errors.append(f"expected_count_below_production_minimum:{index}")

        explicit = str(manifest.get("completeness_verdict") or "").upper()
        if explicit and explicit not in {"COMPLETE", "PASS", "READY"}:
            manifest_errors.append("source_completeness_failed")

        raw_hashes, raw_errors = _validate_raw_artifacts(manifest)
        raw_hash = raw_hashes[0] if len(raw_hashes) == 1 else ""
        if production:
            manifest_errors.extend(raw_errors)

        local_members: list[dict[str, Any]] = []
        local_pairs: set[tuple[str, str]] = set()
        for record_number, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                manifest_errors.append(f"member_not_object:{record_number}")
                continue
            symbol = canonical_symbol(record.get("symbol") or record.get("ticker"))
            if not symbol or not _SYMBOL_PATTERN.fullmatch(symbol):
                manifest_errors.append(f"invalid_symbol:{record_number}")
                continue
            indexes = record.get("index_memberships")
            if isinstance(indexes, str):
                indexes = [indexes]
            if not isinstance(indexes, list):
                indexes = (
                    [record.get("index") or manifest_index]
                    if (record.get("index") or manifest_index)
                    else []
                )
            normalized: list[str] = []
            for value in indexes:
                index = _index_name(value)
                if not index:
                    manifest_errors.append(f"unknown_or_unmapped_index:{value}")
                elif index not in normalized:
                    normalized.append(index)
            if not normalized:
                manifest_errors.append(f"member_index_missing:{symbol}")
                continue
            provider_symbol = canonical_symbol(
                record.get("provider_symbol") or record.get("mapped_symbol") or symbol
            )
            asset_class = (
                str(
                    record.get("asset_class")
                    or record.get("security_type")
                    or record.get("class_share")
                    or ""
                )
                .strip()
                .lower()
            )
            if production and (
                not provider_symbol or not _SYMBOL_PATTERN.fullmatch(provider_symbol)
            ):
                manifest_errors.append(f"provider_symbol_unmapped:{symbol}")
            if production and asset_class not in {"common", "common_stock", "ordinary", "equity"}:
                manifest_errors.append(f"class_share_invalid:{symbol}")
            valid_from = _date_text(record.get("valid_from") or effective)
            valid_to = _date_text(record.get("valid_to"))
            if requested_date and (
                valid_from is None
                or valid_from > requested_date
                or (valid_to is not None and requested_date > valid_to)
            ):
                manifest_errors.append(f"member_not_valid_for_market_date:{symbol}")
                index_date_failures.update(normalized)
            if production and valid_from is None:
                manifest_errors.append(f"member_validity_missing:{symbol}")
            for index in normalized:
                pair = (index, symbol)
                if pair in local_pairs:
                    manifest_errors.append(f"duplicate_member:{index}:{symbol}")
                local_pairs.add(pair)
                local_members.append(
                    {
                        "symbol": symbol,
                        "provider_symbol": provider_symbol,
                        "asset_class": asset_class or "common_stock",
                        "index": index,
                        "valid_from": valid_from,
                        "valid_to": valid_to,
                    }
                )
                existing_mapping = provider_mapping.get(symbol)
                mapping = (provider_symbol, asset_class or "common_stock")
                if existing_mapping is not None and existing_mapping != mapping:
                    errors.append(f"provider_mapping_collision:{symbol}")
                provider_mapping[symbol] = mapping

        declared_member_hash = str(
            manifest.get("canonical_member_set_hash_sha256")
            or manifest.get("member_set_hash_sha256")
            or manifest.get("canonical_members_hash_sha256")
            or manifest.get("canonical_member_set_hash")
            or manifest.get("member_set_hash")
            or ""
        ).lower()
        computed_member_hash = _canonical_member_hash(local_members)
        if production:
            if not _valid_digest(declared_member_hash):
                manifest_errors.append("canonical_member_set_hash_missing_or_invalid")
            elif declared_member_hash != computed_member_hash:
                manifest_errors.append("canonical_member_set_hash_mismatch")
            if not _has_reconstitution_lineage(
                manifest,
                effective_date=effective,
                artifact_hashes=raw_hashes,
                member_hash=computed_member_hash,
            ):
                manifest_errors.append("reconstitution_lineage_invalid")
        source_artifacts.append(
            {
                "source_id": source_id,
                "source_uri": source_uri,
                "raw_artifact_sha256": raw_hash,
                "raw_artifact_hashes": sorted(raw_hashes),
                "canonical_member_set_hash_sha256": computed_member_hash,
                "declared_canonical_member_set_hash_sha256": declared_member_hash or None,
                "error_codes": sorted(set(manifest_errors)),
            }
        )
        errors.extend(f"manifest_{manifest_number}:{item}" for item in manifest_errors)

        # Only records valid for the requested date contribute to the contract.
        for item in local_members:
            if requested_date and (
                item["valid_from"] is None
                or item["valid_from"] > requested_date
                or (item["valid_to"] and requested_date > item["valid_to"])
            ):
                continue
            symbol = item["symbol"]
            row = members.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "provider_symbol": item["provider_symbol"],
                    "asset_class": item["asset_class"],
                    "index_memberships": [],
                    "sources": [],
                },
            )
            row["index_memberships"] = sorted(set(row["index_memberships"]) | {item["index"]})
            row["sources"] = sorted(
                set(row["sources"]) | {value for value in (source_id, source_uri) if value}
            )
            row["valid_from"] = max(
                str(row.get("valid_from") or ""), str(item.get("valid_from") or "")
            )
            row["valid_to"] = max(str(row.get("valid_to") or ""), str(item.get("valid_to") or ""))
            if symbol in per_index[item["index"]]:
                errors.append(f"duplicate_member_global:{item['index']}:{symbol}")
            per_index[item["index"]].add(symbol)

    if expected_conflicts:
        errors.extend(f"expected_count_conflict:{index}" for index in sorted(expected_conflicts))
    if production and len(loaded) == 0:
        errors.append("manifest_absent")
    # Every production index needs a positive expected count and an actual set.
    for index in CORE_INDEXES:
        if expected[index] is None:
            errors.append(f"expected_count_missing:{index}")
        elif len(per_index[index]) != expected[index]:
            errors.append(f"member_count_mismatch:{index}")
        if not per_index[index]:
            errors.append(f"membership_absent:{index}")
    if index_date_failures:
        errors.extend(f"effective_validity_failed:{index}" for index in sorted(index_date_failures))

    freshness = (
        "STALE"
        if any(
            (now - value).total_seconds() > max(0, max_age_days) * 86400
            for value in observed_values
        )
        else "FRESH"
        if observed_values and not any(value > now for value in observed_values)
        else "UNKNOWN"
    )
    index_verdicts: dict[str, dict[str, Any]] = {}
    for index in CORE_INDEXES:
        index_errors = [error for error in errors if f":{index}" in error or error.endswith(index)]
        ready = (
            bool(per_index[index])
            and expected[index] is not None
            and len(per_index[index]) == expected[index]
            and freshness == "FRESH"
            and not any(index in error for error in errors)
        )
        index_verdicts[index] = {
            "status": "READY" if ready else "DATA_UNAVAILABLE",
            "expected_count": expected[index],
            "observed_unique_count": len(per_index[index]),
            "count_verdict": "PASS"
            if expected[index] is not None and len(per_index[index]) == expected[index]
            else "FAIL",
            "freshness_verdict": freshness,
            "effective_date_verdict": "PASS"
            if requested_date and index not in index_date_failures
            else "UNKNOWN"
            if not requested_date
            else "FAIL",
            "completeness_verdict": "COMPLETE" if ready else "INCOMPLETE",
            "blockers": sorted(set(index_errors)),
        }
    status = (
        "READY"
        if loaded
        and not errors
        and all(item["status"] == "READY" for item in index_verdicts.values())
        else "DATA_UNAVAILABLE"
    )
    observed_text = max((item.isoformat() for item in observed_values), default=None)
    effective_text = max(effective_values, default=None)
    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "effective_date": effective_text,
        "observed_at": observed_text,
        "source_ids": sorted(
            {
                str(item.get("source_id") or "")
                for item in loaded
                if str(item.get("source_id") or "")
            }
        ),
        "source_uris": sorted(
            {
                str(item.get("source_uri") or item.get("uri") or item.get("url") or "")
                for item in loaded
                if str(item.get("source_uri") or item.get("uri") or item.get("url") or "")
            }
        ),
        "source_artifacts": source_artifacts,
        "raw_artifact_hashes": sorted(
            digest
            for item in source_artifacts
            for digest in (
                item.get("raw_artifact_hashes")
                or ([item["raw_artifact_sha256"]] if item.get("raw_artifact_sha256") else [])
            )
        ),
        "members": sorted(members.values(), key=lambda row: row["symbol"]),
        "membership_count": len(members),
        "index_verdicts": index_verdicts,
        "requested_market_date": requested_date,
        "completeness_verdict": "COMPLETE" if status == "READY" else "INCOMPLETE",
        "freshness_verdict": freshness,
        "status": status,
        "reason": ""
        if status == "READY"
        else "DATA_UNAVAILABLE: " + ";".join(sorted(set(errors)))
        if errors
        else "DATA_UNAVAILABLE: unavailable",
        "blockers": sorted(
            {
                error.split(":", 1)[1] if error.startswith("manifest_") and ":" in error else error
                for error in set(errors)
            }
        ),
        "research_only": True,
        "broker_execution": "disabled",
        "missing_truth_is_zero": False,
    }
    contract["canonical_member_set_hash_sha256"] = _canonical_member_hash(
        [
            {
                "symbol": row["symbol"],
                "provider_symbol": row.get("provider_symbol"),
                "asset_class": row.get("asset_class"),
                "index": index,
                "valid_from": row.get("valid_from"),
                "valid_to": row.get("valid_to"),
            }
            for row in contract["members"]
            for index in row["index_memberships"]
        ]
    )
    contract["content_hash_sha256"] = _hash(contract)
    contract["content_hash"] = contract["content_hash_sha256"]
    contract["contract_id"] = "luna-core-" + contract["content_hash_sha256"][:24]
    contract["universe_id"] = contract["contract_id"]
    return contract


def canonical_symbol(value: Any) -> str:
    """Return a conservative ticker spelling used for membership deduplication."""

    return str(value or "").strip().upper()


def _build_core_universe_contract_legacy(
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
    source_ids = sorted(
        {
            str(m.get("source_id") or m.get("id") or "").strip()
            for m in loaded
            if str(m.get("source_id") or m.get("id") or "").strip()
        }
    )
    source_uris = sorted(
        {
            str(m.get("source_uri") or m.get("uri") or m.get("url") or "").strip()
            for m in loaded
            if str(m.get("source_uri") or m.get("uri") or m.get("url") or "").strip()
        }
    )
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
        source_uri = str(
            manifest.get("source_uri") or manifest.get("uri") or manifest.get("url") or ""
        ).strip()
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
                        {"symbol": symbol, "index_memberships": [index]} for symbol in symbols
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
                indexes = (
                    [record.get("index") or manifest_index]
                    if (record.get("index") or manifest_index)
                    else []
                )
            normalized_indexes = sorted(
                {_index_name(item) for item in indexes if _index_name(item)}
            )
            if not symbol or not normalized_indexes:
                complete = False
                continue
            normalized_indexes = [item for item in normalized_indexes if item in CORE_INDEXES]
            if not normalized_indexes:
                continue
            row = members_by_symbol.setdefault(
                symbol, {"symbol": symbol, "index_memberships": [], "sources": []}
            )
            if requested_date:
                valid_from = _date_text(record.get("valid_from") or effective)
                valid_to = _date_text(record.get("valid_to"))
                if (
                    valid_from is None
                    or valid_from > requested_date
                    or (valid_to and requested_date > valid_to)
                ):
                    complete = False
                    for index in normalized_indexes:
                        index_dates_ok[index] = False
                    continue
            row["index_memberships"] = sorted(
                set(row["index_memberships"]) | set(normalized_indexes)
            )
            row["sources"] = sorted(
                set(row["sources"]) | {value for value in (source_id, source_uri) if value}
            )
            row["observed_at"] = max(
                str(row.get("observed_at") or ""), observed.isoformat() if observed else ""
            )
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
        count_complete = (expected is not None and observed == expected) or (
            expected is None and index in index_declared_complete and observed > 0
        )
        freshness = "STALE" if stale else "FRESH" if observed_dates else "UNKNOWN"
        index_complete = (
            has_index and count_complete and index_dates_ok[index] and freshness == "FRESH"
        )
        index_verdicts[index] = {
            "status": "READY" if index_complete else "DATA_UNAVAILABLE",
            "expected_count": expected,
            "observed_unique_count": observed,
            "count_verdict": "PASS" if count_complete else "FAIL",
            "completeness_basis": "declared_source_complete"
            if expected is None and count_complete
            else "expected_count",
            "freshness_verdict": freshness,
            "effective_date_verdict": "PASS"
            if index_dates_ok[index] and requested_date
            else "UNKNOWN",
            "completeness_verdict": "COMPLETE" if index_complete else "INCOMPLETE",
        }
    observed_text = max((item.isoformat() for item in observed_dates), default=None)
    effective_text = max(effective_dates, default=None)
    freshness_verdict = "STALE" if stale else "FRESH" if observed_dates else "UNKNOWN"
    all_indexes_complete = all(row["status"] == "READY" for row in index_verdicts.values())
    completeness_verdict = (
        "COMPLETE" if complete and members_by_symbol and all_indexes_complete else "INCOMPLETE"
    )
    status = (
        "READY"
        if completeness_verdict == "COMPLETE" and freshness_verdict == "FRESH"
        else "DATA_UNAVAILABLE"
    )
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
            else "DATA_UNAVAILABLE: "
            + ";".join(
                source_errors
                or _unavailable_reasons(loaded, complete, stale, bool(members_by_symbol))
            )
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
    batch_size: int = 50,
) -> dict[str, Any]:
    """Collect current read-only snapshots for READY core members.

    Discovery failure is returned as a lane-local blocker; callers retain the
    existing mover rows.  No row is synthesized from membership alone.
    """

    if str(contract.get("status") or "") != "READY":
        return {
            "status": "DATA_UNAVAILABLE",
            "rows": [],
            "reason": str(contract.get("reason") or "core universe contract unavailable"),
            "requested_count": 0,
            "returned_count": 0,
        }
    all_members = list(contract.get("members") or [])
    if len(all_members) > max(int(max_symbols), 0):
        return {
            "status": "INCOMPLETE",
            "rows": [],
            "reason": "core universe exceeds bounded discovery capacity",
            "requested_count": len(all_members),
            "returned_count": 0,
            "coverage_receipts": [],
        }
    members = all_members
    symbols = [canonical_symbol(row.get("symbol") or row.get("ticker")) for row in members]
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
        return {
            "status": "DATA_UNAVAILABLE",
            "rows": [],
            "reason": "core universe has no members",
            "requested_count": 0,
            "returned_count": 0,
        }
    active_provider = provider or AlpacaProvider(config)
    memberships = {
        canonical_symbol(row.get("symbol") or row.get("ticker")): list(
            row.get("index_memberships") or []
        )
        for row in members
    }
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    size = max(int(batch_size), 1)
    authenticated = False
    try:
        if hasattr(active_provider, "validate_credentials"):
            active_provider.validate_credentials()
            authenticated = True
        for batch_number, start in enumerate(range(0, len(symbols), size), start=1):
            requested = symbols[start : start + size]
            snapshots = active_provider.get_premarket_snapshot(requested, config)
            if isinstance(snapshots, dict):
                snapshots = [
                    {"ticker": key, **value}
                    for key, value in snapshots.items()
                    if isinstance(value, dict)
                ]
            batch_rows: list[dict[str, Any]] = []
            for snapshot in snapshots or []:
                row = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
                ticker = canonical_symbol(row.get("ticker") or row.get("symbol"))
                batch_rows.append({**row, "ticker": ticker})
            returned = [str(row.get("ticker") or "") for row in batch_rows]
            duplicates = sorted(
                {ticker for ticker in returned if returned.count(ticker) > 1 and ticker}
            )
            unknown = sorted(set(returned) - set(requested))
            missing = sorted(set(requested) - set(returned))
            receipt = {
                "batch_number": batch_number,
                "requested_symbols": requested,
                "requested_count": len(requested),
                "returned_symbols": returned,
                "returned_count": len(returned),
                "missing_symbols": missing,
                "unknown_symbols": unknown,
                "duplicate_symbols": duplicates,
                "status": "READY"
                if not missing
                and not unknown
                and not duplicates
                and len(returned) == len(requested)
                else "INCOMPLETE",
            }
            receipts.append(receipt)
            if receipt["status"] != "READY":
                continue
            for row in batch_rows:
                ticker = str(row["ticker"])
                row["discovery_context"] = "luna_core:" + ",".join(memberships[ticker])
                row["universe_lane"] = "core"
                row["core_universe_memberships"] = memberships[ticker]
                # These statuses describe the authenticated snapshot receipt;
                # halt/SEC/corporate-action statuses are attached only by their
                # respective evidence collectors later in the cycle.
                row["source_quality_status"] = "VERIFIED" if authenticated else "UNKNOWN"
                row["freshness_status"] = "FRESH" if authenticated else "UNKNOWN"
                rows.append(row)
    except (DataProviderError, OSError, TypeError, ValueError) as exc:
        return {
            "status": "BLOCKED_EXTERNAL",
            "rows": [],
            "reason": str(exc),
            "requested_count": len(symbols),
            "returned_count": len(rows),
            "coverage_receipts": receipts,
        }
    complete = (
        len(rows) == len(symbols)
        and len({str(row.get("ticker")) for row in rows}) == len(symbols)
        and all(item["status"] == "READY" for item in receipts)
    )
    return {
        "status": "READY" if complete else "INCOMPLETE",
        "rows": rows,
        "reason": "" if complete else "core snapshot coverage incomplete; no READY claim",
        "requested_count": len(symbols),
        "returned_count": len(rows),
        "coverage_receipts": receipts,
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
            current["core_universe_memberships"] = sorted(
                set(current.get("core_universe_memberships") or [])
                | set(row["core_universe_memberships"])
            )
            current["universe_lane"] = "mover+core"
            current["discovery_context"] = ";".join(
                filter(
                    None,
                    {
                        str(current.get("discovery_context") or ""),
                        "luna_core:" + ",".join(current["core_universe_memberships"]),
                    },
                )
            )
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
        key=lambda row: (
            float(row.get("core_lane_score") or 0),
            canonical_symbol(row.get("ticker")),
        ),
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


def _read_manifest_entries(value: Any) -> list[dict[str, Any]]:
    """Read a manifest or an explicit wrapper of source manifests.

    Wrapper paths are useful for a derived index (for example a rebalance
    lineage): each child remains an independent source artifact and relative
    local artifact paths resolve beside the wrapper file.
    """

    base = Path(value).parent if isinstance(value, (str, Path)) else None
    parsed = _read_manifest(value)
    children = parsed.get("manifests")
    if not isinstance(children, list):
        children = [parsed]
    output: list[dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("manifest wrapper contains a non-object child")
        item = dict(child)
        if base is not None:
            for key in ("raw_artifact", "raw_artifact_path"):
                raw = item.get(key)
                if (
                    isinstance(raw, str)
                    and raw
                    and not Path(raw).is_absolute()
                    and not raw.startswith(("http://", "https://"))
                ):
                    item[key] = str((base / raw).resolve())
            for key in ("source_artifacts", "raw_artifacts"):
                entries = item.get(key)
                if isinstance(entries, list):
                    resolved: list[Any] = []
                    for entry in entries:
                        if not isinstance(entry, dict):
                            resolved.append(entry)
                            continue
                        copy = dict(entry)
                        for path_key in ("path", "file", "local_path"):
                            raw = copy.get(path_key)
                            if (
                                isinstance(raw, str)
                                and raw
                                and not Path(raw).is_absolute()
                                and not raw.startswith(("http://", "https://"))
                            ):
                                copy[path_key] = str((base / raw).resolve())
                        resolved.append(copy)
                    item[key] = resolved
        output.append(item)
    return output


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
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


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


def _strict_positive_int(value: Any) -> int | None:
    """Parse an actual positive integer; bools and fractional text are false."""

    if isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        if (
            not text
            or (text.startswith("+") and not text[1:].isdigit())
            or not text.lstrip("+").isdigit()
        ):
            return None
        parsed = int(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _valid_digest(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _has_reconstitution_lineage(
    manifest: dict[str, Any],
    *,
    effective_date: str | None,
    artifact_hashes: list[str],
    member_hash: str,
) -> bool:
    """Require a structured receipt tied to the exact validated inputs."""

    lineage = (
        manifest.get("reconstitution_lineage")
        or manifest.get("point_in_time_lineage")
        or manifest.get("reconstitution")
    )
    if not isinstance(lineage, dict):
        return False
    schema = str(lineage.get("schema_version") or lineage.get("schema") or "").strip()
    builder = str(
        lineage.get("builder_id")
        or lineage.get("transformation_id")
        or lineage.get("builder")
        or ""
    ).strip()
    lineage_effective = _date_text(lineage.get("effective_date") or lineage.get("as_of_date"))
    input_hashes = lineage.get("input_artifact_hashes") or lineage.get("artifact_hashes")
    lineage_member_hash = str(
        lineage.get("canonical_member_set_hash_sha256")
        or lineage.get("member_set_hash_sha256")
        or ""
    ).lower()
    return (
        bool(schema and builder and lineage_effective and lineage_effective == effective_date)
        and isinstance(input_hashes, list)
        and sorted(str(item).lower() for item in input_hashes) == sorted(artifact_hashes)
        and lineage_member_hash == member_hash
    )


def _validate_raw_artifact(manifest: dict[str, Any]) -> tuple[str, str | None]:
    """Validate a declared raw artifact digest and, where possible, its bytes."""

    declared = (
        str(
            manifest.get("raw_artifact_sha256")
            or manifest.get("raw_sha256")
            or manifest.get("raw_artifact_hash_sha256")
            or manifest.get("raw_artifact_hash")
            or ""
        )
        .strip()
        .lower()
    )
    artifact = manifest.get("raw_artifact") or manifest.get("raw_artifact_path")
    if isinstance(artifact, dict):
        declared = str(artifact.get("sha256") or declared).strip().lower()
        artifact = artifact.get("path") or artifact.get("file") or artifact.get("content")
    if not _valid_digest(declared):
        return declared, "raw_artifact_sha256_missing_or_invalid"
    if artifact and isinstance(artifact, (str, Path)):
        artifact_path = Path(artifact)
        if artifact_path.is_file():
            try:
                actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            except OSError as exc:
                return declared, f"raw_artifact_unreadable:{exc}"
            if actual != declared:
                return declared, "raw_artifact_sha256_mismatch"
        else:
            return declared, "raw_artifact_bytes_missing"
    else:
        content = manifest.get("raw_artifact_content")
        if isinstance(content, str):
            actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if actual != declared:
                return declared, "raw_artifact_sha256_mismatch"
        else:
            return declared, "raw_artifact_bytes_missing"
    return declared, None


def _validate_raw_artifacts(manifest: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate every raw artifact receipt, including rebalance lineages."""

    entries = manifest.get("source_artifacts") or manifest.get("raw_artifacts")
    if isinstance(entries, dict):
        entries = list(entries.values())
    if isinstance(entries, list):
        hashes: list[str] = []
        errors: list[str] = []
        for number, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                errors.append(f"raw_artifact_entry_invalid:{number}")
                continue
            digest = (
                str(
                    entry.get("sha256")
                    or entry.get("raw_artifact_sha256")
                    or entry.get("raw_artifact_hash")
                    or ""
                )
                .strip()
                .lower()
            )
            path = entry.get("path") or entry.get("file") or entry.get("local_path")
            content = entry.get("content")
            if not _valid_digest(digest):
                errors.append(f"raw_artifact_sha256_missing_or_invalid:{number}")
                continue
            if path:
                artifact_path = Path(path)
                if not artifact_path.is_file():
                    errors.append(f"raw_artifact_missing:{number}")
                    continue
                try:
                    actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                except OSError:
                    errors.append(f"raw_artifact_unreadable:{number}")
                    continue
                if actual != digest:
                    errors.append(f"raw_artifact_sha256_mismatch:{number}")
                    continue
            elif isinstance(content, str):
                if hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
                    errors.append(f"raw_artifact_sha256_mismatch:{number}")
                    continue
            else:
                errors.append(f"raw_artifact_bytes_missing:{number}")
                continue
            hashes.append(digest)
        if not entries:
            errors.append("raw_artifact_entries_missing")
        return sorted(hashes), errors
    digest, error = _validate_raw_artifact(manifest)
    return ([digest] if not error else []), ([error] if error else [])


def _canonical_member_hash(records: Iterable[dict[str, Any]]) -> str:
    canonical = []
    for record in records:
        canonical.append(
            {
                "symbol": canonical_symbol(record.get("symbol") or record.get("ticker")),
                "provider_symbol": canonical_symbol(
                    record.get("provider_symbol")
                    or record.get("mapped_symbol")
                    or record.get("symbol")
                    or record.get("ticker")
                ),
                "asset_class": str(
                    record.get("asset_class")
                    or record.get("security_type")
                    or record.get("class_share")
                    or "common_stock"
                )
                .strip()
                .lower(),
                "index": _index_name(record.get("index") or record.get("index_name"))
                or str(record.get("index") or ""),
                "valid_from": _date_text(record.get("valid_from")),
                "valid_to": _date_text(record.get("valid_to")),
            }
        )
    raw = json.dumps(
        sorted(canonical, key=lambda item: tuple(str(item[key]) for key in item)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _unavailable_reasons(
    loaded: list[dict[str, Any]], complete: bool, stale: bool, has_members: bool
) -> list[str]:
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


__all__ = [
    "CORE_INDEXES",
    "SCHEMA_VERSION",
    "build_core_universe_contract",
    "canonical_symbol",
    "discover_core_universe_rows",
    "merge_core_universe_rows",
    "rank_core_universe_rows",
    "read_core_universe_manifest",
    "write_core_universe_contract",
    "write_snapshot_rows",
]
