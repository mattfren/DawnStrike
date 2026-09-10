"""OPS05 delayed bars to the authenticated R3 observation consumer.

The adapter is deliberately label-only.  It verifies the immutable OPS05
receipt and raw bytes, maps bars to the existing observation event contract,
and delegates maturity and daily/weekly handling to the existing V6 consumer.
It never turns delayed bars into timely features, quote truth, fills, or PnL.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.v6.observation_dataset import build_observation_dataset

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HORIZONS = (60, 240, 600)


class Ops06AdapterError(ValueError):
    """The OPS05-to-R3 handoff cannot be authenticated or typed."""


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_json(value: Any) -> str:
    return _sha_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise Ops06AdapterError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _finite(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise Ops06AdapterError("bar price must be finite")
    return result


def _decisions(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("v6_decision_records")
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise Ops06AdapterError("decision artifact must contain v6_decision_records")
    return [dict(row) for row in value]


def _registration_context(
    path: str | Path, *, market_date: str, census: list[dict[str, Any]],
    decisions: list[dict[str, Any]], receipt: dict[str, Any],
) -> dict[str, Any]:
    """Authenticate the optional actual-source producer handoff."""
    context_path = Path(path).resolve()
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Ops06AdapterError("observational registration context is unreadable") from exc
    if (
        not isinstance(context, dict)
        or context.get("schema_version") != "dawnstrike.observation.scope_declaration.v1"
    ):
        raise Ops06AdapterError("observational registration context schema is invalid")
    if context.get("market_date") != market_date:
        raise Ops06AdapterError("observational registration context date mismatch")
    registration = context.get("registration") or {}
    if (
        registration.get("production_registration_performed") is not False
        or registration.get("external_approval") is not False
    ):
        raise Ops06AdapterError("observational context cannot confer production approval")
    receipt_sha = str(registration.get("receipt_sha256") or "").lower()
    receipt_path = context_path.parent / "registration-receipt.json"
    if (
        not _HEX64.fullmatch(receipt_sha)
        or not receipt_path.is_file()
        or _sha_bytes(receipt_path.read_bytes()) != receipt_sha
    ):
        raise Ops06AdapterError("observational registration receipt is not content-bound")
    sources = context.get("source_artifacts") or {}
    if not isinstance(sources, dict) or not sources:
        raise Ops06AdapterError("observational source artifacts are missing")
    for name, item in sources.items():
        if not isinstance(item, dict):
            raise Ops06AdapterError(f"observational source artifact is invalid: {name}")
        source = Path(str(item.get("path") or ""))
        declared = str(item.get("sha256") or "").lower()
        if not source.is_file():
            raise Ops06AdapterError(f"observational source artifact changed: {name}")
        if (
            name == "source_db"
            and item.get("hash_semantics") == "read_only_rowset_at_extraction_time"
        ):
            if not _HEX64.fullmatch(str(item.get("rowset_sha256") or "")):
                raise Ops06AdapterError("observational source DB row-set identity is invalid")
            continue
        if not _HEX64.fullmatch(declared) or _sha_bytes(source.read_bytes()) != declared:
            raise Ops06AdapterError(f"observational source artifact changed: {name}")
    config_item = sources.get("source_config")
    if (
        not isinstance(config_item, dict)
        or str(config_item.get("sha256") or "").lower()
        != str((receipt.get("source_lineage") or {}).get("source_config_sha256") or "").lower()
    ):
        raise Ops06AdapterError("observational source config differs from capture config")
    census_rows = context.get("scopes", {}).get("original_small_cap_gap")
    if not isinstance(census_rows, list) or not census_rows:
        raise Ops06AdapterError("observational full census is missing")
    full_symbols = {str(row.get("symbol") or "").upper() for row in census_rows}
    if len(full_symbols) != len(census_rows) or any(not s for s in full_symbols):
        raise Ops06AdapterError("observational full census is duplicate or malformed")
    sample_symbols = {str(row.get("symbol") or "").upper() for row in census}
    if not sample_symbols <= full_symbols:
        raise Ops06AdapterError("OPS05 sampled census is outside authenticated full census")
    decision_info = context.get("decision_artifact") or {}
    decision_path = Path(str(decision_info.get("path") or ""))
    decision_hash = str(decision_info.get("sha256") or "").lower()
    if (
        not decision_path.is_file()
        or not _HEX64.fullmatch(decision_hash)
        or _sha_bytes(decision_path.read_bytes()) != decision_hash
    ):
        raise Ops06AdapterError("observational decision artifact is not content-bound")
    bound_decisions = _decisions(json.loads(decision_path.read_text(encoding="utf-8")))
    if bound_decisions != decisions:
        raise Ops06AdapterError("caller decision artifact differs from registered decision rows")
    source_identity = context.get("source_identity") or {}
    if (
        source_identity.get("market_date") != market_date
        or not source_identity.get("handoff_run_id")
    ):
        raise Ops06AdapterError("observational source identity is incomplete")
    snapshot_item = sources.get("snapshot")
    snapshot_path = Path(str((snapshot_item or {}).get("path") or ""))
    expected_member_hash = str(source_identity.get("member_source_rowset_sha256") or "").lower()
    if not snapshot_path.is_file() or not _HEX64.fullmatch(expected_member_hash):
        raise Ops06AdapterError("observational member source row-set identity is missing")
    with snapshot_path.open(newline="", encoding="utf-8") as handle:
        snapshot_rows = list(csv.DictReader(handle))
    snapshot_members: dict[str, dict[str, str]] = {}
    for row in snapshot_rows:
        symbol = str(row.get("ticker") or "").strip().upper()
        if not symbol or symbol in snapshot_members:
            raise Ops06AdapterError("observational member source row-set is malformed")
        snapshot_members[symbol] = row
    canonical_members = [
        {
            "ticker": symbol,
            "company": str(row.get("company") or "").strip(),
            "source": str(row.get("source") or ""),
            "source_timestamp": str(row.get("source_timestamp") or ""),
            "as_of_timestamp": str(row.get("as_of_timestamp") or ""),
            "extracted_at": str(row.get("extracted_at") or ""),
        }
        for symbol, row in sorted(snapshot_members.items())
    ]
    if _sha_json(canonical_members) != expected_member_hash:
        raise Ops06AdapterError("observational member source row-set hash is invalid")
    if {str(row.get("symbol") or "").upper() for row in census_rows} != set(snapshot_members):
        raise Ops06AdapterError("observational full census does not match member source row-set")
    return {
        "path": str(context_path),
        "sha256": _sha_bytes(context_path.read_bytes()),
        "versioned_universe_id": registration.get("versioned_universe_id"),
        "source_identity": source_identity,
        "full_census_count": len(census_rows),
        "sample_count": len(census),
        "decision_artifact_sha256": decision_hash,
        "registration_receipt_sha256": receipt_sha,
    }


def _capture_binding(root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    path = root / "capture-binding.json"
    if not path.is_file():
        raise Ops06AdapterError(
            "OPS05 capture binding is missing; legacy archive is UNVERIFIED_PROVENANCE"
        )
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Ops06AdapterError("OPS05 capture binding is unreadable") from exc
    if not isinstance(binding, dict):
        raise Ops06AdapterError("OPS05 capture binding must be an object")
    declared = str(binding.get("binding_sha256") or "").lower()
    unsigned = {key: value for key, value in binding.items() if key != "binding_sha256"}
    if not _HEX64.fullmatch(declared) or _sha_json(unsigned) != declared:
        raise Ops06AdapterError("OPS05 capture binding hash is invalid")
    lineage = receipt.get("source_lineage") or {}
    expected = {
        "market_date": receipt.get("market_date"),
        "provider": lineage.get("provider"),
        "feed": lineage.get("feed"),
        "source_config_sha256": lineage.get("source_config_sha256"),
        "capture_receipt_sha256": lineage.get("capture_receipt_sha256"),
        "raw_event_stream_sha256": receipt.get("raw_event_stream_sha256"),
    }
    if any(binding.get(key) != value for key, value in expected.items()):
        raise Ops06AdapterError("OPS05 capture binding does not match receipt identity")
    return binding


def _page_contract(page: dict[str, Any]) -> dict[str, Any]:
    return {
        key: page.get(key)
        for key in (
            "window",
            "page_number",
            "endpoint",
            "provider",
            "feed",
            "item_count",
            "raw_payload_hash_sha256",
            "request_start",
            "request_end",
        )
    }


def _validate_pages(
    receipt: dict[str, Any], binding: dict[str, Any]
) -> dict[tuple[str, int], dict[str, Any]]:
    pages = receipt.get("pages")
    if not isinstance(pages, list) or not pages:
        raise Ops06AdapterError("OPS05 receipt has no page bodies")
    binding_pages = binding.get("pages")
    if binding_pages != [_page_contract(page) for page in pages]:
        raise Ops06AdapterError("OPS05 capture binding page census changed")
    result: dict[tuple[str, int], dict[str, Any]] = {}
    windows = receipt.get("windows") or {}
    for page in pages:
        if not isinstance(page, dict):
            raise Ops06AdapterError("OPS05 page receipt is invalid")
        body = page.get("raw_payload_items")
        if not isinstance(body, list):
            raise Ops06AdapterError("OPS05 page body is missing")
        page_hash = str(page.get("raw_payload_hash_sha256") or "").lower()
        provider_hash = str(page.get("provider_raw_payload_hash_sha256") or "").lower()
        if not _HEX64.fullmatch(page_hash) or not _HEX64.fullmatch(provider_hash):
            raise Ops06AdapterError("OPS05 page body identity is invalid")
        if _sha_json(body) != page_hash or page.get("item_count") != len(body):
            raise Ops06AdapterError("OPS05 page body does not match its recorded hash")
        window_name = str(page.get("window") or "")
        window = windows.get(window_name)
        if not isinstance(window, dict):
            raise Ops06AdapterError("OPS05 page window is not declared")
        if page.get("request_start") != window.get("start_utc"):
            raise Ops06AdapterError("OPS05 page request start is not bound to its window")
        if page.get("request_end") != window.get("end_utc"):
            raise Ops06AdapterError("OPS05 page request end is not bound to its window")
        provider = str(receipt["source_lineage"].get("provider") or "")
        feed = str(receipt["source_lineage"].get("feed") or "")
        if page.get("provider") != provider or page.get("feed") != feed:
            raise Ops06AdapterError("OPS05 page source identity differs from receipt")
        endpoint = str(page.get("endpoint") or "")
        if endpoint not in {"bars", "corporate_actions"}:
            raise Ops06AdapterError("OPS05 page endpoint is not supported")
        number = page.get("page_number")
        if not isinstance(number, int) or number < 0:
            raise Ops06AdapterError("OPS05 page number is invalid")
        key = (window_name, number)
        if key in result:
            raise Ops06AdapterError("OPS05 page identity is duplicated")
        result[key] = page
    return result


def _raw_value(item: dict[str, Any], long_name: str, short_name: str) -> Any:
    return item.get(short_name, item.get(long_name))


def _validate_raw_rows(
    receipt: dict[str, Any],
    raw_rows: list[dict[str, Any]],
    pages: dict[tuple[str, int], dict[str, Any]],
    binding: dict[str, Any],
) -> None:
    lineage = receipt["source_lineage"]
    expected_source = f"{lineage['provider']}:{lineage['feed']}"
    capture = str(lineage["capture_receipt_sha256"]).lower()
    for row in raw_rows:
        if row.get("source") != expected_source:
            raise Ops06AdapterError("OPS05 bar source identity is not receipt-bound")
        if str(row.get("source_artifact_hash_sha256") or "").lower() != capture:
            raise Ops06AdapterError("OPS05 bar source artifact is not capture-bound")
        window = str(row.get("source_window") or "")
        number = row.get("source_page_number")
        index = row.get("source_page_row_index")
        if not isinstance(number, int) or not isinstance(index, int):
            raise Ops06AdapterError("OPS05 bar source row mapping is missing")
        page = pages.get((window, number))
        if page is None or page.get("endpoint") != "bars":
            raise Ops06AdapterError("OPS05 bar source page is not receipt-bound")
        body = page["raw_payload_items"]
        if index < 0 or index >= len(body):
            raise Ops06AdapterError("OPS05 bar source row offset is invalid")
        source_item = body[index]
        if not isinstance(source_item, dict):
            raise Ops06AdapterError("OPS05 bar source payload is invalid")
        if row.get("source_page_hash_sha256") != page.get("raw_payload_hash_sha256"):
            raise Ops06AdapterError("OPS05 bar page hash binding is invalid")
        if row.get("source_payload_sha256") != _sha_json(source_item):
            raise Ops06AdapterError("OPS05 bar payload is not bound to its source row")
        symbol = str(source_item.get("symbol") or source_item.get("S") or "").upper()
        timestamp = _raw_value(source_item, "timestamp", "t")
        payload = row.get("payload") or {}
        if symbol != str(row.get("symbol") or "").upper() or _utc(timestamp) != _utc(
            row.get("event_time")
        ):
            raise Ops06AdapterError("OPS05 normalized bar does not match source row")
        for payload_key, long_name, short_name in (
            ("open", "open", "o"),
            ("high", "high", "h"),
            ("low", "low", "l"),
            ("close", "close", "c"),
            ("volume", "volume", "v"),
        ):
            if payload.get(payload_key) != _raw_value(source_item, long_name, short_name):
                raise Ops06AdapterError("OPS05 normalized payload does not match source row")
    expected_stream = binding.get("raw_event_stream_sha256")
    if expected_stream != receipt.get("raw_event_stream_sha256"):
        raise Ops06AdapterError("OPS05 raw stream identity is not capture-bound")


def _manifest(
    receipt: dict[str, Any], census: list[dict[str, Any]], raw_hash: str, generation: str,
    registration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_date = str(receipt["market_date"])
    full = receipt["windows"]["full_session"]
    session_id = str(full["session_id"])
    panel = []
    for symbol in receipt["symbols"]["reference_panel"]:
        panel.append(
            {
                "symbol": symbol,
                "membership": "selected",
                "reason_codes": ["fixed_reference_panel"],
                "required_inputs": ["bars"],
            }
        )
    original = []
    for row in census:
        original.append(
            {
                "symbol": str(row["symbol"]).upper(),
                "membership": str(row["membership"]).lower(),
                "reason_codes": ["ops05_full_census"],
                "required_inputs": ["bars"],
            }
        )
    entries = panel + original
    source_config = str(receipt["source_lineage"]["source_config_sha256"]).lower()
    capture = str(receipt["source_lineage"]["capture_receipt_sha256"]).lower()
    if not _HEX64.fullmatch(source_config) or not _HEX64.fullmatch(capture):
        raise Ops06AdapterError("OPS05 source identities must be SHA-256")
    manifest = {
        "schema_version": "dawnstrike.observation.universe.v1",
        "session_id": session_id,
        "market_date": market_date,
        "decision_deadline": full["start_utc"],
        "universe_generation_id": generation,
        "source_config_sha256": source_config,
        "session_close_identity": {
            "close_at": full["end_utc"],
            "early_close": False,
            "calendar_version": "XNYS-OPS05-v1",
        },
        "collection_expectation": {
            "status": "COMPLETE",
            "receipt_id": capture,
            "expected_entries": len(entries),
        },
        "source_lineage": {
            "provider": str(receipt["source_lineage"]["provider"]),
            "feed": str(receipt["source_lineage"]["feed"]),
            "capture_receipt_sha256": capture,
            "session_id": session_id,
            "source_config_sha256": source_config,
            "raw_events_sha256": raw_hash,
            "raw_events_path": "raw-events.jsonl",
        },
        "scopes": {"original_small_cap_gap": original, "liquid_reference_panel": panel},
    }
    if registration is not None:
        manifest["observational_registration"] = registration
        manifest["source_lineage"]["collector_capture_receipt_sha256"] = capture
        manifest["source_lineage"]["registration_source_sha256"] = registration["sha256"]
    return manifest


def _events(
    receipt: dict[str, Any], raw_rows: list[dict[str, Any]], session_id: str
) -> list[dict[str, Any]]:
    panel = {str(item).upper() for item in receipt["symbols"]["reference_panel"]}
    output = []
    valid_sessions = {
        str(receipt["windows"][name]["session_id"]) for name in ("full_session", "prior_close")
    }
    close_at = _utc(receipt["windows"]["full_session"]["end_utc"])
    for row in raw_rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise Ops06AdapterError("OPS05 bar payload is missing")
        symbol = str(row.get("symbol") or "").upper()
        source_session_id = str(row.get("session_id") or "")
        if not symbol or source_session_id not in valid_sessions:
            raise Ops06AdapterError("OPS05 bar session or symbol identity mismatch")
        for key in ("open", "high", "low", "close"):
            _finite(payload.get(key))
        event_time = _utc(row.get("event_time"))
        available_at = _utc(row.get("available_at"))
        if available_at < event_time:
            raise Ops06AdapterError("delayed bar availability precedes event time")
        output.append(
            {
                "event_id": str(row.get("event_id") or ""),
                "session_id": session_id,
                "source_session_id": source_session_id,
                "scope": "liquid_reference_panel" if symbol in panel else "original_small_cap_gap",
                "symbol": symbol,
                "source": (
                    f"{receipt['source_lineage']['provider']}:{receipt['source_lineage']['feed']}"
                ),
                "kind": "bars",
                "event_time": event_time.isoformat(),
                "available_at": available_at.isoformat(),
                "source_artifact_hash_sha256": row.get("source_artifact_hash_sha256"),
                "timing_class": "delayed_historical_label_only",
                "decision_eligible": False,
                "close_proxy": source_session_id == session_id and event_time < close_at,
                "payload": {
                    "o": payload["open"],
                    "h": payload["high"],
                    "l": payload["low"],
                    "c": payload["close"],
                    "v": payload.get("volume"),
                },
            }
        )
    return output


def _horizon_summary(
    decision: dict[str, Any], events: list[dict[str, Any]], close_at: datetime
) -> list[dict[str, Any]]:
    decision_at = _utc(decision.get("decision_at"))
    ordered = sorted(
        (row for row in events if row["symbol"] == str(decision.get("ticker") or "").upper()),
        key=lambda row: _utc(row["event_time"]),
    )
    start = next((row for row in ordered if _utc(row["event_time"]) >= decision_at), None)
    result = []
    for minutes in _HORIZONS:
        target = decision_at + timedelta(minutes=minutes)
        if target >= close_at:
            result.append(
                {
                    "horizon_minutes": minutes,
                    "status": "CENSORED_BY_SESSION_CLOSE",
                    "target_at": target.isoformat(),
                    "price_offset": None,
                    "denominator": None,
                    "gross_return_pct": None,
                    "cost_excluded": True,
                }
            )
            continue
        end = next((row for row in ordered if _utc(row["event_time"]) >= target), None)
        if start is None or end is None:
            result.append(
                {
                    "horizon_minutes": minutes,
                    "status": "MISSING_START_OR_TARGET_BAR",
                    "target_at": target.isoformat(),
                    "price_offset": None,
                    "denominator": None,
                    "gross_return_pct": None,
                    "cost_excluded": True,
                }
            )
            continue
        denominator = _finite(start["payload"]["o"])
        close = _finite(end["payload"]["c"])
        result.append(
            {
                "horizon_minutes": minutes,
                "status": "MATURED_DELAYED_LABEL",
                "target_at": target.isoformat(),
                "price_offset": close - denominator,
                "denominator": denominator,
                "gross_return_pct": (close / denominator - 1.0) * 100.0,
                "cost_excluded": True,
            }
        )
    return result


def adapt_ops05_to_r3(
    *,
    observation_root: str | Path,
    decision_artifact: str | Path,
    output_root: str | Path | None = None,
    as_of: str | None = None,
    write_bytes: Any | None = None,
    registration_context: str | Path | None = None,
) -> dict[str, Any]:
    """Verify OPS05 bytes, emit R3-shaped artifacts, and run the existing consumer."""
    root = Path(observation_root).resolve()
    output = Path(output_root or (root / "r3-adapter")).resolve()
    receipt_path = root / "receipt.json"
    bars_path = root / "raw-bars.jsonl"
    census_path = root / "universe-census.json"
    required = [receipt_path, bars_path, census_path, Path(decision_artifact).resolve()]
    if any(not path.is_file() for path in required):
        raise Ops06AdapterError("OPS05 adapter input is incomplete")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        receipt.get("schema_version") != "dawnstrike.ops05.historical_bars_receipt.v1"
        or receipt.get("status") != "CAPTURED"
    ):
        raise Ops06AdapterError("OPS05 receipt is not a captured historical archive")
    raw_bytes = bars_path.read_bytes()
    raw_hash = _sha_bytes(raw_bytes)
    raw_rows = [json.loads(line) for line in raw_bytes.decode("utf-8").splitlines() if line.strip()]
    if _sha_json(raw_rows) != receipt.get("raw_event_stream_sha256"):
        raise Ops06AdapterError("OPS05 raw event content does not match receipt")
    binding = _capture_binding(root, receipt)
    page_map = _validate_pages(receipt, binding)
    _validate_raw_rows(receipt, raw_rows, page_map, binding)
    census = json.loads(census_path.read_text(encoding="utf-8"))
    decisions = _decisions(json.loads(Path(decision_artifact).read_text(encoding="utf-8")))
    registration = (
        _registration_context(
            registration_context, market_date=str(receipt["market_date"]),
            census=census, decisions=decisions, receipt=receipt,
        )
        if registration_context is not None else None
    )
    session_id = str(receipt["windows"]["full_session"]["session_id"])
    if registration is not None:
        generation = str(registration.get("versioned_universe_id") or "")
        if not generation:
            raise Ops06AdapterError("observational registration universe identity is missing")
    else:
        generation = "ops05-r3-" + _sha_json(
            {
                "market_date": receipt["market_date"],
                "source": receipt["source_lineage"],
                "raw": raw_hash,
            }
        )[:24]
    manifest = _manifest(receipt, census, raw_hash, generation, registration)
    events = _events(receipt, raw_rows, session_id)
    output.mkdir(parents=True, exist_ok=True)
    writer = write_bytes or (lambda path, data: path.write_bytes(data))
    writer(
        output / "universe-manifest.json",
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
    )
    writer(
        output / "raw-events.jsonl",
        b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in events),
    )
    producer = {
        "schema_version": "dawnstrike.observation.producer_receipt.v1",
        "status": "READY",
        "session_id": session_id,
        "manifest_sha256": _sha_json(manifest),
        "source_config_sha256": manifest["source_config_sha256"],
        "capture_receipt_sha256": receipt["source_lineage"]["capture_receipt_sha256"],
        "raw_events_sha256": _sha_bytes((output / "raw-events.jsonl").read_bytes()),
        "timing_class": "delayed_historical_label_only",
        "feature_decision_eligible": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if registration is not None:
        producer["observational_registration"] = {
            "context_sha256": registration["sha256"],
            "receipt_sha256": registration["registration_receipt_sha256"],
            "versioned_universe_id": registration["versioned_universe_id"],
            "full_census_count": registration["full_census_count"],
            "decision_artifact_sha256": registration["decision_artifact_sha256"],
            "external_approval": False,
            "production_registration_performed": False,
        }
    writer(
        output / "producer-receipt.json",
        (json.dumps(producer, sort_keys=True, indent=2) + "\n").encode(),
    )
    target_contract = {
        "target_id": "one_minute_bar_close_return_60m_gross",
        "horizon_minutes": 60,
        "units": "percent",
        "price_basis": "one_minute_bar_close_proxy",
        "return_basis": "one_minute_bar_close_return_60m_gross",
        "evidence_class": "observational_one_minute_bar_close_return_60m_gross",
    }
    packet = build_observation_dataset(
        manifest=manifest,
        producer_receipt=producer,
        raw_events=output / "raw-events.jsonl",
        decisions=decisions,
        as_of=as_of,
        target_contract=target_contract,
        observational_universe_id=(registration or {}).get("versioned_universe_id"),
    )
    close_at = _utc(manifest["session_close_identity"]["close_at"])
    horizons_by_identity: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for decision in decisions:
        decision_id = str(decision.get("decision_id") or "").strip()
        symbol = str(decision.get("ticker") or "").strip().upper()
        decision_at_value = str(decision.get("decision_at") or "").strip()
        if not decision_id or not symbol or not decision_at_value:
            raise Ops06AdapterError("decision identity is missing for horizon binding")
        identity = (decision_id, symbol, decision_at_value)
        if identity in horizons_by_identity:
            raise Ops06AdapterError("decision identity is duplicated for horizon binding")
        summary = _horizon_summary(decision, events, close_at)
        horizons_by_identity[identity] = [
            {
                **row,
                "decision_id": decision_id,
                "symbol": symbol,
                "decision_at": decision_at_value,
            }
            for row in summary
        ]
    for label in packet.get("labels", []):
        label["eligibility_state"] = "OBSERVATIONAL_TARGET_ELIGIBLE"
        label["target_contract"] = dict(target_contract)
        label_identity = (
            str(label.get("decision_id") or "").strip(),
            str(label.get("ticker") or "").strip().upper(),
            str(label.get("decision_at") or "").strip(),
        )
        horizon_definitions = horizons_by_identity.get(label_identity)
        if not horizon_definitions:
            raise Ops06AdapterError("label decision identity is not bound to horizon definitions")
        label["horizon_definitions"] = [dict(row) for row in horizon_definitions]
        label["horizon_binding"] = {
            "decision_id": label_identity[0],
            "symbol": label_identity[1],
            "decision_at": label_identity[2],
            "binding": "exact_decision_identity",
        }
    horizons = list(horizons_by_identity.values())
    packet["ops06_adapter"] = {
        "status": "ADAPTED",
        "raw_source_sha256": raw_hash,
        "page_hashes_verified": len(receipt.get("pages", [])),
        "full_census_count": len(census),
        "decision_count": len(decisions),
        "horizon_minutes": list(_HORIZONS),
        "horizon_binding": "exact_decision_identity_per_label",
        "target_contract": target_contract,
        "timing_class": "delayed_historical_label_only",
        "feature_decision_eligible": False,
        "costs_included": False,
        "broker_execution_enabled": False,
    }
    packet["horizon_summary"] = horizons
    writer(
        output / "observation-dataset.json",
        (json.dumps(packet, sort_keys=True, indent=2) + "\n").encode(),
    )
    return {
        "status": "READY",
        "manifest": manifest,
        "producer_receipt": producer,
        "raw_events": output / "raw-events.jsonl",
        "decisions": decisions,
        "as_of": as_of,
        "target_contract": target_contract,
        "adapter_output_root": str(output),
        "adapter_packet": packet,
        "decision_artifact_path": str(Path(decision_artifact).resolve()),
        "registration_context": registration,
    }


__all__ = ["Ops06AdapterError", "adapt_ops05_to_r3"]
