"""Point-in-time Luna core-universe contract.

The core universe is deliberately source-backed.  This module accepts governed
manifest artifacts (JSON objects or paths), unions the S&P 500 and Nasdaq-100
membership sets, and fails closed when the source is absent, incomplete, or
stale.  It does not turn a broad Nasdaq listing file into Nasdaq-100 membership.
"""

from __future__ import annotations

import ast
import hashlib
import html
import io
import json
import math
import re
import zipfile
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from defusedxml import ElementTree

from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import SNAPSHOT_COLUMNS
from intraday_scanner.providers.alpaca_provider import AlpacaProvider

SCHEMA_VERSION = "dawnstrike.luna.core_universe.v1"
ACTIVE_POINTER_SCHEMA_VERSION = "dawnstrike.luna.core_universe_active_pointer.v1"
CORE_INDEXES = ("S&P 500", "Nasdaq-100")
DEFAULT_MAX_AGE_DAYS = 31
MIN_PRODUCTION_COUNTS = {"S&P 500": 503, "Nasdaq-100": 100}
# A partial current snapshot may contribute research rows, but only after at
# least one member has an independently verified, fresh Alpaca observation.
# The threshold is deliberately small: it is a data-availability threshold,
# not a claim that the whole index was observed.  The resulting PARTIAL
# status and exact coverage counts remain visible to every downstream gate.
MIN_CORE_FRESH_ROWS = 1
CORE_COVERAGE_RECEIPT_SCHEMA_VERSION = "dawnstrike.luna.core_snapshot_coverage_receipt.v2"
CORE_COVERAGE_ROW_PROJECTION_SCHEMA_VERSION = (
    "dawnstrike.luna.core_snapshot_row_projection.v1"
)
CORE_COVERAGE_ROW_BINDING_FIELD = "core_coverage_row_binding_hash_sha256"
_SYMBOL_PATTERN = __import__("re").compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
STATE_STREET_SPY_HOLDINGS_URL = (
    "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
NASDAQ_NDX_SOD_URL_TEMPLATE = (
    "https://indexes.nasdaq.com/Index/ExportWeightings/NDX"
    "?tradeDate={month}%2F{day}%2F{year}&timeOfDay=SOD"
)
NASDAQ_NDX_SOD_2026_08_27_URL = NASDAQ_NDX_SOD_URL_TEMPLATE.format(
    month="08", day="27", year="2026"
)

# The Nasdaq export is an XLSX/ZIP.  ZIP container metadata (for example
# timestamps) changes between otherwise identical official downloads, so a
# raw archive SHA is not a stable trust root.  These are the decompressed
# member hashes and canonical member-list digest from the authenticated
# 2026-08-27 workbook.  The strict parser below requires this exact workbook
# structure and binds every member's content before it derives any cells.
_NDX_CANONICAL_ZIP_MEMBER_NAMES = (
    "[Content_Types].xml",
    "_rels/.rels",
    "docProps/app.xml",
    "docProps/core.xml",
    "docProps/custom.xml",
    "xl/_rels/workbook.xml.rels",
    "xl/sharedStrings.xml",
    "xl/styles.xml",
    "xl/workbook.xml",
    "xl/worksheets/sheet1.xml",
)
_NDX_CANONICAL_ZIP_MEMBER_HASHES = {
    "[Content_Types].xml": "995c7bc44e933d5ba24f893039d8a84dfcc0a12f2f5a3976e415f8324db0c89b",
    "_rels/.rels": "ea5fee20de01f0a4088506a54dc82f632d5cacd5e271b9f348fcea583d818d5b",
    "docProps/app.xml": "72afb5a0b44b3e20d04a41957246fd430e702b1841307752f9397411bdbaa2f7",
    "docProps/core.xml": "79af568221e1a17b8e59f618591359eb00991d86542a1784e2cd7700ee223d1c",
    "docProps/custom.xml": "18d57b34d4d3e2b37bc15406cd59124d2b461d905fe0775466e079611297c77b",
    "xl/_rels/workbook.xml.rels": (
        "995a125c59b9d23ca13e3b07d920b456ba47eabc42dd3e88dd7867cef1c83799"
    ),
    "xl/sharedStrings.xml": "26c260aa1da7b109d97440724126d9cbf660c7c52a5d007f468c75cd1896e840",
    "xl/styles.xml": "26c3e73e2f2ab9946a6e1f0d7128e9a0e8ea49b0448a410a49421844ae619996",
    "xl/workbook.xml": "6d246f4609966d5069771e0fd94f102ebe479f63557a7c6b7469f8bdd3bf4ce6",
    "xl/worksheets/sheet1.xml": "cf8166d6a12a68c6600c37a497521fa789e9f7e1c869b28b5cb3c3ea07dcb50a",
}
_NDX_DYNAMIC_MEMBER_NAMES = {
    "docProps/core.xml",
    "docProps/custom.xml",
    "xl/sharedStrings.xml",
    "xl/worksheets/sheet1.xml",
}
_NDX_CANONICAL_STATIC_MEMBER_HASHES = {
    name: digest
    for name, digest in _NDX_CANONICAL_ZIP_MEMBER_HASHES.items()
    if name not in _NDX_DYNAMIC_MEMBER_NAMES
}
_NDX_CANONICAL_ZIP_CONTENT_DIGEST_SHA256 = (
    "6c8fe9543904412a8ceed93c9554ebad4b64213603e3b1cdccf09ec8ca8a269b"
)
_NDX_CANONICAL_MEMBER_SET_HASH_SHA256 = (
    "c5e8bb1294642e0812f8a8d20f8c015548d41c64bfc6bef0aa0187994828a0ed"
)

# State Street changes the daily holdings date and (usually) weights while
# retaining this workbook package shape.  The proxy trust root therefore pins
# the package structure and the canonical ticker set, rather than volatile ZIP
# bytes or holdings weights.  The dynamic worksheet is still parsed strictly
# by the transformer below; a changed ticker set is never silently accepted.
_SPY_CANONICAL_ZIP_MEMBER_NAMES = (
    "[Content_Types].xml",
    "_rels/.rels",
    "docMetadata/LabelInfo.xml",
    "docProps/app.xml",
    "docProps/core.xml",
    "docProps/custom.xml",
    "xl/_rels/workbook.xml.rels",
    "xl/printerSettings/printerSettings1.bin",
    "xl/sharedStrings.xml",
    "xl/styles.xml",
    "xl/theme/theme1.xml",
    "xl/workbook.xml",
    "xl/worksheets/_rels/sheet1.xml.rels",
    "xl/worksheets/sheet1.xml",
)
_SPY_CANONICAL_STATIC_MEMBER_NAMES = tuple(
    name
    for name in _SPY_CANONICAL_ZIP_MEMBER_NAMES
    if name
    not in {
        "docProps/core.xml",
        "docProps/custom.xml",
        "xl/sharedStrings.xml",
        "xl/worksheets/sheet1.xml",
    }
)
_SPY_CANONICAL_STATIC_MEMBER_HASHES = {
    "[Content_Types].xml": "f496107ed0a062eeffeb1de799398580569de5d5146075b6d5c888d5303ae49d",
    "_rels/.rels": "933a35268fc19adfc9d784bf2c721c43fc36c37328d2a26b38ac0ad4d0fe66c8",
    "docMetadata/LabelInfo.xml": "86ce9b2e439e4c175e3d0ee8f115a616fe4ac1008c8b5b044554397b56623b1b",
    "docProps/app.xml": "758c9da301ae1bb81c9e2804d2db5e7ef1df524daa283702c7748a2de8453fcd",
    "xl/_rels/workbook.xml.rels": (
        "c250eb50b40d8d80e4880e576586c6c2bc330ee55bbadb90347112c347d1b621"
    ),
    "xl/printerSettings/printerSettings1.bin": (
        "79c5035258f390a3257885ad0d5b878ef00e390199ca172ff747653f8ca567e6"
    ),
    "xl/styles.xml": "c618a5f7feb3bc054f6620dd35e07f2c92c32091563fe415cdd173c69302f3e7",
    "xl/theme/theme1.xml": "4ea472506d97887770a296ec998c24f5c4c3eb100c72f9d4cdfb13dae2fe6c29",
    "xl/workbook.xml": "3f02861504a029bb96d8f200bfba51c6cfe14bbd56236ab7017a697a23fdf5f4",
    "xl/worksheets/_rels/sheet1.xml.rels": (
        "2c4b6ba262b6f12c55a743fb76b5909de0080b6d04bfa9c7f5391952c7bd0852"
    ),
}

# These are release trust roots for the currently mounted point-in-time
# sources.  A manifest cannot make a changed source authoritative merely by
# recomputing its self-declared digest; a future source release must add a new
# governed root (or carry a separately signed extraction receipt).
_TRUSTED_SOURCE_ROOTS: dict[str, dict[str, Any]] = {
    "state-street-spy-holdings-proxy-2026-08-24": {
        "index": "S&P 500",
        "effective_date": "2026-08-24",
        # Daily downloads change their raw bytes and embedded as-of date.  The
        # source is accepted only after the strict package/schema and canonical
        # ticker-set checks below; raw ZIP bytes are retained in each active
        # generation for audit but are not a reusable trust root.
        "raw_artifact_hashes": (),
        "canonical_zip_member_names": _SPY_CANONICAL_ZIP_MEMBER_NAMES,
        "canonical_static_member_hashes": _SPY_CANONICAL_STATIC_MEMBER_HASHES,
        "canonical_symbol_set_hash_sha256": (
            "5b5770ad1b7767aa92a785c3d201fa285b81a4f8ec2f9a1691e9121117d8a41e"
        ),
        "allow_future_same_semantic_set_dates": True,
        "maximum_source_age_days": 4,
        "transformation_id": "state-street-spy-holdings-parser-v1",
        "lineage_builder_id": "state-street-spy-holdings-parser-v1",
        "lineage_transformation_id": "exclude-cash-and-contra-holdings-v1",
        "lineage_schema_version": "dawnstrike.core_universe_lineage.v1",
        "reconstitution_id": "spy-holdings-2026-08-24",
        "membership_authority": "tracker_holdings_proxy",
        "official_index_authority": False,
        "source_scope": (
            "SPY tracker holdings used as an explicitly labeled S&P 500 membership proxy"
        ),
        "source_uri": STATE_STREET_SPY_HOLDINGS_URL,
    },
    "nasdaq-ndx-point-in-time-2026-07-07": {
        "index": "Nasdaq-100",
        "effective_date": "2026-07-07",
        "raw_artifact_hashes": (
            "a0dd0736856cee1f530350c642102a90d810704eb1071104a33ca88af2e4a4f4",
            "9be80af743d08fbec0e20162cba787b6377231376dfe7b2446ea62577a874e11",
            "b9ca8b7e8004470c79219b6e4800f2a48e319404e6a396804e6d1a2d022abb5b",
            "65138f5503a7e98a58ae5772300d7f5e50c974bef57b530bb6ec5bbd38639156",
        ),
        "transformation_id": "nasdaq-ndx-reconstitution-replay-v1",
        "lineage_builder_id": "nasdaq-ndx-reconstitution-builder-v1",
        "lineage_transformation_id": "ordered-official-notice-application-v1",
        "lineage_schema_version": "dawnstrike.core_universe_lineage.v1",
        "reconstitution_id": "ndx-through-2026-07-07",
        "membership_authority": "official_index_source",
        "official_index_authority": True,
        "source_scope": "Nasdaq-100 membership replay through the final 2026-07-07 notice",
    },
    # The July replay is retained for historical research only.  This root is
    # the current Aug-27 release root and is deliberately a different source
    # identity, transformer, effective date, and raw byte digest.
    "nasdaq-ndx-point-in-time-2026-08-27": {
        "index": "Nasdaq-100",
        "effective_date": "2026-08-27",
        # Do not pin the raw ZIP SHA: official downloads preserve the same
        # decompressed workbook while changing archive metadata.  The stable
        # trust root below pins every decompressed member and the derived set.
        "raw_artifact_hashes": (),
        "raw_artifact_byte_counts": (8439,),
        "canonical_zip_member_names": _NDX_CANONICAL_ZIP_MEMBER_NAMES,
        "canonical_zip_member_hashes": _NDX_CANONICAL_ZIP_MEMBER_HASHES,
        "canonical_static_member_hashes": _NDX_CANONICAL_STATIC_MEMBER_HASHES,
        "canonical_content_digest_sha256": _NDX_CANONICAL_ZIP_CONTENT_DIGEST_SHA256,
        "canonical_member_set_hash_sha256": _NDX_CANONICAL_MEMBER_SET_HASH_SHA256,
        "allow_future_same_semantic_set_dates": True,
        "source_uri_template": NASDAQ_NDX_SOD_URL_TEMPLATE,
        "source_scope_template": "Official Nasdaq-100 SOD Weightings export for {market_date}",
        "canonical_symbol_set_hash_sha256": (
            "a0fcb7b66b5efa5e1b2ded8dbc225133c7e1274d66abdb84858b9fe8c6483d30"
        ),
        "transformation_id": "nasdaq-ndx-sod-weightings-parser-v1",
        "lineage_builder_id": "nasdaq-ndx-sod-weightings-parser-v1",
        "lineage_transformation_id": "official-sod-weightings-export-v1",
        "lineage_schema_version": "dawnstrike.core_universe_lineage.v1",
        "reconstitution_id": "ndx-sod-2026-08-27",
        "membership_authority": "official_index_source",
        "official_index_authority": True,
        "source_scope": "Official Nasdaq-100 SOD Weightings export for 2026-08-27",
        "source_uri": NASDAQ_NDX_SOD_2026_08_27_URL,
    },
}


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
            # Nasdaq-100 membership is a point-in-time input, not a slowly
            # changing reference table.  A current release must use the
            # exact requested market date; otherwise the July replay (or any
            # other older set) could be observed today and incorrectly pass
            # the broad observation-age check.
            manifest_index_for_date = _index_name(
                manifest.get("index_name") or manifest.get("index")
            )
            if not manifest_index_for_date:
                declared_indexes = manifest.get("expected_counts")
                if isinstance(declared_indexes, dict):
                    normalized_indexes = {
                        _index_name(value) for value in declared_indexes if _index_name(value)
                    }
                    if "Nasdaq-100" in normalized_indexes:
                        manifest_index_for_date = "Nasdaq-100"
                membership_indexes = manifest.get("index_memberships")
                if isinstance(membership_indexes, dict) and "Nasdaq-100" in {
                    _index_name(value) for value in membership_indexes
                }:
                    manifest_index_for_date = "Nasdaq-100"
            if (
                production
                and requested_date
                and manifest_index_for_date == "Nasdaq-100"
                and effective != requested_date
            ):
                manifest_errors.append("currentness_date_mismatch:Nasdaq-100")

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

        raw_hashes, raw_errors, captured_artifacts = _capture_raw_artifacts(manifest)
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
        source_binding: dict[str, Any] = {
            "status": "NOT_CHECKED",
            "transformation_id": None,
            "derived_effective_date": None,
            "derived_member_set_hash_sha256": None,
            "derived_membership_count": 0,
        }
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
            source_binding, binding_errors = _validate_source_binding(
                manifest,
                index_name=_index_name(manifest_index),
                effective_date=effective,
                requested_date=requested_date,
                artifact_hashes=raw_hashes,
                artifact_bytes=captured_artifacts,
                declared_members=local_members,
            )
            manifest_errors.extend(binding_errors)
        source_artifacts.append(
            {
                "source_id": source_id,
                "source_uri": source_uri,
                # Production scope is always emitted from the trusted root;
                # a manifest cannot relabel an official feed or proxy.
                "source_scope": source_binding.get("source_scope")
                or (str(manifest.get("source_scope") or "").strip() if not production else None),
                "raw_artifact_sha256": raw_hash,
                "raw_artifact_hashes": list(raw_hashes),
                "canonical_member_set_hash_sha256": computed_member_hash,
                "declared_canonical_member_set_hash_sha256": declared_member_hash or None,
                "membership_authority": source_binding.get("membership_authority"),
                "official_index_authority": source_binding.get("official_index_authority"),
                "source_binding": source_binding,
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
    # A source-binding failure is a contract-wide provenance failure.  Keep
    # that failure visible on every index verdict so a valid-looking sibling
    # cannot be reported READY while the overall source is forged or stale.
    generic_source_binding_errors = sorted(
        {
            error.split(":", 1)[1] if error.startswith("manifest_") and ":" in error else error
            for error in errors
            if "source_binding_" in error
        }
    )
    for index in CORE_INDEXES:
        index_errors = [error for error in errors if f":{index}" in error or error.endswith(index)]
        index_errors.extend(generic_source_binding_errors)
        ready = (
            bool(per_index[index])
            and expected[index] is not None
            and len(per_index[index]) == expected[index]
            and freshness == "FRESH"
            and not generic_source_binding_errors
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
        "membership_authorities": {
            index: sorted(
                {
                    str(item.get("membership_authority") or "")
                    for item in source_artifacts
                    if item.get("membership_authority")
                    and item.get("source_binding", {}).get("index") == index
                }
            )
            for index in CORE_INDEXES
        },
        "proxy_disclosures": [
            "S&P 500 membership is proxied by SPY tracker holdings; this is not an official "
            "S&P DJI constituent feed."
        ],
        "raw_artifact_hashes": [
            digest
            for item in source_artifacts
            for digest in (
                item.get("raw_artifact_hashes")
                or ([item["raw_artifact_sha256"]] if item.get("raw_artifact_sha256") else [])
            )
        ],
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


def _empty_core_discovery_result(
    *,
    status: str,
    reason: str,
    requested_count: int = 0,
    missing_count: int = 0,
) -> dict[str, Any]:
    """Return the stable shape used when no provider request can be made.

    This path deliberately has no coverage receipt: no provider batch was
    attempted, so claiming a receipt would turn an unavailable contract into
    an observed snapshot.  Counts still make the missing truth explicit to
    lane and run-contract consumers.
    """

    requested = max(int(requested_count), 0)
    missing = max(int(missing_count), 0)
    return {
        "status": str(status),
        "coverage_status": "DATA_UNAVAILABLE",
        "rows": [],
        "reason": str(reason),
        "requested_count": requested,
        "returned_count": 0,
        "eligible_count": 0,
        "fresh_count": 0,
        "fresh_verified_count": 0,
        "stale_count": 0,
        "missing_count": missing,
        "unknown_count": 0,
        "duplicate_count": 0,
        "coverage_receipts": [],
        "coverage_receipt_ids": [],
        "coverage_receipt_hashes": [],
        "attempted_count": 0,
        "attempted_batch_count": 0,
        "failed_batch_count": 0,
        "limitations": ["core_snapshot_not_observed"],
        "discovery_coverage_receipt": None,
    }


def _snapshot_response_hash(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            rows,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


# These are the fields owned by the core-discovery/rank seam: they can affect
# core eligibility, ranking, freshness, membership, or the provenance of the
# authenticated provider observation. Optional halt/news/range enrichment
# owns its own evidence and is intentionally excluded; those stages are
# allowed to add or replace their fields after this receipt is sealed.
_CORE_ROW_NUMERIC_FIELDS = (
    "previous_close",
    "premarket_price",
    "premarket_high",
    "premarket_low",
    "premarket_volume",
    "dollar_volume",
    "gap_pct",
    "float_shares",
    "market_cap",
    "spread_pct",
    "short_float_pct",
    "source_confidence",
    "field_completeness_score",
    "source_reliability_prior",
    "reconciliation_confidence_score",
    "source_count",
    "core_lane_score",
    "missing_enrichment_count",
)
_CORE_ROW_BOOLEAN_FIELDS = (
    "stale_data_flag",
    "core_lane_eligible",
    "shadow_mode",
    "paid_data",
    "fixture_only",
    "manual_uploaded_data",
)
_CORE_ROW_TEXT_FIELDS = (
    "company",
    "source",
    "source_url",
    "extraction_mode",
    "source_timestamp",
    "as_of_timestamp",
    "extracted_at",
    "source_quality_status",
    "data_source_kind",
    "discovery_context",
    "universe_lane",
    "evidence_lane",
    "freshness_status",
    "preferred_source",
    "score_consensus",
    "conflict_flags",
    "row_merge_reason",
    "evidence_confidence_version",
    "reconciliation_status",
    "raw_file_path",
    "imported_at",
)


def _canonical_row_decimal(value: Any) -> str | None:
    """Return a stable decimal representation across JSON/CSV conversions."""

    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    if parsed == 0:
        return "0"
    return format(parsed.normalize(), "f")


def _canonical_decimal_product(price: Any, volume: Any) -> str | None:
    price_text = _canonical_row_decimal(price)
    volume_text = _canonical_row_decimal(volume)
    if price_text is None or volume_text is None:
        return None
    try:
        product = Decimal(price_text) * Decimal(volume_text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not product.is_finite():
        return None
    # Providers publish dollar volume to cents; use the same deterministic
    # projection for a missing value and for the rank score.
    try:
        rounded = product.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return _canonical_row_decimal(rounded)


def _canonical_row_bool(value: Any) -> bool | str:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (float, Decimal)):
        if not math.isfinite(float(value)):
            return "invalid:" + str(value).lower()
        return value != 0
    if isinstance(value, int):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"true", "t", "1", "yes", "y"}:
        return True
    if normalized in {"false", "f", "0", "no", "n", ""}:
        return False
    # Keep invalid values distinct in the hash. Ranking still fails closed
    # when an invalid boolean reaches a model parser.
    return "invalid:" + normalized


def _canonical_row_timestamp(value: Any) -> str:
    if value is None or not str(value).strip():
        return ""
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed is not None else str(value).strip()


def _canonical_row_memberships(value: Any) -> list[str]:
    """Normalize list values emitted as native JSON, Python, or CSV text."""

    if value is None:
        return []
    parsed: Any = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        for loader in (json.loads, ast.literal_eval):
            try:
                candidate = loader(text)
            except (TypeError, ValueError, SyntaxError, json.JSONDecodeError):
                continue
            if isinstance(candidate, (list, tuple, set)):
                parsed = candidate
                break
        else:
            parsed = [part.strip() for part in text.replace(";", ",").split(",")]
    if isinstance(parsed, dict) or not isinstance(parsed, (list, tuple, set)):
        parsed = [parsed]
    normalized: list[str] = []
    for item in parsed:
        text = str(item).strip().strip("'\"")
        if not text:
            continue
        normalized.append(_index_name(text) or text)
    return sorted(normalized)


def _row_freshness_status(row: dict[str, Any]) -> str:
    explicit = str(row.get("freshness_status") or "").strip()
    if explicit:
        return explicit
    payload_text = row.get("core_coverage_receipt_payload_json")
    if not isinstance(payload_text, str) or not payload_text.strip():
        return ""
    try:
        receipt = json.loads(payload_text)
        observed_at = _parse_datetime(receipt.get("observed_at"))
        max_age_seconds = int(receipt.get("max_age_seconds") or 0)
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return ""
    if observed_at is None or max_age_seconds <= 0:
        return ""
    return _snapshot_freshness_status(
        row,
        observed_at=observed_at,
        max_age_seconds=max_age_seconds,
    )


def build_core_row_binding_projection(
    row: dict[str, Any], *, freshness_status: str | None = None
) -> dict[str, Any]:
    """Build the canonical values used by the core publication/rank gate.

    This projection intentionally excludes receipt identity metadata and raw
    provider-only fields. It includes every normalized value that can affect
    core eligibility/ranking or its provenance. Missing ``dollar_volume`` is
    represented by a deterministic price*volume value so a CSV model
    round-trip does not turn a legitimate row into a different observation.
    """

    ticker = canonical_symbol(row.get("ticker") or row.get("symbol"))
    effective_row = dict(row)
    # SnapshotRow.from_mapping fills these stable defaults while reading CSV.
    # Apply the same defaults before hashing so a provider dict and its
    # production CSV representation describe the same observation.
    effective_row["company"] = str(row.get("company") or ticker).strip()
    effective_row["previous_close"] = (
        row.get("previous_close") if row.get("previous_close") not in {None, ""} else 0
    )
    effective_row["spread_pct"] = (
        row.get("spread_pct") if row.get("spread_pct") not in {None, ""} else 0
    )
    effective_row["source_confidence"] = (
        row.get("source_confidence")
        if row.get("source_confidence") not in {None, ""}
        else 0
    )
    effective_row["source_count"] = (
        row.get("source_count") if row.get("source_count") not in {None, ""} else 1
    )
    effective_row["missing_enrichment_count"] = (
        row.get("missing_enrichment_count")
        if row.get("missing_enrichment_count") not in {None, ""}
        else 0
    )
    effective_row["score_consensus"] = str(row.get("score_consensus") or "single_source")
    effective_row["row_merge_reason"] = str(row.get("row_merge_reason") or "single_source")
    effective_row["extraction_mode"] = str(
        row.get("extraction_mode") or row.get("data_source_kind") or ""
    )
    # Freshness uses source_timestamp first and as_of_timestamp as its legacy
    # fallback. Bind both timestamp values while filling a missing as_of value
    # from source_timestamp so SnapshotRow's current-time default cannot alter
    # a legitimate row after CSV roundtrip.
    effective_timestamp = row.get("source_timestamp") or row.get("as_of_timestamp")
    effective_row["source_timestamp"] = effective_timestamp
    effective_row["as_of_timestamp"] = row.get("as_of_timestamp") or effective_timestamp
    projection: dict[str, Any] = {
        "schema_version": CORE_COVERAGE_ROW_PROJECTION_SCHEMA_VERSION,
        "ticker": ticker,
        "core_universe_memberships": _canonical_row_memberships(
            effective_row.get("core_universe_memberships")
        ),
        "source_timestamp_present": bool(str(row.get("source_timestamp") or "").strip()),
        "as_of_timestamp_present": bool(str(row.get("as_of_timestamp") or "").strip()),
        "dollar_volume_present": bool(str(row.get("dollar_volume") or "").strip()),
        "core_lane_score_present": bool(str(row.get("core_lane_score") or "").strip()),
    }
    for field in _CORE_ROW_NUMERIC_FIELDS:
        value = effective_row.get(field)
        canonical = _canonical_row_decimal(value)
        if field == "gap_pct" and canonical == "0":
            previous_close = _canonical_row_decimal(effective_row.get("previous_close"))
            gap_source = str(effective_row.get("gap_pct_source") or "").strip()
            try:
                no_previous_close = previous_close is None or Decimal(previous_close) <= 0
            except (InvalidOperation, TypeError, ValueError):
                no_previous_close = True
            if no_previous_close and not gap_source:
                # SnapshotRow derives a zero for an absent gap when there is
                # no previous close. Keep the canonical projection as
                # absence so that CSV/model fallback does not manufacture a
                # provider assertion.
                canonical = None
        if field == "dollar_volume" and canonical is None:
            canonical = _canonical_decimal_product(
                effective_row.get("premarket_price"), effective_row.get("premarket_volume")
            )
        if field == "core_lane_score" and canonical is None:
            canonical = _canonical_decimal_product(
                effective_row.get("premarket_price"), effective_row.get("premarket_volume")
            )
        projection[field] = canonical
    for field in _CORE_ROW_BOOLEAN_FIELDS:
        projection[field] = _canonical_row_bool(effective_row.get(field))
    for field in _CORE_ROW_TEXT_FIELDS:
        value = freshness_status if field == "freshness_status" else effective_row.get(field)
        if field == "freshness_status" and not str(value or "").strip():
            value = _row_freshness_status(row) or "UNKNOWN"
        if field in {"source", "preferred_source"}:
            projection[field] = str(value or "").strip().lower()
        elif field in {
            "source_quality_status",
            "freshness_status",
            "reconciliation_status",
            "enrichment_status",
            "enrichment_fallback_status",
        }:
            projection[field] = str(value or "").strip().upper()
        elif field in {"universe_lane", "evidence_lane"}:
            projection[field] = str(value or "").strip().lower()
        elif field in {
            "source_timestamp",
            "as_of_timestamp",
            "extracted_at",
            "imported_at",
            "enrichment_observed_at",
            "enrichment_bar_completed_at",
            "prior_daily_high_observed_at",
            "prior_daily_high_completed_at",
        }:
            projection[field] = _canonical_row_timestamp(value)
        else:
            projection[field] = str(value or "").strip()
    universe_lane = str(projection.get("universe_lane") or "")
    if not projection.get("evidence_lane") and universe_lane in {"core", "mover+core"}:
        # ``evidence_lane`` was historically omitted from the snapshot CSV;
        # infer the safe core value for old readers while still hashing an
        # explicitly supplied, conflicting value.
        projection["evidence_lane"] = "core"
    return projection


def core_row_binding_hash(
    row: dict[str, Any], *, freshness_status: str | None = None
) -> str:
    projection = build_core_row_binding_projection(
        row, freshness_status=freshness_status
    )
    return hashlib.sha256(
        json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _failed_core_coverage_receipt(
    *,
    requested: list[str],
    requested_memberships: dict[str, list[str]],
    batch_number: int,
    authenticated: bool,
    contract_hash: str,
    observed_at: datetime,
    max_age_seconds: int,
    error_class: str,
) -> dict[str, Any]:
    """Build a content-addressed receipt for one failed provider batch.

    Only the exception class crosses the provider boundary.  In particular,
    the message is never copied into a receipt because provider errors can
    contain URLs, request identifiers, or accidental credential material.
    """

    requested_symbols = [
        canonical_symbol(symbol) for symbol in requested if canonical_symbol(symbol)
    ]
    base: dict[str, Any] = {
        "schema_version": CORE_COVERAGE_RECEIPT_SCHEMA_VERSION,
        "contract_hash_sha256": str(contract_hash or ""),
        "batch_number": int(batch_number),
        "requested_symbols": requested_symbols,
        "requested_memberships": {
            symbol: sorted(
                {
                    str(value).strip()
                    for value in requested_memberships.get(symbol, [])
                    if str(value).strip()
                }
            )
            for symbol in requested_symbols
        },
        "requested_count": len(requested_symbols),
        "returned_symbols": [],
        "returned_count": 0,
        "missing_symbols": requested_symbols,
        "missing_count": len(requested_symbols),
        "unknown_symbols": [],
        "unknown_count": 0,
        "duplicate_symbols": [],
        "duplicate_count": 0,
        "fresh_symbols": [],
        "fresh_count": 0,
        "fresh_verified_symbols": [],
        "fresh_verified_count": 0,
        "stale_symbols": [],
        "stale_count": 0,
        "unknown_freshness_symbols": [],
        "unknown_freshness_count": 0,
        "eligible_symbols": [],
        "eligible_count": 0,
        "unverified_symbols": requested_symbols,
        "unverified_count": len(requested_symbols),
        "authenticated_provider": bool(authenticated),
        "provider": "",
        "observed_at": observed_at.isoformat(),
        "max_age_seconds": int(max_age_seconds),
        "row_quality": [],
        "row_binding_schema_version": CORE_COVERAGE_ROW_PROJECTION_SCHEMA_VERSION,
        "row_binding_hashes": {},
        "row_bindings": [],
        "response_hash_sha256": _snapshot_response_hash([]),
        "status": "FAILED",
        "error_class": str(error_class or "ProviderError"),
        "limitations": ["provider_batch_error"],
    }
    digest = _coverage_receipt_digest(base)
    base["coverage_receipt_hash_sha256"] = digest
    base["coverage_receipt_id"] = "luna-core-coverage-" + digest[:24]
    return base


def _classify_core_snapshot_batch(
    snapshots: Any,
    *,
    requested: list[str],
    requested_memberships: dict[str, list[str]],
    batch_number: int,
    authenticated: bool,
    contract_hash: str,
    observed_at: datetime,
    max_age_seconds: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Classify one provider response and return only safe rows.

    The receipt counts raw observations, while ``eligible_symbols`` is a
    unique set.  A duplicated ticker is therefore visible in ``returned`` and
    ``duplicate`` counts but is never promoted to a research row.
    """

    requested_symbols = [
        canonical_symbol(symbol) for symbol in requested if canonical_symbol(symbol)
    ]
    requested_set = set(requested_symbols)
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
    response_hash = _snapshot_response_hash(batch_rows)

    returned = [str(row.get("ticker") or "") for row in batch_rows]
    returned_nonempty = [ticker for ticker in returned if ticker]
    duplicates = sorted(
        ticker
        for ticker, count in Counter(returned_nonempty).items()
        if count > 1
    )
    unknown = sorted(set(returned_nonempty) - requested_set)
    missing = sorted(requested_set - set(returned_nonempty))
    row_quality: list[dict[str, Any]] = []
    row_status: dict[int, tuple[bool, str]] = {}
    for index, row in enumerate(batch_rows):
        ticker = str(row.get("ticker") or "")
        source_verified = bool(
            authenticated
            and str(row.get("source") or "").lower().startswith("alpaca")
        )
        freshness = _snapshot_freshness_status(
            row,
            observed_at=observed_at,
            max_age_seconds=max_age_seconds,
        )
        row_status[index] = (source_verified, freshness)
        row_quality.append(
            {
                "ticker": ticker,
                "provider": "alpaca" if source_verified else "",
                "source_verified": source_verified,
                "freshness_status": freshness,
            }
        )

    fresh_symbols = sorted(
        {
            str(row.get("ticker") or "")
            for index, row in enumerate(batch_rows)
            if str(row.get("ticker") or "") in requested_set
            and row_status[index][1] == "FRESH"
        }
    )
    fresh_verified_symbols = sorted(
        {
            str(row.get("ticker") or "")
            for index, row in enumerate(batch_rows)
            if str(row.get("ticker") or "") in requested_set
            and row_status[index][0]
            and row_status[index][1] == "FRESH"
        }
    )
    stale_symbols = sorted(
        {
            str(row.get("ticker") or "")
            for index, row in enumerate(batch_rows)
            if str(row.get("ticker") or "") in requested_set
            and row_status[index][1] == "STALE"
        }
    )
    unknown_freshness_symbols = sorted(
        {
            str(row.get("ticker") or "")
            for index, row in enumerate(batch_rows)
            if str(row.get("ticker") or "") in requested_set
            and row_status[index][1] in {"UNKNOWN", "FUTURE"}
        }
    )
    unverified_symbols = sorted(
        {
            str(row.get("ticker") or "")
            for index, row in enumerate(batch_rows)
            if str(row.get("ticker") or "") in requested_set
            and not row_status[index][0]
        }
    )
    eligible_symbols = sorted(
        set(fresh_verified_symbols) - set(duplicates)
    )
    # A provider response containing explicit stale_data_flag=true is not
    # eligible even if its timestamp is fresh: preserve the stronger source
    # safety declaration rather than overwriting it.
    eligible_symbols = [
        symbol
        for symbol in eligible_symbols
        if not any(
            str(row.get("ticker") or "") == symbol
            and _truthy(row.get("stale_data_flag"))
            for row in batch_rows
        )
    ]
    row_binding_hashes: dict[str, str] = {}
    row_bindings: list[dict[str, Any]] = []
    # Attach every value consumed by the core rank gate before hashing. The
    # receipt then binds the exact normalized row that is returned to callers;
    # receipt identity fields are added only after this projection is sealed.
    for index, row in enumerate(batch_rows):
        ticker = str(row.get("ticker") or "")
        if ticker not in eligible_symbols or row_status[index][1] != "FRESH":
            continue
        row["discovery_context"] = "luna_core:" + ",".join(
            requested_memberships.get(ticker, [])
        )
        row["universe_lane"] = "core"
        row["evidence_lane"] = "core"
        row["core_universe_memberships"] = list(requested_memberships.get(ticker, []))
        row["source_quality_status"] = "VERIFIED"
        row["freshness_status"] = "FRESH"
        effective_source_timestamp = row.get("source_timestamp") or row.get("as_of_timestamp")
        row["source_timestamp"] = effective_source_timestamp
        row["as_of_timestamp"] = row.get("as_of_timestamp") or effective_source_timestamp
        gap_value = row.get("gap_pct")
        if gap_value is None or (isinstance(gap_value, str) and not gap_value.strip()):
            # Match SnapshotRow/formula semantics: derive the real gap when
            # the provider supplied a usable previous close.  Leave it
            # absent when no previous close exists; formula scoring treats
            # that absence as zero without turning the provider observation
            # into an asserted zero-valued gap.
            try:
                price = float(row.get("premarket_price"))
                previous_close = float(row.get("previous_close"))
            except (TypeError, ValueError):
                price = 0.0
                previous_close = 0.0
            if previous_close > 0:
                row["gap_pct"] = ((price - previous_close) / previous_close) * 100
        row["core_lane_eligible"] = True
        score_text = _canonical_decimal_product(
            row.get("premarket_price"), row.get("premarket_volume")
        )
        if score_text is not None:
            # Dollar volume is a derived rank input. Normalize it to the
            # bound price*volume product so callers cannot later supply a
            # mutable, unbound score value.
            row["dollar_volume"] = float(score_text)
        row["core_lane_score"] = float(score_text) if score_text is not None else None
        binding_hash = core_row_binding_hash(row, freshness_status="FRESH")
        row_binding_hashes[ticker] = binding_hash
        row_bindings.append(
            {
                "row_index": index,
                "ticker": ticker,
                "row_binding_hash_sha256": binding_hash,
            }
        )
        row_quality[index]["row_binding_hash_sha256"] = binding_hash
    quality_ready = bool(requested_symbols) and (
        len(returned) == len(requested_symbols)
        and not missing
        and not unknown
        and not duplicates
        and len(fresh_verified_symbols) == len(requested_symbols)
        and len(eligible_symbols) == len(requested_symbols)
        and authenticated
    )
    limitations: list[str] = []
    if missing:
        limitations.append("missing_requested_symbols")
    if unknown:
        limitations.append("unknown_returned_symbols")
    if duplicates:
        limitations.append("duplicate_returned_symbols")
    if stale_symbols:
        limitations.append("stale_requested_snapshots")
    if unknown_freshness_symbols:
        limitations.append("unknown_snapshot_freshness")
    if unverified_symbols:
        limitations.append("unverified_provider_rows")
    if not authenticated:
        limitations.append("provider_not_authenticated")
    if not eligible_symbols:
        limitations.append("no_fresh_verified_core_rows")
    status = "READY" if quality_ready else "PARTIAL" if eligible_symbols else "INCOMPLETE"
    base: dict[str, Any] = {
        "schema_version": CORE_COVERAGE_RECEIPT_SCHEMA_VERSION,
        "contract_hash_sha256": str(contract_hash or ""),
        "batch_number": int(batch_number),
        "requested_symbols": requested_symbols,
        "requested_memberships": {
            symbol: sorted(
                {
                    str(value).strip()
                    for value in requested_memberships.get(symbol, [])
                    if str(value).strip()
                }
            )
            for symbol in requested_symbols
        },
        "requested_count": len(requested_symbols),
        "returned_symbols": returned,
        "returned_count": len(returned),
        "missing_symbols": missing,
        "missing_count": len(missing),
        "unknown_symbols": unknown,
        "unknown_count": len(unknown),
        "duplicate_symbols": duplicates,
        "duplicate_count": len(duplicates),
        "fresh_symbols": fresh_symbols,
        "fresh_count": len(fresh_symbols),
        "fresh_verified_symbols": fresh_verified_symbols,
        "fresh_verified_count": len(fresh_verified_symbols),
        "stale_symbols": stale_symbols,
        "stale_count": len(stale_symbols),
        "unknown_freshness_symbols": unknown_freshness_symbols,
        "unknown_freshness_count": len(unknown_freshness_symbols),
        "eligible_symbols": eligible_symbols,
        "eligible_count": len(eligible_symbols),
        "unverified_symbols": unverified_symbols,
        "unverified_count": len(unverified_symbols),
        "authenticated_provider": bool(authenticated),
        "provider": "alpaca" if authenticated and eligible_symbols else "",
        "observed_at": observed_at.isoformat(),
        "max_age_seconds": int(max_age_seconds),
        "row_quality": row_quality,
        "row_binding_schema_version": CORE_COVERAGE_ROW_PROJECTION_SCHEMA_VERSION,
        "row_binding_hashes": row_binding_hashes,
        "row_bindings": row_bindings,
        "response_hash_sha256": response_hash,
        "status": status,
        "limitations": sorted(set(limitations)),
    }
    digest = _coverage_receipt_digest(base)
    base["coverage_receipt_hash_sha256"] = digest
    base["coverage_receipt_id"] = "luna-core-coverage-" + digest[:24]
    receipt_payload = json.dumps(base, sort_keys=True, separators=(",", ":"), default=str)
    eligible_rows: list[dict[str, Any]] = []
    for index, row in enumerate(batch_rows):
        ticker = str(row.get("ticker") or "")
        if ticker not in eligible_symbols or row_status[index][1] != "FRESH":
            continue
        row["core_coverage_receipt_id"] = base["coverage_receipt_id"]
        row["core_coverage_receipt_hash_sha256"] = base["coverage_receipt_hash_sha256"]
        row["core_coverage_receipt_status"] = status
        row[CORE_COVERAGE_ROW_BINDING_FIELD] = row_binding_hashes.get(ticker, "")
        row["core_coverage_receipt_payload_json"] = receipt_payload
        eligible_rows.append(row)
    return base, eligible_rows


def discover_core_universe_rows(
    contract: dict[str, Any],
    *,
    config: ScannerConfig,
    provider: Any | None = None,
    observed_at: datetime | None = None,
    max_symbols: int = 600,
    batch_size: int = 50,
    minimum_fresh_rows: int = MIN_CORE_FRESH_ROWS,
) -> dict[str, Any]:
    """Collect current read-only snapshots for READY core members.

    A provider response is a *coverage observation*, not an all-or-nothing
    batch.  Inactive symbols, omitted snapshots, and stale observations are
    retained in the immutable per-batch receipt while independently verified
    fresh rows from the same response remain usable.  ``READY`` is reserved
    for complete coverage; any usable subset is explicitly ``PARTIAL`` and
    can never imply full S&P 500/Nasdaq-100 coverage.

    Discovery failure is returned as a lane-local blocker; callers retain the
    existing mover rows.  No row is synthesized from membership alone.
    """

    if str(contract.get("status") or "") != "READY":
        return _empty_core_discovery_result(
            status="DATA_UNAVAILABLE",
            reason=str(contract.get("reason") or "core universe contract unavailable"),
        )
    all_members = list(contract.get("members") or [])
    if len(all_members) > max(int(max_symbols), 0):
        return _empty_core_discovery_result(
            status="INCOMPLETE",
            reason="core universe exceeds bounded discovery capacity",
            requested_count=len(all_members),
            missing_count=len(all_members),
        )
    members = all_members
    symbols = [canonical_symbol(row.get("symbol") or row.get("ticker")) for row in members]
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
        return _empty_core_discovery_result(
            status="DATA_UNAVAILABLE", reason="core universe has no members"
        )
    active_provider = provider or AlpacaProvider(config)
    discovered_at = observed_at or datetime.now(timezone.utc)
    if discovered_at.tzinfo is None:
        discovered_at = discovered_at.replace(tzinfo=timezone.utc)
    else:
        discovered_at = discovered_at.astimezone(timezone.utc)
    max_snapshot_age_seconds = max(
        int(getattr(config, "premarket_enrichment_max_age_seconds", 1_200) or 1_200),
        60,
    )
    memberships = {
        canonical_symbol(row.get("symbol") or row.get("ticker")): sorted(
            {
                str(value).strip()
                for value in (
                    [row.get("index_memberships")]
                    if isinstance(row.get("index_memberships"), str)
                    else row.get("index_memberships") or []
                )
                if str(value).strip()
            }
        )
        for row in members
    }
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    size = max(int(batch_size), 1)
    minimum_rows = max(int(minimum_fresh_rows), 1)
    authenticated = False
    authentication_error = ""
    if hasattr(active_provider, "validate_credentials"):
        try:
            active_provider.validate_credentials()
            authenticated = True
        except Exception as exc:
            # Keep the public receipt free of provider URLs, credentials, or
            # exception text; only the stable class is useful for diagnosis.
            authentication_error = type(exc).__name__
    for batch_number, start in enumerate(range(0, len(symbols), size), start=1):
        requested = symbols[start : start + size]
        if authentication_error:
            receipts.append(
                _failed_core_coverage_receipt(
                    requested=requested,
                    requested_memberships=memberships,
                    batch_number=batch_number,
                    authenticated=False,
                    contract_hash=str(contract.get("content_hash_sha256") or ""),
                    observed_at=discovered_at,
                    max_age_seconds=max_snapshot_age_seconds,
                    error_class=authentication_error,
                )
            )
            continue
        try:
            snapshots = active_provider.get_premarket_snapshot(requested, config)
            receipt, eligible_rows = _classify_core_snapshot_batch(
                snapshots,
                requested=requested,
                requested_memberships=memberships,
                batch_number=batch_number,
                authenticated=authenticated,
                contract_hash=str(contract.get("content_hash_sha256") or ""),
                observed_at=discovered_at,
                max_age_seconds=max_snapshot_age_seconds,
            )
        except Exception as exc:
            receipts.append(
                _failed_core_coverage_receipt(
                    requested=requested,
                    requested_memberships=memberships,
                    batch_number=batch_number,
                    authenticated=authenticated,
                    contract_hash=str(contract.get("content_hash_sha256") or ""),
                    observed_at=discovered_at,
                    max_age_seconds=max_snapshot_age_seconds,
                    error_class=type(exc).__name__,
                )
            )
            continue
        receipts.append(receipt)
        rows.extend(eligible_rows)

    fresh_count = sum(int(item.get("fresh_count") or 0) for item in receipts)
    fresh_verified_count = sum(
        int(item.get("fresh_verified_count") or 0) for item in receipts
    )
    stale_count = sum(int(item.get("stale_count") or 0) for item in receipts)
    missing_count = sum(int(item.get("missing_count") or 0) for item in receipts)
    unknown_count = sum(int(item.get("unknown_count") or 0) for item in receipts)
    unknown_freshness_count = sum(
        int(item.get("unknown_freshness_count") or 0) for item in receipts
    )
    unverified_count = sum(int(item.get("unverified_count") or 0) for item in receipts)
    duplicate_count = sum(int(item.get("duplicate_count") or 0) for item in receipts)
    returned_count = sum(int(item.get("returned_count") or 0) for item in receipts)
    complete = (
        len(rows) == len(symbols)
        and len({str(row.get("ticker")) for row in rows}) == len(symbols)
        and all(item["status"] == "READY" for item in receipts)
    )
    partial = bool(rows) and len(rows) >= minimum_rows
    limitations = sorted(
        {
            limitation
            for item in receipts
            for limitation in item.get("limitations") or []
        }
    )
    failed_batches = sum(1 for item in receipts if item.get("status") == "FAILED")
    status = "READY" if complete else "PARTIAL" if partial else (
        "BLOCKED_EXTERNAL" if failed_batches else "DATA_UNAVAILABLE"
    )
    if failed_batches:
        limitations = [
            limitation for limitation in limitations if limitation != "provider_batch_error"
        ]
        limitations.append(
            "provider_batch_error_after_partial_coverage" if rows else "provider_batch_error"
        )
    if not complete and "full_core_universe_coverage_not_observed" not in limitations:
        limitations.append("full_core_universe_coverage_not_observed")
    limitations = sorted(set(limitations))
    aggregate_payload = {
        "schema_version": CORE_COVERAGE_RECEIPT_SCHEMA_VERSION,
        "contract_hash_sha256": str(contract.get("content_hash_sha256") or ""),
        "requested_symbols": symbols,
        "requested_count": len(symbols),
        "returned_count": returned_count,
        "eligible_count": len(rows),
        "fresh_count": fresh_count,
        "fresh_verified_count": fresh_verified_count,
        "stale_count": stale_count,
        "missing_count": missing_count,
        "unknown_count": unknown_count,
        "unknown_freshness_count": unknown_freshness_count,
        "unverified_count": unverified_count,
        "duplicate_count": duplicate_count,
        "observed_at": discovered_at.isoformat(),
        "max_age_seconds": max_snapshot_age_seconds,
        "batch_receipt_ids": [str(item["coverage_receipt_id"]) for item in receipts],
        "batch_receipt_hashes": [
            str(item["coverage_receipt_hash_sha256"]) for item in receipts
        ],
        "attempted_count": sum(
            int(item.get("requested_count") or 0) for item in receipts
        ),
        "attempted_batch_count": len(receipts),
        "failed_batch_count": failed_batches,
        "status": status,
        "limitations": limitations,
    }
    aggregate_hash = _coverage_receipt_digest(aggregate_payload)
    return {
        "status": status,
        "coverage_status": "COMPLETE" if complete else "LIMITED" if partial else "DATA_UNAVAILABLE",
        "rows": rows,
        "reason": (
            ""
            if complete
            else "one or more provider batches failed; no fresh verified core rows"
            if failed_batches and not partial
            else "core snapshot coverage unavailable; no fresh verified core rows"
            if not partial
            else (
                "core snapshot coverage is partial; full S&P 500/Nasdaq-100 coverage "
                "was not observed"
            )
        ),
        "requested_count": len(symbols),
        "returned_count": returned_count,
        "eligible_count": len(rows),
        "fresh_count": fresh_count,
        "fresh_verified_count": fresh_verified_count,
        "stale_count": stale_count,
        "missing_count": missing_count,
        "unknown_count": unknown_count,
        "unknown_freshness_count": unknown_freshness_count,
        "unverified_count": unverified_count,
        "duplicate_count": duplicate_count,
        "coverage_receipts": receipts,
        "coverage_receipt_ids": [str(item["coverage_receipt_id"]) for item in receipts],
        "coverage_receipt_hashes": [
            str(item["coverage_receipt_hash_sha256"]) for item in receipts
        ],
        "attempted_count": sum(int(item.get("requested_count") or 0) for item in receipts),
        "attempted_batch_count": len(receipts),
        "failed_batch_count": failed_batches,
        "discovery_coverage_receipt": {
            **aggregate_payload,
            "coverage_receipt_hash_sha256": aggregate_hash,
            "coverage_receipt_id": "luna-core-discovery-" + aggregate_hash[:24],
        },
        "limitations": limitations,
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
            # Mover precedence controls legacy scoring fields, but it must not
            # discard the core snapshot's immutable coverage binding.  A later
            # slate/watcher's source gate needs to know which exact authenticated
            # batch supplied the core observation.
            for key in (
                "core_coverage_receipt_id",
                "core_coverage_receipt_hash_sha256",
                "core_coverage_receipt_status",
                "core_coverage_receipt_payload_json",
                CORE_COVERAGE_ROW_BINDING_FIELD,
            ):
                if row.get(key) and not current.get(key):
                    current[key] = row[key]
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
        if not _core_coverage_binding_valid(row):
            continue
        try:
            price_text = _canonical_row_decimal(row.get("premarket_price"))
            volume_text = _canonical_row_decimal(row.get("premarket_volume"))
            if price_text is None or volume_text is None:
                continue
            price = Decimal(price_text)
            volume = Decimal(volume_text)
            score_text = _canonical_decimal_product(price_text, volume_text)
            reported_dollar_volume = _canonical_row_decimal(row.get("dollar_volume"))
            if reported_dollar_volume is None:
                reported_dollar_volume = score_text
            bound_score = _canonical_row_decimal(row.get("core_lane_score"))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if (
            price <= 0
            or volume <= 0
            or _canonical_row_bool(row.get("stale_data_flag")) is not False
            or score_text is None
            or (
                bool(row.get(CORE_COVERAGE_ROW_BINDING_FIELD))
                and (
                    reported_dollar_volume != score_text
                    or bound_score != score_text
                )
            )
        ):
            continue
        if str(row.get("universe_lane") or "").strip().lower() not in {
            "core",
            "mover+core",
        }:
            row["universe_lane"] = "core"
        row["core_lane_eligible"] = True
        row["core_lane_score"] = float(score_text)
        eligible.append(row)
    return sorted(
        eligible,
        key=lambda row: (
            float(row.get("core_lane_score") or 0),
            canonical_symbol(row.get("ticker")),
        ),
        reverse=True,
    )[: max(int(max_rows), 0)]


def _core_coverage_binding_valid(row: dict[str, Any]) -> bool:
    """Verify production core rows still point at an immutable batch receipt.

    Legacy/unit-test rows without a coverage binding remain usable by this
    pure ranking helper.  Once discovery has attached a binding, however, a
    missing, mutated, or cross-symbol receipt fails closed before scoring.
    """

    marker_keys = {
        "core_coverage_receipt_id",
        "core_coverage_receipt_hash_sha256",
        "core_coverage_receipt_payload_json",
        CORE_COVERAGE_ROW_BINDING_FIELD,
    }
    if not any(key in row for key in marker_keys):
        universe_lane = str(row.get("universe_lane") or "").strip().lower()
        evidence_lane = str(row.get("evidence_lane") or "").strip().lower()
        discovery_context = str(row.get("discovery_context") or "").strip().lower()
        memberships = row.get("core_universe_memberships")
        membership_marker = bool(
            memberships
            and (
                not isinstance(memberships, str)
                or bool(memberships.strip())
            )
        )
        # Unmarked legacy rows remain usable only when they are genuinely
        # unlabeled.  A caller cannot strip the immutable receipt from a row
        # that still claims core provenance and then fall back to legacy rank.
        return not (
            universe_lane in {"core", "mover+core"}
            or evidence_lane == "core"
            or discovery_context.startswith("luna_core:")
            or membership_marker
        )
    receipt_id = str(row.get("core_coverage_receipt_id") or "").strip()
    receipt_hash = str(row.get("core_coverage_receipt_hash_sha256") or "").strip().lower()
    payload_text = row.get("core_coverage_receipt_payload_json")
    if not receipt_id or not _valid_digest(receipt_hash) or not isinstance(payload_text, str):
        return False
    try:
        receipt = json.loads(payload_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict):
        return False
    if (
        receipt.get("schema_version") != CORE_COVERAGE_RECEIPT_SCHEMA_VERSION
        or receipt.get("row_binding_schema_version")
        != CORE_COVERAGE_ROW_PROJECTION_SCHEMA_VERSION
        or receipt.get("coverage_receipt_hash_sha256") != receipt_hash
        or receipt.get("coverage_receipt_id") != receipt_id
        or receipt_hash != _coverage_receipt_digest(receipt)
        or receipt_id != "luna-core-coverage-" + receipt_hash[:24]
        or str(row.get("core_coverage_receipt_status") or "") != str(
            receipt.get("status") or ""
        )
    ):
        return False
    ticker = canonical_symbol(row.get("ticker") or row.get("symbol"))
    requested = {
        canonical_symbol(value) for value in receipt.get("requested_symbols") or []
    }
    eligible = {
        canonical_symbol(value) for value in receipt.get("eligible_symbols") or []
    }
    fresh_verified = {
        canonical_symbol(value) for value in receipt.get("fresh_verified_symbols") or []
    }
    duplicates = {
        canonical_symbol(value) for value in receipt.get("duplicate_symbols") or []
    }
    if (
        not ticker
        or ticker not in requested
        or ticker not in eligible
        or ticker not in fresh_verified
        or ticker in duplicates
    ):
        return False
    declared_memberships = receipt.get("requested_memberships")
    if not isinstance(declared_memberships, dict):
        return False
    expected_memberships = _canonical_row_memberships(declared_memberships.get(ticker))
    actual_memberships = _canonical_row_memberships(row.get("core_universe_memberships"))
    if expected_memberships != actual_memberships:
        return False
    binding_hash = str(row.get(CORE_COVERAGE_ROW_BINDING_FIELD) or "").strip().lower()
    if not _valid_digest(binding_hash):
        return False
    declared_hashes = receipt.get("row_binding_hashes")
    if not isinstance(declared_hashes, dict):
        return False
    if str(declared_hashes.get(ticker) or "").strip().lower() != binding_hash:
        return False
    row_bindings = receipt.get("row_bindings")
    if not isinstance(row_bindings, list):
        return False
    matching_bindings = [
        item
        for item in row_bindings
        if isinstance(item, dict)
        and canonical_symbol(item.get("ticker")) == ticker
        and str(item.get("row_binding_hash_sha256") or "").strip().lower()
        == binding_hash
    ]
    if len(matching_bindings) != 1:
        # A duplicate or ambiguous ticker must never be resolved by map
        # iteration order or by whichever provider row happened to win.
        return False
    if receipt.get("status") not in {"READY", "PARTIAL"}:
        return False
    try:
        fresh_verified_count = int(receipt.get("fresh_verified_count") or 0)
    except (TypeError, ValueError):
        return False
    if fresh_verified_count < 1:
        return False
    if str(row.get("source_quality_status") or "").upper() != "VERIFIED":
        return False
    freshness_status = str(row.get("freshness_status") or "").strip()
    if not freshness_status:
        receipt_observed_at = _parse_datetime(receipt.get("observed_at"))
        try:
            receipt_max_age = int(receipt.get("max_age_seconds") or 0)
        except (TypeError, ValueError):
            return False
        if receipt_observed_at is None or receipt_max_age <= 0:
            return False
        freshness_status = _snapshot_freshness_status(
            row,
            observed_at=receipt_observed_at,
            max_age_seconds=receipt_max_age,
        )
    if freshness_status.upper() != "FRESH":
        return False
    if _canonical_row_bool(row.get("stale_data_flag")) is not False:
        return False
    if _canonical_row_bool(row.get("core_lane_eligible")) is not True:
        return False
    if (
        core_row_binding_hash(row, freshness_status=freshness_status.upper())
        != binding_hash
    ):
        return False
    return True


def core_discovery_data_eligible(
    discovery: dict[str, Any] | None, *, minimum_fresh_rows: int = MIN_CORE_FRESH_ROWS
) -> bool:
    """Return whether a discovery result may feed the core research lane.

    Only a complete ``READY`` result or an explicitly partial result with the
    minimum number of fresh, verified rows qualifies.  This helper intentionally
    does not collapse ``PARTIAL`` into ``COMPLETE``; callers must carry the
    status and limitations into their lane/run/slate contracts.
    """

    payload = dict(discovery or {})
    status = str(payload.get("status") or "").upper()
    if status not in {"READY", "PARTIAL", "LIMITED"}:
        return False
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) < max(int(minimum_fresh_rows), 1):
        return False
    try:
        eligible_count = int(payload.get("eligible_count") or len(rows))
    except (TypeError, ValueError):
        return False
    if "fresh_verified_count" in payload:
        try:
            if int(payload.get("fresh_verified_count") or 0) < max(
                int(minimum_fresh_rows), 1
            ):
                return False
        except (TypeError, ValueError):
            return False
    return eligible_count >= max(int(minimum_fresh_rows), 1)


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
    return _read_manifest_path(Path(value), seen=set())


def _read_manifest_path(path: Path, *, seen: set[Path]) -> dict[str, Any]:
    """Read a manifest, resolving and authenticating an active generation pointer."""

    resolved_path = path.resolve()
    if resolved_path in seen:
        raise ValueError("universe manifest active pointer cycle")
    try:
        raw = path.read_bytes()
    except OSError:
        raise
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"universe manifest JSON invalid: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("universe manifest must be a JSON object")
    if parsed.get("schema_version") != ACTIVE_POINTER_SCHEMA_VERSION:
        return parsed
    target = _active_pointer_target(path, parsed)
    target_raw = target.read_bytes()
    expected_hash = str(parsed.get("manifest_sha256") or "").strip().lower()
    if not _valid_digest(expected_hash):
        raise ValueError("active pointer manifest SHA-256 missing or invalid")
    if hashlib.sha256(target_raw).hexdigest() != expected_hash:
        raise ValueError("active pointer manifest SHA-256 mismatch")
    try:
        target_parsed = json.loads(target_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"active target manifest JSON invalid: {exc}") from exc
    if not isinstance(target_parsed, dict):
        raise ValueError("active target manifest must be a JSON object")
    if target_parsed.get("schema_version") == ACTIVE_POINTER_SCHEMA_VERSION:
        return _read_manifest_path(target, seen={*seen, resolved_path})
    return target_parsed


def _active_pointer_target(path: Path, pointer: dict[str, Any]) -> Path:
    target_text = pointer.get("manifest_path")
    if not isinstance(target_text, str) or not target_text.strip():
        raise ValueError("active pointer manifest path missing")
    target_value = Path(target_text)
    if target_value.is_absolute():
        raise ValueError("active pointer manifest path must be relative")
    root = path.resolve().parent
    target = (root / target_value).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("active pointer target escapes config root") from exc
    if not target.is_file():
        raise ValueError(f"active pointer target missing: {target}")
    return target


def _read_manifest_entries(value: Any) -> list[dict[str, Any]]:
    """Read a manifest or an explicit wrapper of source manifests.

    Wrapper paths are useful for a derived index (for example a rebalance
    lineage): each child remains an independent source artifact and relative
    local artifact paths resolve beside the wrapper file.
    """

    base = Path(value).parent if isinstance(value, (str, Path)) else None
    if base is not None:
        pointer_path = Path(value)
        try:
            pointer_bytes = pointer_path.read_bytes()
            pointer = json.loads(pointer_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pointer = None
        if (
            isinstance(pointer, dict)
            and pointer.get("schema_version") == ACTIVE_POINTER_SCHEMA_VERSION
        ):
            base = _active_pointer_target(pointer_path, pointer).parent
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
    builder = str(lineage.get("builder_id") or lineage.get("builder") or "").strip()
    transformation = str(
        lineage.get("transformation_id") or lineage.get("transformation") or ""
    ).strip()
    reconstitution_id = str(
        lineage.get("reconstitution_id") or lineage.get("lineage_id") or ""
    ).strip()
    lineage_effective = _date_text(lineage.get("effective_date") or lineage.get("as_of_date"))
    input_hashes = lineage.get("input_artifact_hashes") or lineage.get("artifact_hashes")
    lineage_member_hash = str(
        lineage.get("canonical_member_set_hash_sha256")
        or lineage.get("member_set_hash_sha256")
        or ""
    ).lower()
    return (
        bool(
            schema
            and builder
            and transformation
            and reconstitution_id
            and lineage_effective
            and lineage_effective == effective_date
        )
        and isinstance(input_hashes, list)
        and [str(item).lower() for item in input_hashes] == artifact_hashes
        and lineage_member_hash == member_hash
    )


def _snapshot_freshness_status(
    row: dict[str, Any],
    *,
    observed_at: datetime,
    max_age_seconds: int,
) -> str:
    timestamp = _parse_datetime(row.get("source_timestamp") or row.get("as_of_timestamp"))
    if timestamp is None:
        return "UNKNOWN"
    age_seconds = (observed_at - timestamp).total_seconds()
    if age_seconds < -60:
        return "FUTURE"
    if age_seconds > max_age_seconds:
        return "STALE"
    return "FRESH"


def _coverage_receipt_digest(receipt: dict[str, Any]) -> str:
    """Hash receipt content without trusting caller-supplied identity fields."""

    payload = {
        key: value
        for key, value in receipt.items()
        if key not in {"coverage_receipt_id", "coverage_receipt_hash_sha256"}
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _validate_raw_artifact(manifest: dict[str, Any]) -> tuple[str, str | None]:
    """Compatibility wrapper over the one-read capture path."""

    entries = _declared_artifact_entries(manifest)
    declared = ""
    if entries:
        declared = (
            str(
                entries[0].get("sha256")
                or entries[0].get("raw_artifact_sha256")
                or entries[0].get("raw_artifact_hash")
                or ""
            )
            .strip()
            .lower()
        )
    hashes, errors, _captured = _capture_raw_artifacts(manifest)
    return (hashes[0] if hashes else declared), (errors[0] if errors else None)


def _capture_raw_artifacts(
    manifest: dict[str, Any],
) -> tuple[list[str], list[str], list[bytes]]:
    """Capture, hash, and validate each source artifact exactly once.

    The returned byte snapshots are the only bytes the production source
    binding/replay path may consume.  Keeping the hash and parser input in one
    tuple prevents a source path that changes between reads from becoming a
    falsely trusted READY manifest.
    """

    entries = _declared_artifact_entries(manifest)
    if not entries:
        return [], ["raw_artifact_entries_missing"], []
    if (
        len(entries) == 1
        and set(entries[0]) == {"sha256"}
        and not (manifest.get("source_artifacts") or manifest.get("raw_artifacts"))
    ):
        return [], ["raw_artifact_bytes_missing"], []
    hashes: list[str] = []
    errors: list[str] = []
    captured: list[bytes] = []
    for number, entry in enumerate(entries, start=1):
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
        artifact_bytes: bytes | None = None
        if not _valid_digest(digest):
            errors.append(f"raw_artifact_sha256_missing_or_invalid:{number}")
            continue
        if path:
            artifact_path = Path(path)
            if not artifact_path.is_file():
                errors.append(f"raw_artifact_missing:{number}")
                continue
            try:
                # This is the one and only read of this declared path for the
                # entire build.  Hashing and replay use this same object.
                artifact_bytes = artifact_path.read_bytes()
            except OSError:
                errors.append(f"raw_artifact_unreadable:{number}")
                continue
        elif isinstance(content, bytes):
            artifact_bytes = content
        elif isinstance(content, str):
            artifact_bytes = content.encode("utf-8")
        else:
            errors.append(f"raw_artifact_bytes_missing:{number}")
            continue
        actual = hashlib.sha256(artifact_bytes).hexdigest()
        if actual != digest:
            errors.append(f"raw_artifact_sha256_mismatch:{number}")
            continue
        declared_size = entry.get("byte_count")
        if declared_size is not None:
            try:
                if isinstance(declared_size, bool) or int(declared_size) != len(artifact_bytes):
                    errors.append(f"raw_artifact_byte_count_mismatch:{number}")
                    continue
            except (TypeError, ValueError):
                errors.append(f"raw_artifact_byte_count_invalid:{number}")
                continue
        hashes.append(digest)
        captured.append(artifact_bytes)
    return hashes, errors, captured


def _validate_raw_artifacts(manifest: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate every raw artifact receipt, including rebalance lineages."""

    hashes, errors, _captured = _capture_raw_artifacts(manifest)
    return hashes, errors


def _nasdaq_sod_url_for_date(market_date: str) -> str:
    """Render the authenticated Nasdaq SOD URL for one requested session."""

    parsed = date.fromisoformat(market_date)
    return NASDAQ_NDX_SOD_URL_TEMPLATE.format(
        month=f"{parsed.month:02d}",
        day=f"{parsed.day:02d}",
        year=f"{parsed.year:04d}",
    )


def _trusted_source_uri(root: dict[str, Any], market_date: str | None) -> str:
    template = str(root.get("source_uri_template") or "").strip()
    if template and market_date:
        try:
            return _nasdaq_sod_url_for_date(market_date)
        except ValueError:
            return ""
    return str(root.get("source_uri") or "").strip()


def _trusted_source_scope(root: dict[str, Any], market_date: str | None) -> str:
    template = str(root.get("source_scope_template") or "").strip()
    if template and market_date:
        return template.format(market_date=market_date)
    return str(root.get("source_scope") or "").strip()


def _validate_source_binding(
    manifest: dict[str, Any],
    *,
    index_name: str,
    effective_date: str | None,
    requested_date: str | None,
    artifact_hashes: list[str],
    artifact_bytes: list[bytes],
    declared_members: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Replay a release-trusted source artifact and compare exact members.

    Member-set and raw-byte hashes supplied by the same manifest are not a
    trust root: an operator could change both and recompute both digests. The
    currently supported production sources therefore have code-pinned roots
    (source identity, authenticated URI, stable workbook content, transformer,
    and an anchored effective date). A later requested date is admissible only
    for a root explicitly governed for same-semantic-set continuity, and only
    after the current source's strict structure and canonical ticker set replay
    exactly. The transformer then derives membership from the already captured
    bytes and compares the full canonical rows.
    """

    source_id = str(manifest.get("source_id") or manifest.get("id") or "").strip()
    root = _TRUSTED_SOURCE_ROOTS.get(source_id)
    binding: dict[str, Any] = {
        "status": "BLOCKED",
        "authority": "release_trust_root",
        "index": root.get("index") if root else index_name,
        "membership_authority": root.get("membership_authority") if root else None,
        "official_index_authority": root.get("official_index_authority") if root else None,
        "source_scope": (
            _trusted_source_scope(root, requested_date or effective_date) if root else None
        ),
        "source_id": source_id or None,
        "transformation_id": root.get("transformation_id") if root else None,
        "derived_effective_date": None,
        "derived_member_set_hash_sha256": None,
        "derived_membership_count": 0,
    }
    errors: list[str] = []
    if root is None:
        return binding, ["source_binding_trust_root_unknown"]
    root_effective = _date_text(root.get("effective_date"))
    same_set_dates = bool(root.get("allow_future_same_semantic_set_dates"))
    requested_after_root = bool(
        requested_date and root_effective and requested_date > root_effective
    )
    effective_after_root = bool(
        effective_date and root_effective and effective_date > root_effective
    )
    recurring_allowed = same_set_dates and requested_after_root
    if index_name != root["index"]:
        errors.append("source_binding_index_mismatch")
    declared_index_label = str(manifest.get("index_name") or manifest.get("index") or "").strip()
    if declared_index_label != str(root["index"]):
        errors.append("source_binding_index_label_not_trusted")
    trusted_uri = _trusted_source_uri(root, requested_date or effective_date)
    source_uri = str(
        manifest.get("source_uri") or manifest.get("uri") or manifest.get("url") or ""
    ).strip()
    if trusted_uri and source_uri != trusted_uri:
        errors.append("source_binding_source_uri_not_trusted")
    if trusted_uri:
        artifact_uris = {
            str(entry.get("uri") or entry.get("url") or "").strip()
            for entry in _declared_artifact_entries(manifest)
        }
        if artifact_uris != {trusted_uri}:
            errors.append("source_binding_artifact_uri_not_trusted")
    if effective_date != root_effective:
        if index_name == "Nasdaq-100":
            # The official SOD workbook is the exact requested-date input.
            if not (
                recurring_allowed and effective_date == requested_date and effective_after_root
            ):
                errors.append("source_binding_effective_date_not_trusted")
        elif index_name == "S&P 500":
            # State Street embeds the holdings' as-of date.  A fresh daily
            # download may be one or more trading sessions behind the request,
            # but it must not predate the release anchor or be future-dated.
            maximum_age = int(root.get("maximum_source_age_days") or 0)
            source_age = (
                (date.fromisoformat(requested_date) - date.fromisoformat(effective_date)).days
                if requested_date and effective_date
                else -1
            )
            if not (
                recurring_allowed
                and effective_date
                and root_effective
                and root_effective <= effective_date <= (requested_date or effective_date)
                and maximum_age > 0
                and 0 <= source_age <= maximum_age
            ):
                errors.append("source_binding_effective_date_not_trusted")
        else:
            errors.append("source_binding_effective_date_not_trusted")
    if requested_date and effective_date and effective_date > requested_date:
        errors.append("source_binding_effective_date_after_market_date")
    trusted_scope = _trusted_source_scope(root, requested_date or effective_date)
    declared_scope = str(manifest.get("source_scope") or "").strip()
    if trusted_scope and declared_scope != trusted_scope:
        errors.append("source_binding_source_scope_not_trusted")
    declared_reconstitution = str(manifest.get("reconstitution_id") or "").strip()
    if declared_reconstitution != str(root.get("reconstitution_id") or "").strip():
        errors.append("source_binding_reconstitution_id_not_trusted")
    trusted_raw_hashes = root.get("raw_artifact_hashes")
    if trusted_raw_hashes and list(artifact_hashes) != list(trusted_raw_hashes):
        errors.append("source_binding_raw_artifact_hashes_not_trusted")
    lineage = (
        manifest.get("reconstitution_lineage")
        or manifest.get("point_in_time_lineage")
        or manifest.get("reconstitution")
    )
    if not isinstance(lineage, dict):
        errors.append("source_binding_lineage_missing")
    else:
        expected_schema = str(
            root.get("lineage_schema_version") or "dawnstrike.core_universe_lineage.v1"
        ).strip()
        if str(lineage.get("schema_version") or "").strip() != expected_schema:
            errors.append("source_binding_lineage_schema_mismatch")
        if str(lineage.get("builder_id") or "").strip() != root["lineage_builder_id"]:
            errors.append("source_binding_lineage_builder_mismatch")
        if str(lineage.get("transformation_id") or "").strip() != root["lineage_transformation_id"]:
            errors.append("source_binding_lineage_transformation_mismatch")
        if str(lineage.get("reconstitution_id") or "").strip() != root["reconstitution_id"]:
            errors.append("source_binding_lineage_reconstitution_mismatch")
        if _date_text(lineage.get("effective_date")) != effective_date:
            errors.append("source_binding_lineage_effective_date_mismatch")
        lineage_input_hashes = lineage.get("input_artifact_hashes")
        if (
            not isinstance(lineage_input_hashes, list)
            or [str(item).lower() for item in lineage_input_hashes] != artifact_hashes
        ):
            errors.append("source_binding_lineage_input_hashes_mismatch")
        lineage_member_hash = str(lineage.get("canonical_member_set_hash_sha256") or "").lower()
        if lineage_member_hash != _canonical_member_hash(declared_members):
            errors.append("source_binding_lineage_member_set_mismatch")
    trusted_sizes = root.get("raw_artifact_byte_counts")
    if (
        isinstance(trusted_sizes, (list, tuple))
        and not root.get("canonical_content_digest_sha256")
        and list(map(len, artifact_bytes)) != list(trusted_sizes)
    ):
        errors.append("source_binding_raw_artifact_sizes_not_trusted")
    if not artifact_bytes:
        return binding, [*errors, "source_binding_artifacts_missing"]
    if (
        index_name == "Nasdaq-100"
        and root.get("transformation_id") == "nasdaq-ndx-sod-weightings-parser-v1"
        and len(artifact_bytes) != 1
    ):
        return binding, [*errors, "source_binding_artifact_count_not_trusted"]
    try:
        if index_name == "S&P 500":
            derived_members, derived_effective, holdings_attestation = (
                _parse_spy_holdings_xlsx_with_attestation(artifact_bytes)
            )
            binding["workbook_attestation"] = holdings_attestation
            expected_names = root.get("canonical_zip_member_names")
            if expected_names and list(holdings_attestation["member_names"]) != list(
                expected_names
            ):
                errors.append("source_binding_workbook_structure_not_trusted")
            expected_static = root.get("canonical_static_member_hashes")
            if expected_static and holdings_attestation["static_member_hashes"] != dict(
                expected_static
            ):
                errors.append("source_binding_workbook_members_not_trusted")
            expected_schema = str(root.get("canonical_schema_digest_sha256") or "").lower()
            if expected_schema and holdings_attestation["schema_digest_sha256"] != expected_schema:
                errors.append("source_binding_workbook_schema_not_trusted")
            expected_content = str(root.get("canonical_content_digest_sha256") or "").lower()
            if (
                expected_content
                and holdings_attestation["content_digest_sha256"] != expected_content
            ):
                errors.append("source_binding_workbook_content_not_trusted")
            expected_symbol_set = str(root.get("canonical_symbol_set_hash_sha256") or "").lower()
            if expected_symbol_set and (
                holdings_attestation["symbol_set_hash_sha256"] != expected_symbol_set
            ):
                errors.append("source_binding_member_set_not_trusted")
            declared_names = manifest.get("canonical_zip_member_names")
            if declared_names is not None and list(holdings_attestation["member_names"]) != list(
                declared_names
            ):
                errors.append("source_binding_declared_workbook_structure_mismatch")
            declared_static = manifest.get("canonical_static_member_hashes")
            if declared_static is not None and holdings_attestation["static_member_hashes"] != dict(
                declared_static
            ):
                errors.append("source_binding_declared_workbook_members_mismatch")
            declared_schema = str(manifest.get("canonical_schema_digest_sha256") or "").lower()
            if declared_schema and holdings_attestation["schema_digest_sha256"] != declared_schema:
                errors.append("source_binding_declared_workbook_schema_mismatch")
            declared_content = str(manifest.get("canonical_content_digest_sha256") or "").lower()
            if (
                declared_content
                and holdings_attestation["content_digest_sha256"] != declared_content
            ):
                errors.append("source_binding_declared_workbook_content_mismatch")
            declared_symbol_set = str(
                manifest.get("canonical_symbol_set_hash_sha256") or ""
            ).lower()
            if (
                declared_symbol_set
                and holdings_attestation["symbol_set_hash_sha256"] != declared_symbol_set
            ):
                errors.append("source_binding_declared_member_set_mismatch")
        elif index_name == "Nasdaq-100":
            if root.get("transformation_id") == "nasdaq-ndx-sod-weightings-parser-v1":
                derived_members, workbook_attestation = (
                    _parse_nasdaq_sod_weightings_xlsx_with_attestation(
                        artifact_bytes[0], effective_date=effective_date or root_effective
                    )
                )
                binding["workbook_attestation"] = workbook_attestation
                expected_names = root.get("canonical_zip_member_names")
                if expected_names and list(workbook_attestation["member_names"]) != list(
                    expected_names
                ):
                    errors.append("source_binding_workbook_structure_not_trusted")
                declared_names = manifest.get("canonical_zip_member_names")
                if declared_names is not None and list(
                    workbook_attestation["member_names"]
                ) != list(declared_names):
                    errors.append("source_binding_declared_workbook_structure_mismatch")
                expected_hashes = root.get("canonical_zip_member_hashes")
                expected_static = root.get("canonical_static_member_hashes")
                if expected_static and workbook_attestation["static_member_hashes"] != dict(
                    expected_static
                ):
                    errors.append("source_binding_workbook_static_members_not_trusted")
                declared_static = manifest.get("canonical_static_member_hashes")
                if declared_static is not None and workbook_attestation[
                    "static_member_hashes"
                ] != dict(declared_static):
                    errors.append("source_binding_declared_workbook_static_members_mismatch")
                if (
                    effective_date == root_effective
                    and expected_hashes
                    and workbook_attestation["member_hashes"] != dict(expected_hashes)
                ):
                    errors.append("source_binding_workbook_members_not_trusted")
                declared_hashes = manifest.get("canonical_zip_member_hashes")
                if declared_hashes is not None and workbook_attestation["member_hashes"] != dict(
                    declared_hashes
                ):
                    errors.append("source_binding_declared_workbook_members_mismatch")
                expected_content = str(root.get("canonical_content_digest_sha256") or "").lower()
                if (
                    effective_date == root_effective
                    and expected_content
                    and workbook_attestation["content_digest_sha256"] != expected_content
                ):
                    errors.append("source_binding_workbook_content_not_trusted")
                declared_content = str(
                    manifest.get("canonical_content_digest_sha256") or ""
                ).lower()
                if (
                    declared_content
                    and workbook_attestation["content_digest_sha256"] != declared_content
                ):
                    errors.append("source_binding_declared_workbook_content_mismatch")
                expected_symbol_set = str(
                    root.get("canonical_symbol_set_hash_sha256") or ""
                ).lower()
                if expected_symbol_set:
                    if workbook_attestation.get("symbol_set_hash_sha256") != expected_symbol_set:
                        errors.append("source_binding_member_set_not_trusted")
                elif effective_date != root_effective and recurring_allowed:
                    errors.append("source_binding_currentness_root_missing")
                expected_member_set = str(
                    root.get("canonical_member_set_hash_sha256") or ""
                ).lower()
                if expected_member_set and effective_date == root_effective:
                    derived_set = _canonical_member_hash(
                        [
                            {
                                "symbol": symbol,
                                "provider_symbol": symbol,
                                "asset_class": "common_stock",
                                "index": "Nasdaq-100",
                                "valid_from": effective_date,
                                "valid_to": None,
                            }
                            for symbol in derived_members
                        ]
                    )
                    if derived_set != expected_member_set:
                        errors.append("source_binding_member_set_not_trusted")
                declared_member_set = str(
                    manifest.get("canonical_member_set_hash_sha256") or ""
                ).lower()
                if (
                    declared_member_set
                    and workbook_attestation["member_set_hash_sha256"] != declared_member_set
                ):
                    errors.append("source_binding_declared_member_set_mismatch")
                derived_effective = effective_date
            else:
                derived_members, derived_effective = _replay_nasdaq_reconstitution(artifact_bytes)
        else:
            return binding, ["source_binding_transformer_unavailable"]
    except (OSError, ValueError, TypeError, zipfile.BadZipFile) as exc:
        return binding, [f"source_binding_replay_failed:{exc}"]

    derived = [
        {
            "symbol": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index": index_name,
            "valid_from": derived_effective,
            "valid_to": None,
        }
        for symbol in derived_members
    ]
    binding["derived_effective_date"] = derived_effective
    binding["derived_membership_count"] = len(derived)
    binding["derived_member_set_hash_sha256"] = _canonical_member_hash(derived)
    if derived_effective != effective_date:
        errors.append("source_binding_effective_date_mismatch")
    if _canonical_member_hash(derived) != _canonical_member_hash(declared_members):
        errors.append("source_binding_membership_mismatch")
    if not errors:
        binding["status"] = "VERIFIED"
    return binding, errors


def _declared_artifact_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    entries = manifest.get("source_artifacts") or manifest.get("raw_artifacts")
    if isinstance(entries, dict):
        entries = list(entries.values())
    if isinstance(entries, list):
        return [dict(entry) for entry in entries if isinstance(entry, dict)]
    artifact = manifest.get("raw_artifact") or manifest.get("raw_artifact_path")
    if isinstance(artifact, dict):
        return [dict(artifact)]
    if artifact:
        entry: dict[str, Any] = {"path": artifact}
        digest = (
            manifest.get("raw_artifact_sha256")
            or manifest.get("raw_sha256")
            or manifest.get("raw_artifact_hash_sha256")
            or manifest.get("raw_artifact_hash")
        )
        if digest:
            entry["sha256"] = digest
        return [entry]
    if isinstance(manifest.get("raw_artifact_content"), str):
        entry = {"content": manifest["raw_artifact_content"]}
        digest = (
            manifest.get("raw_artifact_sha256")
            or manifest.get("raw_sha256")
            or manifest.get("raw_artifact_hash_sha256")
            or manifest.get("raw_artifact_hash")
        )
        if digest:
            entry["sha256"] = digest
        return [entry]
    digest = (
        manifest.get("raw_artifact_sha256")
        or manifest.get("raw_sha256")
        or manifest.get("raw_artifact_hash_sha256")
        or manifest.get("raw_artifact_hash")
    )
    if digest:
        return [{"sha256": digest}]
    return []


def _replay_spy_holdings_xlsx(payloads: list[bytes]) -> tuple[list[str], str]:
    """Extract the exact 503 common-stock rows from the State Street XLSX."""

    symbols, effective, _attestation = _parse_spy_holdings_xlsx_with_attestation(payloads)
    return symbols, effective


def _parse_spy_holdings_xlsx_with_attestation(
    payloads: list[bytes],
) -> tuple[list[str], str, dict[str, Any]]:
    """Replay one SPY workbook and return stable package/set attestations."""

    if len(payloads) != 1:
        raise ValueError("SPY transformer requires one XLSX artifact")
    with zipfile.ZipFile(io.BytesIO(payloads[0])) as archive:
        names = tuple(sorted(archive.namelist()))
        if len(names) != len(set(names)):
            raise ValueError("SPY workbook contains duplicate members")
        try:
            contents = {name: archive.read(name) for name in names}
        except KeyError as exc:
            raise ValueError("SPY XLSX worksheet/shared strings missing") from exc
    for name, content in contents.items():
        if name.endswith(".xml"):
            try:
                ElementTree.fromstring(content)
            except ElementTree.ParseError as exc:
                raise ValueError("SPY XLSX workbook XML invalid") from exc
    try:
        shared = ElementTree.fromstring(contents["xl/sharedStrings.xml"])
        sheet = ElementTree.fromstring(contents["xl/worksheets/sheet1.xml"])
    except KeyError as exc:
        raise ValueError("SPY XLSX worksheet/shared strings missing") from exc
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings = ["".join(item.itertext()) for item in shared.findall("main:si", namespace)]
    rows: list[tuple[int, dict[str, str]]] = []
    for row in sheet.findall(".//main:row", namespace):
        try:
            row_number = int(row.attrib.get("r", "0"))
        except (TypeError, ValueError):
            continue
        values: dict[str, str] = {}
        for cell in row.findall("main:c", namespace):
            ref = str(cell.attrib.get("r") or "")
            column = re.match(r"[A-Z]+", ref)
            if column is None:
                continue
            value = cell.find("main:v", namespace)
            text = value.text if value is not None and value.text is not None else ""
            if cell.attrib.get("t") == "s" and text:
                try:
                    text = strings[int(text)]
                except (IndexError, ValueError):
                    raise ValueError("SPY shared-string index invalid") from None
            elif cell.attrib.get("t") == "inlineStr":
                inline = cell.find("main:is", namespace)
                text = "".join(inline.itertext()) if inline is not None else ""
            values[column.group()] = text
        rows.append((row_number, values))
    header_number = next(
        (number for number, values in rows if values.get("B", "").strip() == "Ticker"),
        None,
    )
    if header_number != 5:
        raise ValueError("SPY ticker header must be row 5")
    holdings = next(
        (values.get("B", "") for number, values in rows if number == 3),
        "",
    )
    date_match = re.search(
        r"As of\s+(\d{1,2})-([A-Za-z]{3})-(\d{4})", holdings, flags=re.IGNORECASE
    )
    if date_match is None:
        raise ValueError("SPY holdings date missing")
    try:
        effective = datetime.strptime("-".join(date_match.groups()), "%d-%b-%Y").date().isoformat()
    except ValueError as exc:
        raise ValueError("SPY holdings date invalid") from exc
    # The source sheet has a contiguous holdings block.  It intentionally
    # contains two non-equity rows (US DOLLAR and a contra line); both are
    # checked explicitly so a changed layout cannot silently become a valid
    # membership set.
    row_map = {number: values for number, values in rows}
    expected_rows = list(range(6, 511))
    if any(number not in row_map for number in expected_rows):
        raise ValueError("SPY holdings rows 6-510 are incomplete")
    holding_rows = [row_map[number] for number in expected_rows]
    invalid = [
        row.get("B", "").strip().upper()
        for row in holding_rows
        if not _SYMBOL_PATTERN.fullmatch(row.get("B", "").strip().upper())
    ]
    if sorted(invalid) != ["-", "2602335D"]:
        raise ValueError("SPY non-equity exclusion rows changed")
    symbols = [
        row["B"].strip().upper()
        for row in holding_rows
        if _SYMBOL_PATTERN.fullmatch(row.get("B", "").strip().upper())
    ]
    if len(symbols) != 503 or len(set(symbols)) != len(symbols):
        raise ValueError("SPY membership count or uniqueness invalid")
    static_member_hashes = {
        name: hashlib.sha256(contents[name]).hexdigest()
        for name in names
        if name in _SPY_CANONICAL_STATIC_MEMBER_NAMES
    }
    attestation: dict[str, Any] = {
        "member_names": list(names),
        "static_member_hashes": static_member_hashes,
        "schema_digest_sha256": _canonical_zip_content_digest(
            {
                **static_member_hashes,
                **{
                    name: "dynamic"
                    for name in names
                    if name not in _SPY_CANONICAL_STATIC_MEMBER_NAMES
                },
            }
        ),
        "symbol_set_hash_sha256": _canonical_symbol_set_hash(symbols, "S&P 500"),
    }
    # This is intentionally stable across the daily as-of date and holdings
    # weights.  It binds the complete known package structure plus the exact
    # canonical ticker set that the transformer contributes to the proxy.
    attestation["content_digest_sha256"] = _canonical_zip_content_digest(
        {
            **static_member_hashes,
            "canonical_symbol_set_sha256": attestation["symbol_set_hash_sha256"],
        }
    )
    return symbols, effective, attestation


def _xlsx_cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    """Decode one XLSX cell without coercing malformed values into truth."""

    value = cell.find(
        "main:v", {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    )
    text = value.text if value is not None and value.text is not None else ""
    if cell.attrib.get("t") == "s" and text:
        try:
            text = shared_strings[int(text)]
        except (IndexError, ValueError):
            raise ValueError("Nasdaq SOD shared-string index invalid") from None
    elif cell.attrib.get("t") == "inlineStr":
        inline = cell.find(
            "main:is", {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        )
        text = "".join(inline.itertext()) if inline is not None else ""
    return str(text).strip()


def _canonical_zip_content_digest(member_hashes: dict[str, str]) -> str:
    canonical = {
        "members": [{"name": name, "sha256": member_hashes[name]} for name in sorted(member_hashes)]
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_nasdaq_sod_weightings_xlsx_with_attestation(
    payload: bytes,
    *,
    effective_date: str = "2026-08-27",
) -> tuple[list[str], dict[str, Any]]:
    """Parse and attest the exact authenticated Nasdaq SOD workbook shape."""

    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = tuple(sorted(archive.namelist()))
            if len(names) != len(set(names)):
                raise ValueError("Nasdaq SOD workbook contains duplicate members")
            if names != _NDX_CANONICAL_ZIP_MEMBER_NAMES:
                raise ValueError("Nasdaq SOD workbook structure is unknown")
            contents = {name: archive.read(name) for name in names}
    except zipfile.BadZipFile as exc:
        raise ValueError("Nasdaq SOD export is not a valid XLSX") from exc
    except KeyError as exc:
        raise ValueError("Nasdaq SOD workbook member missing") from exc

    # Parse every XML member before using any cell.  This both rejects a
    # malformed unused member and makes the stable digest cover the complete
    # workbook, not merely the two cells used for membership.
    for content in contents.values():
        try:
            ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise ValueError("Nasdaq SOD workbook XML invalid") from exc
    member_hashes = {name: hashlib.sha256(contents[name]).hexdigest() for name in names}
    attestation: dict[str, Any] = {
        "member_names": list(names),
        "member_hashes": member_hashes,
        "static_member_hashes": {
            name: member_hashes[name] for name in names if name not in _NDX_DYNAMIC_MEMBER_NAMES
        },
        "content_digest_sha256": _canonical_zip_content_digest(member_hashes),
    }
    shared_root = ElementTree.fromstring(contents["xl/sharedStrings.xml"])
    shared = [
        "".join(item.itertext()).strip() for item in shared_root.findall("main:si", namespace)
    ]
    sheet_root = ElementTree.fromstring(contents["xl/worksheets/sheet1.xml"])

    rows: dict[int, dict[str, str]] = {}
    for row in sheet_root.findall(".//main:row", namespace):
        try:
            number = int(row.attrib.get("r", "0"))
        except (TypeError, ValueError):
            continue
        if number <= 0:
            raise ValueError("Nasdaq SOD row number invalid")
        values: dict[str, str] = {}
        for cell in row.findall("main:c", namespace):
            reference = str(cell.attrib.get("r") or "")
            column_match = re.match(r"[A-Z]+", reference)
            if column_match is None:
                raise ValueError("Nasdaq SOD cell reference invalid")
            values[column_match.group()] = _xlsx_cell_text(cell, shared)
        rows[number] = values

    headers = [
        number
        for number, values in rows.items()
        if values.get("A", "").casefold() == "company name"
        and values.get("B", "").casefold() == "security symbol"
    ]
    if len(headers) != 1:
        raise ValueError("Nasdaq SOD header schema invalid")
    header = headers[0]
    symbols: list[str] = []
    row_number = header + 1
    while True:
        values = rows.get(row_number)
        if values is None:
            raise ValueError("Nasdaq SOD member rows are not contiguous")
        company = values.get("A", "").strip()
        symbol = canonical_symbol(values.get("B", ""))
        if not company and not symbol:
            break
        if not company or not symbol or not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("Nasdaq SOD member row schema invalid")
        symbols.append(symbol)
        row_number += 1
    # A later populated row indicates an inserted gap or footer that was
    # mistaken for a valid terminator.
    if any(
        (values.get("A", "").strip() or values.get("B", "").strip())
        for number, values in rows.items()
        if number > row_number
    ):
        raise ValueError("Nasdaq SOD rows continue after member block")
    if len(symbols) != 102 or len(set(symbols)) != len(symbols):
        raise ValueError("Nasdaq SOD membership count or uniqueness invalid")
    attestation["member_set_hash_sha256"] = _canonical_member_hash(
        [
            {
                "symbol": symbol,
                "provider_symbol": symbol,
                "asset_class": "common_stock",
                "index": "Nasdaq-100",
                "valid_from": effective_date,
                "valid_to": None,
            }
            for symbol in symbols
        ]
    )
    attestation["symbol_set_hash_sha256"] = _canonical_symbol_set_hash(symbols, "Nasdaq-100")
    return symbols, attestation


def _parse_nasdaq_sod_weightings_xlsx(payload: bytes) -> list[str]:
    """Parse the official Nasdaq SOD export's strict two-column schema."""

    symbols, _attestation = _parse_nasdaq_sod_weightings_xlsx_with_attestation(payload)
    return symbols


def parse_nasdaq_sod_weightings_xlsx(payload: bytes) -> list[str]:
    """Parse a governed Nasdaq SOD workbook for callers building evidence."""

    return _parse_nasdaq_sod_weightings_xlsx(payload)


def _replay_nasdaq_reconstitution(payloads: list[bytes]) -> tuple[list[str], str]:
    """Replay the NDX base PDF and ordered Nasdaq notice deltas."""

    if len(payloads) != 4:
        raise ValueError("Nasdaq transformer requires base PDF plus three notices")
    base_symbols, base_date = _extract_ndx_pdf_symbols(payloads[0])
    symbols = list(base_symbols)
    effective = base_date
    for payload in payloads[1:]:
        additions, removals, notice_date = _extract_nasdaq_notice(payload)
        if not additions:
            raise ValueError("Nasdaq notice has no additions")
        for symbol in removals:
            if symbol not in symbols:
                raise ValueError(f"Nasdaq notice removes absent symbol: {symbol}")
            symbols.remove(symbol)
        for symbol in additions:
            if symbol in symbols:
                raise ValueError(f"Nasdaq notice adds existing symbol: {symbol}")
            symbols.append(symbol)
        if notice_date <= effective:
            raise ValueError("Nasdaq notice effective dates are not increasing")
        effective = notice_date
    if len(symbols) != 102 or len(set(symbols)) != len(symbols):
        raise ValueError("Nasdaq replay membership count or uniqueness invalid")
    return symbols, effective


def _extract_ndx_pdf_symbols(payload: bytes) -> tuple[list[str], str]:
    """Read text operators from Flate streams without trusting PDF metadata."""

    import zlib

    streams: list[bytes] = []
    for match in re.finditer(b"stream(?:\\r\\n|\\n|\\r)", payload):
        end = payload.find(b"endstream", match.end())
        if end < 0:
            continue
        try:
            streams.append(zlib.decompress(payload[match.end() : end]))
        except zlib.error:
            continue
    if not streams:
        raise ValueError("NDX PDF has no readable Flate streams")
    text = b"\n".join(streams).decode("latin-1", errors="strict")
    cleaned = text.replace("\\n", " ")
    # Each table cell is emitted as a literal (...) Tj operator.  Looking for
    # an uppercase ticker followed by a decimal weight avoids treating company
    # names as symbols, while retaining dual-class symbols.
    literals = [value.strip() for value in re.findall(r"\(([^()]*)\)Tj", cleaned) if value.strip()]
    # The text layer is a sequence of company-name, ticker, and weight cells.
    # A few names are split across cells (COCA/-/COLA, T-MOBILE, TAKE-TWO),
    # so use the ticker immediately followed by a decimal weight rather than
    # assuming every preceding cell is a name.
    symbols: list[str] = []
    for index, value in enumerate(literals[:-1]):
        if _SYMBOL_PATTERN.fullmatch(value) and re.fullmatch(
            r"[0-9]+\.[0-9]+", literals[index + 1]
        ):
            symbols.append(value)
    if len(symbols) != 101 or len(set(symbols)) != 101:
        raise ValueError(f"NDX base membership count invalid: {len(symbols)}")
    date_match = re.search(r"Data as of:.*?([0-9]{2}/[0-9]{2}/[0-9]{4})", text, flags=re.DOTALL)
    if date_match is None:
        raise ValueError("NDX base effective date missing")
    try:
        effective = datetime.strptime(date_match.group(1), "%m/%d/%Y").date().isoformat()
    except ValueError as exc:
        raise ValueError("NDX base effective date invalid") from exc
    return symbols, effective


def _extract_nasdaq_notice(payload: bytes) -> tuple[list[str], list[str], str]:
    text = html.unescape(payload.decode("utf-8", errors="strict"))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    start = text.lower().find("today announced")
    end = text.lower().find("for additional information", start)
    if start < 0 or end < 0:
        raise ValueError("Nasdaq notice announcement body missing")
    body = text[start:end]
    date_match = re.search(
        r"(?:prior to market open on|effective[^,]{0,80}on)\s+"
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday),\s+"
        r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
        body,
    )
    if date_match is None:
        raise ValueError("Nasdaq notice effective date missing")
    try:
        effective = datetime.strptime(date_match.group(1), "%B %d, %Y").date().isoformat()
    except ValueError as exc:
        raise ValueError("Nasdaq notice effective date invalid") from exc
    added: list[str] = []
    removed: list[str] = []
    lower = body.lower()
    added_marker = lower.find("added to the index")
    removed_marker = lower.find("removed from the index")
    if added_marker >= 0 and removed_marker > added_marker:
        added = re.findall(r"Nasdaq:\s*([A-Z][A-Z0-9.-]{0,14})", body[added_marker:removed_marker])
        removed = re.findall(r"Nasdaq:\s*([A-Z][A-Z0-9.-]{0,14})", body[removed_marker:])
    else:
        replacement = re.search(
            r"Nasdaq:\s*([A-Z][A-Z0-9.-]{0,14}).{0,600}?replacing.{0,600}?Nasdaq:\s*([A-Z][A-Z0-9.-]{0,14})",
            body,
            flags=re.IGNORECASE,
        )
        if replacement:
            added = [replacement.group(1)]
            removed = [replacement.group(2)]
        else:
            added = re.findall(r"Nasdaq:\s*([A-Z][A-Z0-9.-]{0,14})", body)
    if not added or len(set(added)) != len(added) or len(set(removed)) != len(removed):
        raise ValueError("Nasdaq notice delta is invalid")
    return added, removed, effective


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


def _canonical_symbol_set_hash(symbols: Iterable[str], index: str) -> str:
    """Hash a semantic ticker set without volatile validity/as-of fields."""

    canonical = {
        "index": _index_name(index) or str(index),
        "symbols": sorted({canonical_symbol(symbol) for symbol in symbols}),
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
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
    "CORE_COVERAGE_RECEIPT_SCHEMA_VERSION",
    "CORE_COVERAGE_ROW_BINDING_FIELD",
    "CORE_COVERAGE_ROW_PROJECTION_SCHEMA_VERSION",
    "MIN_CORE_FRESH_ROWS",
    "NASDAQ_NDX_SOD_2026_08_27_URL",
    "NASDAQ_NDX_SOD_URL_TEMPLATE",
    "SCHEMA_VERSION",
    "STATE_STREET_SPY_HOLDINGS_URL",
    "build_core_universe_contract",
    "build_core_row_binding_projection",
    "canonical_symbol",
    "core_row_binding_hash",
    "core_discovery_data_eligible",
    "discover_core_universe_rows",
    "merge_core_universe_rows",
    "parse_nasdaq_sod_weightings_xlsx",
    "rank_core_universe_rows",
    "read_core_universe_manifest",
    "write_core_universe_contract",
    "write_snapshot_rows",
]
